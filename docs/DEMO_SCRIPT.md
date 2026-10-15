# Demo video: shot-by-shot (target 3:45)

Footage: `scripts/record_demo.ts` (Playwright) drives the deployed app and records 1440×900 video. A human narrates
over it. Each beat lists **what's on screen** and **what is said**. Keep the cursor slow, and pause about 1 s on
every number that matters.

---

### 0:00 – 0:20 · Hook (Overview page)
**Screen:** the hero headline; the stats tiles count up (7,544 nodes · 12,838 relationships · 1,648 claims · 114 decisions).
**Say:** "Indian infrastructure firms win public tenders by proving their past: similar works, qualified engineers,
valid bank guarantees. That proof lives in hundreds of documents that contradict each other. BidBrain is a company
brain on FalkorDB that turns all of it into one graph that agents act on."

### 0:20 – 1:15 · Hero 1: tender in, bid memo out (Qualify)
**Screen:** pick *4-laning of Greenfield Expressway, ₹250 Cr* → **Qualify this tender**. The agent column ticks
through ClauseParser → EvidenceHunter → AbsenceAuditor → ConflictSentinel → **Verifier** → Assembler. Clause rows fill
in with PASS badges, and the evidence graph grows. Click clause 2.1 to highlight Ring Road UP Pkg-113 →
DOC-CC-113 p.1, and open the PDF at that page.
**Say:** "Six agents share one case graph. Each writes its conclusions as nodes the next one reads, so every handoff
is an edge. The similar-works rule is the real CPWD rule: one work at 80% of the cost, or two at 60%, or three at
40%. Every verdict links to the certificate page that proves it. The Verifier uses no LLM; it re-derives every
verdict from the graph."
**Then:** switch to *Institutional Buildings, ₹90 Cr* and run it. Turnover **FAILS by ₹1.08 Cr**. Point at the
ConflictSentinel finding: "that year was restated by the next year's accounts — here's which figure we used, and why."

### 1:15 – 1:55 · Hero 2: proof of absence (Ask)
**Screen:** suggested question *"Which of Trishakti Power's completed works have no client reference letter?"*. The
trace streams: `resolve_entity` → `prove_absence`, and the graph draws 19 works with two red dashed "?" nodes.
The answer is 2: Pkg-85 and Pkg-43. Expand the proof: 19/19 attested by three views, 132/132 letters linked, orphan
search empty. Click **Compare with vector RAG**: it answers from the 8 chunks it retrieved.
**Say:** "Questions about what's *missing* are where retrieval breaks. A retriever only reasons over what it fetched,
so it never sees the letter that isn't there. The graph knows the boundary of the set and returns a proof, not a guess."

### 1:55 – 2:50 · Hero 3: live documents ripple (Live documents)
**Screen:** with the expressway case open, send **"Revised Completion Certificate — Pkg-116"**. It is auto-resolved
by *recency*, and the feed notes "the bid now rests on one work". Send **"Corrigendum — Ring Road UP Pkg-113"**:
the old claims are struck through, and the ripple card shows clause 2.1 **PASS → FAIL** and **BID → NO-BID**. The
ripple graph lights up the case and 6 past bids that cited this work.
**Say:** "Documents keep arriving. Each becomes a claim, nothing is overwritten, and conflicts are settled by an
authority-and-recency policy. Then BidBrain walks outward from the changed fact to every open bid that relied on it.
Two documents later, a winnable bid is a no-bid, and we know exactly why."
**Then:** send **"JV Experience Certificate"**. It's a close call, so it lands in *Needs your judgement*. Click
**Choose + learn rule**. Send the second JV certificate: it **resolves itself**, citing your decision.
**Say:** "When the policy can't decide, it asks. When you decide, it learns."

### 2:50 – 3:10 · Decisions & graph algorithms (Decisions → Explore)
**Screen:** Decisions page. Click the corrigendum decision: the trace graph shows Decision → SELECTS → claim → FROM →
DOC, and DECIDED_BY. Then Explore → **Bus factor (betweenness)**: Rahul Menon tops the list. **Audit past bids**:
every past dossier cited works not yet complete at submission.
**Say:** "Every fact traces back to a decision, a decider, and two documents. FalkorDB's algorithms are tools the
agents call: betweenness shows who the company can't afford to lose."

### 3:10 – 3:40 · Benchmark (Benchmark page)
**Screen:** the accuracy table (same model across BidBrain, text-to-Cypher, vector RAG and GraphRAG-SDK), the hop-count
bars, tool sufficiency 354/354, and conflict-engine P/R on 275 planted events plus 50 rendered PDFs.
**Say:** "Same model, same database, different context. [Read the headline gap at 5–6 hops and on absence
questions.] And we publish what broke: the PDF eval found two real bugs on its first run."

### 3:40 – 3:45 · Close
**Screen:** Overview, with the MCP endpoint and repo link.
**Say:** "BidBrain: the bid desk's company brain. Every answer shows its path."

---

**Before recording:** hit **Reset** (fresh session graph), make sure the LLM chip in the sidebar shows `groq`, and
warm the Space with one request.
