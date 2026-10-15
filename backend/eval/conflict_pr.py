"""Conflict-engine evaluation on the planted stream (JSON path).

    python -m eval.conflict_pr [--events data/conflicts/events.json]

Each event is applied to a fresh copy of the base graph (GRAPH.COPY), so events
cannot interact. Scored on three things:
  detection  P / R / F1 of "a contradiction was detected" vs expect.conflict
  routing    of true conflicts, was it auto-resolved vs sent to a human correctly
  winner     of auto-resolved conflicts, did the graph end up showing the right claim
"""
from __future__ import annotations

import argparse
import collections
import json
import time

from bidbrain import config
from bidbrain.conflicts.events import apply_event
from bidbrain.graph.client import Graph


def evaluate(events: list[dict], base: str = config.BASE_GRAPH) -> dict:
    src = Graph(base)
    rows = []
    t0 = time.time()
    for ev in events:
        tmp = f"eval_cf_{ev['event_id']}"
        g = src.copy_to(tmp)
        try:
            out = apply_event(g, ev)
            mine = set(out["claims"])
            dec = [d for d in out["decisions"] if mine & {c["id"] for c in d["conflict"]["claims"]}]
            cards = [c for c in out["cards"] if mine & {x["id"] for x in c["conflict"]["claims"]}]
            detected = bool(dec or cards)
            route = "auto" if dec else ("human" if cards else "none")
            winner = None
            if dec:
                winner = "new" if dec[-1]["winner"] in mine else "old"
            rows.append({"event_id": ev["event_id"], "type": ev["type"], "expect": ev["expect"],
                         "detected": detected, "route": route, "winner": winner,
                         "policy": dec[-1]["policy"] if dec else None})
        finally:
            g.delete()
    secs = time.time() - t0
    tp = sum(1 for r in rows if r["detected"] and r["expect"]["conflict"])
    fp = sum(1 for r in rows if r["detected"] and not r["expect"]["conflict"])
    fn = sum(1 for r in rows if not r["detected"] and r["expect"]["conflict"])
    p = tp / (tp + fp) if tp + fp else 0.0
    rcl = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * rcl / (p + rcl) if p + rcl else 0.0
    true_conf = [r for r in rows if r["expect"]["conflict"]]
    route_ok = sum(1 for r in true_conf if r["route"] == r["expect"]["route"])
    autos = [r for r in true_conf if r["expect"]["route"] == "auto" and r["route"] == "auto"]
    win_ok = sum(1 for r in autos if r["winner"] == r["expect"]["winner"])
    per_type = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        ok = (r["detected"] == r["expect"]["conflict"] and r["route"] == r["expect"]["route"]
              and (r["expect"]["winner"] is None or r["winner"] == r["expect"]["winner"]))
        per_type[r["type"]]["n"] += 1
        per_type[r["type"]]["correct"] += ok
    summary = {"n_events": len(rows), "detection": {"precision": round(p, 4), "recall": round(rcl, 4),
                                                     "f1": round(f1, 4), "tp": tp, "fp": fp, "fn": fn},
               "routing_accuracy": round(route_ok / len(true_conf), 4) if true_conf else None,
               "winner_accuracy": round(win_ok / len(autos), 4) if autos else None,
               "per_type": {k: dict(v) for k, v in sorted(per_type.items())},
               "seconds": round(secs, 1)}
    return {"summary": summary, "rows": rows}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(config.DATA / "conflicts" / "events.json"))
    ap.add_argument("--out", default=str(config.DATA / "eval" / "conflict_pr_json.json"))
    a = ap.parse_args()
    res = evaluate(json.load(open(a.events)))
    json.dump(res, open(a.out, "w"), indent=1)
    print(json.dumps(res["summary"], indent=1))
    bad = [r for r in res["rows"] if not (r["detected"] == r["expect"]["conflict"] and r["route"] == r["expect"]["route"])]
    for r in bad[:10]:
        print("  WRONG", r)
