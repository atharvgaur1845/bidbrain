"""Q&A benchmark runner.

    LLM_PROVIDERS=vllm python -m eval.run --system bidbrain --set S21 [--workers 12] [--limit N]
    python -m eval.run --summarize          # merge every results file into data/eval/qa_summary.json

Systems (all on the same model and the same FalkorDB instance):
    bidbrain      single agent over BidBrain's graph tools
    vector_rag    top-k chunks from FalkorDB's vector index -> LLM
    text2cypher   LLM writes Cypher from the schema -> rows -> LLM
    graphrag_sdk  FalkorDB GraphRAG-SDK, auto-extracted schema (see eval/baselines/graphrag_sdk.py)
Sets:
    S21   21 sample questions, true gold
    Q333  333 hidden questions, near-gold (the deterministic engine, 99.775 on the leaderboard)
    ABS   synthesized absence questions, gold from Cypher            (eval/synth.py)
    TND   synthesized tender-clause questions, gold from Cypher/Python (eval/synth.py)

Scores: exact (within 0.5% relative, or 0.01 absolute for percentages, exact for counts/days)
and the dataset's official proportional credit, max(0, 1 - |got-gold|/|gold|).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import statistics
import time

from bidbrain import config
from bidbrain.graph.client import Graph

OUT = config.DATA / "eval" / "results"


def load_set(name: str) -> list[dict]:
    if name in ("S21", "Q333"):
        rows = [json.loads(l) for l in open(config.DATA / "eval" / "questions_with_engine.jsonl")]
        out = []
        for r in rows:
            if r["set"] != name:
                continue
            gold = r.get("gold", r["engine"])
            out.append({"qid": r["qid"], "question": r["question"], "answer_type": r.get("answer_type"),
                        "gold": gold, "hops": r.get("hops") or hops_of(r["trace"]), "family": family(r["trace"]),
                        "absence": r["trace"].get("op") in ("absence_count", "referenced_share")})
        return out
    path = config.DATA / "eval" / f"{name.lower()}_set.json"
    return json.load(open(path))


def family(t: dict) -> str:
    return t.get("block") or t.get("op", "?").replace(" (signed)", "")


def hops_of(t: dict) -> int:
    """Hop count of the canonical traversal, calibrated on the 21 sample questions' own `hops`."""
    op, scope = (t.get("op") or "").replace(" (signed)", ""), t.get("scope") or ""
    if t.get("block") == "invoices":
        return 2
    if op == "collection_rate":
        return 4 if t.get("via_work") else 2
    if op == "awarded_vs_billed":
        return 3
    if op == "absence_count":
        return 3
    if op in ("date_span", "distinct_categories"):
        return 4
    if scope.startswith("client_via_work"):
        return 6 if op == "mean_median_gap" else 5
    if op in ("gap_to_threshold", "rank_gap", "referenced_share", "mean_median_gap",
              "category_difference", "year_difference"):
        return 5
    if t.get("exclude_category") or t.get("threshold"):
        return 5
    return 4


def band(h: int) -> str:
    return "2-3" if h <= 3 else ("4" if h == 4 else "5-6")


def score(got, gold, answer_type=None) -> tuple[bool, float]:
    if isinstance(gold, str) or isinstance(got, str):
        ok = str(got).strip().lower() == str(gold).strip().lower()
        return ok, 1.0 if ok else 0.0
    try:
        got, gold = float(got), float(gold)
    except (TypeError, ValueError):
        return False, 0.0
    prop = (1.0 if got == gold else 0.0) if gold == 0 else max(0.0, 1 - abs(got - gold) / abs(gold))
    if answer_type in ("count", "days"):
        exact = round(got) == round(gold)
    elif answer_type == "percent":
        exact = abs(got - gold) <= 0.011
    else:
        exact = abs(got - gold) <= max(1.0, 0.005 * abs(gold))
    return exact, prop


def answer(system: str, g: Graph, q: dict) -> dict:
    from bidbrain.agents import baselines, qa
    if system == "bidbrain":
        r = qa.ask(g, q["question"])
        return {"answer": r["answer"], "raw": r["answer_text"], "usage": r["usage"], "ms": r["ms"],
                "error": r["error"], "steps": [s["tool"] for s in r["steps"]]}
    if system == "vector_rag":
        r = baselines.vector_rag(g, q["question"])
        return {"answer": r["answer"], "raw": r["answer_text"], "usage": r["usage"], "ms": r["ms"]}
    if system == "text2cypher":
        r = baselines.text2cypher(g, q["question"])
        return {"answer": r["answer"], "raw": r["answer_text"], "usage": r["usage"], "ms": r["ms"],
                "cypher": r["cypher"], "error": r["error"]}
    if system == "graphrag_sdk":
        from eval.baselines.graphrag_sdk import ask_sdk
        return ask_sdk(q["question"])
    raise ValueError(system)


def run(system: str, set_name: str, workers: int = 8, limit: int | None = None) -> dict:
    qs = load_set(set_name)[: limit or None]
    g = Graph()            # read-only questions: the base graph is safe to share
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{system}__{set_name}.jsonl"
    done = {}
    if path.exists():
        for l in open(path):
            r = json.loads(l)
            if not r.get("error"):
                done[r["qid"]] = r
    todo = [q for q in qs if q["qid"] not in done]
    print(f"{system} on {set_name}: {len(done)} cached, {len(todo)} to run")

    def one(q):
        t0 = time.time()
        try:
            r = answer(system, g, q)
        except Exception as exc:
            r = {"answer": None, "error": f"{type(exc).__name__}: {exc}"}
        ok, prop = score(r.get("answer"), q["gold"], q.get("answer_type"))
        return {**q, **r, "exact": ok, "prop": prop, "wall_s": round(time.time() - t0, 2)}

    with open(path, "a") as fh, cf.ThreadPoolExecutor(workers) as ex:
        for i, rec in enumerate(ex.map(one, todo)):
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            done[rec["qid"]] = rec
            if (i + 1) % 20 == 0:
                print(f"  {i + 1}/{len(todo)}  running exact={sum(r['exact'] for r in done.values()) / len(done):.3f}")
    return summarize_file(path)


def summarize_file(path) -> dict:
    rows = [json.loads(l) for l in open(path)]
    by = {}
    for r in rows:
        by[r["qid"]] = r                      # last write wins
    rows = list(by.values())
    n = len(rows)
    lat = [r.get("ms") or r.get("wall_s", 0) * 1000 for r in rows if not r.get("error")]
    bands = {}
    for b in ("2-3", "4", "5-6"):
        rs = [r for r in rows if band(r.get("hops") or 4) == b]
        if rs:
            bands[b] = round(sum(r["exact"] for r in rs) / len(rs), 4)
    absn = [r for r in rows if r.get("absence")]
    fams = {}
    for r in rows:
        fams.setdefault(r.get("family", "?"), []).append(r["exact"])
    return {"n": n, "accuracy": round(sum(r["exact"] for r in rows) / n, 4) if n else None,
            "proportional": round(sum(r["prop"] for r in rows) / n, 4) if n else None,
            "errors": sum(1 for r in rows if r.get("error")),
            "by_hops": bands, "absence_accuracy": round(sum(r["exact"] for r in absn) / len(absn), 4) if absn else None,
            "by_family": {k: round(sum(v) / len(v), 3) for k, v in fams.items()},
            "latency": {"p50": round(statistics.median(lat), 1) if lat else None,
                        "p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 1) if lat else None},
            "llm_calls": round(statistics.fmean([(r.get("usage") or {}).get("llm_calls", 0) for r in rows]), 2),
            "tool_calls": round(statistics.fmean([(r.get("usage") or {}).get("tool_calls", 0) for r in rows]), 2)}


def summarize_all() -> dict:
    systems: dict = {}
    for p in sorted(OUT.glob("*__*.jsonl")):
        sys_, set_ = p.stem.split("__")
        s = summarize_file(p)
        systems.setdefault(sys_, {})[set_] = s
        if set_ == "Q333":
            systems[sys_]["latency"] = s["latency"]
            systems[sys_]["llm_calls"] = s["llm_calls"]
            systems[sys_]["tool_calls"] = s["tool_calls"]
    out = {"summary": {"systems": systems, "model": "Qwen3-32B", "generated_at": time.strftime("%Y-%m-%d %H:%M")}}
    (config.DATA / "eval" / "qa_summary.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", default="bidbrain")
    ap.add_argument("--set", default="S21")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--summarize", action="store_true")
    a = ap.parse_args()
    if a.summarize:
        print(json.dumps(summarize_all()["summary"], indent=1)[:3000])
    else:
        print(json.dumps(run(a.system, a.set, a.workers, a.limit), indent=1))
