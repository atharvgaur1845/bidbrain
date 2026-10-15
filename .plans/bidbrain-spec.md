# BidBrain — Spec & Build Plan (Graph Hacks × FalkorDB)

> On approval, this spec is copied verbatim to
> `/home/atharv/Desktop/projects/GraphHacks-Team_AG_X_GOD/.plans/bidbrain-spec.md` (interview-skill save location).

## Context

**Why:** Win "Graph Hacks: Context for AI Agents" (WeMakeDevs × FalkorDB). FalkorDB must be the primary DB and must "do real work, not decoration." Prizes go per track (T1 iPhone, T2 PS5, T3 Mac Mini), plus a best-blog prize. Entering all three tracks is allowed.

**Hard facts (from https://www.wemakedevs.org/hackathons/falkordb):**
- **Window:** Oct 15 00:01 IST → **Oct 18 23:59 IST** (18:29 UTC). Internal submit target: **Oct 18 18:00 IST**.
- **Required:** public repo, README with **graph data model** and **the Cypher queries / graph algorithms the product depends on**, a demo video, chosen track(s), and a live deployment (preferred).
- **Data** must be public, synthetic or our own.
- **The main FalkorDB implementation must be new work.**
- **AI-tool usage must be disclosed** (a rule, so a short disclosure section is included).
- Judging criteria are unpublished; ask in Discord (https://discord.com/invite/Y2mMT9VdMy).

**What won before** (FalkorDB "Memory Meets Motion"):
- the graph carries the logic
- "no path found" treated as proof (Hopper)
- decisions written back as precedent (AgentCourt)
- deterministic checks outside the LLM

**Overdone:** generic "chat with your docs" GraphRAG.

**Interview decisions:**

| Topic | Decision |
|---|---|
| Domain | Fusion: bid/tender desk company brain + conflict-aware living knowledge |
| Lead track | T3 Company Brain; also enter T1 + T2 |
| Scope | **No reduction.** Full conflict engine, all 4 hero moments. Claude builds everything. |
| Reuse | Old bid-intelligence extraction code + facts.json (Shoyam is on the team; credited in README) |
| Conflicts | Deterministic authority/recency policy auto-resolves clear cases, a human resolve card handles ambiguous ones, and every resolution becomes a `Decision` node |
| Memory | Custom typed graph + Claim layer (not Graphiti) |
| LLM | Qwen3-32B everywhere: vLLM on Sharanga H200 for extraction + evals; Groq free (primary) → Cerebras free (fallback) for the deployed demo. Local CPU embeddings. |
| Isolation | Graph-per-session via GRAPH.COPY of `bidbrain_base` |
| Hosting | Backend + FalkorDB in one Docker on **HF Spaces**; Next.js on **Vercel**; keep-alive GitHub Action |
| Agents | Own **FastMCP** server + thin OpenAI-compatible tool loop; shared state = FalkorDB case subgraph |
| Frontend | Next.js + shadcn/ui + Sigma.js (graphology) |
| Tenders | Real-shaped public clauses (demo) + synthesized (eval) |
| Conflict data | Rendered PDFs through the same ingestion (demo) + JSON events (P/R eval) |
| Baselines | Vector RAG (same LLM), GraphRAG-SDK auto-graph, text-to-Cypher only, conflict P/R |
| Video | Playwright-captured footage + human narration |
| Sharanga | Claude submits SLURM jobs via the user's terminal tab (user approves each) |

## Pitch

**BidBrain: the bid desk's company brain.** Indian infrastructure firms win government tenders by *proving* past performance: similar works ≥ ₹X Cr for central clients, qualified key personnel, valid bank guarantees, turnover. The proof is scattered across hundreds of certificates, letters, CVs, ledgers and bonds, and documents contradict each other over time. BidBrain puts all of it in FalkorDB. Agents qualify a tender clause by clause, **prove absences**, catch conflicting claims as new documents arrive, ripple changes to every open bid that depends on them, and show the evidence path down to the PDF page for every answer.

**Track mapping:**
- **T3:** people, works, clients, docs, decisions and sources in one graph; decision tracing; knowledge kept current.
- **T1:** multi-hop tools and algorithms over MCP; agents act (flag, re-plan, assemble the bid pack).
- **T2:** per-session tenant graphs, case graph as shared multi-agent state, persistent decisions/precedents as episodic + procedural memory.

## Data (MIT, synthetic): `/home/atharv/Desktop/projects/bits-hackathon-bid-intelligence/dataset`

**Corpus:** 687 docs (678 PDFs of 20 types + 9 xlsx).
- `BRIEFING.md` describes 4–6-hop joins and "absence" questions.
- `questions.json` has 333 questions; the old engine scored 99.775 on them, so its answers serve as **near-gold**.
- `sample_questions.json` has 21 questions with **true gold**.
- `evaluate.py` is the scorer.

**Findings from the design pass:**
1. **114 organic restatement conflicts** come from comparative "previous year" columns in the financials. Policy: a year's own statement wins (authority 95 vs 50). Auto-resolve these, show them in an "organic restatements" panel, and exclude them from planted P/R.
2. **One real value conflict:** Road Widening — Maharashtra Pkg-21 is 193,299,999 on its certificate and 193,300,000 in the portfolio. The certificate wins, which matches gold. Demo material.
3. **The 333 questions under-cover the graph.** Only 8 test absence, and none cover bonds, compliance, financials or assets. Absence and tender eval sets are therefore **synthesized, with gold computed from Cypher**.
4. **40 compliance matrices hold 491 real requirement rows** (120 with `threshold_value`, e.g. "Annual Turnover ≥ INR 240 Cr each of last 3 FY"). These seed real-shaped clauses, and the Verifier re-checks each MET status.
5. **6 tender dossiers (48 pp each)** contain past-bid annexures that were never extracted. A new dossier extractor seeds real `Bid` memory.

**Reused code** goes into `backend/bidbrain/vendor/` unchanged, with credit:
- `src/pdftext.py`, `money.py`, `extract.py`, `workbooks.py`, `financials.py`, `people.py`, `instruments.py`, `build_facts.py`
- **Not ported:** `answer.py` / `domains.py` (regex Q&A), except `answer.py`'s client/state alias logic (`_state_forms`), which moves to `ingest/aliases.py`.

## Architecture

```
PDF/xlsx ─▶ vendored extractors ─▶ facts.json ─▶ ingest/load_graph.py ─▶ FalkorDB "bidbrain_base"
new PDF / JSON event ─▶ ingest_document() ─▶ claims.py ─▶ conflicts/detect.py ─▶ policy.py ─▶ Decision | resolve card
                                                              │
FastAPI (SSE) ── session graph session_<id> (GRAPH.COPY) ── tools/*.py (pure fns) ── mcp/server.py (FastMCP)
     │                                                              ▲
     └── agents/orchestrator: ClauseParser → EvidenceHunter → AbsenceAuditor → ConflictSentinel → Verifier(no LLM) → Assembler
                                 (each writes to the Case subgraph = shared state/handoff)
LLM provider switch: vLLM@Sharanga | Groq | Cerebras (OpenAI-compatible)       Next.js (Vercel) ⇄ FastAPI (HF Space)
```

## Graph data model (`bidbrain_base`)

**Nodes** (counts are from facts.json):
- **Organisations and works:** `Company`(1), `Client{key,name,govt_level,state}`(29), `Work{key,title,cert_ref,value:int,completed_day:int,completed_year,category,grading,role,state,views:[cc,ccc,pf]}`(155), `Category`(13), `State`
- **People:** `Person{name,employee_id,designation,experience_years,qualification,joined}`(39), `Credential{id,kind,issued_day,valid_through_day}`(48), `BusinessUnit`(6)
- **Documents:** `Document{doc_id,doc_type,pages}`(687), `Chunk{doc_id,page,text,embedding}` with a vector index (384-d) and a fulltext index
- **Tenders and bids:** `Tender{ref}`(60), `Requirement{text,kind,threshold_value,threshold_unit,status}`(491), `Bid{tender_ref,bid_value,submitted}`(6, from the new dossier extractor)
- **Instruments:** `Bond`(60), `Bank`, `Certification`(5), `Contract`(6), `BOQItem`(43), `Measurement`(407)
- **Finance and assets:** `Invoice`(518), `FinancialStatement`(7), `FiscalYear`, `TBLine`(194), `LedgerAccount`(201), `Asset`(210)

**Edges:**
- **Work:** `(Work)-[:FOR_CLIENT]->(Client)`, `-[:LED_BY]->(Person)`, `-[:IN_CATEGORY]->`, `-[:IN_STATE]->`, `-[:HAS_REFERENCE{page}]->(Document)`
- **Provenance:** `(*)-[:EVIDENCED_BY{kind,page}]->(Document)`
- **People:** `(Person)-[:HOLDS]->(Credential)`, `-[:MEMBER_OF]->(BusinessUnit)`
- **Tenders:** `(Tender)-[:REQUIRES]->(Requirement)`
- **Bonds:** `(Bond)-[:SECURES]->(Tender)`, `-[:ISSUED_BY]->(Bank)`
- **Money:** `(Contract|Invoice)-[:FOR_CLIENT|BILLED_TO]->(Client)`, `(Contract)-[:HAS_ITEM]->(BOQItem)<-[:MEASURES]-(Measurement)`
- **Finance:** `(FinancialStatement|TBLine|LedgerAccount)-[:FOR_FY]->(FiscalYear)`
- **Bids:** `(Bid)-[:FOR_TENDER|CITES|PROPOSES|SECURED_BY]->(Tender|Work|Person|Bond)`

**Provenance pages:** `ingest/pages.py` locates the page of each value. It searches every money rendering (Cr, Lakh, Indian grouping, via `money.py`) in `results/raw/text_cache.json` and counts the form feeds before the match.

**Dates** are stored as integer `*_day` fields, so the Cypher never depends on date functions.

**Claim layer** (~1.3k contestable scalars):
- **Shape:** `(:Claim{id,subject_key,predicate,value,valid_from,valid_to,source_doc,page,issuer,authority,extracted_by,status})-[:ABOUT]->(subject)`, `-[:FROM]->(Document)`.
- **Entity properties** are a materialized view of the winning claim.
- **Conflict edges:** `(new)-[:SUPERSEDES]->(old)` and `(a)-[:CONTRADICTS{rule,detected_at}]->(b)`.
- **Decisions:** `(:Decision{policy,rationale,at})-[:DECIDED_BY]->(Agent|User)`, `-[:BASED_ON]->(Claim)`, `-[:SELECTS]->(Claim)`, `-[:SUPERSEDES]->(Decision)`.
- **Authority ladder:**

  | Source | Authority |
  |---|---|
  | Client corrigendum | 95 |
  | Bank instrument | 95 |
  | Client certificate | 90 |
  | Company record | 70 |
  | Portfolio | 60 |
  | Comparative column | 50 |
  | CV | 40 |
  | LLM-extracted prose | 30 |

- **Auto-resolve** when the same issuer published later (recency) or the authority gap is ≥ 20. Anything else becomes a resolve card.
- **Procedural memory:** a human resolution with "apply to similar" creates `(:PolicyRule)`, which auto-applies to future conflicts of the same (predicate, issuer pair).

**Case subgraph (shared multi-agent state):**
- `(Case)-[:HAS_CLAUSE]->(Clause{kind,params})-[:SATISFIED_BY{credited}]->(Work|Person|Bond|FinancialStatement)`
- `(Clause)-[:HAS_FINDING]->(Finding{kind:absence|conflict|expiry})-[:CITES]->(Claim)`
- `(AgentRun{agent,status})-[:WROTE]->(…)`
- `(Case)-[:PRECEDENT]->(Bid|Case)`
- `(Conversation)-[:HAS_TURN]->(Turn)` for persistent chat history

**Data gaps to handle explicitly:**
- **Bonds** link only to a tender + category string, and 32 of 60 have no status.
- **Tender→Client** is known only for the 6 dossiers.
- **Invoices** link to a client, not a work.
- **Contracts 71–79** are separate from Works.
- **Personnel** join by name/`credential_key` (case-insensitive).
- **Grading** is missing on 31 works. Qwen3 prose extraction fills it as authority-30 claims.
- **Trial balance vs ledger** is a cross-check rule.
- **Compliance status:** normalise MET/Complied to one value.
- **bank_statement and ra_bill** go into Chunks only.

## MCP tool catalog (`backend/bidbrain/tools/*.py`, exposed by `mcp/server.py`)

All arithmetic is exact integer Python, never done by the LLM.

**Read and query tools:**
- `resolve_entity(text, kind)`: fulltext + aliases (handles "gujarat pw", "phed odisha")
- `find_portfolio(client|person|via_work, cats_in, cats_not, min_value, after_day, role, led_by)`
- `aggregate(work_keys, op)`: sum / avg / median / mean_median_gap / rank_gap / category_difference / year_difference / distinct_categories / gap_to_threshold / count
- `credential_timeline`, `receivables`, `collection_rate`, `awarded_vs_billed`
- `prove_absence(scope, evidence)` returns a **proof object**:
  - (1) the universe, i.e. the client's FOR_CLIENT set;
  - (2) three-view agreement (`views` = cc/ccc/pf);
  - (3) corpus closure: all 132 reference letters are linked, so an absence ≠ an extraction miss;
  - (4) orphan check: no fulltext hits for the missing titles in reference-letter chunks.
- `qualify_clause(clause)`: similar_works (CPWD 1×80% / 2×60% / 3×40% rule in Python, JV share credit), turnover, key_personnel, bank_guarantee, certification
- `blast_radius(claim_id)`: dependents via `SATISFIED_BY|CITES|BASED_ON*1..3`, then re-runs `qualify_clause` with the new value and reports PASS→FAIL flips
- `explain_path(a,b)`: `algo.SPpaths` (maxLen 6, pathCount 3)
- `key_people`: `algo.betweenness`, the "bus factor"
- `client_communities`: `algo.labelPropagation`
- `components`: `algo.WCC`
- `vector_search(text,k)`
- `cypher_readonly(q)`: read-only query, timeout

**Write tools:**
- `list_conflicts`, `resolve_conflict(id, choice, rationale, apply_to_similar)`
- `case_open`, `case_add_clause`, `case_attach`, `case_add_finding`, `case_get`

The Cypher for find_portfolio, the via_work hop, prove_absence, qualify_clause, key_personnel, blast_radius and betweenness is drafted (design-pass output). It goes into `docs/CYPHER.md` and the README catalog. Verify procedure names and signatures on Day 0 with `CALL dbms.procedures()`.

## Agents (`backend/bidbrain/agents/`)

**Infrastructure:**
- **`providers.py`:** OpenAI-compatible client with a vLLM | Groq | Cerebras switch.
  - A token-bucket router fails over Groq → Cerebras.
  - Responses are cached by (graph_version, input).
  - Qwen3 thinking is off for routing turns.
- **`loop.py`:** thin tool-calling loop. Each agent gets **only its own tool subset**, and every step is streamed as an SSE event.

**Pipeline (`orchestrator.py`).** Each agent writes to the Case subgraph, and the next agent reads from it. That is the handoff.
1. **ClauseParser:** tender text → typed `Clause` JSON (schema in `data/tenders/schema.json`).
2. **EvidenceHunter:** qualify_clause / find_portfolio → `SATISFIED_BY` edges.
3. **AbsenceAuditor:** prove_absence on required evidence (reference letters, valid bonds, credentials) → `Finding{absence}`.
4. **ConflictSentinel:** checks every claim the case relies on for open CONTRADICTS → `Finding{conflict}` and a resolve card.
5. **Verifier (no LLM):** re-executes each clause deterministically from the graph and recomputes totals. A disagreement fails loudly.
6. **Assembler:** produces the bid pack (markdown/PDF): verdict per clause, evidence table with doc + page links, gaps, risks, and `PRECEDENT` links to similar past bids.

**Q&A mode:** a single agent with all read tools, for the 333-question eval and the Ask page.

## Conflict engine (`backend/bidbrain/conflicts/`)

- **`generate.py`:** planted-conflict generator with known labels. It covers six types:
  - corrigendum (value/date)
  - revised completion certificate
  - bank guarantee expiry/invocation
  - CV vs personnel-certificate years
  - duplicate reference letter with a different grading
  - restatement

  **Hard negatives:** rounding (like the real 193,299,999 case), edits to unrelated fields, consistent restatements. **Held-out templates** are not used to tune the rules.
- **`render_pdf.py`:** renders the same events as corpus-style PDFs (reportlab). **Gate:** each must round-trip through the vendored `pdftext` / `extract`.
- **`detect.py`:** same (subject, predicate) with a differing value inside an overlapping validity window → CONTRADICTS.
- **`policy.py`:** authority / recency / PolicyRule resolution → Decision, or a resolve card.
- **`ingest_document()`:** the live path, PDF → claims → detect → policy → blast_radius → SSE notifications.

## Session isolation & concurrency

- **First visit:** `GRAPH.COPY bidbrain_base session_<id>`. Fallback: DUMP/RESTORE, or replaying the loader (~3k nodes, under 2 s).
- **Cap:** ~50 sessions, 2 h TTL reaper.
- **Client:** falkordb-py 1.8 async with a `BlockingConnectionPool`.
- The base graph is read-only for sessions.

## LLM & infrastructure

**Sharanga** (`ssh sharanga`, user `pabitra`, partition `gpu_h200_8`):
- Reuse the coherent venv/toolchain pattern from `/home/atharv/Desktop/projects/Gemma-4_Developer_Agent_Competition/serving/serve_clean.sbatch` in a new `infra/sharanga/serve_qwen.sbatch`.
- **vLLM flags:** Qwen3-32B with `--enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3`.
- **Readiness gate:** a real tool call must succeed, not just `/v1/models`.
- **Tunnel:** `ssh -L` back to the laptop.
- **Uses:** grading prose extraction, all eval runs, and recorded "replay" traces for the canned demo tenders.

**Deployed demo:**
- Groq `qwen/qwen3-32b`, falling back to Cerebras Qwen3-32B.
- Embeddings: `fastembed` bge-small on CPU, in the container.

**HF Space Docker (`backend/Dockerfile`):**
- `FROM` a pinned `falkordb/falkordb` image.
- Runs redis-server + module on localhost, plus FastAPI on 7860, as uid 1000.
- At boot, **rebuilds the graph from facts.json and the planted data** (no RDB dependency).
- Fetches the dataset PDFs from upstream `satvikGIKA/BITS-Hackathon-Dataset` at a pinned commit, for the page viewer.
- Secrets: GROQ/CEREBRAS keys.

**Vercel:** Next.js app pointing at the HF Space URL.

**Keep-alive:** `.github/workflows/keepalive.yml` pings `/health` every 6 h.

## Frontend (`frontend/`, Next.js + shadcn/ui + Sigma.js/graphology + framer-motion)

1. **/ask:** chat with streamed agent steps.
   - The answer card animates its **evidence path** on the graph canvas; each hop opens the PDF at its page.
   - A **"Compare: vector RAG"** toggle shows the baseline answer side by side (hero moment 2, live).
2. **/qualify:** pick a demo tender or paste a clause.
   - A multi-agent timeline shows each agent's graph writes appearing.
   - A clause verdict matrix shows PASS / FAIL / GAP, and the bid pack can be exported (hero moment 1).
3. **/conflicts:** an inbox for dropping a PDF or firing an event.
   - Conflict cards show both claims, their sources and the suggested policy.
   - Resolving creates a Decision. A **ripple panel** shows which bids flip (hero moment 3).
   - Includes the organic-restatements panel.
4. **/graph:** schema view, algorithm overlays (bus factor, communities, components) and a path finder.
5. **/decisions:** "why was X decided" traces Decision → Claims → Docs → people.
6. **/eval:** benchmark dashboard built from eval JSON (hero moment 4).

## Evaluation (`backend/eval/`, all on vLLM Qwen3-32B, temperature 0)

**Sets:**

| Set | Size | Gold |
|---|---|---|
| S21 | 21 | true gold |
| Q333 | 333 | near-gold; every disagreement with the old engine is listed |
| ABS | ~150 synthetic absence questions | Cypher gold |
| TND | ~120 qualify items from real compliance thresholds + NHAI/CPPP templates | Cypher gold |
| CONF | ~300 JSON planted + hard negatives; ~25 PDFs | known labels |

**Systems:**
- `baselines/vector_rag.py` (FalkorDB vector index over Chunks, same LLM)
- `baselines/text2cypher.py` (schema in prompt, no domain tools)
- `baselines/graphrag_sdk.py` (auto-schema, LiteLLM→vLLM, thinking off, started early)
- BidBrain single-agent
- BidBrain multi-agent + Verifier

**Gate before the agent:** a "tool oracle" maps the old engine's traces to tool calls and must reproduce all 354 answers. That proves the tools are sufficient.

**Metrics:**
- `evaluate.py` score and exact match
- breakdowns by hop band (2–3 / 4 / 5–6), absence vs presence, informal vs canonical entity mention, single- vs cross-block
- latency p50/p95, LLM calls and tokens per question
- conflict P/R/F1, with the PDF path and the JSON path reported separately

**Outputs:** `eval/results/*.json` → `eval/report.py` → charts for the README, /eval and the video.

## Deliverables

- **`README.md`:** pitch, track mapping, live links, architecture diagram, **graph data model**, **Cypher + algorithms catalog**, eval table, setup (docker compose), AI-usage disclosure, credits (old repo, dataset MIT).
- **Docs:** `docs/EXPLAINER.md`, `docs/CYPHER.md`, `docs/DEMO_SCRIPT.md` (3–4 min, shot by shot), `docs/BLOG.md` (Hashnode/dev.to post).
- **Demo footage:** `scripts/record_demo.ts` (Playwright video capture); a teammate adds narration.
- **Local dev:** `docker-compose.yml` (falkordb + backend + frontend).

**Demo video order:**
1. Tender in → bid pack out (evidence path to the PDF page)
2. Proof of absence vs vector RAG's confident "0"
3. Live PDF drop → conflict → ripple → resolve → Decision trace
4. Benchmark chart

## Repo layout (`/home/atharv/Desktop/projects/GraphHacks-Team_AG_X_GOD`)

```
.plans/bidbrain-spec.md
backend/bidbrain/{vendor,ingest,graph,tools,mcp,agents,conflicts,api}/  backend/eval/  backend/tests/  backend/Dockerfile
frontend/            data/{tenders,conflicts}/            infra/sharanga/            scripts/
docs/                .github/workflows/keepalive.yml      docker-compose.yml         README.md
```

## Build sequence (★ = critical path, each step has a gate)

**Day 0 (Oct 15):**
- ★ Spikes:
  - pinned FalkorDB image: GRAPH.COPY, `algo.*` names, vector and fulltext indexes, `ro_query`
  - hello-world HF Space (FalkorDB + FastAPI on 7860)
  - a Groq tool-call test
  - **submit the Qwen3 SLURM job**
- ★ `git init`, vendor the old code, then `load_graph.py` + `pages.py` + `aliases.py` + `claims.py`.
  - **Gate:** a Cypher-only replay of S21 scores 21/21.
  - **Gate:** ≥ 99% agreement on Q333.

**Day 1 (Oct 16):**
- ★ tools/ + MCP server. **Gate:** the tool oracle reproduces all 354 answers.
- ★ providers + agent loop, then a single-agent S21/Q333 run on vLLM.
- Conflict engine: generate / detect / policy, the JSON stream, the P/R harness and Decision writes.

**Day 2 (Oct 17):**
- render_pdf + ingest_document + resolve cards.
- Multi-agent orchestrator, Case subgraph, Verifier, tender schema, Assembler, dossier extractor (Bid memory), PolicyRule.
- Baselines: start the GraphRAG-SDK ingestion on Sharanga in the morning, then vector RAG and text2cypher.
- Synthesize the ABS and TND sets.

**Day 3 (Oct 18):**
- ★ FastAPI + SSE, session graphs + reaper, deploy the HF Space and Vercel, keep-alive.
- ★ Next.js pages.
- Full eval matrix plus the report.

**Final 12 h:**
- ★ README / EXPLAINER / CYPHER docs.
- Playwright footage, the narration handoff, the blog.
- **Submit by 18:00 IST.**

**If time collapses, order of last resort** (the user asked for no reduction, so this only orders the risk):
1. A partial GraphRAG-SDK run, documented as partial.
2. PDF rendering for only 3 of the 6 conflict types; the rest stay JSON-only.
3. The pageRank / community UI panels.
4. Blog polish.

**Never cut:** the claim layer, prove_absence, the Verifier, the eval table.

## Top risks → mitigations

1. **FalkorDB on HF Spaces** (port, uid, ephemeral disk) → rebuild at boot from facts; fallback FalkorDB Cloud free or Render.
2. **No GRAPH.COPY** → DUMP/RESTORE or loader replay.
3. **Groq free-tier TPM limits** → per-agent tool subsets, thinking off, Cerebras failover, response cache, replay traces for the canned tenders.
4. **vLLM Qwen3 parser flags / SLURM queue** → reuse serve_clean's toolchain + tool-call readiness gate; submit on Day 0.
5. **GraphRAG-SDK cost** (~1M ingest tokens, JSON breaks under thinking) → thinking off, run early, partial is acceptable.
6. **Near-gold isn't gold** → report S21 separately and list the disagreements.
7. **Circular conflict P/R** → hard negatives, held-out templates, PDF vs JSON reported separately.
8. **Informal entity names / Cypher dialect gaps** → aliases + fulltext, integer days, pattern predicates instead of `EXISTS{}`.

## Prerequisites the user must do (Claude cannot create accounts or enter credentials)

- Register the team on the WeMakeDevs page. Ask Discord for the judging criteria.
- Create and paste into `.env` (never committed):
  - Groq API key
  - Cerebras API key
  - HF write token (+ create the Space)
  - a Vercel account linked to GitHub
- Create the public GitHub repo, or approve when Claude runs `gh repo create`.
- SSH into `sharanga` in the terminal tab; handle any 2FA.
- A teammate records the narration over the Playwright footage.
- Claude asks before each outward-facing action: pushing, creating the repo, deploying, publishing the blog.

## Verification

**Backend:**
- `docker compose up` locally.
- `pytest backend/tests`: loader counts, page provenance spot checks, each tool's Cypher, the policy table, absence proofs.
- `python -m eval.run --set S21 --system bidbrain` gives 21/21, and the tool oracle gives 354/354.

**Conflict engine:**
- Drop `data/conflicts/pdf/corrigendum_*.pdf` into /conflicts → card appears → resolve → `MATCH (d:Decision)-[:BASED_ON]->(c:Claim) RETURN d,c` shows the trace → ripple flips the expected clause.

**Isolation:** two browser sessions; a mutation in one is invisible in the other (`GRAPH.LIST` shows two session graphs).

**Deployed app:**
- HF Space cold start → `/health` → the full demo flow on Groq.
- Kill the Groq key → Cerebras failover works.

**Frontend:** Playwright script runs end to end on the deployed URLs, and the video is recorded from it.

**Submission:** checklist — repo public, README has the data model + Cypher catalog + AI disclosure, video link, tracks selected.
