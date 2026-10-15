# Proving what isn't there: building a company brain on FalkorDB

*How we built BidBrain for Graph Hacks: agents that qualify government tenders, prove absences, settle conflicting
documents, and show the path behind every answer.*

---

## The question that breaks retrieval

Here is a question a bid manager at an Indian construction firm asks every week:

> *"How many of this client's completed works have no reference letter on file?"*

It sounds trivial, but a retrieval-augmented LLM cannot answer it. A retriever embeds the question, fetches the eight
most similar chunks (which are reference letters, since the question mentions them), and the model reasons over
what it was handed. The letters that *don't exist* are never in the context, because nothing can be retrieved for a
document that was never written. The model counts what it sees and answers confidently.

Answering it properly means knowing **the boundary of the set**: every work this client ever certified, attested
independently, and the proof that each letter in the estate is accounted for. That is a graph question. This post
is about building the system that answers it, plus three harder ones, on FalkorDB.

## The problem: proof is scattered, and it contradicts itself

Public tenders in India are won on eligibility before price. The CPWD rule for "similar works" is typical: in the
last seven years, **one** completed work of at least 80% of the estimated cost, **or two** of 60%, **or three**
of 40%, for government clients, each backed by the client's completion certificate. Add PMP-certified project
managers valid *on the bid date*, average turnover over three audited years, ISO certificates and bank guarantees.

Our data is a synthetic but realistic construction company: 687 documents covering 155 completion certificates
from 28 clients, company completion records, 132 reference letters, CVs, personnel certificates, bank guarantees,
compliance matrices, audited statements, tender dossiers and ledgers. **Nothing says which document belongs to which
project.** A typical question walks person → credential → work → client → that client's *whole portfolio* → each
certificate's value. That's five hops, and the answer set is not the thing the question names.

The documents also disagree. Each year's audited statement prints a "previous year" column, and in this corpus that
column disagrees with the previous year's own statement on 114 fields. Revenue for FY 2024-25 is ₹238.92 Cr in its
own statement and ₹232.26 Cr in the next year's comparative column. A tender demanding "₹240 Cr in each of the last
three years" therefore fails by ₹1.08 Cr or by ₹7.74 Cr depending on which document you trust. A useful system has
to know that, and say so.

## Claims, not overwrites

The single most important modelling decision was to stop treating property values as facts. Every contestable value
in BidBrain is a **claim node**:

```cypher
(:Claim {predicate:'value', value:2000000000, source_doc:'DOC-CC-113', page:1,
         issuer:'National Special Projects Office', source_kind:'client_certificate',
         authority:90, observed_day:20543})-[:ABOUT]->(:Work)
```

The familiar `Work.value` is a **materialized view of the winning claim**. When a corrigendum arrives we add a
claim, detect the disagreement (`CONTRADICTS`), and settle it with a `(:Decision)` that records the policy, the
rationale, who decided, and both documents. History is never edited, so *"why do we believe this number?"* is a
traversal:

```cypher
MATCH (d:Decision)-[:SELECTS]->(w:Claim)-[:FROM]->(doc:Document) WHERE d.subject_key = $work
RETURN d.policy, d.rationale, w.value, doc.doc_id
```

The policy is deliberately boring and explainable:

1. a rule a human taught us,
2. the same issuer amending itself **with a document at least as formal** (a client's chatty letter must not silently
   override its own certificate),
3. a clear authority gap (client corrigendum 95, certificate 90 … CV 40),
4. otherwise, ask a human.

When the human answers with "apply to similar", their decision becomes a `(:PolicyRule)-[:LEARNED_FROM]->(:Decision)`,
and the next conflict of that shape resolves itself and cites them. That is procedural memory you can audit.

## Agents that hand off through the graph

Qualifying a tender is a team effort. Six agents:

1. read the eligibility text into typed clauses;
2. qualify each clause by traversal;
3. prove absences (missing letters, credentials lapsing before the bid date);
4. check the conflict history of every fact the verdict relies on;
5. **re-derive every verdict with no LLM at all**;
6. write the bid memo.

They share no framework memory. A handoff is a graph write: each agent is an `(:AgentRun)` that writes `Clause`,
`SATISFIED_BY` and `Finding` nodes, and the next one reads them (`HANDED_OFF_TO`).

That choice paid off in a way we didn't plan. Because verdicts are edges to the facts they rely on, **a change can
find everything it breaks**. When a corrigendum cuts a ₹200 Cr work to ₹188 Cr, `blast_radius` walks outward from
that work. It finds open clauses that rely on it, past bids that cited it, and tenders it secured. It re-runs the
affected clauses. On screen, the NEDA expressway bid flips from **BID to NO-BID**, two documents after it looked
safe, and the reason is one click away.

## Tools, not text-to-Cypher

We gave the agent 25 named tools instead of raw Cypher. `find_portfolio` with a `via_work` hop, `prove_absence`,
`qualify_clause` and `explain_path` are each a tested traversal that returns evidence (document and page) and a
subgraph for the UI. **Arithmetic never happens in the model.** `aggregate` takes *work keys*, re-reads values from
the graph and computes in exact integer rupees, so the model can neither misadd nor smuggle in a number.

The same registry is served three ways: to our agents, over **MCP** (any MCP client can connect to `/mcp`), and to
an **oracle** that replays a deterministic engine's parsed parameters through our tools. The oracle matters for
evaluation. Our tools reproduce **354 of 354** reference answers, so any accuracy the LLM agent loses is planning,
not missing graph capability.

## Things FalkorDB taught us

* **Full-text tokens don't break on newlines.** Our page chunks kept PDF line breaks, and RediSearch indexed
  `Work\nAnganwadi` as one token. About 60% of keyword hits silently disappeared until we indexed a
  whitespace-folded copy. Check your hit counts against a `CONTAINS` scan.
* **Centrality needs the right projection.** Our domain edges point `Work→Person` and `Work→Client`, so in a directed
  graph no shortest path ever passes *through* a person, and betweenness was 0 for everyone. An explicit
  person⇄client projection (`WORKED_FOR` / `ENGAGED`) turned it into a real bus-factor ranking.
* **`algo.SPpaths` with `relDirection:'both'`** is the easy way to answer "how are these two connected?". Cypher's
  `shortestPath` is directed-only and limited to `WITH`/`RETURN`.
* **`GRAPH.COPY` is a tenancy model.** Every visitor to our demo gets their own copy of the company graph on the first
  request. One judge resolving a conflict never changes what the next judge sees, and a returning visitor gets their
  cases, decisions and learned rules back.

## Did the graph do the work?

Same model (Qwen3-32B), same FalkorDB instance, four context strategies: BidBrain's tools, text-to-Cypher, vector RAG
over FalkorDB's own vector index, and FalkorDB's GraphRAG-SDK with automatic schema extraction.

_[results table: S21 true gold · Q333 · absence set · tender set, and accuracy by hop count]_

We publish what broke, too. Our conflict engine scores F1 1.00 on a stream of 275 planted events, including 75 hard
negatives such as rounding-only restatements. We wrote the labels and the rules, though, so that mostly measures
consistency. The honest test was rendering the events as PDFs and pushing them through extraction and entity
resolution with deliberately mangled titles ("Pkg 151", "(Package 151)"). The first run scored F1 0.976 and found two
real bugs, a grade word wrapped across a line and a precision mismatch. The eval also caught us attributing our own
records' gradings to the client, which made the recency rule fire wrongly 11 times.

## Try it

* **Live demo:** _<HF Space URL>_ (every visitor gets their own graph; break anything you like)
* **Code:** github.com/atharvgaur1845/bidbrain
* **Cypher catalog:** `docs/CYPHER.md`, generated by executing every tool

*Built for Graph Hacks (WeMakeDevs × FalkorDB), with the help of AI coding assistants, disclosed in the README.*
