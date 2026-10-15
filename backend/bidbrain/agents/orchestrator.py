"""The tender qualification team: six agents, one shared case graph.

    ClauseParser -> EvidenceHunter -> AbsenceAuditor -> ConflictSentinel -> Verifier -> Assembler

Each agent is an AgentRun node; it reads what the previous one wrote to the
Case subgraph and writes its own conclusions there (HANDED_OFF_TO links the
runs). Only two steps need language: reading the tender (ClauseParser) and
writing the memo (Assembler). Qualification, absence proofs, conflict checks
and verification are deterministic graph work — and the Verifier, which uses
no LLM at all, re-derives every verdict from the graph and fails loudly on any
disagreement. Both LLM steps degrade to deterministic fallbacks, so a demo can
never be stopped by a rate limit.
"""
from __future__ import annotations

import json
import time

from bidbrain.agents import clauses as clause_mod
from bidbrain.agents import providers
from bidbrain.agents.loop import run_agent
from bidbrain.agents.prompts import ASSEMBLER_SYSTEM
from bidbrain.graph.client import Graph
from bidbrain.tools import call
from bidbrain.tools import case as C
from bidbrain.tools.proof import prove_absence, qualify_clause

SAT_LABEL = {"similar_works": "Work", "key_personnel": "Person", "certification": "Certification"}
# which facts each clause kind actually depends on (ConflictSentinel only reports these)
RELEVANT = {"similar_works": ["value", "completed", "role", "client", "grading"],
            "turnover": ["revenue_operations"], "key_personnel": ["experience_years", "valid_through"],
            "certification": ["valid_until"], "years_in_business": [], "other": []}


def qualify_tender(g: Graph, tender: dict, emit=None, prefer_llm: bool = True) -> dict:
    emit = emit or (lambda e: None)
    t_start = time.perf_counter()
    case_id = C.case_open(g, title=tender.get("title", "Tender"), tender_text=tender.get("text", ""),
                          bid_date=tender.get("bid_date"), tender_ref=tender.get("id"),
                          category=tender.get("category"))["result"]["case_id"]
    emit({"type": "case_open", "case_id": case_id, "title": tender.get("title")})

    # 1 ── ClauseParser ──────────────────────────────────────────────────────
    r1 = C.agent_run(g, case_id, "ClauseParser")
    emit({"type": "agent_start", "agent": "ClauseParser", "run": r1})
    if tender.get("clauses") and not prefer_llm:
        parsed, how = tender["clauses"], "pre-parsed"
    else:
        parsed, how = clause_mod.parse(tender.get("text", ""), prefer_llm=prefer_llm)
    clause_ids = []
    for c in parsed:
        kid = C.add_clause(g, case_id, c, r1)
        clause_ids.append((kid, c))
        emit({"type": "clause", "agent": "ClauseParser", "clause_id": kid, "ref": c["id"], "kind": c["kind"],
              "text": c["text"], "params": c["params"]})
    C.agent_done(g, r1, method=how, n_clauses=len(parsed))
    emit({"type": "agent_done", "agent": "ClauseParser", "summary": f"{len(parsed)} clauses ({how})"})

    # 2 ── EvidenceHunter ────────────────────────────────────────────────────
    r2 = C.agent_run(g, case_id, "EvidenceHunter", prev_run=r1)
    emit({"type": "agent_start", "agent": "EvidenceHunter", "run": r2})
    verdicts = {}
    for kid, c in clause_ids:
        if c["kind"] == "other":
            C.set_verdict(g, kid, "DECLARE", "self-declaration / document to enclose — not provable from records",
                          [], r2)
            verdicts[kid] = {"verdict": "DECLARE", "satisfied_by": []}
            emit({"type": "verdict", "agent": "EvidenceHunter", "clause_id": kid, "ref": c["id"],
                  "verdict": "DECLARE", "basis": "declaration — include signed undertaking"})
            continue
        out = qualify_clause(g, c["kind"], c["params"], tender.get("bid_date"))
        res = out["result"]
        sat = []
        for s in res.get("satisfied_by") or []:
            label = SAT_LABEL.get(c["kind"])
            if label:
                sat.append((label, s["key"], {"credited": s.get("value") or s.get("experience_years") or 1}))
        if c["kind"] == "turnover":
            sat = [("FinancialStatement", f"FS-{y['fy']}", {"credited": y["revenue"]}) for y in res.get("years", [])]
        if c["kind"] == "years_in_business":
            sat = [("Company", "company", {"credited": 1})]
        C.set_verdict(g, kid, res["verdict"], res["basis"], sat, r2)
        verdicts[kid] = res
        emit({"type": "verdict", "agent": "EvidenceHunter", "clause_id": kid, "ref": c["id"], "kind": c["kind"],
              "verdict": res["verdict"], "basis": res["basis"], "subgraph": out.get("subgraph"),
              "evidence": out.get("evidence", [])[:8], "cypher": out.get("cypher", [])[:1]})
    C.agent_done(g, r2, n_clauses=len(clause_ids))
    emit({"type": "agent_done", "agent": "EvidenceHunter"})

    # 3 ── AbsenceAuditor ────────────────────────────────────────────────────
    r3 = C.agent_run(g, case_id, "AbsenceAuditor", prev_run=r2)
    emit({"type": "agent_start", "agent": "AbsenceAuditor", "run": r3})
    n_find = 0
    for kid, c in clause_ids:
        res = verdicts.get(kid) or {}
        if c["kind"] == "similar_works" and res.get("satisfied_by"):
            keys = [w["key"] for w in res["satisfied_by"]]
            missing = g.read("MATCH (w:Work) WHERE w.key IN $k AND NOT (w)-[:HAS_REFERENCE]->() "
                             "RETURN w.key AS key, w.title AS title", {"k": keys})
            if missing:
                proof = prove_absence(g, "reference_letter",
                                      client=g.scalar("MATCH (w:Work {key:$k})-[:FOR_CLIENT]->(c) RETURN c.key",
                                                      {"k": missing[0]["key"]}))
                txt = (f"No client reference letter on file for {', '.join(m['title'] for m in missing)} "
                       f"(proof: {proof['result']['checks']['corpus_closure']}; boundary "
                       f"{proof['result']['checks']['boundary_attested_by_3_views']} attested). "
                       f"Request letters from the client before submission or cite alternates.")
                C.add_finding(g, kid, "absence", txt, "high" if "reference" in c["text"].lower() else "medium",
                              [("Work", m["key"]) for m in missing], r3)
                n_find += 1
                emit({"type": "finding", "agent": "AbsenceAuditor", "clause_id": kid, "kind": "absence", "text": txt,
                      "subgraph": proof.get("subgraph")})
        if c["kind"] == "key_personnel":
            exp = res.get("expiring_within_90d") or []
            if exp:
                alts = ", ".join(a["name"] for a in res.get("safe_alternates", []))
                txt = (f"PMP of {', '.join(e['name'] + ' (to ' + e['valid_through'] + ')' for e in exp[:4])} lapses within "
                       f"90 days of the bid date. Renew now, or propose {alts or 'alternates'} (valid well beyond).")
                C.add_finding(g, kid, "expiry", txt, "high" if res.get("verdict") == "AT_RISK" else "low",
                              [("Person", e) for e in []], r3)
                n_find += 1
                emit({"type": "finding", "agent": "AbsenceAuditor", "clause_id": kid, "kind": "expiry", "text": txt})
        if c["kind"] == "similar_works" and res.get("verdict") == "FAIL" and res.get("near_misses"):
            nm = res["near_misses"][:3]
            txt = ("No qualifying combination. Closest works: " +
                   "; ".join(f"{w['title']} ({w['value'] / 1e7:.2f} Cr)" for w in nm) +
                   ". Consider a JV with a partner holding a larger bridge credential.")
            C.add_finding(g, kid, "gap", txt, "high", [("Work", w["key"]) for w in nm], r3)
            n_find += 1
            emit({"type": "finding", "agent": "AbsenceAuditor", "clause_id": kid, "kind": "gap", "text": txt})
    C.agent_done(g, r3, findings=n_find)
    emit({"type": "agent_done", "agent": "AbsenceAuditor", "summary": f"{n_find} findings"})

    # 4 ── ConflictSentinel ──────────────────────────────────────────────────
    r4 = C.agent_run(g, case_id, "ConflictSentinel", prev_run=r3)
    emit({"type": "agent_start", "agent": "ConflictSentinel", "run": r4})
    rows = g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause)-[:SATISFIED_BY]->(s)<-[:ABOUT]-(cl:Claim)
                     OPTIONAL MATCH (d:Decision)-[:BASED_ON]->(cl)
                     WITH k, s, cl, collect(DISTINCT d) AS ds
                     WHERE cl.status IN ['contested','rejected','superseded']
                       AND cl.predicate IN $preds[k.kind]
                     RETURN k.id AS clause_id, labels(s)[0] AS label, s.key AS key,
                            coalesce(s.title, s.name, s.key) AS name, cl.predicate AS predicate,
                            cl.value_str AS value, cl.source_doc AS doc, cl.status AS status, cl.id AS claim,
                            [x IN ds | {policy: x.policy, why: x.rationale, chosen: x.chosen_value}] AS decisions""",
                  {"c": case_id, "preds": RELEVANT})
    seen = set()
    for r in rows:
        if (r["clause_id"], r["key"], r["predicate"]) in seen:
            continue
        seen.add((r["clause_id"], r["key"], r["predicate"]))
        if r["status"] == "contested":
            txt = (f"{r['name']}: '{r['predicate']}' is contested ({r['value']} in {r['doc']}) — "
                   f"open resolve card; verdict relies on an unresolved fact.")
            sev = "high"
        else:
            d = r["decisions"][0] if r["decisions"] else {}
            txt = (f"{r['name']}: '{r['predicate']}' had a competing value {r['value']} ({r['doc']}) "
                   f"resolved by {d.get('policy', 'policy')} → {d.get('chosen', '?')}. {d.get('why', '')}")
            sev = "info"
        C.add_finding(g, r["clause_id"], "conflict", txt, sev, [("Claim", r["claim"])], r4)
        emit({"type": "finding", "agent": "ConflictSentinel", "clause_id": r["clause_id"], "kind": "conflict",
              "text": txt, "severity": sev})
    C.agent_done(g, r4, findings=len(seen))
    emit({"type": "agent_done", "agent": "ConflictSentinel", "summary": f"{len(seen)} facts with history"})

    # 5 ── Verifier (no LLM) ────────────────────────────────────────────────
    r5 = C.agent_run(g, case_id, "Verifier", prev_run=r4, uses_llm=False)
    emit({"type": "agent_start", "agent": "Verifier", "run": r5})
    stored = g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause)
                       OPTIONAL MATCH (k)-[:SATISFIED_BY]->(s)
                       RETURN k.id AS id, k.kind AS kind, k.params AS params, k.verdict AS verdict,
                              collect(s.key) AS sat""", {"c": case_id})
    mismatches = []
    for s in stored:
        if s["kind"] == "other":
            continue
        fresh = qualify_clause(g, s["kind"], json.loads(s["params"] or "{}"), tender.get("bid_date"))["result"]
        fresh_keys = sorted(x["key"] for x in fresh.get("satisfied_by") or [] if "key" in x)
        stored_keys = sorted(k for k in s["sat"] if k and not k.startswith("FS-") and k != "company")
        ok = fresh["verdict"] == s["verdict"] and (not fresh_keys or fresh_keys == stored_keys)
        if not ok:
            mismatches.append({"clause": s["id"], "stored": s["verdict"], "fresh": fresh["verdict"]})
            C.add_finding(g, s["id"], "verification_failed",
                          f"Verifier re-derived {fresh['verdict']} but the case says {s['verdict']}", "high", [], r5)
    C.agent_done(g, r5, mismatches=len(mismatches))
    g.run("MATCH (c:Case {id:$c}) SET c.verified = $v", {"c": case_id, "v": not mismatches})
    emit({"type": "verified", "agent": "Verifier", "ok": not mismatches, "mismatches": mismatches,
          "checked": sum(1 for s in stored if s["kind"] != "other")})

    # 6 ── Assembler ─────────────────────────────────────────────────────────
    r6 = C.agent_run(g, case_id, "Assembler", prev_run=r5)
    emit({"type": "agent_start", "agent": "Assembler", "run": r6})
    rec = C.case_get(g, case_id)["result"]
    prec = call(g, "precedents", {"category": tender.get("category")})
    for p in prec.get("result", [])[:2]:
        g.run("MATCH (c:Case {id:$c}), (b:Bid {key:$b}) MERGE (c)-[:PRECEDENT]->(b)", {"c": case_id, "b": p["bid"]})
    verds = [c["verdict"] for c in rec["clauses"] if c["verdict"] != "DECLARE"]
    sev = [f["severity"] for c in rec["clauses"] for f in c["findings"] if f]
    overall = "NO-BID" if "FAIL" in verds else ("BID WITH FIXES" if "AT_RISK" in verds or "high" in sev else "BID")
    memo, how = None, "template"
    if providers.available() or providers.REPLAY:
        try:
            ctx = json.dumps({"tender": {k: tender.get(k) for k in ("id", "title", "authority", "bid_date")},
                              "overall": overall, "clauses": rec["clauses"],
                              "precedents": prec.get("result", [])[:2]}, default=str)[:14000]
            res = run_agent(g, "Assembler", ASSEMBLER_SYSTEM, ctx, ["who_knows", "key_people"], max_steps=4, emit=emit)
            if res.final and not res.error:
                memo, how = res.final, "llm"
        except Exception:
            memo = None
    if not memo:
        memo = _template_memo(tender, rec, overall, prec.get("result", []))
    g.run("MATCH (c:Case {id:$c}) SET c.memo = $m, c.verdict = $v, c.status = 'open'",
          {"c": case_id, "m": memo, "v": overall})
    C.agent_done(g, r6, method=how)
    emit({"type": "memo", "agent": "Assembler", "markdown": memo, "overall": overall, "method": how})
    final = C.case_get(g, case_id)["result"]
    final["ms"] = round((time.perf_counter() - t_start) * 1000, 1)
    emit({"type": "done", "case_id": case_id, "overall": overall, "ms": final["ms"]})
    return final


def _template_memo(tender: dict, rec: dict, overall: str, precedents: list) -> str:
    lines = [f"## {tender.get('title')} — recommendation: **{overall}**", "",
             f"Authority: {tender.get('authority', '—')} · bid due {tender.get('bid_date', '—')}", "",
             "| Clause | Verdict | Basis |", "|---|---|---|"]
    for c in rec["clauses"]:
        lines.append(f"| {c['ref']} {c['kind']} | {c['verdict']} | {(c.get('basis') or '').replace('|', '/')} |")
    risks = [(c["ref"], f) for c in rec["clauses"] for f in c["findings"] if f and f.get("severity") in ("high", "medium")]
    if risks:
        lines += ["", "### Gaps & risks"] + [f"- **{ref}** ({f['kind']}): {f['text']}" for ref, f in risks]
    if precedents:
        lines += ["", "### Precedents"] + [f"- {p['bid']} ({p['category']}, {p['client']}, submitted {p['submitted']})"
                                           for p in precedents[:2]]
    return "\n".join(lines)
