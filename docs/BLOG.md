# Proving what isn't there: building a company brain on FalkorDB

*How we built BidBrain for Graph Hacks: agents that qualify government tenders, prove absences, settle conflicting
documents, and show the path behind every answer.*

> **TL;DR**
> * BidBrain loads a construction company's 687 documents into one FalkorDB graph. A planner and six agents use it to qualify tenders clause by clause, prove what is *missing*, settle documents that contradict each other, and flip a bid from BID to NO-BID when a corrigendum arrives.
> * Same model (Qwen3-32B), same database, four context strategies. Graph tools score **.810** on true-gold questions, **.780** on absence questions and **.896** on tender questions. The best baseline on each set scores .333, .537 and .542.
> * The FalkorDB parts are below as copy-paste recipes: an absence proof, a betweenness projection, a full-text gotcha, `algo.SPpaths`, `GRAPH.COPY` tenancy, and a claim → decision → policy-rule schema.
> * Caveats sit next to the results table, not at the end: single runs on the pre-final code, one near-gold set whose gold our tools mirror, and GraphRAG-SDK run without a domain ontology.

---

## The question that breaks retrieval

A bid manager at an Indian construction firm asks this every week:

> *"Which of Trishakti Power Generation Corporation's completed works have no client reference letter on file?"*

A retrieval-augmented LLM cannot answer it reliably. The retriever fetches the eight chunks most similar to the
question. Those are reference letters, because the question mentions them. A letter that was never written has no
chunk, so it can never be retrieved. The model can only count what it was handed, or give up. On our 164 absence
questions, vector RAG gave a wrong count 80 times and no number 40 times. On this exact question it answered 8. The
right answer is 2.

Answering properly needs **the boundary of the set**: every work this client certified, attested independently,
plus proof that every letter in the estate is accounted for. That is a graph question. Here is what BidBrain's
`prove_absence` tool returns for it, verbatim (no LLM involved):

```json
{"claim": "2 of 19 works in scope have no reference letter on file",
 "verdict": "PROVEN", "universe": 19, "present": 17, "absent_count": 2,
 "absent": [{"key": "robuttarpradeshpkg85",      "title": "ROB — Uttar Pradesh Pkg-85",      "orphan_search_hits": []},
            {"key": "flyoveruttarpradeshpkg43", "title": "Flyover — Uttar Pradesh Pkg-43", "orphan_search_hits": []}],
 "checks": {"boundary_attested_by_3_views": "19/19",
            "corpus_closure": "132/132 letters linked",
            "orphan_search": "no unlinked letter mentions a missing work"}}
```

The rest of this post covers how we got there, and then gives you the Cypher.

## The problem: proof is scattered, and it contradicts itself

Indian public tenders are won on eligibility before price. The CPWD "similar works" rule is typical: in the last
seven years, **one** completed government work worth at least 80% of the estimated cost, **or two** at 60%, **or
three** at 40%, each backed by the client's completion certificate.

Our data is a synthetic company: 687 documents, including 155 completion certificates from 28 clients and 132
reference letters. **Nothing says which document belongs to which project**, so a typical question walks person →
credential → work → client → *that client's whole portfolio* → each certificate's value. Five hops.

The documents also disagree. Each audited statement's "previous year" column contradicts the previous year's own
statement on 114 fields. FY 2024-25 revenue is ₹238.92 Cr in one and ₹232.26 Cr in the other, so a "₹240 Cr in each
of the last three years" clause fails by ₹1.08 Cr or by ₹7.74 Cr, depending on which document you trust.

## Claims, not overwrites

The most important modelling decision was to stop treating property values as facts. Every contestable value is a
**claim node**:

```cypher
(:Claim {predicate:'value', value:2000000000, source_doc:'DOC-CC-113', page:1,
         issuer:'National Special Projects Office', source_kind:'client_certificate',
         authority:90, observed_day:20543})-[:ABOUT]->(:Work)
```

`Work.value` is a **materialized view of the winning claim**. When a corrigendum arrives we add a claim, link the
disagreement with `CONTRADICTS`, and settle it with a `(:Decision)` that records the policy, the rationale, the
decider and both documents. History is never edited. The policy is deliberately boring:

1. a rule a human taught this session;
2. a rule someone promoted to **company memory** (opt-in, with provenance);
3. the same issuer amending itself **with a document at least as formal** (a client's chatty letter must not
   silently override its own certificate);
4. a clear authority gap (client corrigendum 95, certificate 90 … CV 40);
5. otherwise, ask a human.

The schema is reusable outside tenders, so it gets its own recipe at the end.

## Agents that hand off through the graph

Six agents qualify a tender, coordinated by a planner:

1. read the eligibility text into typed clauses;
2. qualify each clause by traversal;
3. prove absences (missing letters, credentials lapsing before the bid date);
4. check the conflict history of every fact the verdict relies on;
5. **re-derive every verdict with no LLM at all**;
6. write the bid memo.

They share no framework memory. Each agent is an `(:AgentRun)` that writes `Clause`, `SATISFIED_BY` and `Finding`
nodes, and the next agent reads them (`HANDED_OFF_TO`). Four of the six are deterministic graph code. Only the
clause parser and the memo writer use an LLM, and both have rule-based fallbacks. The whole team takes about 50 ms
plus LLM time.

The pipeline is not a fixed line. A **Planner** (no LLM) reads the clause kinds and stores on the `Case` node which
agents run on which clauses, and every agent reads its assignment from there. Agents can also hand work back
through the graph. If the AbsenceAuditor finds that the works a clause relies on have no reference letter, it
leaves a request on that `Finding`. The EvidenceHunter then runs again and re-selects referenced works, keeping
the new selection only if the clause still passes. If the Verifier disagrees with a verdict, the EvidenceHunter
re-derives that clause once. The case is marked NEEDS REVIEW only if they still disagree.

![Qualify page: seven clauses of the NEDA expressway tender, each with a verdict and the numbers behind it; the ConflictSentinel notes which audited-statement figure the turnover clause used](img/qualify.png)

Because verdicts are edges to the facts they rely on, **a change can find everything it breaks**. A corrigendum cuts
the Pkg-113 ring road from ₹200 Cr to ₹188 Cr. `blast_radius` walks outward from that work and finds the open clauses
that rely on it, the past bids that cited it, and the tenders it secured. It then re-runs the affected clauses. The
expressway bid flips **from BID to NO-BID**, and the reason is one click away.

![Live documents: the corrigendum is auto-resolved by the recency policy, the similar-works clause flips Pass to Fail, the recommendation flips Bid to No-bid, and the ripple graph shows the case and six past bids that cited the work](img/live.png)

## Tools, not text-to-Cypher

The registry has 28 named tools, and the Q&A agent is offered 21 of them. Raw Cypher is not the interface.
`find_portfolio` (with a `via_work` hop), `prove_absence`, `qualify_clause` and `explain_path` are each a tested
traversal that returns evidence (document and page) and a subgraph for the UI. **Arithmetic never happens in the
model.** `aggregate` takes *work keys*, re-reads values from the graph and computes in exact integer rupees.

The same registry serves our agents, any **MCP** client (`/mcp`, 27 read-only tools), and an evaluation **oracle**.
The oracle replays a deterministic engine's parsed parameters through our tools and reproduces **354 of 354**
reference answers. So any accuracy the LLM agent loses comes from planning, not from missing graph capability.

## Did the graph do the work?

Same model (Qwen3-32B on vLLM, temperature 0), same FalkorDB instance, four context strategies: BidBrain's tools,
LLM text-to-Cypher, vector RAG over FalkorDB's own vector index, and FalkorDB's GraphRAG-SDK. Exact-match accuracy
with Wilson 95% intervals, from `data/eval/qa_summary.json`. **These runs used the pre-final code (summary generated
17 Oct 07:33).** They predate the Planner, the hand-backs, the unasked-filter guard and the latest tools, and will
be re-run on the final code.

| System | S21, true gold (n=21) | ABS, absence (n=164) | TND, tender (n=144) | Q333, near-gold (n=333) |
|---|---|---|---|---|
| **BidBrain (graph tools)** | **.810** [.600, .923] | **.780** [.711, .837] | **.896** [.835, .936] | **.640** [.587, .689] |
| LLM text-to-Cypher | .333 [.172, .546] | .537 [.460, .611] | .264 [.199, .341] | .180 [.143, .225] |
| GraphRAG-SDK (auto schema) | .190 [.077, .400] | .311 [.245, .385] | .542 [.460, .621] | .099 [.071, .136] |
| Vector RAG | .190 [.077, .400] | .268 [.206, .341] | .389 [.313, .470] | .075 [.051, .108] |
| Constant answer (always 0 / majority) | .190 | .427 | .472 | .045 |

**Read these caveats with the table:**

* **Q333's gold is the engine our tools mirror.** It is the output of our earlier deterministic engine, and the tool
  oracle reproduces it 354/354. Q333 therefore flatters BidBrain. Judge us on S21 (true gold) and on ABS and TND,
  whose gold comes from independent Cypher and a separate re-implementation of the rules (`eval/synth.py`).
* **GraphRAG-SDK ran without an ontology.** We used its automatic schema extraction and gave it no hand-written
  ontology. A domain ontology would very likely score higher, so read its row as "generic GraphRAG", not as a ceiling.
* **Single runs.** Between two BidBrain runs on Q333, 19 answers went right→wrong and 17 went wrong→right. S21 has
  only 21 questions, so treat it as a sanity check.
* **BidBrain was re-run once.** The first run scored .313 on TND: the agent's one-line description of
  `qualify_clause` named no parameters, so the model invented keys the tool silently ignored. We documented and
  validated the parameters and re-ran every set. The first run is kept in `data/eval/results/run1/`. The baselines
  do not use these tools, so they were not re-run.
* **Constant answers are informative.** Always answering 0 beats vector RAG and GraphRAG-SDK on ABS. The majority
  verdict beats text-to-Cypher and vector RAG on TND.
* **Cost.** BidBrain spends about 13k tokens per Q333 question (3.9 LLM calls, p50 4.2 s). Text-to-Cypher spends
  about 1.3k tokens (2.5 s), roughly a tenth.

![Accuracy by hop count on Q333: BidBrain 92.0 / 72.3 / 52.4% at 2-3 / 4 / 5-6 hops; text-to-Cypher 32.0 / 18.1 / 14.3; vector RAG 0.0 / 6.4 / 10.1; GraphRAG-SDK 0.0 / 7.4 / 13.8](img/benchmark_hops.png)

The gap widens with the number of joins. GraphRAG-SDK built an 8,781-node graph from the 678 PDFs, but it infers
its schema one chunk at a time. Nothing in that graph knows that a certificate's value and a portfolio's value are
the same fact, or what "the client behind Pkg-113" means. That knowledge is the data model, and the data model is
what a domain graph buys you.

## FalkorDB recipes

Everything below is taken from the repo (`backend/bidbrain/`) and runs on stock `falkordb/falkordb:latest`.

### 1. Prove an absence: boundary, partition, closure, orphan search

A count of zero proves nothing unless you also show that the set was complete and that nothing was missed while
linking documents. `prove_absence` (`tools/proof.py`) runs four queries. The boundary query below is trimmed from
`find_portfolio`, which adds person, category, value and date filters.

```cypher
// 1. Boundary: every work this client certified
MATCH (w:Work)-[:FOR_CLIENT]->(c:Client {key:$client}),
      (w)-[:EVIDENCED_BY {kind:'completion_certificate'}]->(:Document)
RETURN collect(w.key) AS keys

// 2. Partition: which of them have a reference letter (w.views lists the independent sources that attest the work)
MATCH (w:Work) WHERE w.key IN $keys
OPTIONAL MATCH (w)-[h:HAS_REFERENCE]->(r:Document)
RETURN w.key AS key, w.title AS title, w.views AS views,
       collect({doc: r.doc_id, page: h.page}) AS refs

// 3. Corpus closure: every letter in the estate is linked to some work,
//    so a missing link cannot be a letter we failed to match
MATCH (d:Document {doc_type:'reference_letter'})
OPTIONAL MATCH (w:Work)-[:HAS_REFERENCE]->(d)
RETURN count(d) AS letters, sum(CASE WHEN w IS NULL THEN 1 ELSE 0 END) AS unlinked

// 4. Orphan search: full-text for each missing title over reference-letter text only
CALL db.idx.fulltext.queryNodes('Chunk', $q) YIELD node
WITH node WHERE node.doc_id STARTS WITH 'DOC-REF-'
RETURN collect(DISTINCT node.doc_id) AS hits
```

The verdict is `PROVEN` only if `unlinked = 0`, every work in the boundary has all three views, and every orphan
search comes back empty. Otherwise the tool says `UNPROVEN` instead of guessing. The credential and bond variants
use the same pattern with their own closure checks: every credential has a holder and a date, and every bond
secures a tender.

### 2. Betweenness needs an undirected projection

Our domain edges point `(:Work)-[:LED_BY]->(:Person)` and `(:Work)-[:FOR_CLIENT]->(:Client)`. In that directed
graph, no shortest path passes *through* a person. We ran `algo.betweenness` over `LED_BY`/`FOR_CLIENT`, and every
one of the 39 people scored exactly 0. The fix is an explicit person⇄client projection, built at load time
(`ingest/load_graph.py`):

```cypher
MATCH (p:Person)<-[:LED_BY]-(w:Work)-[:FOR_CLIENT]->(c:Client)
WITH p, c, count(w) AS n, sum(w.value) AS v
CREATE (p)-[:WORKED_FOR {works:n, value:v}]->(c), (c)-[:ENGAGED {works:n, value:v}]->(p)
```

```cypher
CALL algo.betweenness({nodeLabels:['Person','Client'], relationshipTypes:['WORKED_FOR','ENGAGED'],
                       samplingSize:100000, samplingSeed:$seed})
YIELD node, score WITH node AS p, score WHERE p:Person RETURN p.key AS key, score
```

There is a second trap. `algo.betweenness` samples source nodes even when `samplingSize` is larger than the graph,
so below the top two, one call's ranking depended on the seed. `key_people` (`tools/algos.py`) now averages 32 seeded
runs. Two disjoint seed sets give the same top 6, and the top 3 match exact Brandes on this projection.

### 3. Full-text: index a whitespace-folded copy

The full-text index does not split tokens on newlines. Our page chunks kept the PDF line breaks, so
`Work\nAnganwadi` was indexed as one token. About 60% of keyword hits silently disappeared. (We reproduced this on
a throwaway graph: a search for `Anganwadi` finds 0 nodes on the raw text and 1 on the folded copy.) Keep the raw text
for display and index a folded copy (`ingest/chunks.py`):

```python
def _ft(text: str) -> str:
    """Whitespace folded, curly quotes and dashes turned into spaces."""
    return " ".join(re.sub(r"[‘’‚“”„–—―…«»]", " ", text).split())

g.run("""UNWIND $rows AS r MATCH (d:Document {doc_id:r.doc_id})
         CREATE (c:Chunk {key:r.id, doc_id:r.doc_id, page:r.page, text:r.text, ft:r.ft,
                          embedding: vecf32(r.emb)})
         CREATE (c)-[:PART_OF]->(d)""", {"rows": rows})
g.run("CALL db.idx.fulltext.createNodeIndex('Chunk', 'ft')")
```

To check your own index, compare `db.idx.fulltext.queryNodes` hit counts with a `WHERE n.text CONTAINS $word` scan
on a few words.

### 4. "How are these connected?": `algo.SPpaths`, not `shortestPath`

Cypher's `shortestPath` is the obvious first try, but FalkorDB rejects both of the forms you would reach for:

```
MATCH p = shortestPath((a)-[*]-(b)) ...   -> FalkorDB currently only supports shortestPaths in WITH or RETURN clauses
RETURN shortestPath((a)-[*]-(b))          -> FalkorDB does not currently support undirected shortestPath traversals
```

`algo.SPpaths` takes a direction, a hop limit and a path count (`explain_path` in `tools/algos.py`):

```cypher
MATCH (a:Person {key:$a}), (b:Client {key:$b})
CALL algo.SPpaths({sourceNode:a, targetNode:b,
                   relTypes:['LED_BY','FOR_CLIENT','HOLDS','SECURES','REQUIRES','HAS_REFERENCE'],
                   relDirection:'both', maxLen:6, pathCount:3})
YIELD path, pathWeight
RETURN [n IN nodes(path) | coalesce(n.name, n.title, n.key)] AS nodes,
       [r IN relationships(path) | type(r)] AS rels, pathWeight AS hops
ORDER BY hops
```

![Explore page: algo.SPpaths returns three shortest paths between Sunita Joshi and the National Expressway Development Authority, through the works each person led](img/explore.png)

### 5. `GRAPH.COPY` as a tenancy model

Every visitor to the demo gets a private copy of the company graph on their first request. One judge resolving a
conflict never changes what the next judge sees. The session id lives in the browser, so a returning visitor gets
back their own cases, decisions and learned rules (`api/sessions.py`, trimmed):

```python
def graph_for(sid: str) -> Graph:
    name = f"session_{sid}"
    with _lock:
        live = db().list_graphs()
        if name not in live:
            _evict_if_full(live)                                     # at most 60 live; oldest idle copy goes
            db().connection.execute_command("GRAPH.COPY", "bidbrain_base", name)
        _last_seen[name] = time.time()                               # a reaper drops copies idle for 24 h
    return Graph(name)
```

**What it costs.** We measured three copies of our base graph (7,544 nodes and 12,839 edges at the time). Each took about 0.5 s, including the
first query, and added **29.5 MB** of Redis memory. `GRAPH.MEMORY USAGE` reports 16 MB for the graph and 3 MB for
its indexes. 9 MB of the graph is page chunks with their 384-d embeddings, which no visitor ever edits. At our cap of
60 sessions, that is about 1.8 GB. If we did it again, we would keep the chunks and vectors in one shared read-only
graph and copy only the part people can change.

## Steal this pattern: claim → decision → policy rule

Any company brain ingests documents that disagree: price lists, contracts, HR records, specs. This schema keeps
every version, settles disagreements explainably, and learns from the people who settle them. The code is in
`conflicts/engine.py`.

```
(:Claim {predicate, value, source_kind, authority, issuer, observed_day, status})
    -[:ABOUT]->(subject)   -[:FROM {page}]->(:Document)
(:Claim)-[:CONTRADICTS {rule, detected_at}]->(:Claim)
(:Decision {subject_key, predicate, policy, rationale, chosen_value, at})
    -[:SELECTS]->(winner:Claim)   -[:BASED_ON]->(every claim)   -[:DECIDED_BY]->(:Agent|:User)
    -[:SUPERSEDES]->(older :Decision on the same fact)   -[:APPLIED_RULE]->(:PolicyRule)
(:PolicyRule {predicate, prefer_kind, over_kind, rationale})-[:LEARNED_FROM]->(:Decision)
```

**Detect.** Group the live claims by (subject, predicate). Compare values in code, using the coarser source's
precision, so that ₹193,299,999 against ₹193,300,000 counts as rounding, not a conflict. Then record the
disagreement once:

```cypher
MATCH (a:Claim {id:$a}), (b:Claim {id:$b}) WHERE NOT (a)-[:CONTRADICTS]-(b)
CREATE (a)-[:CONTRADICTS {rule:'value_mismatch', detected_at:$t}]->(b)
```

**Decide**, whether a policy or a person picks the winner, and keep one head decision per fact:

```cypher
MERGE (a:Agent {key:$actor}) ON CREATE SET a.name = $actor
CREATE (d:Decision {id:$id, subject_key:$sk, predicate:$pred, policy:$policy,
                    rationale:$why, at:timestamp(), chosen_value:$val})
CREATE (d)-[:DECIDED_BY]->(a)
WITH d MATCH (w:Claim {id:$win}) CREATE (d)-[:SELECTS]->(w)
WITH d UNWIND $ids AS cid MATCH (c:Claim {id:cid}) CREATE (d)-[:BASED_ON]->(c)
```

```cypher
MATCH (old:Decision {subject_key:$sk, predicate:$pred}) WHERE old.id <> $d AND NOT ()-[:SUPERSEDES]->(old)
WITH old MATCH (d:Decision {id:$d}) CREATE (d)-[:SUPERSEDES]->(old)
```

Then set the losing claims to `rejected` (lost on authority) or `superseded` (the same issuer amended them), and copy
the winner's value onto the subject property.

**Learn.** When a person resolves a card and ticks "apply to similar", their choice becomes a rule. The next
conflict with the same predicate and the same pair of source kinds resolves itself and cites them. Before writing
the rule, retire any rule that says the opposite, so the latest human word wins:

```cypher
MERGE (r:PolicyRule {id:$id})
SET r.predicate = $p, r.prefer_kind = $pk, r.over_kind = $ok, r.rationale = $why,
    r.created_by = $u, r.created_at = timestamp(), r.retired = false
WITH r MATCH (d:Decision {id:$d}) MERGE (r)-[:LEARNED_FROM]->(d)
```

**Explain.** "Why do we believe this number?" is now a traversal (`fact_history`):

```cypher
MATCH (dec:Decision {subject_key:$k})
OPTIONAL MATCH (dec)-[:DECIDED_BY]->(a) OPTIONAL MATCH (dec)-[:SELECTS]->(w:Claim)
OPTIONAL MATCH (dec)-[:APPLIED_RULE]->(r:PolicyRule)
RETURN dec.policy, dec.rationale, dec.chosen_value, labels(a)[0] AS decider_kind, a.key AS decider,
       w.source_doc AS winning_doc, r.id AS learned_rule
ORDER BY dec.at DESC
```

Rules learned in a visitor's session stay in that session. A bid manager can **promote** one to a shared
`bidbrain_org` graph. Nothing writes to that graph implicitly, each promoted rule keeps a copy of the decision it
came from, and every session checks company rules after its own rules and before recency and authority.

![Decisions page: the Pkg-113 value decision. The client corrigendum (₹188 Cr, authority 95) was selected over the client certificate and the portfolio (₹200 Cr each) by the recency_same_issuer policy, with the trace graph and source pages](img/decisions.png)

## What didn't work

* **The LLM clause parser broke the hero flow.** Before our last fix, the default Qualify path crashed on 3 of the 4
  sample tenders. The model invented an `options` key, wrote "roads" as a category, put two ISO standards in one
  clause, read "₹240 Cr in each of" as an average, and read ₹75 Cr as ₹750 Cr. Sample tenders now use vetted
  clauses. LLM clauses are mapped onto the engine's schema and dry-run against the graph. When the LLM and the rule
  parser disagree on an amount, the rule parser wins.
* **The Q&A agent added filters nobody asked for.** In one judge's question it added a "Prime" role and a years
  filter, and the answer dropped from ₹2.10 B to ₹0.42 B. We now check every narrowing filter for lexical support
  in what the user asked. An unsupported filter gets a note in the tool result asking the model to call again
  without it. This is a heuristic guard, not a real self-check.
* **Our weakest absence families.** Per-category absence scores .524 and per-person absence .611, against 1.0 for
  per-client absence.
* **The 144/144 team score is tuned on the test set.** The deterministic six-agent pipeline scored 136/144 on TND at
  first. All 8 misses were one parser bug ("buildings" also matched "small buildings"). We fixed it after seeing them.
* **Our first community detection found nothing useful.** `algo.labelPropagation` on a "shared at least two
  clients" projection returned one big community, because the synthetic company is evenly staffed. A projection
  that is too dense has no structure to find. The reworked version links two people only when their shared
  clients make up at least 25% of their combined client lists, and reports the split's modularity, so you can see
  whether it beats one block.
* **Most of our tools are 1-2-hop Cypher composed in Python.** That made them easy to test, but the graph does less
  of the work than it could. `trace_from_credential`, which runs the six-hop credential → person → work → client →
  portfolio → certificate chain in one query, is the exception.
* **The demo model is not the benchmark model.** Groq no longer serves Qwen3-32B, so the live app rotates through a
  pool of free Groq models, with 3-62 s latency under rate limits. Each answer now stays on the model that served
  its first step, unless that model hits a rate limit, so an answer is no longer stitched together from several
  models. The benchmark measures context strategies on a fixed model, not the deployed app.
* **Our own eval caught us misattributing data.** We had recorded the gradings in our own completion records as
  statements by the client, which made the recency rule fire wrongly 11 times. Rendering the planted conflicts as
  PDFs and pushing them through extraction found two more bugs (a grade word wrapped across a line, and a precision
  mismatch). The first run scored F1 0.976; after the fixes it scores 1.00.

## Try it

* **Live demo:** _<HF Space URL>_ (every visitor gets their own graph, so break anything you like)
* **Code:** github.com/atharvgaur1845/bidbrain
* **Cypher catalog:** `docs/CYPHER.md`, generated by executing every tool
* **Run locally:** `docker compose up`, then open http://localhost:7860

*Built for Graph Hacks (WeMakeDevs × FalkorDB), with the help of AI coding assistants, disclosed in the README.*
