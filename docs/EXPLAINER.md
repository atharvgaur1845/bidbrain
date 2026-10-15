# BidBrain: how it works, and why each choice was made

A guide for anyone defending this project in front of judges. Each section answers "what does it do", "why this
way", and "what would break otherwise".

---

## 1. The data and why it is graph-shaped

* **Corpus.** 687 synthetic documents of one construction company, "National Infrastructure Corp. Ltd." There are
  155 completion certificates (from 28 clients), 155 company completion records, 132 reference letters, 48
  personnel certificates, 39 CVs, 60 bank guarantees, 40 compliance matrices, 7 audited statements, 6 tender
  dossiers, ledgers, BOQ workbooks and more (MIT licence, from the BITS Hackathon dataset).
* **Nothing says which document belongs to which project.** Linking them is the work. A certificate names a
  work and a client, the company record names the project lead, the portfolio gives the Prime/JV role, and a
  reference letter only quotes the work title.
* **Questions need 4–6 joins.** A typical one: person → credential → anchor work → its client → that client's whole
  portfolio → each work's certificate value. The dataset's own `hops` labels say 3–5. Our hop formula reproduces all
  21 of them.

## 2. Ingestion: reused extractors, new graph

* `backend/vendor_bidintel/` is the deterministic PDF/xlsx extraction layer from our earlier hackathon
  (PyMuPDF layout-preserving blocks, label/value parsing, exact Indian money parsing). It outputs `facts.json`. It is
  credited and unchanged, and it contains no FalkorDB code.
* `bidbrain/ingest/load_graph.py` turns facts into the typed graph with batched `UNWIND … CREATE`. It rebuilds from
  scratch in about 2 s, which is why the container builds the graph at boot instead of shipping a database file.
* **Every entity has a uniform `key`.** The claim layer, the case graph and the tools can then address any subject
  the same way: `(s:Label {key:$k})`.
* **Page provenance** (`ingest/pages.py`). The text cache separates pages with form feeds. A value's page is the
  first page containing any of its renderings. The corpus writes money as `INR 33.38 Cr`, `3,338.00 Lakh`,
  `33,38,00,000`, whole lakhs `33,380`, and so on, and dates in at least five formats.
* **Dates are stored twice**, as ISO strings and as integer `*_day` fields. Cypher filters compare integers and
  never depend on the dialect's date functions.
* **Chunks**: one per page, with windows of 1,400 characters. They go into FalkorDB itself with an HNSW vector index
  (bge-small, 384-d) and a full-text index. We found that RediSearch does **not** split tokens on newlines
  (`Work\nAnganwadi` is indexed as one token), which silently lost about 60% of keyword hits. We now index a
  whitespace-folded copy (`ft`).

## 3. The claim layer: why values are not overwritten

* Every contestable scalar is a `(:Claim)` with `predicate`, `value`, `source_doc`, `page`, `issuer`, `source_kind`,
  `authority` and `observed_day`, linked `-[:ABOUT]->` its subject and `-[:FROM]->` its document.
  Examples are a work's value, completion date, grading and role; a person's experience; a bond's status and expiry;
  and each line of each financial statement.
* The entity property (`Work.value`) is a **materialized view** of the claim that currently wins. When a corrigendum
  arrives we add a claim; we never edit history. That is what makes "why did we decide this?" answerable.
* **Issuer is whoever issued the document.** Our own completion record quoting the client's grading is a *company*
  statement. The planted-conflict eval caught us attributing it to the client, which made the recency rule fire
  wrongly 11 times.

## 4. Conflict policy (`conflicts/engine.py`)

1. **Detect.** Two live claims about the same (subject, predicate) disagree **beyond the coarser source's
   precision**. The portfolio prints 0.01 Cr, so ₹193,299,999 against ₹193,300,000 is rounding, not a conflict. A
   detection writes `(a)-[:CONTRADICTS]->(b)`.
2. **Resolve**, in order:
   - **Learned rule.** A previous human decision generalised into `(:PolicyRule)` matches the predicate and the pair
     of source kinds.
   - **Recency.** The same issuer amended its own statement, with a document *at least as formal* as the one it
     replaces (a corrigendum, a re-issued certificate, a revised audited statement), and no other issuer outranks
     it. A client's chatty letter does not silently override its own certificate.
   - **Authority.** A gap of 20 or more on the ladder (client corrigendum 95 … CV 40 … LLM-read prose 30).
   - Otherwise a **resolve card** goes to a human. "Choose + learn rule" creates the PolicyRule.
3. **Record.** A `(:Decision)` with `SELECTS` (winner), `BASED_ON` (all claims), `DECIDED_BY` (agent or user),
   `SUPERSEDES` (earlier decisions on the same fact) and `APPLIED_RULE`. Loser claims become `superseded` (recency)
   or `rejected` (authority). The property is re-materialized and the graph version is bumped.
4. **Ripple** (`tools/case.py::blast_radius`). Find every open clause whose *candidate set* could include the subject,
   re-run it, and report flips. We re-check failing clauses as well as satisfied ones, because a corrigendum can make
   a failing clause pass. The case's overall recommendation is recomputed.

**Organic conflicts.** The corpus's audited statements print a "previous year" column that disagrees with the
previous year's own statement on 114 fields (the generator was not internally consistent). All 114 are
auto-resolved at boot, with the own statement (95) beating the comparative column (50). They are why the turnover
clause can explain which figure it used.

## 5. Tools (`bidbrain/tools/`): why tools instead of free Cypher

* The Q&A agent gets **16 named tools** (a subset of the 25 in the registry) rather than writing Cypher; a read-only
  `cypher_readonly` escape hatch is among them, for questions no tool covers. Each tool is a tested multi-hop traversal with exact
  arithmetic and evidence attached. Raw NL→Cypher is our *baseline* (`text2cypher`), and the benchmark shows the
  difference.
* **`aggregate` takes work keys, not numbers.** It re-reads values from the graph, so the model can neither misadd
  nor inject a hallucinated value.
* **`prove_absence` returns a proof**, not just a count:
  1. the universe, which is the client's `FOR_CLIENT` set;
  2. every work in it attested by three independent views (certificate, company record, portfolio);
  3. **corpus closure**: all 132 letters in the estate are linked to some work, so a missing link cannot be a
     matching miss;
  4. an orphan full-text search for each missing title over reference-letter text, which finds nothing.
* **`qualify_clause` encodes real Indian eligibility rules.**
  - The CPWD similar-works test: 1 work ≥ 80%, or 2 ≥ 60%, or 3 ≥ 40% of the estimated cost.
  - Turnover, using the winning claim and reporting restatements.
  - Key personnel, with credentials valid **on the bid date**. The verdict is `AT_RISK` if the top candidates lapse
    within 90 days.
  - Certifications.
  - Years in business, from the CIN printed on every statement: `U45201WB`**`2005`**`PLC…`.
* **Algorithms** use FalkorDB's built-ins:
  - **Betweenness for the bus factor.** Domain edges are directed (`Work→Person`, `Work→Client`), so no shortest path
    ever passes *through* a person and everyone scored 0. We therefore build an explicit person⇄client projection
    (`WORKED_FOR` / `ENGAGED`), which is standard practice.
  - **WCC.** Finds the client we bill but hold no completion certificate for.
  - **SPpaths with `relDirection:'both'`.** Explains how two entities connect.
  - **LabelPropagation.** Runs on a people-by-shared-clients projection. The synthetic company is very evenly
    staffed, so the output is one big community; we report that as it is.
* **One registry, three consumers.** The same tool functions are called by the agents, exposed over MCP, and replayed
  by the eval oracle.

## 6. Agents

* **Provider layer** (`agents/providers.py`).
  - **Models:** the demo runs on a pool of free Groq models (openai/gpt-oss-120b, qwen/qwen3.8-27b,
    openai/gpt-oss-20b). The benchmark runs every system on one model, Qwen3-32B on vLLM on our HPC cluster. So the
    benchmark compares context strategies on a fixed model; it is not a measurement of the demo's models.
  - **Rate limits:** every (provider, model) pair is its own quota bucket. A 429 parks that bucket until the reset
    time the server reports and the next bucket is tried; when all are parked, the call waits (bounded) for the
    earliest reset instead of failing.
  - **Cache:** every completion is stored in SQLite, and `BIDBRAIN_REPLAY=1` replays recorded runs offline.
  - **Thinking off** (`/no_think`, or `enable_thinking=false` on vLLM). Tool routing does not need hidden reasoning,
    and it burns free-tier tokens.
* **Loop** (`agents/loop.py`): about 100 lines, no framework. Every step is emitted as an event (tool, args, ms,
  summary, Cypher, subgraph), and that event stream is what the UI animates.
* **Tender team** (`agents/orchestrator.py`):
  - The six agents share **one case subgraph**. A handoff is a graph write the next agent reads, recorded as
    `(:AgentRun)-[:HANDED_OFF_TO]->(:AgentRun)`.
  - Only the ClauseParser and the Assembler need language, and both fall back to deterministic code, so a demo
    cannot be stopped by a rate limit.
  - The **Verifier uses no LLM.** It recomputes every verdict from the graph and flags any disagreement.

## 7. Memory and tenancy

* **Per-visitor graph.** `GRAPH.COPY bidbrain_base session_<id>` on the first request. The session id lives in the
  browser, so a returning visitor gets their own graph back: cases, decisions, learned rules and conversation. Copies
  are reaped after 24 h idle, with at most 60 live (oldest evicted).
* **Episodic memory:** past bids (6 tender dossiers parsed into `(:Bid)` nodes with `CITES` / `PROPOSES` /
  `SECURED_BY` edges) and conversation turns `(:Conversation)-[:HAS_TURN]->(:Turn)-[:MENTIONS]->(entity)`.
* **Semantic memory:** the company graph itself.
* **Procedural memory:** `(:PolicyRule)` learned from human decisions.
* **Shared state between agents:** the case subgraph.
* **Integrity finding.** Every one of the 6 past bids cites works that were not complete at submission (56 in total).
  This is a synthetic-data artifact, but it is exactly the check (`audit_bid`) a real bid auditor runs, and it is a
  one-line traversal.

## 8. Evaluation design

* **Same model, same database, different context strategy:**
  - BidBrain (agent + tools);
  - text-to-Cypher (schema in the prompt, one query);
  - vector RAG (top-8 chunks from FalkorDB's vector index);
  - FalkorDB's GraphRAG-SDK (automatic schema extraction).
* **Gold sources:**
  - **S21** is true gold.
  - **Q333** is the old deterministic engine's output (99.775 on the hidden leaderboard), so it is near-gold and
    reported separately.
  - **ABS / TND** are synthesized absence and tender questions with gold computed by *independent* Cypher and a
    separate re-implementation of the rules (`eval/synth.py`), not by our tools.
* **Tool oracle.** Feed the engine's parsed parameters to our tools: 354/354. This separates "the graph can't
  answer" from "the agent planned badly".
* **Conflict engine.** The JSON stream (275 events, 75 of them hard negatives) checks rule consistency. The PDF path
  (render → extract → resolve entity → detect) checks the parts that can really fail. Its first-run numbers (F1
  0.976) are reported next to the post-fix numbers (1.00), with the two bugs it found.

## 9. Likely judge questions

* **"Isn't this just RAG with extra steps?"** No: vector RAG is one of our baselines. It reasons over what it
  retrieved, so on absence questions it answers about the letters it saw. The graph knows the boundary of the set.
* **"Why not let the LLM write Cypher?"** That is the `text2cypher` baseline, and the benchmark shows the gap.
  Curated tools also make arithmetic exact and evidence automatic.
* **"What if the LLM is down?"** Tender qualification, live documents, decisions and exploration run without it. Only
  free-text Q&A needs it, and it rotates across the Groq model pool. A recorded run can be replayed offline with
  `BIDBRAIN_REPLAY=1`, but that is a separate mode, not a failover step.
* **"Is the conflict F1 of 1.00 meaningful?"** On the JSON path it measures consistency, since we wrote the rules and
  the labels. The PDF path and the hard negatives are the real tests, and we publish the first-run failures.
* **"What's new versus the earlier hackathon?"** That project was a regex question parser over a fact table, with no
  graph, no LLM, no agents and no conflicts. Here only its extractors are reused, credited.
