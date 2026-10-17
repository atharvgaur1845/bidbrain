"""Q&A benchmark runner.

    LLM_PROVIDERS=vllm python -m eval.run --system bidbrain --set S21 [--workers 12] [--limit N]
    python -m eval.run --summarize          # merge every results file into data/eval/qa_summary.json

Systems (all on the same model and the same FalkorDB instance):
    bidbrain      single agent over BidBrain's graph tools
    team          the six-agent qualification team (orchestrator.qualify_tender) on a graph copy, TND only,
                  with prefer_llm=False: clauses come from the rule parser reading the clause TEXT (the
                  pre-parsed path is never used), the memo is the template. No LLM is called, so this
                  measures the deterministic pipeline end to end (parse -> qualify -> audit -> verify),
                  not the model; gold is independent Cypher in eval/synth.py
    vector_rag    top-k chunks from FalkorDB's vector index -> LLM
    text2cypher   LLM writes Cypher from the schema -> rows -> LLM
    graphrag_sdk  FalkorDB GraphRAG-SDK, auto-extracted schema (see eval/baselines/graphrag_sdk.py)
Sets:
    S21   21 sample questions, true gold
    Q333  333 hidden questions, near-gold (the deterministic engine, 99.775 on the leaderboard)
    ABS   synthesized absence questions, gold from Cypher            (eval/synth.py)
    TND   synthesized tender-clause questions, gold from Cypher/Python (eval/synth.py)

Scores (one scorer for every system): exact for counts/days; percentages within 0.011, or the rounded
gold when the question asks for a whole number; money within max(1 rupee, min(0.5%, Rs 50,000)) so an
answer printed to 0.01 Cr passes but a sum that leaves out a whole work does not; verdicts are mapped
to QUALIFIES / DOES NOT QUALIFY before comparing. Plus the dataset's official proportional credit,
max(0, 1 - |got-gold|/|gold|). The summary re-parses and re-scores every stored answer, so it always
reflects the current scorer.

    python -m eval.run --selfcheck          # scorer/parser self-test over synthetic answer strings
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import pathlib
import re
import statistics
import time

from bidbrain import config
from bidbrain.graph.client import Graph

OUT = config.DATA / "eval" / "results"
SDK_INGEST = OUT / "graphrag_sdk_ingest.json"     # written by eval/baselines/graphrag_sdk.py ingest
SDK_MIN_COVERAGE = 0.90
# The vendored engine's absence test (vendor_bidintel/src/answer.py ABSENT, also used by eval/oracle.py);
# copied so the SDK venv need not import the engine. --selfcheck asserts it still matches.
ABSENT = (r"\bno\b(?!\s+(?:more|less|greater|fewer|higher|lower|larger|smaller)\b)|"
          r"\bnone\b|without|lack|missing|absent|unreferenced|hold no|don'?t have|do not have")
WHOLE_NUMBER = re.compile(r"\bwhole number\b|\bnearest (?:whole|integer)\b|\bas an integer\b", re.I)
MONEY_ABS_TOL = 50_000      # 0.005 Cr: the rounding of an answer printed in crore to 2 decimals


def is_absence(trace: dict, question: str) -> bool:
    """A question about something MISSING. referenced_share asks for the share of works that HAVE a
    reference unless its wording says otherwise, the same test eval/oracle.py uses."""
    op = trace.get("op")
    return op == "absence_count" or (op == "referenced_share" and bool(re.search(ABSENT, question.lower())))


def _check_unique(qs: list[dict], name: str) -> list[dict]:
    dup = [k for k, v in collections.Counter(q["qid"] for q in qs).items() if v > 1]
    if dup:
        raise ValueError(f"set {name}: {len(dup)} duplicate qids (results would overwrite each other): {dup[:5]}")
    return qs


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
                        "absence": is_absence(r["trace"], r["question"])})
        return _check_unique(out, name)
    path = config.DATA / "eval" / f"{name.lower()}_set.json"
    return _check_unique(json.load(open(path)), name)


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


_WORDNUM = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                       "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
_WORDNUM.update({"none": 0, "no": 0, "nil": 0, "nothing": 0})


def _norm_text(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().strip(" \"'`\u201c\u201d\u2018\u2019").rstrip(".,:;!").strip().lower()


def score(got, gold, answer_type=None, question: str | None = None) -> tuple[bool, float]:
    """(exact, proportional). The same rules for every system; see the module docstring."""
    from bidbrain.agents.qa import normalize_verdict
    if answer_type == "verdict" or (isinstance(gold, str) and normalize_verdict(gold) == gold):
        ok = got is not None and normalize_verdict(got) == normalize_verdict(gold)
        return ok, 1.0 if ok else 0.0
    if isinstance(gold, str):
        ok = got is not None and _norm_text(got) == _norm_text(gold)
        return ok, 1.0 if ok else 0.0
    if isinstance(got, str) and answer_type in ("count", "days"):
        m = re.match(r"([a-z]+)\b", _norm_text(got))
        got = _WORDNUM.get(m.group(1)) if m else None
    if isinstance(got, bool):
        return False, 0.0
    try:
        got, gold = float(got), float(gold)
    except (TypeError, ValueError):
        return False, 0.0
    prop = (1.0 if got == gold else 0.0) if gold == 0 else max(0.0, 1 - abs(got - gold) / abs(gold))
    if answer_type in ("count", "days"):
        exact = round(got) == round(gold)
    elif answer_type == "percent":
        exact = abs(got - gold) <= 0.011
        if not exact and question and WHOLE_NUMBER.search(question):
            exact = abs(got - int(gold + 0.5 if gold >= 0 else gold - 0.5)) <= 0.011   # half-up, as asked
    else:
        exact = abs(got - gold) <= max(1.0, min(0.005 * abs(gold), MONEY_ABS_TOL))
    return exact, prop


def rescore(r: dict) -> tuple[bool, float, object]:
    """Re-parse the stored ANSWER text with the current shared parser and score it."""
    from bidbrain.agents.qa import parse_answer
    got = r.get("answer")
    if r.get("raw") not in (None, ""):
        got = parse_answer(f"ANSWER: {r['raw']}")[0]
    ok, prop = score(got, r["gold"], r.get("answer_type"), r.get("question"))
    return ok, prop, got


def _provenance(calls: list[str] | None) -> dict:
    """Per-row model provenance from the per-call provider list ('vllm:Qwen/Qwen3-32B (cached)', ...)."""
    calls = list(calls or [])
    hits = sum(1 for c in calls if c.endswith("(cached)"))
    names = [c.replace("(cached)", "").strip() for c in calls]
    return {"providers": list(dict.fromkeys(names)), "llm_cache_hits": hits, "cached": hits > 0}


TEAM_GRAPH = "eval_team_tmp"
_CLAUSE_Q = re.compile(r"Tender clause \(bid due (\d{4}-\d{2}-\d{2})\): '(.+)' Do we qualify", re.S)
_PM_Q = re.compile(r"bid due (\d{4}-\d{2}-\d{2}) that requires a PMP valid on that date, at least (\d+) years' "
                   r"experience, and having led at least one (.+?) project\?")


def team_tender(q: dict) -> dict:
    """The TND question as a tender: the quoted clause text verbatim, or the Project Manager question
    restated as the key-personnel clause a tender would print."""
    m = _CLAUSE_Q.search(q["question"])
    if m:
        return {"id": q["qid"], "title": q["qid"], "bid_date": m[1], "text": m[2]}
    m = _PM_Q.search(q["question"])
    if m:
        return {"id": q["qid"], "title": q["qid"], "bid_date": m[1],
                "text": f"Project Manager with a PMP valid on the bid date, at least {m[2]} years' experience, "
                        f"having led at least one {m[3]} project."}
    raise ValueError("not a tender question")


def team_answer(g: Graph, q: dict) -> dict:
    from bidbrain.agents.orchestrator import qualify_tender
    t0 = time.perf_counter()
    case = qualify_tender(g, team_tender(q), prefer_llm=False)
    # the answer is read back from the case subgraph the agents wrote, not from Python return values
    cls = g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause) WHERE k.kind <> 'other'
                    RETURN k.kind AS kind, k.verdict AS verdict, k.basis AS basis""", {"c": case["case"]["id"]})
    verified = g.read("MATCH (c:Case {id:$c}) RETURN c.verified AS v", {"c": case["case"]["id"]})[0]["v"]
    if len(cls) != 1:
        raise ValueError(f"parser produced {len(cls)} provable clauses")
    k = cls[0]
    if q.get("answer_type") == "count":
        m = re.match(r"(\d+) people", k["basis"] or "")
        ans = int(m[1]) if m else None
    else:
        ans = "QUALIFIES" if k["verdict"] in ("PASS", "AT_RISK") else "DOES NOT QUALIFY"
    u = case["usage"]
    return {"answer": ans, "raw": str(ans) if ans is not None else "",
            "basis": f"{k['kind']} {k['verdict']}: {k['basis']} (case verified={verified})",
            "usage": {"llm_calls": u["llm_calls"], "tool_calls": u["tool_calls"], "prompt": u["prompt_tokens"],
                      "completion": u["completion_tokens"]},
            "ms": round((time.perf_counter() - t0) * 1000, 1), "error": None,
            "agents": [{k2: a[k2] for k2 in ("agent", "ms", "llm_calls", "tool_calls")} for a in case["agents"]],
            "verified": verified, "providers": []}


def answer(system: str, g: Graph, q: dict) -> dict:
    from bidbrain.agents import baselines, qa
    if system == "team":
        return team_answer(g, q)
    if system == "bidbrain":
        r = qa.ask(g, q["question"])
        return {"answer": r["answer"], "raw": r["answer_text"], "usage": r["usage"], "ms": r["ms"],
                "error": r["error"], "steps": [s["tool"] for s in r["steps"]], **_provenance(r.get("providers"))}
    if system == "vector_rag":
        r = baselines.vector_rag(g, q["question"])
        return {"answer": r["answer"], "raw": r["answer_text"], "usage": r["usage"], "ms": r["ms"],
                **_provenance(r.get("providers"))}
    if system == "text2cypher":
        r = baselines.text2cypher(g, q["question"])
        return {"answer": r["answer"], "raw": r["answer_text"], "usage": r["usage"], "ms": r["ms"],
                "cypher": r["cypher"], "error": r["error"], **_provenance(r.get("providers"))}
    if system == "graphrag_sdk":
        from eval.baselines.graphrag_sdk import ask_sdk
        r = ask_sdk(q["question"])
        return {**r, **_provenance(r.pop("providers", None))}
    raise ValueError(system)


def sdk_ingest_status() -> dict | None:
    """The GraphRAG-SDK ingest record (eval/baselines/graphrag_sdk.py ingest), or None if never finished."""
    try:
        return json.loads(SDK_INGEST.read_text())
    except (OSError, ValueError):
        return None


def check_sdk_ingest(allow_partial: bool = False) -> dict | None:
    st = sdk_ingest_status()
    cov = (st or {}).get("coverage") or 0.0
    if st and st.get("finalized") and cov >= SDK_MIN_COVERAGE:
        return st
    why = f"no ingest record at {SDK_INGEST}" if not st else \
          "the ingest record shows the ingest never finished (finalized=false)" if not st.get("finalized") else \
          f"ingest coverage {cov:.1%} ({st.get('ok')}/{st.get('n_docs')} docs) is below {SDK_MIN_COVERAGE:.0%}"
    if not allow_partial:
        raise SystemExit(f"graphrag_sdk: refusing to score a partial graph: {why}. Re-run the ingest "
                         f"(drop graph grsdk_bidbrain first) or pass --allow-partial.")
    print(f"WARNING graphrag_sdk: {why}; scoring anyway (--allow-partial)")
    return st


def run(system: str, set_name: str, workers: int = 8, limit: int | None = None,
        allow_partial: bool = False) -> dict:
    if system == "graphrag_sdk":
        check_sdk_ingest(allow_partial)
    qs = load_set(set_name)[: limit or None]
    g = Graph()            # read-only questions: the base graph is safe to share
    if system == "team":
        # the team WRITES (cases, clauses, agent runs): work in a throwaway copy, one writer at a time
        if set_name != "TND":
            raise SystemExit("team answers tender clauses: TND only")
        from bidbrain.graph.client import db
        if TEAM_GRAPH in db().list_graphs():
            Graph(TEAM_GRAPH).delete()
        Graph(config.BASE_GRAPH).copy_to(TEAM_GRAPH)
        g, workers = Graph(TEAM_GRAPH), 1
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{system}__{set_name}.jsonl"
    want = {q["qid"]: q for q in qs}
    done = {}
    if path.exists():
        for l in open(path):
            r = json.loads(l)
            q = want.get(r["qid"])
            # a cached row counts only if it answered the same question (guards against reused qids)
            if not r.get("error") and q is not None and r.get("question") == q["question"]:
                done[r["qid"]] = r
    todo = [q for q in qs if q["qid"] not in done]
    print(f"{system} on {set_name}: {len(done)} cached, {len(todo)} to run")

    def one(q):
        t0 = time.perf_counter()
        try:
            r = answer(system, g, q)
        except Exception as exc:
            r = {"answer": None, "error": f"{type(exc).__name__}: {exc}"}
        ok, prop = score(r.get("answer"), q["gold"], q.get("answer_type"), q.get("question"))
        wall = time.perf_counter() - t0
        return {**q, **r, "exact": ok, "prop": prop, "wall_ms": round(wall * 1000, 1), "wall_s": round(wall, 2)}

    with open(path, "a") as fh, cf.ThreadPoolExecutor(workers) as ex:
        for i, rec in enumerate(ex.map(one, todo)):
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            done[rec["qid"]] = rec
            if (i + 1) % 20 == 0:
                print(f"  {i + 1}/{len(todo)}  running exact={sum(r['exact'] for r in done.values()) / len(done):.3f}")
    if system == "team":
        g.delete()
    return summarize_file(path)


def _pcts(xs: list[float]) -> dict:
    xs = sorted(xs)
    return {"p50": round(statistics.median(xs), 1) if xs else None,
            "p95": round(xs[int(0.95 * (len(xs) - 1))], 1) if xs else None, "n": len(xs)}


def _wall_ms(r: dict) -> float | None:
    if r.get("wall_ms") is not None:
        return float(r["wall_ms"])
    return float(r["wall_s"]) * 1000 if r.get("wall_s") is not None else None


def _was_cached(r: dict) -> bool:
    if "cached" in r:
        return bool(r["cached"])
    return r.get("ms") == 0          # rows written before the cache flag existed


def wilson(k: int, n: int, z: float = 1.96) -> list | None:
    """95% Wilson interval for k/n: single runs have no error bars, this is the sampling part of one."""
    if not n:
        return None
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(c - h, 3), round(c + h, 3)]


def summarize_file(path, set_name: str | None = None) -> dict:
    """Score one results file against the CURRENT set definition: rows are joined to the set by qid
    and question (stale or foreign rows are dropped), re-parsed and re-scored with the current scorer,
    and take hops/family/absence from the set, not from the row."""
    set_name = set_name or pathlib.Path(path).stem.split("__")[-1]
    try:
        qs = {q["qid"]: q for q in load_set(set_name)}
    except (OSError, ValueError, KeyError):
        qs = None
    by = {}
    for l in open(path):
        r = json.loads(l)
        if qs is not None:
            q = qs.get(r["qid"])
            if q is None or q["question"] != r.get("question"):
                continue
            r = {**r, **q}
        by[r["qid"]] = r                      # last write wins
    rows = list(by.values())
    for r in rows:
        r["exact"], r["prop"], r["answer"] = rescore(r)
    n = len(rows)
    acc = lambda rs: round(sum(r["exact"] for r in rs) / len(rs), 4) if rs else None  # noqa: E731
    errs = [r for r in rows if r.get("error")]
    fresh = [r for r in rows if not _was_cached(r) and _wall_ms(r) is not None]
    bands = {}
    for b in ("2-3", "4", "5-6"):
        rs = [r for r in rows if band(r.get("hops") or 4) == b]
        if rs:
            bands[b] = acc(rs)
    absn = [r for r in rows if r.get("absence")]
    fams = {}
    for r in rows:
        fams.setdefault(r.get("family", "?"), []).append(r["exact"])
    calls = [(r.get("usage") or {}).get("llm_calls") for r in rows]
    calls = [c for c in calls if c is not None]
    tools = [(r.get("usage") or {}).get("tool_calls") for r in rows]
    tools = [c for c in tools if c is not None]
    models = collections.Counter(p for r in rows for p in (r.get("providers") or []))
    pres = [r for r in rows if not r.get("absence")]
    # tokens only where the system reported both sides (graphrag_sdk does not expose them)
    toks = [(r.get("usage") or {}) for r in rows]
    toks = [u["prompt"] + u["completion"] for u in toks if u.get("prompt") is not None and u.get("completion") is not None]
    return {"n": n, "n_set": len(qs) if qs is not None else None,
            "accuracy": acc(rows), "accuracy_ci95": wilson(sum(r["exact"] for r in rows), n),
            "presence_accuracy": acc(pres), "presence_n": len(pres),
            "tokens_per_q": round(statistics.fmean(toks)) if toks else None, "tokens_n": len(toks),
            "proportional": round(sum(r["prop"] for r in rows) / n, 4) if n else None,
            "errors": len(errs),
            "by_hops": bands, "absence_accuracy": acc(absn), "absence_n": len(absn),
            "by_family": {k: round(sum(v) / len(v), 3) for k, v in fams.items()},
            # wall time per question for every system, measured the same way (eval.run's own clock);
            # cache replays and errored runs are left out, errored runs are reported on their own
            "latency": _pcts([_wall_ms(r) for r in fresh if not r.get("error")]),
            "latency_errored": _pcts([_wall_ms(r) for r in fresh if r.get("error")]),
            "cached_rows": sum(1 for r in rows if _was_cached(r)),
            "llm_calls": round(statistics.fmean(calls), 2) if calls else None,
            "tool_calls": round(statistics.fmean(tools), 2) if tools else None,
            "models": dict(models)}


def constant_baselines(set_name: str) -> dict:
    """Accuracy of answering a constant: 0 everywhere, and the most common gold per answer type."""
    qs = load_set(set_name)
    maj = {}
    for t in {q.get("answer_type") for q in qs}:
        golds = [json.dumps(q["gold"]) for q in qs if q.get("answer_type") == t]
        maj[t] = json.loads(collections.Counter(golds).most_common(1)[0][0])
    zero = [score(0, q["gold"], q.get("answer_type"), q["question"])[0] for q in qs]
    mj = [score(maj[q.get("answer_type")], q["gold"], q.get("answer_type"), q["question"])[0] for q in qs]
    return {"always_zero": round(sum(zero) / len(qs), 4), "majority_per_type": round(sum(mj) / len(qs), 4),
            "majority_answers": {str(k): v for k, v in maj.items()}}


def summarize_all() -> dict:
    systems: dict = {}
    models: dict = {}
    sets: set = set()
    for p in sorted(OUT.glob("*__*.jsonl")):
        sys_, set_ = p.stem.split("__")
        s = summarize_file(p, set_)
        sets.add(set_)
        systems.setdefault(sys_, {})[set_] = s
        for m, c in s["models"].items():
            models.setdefault(sys_, collections.Counter())[m] += c
        if set_ == "Q333":
            systems[sys_]["latency"] = s["latency"]
            systems[sys_]["llm_calls"] = s["llm_calls"]
            systems[sys_]["tool_calls"] = s["tool_calls"]
            systems[sys_]["tokens_per_q"] = s["tokens_per_q"]
    every = collections.Counter()
    for c in models.values():
        every.update(c)
    names = sorted({m.split(":", 1)[-1] for m in every})
    model = names[0] if len(names) == 1 else ("mixed: " + ", ".join(names) if names else None)
    consts = {}
    for st in sorted(sets):
        try:
            consts[st] = constant_baselines(st)
        except (OSError, ValueError, KeyError):
            pass
    sdk = sdk_ingest_status()
    if sdk and "graphrag_sdk" in systems:
        systems["graphrag_sdk"]["ingest_coverage"] = sdk.get("coverage") if sdk.get("finalized") else 0.0
    # a repeat run of the same code (fresh LLM cache) for run-to-run variance: accuracy per run and the mean
    repeat: dict = {}
    for p in sorted((OUT / "run2_final").glob("*__*.jsonl")):
        sys_, set_ = p.stem.split("__")
        r2 = summarize_file(p, set_)
        r1 = systems.get(sys_, {}).get(set_)
        repeat.setdefault(sys_, {})[set_] = {
            "run2_accuracy": r2["accuracy"], "run2_ci95": r2["accuracy_ci95"], "run2_n": r2["n"],
            "mean_of_runs": round((r1["accuracy"] + r2["accuracy"]) / 2, 4) if r1 else None}
    out = {"summary": {"systems": systems, "model": model, "repeat_run": repeat,
                       "models_by_system": {k: dict(v) for k, v in models.items()},
                       "constant_baselines": consts,
                       "graphrag_sdk_ingest": None if sdk is None else
                       {k: sdk.get(k) for k in ("n_docs", "ok", "failed_n", "coverage", "finalized", "finished_at")},
                       "generated_at": time.strftime("%Y-%m-%d %H:%M")}}
    (config.DATA / "eval" / "qa_summary.json").write_text(json.dumps(out, indent=1))
    return out


def selfcheck() -> None:
    """Parser + scorer over synthetic answer strings, one block per answer_type. Every system's raw
    ANSWER line goes through exactly this path, so a failure here is a fairness bug."""
    from bidbrain.agents.qa import parse_answer
    whole = "what whole number out of one hundred represents the share?"
    cases = [  # (answer text, gold, answer_type, question, expected exact)
        ("ANSWER: 2942400000", 2942400000, "money", "", True),
        ("ANSWER: 2,942,400,000 rupees (\u20b9294.24 Cr)", 2942400000, "money", "", True),
        ("ANSWER: \u20b9294.24 Cr", 2942400000, "money", "", True),
        ("ANSWER: INR 5,00,00,000/-", 50000000, "money", "", True),
        ("ANSWER: 1.5 crore 20 lakh", 17000000, "money", "", True),
        ("ANSWER: 1,000 crore rupees", 10**10, "money", "", True),
        ("ANSWER: 8,547,800,000", 8563200000, "money", "", False),     # one 1.54 Cr work left out of 856.32 Cr
        ("ANSWER: 856.32 Cr", 8563212345, "money", "", True),            # printed to 0.01 Cr
        ("ANSWER: 33.33", 33.33, "percent", whole, True),
        ("ANSWER: 33", 33.33, "percent", whole, True),                   # the question asks for a whole number
        ("ANSWER: 33", 33.33, "percent", "what share out of 100?", False),
        ("ANSWER: 89", 88.89, "percent", whole, True),
        ("ANSWER: 45.50 percent", 45.5, "percent", "", True),
        ("ANSWER: 87.35%.", 87.35, "percent", "", True),
        ("ANSWER: 0.8735", 87.35, "percent", "", False),
        ("ANSWER: 3 works", 3, "count", "", True),
        ("ANSWER: 2 people", 2, "count", "", True),
        ("ANSWER: 12 works", 12, "count", "", True),
        ("ANSWER: 7 clients", 7, "count", "", True),
        ("ANSWER: approximately 12", 12, "count", "", True),
        ("ANSWER: None", 0, "count", "", True),
        ("ANSWER: zero works", 0, "count", "", True),
        ("ANSWER: 4", 3, "count", "", False),
        ("ANSWER: 1,234 days", 1234, "days", "", True),
        ("ANSWER: 1234 days from 2020-01-01", 1234, "days", "", True),
        ("ANSWER: QUALIFIES", "QUALIFIES", "verdict", "", True),
        ("ANSWER: QUALIFIES.", "QUALIFIES", "verdict", "", True),
        ('ANSWER: "QUALIFIES"', "QUALIFIES", "verdict", "", True),
        ("ANSWER: QUALIFIES - two works", "QUALIFIES", "verdict", "", True),
        ("ANSWER: DOES NOT QUALIFY (avg 253.5 Cr < 260 Cr)", "DOES NOT QUALIFY", "verdict", "", True),
        ("ANSWER: DOES NOT QUALIFY", "QUALIFIES", "verdict", "", False),
        ("ANSWER: QUALIFIES", "DOES NOT QUALIFY", "verdict", "", False),
        ("ANSWER: No, we do not qualify.", "DOES NOT QUALIFY", "verdict", "", True),
        ("ANSWER: Not qualified", "QUALIFIES", "verdict", "", False),
        ("ANSWER: Yes", "QUALIFIES", "verdict", "", True),
        ("ANSWER: Road Widening - Maharashtra Pkg-21.", "Road Widening - Maharashtra Pkg-21", None, "", True),
        ("ANSWER: 'Road Widening - Maharashtra Pkg-21'", "Road Widening - Maharashtra Pkg-21", None, "", True),
    ]
    bad = []
    for text, gold, at, q, want in cases:
        got = parse_answer(text)[0]
        ok = score(got, gold, at, q)[0]
        if ok != want:
            bad.append((text, gold, at, got, ok))
    # values that must stay text: a title is never cut at its dash or read as a number
    for text, want in (("ANSWER: Road Widening - Maharashtra Pkg-21", "Road Widening - Maharashtra Pkg-21"),
                       ("ANSWER: 12th Finance Commission Road", "12th Finance Commission Road"),
                       ("ANSWER: 2024-25", "2024-25")):
        if parse_answer(text)[0] != want:
            bad.append((text, want, "text", parse_answer(text)[0], False))
    try:
        import sys
        sys.path.insert(0, str(config.VENDOR / "src"))
        import answer as OLD  # noqa: E402
        assert OLD.ABSENT == ABSENT, "ABSENT drifted from the vendored engine's regex"
    except ImportError:
        print("  (vendored engine not importable here; ABSENT equality not checked)")
    sizes = {}
    for st in ("S21", "Q333", "ABS", "TND"):
        qs = load_set(st)              # raises on duplicate qids
        sizes[st] = (len(qs), sum(1 for q in qs if q.get("absence")))
    for text, gold, at, got, ok in bad:
        print(f"  FAIL {text!r} gold={gold!r} type={at} parsed={got!r} exact={ok}")
    print(f"selfcheck: {len(cases) + 3 - len(bad)}/{len(cases) + 3} ok; sets (n, absence): {sizes}")
    if bad:
        raise SystemExit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", default="bidbrain")
    ap.add_argument("--set", default="S21")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--summarize", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--allow-partial", action="store_true", help="graphrag_sdk: score even if its ingest was partial")
    a = ap.parse_args()
    if a.selfcheck:
        selfcheck()
    elif a.summarize:
        print(json.dumps(summarize_all()["summary"], indent=1)[:3000])
    else:
        print(json.dumps(run(a.system, a.set, a.workers, a.limit, a.allow_partial), indent=1))
