"""Tool-sufficiency gate: can the graph tools reproduce every answer?

For each of the 354 questions, take the *parameters* the old deterministic
engine extracted (its trace: client, scope, op, filters) and answer using only
BidBrain's graph tools. If the tools reproduce the engine's numbers, then any
accuracy the LLM agent loses later is the agent's fault (planning, resolution),
not the graph's — which is what makes the benchmark interpretable.

    python -m eval.oracle [--set S21|Q333|all] [--show-misses]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys

from bidbrain import config
from bidbrain.graph.client import Graph
from bidbrain.tools import read as T

sys.path.insert(0, str(config.VENDOR / "src"))
import answer as OLD  # noqa: E402  (vendored engine: used only for its regexes here)

QFILE = config.DATA / "eval" / "questions_with_engine.jsonl"


def _close(a, b) -> bool:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= max(0.01, 1e-6 * abs(b))


def oracle_answer(g: Graph, rec: dict):
    t, q = rec["trace"], rec["question"]
    op = (t.get("op") or "").replace(" (signed)", "")
    if op == "collection_rate":
        return T.collection_rate(g, client=t["client"])["result"]["rate_pct"]
    if op == "awarded_vs_billed":
        return T.awarded_vs_billed(g, client=t["client"])["result"]["gap"]
    if t.get("block") == "invoices":
        dims = ast.literal_eval(t["dims"]) if t.get("dims") else {}
        return T.receivables(g, measure=t["measure"], client=dims.get("client"))["result"]["total"]

    scope = t.get("scope") or ""
    kw: dict = {}
    if t.get("exclude_category"):
        kw["exclude_categories"] = [t["exclude_category"]]
    if t.get("threshold"):
        kw["min_value"] = int(t["threshold"].split()[1])
    if t.get("after"):
        kw["completed_after"] = t["after"]
    if t.get("before"):
        kw["completed_before"] = t["before"]
    if t.get("year") and not t.get("years_are_the_measure"):
        kw["years"] = [int(y) for y in t["year"].split("/")]

    if op == "date_span":
        kind = "six sigma" if re.search(r"six sigma|6s-", q, re.I) else ("pmp" if re.search(r"\bpmp\b", q, re.I) else None)
        return T.date_span(g, person=t["person"], credential_kind=kind, work=t["work"])["result"]["days"]

    if scope.startswith("client_via_work"):
        res = T.find_portfolio(g, via_work=t["work"], **kw)
    elif scope.startswith("client"):
        res = T.find_portfolio(g, client=t["client"], **kw)
    elif scope == "person":
        res = T.find_portfolio(g, person=t["person"], **kw)
    else:
        raise ValueError(f"unhandled scope {scope}")
    keys = [w["key"] for w in res["result"]["works"]]
    akw: dict = {}
    if op == "category_difference":
        a, b = ast.literal_eval(t["categories"])
        akw = {"category_a": a, "category_b": b}
    elif op == "year_difference":
        ya, yb = sorted(int(y) for y in ast.literal_eval(t["years"]))
        akw = {"year_a": ya, "year_b": yb}
    elif op == "gap_to_threshold":
        akw = {"threshold": OLD.parse_threshold(q)}
    elif op == "mean_median_gap":
        akw = {"signed": "(signed)" in t["op"]}
    elif op == "referenced_share":
        akw = {"absent": bool(re.search(OLD.ABSENT, q.lower()))}
    return T.aggregate(g, work_keys=keys, op=op, **akw)["result"]["value"]


def run(which: str = "all", show: bool = False) -> dict:
    g = Graph()
    recs = [json.loads(l) for l in open(QFILE)]
    if which != "all":
        recs = [r for r in recs if r["set"] == which]
    stats = {"n": 0, "match_engine": 0, "match_gold": 0, "n_gold": 0, "errors": 0}
    misses = []
    for r in recs:
        stats["n"] += 1
        try:
            got = oracle_answer(g, r)
        except Exception as exc:
            stats["errors"] += 1
            misses.append((r["qid"], r["trace"].get("op"), f"ERR {type(exc).__name__}: {exc}", r["engine"]))
            continue
        if _close(got, r["engine"]):
            stats["match_engine"] += 1
        else:
            misses.append((r["qid"], r["trace"].get("op"), got, r["engine"]))
        if "gold" in r:
            stats["n_gold"] += 1
            stats["match_gold"] += _close(got, r["gold"])
    print(json.dumps(stats))
    if which == "all":
        (config.DATA / "eval" / "tool_oracle.json").write_text(json.dumps({"summary": stats, "misses": misses}, indent=1, default=str))
    if show:
        for m in misses:
            print("  MISS", *m)
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="all")
    ap.add_argument("--show-misses", action="store_true")
    a = ap.parse_args()
    run(a.set, a.show_misses)
