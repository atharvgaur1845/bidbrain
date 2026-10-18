# Demo runbook

How to launch BidBrain, then run the deterministic demo sequence: what each step shows, how long it takes and
what to say. The script `scripts/record_demo.mjs` drives the same sequence and writes one screenshot per step to
`docs/demo/` (or a video, see below). Steps 1 to 7 use no LLM.

## 1. Launch

### Option A: Docker Compose

```bash
cp .env.example .env                 # LLM keys are optional; the demo path below needs none
python scripts/make_demo_principals.py   # only for step 8: writes BIDBRAIN_PRINCIPALS, BIDBRAIN_AUTH_KEY and the demo secrets into .env
docker compose up --build            # http://localhost:7860
```

The compose file reads `env_file` with `required: false`, which needs Docker Compose 2.24 or later.

### Option B: plain Docker (verified on 2026-10-18; this host has no Compose plugin)

```bash
docker build -t bidbrain .                                      # no cache: 1 min 59 s
docker volume create bidbrain-state
docker run -d --name bidbrain -p 7860:7860 --env-file .env \
  -e BIDBRAIN_STATE_DIR=/app/state -v bidbrain-state:/app/state --stop-timeout 20 bidbrain
curl -s localhost:7860/api/health                              # ok after about 5 s
```

Expected boot log:
* `FalkorDB up: ... graph ...`
* `built bidbrain_base in 1.7s: ... edges: 13507`
* `6 demo events`
* `30 sample PDFs rendered`
* `Uvicorn running on http://0.0.0.0:7860`

Expected `/api/health`: `"ok": true, "base_graph": true, "tools": 32`. With no LLM key, `"llm": []`.

Without `BIDBRAIN_PRINCIPALS`, the log says `auth: no BIDBRAIN_PRINCIPALS; shared publication disabled`. Steps 1 to 7
still work, but step 8 (promote) fails closed.

### API smoke test (no LLM, no browser)

1. `POST /api/session` → `session_id`. Send it as `X-Session` on every request below.
2. `POST /api/qualify {"tender_id":"T-NHX-2026-014","use_llm":false}` (SSE) → the case is **BID**.
3. `POST /api/events/DEMO-0/fire`, then `POST /api/events/DEMO-1/fire` → the case is **NO-BID**.
4. `GET /api/actions` → a `review_bid` action, `pending`.
5. `POST /api/actions/<id>/approve` → `approved`.
6. `POST /api/actions/<id>/execute` → `executed`, with the result *re-verified 6 clause(s) against the graph;
   recommendation NO-BID approved*.

On a clean container, these six calls take 1.9 s.

## 2. Record

```bash
cd scripts && npm i                                       # playwright only
CHROME=/usr/bin/google-chrome node record_demo.mjs        # BASE=http://localhost:7860 by default
```

* **Output:** `docs/demo/01-…png` to `15-…png`, each 1440×900, with a caption bar naming the step.
* **Run time:** about 21 s. `PAUSE=<ms>` sets the hold before each shot (600 ms default).
* **Video:** `VIDEO=1` also writes `scripts/videos/workspace-{a,b}/*.webm` and holds each step for 2.5 s. It needs
  Playwright's ffmpeg (`npx playwright install ffmpeg`), which this machine does not have. Without it, the run stops
  with `Executable doesn't exist at ~/.cache/ms-playwright/ffmpeg-1011/ffmpeg-linux`.
* **Secret handling:** step 8 signs in as `manager`. The script reads `BIDBRAIN_DEMO_SECRET_MANAGER` from `.env` (or
  the environment) and types it into the masked Sign in field. It never prints the secret or writes it anywhere.
* **Cleanup:** at the end, the script retires the company rule it promoted, as the same principal. Company memory
  ends as it began. The two sessions expire by `SESSION_TTL`.
* **Before recording:** company memory must hold no active `role` rule (check the Memory page). Otherwise DEMO-3
  resolves itself in workspace A and step 8 has no conflict card to show.

## 3. The sequence

Every document in this demo is a **fixture**: DEMO-0, DEMO-1, DEMO-3 and DEMO-4 come from
`data/conflicts/demo_events.json`, with PDFs rendered at boot. They are synthetic, they are the same on every run,
and the UI lists them under Live documents → Inbox → **Scenarios**. Say so on camera.

The times below are for narrated recording. The script runs faster.

| # | Time | Screen (screenshot) | What it shows | Narration |
|---|------|---------------------|---------------|-----------|
| 1 | 0:00–0:12 | Control room (`01-control-room`) | T-NHX-2026-014 (4-laning of Greenfield Expressway, ₹250 Cr) is qualified **BID**, rules only. Open cases 1, nothing needs attention. | "The expressway tender qualifies: BID. But clause 2.1, similar works, rests on a single work, Ring Road Pkg-113, at ₹200 crore. That bid is one document away from failing." |
| 2 | 0:12–0:30 | Live documents → Scenarios (`02-fixtures-inbox`, `03-fixtures-sent`) | **Send** fixture DEMO-0 (revised completion certificate: Pkg-116 moves outside the 7-year window), then fixture DEMO-1 (corrigendum: Pkg-113 ₹200 Cr → ₹188 Cr). The Activity panel shows Auto-resolved (recency_same_issuer), the ripple and *Bid → No-bid*. | "These are demo fixtures, the same every run. First, a revised certificate pushes one work out of the window. Then the issuer's corrigendum restates Pkg-113 at ₹188 crore." |
| 3 | 0:30–0:42 | Change impact → corrigendum → Dependency graph, select *Claim · active* (`04-claim-evidence`) | The ₹188 Cr claim stands against two ₹200 Cr claims, now Superseded (DOC-CC-113, DOC-PPP-001). The Inspector shows the selected claim's evidence: source_doc DOC-DEMO-1, page 1, issuer, authority 95, and **Open DOC-DEMO-1 p.1**. | "The new claim contradicts two earlier ones. Here is the evidence behind the claim we kept: its document, its page, its issuer and its authority." |
| 4 | 0:42–0:55 | Change impact chain, steps 2–4 (`05-graph-traversal`) | Adjudication: *Recency (same issuer)*, "policy, no LLM", with the decision id. Affected requirements: clause **2.1**, Pass → **Fail**, "graph traversal". | "Policy settles it, with no LLM: same issuer, later document. Then the graph walks from that fact to every open requirement that depends on it." |
| 5 | 0:55–1:03 | Verdict change card (`06-verdict-change`) | Fact ₹200 Cr → ₹188 Cr; clause Pass → Fail; bid **Bid → No-bid**. Basis: *no option met: need 1 ≥ ₹200 Cr*. | "Deterministic requalification: the bid flips from BID to NO-BID." |
| 6 | 1:03–1:20 | Required actions (`07-action-pending` to `10-action-executed`) | *Re-approve bid* is **Pending**. Approve → Confirm approve → **Approved**. Execute → Confirm execute → **Executed**: *re-verified 6 clause(s); recommendation NO-BID approved*. *Publish to company record* stays Pending. | "The change raises an action. Nothing executes without approval. Execution re-verifies every clause against the graph and records the outcome in the audit trail." |
| 7 | 1:20–1:30 | **Open PDF** (`11-provenance-pdf`) | The corrigendum fixture PDF opens at page 1: *Contract Value (Original) Rs. 20,000.00 Lakh*, *Amended Entry INR 1,88,00,00,000/-*. | "And here is the page it came from." |
| 8 | 1:30–2:00 | Live documents (`12-rule-card`, `13-rule-promoted`), then a second session (`14-second-session`, `15-rule-reused`) | **Send** fixture DEMO-3. A card appears: *Prime* (portfolio, authority 60) vs *JV Partner* (partner certificate, authority 75). The two are close, so a human decides. **Choose and learn rule** on JV Partner. **Sign in** as `manager`, then **Promote to company memory** → `ORG-…`. In a new private window (separate session, not signed in), tick **Use company memory in this session** and send fixture DEMO-4. It is **Auto-resolved by company memory** (`org_rule`), and the rationale cites the company rule, the promoting session and the source decision. | "When sources are close, the system asks. The bid manager decides once and promotes the rule, signed in. A different session that opts in to company memory never sees that conflict again: the next certificate resolves itself and cites the rule." |

**Afterwards (manual run only):** retire the promoted rule as the principal who promoted it. Either sign in as
`manager` and retire it on the Memory page, or send `POST /api/memory/org/<ORG-id>/retire` with that principal's
cookie or Bearer token. The script does this automatically.

## 4. Verified run (2026-10-18, dev app at :7860)

* `CHROME=/usr/bin/google-chrome node record_demo.mjs`: 15 screenshots in 21.0 s.
* Qualify gave BID. After DEMO-0 and DEMO-1, the case was NO-BID. *Re-approve bid* went Pending → Approved → Executed.
* The PDF iframe rendered page 1.
* `ORG-2fbb985d8f` was promoted, then reused in the second session for DEMO-4. Retire returned HTTP 200.
* Afterwards: 0 active company rules, 0 active company-record facts, 0 pending publication requests.
