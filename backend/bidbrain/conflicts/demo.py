"""Curated demo scenarios that chain into the demo tenders.

    python -m bidbrain.conflicts.demo     (writes data/conflicts/demo_events.json + PDFs)

Each is an ordinary event (same claim layer, same engine) chosen so the ripple
is visible: a corrigendum that knocks the one work carrying a clause below its
threshold, revised audited accounts that lift a failing turnover year, two JV
certificates that show a learned rule resolving the second one by itself.
"""
from __future__ import annotations

import json
import random

from bidbrain import config
from bidbrain.conflicts.render_pdf import render
from bidbrain.graph.client import Graph


def build(g: Graph) -> list[dict]:
    w113 = g.read("""MATCH (w:Work)-[:FOR_CLIENT]->(c:Client) WHERE w.title ENDS WITH 'Pkg-113'
                     RETURN w.key AS key, w.title AS title, w.value AS value, c.name AS client""")[0]
    primes = g.read("""MATCH (w:Work {role:'Prime'})-[:FOR_CLIENT]->(c:Client) WHERE w.category IN ['buildings','small buildings']
                       RETURN w.key AS key, w.title AS title, c.name AS client ORDER BY w.value DESC LIMIT 2""")
    bond = g.read("MATCH (b:Bid)-[:SECURED_BY]->(bd:Bond)-[:ISSUED_BY]->(k:Bank) RETURN bd.key AS key, bd.status AS status, k.name AS bank LIMIT 1")
    ev = []

    def add(eid, render_as, doc_type, issuer, issued, label, key, pred, value, kind, title, narrative):
        ev.append({"event_id": eid, "type": "demo", "render_as": render_as,
                   "expect": {"conflict": True, "route": "auto", "winner": "new"},
                   "doc": {"doc_id": f"DOC-{eid}", "doc_type": doc_type, "issuer": issuer, "issued": issued, "title": title},
                   "claims": [{"subject_label": label, "subject_key": key, "predicate": pred, "value": value,
                               "source_kind": kind, "issuer": issuer, "observed": issued, "page": 1}],
                   "narrative": narrative})

    w116 = g.read("""MATCH (w:Work)-[:FOR_CLIENT]->(c:Client) WHERE w.title ENDS WITH 'Pkg-116'
                     RETURN w.key AS key, w.title AS title, w.completed AS completed, c.name AS client""")[0]
    add("DEMO-0", "revised_completion", "revised_completion_certificate", w116["client"], "2026-05-12", "Work",
        w116["key"], "completed", "2019-04-18", "revised_certificate", f"Revised Completion Certificate — {w116['title']}",
        f"{w116['client']} re-issues the certificate for {w116['title']}: completed 2019-04-18 (was {w116['completed']}). "
        f"That moves it outside the 7-year window — the NEDA bid now rests on a single ₹200 Cr work.")
    add("DEMO-1", "corrigendum_value", "corrigendum", w113["client"], "2026-05-20", "Work", w113["key"], "value",
        1_880_000_000, "client_corrigendum", f"Corrigendum — {w113['title']}",
        f"{w113['client']} revises {w113['title']} from ₹{w113['value'] / 1e7:.0f} Cr to ₹188 Cr. It is the one work "
        f"left carrying the NEDA expressway tender's similar-works clause (send the revised certificate first).")
    add("DEMO-2", None, "revised_financial_statement", "auditor", "2026-04-28", "FinancialStatement", "FS-2024-25",
        "revenue_operations", 2_415_000_000, "financial_statement", "Revised audited statement — FY2024-25",
        "The auditor re-issues FY2024-25 accounts: revenue from operations ₹241.5 Cr (was ₹238.92 Cr) — the year the "
        "buildings tender failed on by ₹1.08 Cr.")
    if primes:
        p = primes[0]
        add("DEMO-3", "jv_role_dispute", "partner_certificate", "Vardhaman Infraprojects Ltd.", "2026-05-02", "Work",
            p["key"], "role", "JV Partner", "partner_certificate", f"JV Experience Certificate — {p['title']}",
            f"Vardhaman Infraprojects, as JV lead, certifies our role in {p['title']} as 'JV Partner' — our portfolio "
            f"says 'Prime'. Close authority, different issuers: you decide (try 'Choose + learn rule').")
    if len(primes) > 1:
        p = primes[1]
        add("DEMO-4", "jv_role_dispute", "partner_certificate", "Konark Buildcon Pvt. Ltd.", "2026-05-09", "Work",
            p["key"], "role", "JV Partner", "partner_certificate", f"JV Experience Certificate — {p['title']}",
            f"A second JV lead certifies {p['title']} as 'JV Partner'. If you taught a rule on the first one, this "
            f"resolves itself and cites your decision.")
    if bond:
        b = bond[0]
        add("DEMO-5", "bond_invoked", "bank_notice", b["bank"], "2026-05-15", "Bond", b["key"], "status", "Invoked",
            "bank_notice", f"Notice of Invocation — {b['key']}",
            f"{b['bank']} invokes performance bond {b['key']} — the bid security of a past bid. See what it secured.")
    return ev


if __name__ == "__main__":
    g = Graph()
    evs = build(g)
    out = config.DATA / "conflicts" / "demo_events.json"
    out.write_text(json.dumps(evs, indent=1, ensure_ascii=False))
    for e in evs:
        if e.get("render_as"):
            render({**e, "type": e["render_as"]}, g, random.Random(e["event_id"]))
    print(len(evs), "demo events ->", out)
    # sample PDFs for the Live page: three of each planted type (incl. hard negatives)
    import collections
    seen = collections.Counter()
    for e in json.loads((config.DATA / "conflicts" / "events.json").read_text()):
        if e["type"] != "llm_grading" and seen[e["type"]] < 3:
            seen[e["type"]] += 1
            render(e, g, random.Random(e["event_id"]))
    print(sum(seen.values()), "sample PDFs rendered")
