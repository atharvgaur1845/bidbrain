"""Conflict-engine evaluation on the PDF path: render -> extract -> resolve -> detect -> route.

    python -m eval.conflict_pdf [--per-type 3]

Unlike the JSON path this exercises the parts that can genuinely fail:
layout-preserving extraction, label/value parsing of new document kinds, and
entity resolution from varied title spellings ("Pkg 151", "(Package 151)").
"""
from __future__ import annotations

import argparse
import collections
import json
import random

from bidbrain import config
from bidbrain.conflicts.ingest_doc import ingest_pdf
from bidbrain.conflicts.render_pdf import render
from bidbrain.graph.client import Graph


def run(per_type: int = 3, seed: int = 11) -> dict:
    events = json.load(open(config.DATA / "conflicts" / "events.json"))
    rnd = random.Random(seed)
    by_type = collections.defaultdict(list)
    for e in events:
        if e["type"] != "llm_grading":
            by_type[e["type"]].append(e)
    chosen = [e for t, es in sorted(by_type.items()) for e in rnd.sample(es, min(per_type, len(es)))]
    base = Graph()
    rows = []
    for ev in chosen:
        path = render(ev, base, random.Random(ev["event_id"]))
        g = base.copy_to(f"eval_pdf_{ev['event_id']}")
        try:
            out = ingest_pdf(g, path, doc_id=ev["doc"]["doc_id"])
            exp_claim = ev["claims"][0]
            subj_ok = (out.get("subject") or {}).get("key") == exp_claim["subject_key"]
            got = {x["predicate"]: x["value"] for x in out.get("extracted", [])}
            gv, ev_ = got.get(exp_claim["predicate"]), exp_claim["value"]
            if ev["type"] == "rounding_restatement" and isinstance(gv, int):
                val_ok = abs(gv - ev_) <= 50_000          # brochure prints 0.01 Cr by design
            else:
                val_ok = str(gv) == str(ev_)
            mine = set(out.get("claims", []))
            dec = [d for d in out.get("decisions", []) if mine & {c["id"] for c in d["conflict"]["claims"]}]
            cards = [c for c in out.get("cards", []) if mine & {x["id"] for x in c["conflict"]["claims"]}]
            route = "auto" if dec else ("human" if cards else "none")
            winner = ("new" if dec[-1]["winner"] in mine else "old") if dec else None
            rows.append({"event_id": ev["event_id"], "type": ev["type"], "file": path.name,
                         "subject_ok": subj_ok, "value_ok": val_ok, "resolved_via": (out.get("subject") or {}).get("resolved_via"),
                         "detected": bool(dec or cards), "route": route, "winner": winner, "expect": ev["expect"],
                         "error": out.get("error")})
        finally:
            g.delete()
    n = len(rows)
    tp = sum(1 for r in rows if r["detected"] and r["expect"]["conflict"])
    fp = sum(1 for r in rows if r["detected"] and not r["expect"]["conflict"])
    fn = sum(1 for r in rows if not r["detected"] and r["expect"]["conflict"])
    p = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    summary = {"n_pdfs": n,
               "extraction": {"subject_resolution": round(sum(r["subject_ok"] for r in rows) / n, 4),
                              "value_extraction": round(sum(r["value_ok"] for r in rows) / n, 4)},
               "detection": {"precision": round(p, 4), "recall": round(rc, 4),
                             "f1": round(2 * p * rc / (p + rc), 4) if p + rc else 0.0, "tp": tp, "fp": fp, "fn": fn},
               "routing_accuracy": round(sum(1 for r in rows if r["expect"]["conflict"] and r["route"] == r["expect"]["route"])
                                         / max(1, sum(1 for r in rows if r["expect"]["conflict"])), 4),
               "resolved_via": dict(collections.Counter(r["resolved_via"] for r in rows))}
    return {"summary": summary, "rows": rows}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-type", type=int, default=3)
    a = ap.parse_args()
    res = run(a.per_type)
    json.dump(res, open(config.DATA / "eval" / "conflict_pr_pdf.json", "w"), indent=1)
    print(json.dumps(res["summary"], indent=1))
    for r in res["rows"]:
        if not (r["subject_ok"] and r["value_ok"]) or r["error"] or r["route"] != r["expect"]["route"]:
            print("  ISSUE", r)
