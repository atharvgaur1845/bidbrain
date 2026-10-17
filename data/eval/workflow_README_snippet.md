### Workflow evaluation (deterministic)

The answer benchmarks above score answers. `eval/workflow.py` scores the **company loop** itself, with no LLM
(`providers.chat` raises), on throwaway graphs only: FastAPI TestClient against the real API, session copies and a
private company graph, all deleted afterwards; the run checks that `bidbrain_base` and `bidbrain_org` are unchanged.
Each expectation comes from the fixtures (`demo_events.json`, `demo_tenders.json`), from separate Cypher, or from
re-running `qualify_clause` on a graph copy. It never comes from the function being tested.

    cd backend && .venv/bin/python -m eval.workflow          # ~25 s, writes data/eval/workflow_summary.json
    .venv/bin/python -m eval.workflow --report               # table of the last run

| dimension | cases | passed | what is checked |
|---|---:|---:|---|
| task_completion | 15 | 14 | corrigendum → review_bid → approve → execute sets `approved_recommendation`; renewal/letter actions create a `DocumentRequest`; rejected actions never run; execute is idempotent; a duplicate ripple adds no action |
| impact_recall | 18 | 18 | 9 events (DEMO-0..4, plus corrigenda aimed at T-RBC-2026-31 and T-JN-LB-2026-09). The flips `/api/impact` reports match a separate before/after re-run of every open clause: (case, clause) precision 1.00 and recall 1.00 (4/4 flips, 0 false), 198 clause re-checks |
| conflict (adversarial) | 16 | 16 | rounding vs just-past-precision, unrelated corrigenda (different subject or predicate) flip nothing, same-issuer recency, a later but less formal letter does not win by recency, close authority goes to a human, credentials expired at the bid date, absence PROVEN vs UNPROVEN (broken closure, unlinked letter text), aliases (`ISRO` not found, `phed odisha` resolves) |
| memory_transfer | 9 | 9 | a promoted rule applies in another session only after opt-in; contest/retire reopens; a published fact applies in new and existing workspaces with policy `company_record`, citing its OrgFact |
| isolation | 9 | 9 | session B sees none of A's cases, actions, turns, cards or uploads; A's ids return 404 from B; MCP `qualify_tender` refuses non-session graphs |
| temporal | 4 | 4 | `fact_as_of` 5 ms before and after the corrigendum; superseded claims keep their value and `invalidated_at` |
| provenance | 8 | 8 | every evidence row names an existing Document and a valid page; every Decision SELECTS one of its BASED_ON claims, and each of those comes FROM a document |
| **total** | **79** | **78** | conflict detection P/R is not repeated here (see `eval.conflict_pr`: 275 events, P = R = 1.0) |

The one failure is a real product finding. `renew_credential` actions are created for every PMP holder whose
credential lapses within 90 days (the first four the finding names). Some of these people are not proposed on the
clause, and some proposed people whose credential lapses may be left out. See `tc.renewal_actions_match_lapsing` in
`data/eval/results/workflow.jsonl`.
