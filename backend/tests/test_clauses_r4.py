"""Round-4 clause parsing and Verifier (judging round 3, track C):

* "at least one road work ... not less than 40% of the estimated cost of INR 80 Cr" is ONE work of 32 Cr, not
  the CPWD 80/60/40 rule on an 80 Cr estimate (Pkg-7 at 37.52 Cr passes);
* "one work >= 30 Cr and a cumulative value of INR 500 Cr" keeps the cumulative part (min_total) and fails
  (road works total 131.82 Cr);
* "average turnover ... at least 30% of the estimated cost" with the cost only in the tender header is resolved
  (30% of 2000 Cr), and an unparsed requirement of a known kind is NEEDS REVIEW, never DECLARE / BID;
* the Verifier's amount reading is independent of the parser (a parser that drops the cumulative value is
  caught), never claims verified=true on a clause it could not check, and catches the LLM's 10x errors;
* memo_check ties every clause reference and ₹ figure to that clause's own record.

Both the rule path and an LLM path (providers.chat stubbed with the judges' bad JSON) are covered. Mutating
tests run on a GRAPH.COPY of bidbrain_base.

    cd backend && pytest -q tests/test_clauses_r4.py
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bidbrain.agents import clauses, orchestrator, providers
from bidbrain.agents.orchestrator import memo_problems, qualify_tender
from bidbrain.graph.client import Graph
from bidbrain.tools.proof import qualify_clause

BID_DATE = "2026-06-30"
PKG7 = "rigidpavementmadhyapradeshpkg7"
CR = 10_000_000

ROAD_40PCT = ("1. Similar works: the bidder shall have completed at least one road work during the last 7 years "
              "of value not less than 40% of the estimated cost of INR 80 Cr.")
ROAD_CUMULATIVE = ("2. Similar works: one road work of value at least INR 30 Cr completed in the last 7 years, and "
                   "road works of a cumulative value of INR 500 Cr in the same period.")
TURNOVER_PCT = ("3. Average annual turnover of the last 3 financial years shall be at least 30% of the estimated "
                "cost.")
HEADER = "Estimated cost Rs 2000 Cr\nEligibility Criteria\n"
TENDER_TEXT = HEADER + "\n".join([ROAD_40PCT, ROAD_CUMULATIVE, TURNOVER_PCT])

# what the judges' LLM returned: 40% of 80 Cr read as 3.2 Cr, "INR 30 Cr" read as 3 Cr, the cumulative value
# read as 50 Cr, and the turnover clause filed as a declaration
BAD_LLM = [
    {"id": "C1", "kind": "similar_works", "text": ROAD_40PCT,
     "params": {"categories": ["roads highways"], "min_value": 32_000_000, "count": 1, "lookback_years": 7}},
    {"id": "C2", "kind": "similar_works", "text": ROAD_CUMULATIVE,
     "params": {"categories": ["roads highways"], "min_value": 30_000_000, "count": 1, "lookback_years": 7,
                "cumulative_value": 500_000_000}},
    {"id": "C3", "kind": "other", "text": TURNOVER_PCT, "params": {}},
]


@pytest.fixture(scope="module")
def g():
    g = Graph()
    assert g.exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    return g


@pytest.fixture(scope="module")
def scratch(g):
    s = g.copy_to("pytest_clauses_r4")
    yield s
    s.delete()


def _fake_llm(monkeypatch, clauses_json: list[dict]):
    monkeypatch.setattr(providers, "chat",
                        lambda *a, **k: SimpleNamespace(content=json.dumps({"clauses": clauses_json})))


def _one(text: str, **kw) -> dict:
    out = clauses.parse_rules(text, **kw)
    assert len(out) == 1, out
    return out[0]


def _tender(text: str, clauses_=None, title="Track C test") -> dict:
    t = {"id": "T-TEST-R4", "title": title, "bid_date": BID_DATE, "category": "roads highways", "text": text}
    if clauses_ is not None:
        t["clauses"] = clauses_
    return t


def _verified(ev: list[dict]) -> dict:
    return [e for e in ev if e["type"] == "verified"][0]


# ------------------------------------------------------------------ 1. 40% of the estimated cost: one threshold

def test_rules_single_percentage_is_one_threshold_not_cpwd(g):
    c = _one(ROAD_40PCT)
    assert c["kind"] == "similar_works"
    assert c["params"]["min_value"] == 32 * CR and c["params"]["count"] == 1
    assert "estimated_cost" not in c["params"] and "options" not in c["params"]
    r = qualify_clause(g, "similar_works", c["params"], BID_DATE)["result"]
    assert r["verdict"] == "PASS" and [w["key"] for w in r["satisfied_by"]] == [PKG7]
    # the old misreading (CPWD on an 80 Cr estimate) fails the same company
    assert qualify_clause(g, "similar_works", {**c["params"], "min_value": None, "count": None,
                                               "estimated_cost": 80 * CR}, BID_DATE)["result"]["verdict"] == "FAIL"
    assert clauses.amount_check("similar_works", c["params"], ROAD_40PCT)["status"] == "verified"


def test_rules_cpwd_only_when_the_structure_is_there():
    # 80/60/40 written without 'or' between the options is still the CPWD rule
    c = _one("Similar works (roads) in the last 7 years: (a) one work of 80% of the estimated cost of INR 50 Cr; "
             "(b) two works of 60% of the estimated cost; (c) three works of 40% of the estimated cost.")
    assert c["params"]["estimated_cost"] == 50 * CR
    # three amounts that are not 80/60/40 of one estimate are not the CPWD rule
    c = _one("Similar works (roads) in the last 7 years: one work of INR 50 Cr, or two of INR 45 Cr, or three of "
             "INR 10 Cr.")
    assert "estimated_cost" not in c["params"] and len(c["params"]["options"]) == 3
    # a single 60% of a stated estimate: one work of 60% of it
    c = _one("One similar road work of at least 60% of the estimated cost of INR 100 Cr in the last 7 years.")
    assert c["params"]["min_value"] == 60 * CR and "estimated_cost" not in c["params"]


# ------------------------------------------------------------------ 2. cumulative value -> min_total

def test_rules_cumulative_value_is_kept_and_fails(g):
    c = _one(ROAD_CUMULATIVE)
    assert c["params"]["min_value"] == 30 * CR and c["params"]["min_total"] == 500 * CR
    r = qualify_clause(g, "similar_works", c["params"], BID_DATE)["result"]
    assert r["verdict"] == "FAIL"
    assert r["cumulative_value"] == 1_318_200_000 and r["min_total"] == 500 * CR and not r["cumulative_met"]
    assert "cumulative value" in r["basis"]
    # the single-work part alone passes: dropping the cumulative value is what made the clause pass wrongly
    assert qualify_clause(g, "similar_works", {k: v for k, v in c["params"].items() if k != "min_total"},
                          BID_DATE)["result"]["verdict"] == "PASS"
    assert clauses.amount_check("similar_works", c["params"], ROAD_CUMULATIVE)["status"] == "verified"


def test_min_total_engine_semantics(g):
    base = {"categories": ["roads highways", "expressways"], "lookback_years": 7}
    ok = qualify_clause(g, "similar_works", {**base, "min_value": 30 * CR, "min_total": 500 * CR}, BID_DATE)["result"]
    assert ok["verdict"] == "PASS" and sum(w["value"] for w in ok["satisfied_by"]) >= 500 * CR
    alone = qualify_clause(g, "similar_works", {**base, "min_total": 500 * CR}, BID_DATE)["result"]
    assert alone["verdict"] == "PASS" and alone["cumulative_met"]
    with pytest.raises(ValueError):
        qualify_clause(g, "similar_works", {**base, "min_total": -1})
    for text in ("Similar works: aggregate value of similar works (roads) completed in the last 7 years not less "
                 "than INR 500 Cr.", "Similar works: one road work of at least INR 50 Cr and total road works of "
                 "INR 120 Cr."):
        assert _one(text)["params"]["min_total"] in (500 * CR, 120 * CR)


def test_rules_recognised_but_unreadable_cumulative_is_needs_review_not_dropped():
    c = _one("2. Similar works: one road work of INR 30 Cr and a cumulative value of road works as approved by "
             "the Engineer.")
    assert c["kind"] == "other" and c["params"]["unparsed"] == "similar_works"


# ------------------------------------------------------------------ 3. % of the estimated cost from the header

def test_rules_percentage_resolved_from_the_tender_header():
    out = {c["id"]: c for c in clauses.parse_rules(TENDER_TEXT)}
    assert set(out) == {"1", "2", "3"}                         # the header line is context, not a clause
    assert out["3"]["kind"] == "turnover" and out["3"]["params"]["min_average"] == 600 * CR
    assert out["1"]["params"]["min_value"] == 32 * CR       # its own estimate wins over the header's
    # or from another clause's estimate
    other = clauses.parse_rules("1. Similar works: one road work of 80% of the estimated cost of INR 250 Cr, or two "
                                "of 60%, or three of 40%, last 7 years.\n2. " + TURNOVER_PCT[3:])
    assert other[1]["params"]["min_average"] == 75 * CR
    # no estimate anywhere: unparsed, of kind turnover
    c = _one(TURNOVER_PCT)
    assert c["kind"] == "other" and c["params"]["unparsed"] == "turnover"
    assert clauses.known_kind(TURNOVER_PCT) == "turnover"
    assert clauses.known_kind("2.6 The bidder shall not have been blacklisted by any agency (declaration).") is None


def test_unparsed_turnover_cannot_yield_bid(scratch):
    """Every other clause passes; the turnover clause cannot be read. The old pipeline said DECLARE -> BID."""
    text = ("1. Valid ISO 9001:2015 certificate.\n2. The bidder shall have been in business for at least 10 years.\n"
            + TURNOVER_PCT)
    ev = []
    case = qualify_tender(scratch, _tender(text), emit=ev.append, prefer_llm=False)
    cid = case["case"]["id"]
    by = {c["ref"]: c for c in case["clauses"]}
    assert by["3"]["verdict"] == "NEEDS REVIEW" and by["3"]["verdict"] != "DECLARE"
    assert any(f and f["kind"] == "needs_review" and f["severity"] == "high" for f in by["3"]["findings"])
    assert case["case"]["verdict"] == "NEEDS REVIEW"
    assert scratch.scalar("MATCH (c:Case {id:$c}) RETURN c.verified", {"c": cid}) is False
    assert not _verified(ev)["ok"] and _verified(ev)["needs_review"] == ["3"]
    # the shared rule (ripple, review_bid) cannot say BID either: the finding is live
    from bidbrain.tools.case import overall_verdict
    assert overall_verdict(scratch, cid) != "BID"
    assert "Needs review" in case["case"]["memo"]


def test_header_turnover_gives_the_right_verdict(scratch):
    case = qualify_tender(scratch, _tender(HEADER + TURNOVER_PCT), prefer_llm=False)
    (c,) = case["clauses"]
    assert c["kind"] == "turnover" and c["verdict"] == "FAIL"          # average 253.53 Cr < 600 Cr
    assert case["case"]["verdict"] == "NO-BID"


def test_whole_tender_on_the_rules_path(scratch):
    ev = []
    case = qualify_tender(scratch, _tender(TENDER_TEXT), emit=ev.append, prefer_llm=False)
    by = {c["ref"]: c for c in case["clauses"]}
    assert by["1"]["verdict"] == "PASS" and [s["key"] for s in by["1"]["satisfied_by"]] == [PKG7]
    assert by["2"]["verdict"] == "FAIL" and by["3"]["verdict"] == "FAIL"
    assert case["case"]["verdict"] == "NO-BID" and _verified(ev)["ok"]


# ------------------------------------------------------------------ 4. the LLM path, with the judges' bad JSON

def test_llm_10x_amounts_and_dropped_parts_are_corrected(monkeypatch):
    _fake_llm(monkeypatch, BAD_LLM)
    out = {c["id"]: c for c in clauses.parse_llm(TENDER_TEXT)}
    c1, c2, c3 = out["C1"], out["C2"], out["C3"]
    assert c1["params"]["min_value"] == 32 * CR and "estimated_cost" not in c1["params"]
    assert "amounts" in c1["corrected"] and "₹3.20 Cr" in c1["corrected"]
    assert c2["params"]["min_value"] == 30 * CR and c2["params"]["min_total"] == 500 * CR
    assert "₹3.00 Cr" in c2["corrected"] and "₹50.00 Cr" in c2["corrected"]
    assert c3["kind"] == "turnover" and c3["params"]["min_average"] == 600 * CR
    assert "declaration" in c3["corrected"]
    for c in out.values():
        assert clauses.amount_check(c["kind"], c["params"], c["text"],
                                    clauses.stated_estimate(TENDER_TEXT))["status"] == "verified"


def test_llm_drops_the_cumulative_value(monkeypatch):
    _fake_llm(monkeypatch, [{"id": "C2", "kind": "similar_works", "text": ROAD_CUMULATIVE,
                             "params": {"categories": ["roads highways"], "min_value": 300_000_000, "count": 1}}])
    (c,) = clauses.parse_llm(ROAD_CUMULATIVE)
    assert c["params"]["min_total"] == 500 * CR and "₹500.00 Cr" in c["corrected"]


def test_llm_turnover_10x_with_the_header_estimate(monkeypatch):
    _fake_llm(monkeypatch, [{"id": "C3", "kind": "turnover", "text": TURNOVER_PCT,
                             "params": {"min_average": 600_000_000, "years": 3}}])
    (c,) = clauses.parse_llm(HEADER + TURNOVER_PCT)
    assert c["params"]["min_average"] == 600 * CR and "corrected" in c


def test_power_of_ten_fallback_without_a_rule_reading():
    c = {"id": "X", "kind": "similar_works", "text": "One work of INR 30 Cr.", "params": {"min_value": 30_000_000}}
    fixed = clauses._fix_amounts(c, None, None)
    assert fixed["params"]["min_value"] == 30 * CR and "power of ten" in fixed["corrected"]


def test_llm_path_end_to_end(scratch, monkeypatch):
    _fake_llm(monkeypatch, BAD_LLM)
    monkeypatch.setattr(providers, "available", lambda: False)      # template memo; clauses still from the LLM
    ev = []
    case = qualify_tender(scratch, _tender(TENDER_TEXT), emit=ev.append, prefer_llm=True)
    by = {c["ref"]: c for c in case["clauses"]}
    assert set(by) == {"C1", "C2", "C3"}
    assert by["C1"]["verdict"] == "PASS" and by["C2"]["verdict"] == "FAIL" and by["C3"]["verdict"] == "FAIL"
    assert all(by[r]["corrected"] for r in by)
    assert case["case"]["verdict"] == "NO-BID" and _verified(ev)["ok"]


def test_llm_declaration_stays_declare(monkeypatch):
    text = "2.6 The bidder shall not have been blacklisted by any Central/State agency (declaration)."
    _fake_llm(monkeypatch, [{"id": "2.6", "kind": "other", "text": text, "params": {}}])
    (c,) = clauses.parse_llm(text)
    assert c["kind"] == "other" and not c["params"] and not c.get("corrected")


# ------------------------------------------------------------------ 5. the Verifier: independent, honest

def test_amount_check_flags_a_dropped_cumulative_value():
    chk = clauses.amount_check("similar_works", {"categories": ["roads highways"], "min_value": 30 * CR, "count": 1},
                               ROAD_CUMULATIVE)
    assert chk["status"] == "mismatch" and any("₹500.00 Cr" in p for p in chk["problems"])
    # a 10x threshold is not an amount of the text
    chk = clauses.amount_check("similar_works", {"min_value": 32_000_000, "count": 1}, ROAD_40PCT)
    assert chk["status"] == "mismatch" and any("₹3.20 Cr" in p for p in chk["problems"])
    # the CPWD rule on an 80 Cr estimate leaves the 40% clause's thresholds unexplained
    chk = clauses.amount_check("similar_works", {"estimated_cost": 80 * CR}, ROAD_40PCT)
    assert chk["status"] == "mismatch"


def test_verifier_flags_a_dropped_cumulative_value(scratch):
    """Pre-parsed params without the cumulative value: the EvidenceHunter says PASS, the Verifier's amount
    reading finds ₹500 Cr unaccounted, the text's reading gives FAIL, so the case is NEEDS REVIEW."""
    ev = []
    case = qualify_tender(scratch, _tender(ROAD_CUMULATIVE, [
        {"id": "2", "kind": "similar_works", "text": ROAD_CUMULATIVE,
         "params": {"categories": ["roads highways"], "lookback_years": 7, "min_value": 30 * CR, "count": 1}}]),
        emit=ev.append, prefer_llm=False)
    v = _verified(ev)
    assert case["case"]["verdict"] == "NEEDS REVIEW" and not v["ok"]
    m = v["mismatches"][0]
    assert m["stored"] == "PASS" and m["fresh"] == "FAIL"
    assert any(r.startswith("amounts:") and "₹500.00 Cr" in r for r in m["reasons"])


def test_verifier_is_independent_of_the_parser(scratch, monkeypatch):
    """Break the parser so it drops the cumulative value (the round-3 bug): the rules path then reads only the
    30 Cr single-work threshold and passes. The Verifier's amount reading shares none of the parser's code and
    still flags the unaccounted ₹500 Cr: NEEDS REVIEW, not verified."""
    monkeypatch.setattr(clauses, "_cumulative", lambda s, est=None: (None, None, False))
    (c,) = clauses.parse_rules(ROAD_CUMULATIVE)
    assert "min_total" not in c["params"]                       # the parser really is broken
    ev = []
    case = qualify_tender(scratch, _tender(ROAD_CUMULATIVE), emit=ev.append, prefer_llm=False)
    v = _verified(ev)
    assert case["case"]["verdict"] == "NEEDS REVIEW" and not v["ok"]
    assert any(r.startswith("amounts:") and "₹500.00 Cr" in r for r in v["mismatches"][0]["reasons"])
    assert scratch.scalar("MATCH (c:Case {id:$c}) RETURN c.verified", {"c": case["case"]["id"]}) is False


def test_verifier_never_claims_verified_on_a_clause_it_cannot_check(scratch):
    text = "1. Similar works: one road work in the last 7 years."          # no amount to check 30 Cr against
    ev = []
    case = qualify_tender(scratch, _tender(text, [
        {"id": "1", "kind": "similar_works", "text": text,
         "params": {"categories": ["roads highways"], "lookback_years": 7, "min_value": 30 * CR, "count": 1}}]),
        emit=ev.append, prefer_llm=False)
    v = _verified(ev)
    assert not v["ok"] and v["unverified"] == ["1"] and case["case"]["verdict"] == "NEEDS REVIEW"
    cid = case["case"]["id"]
    assert scratch.scalar("MATCH (c:Case {id:$c}) RETURN c.verified", {"c": cid}) is False
    assert scratch.scalar("MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause) RETURN k.verification",
                          {"c": cid}) == "unverified"


# ------------------------------------------------------------------ 6. memo_check against the clause table

CLAUSE_TABLE = [
    {"ref": "C1", "kind": "similar_works", "params": json.dumps({"min_value": 30 * CR, "count": 1, "min_total": 500 * CR}),
     "verdict": "FAIL", "basis": "need 1 work(s) >= 300,000,000; found 1; cumulative value of 7 work(s) in scope = "
                                 "1,318,200,000 vs required 5,000,000,000", "corrected": None, "findings": []},
    {"ref": "C2", "kind": "turnover", "params": json.dumps({"min_average": 50 * CR, "years": 3}), "verdict": "PASS",
     "basis": "average of last 3 FY = 2,535,266,666 vs required 500,000,000", "corrected": None, "findings": []},
]
EVIDENCE = [{"clause": "C1", "kind": "Work", "item": "Rigid Pavement — Madhya Pradesh Pkg-7", "credited": 375_200_000,
             "doc": "DOC-CC-007", "page": 1}]


def _facts() -> str:
    return json.dumps(orchestrator._money_facts({"clauses": CLAUSE_TABLE, "evidence": EVIDENCE}), ensure_ascii=False)


def test_memo_check_ties_figures_and_conflicts_to_their_clause():
    good = ("NO-BID. C1 fails: Pkg-7 (₹37.52 Cr, DOC-CC-007 p.1) meets the ₹30.00 Cr single-work bar, but road "
            "works total ₹131.82 Cr against a cumulative ₹500.00 Cr. C2 passes (average ₹253.53 Cr vs ₹50.00 Cr).")
    assert memo_problems(good, _facts(), clauses=CLAUSE_TABLE, evidence=EVIDENCE) == []
    # the judges' memo: a conflict that does not exist, with a figure that belongs to another clause
    bad = "C1 cumulative value conflict: the params say ₹50.00 Cr."
    assert memo_problems(bad, _facts()) == []                      # the old check: ₹50 Cr is "in the facts"
    probs = memo_problems(bad, _facts(), clauses=CLAUSE_TABLE, evidence=EVIDENCE)
    assert any("₹50.00 Cr for clause C1" in p for p in probs)
    assert any("conflict" in p and "C1" in p for p in probs)
    assert any("C9" in p for p in memo_problems("Clause C9 passes.", _facts(), clauses=CLAUSE_TABLE,
                                                 evidence=EVIDENCE))
    # a table row is checked against its own clause
    row = "| C2 turnover | PASS | Pkg-7 ₹37.52 Cr |"
    assert any("C2" in p for p in memo_problems(row, _facts(), clauses=CLAUSE_TABLE, evidence=EVIDENCE))


def test_llm_memo_with_a_misattributed_figure_falls_back_to_the_template(scratch, monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: True)
    t = _tender(TENDER_TEXT)
    ev = []

    def fake_agent(*a, **k):
        return SimpleNamespace(final="BID WITH FIXES. Clause 2 cumulative value conflict: params say ₹32.00 Cr.",
                               error=None, evidence=[], usage={"llm_calls": 1, "prompt": 1, "completion": 1,
                                                               "tool_calls": 0})
    monkeypatch.setattr(orchestrator, "run_agent", fake_agent)
    monkeypatch.setattr(providers, "chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no LLM")))
    case = qualify_tender(scratch, t, emit=ev.append, prefer_llm=True)       # clauses fall back to the rules
    memo = [e for e in ev if e["type"] == "memo"][0]
    assert memo["method"] == "template" and not memo["memo_check"]["ok"]
    assert any("clause 2" in p for p in memo["memo_check"]["problems"])
    assert case["case"]["verdict"] == "NO-BID"
