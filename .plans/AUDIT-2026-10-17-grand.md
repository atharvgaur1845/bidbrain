# Grand-prize audit, 17 Oct 2026 13:30 IST (HEAD 4b1ee19 + round-3 work in flight)

Sources: official rules + brief (re-read today), the code, 59 tests, oracle 354/354, two judging rounds
(.plans/JUDGING-2026-10-17*.md). Official rule that matters most: "If the product still works with FalkorDB removed,
the graph is decoration." No published rubric; the weights below are our own.

## Score vs the unofficial rubric (as of now)

| Dimension (weight) | Implemented | Proven | Only claimed / weak | Est. |
|---|---|---|---|---|
| FalkorDB central (25) | All 28 tools are Cypher; prove_absence, blast_radius, conflict engine, algo.*, GRAPH.COPY, vector+FT | oracle 354/354; sponsor re-ran every recipe | most tools 1-2 hop composed in Python; communities weak | 21 |
| Company workflow (20) | qualify -> ripple on corrigendum -> verdict flip | DEMO-0/1 flips BID->NO-BID (tests) | **stops at a recommendation**: no action, no owner, no follow-up, memo stale after ripple; company state never leaves the session | 12 |
| Reasoning/evidence (15) | evidence table with doc+page, decision trace, fact_history | judges verified traces | either/or clause misparse; memo LLM can cite docs not in evidence; Verifier re-runs same function | 11 |
| Memory & multi-agent (15) | Case subgraph handoffs, PolicyRule, org rules, MENTIONS recall | T2 verified promote->org_rule in session B | org memory in RAM only; retire doesn't recheck; facts never shared across sessions | 9 |
| UX & demo clarity (10) | Sprig+glass UI, 8 pages | screenshots | reads as AI-generated glass dashboard; no "what needs attention" screen; impact chain not a first-class view | 6 |
| Reliability & eval (10) | S21/Q333/ABS/TND, CIs, baselines, disclosures | selfcheck 40/40 | pre-final single runs; Q333 engine-derived; TND fixed on test set; demo LLM pool flaky | 6 |
| Submission readiness (5) | README, CYPHER.md, docker-compose, Dockerfile | local run | no deployment, no video (team) | 3 |
| **Total** | | | | **~68/100** |

## The 10 questions
1. Graph determines decisions? **Yes** for qualify (deterministic qualify_clause over Cypher) and conflicts (policy over
   Claim authority). The LLM only parses clauses (validated, rule parser cross-check) and writes the memo.
2. Affected set without text retrieval? **Mostly**: blast_radius walks Claim-ABOUT->subject<-SATISFIED_BY-Clause and
   re-checks open clauses of dependent kinds by graph query. No variable-length lineage yet.
3. Agent action recorded and reused next session? **No.** Ripple writes verdicts/findings into a private session graph;
   nothing becomes an accountable task, nothing reaches company state. <- biggest gap.
4. Shared memory changes a later outcome? **Yes, rules only** (org_rule auto-resolves in session B). Facts no.
5. Full trace decision -> evidence -> page -> claim -> policy -> agent run -> downstream action? Up to agent run yes;
   **downstream action missing**.
6. Disagreement/ambiguity/missing: conflict cards, NEEDS REVIEW, prove_absence UNPROVEN; resolve_entity never says
   not_found (fixing in R3).
7. LLM failure: qualify falls back to rule parser; Ask loses answer on 429 (fixing in R3).
8. Isolation: GRAPH.COPY per session, tested; org governance weak (fixing in R3).
9. Deterministic demo without LLM: qualify(use_llm=false) + fired events + resolve all deterministic. **Yes** (needs a
   one-click scripted path and to be the documented default).
10. Differentiators: proof-of-absence with corpus closure; claim->decision->policy-rule ledger with ripple;
    deterministic CPWD qualification with verifier. Baseline-ish: vector/FT search, chat, graph viz.

## What judges should remember
"BidBrain turns changing evidence into auditable business decisions and coordinated actions."
Loop to make real end to end: new document -> claim -> conflict adjudication -> graph impact traversal -> deterministic
requalification -> accountable actions (owner, approval gate, execution, audit) -> company record + learned rule ->
a clean second session sees the new company state and applies the rule, with no private memory leakage.

## Plan (priority order)
- P0: R3 fixes land green; deterministic demo path is the default; docker compose fresh-run check; persistence.
- P1: Action layer (Action nodes, lifecycle pending->approved/rejected->executed, idempotency, audit ActionEvent),
  created by the ripple and by findings; "publish to company record" (OrgFact in bidbrain_org, replayed into new
  sessions through the real engine); bitemporal claims (recorded_at / invalidated_at, fact_as_of); impact API.
- P2: light enterprise UI (tokens, Shell), Bid Control Room overview, Change-impact center (signature), tender
  workspace, decision & memory center, evidence explorer, honest benchmark page.
- P3: tests for the loop (task completion, memory transfer, isolation, idempotency, temporal), adversarial cases,
  re-run benchmark on final code (GPU job 399564/399236 pending), keep run-1 artifacts.
- P4: README feature/requirement matrix, reuse + AI disclosure, 2-minute demo script, final judge round, report.
