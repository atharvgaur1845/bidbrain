"""Closeout P0-2: no unsafe tender-qualification fallbacks.

An unparsed, partially parsed or unsupported clause must never reach a plain BID. Contract:
proven satisfied -> PASS, proven unsatisfied -> FAIL, unverified / ambiguous / unsupported -> NEEDS REVIEW;
DECLARE only for genuine self-declarations. Every test checks the final CASE verdict, through the rule
parser (prefer_llm=False) and, where it matters, through a stubbed LLM. Runs on a GRAPH.COPY of bidbrain_base.

    cd backend && .venv/bin/python -m pytest -q tests/test_qualify_closeout.py
"""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from bidbrain import config
from bidbrain.agents import clauses, orchestrator, providers
from bidbrain.agents.orchestrator import memo_problems, qualify_tender
from bidbrain.graph.client import Graph

BID_DATE = "2026-10-30"
OK = ("BID", "BID WITH FIXES")


@pytest.fixture(scope="module")
def g():
    g = Graph()
    assert g.exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    name = f"pytest_qualify_closeout_{os.getpid()}"
    if Graph(name).exists():
        Graph(name).delete()
    for attempt in range(20):            # GRAPH.COPY forks; it can fail while another copy is running
        try:
            s = g.copy_to(name)
            break
        except Exception:
            if attempt == 19:
                raise
            time.sleep(1.5)
    yield s
    s.delete()


@pytest.fixture
def no_memo_llm(monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: [])
    monkeypatch.setattr(providers, "REPLAY", False)


def _llm(monkeypatch, clauses_json: list[dict]):
    monkeypatch.setattr(providers, "chat",
                        lambda *a, **k: SimpleNamespace(content=json.dumps({"clauses": clauses_json})))


def _run(g, text: str, prefer_llm: bool = False, title: str = "closeout") -> dict:
    case = qualify_tender(g, {"title": title, "text": text, "bid_date": BID_DATE}, prefer_llm=prefer_llm)
    case["by_kind"] = {}
    for c in case["clauses"]:
        case["by_kind"].setdefault(c["kind"], []).append(c)
    return case


def _verdict(case) -> str:
    return case["case"]["verdict"]


def _no_declare_for(case, word: str):
    for c in case["clauses"]:
        if word.lower() in (c["text"] or "").lower():
            assert c["verdict"] != "DECLARE", c


def _memo_agrees(case):
    memo = case["case"]["memo"] or ""
    assert f"recommendation: **{_verdict(case)}**" in memo


def _review(case, word: str):
    assert _verdict(case) == "NEEDS REVIEW", (_verdict(case), [(c["ref"], c["kind"], c["verdict"]) for c in case["clauses"]])
    hit = [c for c in case["clauses"] if word.lower() in (c["text"] or "").lower()]
    assert hit and all(c["verdict"] == "NEEDS REVIEW" for c in hit), hit
    _memo_agrees(case)
    assert "Needs review" in case["case"]["memo"]


# ------------------------------------------------------------------ 1. cumulative value, no 'similar' word
CUM_1 = "Bidder shall have executed bridge works of cumulative value INR 500 Cr in the last 7 years."


@pytest.mark.parametrize("text", [
    CUM_1,
    "Bidder shall have executed bridge works of aggregate value of INR 500 Cr in the last 7 years.",
    "Bidder shall have completed bridge works of total value not less than INR 500 Cr in the last 7 years.",
])
def test_1_cumulative_without_similar_is_a_min_total_not_declare(g, no_memo_llm, text):
    r = clauses.parse_rules(text)
    assert [c["kind"] for c in r] == ["similar_works"] and r[0]["params"]["min_total"] == 5_000_000_000
    assert "min_value" not in r[0]["params"]
    case = _run(g, text)
    _no_declare_for(case, "bridge")
    assert _verdict(case) in ("BID", "BID WITH FIXES", "NO-BID")       # proven either way, never DECLARE -> BID
    k = case["by_kind"]["similar_works"][0]
    assert k["verdict"] in ("PASS", "FAIL", "AT_RISK")
    assert (_verdict(case) == "NO-BID") == (k["verdict"] == "FAIL")


def test_1_small_cumulative_passes_and_large_fails(g, no_memo_llm):
    lo = _run(g, CUM_1.replace("INR 500 Cr", "INR 50 Cr"))
    hi = _run(g, CUM_1.replace("INR 500 Cr", "INR 50000 Cr"))
    assert lo["by_kind"]["similar_works"][0]["verdict"] == "PASS" and _verdict(lo) in OK
    assert hi["by_kind"]["similar_works"][0]["verdict"] == "FAIL" and _verdict(hi) == "NO-BID"


def test_1_llm_files_cumulative_as_declaration(g, no_memo_llm, monkeypatch):
    _llm(monkeypatch, [{"id": "1", "kind": "other", "text": CUM_1, "params": {}}])
    case = _run(g, CUM_1, prefer_llm=True)
    _no_declare_for(case, "bridge")
    k = case["by_kind"]["similar_works"][0]
    assert json.loads(k["params"])["min_total"] == 5_000_000_000


# ------------------------------------------------------------------ 2. 'of cumulative INR 500 Cr' is not one work
CUM_2 = "Experience of similar works (bridges) of cumulative INR 500 Cr in the last 7 years."


def test_2_cumulative_amount_is_not_a_single_work(g, no_memo_llm):
    p = clauses.parse_rules(CUM_2)[0]["params"]
    assert p.get("min_total") == 5_000_000_000 and "min_value" not in p
    case = _run(g, CUM_2.replace("500", "50"))
    assert case["by_kind"]["similar_works"][0]["verdict"] == "PASS" and _verdict(case) in OK


def test_2_llm_reads_cumulative_as_one_work_and_is_corrected(g, no_memo_llm, monkeypatch):
    text = CUM_2.replace("500", "50")
    _llm(monkeypatch, [{"id": "1", "kind": "similar_works", "text": text,
                        "params": {"categories": ["bridges flyovers"], "min_value": 500_000_000, "count": 1}}])
    out = clauses.parse_llm(text)
    assert out[0]["params"].get("min_total") == 500_000_000 and "min_value" not in out[0]["params"]
    case = _run(g, text, prefer_llm=True)
    assert _verdict(case) in OK


def test_2_amount_check_flags_a_dropped_cumulative():
    chk = clauses.amount_check("similar_works", {"min_value": 5_000_000_000, "count": 1}, CUM_2)
    assert chk["status"] == "mismatch" and any("min_total" in p for p in chk["problems"])


# ------------------------------------------------------------------ 3. unsupported financial terms
@pytest.mark.parametrize("text,word", [
    ("Net worth shall be positive.", "net worth"),
    ("The bidder shall have a positive net worth in each of the last 3 years.", "net worth"),
    ("Bid capacity = 2.5·A·N − B must exceed estimated cost.", "bid capacity"),
    ("The available bid capacity ≥ estimated cost of INR 120 Cr.", "bid capacity"),
    ("Bidder shall have completed works worth at least twice the estimated cost (INR 40 Cr).", "twice"),
    ("Solvency certificate from a scheduled bank of not less than INR 20 Cr.", "solvency"),
])
def test_3_unsupported_terms_need_review(g, no_memo_llm, text, word):
    case = _run(g, text)
    _review(case, word)


@pytest.mark.parametrize("llm_kind,params", [
    ("other", {}),
    ("turnover", {"min_average": 1, "years": 3}),            # an invented threshold
    ("net_worth", {"min": 0}),                               # a kind the engine does not have
])
def test_3_llm_cannot_turn_net_worth_into_a_bid(g, no_memo_llm, monkeypatch, llm_kind, params):
    text = "Net worth shall be positive in each of the last 3 years."
    _llm(monkeypatch, [{"id": "1", "kind": llm_kind, "text": text, "params": params}])
    case = _run(g, text, prefer_llm=True)
    _review(case, "net worth")


def test_3_net_worth_next_to_a_passing_clause_blocks_bid(g, no_memo_llm):
    text = "1. Average annual turnover of the last 3 financial years not less than INR 10 Cr.\n2. Net worth shall be positive."
    case = _run(g, text)
    assert case["by_kind"]["turnover"][0]["verdict"] == "PASS"
    _review(case, "net worth")


# ------------------------------------------------------------------ 4. header lines are context
HEADER = ("Name of Work: Construction of a bridge over river X\nEstimated Cost: Rs. 120 Crore\n"
          "Average annual turnover of the last 3 financial years not less than 30% of the estimated cost.")


def test_4_header_line_is_not_a_clause_and_not_joined(g, no_memo_llm):
    r = clauses.parse_rules("Estimated Cost: Rs. 120 Crore")
    assert r == []
    r = clauses.parse_rules(HEADER)
    assert [c["kind"] for c in r] == ["turnover"]
    assert r[0]["params"]["min_average"] == 360_000_000         # 30% of the header's 120 Cr
    assert "Estimated Cost" not in r[0]["text"]
    case = _run(g, HEADER)
    assert len(case["clauses"]) == 1 and _verdict(case) in OK


def test_4_header_amount_only_with_a_plain_turnover_line(g, no_memo_llm):
    text = "Estimated Cost: Rs. 120 Crore\nAverage annual turnover of Rs 60 Cr in the last 3 years."
    r = clauses.parse_rules(text)
    assert [c["params"].get("min_average") for c in r] == [600_000_000]       # not 120 Cr
    case = _run(g, text)
    assert [c["kind"] for c in case["clauses"]] == ["turnover"] and _verdict(case) in OK


# ------------------------------------------------------------------ 5. ';' splits two requirements
MERGED = ("1. three similar works (bridge) each of value not less than 35% of estimated cost (Rs. 150 crore) "
          "during last 5 years; average annual turnover of Rs 60 Cr.")


def test_5_semicolon_splits_works_and_turnover(g, no_memo_llm):
    r = {c["kind"]: c["params"] for c in clauses.parse_rules(MERGED)}
    assert set(r) == {"similar_works", "turnover"}
    assert r["similar_works"]["min_value"] == 525_000_000 and r["similar_works"]["count"] == 3
    assert r["similar_works"]["lookback_years"] == 5
    assert r["turnover"] == {"min_average": 600_000_000, "years": 3}
    case = _run(g, MERGED)
    assert {c["kind"] for c in case["clauses"]} == {"similar_works", "turnover"}
    sw = case["by_kind"]["similar_works"][0]["verdict"]
    assert (_verdict(case) == "NO-BID") == (sw == "FAIL")


def test_5_llm_merged_clause_is_split(g, no_memo_llm, monkeypatch):
    _llm(monkeypatch, [{"id": "1", "kind": "turnover", "text": MERGED,
                        "params": {"min_each_year": 525_000_000, "years": 5}}])
    out = clauses.parse_llm(MERGED)
    assert {c["kind"] for c in out} == {"similar_works", "turnover"} and all("merged" in c["corrected"] for c in out)
    case = _run(g, MERGED, prefer_llm=True)
    assert {c["kind"] for c in case["clauses"]} >= {"similar_works", "turnover"}


def test_5_llm_dropping_the_works_half_is_caught_by_the_verifier(g, no_memo_llm, monkeypatch):
    # the LLM emits only the turnover half, with only its text: the similar-works amounts are in no clause
    _llm(monkeypatch, [{"id": "1", "kind": "turnover", "text": "average annual turnover of Rs 60 Cr.",
                        "params": {"min_average": 600_000_000, "years": 3}}])
    case = _run(g, MERGED, prefer_llm=True)
    assert _verdict(case) == "NEEDS REVIEW"
    v = [c for c in case["clauses"] if c["ref"].startswith("V")]
    assert v and v[0]["verdict"] == "NEEDS REVIEW" and "35%" in v[0]["text"]
    _memo_agrees(case)


def test_5_either_or_with_a_percentage_and_a_header_estimate(g, no_memo_llm):
    text = ("Estimated Cost: Rs. 100 Crore\nSimilar works: either one bridge work of 40% of the estimated cost or "
            "two bridge works each of INR 30 Cr, in the last 7 years.")
    r = clauses.parse_rules(text)
    assert r[0]["params"]["options"] == [{"count": 1, "min_value": 400_000_000}, {"count": 2, "min_value": 300_000_000}]
    case = _run(g, text)
    assert case["by_kind"]["similar_works"][0]["verdict"] in ("PASS", "FAIL", "AT_RISK")
    assert _verdict(case) != "NEEDS REVIEW"


def test_5_either_or_percentage_with_no_estimate_needs_review(g, no_memo_llm):
    text = ("Similar works: either one bridge work of 40% of the estimated cost or two bridge works each of "
            "INR 30 Cr, in the last 7 years.")
    case = _run(g, text)
    _review(case, "either")


# ------------------------------------------------------------------ 6. no reference letter -> conditional BID
def test_6_credited_work_without_letter_is_bid_with_fixes_with_actions(g, no_memo_llm):
    t = {x["id"]: x for x in json.load(open(config.DATA / "tenders" / "demo_tenders.json"))}["T-RBC-2026-31"]
    text = t["text"].split("\n")[2]                    # clause 2: flyover works, no letter asked for
    case = _run(g, text)
    k = case["by_kind"]["similar_works"][0]
    assert k["verdict"] == "PASS"
    assert any(f and f["kind"] == "absence" and f["severity"] == "high" for f in k["findings"])
    assert _verdict(case) == "BID WITH FIXES"
    assert any(a["kind"] == "request_reference_letter" for a in case["actions"])
    assert "Open actions" in case["case"]["memo"] and "reference letter" in case["case"]["memo"]
    _memo_agrees(case)


# ------------------------------------------------------------------ LLM omits quantities / invents values
def test_llm_omits_the_threshold(g, no_memo_llm, monkeypatch):
    text = "Average annual turnover of the last 3 financial years not less than INR 10 Cr."
    _llm(monkeypatch, [{"id": "1", "kind": "turnover", "text": text, "params": {"years": 3}}])
    case = _run(g, text, prefer_llm=True)
    assert json.loads(case["by_kind"]["turnover"][0]["params"])["min_average"] == 100_000_000


@pytest.mark.parametrize("kind,params", [("certification", {}), ("years_in_business", {}),
                                         ("years_in_business", {"min_years": 0}), ("key_personnel", {"count": 1})])
def test_llm_empty_params_never_pass_vacuously(g, no_memo_llm, monkeypatch, kind, params):
    text = "The bidder shall meet the requirement stated in the annexure."
    _llm(monkeypatch, [{"id": "1", "kind": kind, "text": text, "params": params}])
    case = _run(g, text, prefer_llm=True)
    assert _verdict(case) == "NEEDS REVIEW"


def test_llm_invented_amount_is_not_verified(g, no_memo_llm, monkeypatch):
    text = "Average annual turnover of the last 3 financial years not less than INR 10 Cr."
    _llm(monkeypatch, [{"id": "1", "kind": "turnover", "text": text, "params": {"min_average": 1, "years": 3}}])
    case = _run(g, text, prefer_llm=True)
    # corrected to the stated amount (one amount in the text), never the invented ₹1
    assert json.loads(case["by_kind"]["turnover"][0]["params"])["min_average"] == 100_000_000


def test_llm_amount_on_a_declaration_is_reviewed(g, no_memo_llm, monkeypatch):
    text = "The bidder shall have liquid assets of at least INR 15 Cr (declaration)."
    _llm(monkeypatch, [{"id": "1", "kind": "other", "text": text, "params": {}}])
    _review(_run(g, text, prefer_llm=True), "liquid")


# ------------------------------------------------------------------ DECLARE stays for genuine declarations
def test_genuine_declarations_and_deposits_stay_declare(g, no_memo_llm):
    text = ("1. The bidder shall not have been blacklisted by any Central/State agency (declaration).\n"
            "2. Bid security of INR 1 Cr in the form of a bank guarantee, with a signed integrity pact.\n"
            "3. An affidavit that all information furnished is true.")
    case = _run(g, text)
    assert [c["verdict"] for c in case["clauses"]] == ["DECLARE"] * 3
    assert _verdict(case) == "BID"


def test_verifier_reopens_a_preparsed_declaration_with_an_amount(g, no_memo_llm):
    text = "1. Minimum liquid assets / credit facilities of INR 25 Cr."
    t = {"title": "pre", "text": text, "bid_date": BID_DATE, "clauses": [{"id": "1", "text": text, "kind": "other",
                                                                          "params": {}}]}
    case = qualify_tender(g, t, prefer_llm=False)
    assert case["case"]["verdict"] == "NEEDS REVIEW" and case["clauses"][0]["verdict"] == "NEEDS REVIEW"


# ------------------------------------------------------------------ the memo must state the case's verdict
def test_llm_memo_with_another_recommendation_is_rejected():
    probs = memo_problems("## Recommendation: **BID**\nAll clauses pass.", "{}", overall="NEEDS REVIEW")
    assert any("recommends BID" in p for p in probs)
    assert not [p for p in memo_problems("Recommendation: NEEDS REVIEW. A person must read 2.", "{}",
                                         overall="NEEDS REVIEW") if "recommends" in p]


# ------------------------------------------------------------------ the demo verdicts do not move
@pytest.mark.parametrize("tid,want", [("T-NHX-2026-014", "BID"), ("T-PHED-WTP-2027-03", "BID WITH FIXES"),
                                      ("T-JN-LB-2026-09", "NO-BID"), ("T-CWBB-2026-21", "NO-BID"),
                                      ("T-RBC-2026-31", "BID WITH FIXES")])
def test_demo_verdicts(g, no_memo_llm, tid, want):
    t = {x["id"]: x for x in json.load(open(config.DATA / "tenders" / "demo_tenders.json"))}[tid]
    assert qualify_tender(g, t, prefer_llm=False)["case"]["verdict"] == want
    text_only = {k: v for k, v in t.items() if k != "clauses"}
    assert qualify_tender(g, text_only, prefer_llm=False)["case"]["verdict"] == want
