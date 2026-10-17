# BidBrain: how it works, and why each choice was made

A guide for anyone defending this project in front of judges. Each section answers "what does it do", "why this
way", and "what would break otherwise".

**The thesis in one line:** BidBrain is a company brain that turns changing evidence into auditable business
decisions and coordinated actions; the construction bid desk is the proving ground. The loop: new document → claim
with source and page → adjudication by policy (or a human) → graph impact traversal → deterministic requalification
→ accountable actions → company record → a clean workspace starts from the new fact. Sections 3, 4, 7a and 7b
cover each step.

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
* **Bitemporal: when we learned it, and when we stopped believing it.** Every claim gets `recorded_at` (transaction
  time: when the graph learned it). When a decision sets a claim aside, its status becomes `superseded` or `rejected`
  and it gets `invalidated_at`; its value stays. Separately, `observed_day` / `valid_from_day` / `valid_to_day` hold
  the document's own dates. `fact_as_of(key, predicate, as_of)` (`tools/impact.py`) answers "what did the company
  believe at time T": among the claims recorded by T and not invalidated by T, the one a decision selected wins. The
  workflow eval checks it 5 ms before and after a corrigendum.
* **Issuer is whoever issued the document.** Our own completion record quoting the client's grading is a *company*
  statement. The planted-conflict eval caught us attributing it to the client, which made the recency rule fire
  wrongly 11 times.

## 4. Conflict policy (`conflicts/engine.py`)

1. **Detect.** Two live claims about the same (subject, predicate) disagree **beyond the coarser source's
   precision**. The portfolio prints 0.01 Cr, so ₹193,299,999 against ₹193,300,000 is rounding, not a conflict. A
   detection writes `(a)-[:CONTRADICTS]->(b)`.
2. **Resolve**, in order:
   - **Company record.** One of the claims was written from a published `OrgFact` (section 7b): policy
     `company_record`, and the rationale names the fact. A fact the company already approved is not re-litigated.
   - **Learned rule.** A previous human decision in this session, generalised into `(:PolicyRule)`, matches the
     predicate and the pair of source kinds.
   - **Company rule.** A rule someone promoted to the shared `bidbrain_org` graph matches (policy `org_rule`), and this
     session has turned on "Use company memory" (off by default). The
     Decision cites it with `(d)-[:APPLIED_ORG_RULE]->(:OrgRuleRef)`.
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
   a failing clause pass. The case's overall recommendation is recomputed. Each flip is written as
   `(:Finding {kind:'ripple'})-[:CAUSED_BY]->(:Decision)` on the clause, which is what lets `change_impact` read the
   chain back later.
5. **Act.** The ripple creates one `review_bid` action per case whose recommendation changed, and the decision gets a
   `publish_to_company_record` action (section 7a).

**Organic conflicts.** The corpus's audited statements print a "previous year" column that disagrees with the
previous year's own statement on 114 fields (the generator was not internally consistent). All 114 are
auto-resolved at boot, with the own statement (95) beating the comparative column (50). They are why the turnover
clause can explain which figure it used.

## 5. Tools (`bidbrain/tools/`): why tools instead of free Cypher

* The Q&A agent gets **24 named tools** (a subset of the 31 in the registry) rather than writing Cypher; a read-only
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
    (`WORKED_FOR` / `ENGAGED`), which is standard practice. `algo.betweenness` samples source nodes even when
    `samplingSize` exceeds the 68 nodes, so its ranking changes with the seed (a 32-seed mean still swaps ranks 4-6).
    `key_people` therefore computes **exact** Brandes betweenness in one Cypher query: `allShortestPaths` over
    `WORKED_FOR*` for every pair, each inner node credited 1/n of the pair's n shortest paths (~0.1 s; identical to
    a Python Brandes). The sampled procedure is reported beside it as a cross-check (top 3 identical, 9/10 of the top 10).
  - **WCC as an evidence-integrity check.** `isolated_entities` runs `algo.WCC` over the evidence edges only
    (`scope:'provenance'` or `'corpus'`). Everything outside the main component is a finding: documents no fact
    cites, the client we bill but hold no completion certificate for, staff with credentials who never led a work.
  - **Compliance audit.** `compliance_check` re-tests each of the 491 `Requirement` rows the company marked MET in
    its past compliance matrices, on the date the matrix was signed or submitted, with the same `qualify_clause`
    rules the bid team uses: 318 checkable, 194 agree, 119 disagree, 5 unconfirmed. Most disagreements are probably
    the synthetic generator's, not the company's, and the tool reports them as `likely_generator_artifacts`: all
    52 ISO disagreements are 2019-22 matrices citing certificates whose `initial_date` is 2022; all 13 PMP ones
    predate the single issue date (2021-03-10) every PMP on file shares; all 40 plant ones state 210 "owned assets",
    which is the whole register (154 owned + 56 leased, including plant bought after the bid). The 14 turnover
    disagreements fit no such pattern.
  - **SPpaths with `relDirection:'both'`.** Explains how two entities connect.
  - **LabelPropagation.** `algo.labelPropagation` takes no edge weights (it rejects any weight key, and parallel
    edges do not change its result), so the weighting goes into the projection. `OVERLAPS_CLIENTS` links two project
    leads when they share at least 1.5× the clients the configuration model (modularity's null model) expects,
    `shared(a,b) >= 1.5 · d(a)·d(b)/2m`. Label propagation over it finds 3 client circles (15 / 12 / 10 people,
    2 singletons) with **modularity 0.203** against the full co-client graph; random partitions with the same sizes
    score −0.021, and a greedy local-move optimiser reaches 0.21. The split is unchanged for any factor from 1.2 to
    1.6. The earlier projections scored 0.107 ("≥ 2 shared clients and Jaccard ≥ 0.25") and 0.036 (clients sharing
    ≥ 2 leads); label propagation run directly on any client-client projection, with or without a Jaccard filter,
    either merged everything or shattered it (≤ 0.14). `of:'clients'` therefore puts each client in the people
    circle holding most of its leads: modularity 0.175 (random −0.039; greedy 0.18).
  - **One-query chains.** `trace_from_credential` runs the five-hop credential → person → work → client →
    portfolio → certificate chain as a single Cypher query. `depends_on` is a variable-length match,
    `(x)-[:FROM|EVIDENCED_BY|HAS_REFERENCE|HOLDS|BASED_ON|SELECTS|CITES|...*1..4]->(:Document {doc_id:$doc})`, that
    returns everything resting on one document and which facts it is the only source for. Most other tools are
    1-2-hop queries composed in Python.
* **One registry, three consumers.** The same tool functions are called by the agents, exposed over MCP, and replayed
  by the eval oracle. MCP exposes the 30 read-only tools (run as `GRAPH.RO_QUERY` on the base graph, annotated
  `readOnlyHint`) plus one acting tool, `qualify_tender`, which runs the tender team on a session's own graph copy
  and refuses any other graph. The registry's write tool `case_open` is not exposed.
* **Change impact** (`tools/impact.py::change_impact`). Given a document, it reads from the graph alone: the claims
  the document carried, the decisions they triggered (policy, before → after), the ripple findings those decisions
  caused on open clauses, and the actions they triggered. The Change impact page is this tool. The workflow eval
  compares its flips with an independent before/after re-run of every open clause (P = R = 1.0 over 9 events).

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
* **Loop** (`agents/loop.py`): about 300 lines, no framework. Every step is emitted as an event (tool, args, ms,
  summary, Cypher, subgraph), and that event stream is what the UI animates. Two guards:
  - **Unasked filters.** Every narrowing filter on `find_portfolio`, `prove_absence` or `receivables` is checked for
    lexical support in what the user asked. An unsupported filter gets a note in the tool result asking for the call
    again without it. This came from a judge's question where an invented "Prime" role and years filter cut the
    answer from ₹2.10 B to ₹0.42 B.
  - **One model per answer.** Later steps prefer the provider bucket that served the first step.
* **Tender team** (`agents/orchestrator.py`):
  - The six agents share **one case subgraph**. A handoff is a graph write the next agent reads, recorded as
    `(:AgentRun)-[:HANDED_OFF_TO]->(:AgentRun)`.
  - A **Planner** (no LLM) reads the clause kinds and writes the plan on the `Case` node: which agents run on which
    clauses. Every agent reads its assignment from there.
  - **Hand-backs.** When the AbsenceAuditor finds that a similar-works clause relies on works with no reference
    letter, it leaves a request on the `Finding`. The EvidenceHunter runs again and re-selects referenced works,
    keeping them only if the clause still passes. A second pass is a second `AgentRun` with `pass_no`.
  - Only the ClauseParser and the Assembler need language, and both fall back to deterministic code, so a demo
    cannot be stopped by a rate limit.
  - **ClauseParser safeguards.** Sample tenders with unedited text use vetted clauses. LLM clauses are mapped onto
    the engine's schema and dry-run against the graph. Where the LLM and the rule parser read a threshold
    differently, the rule parser wins (the LLM once read ₹75 Cr as ₹750 Cr). A clause that still cannot be read
    falls back to the rule parser or becomes an explicit unparsed clause; it never aborts the case.
  - The **Verifier uses no LLM.** It recomputes every verdict from the graph. On a disagreement the EvidenceHunter
    re-derives that clause once, and the case becomes NEEDS REVIEW only if the mismatch persists.

## 7. Memory and tenancy

* **Per-visitor graph.** `GRAPH.COPY bidbrain_base session_<id>` on the first request. The session id lives in the
  browser, so a returning visitor gets their own graph back: cases, decisions, learned rules and conversation. Copies
  are reaped after 24 h idle, with at most 60 live (oldest evicted). Measured cost: about 0.5 s and 29.5 MB of Redis
  memory per copy. `GRAPH.MEMORY USAGE` reports 16 MB for the graph (9 MB of it page chunks with embeddings) and
  3 MB for indexes.
* **Company memory (opt-in).** A learned rule leaves its session only when a bid manager promotes it to the shared
  `bidbrain_org` graph. Nothing writes there implicitly. Each `(:OrgRule)` keeps who promoted it, when, the
  source session as a hash, and a copy of the decision it was learned from. Rules can be retired, and there is a cap
  of 50 active rules.
* **Episodic memory:** past bids (6 tender dossiers parsed into `(:Bid)` nodes with `CITES` / `SECURED_BY` edges;
  `CITES` covers only the Annexure D rows in the tender's own category, 10-14 per bid, because the rest of the
  48-row table is the same padding of top-value works in every dossier; Annexure E reprints all 39 project managers,
  so no `PROPOSES` edge is made from it) and conversation turns `(:Conversation)-[:HAS_TURN]->(:Turn)-[:MENTIONS]->(entity)`. A
  follow-up that names an entity an older turn `MENTIONS` recalls up to 3 such turns before the last-6-turn window.
* **Semantic memory:** the company graph itself.
* **Procedural memory:** `(:PolicyRule)` learned from human decisions, and `(:OrgRule)` once promoted.
* **Shared state between agents:** the case subgraph.
* **Integrity finding.** 5 of the 6 past bids cite works that were not complete at submission (14 in total).
  This is a synthetic-data artifact, but it is exactly the check (`audit_bid`) a real bid auditor runs, and it is a
  one-line traversal.

## 7a. Accountable actions (`company/actions.py`)

* **Why.** A verdict flip is only a recommendation. Someone has to own it, approve it, and do something, and the
  company has to be able to show later what was done.
* **What.** `(:Action {kind, status, owner, ...})` with `FOR_CASE`, `TRIGGERED_BY` (the Decision or Finding that
  caused it), `CITES` (documents), `ASSIGNED_TO` (a Person, when the graph knows one) and `HAS_EVENT` →
  `(:ActionEvent {at, from, to, by, note})`. Kinds: `review_bid` (from a ripple), `request_reference_letter` (from an
  absence finding, assigned to the work's lead), `renew_credential` (from an expiry finding, one per named person),
  `publish_to_company_record` (from a decision).
* **Lifecycle.** `pending → approved → executed`; `reject` from pending or approved. Each transition is a
  compare-and-set (`MATCH ... WHERE a.status IN $frm SET a.status = $to`): a stale transition returns 409; repeating
  the same transition returns the current state with no new event. Execute before approve is 409.
* **Idempotent creation.** `MERGE (a:Action {idem:$idem})` with `idem` = hash(kind, case, trigger, target): a
  retried ripple never doubles the queue.
* **Executors are deterministic code.** `review_bid` re-runs `qualify_clause` on every clause, refuses if a stored
  verdict disagrees, then sets `Case.approved_recommendation`. Letter and renewal requests create an internal
  `(:DocumentRequest {channel:'internal'})`; **no email is sent**, and the result says so. Execute is idempotent:
  the stored result is returned, nothing is re-run.

## 7b. The company record (`company/record.py`)

* **Why.** Workspaces are private copies, so a corrigendum accepted in one would otherwise never reach anyone else.
  Copying whole workspaces would leak private work.
* **Publish.** An approved and executed `publish_to_company_record` action copies exactly one decided fact into
  `bidbrain_org` as `(:OrgFact)`: subject, predicate, value, the claim and its source document (as JSON), the source
  decision and policy, the publishing workspace's hashed tag, the approver and the time. Planted evaluation claims
  are refused.
* **Apply.** Whenever a workspace is opened (`sessions.graph_for`), `record.apply` writes each active fact it has not
  yet seen as a claim plus its source document and lets the conflict engine settle it (policy `company_record`).
  `(:CompanyRecordRef {id})` marks it applied. Unlike company rules, published facts need no opt-in, because they
  passed an approval gate.
* **History and governance.** A newer fact for the same subject and predicate marks the older one `superseded`.
  Only the publishing workspace (or an operator with `ORG_ADMIN_TOKEN`) can retire a fact; a retired fact keeps its
  row with status `retired` and is no longer applied. Workspaces that already applied it keep their decision.
* **Persistence.** `bidbrain_org` is saved in FalkorDB's RDB snapshots and mirrored to `org_memory.json` after every
  publish and retire of a fact (and every promote, retire or contest of a rule); the mirror restores both at boot if
  the graph is missing.
* **Tested.** `tests/test_loop_e2e.py` runs corrigendum → impact → review action → publish → a clean second
  workspace that starts from ₹188 Cr, decided by `company_record`, with no cases, actions or turns from the first.

## 8. Evaluation design

* **Workflow evaluation first** (`eval/workflow.py`, no LLM, throwaway graphs): 79/79 cases across task completion
  (15), impact recall (18, P = R = 1.0 against an independent recompute), adversarial conflicts (16), memory transfer
  (9), isolation (9), temporal (4) and provenance (8). Expectations come from fixtures, separate Cypher or a re-run
  of `qualify_clause`, never from the function under test. The first run was 78/79 and found a real bug (renewal
  actions for people the clause did not rely on), now fixed.
* **Same model, same database, different context strategy:**
  - BidBrain (agent + tools);
  - text-to-Cypher (schema in the prompt, one query);
  - vector RAG (top-8 chunks from FalkorDB's vector index);
  - FalkorDB's GraphRAG-SDK (automatic schema extraction).
* **Gold sources:**
  - **S21** is independent gold.
  - **Q333** is engine-derived near-gold: the old deterministic engine's output (99.775 on the hidden leaderboard),
    reported separately.
  - **ABS / TND** are synthesized absence and tender questions with gold computed by *independent* Cypher and a
    separate re-implementation of the rules (`eval/synth.py`), not by our tools.
* **Tool oracle.** Feed the engine's parsed parameters to our tools: 354/354. This separates "the graph can't
  answer" from "the agent planned badly". It also means Q333 flatters BidBrain, because our tools mirror the engine
  that produced its gold. Lead with S21, ABS and TND.
* **Uncertainty.** Every set reports a 95% Wilson interval (`accuracy_ci95` in `qa_summary.json`). These are single
  runs: between two BidBrain runs, 19 Q333 answers went right→wrong and 17 wrong→right. The current numbers are
  pre-final; the README's benchmark status note says whether the final re-run has landed. The benchmark model
  (Qwen3-32B on vLLM) is not the demo model pool (Groq gpt-oss-120b / qwen3.8-27b / gpt-oss-20b).
* **Team on TND.** The deterministic team scored 136/144, then 144/144 after fixing a parser bug found on the test
  set; the final code scores 144/144. Treat it as tuned on that set.
* **GraphRAG-SDK** used its automatic schema extraction with no hand-written ontology.
* **Conflict engine.** The JSON stream (275 events, 75 of them hard negatives) checks rule consistency. The PDF path
  (render → extract → resolve entity → detect) checks the parts that can really fail. Its first-run numbers (F1
  0.976) are reported next to the post-fix numbers (1.00), with the two bugs it found.

## 9. Likely judge questions

* **"Isn't this just RAG with extra steps?"** No: vector RAG is one of our baselines. It reasons over what it
  retrieved, so on absence questions it answers about the letters it saw. The graph knows the boundary of the set.
* **"Why not let the LLM write Cypher?"** That is the `text2cypher` baseline, and the benchmark shows the gap.
  Curated tools also make arithmetic exact and evidence automatic.
* **"What if the LLM is down?"** Tender qualification, live documents, decisions, change impact, actions, the company
  record and exploration run without it. Only
  free-text Q&A needs it, and it rotates across the Groq model pool. A recorded run can be replayed offline with
  `BIDBRAIN_REPLAY=1`, but that is a separate mode, not a failover step.
* **"Is the conflict F1 of 1.00 meaningful?"** On the JSON path it measures consistency, since we wrote the rules and
  the labels. The PDF path and the hard negatives are the real tests, and we publish the first-run failures.
* **"Does a decision ever reach anyone outside the workspace?"** Only through an approved action
  (`publish_to_company_record`) or an explicit rule promotion. Both carry provenance; both can be withdrawn by their
  publisher.
* **"Can you prove which bids a document affected?"** `change_impact` reads it from the graph, and the workflow eval
  checks those flips against an independent re-run of every open clause (P = R = 1.0, 9 events).
* **"What's new versus the earlier hackathon?"** That project was a regex question parser over a fact table, with no
  graph, no LLM, no agents and no conflicts. Here only its extractors are reused, credited.
