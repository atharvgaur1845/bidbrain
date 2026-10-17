# Demo video: 2 minutes, shot by shot

Recorded at 1440×900. A human narrates. Keep the graph and the workflow on screen the whole time; no architecture
slides. Pause about 1 s on every number that matters.

**The core path (0:00–1:48) uses no LLM.** It runs the same with an empty `.env`. The LLM is used only by **Ask**
(free-text questions), by the ClauseParser for new or edited tender text, and by the bid-memo prose (template
fallback). None of these is in the core path.

**Before recording**
* Browser 1: a fresh workspace (press **Reset graph**, top right). Browser 2: a private window, not opened yet.
* Check that the company record is empty: **Memory** → *Company record* shows no active fact. Step 1:20 publishes a
  fact that a workspace applies once it ticks **Use company memory**. After recording, retire it from browser 1's workspace with
  `POST /api/company/record/<id>/retire` and header `X-Session: <browser 1's session id>` (the UI has no retire
  button for facts yet). Only the publishing workspace can retire it.

---

### 0:00 – 0:08 · The problem (Control room, empty)
**Screen:** the **Control room** in browser 1.
**Say:** "A construction firm bids on public tenders with proof scattered across hundreds of documents, and those
documents keep changing. One corrigendum can quietly make a bid ineligible."

### 0:08 – 0:22 · Qualify a tender (Change impact → demo runner)
**Click:** sidebar **Change impact** → **Run the demo steps**. The first step, *Qualify T-NHX-2026-014 (rules only, no
LLM)*, turns *Done: recommendation BID*.
(Alternative: **Control room** → **Qualify a tender** → *4-laning of Greenfield Expressway Package-III* → **Run
agents**, then go to **Change impact**.)
**Say:** "Six agents qualify the expressway tender clause by clause against the company graph. Verdict: BID. The
similar-works clause rests on Ring Road Pkg-113, a ₹200 crore work."

### 0:22 – 0:55 · The graph changes a decision (Change impact)
**Screen:** the runner sends **DEMO-0** (revised completion certificate) and **DEMO-1** (corrigendum). The page
switches to the document list. **Click** the *Corrigendum — Ring Road — Uttar Pradesh Pkg-113* event (badge
*1 bid · 2 to act*). Move the cursor down the numbered chain:
1. **Document**: DOC-DEMO-1, p.1, issuer National Special Projects Office.
2. **Changed fact**: Pkg-113 contract value ~~₹200 Cr~~ → **₹188 Cr**.
3. **Adjudication**: *Recency (same issuer)*, "policy, no LLM", decision id.
4. **Affected requirements**: clause **2.1 similar works**, Pass → **Fail**, "graph traversal".
5. **Bid verdict change**: **Bid → No-bid**, "deterministic requalification".
6. **Required actions**: *Re-approve bid* and *Publish to company record*, both **Pending**.
**Click:** **Graph query** at the bottom, for one second, to show the Cypher.
**Say:** "A corrigendum arrives. It becomes a claim with its page. The policy picks the winner: same issuer, later
document. Then the graph walks from that fact to every open clause that relied on it, re-runs them, and the bid
flips to NO-BID. No text search, no LLM. And the system doesn't stop at a recommendation: it opens actions."

![Change impact chain for the Pkg-113 corrigendum](img/impact.png)

### 0:55 – 1:08 · An accountable action (Change impact → Actions)
**Click:** on *Re-approve bid*: **Approve** → **Execute** → **Confirm execute**. It shows *Executed*. Then sidebar
**Actions** → tab **Executed** → the row. Point at the result (*re-verified 6 clauses; recommendation NO-BID
approved*) and the **Audit trail**.
**Say:** "Nothing executes without approval. Execution is deterministic: it re-verifies every clause against the
graph before recording the new recommendation, and every step is in the audit trail. Requests for missing letters
become internal requests; nothing is emailed."

![Actions: the executed re-approval with its result, trigger, cited document and audit trail](img/actions.png)

### 1:08 – 1:20 · Trace the decision (Decisions)
**Click:** sidebar **Decisions** → tab **Recency** → *Ring Road — Uttar Pradesh Pkg-113*. The trace graph shows
Decision → SELECTS → the ₹188 Cr claim → FROM → DOC-DEMO-1, and the two ₹200 Cr claims set aside (**Superseded**),
each with its document and page.
**Say:** "Every settled fact keeps the policy, the decider and every source. Nothing was overwritten."

### 1:20 – 1:26 · Publish to the company record (Decisions → Actions)
**Click:** **Publish to company record** → **Open action** → **Approve** (files a publish request; a second person
must approve it).
**Say:** "The bid manager proposes this as the company's truth. That is an action too, and it needs a second approver."

### 1:26 – 1:48 · A clean second workspace (browser 2)
**Click:** open browser 2 (private window) at the app. Sidebar **Memory**: approve the pending publish request; tick
**Use company memory in this session**. Back in browser 1, **Execute** → **Confirm execute**. In browser 2, *Company
record* lists Pkg-113 = ₹188 Cr, **Applied**, with the source decision and document. Then **Decisions** → tab **Company record** → the row: the
decision has policy *Company record* and its rationale names the OrgFact. **Control room**: *Open cases 0*.
**Say:** "A second person approved it, and a workspace that opts in starts from the new fact. It was applied through the same conflict engine, with a policy
that cites where it came from. Nothing private crossed over: no cases, no actions, no conversation."

*(Optional, +15 s, company rule beat.)* In browser 1, **Live documents** → send *JV Experience Certificate —
Residential Quarters* (DEMO-3); it lands as a card; **Choose and learn rule**; **Promote to company memory**. In
browser 2 (already opted in), **Live documents** → send DEMO-4: it
resolves itself, citing the promoted rule.

### 1:48 – 2:00 · Evidence (Benchmark)
**Screen:** sidebar **Benchmark**. One beat each:
* "The workflow evaluation runs this whole loop without an LLM: 81 of 81 checks. The bids it says flipped match an
  independent before-and-after recompute exactly: precision and recall 1.0."
* "Absence is proved, not guessed: 2 of Trishakti Power's 19 works have no reference letter, with the boundary
  attested three ways and all 132 letters accounted for."
* "On answer accuracy, with the same model, graph tools beat text-to-Cypher, GraphRAG-SDK and vector RAG. The
  labels say which sets have independent gold and which are engine-derived."
**Close:** "BidBrain: changing evidence in, auditable decisions and accountable actions out."

---

**Notes for the editor**
* The benchmark numbers on screen are the final run 1; say "about .79 on independent gold across two runs" rather than a single point.
* If the runner was already used in this workspace, the page shows the document list instead of the button: press
  **Reset graph** first.
* The absence beat can be shown live only with an LLM key (**Ask** → *Which of Trishakti Power's completed works have
  no client reference letter?*); otherwise say it over the Benchmark page.
