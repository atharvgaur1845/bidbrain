"""Round-3 qualification correctness: either/or clauses, when the rule parser may correct an LLM reading,
_sanitize not widening a clause, a Verifier whose checks are independent of the EvidenceHunter (and whose
retry / NEEDS REVIEW path is reached without monkeypatching), the Assembler memo's own check, and the memo
re-assembled after a ripple. Mutating tests run on a GRAPH.COPY of bidbrain_base.

    cd backend && pytest -q tests/test_qualify_r3.py
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bidbrain import config
from bidbrain.agents import clauses, orchestrator, providers
from bidbrain.agents.orchestrator import memo_problems, qualify_tender
from bidbrain.graph.client import Graph
from bidbrain.tools.proof import qualify_clause

EITHER_OR = ("2.1 Similar works: either one building work of value at least INR 50 Cr, or two building works "
             "each of value at least INR 40 Cr, completed in the last 7 years.")
OPTIONS = [{"count": 1, "min_value": 500_000_000}, {"count": 2, "min_value": 400_000_000}]
BUILDINGS_40CR = {"residentialquartersuttarpradeshpkg25", "institutionalbuildingdelhipkg81",
                  "hospitalblockwestbengalpkg60"}


@pytest.fixture(scope="module")
def g():
    g = Graph()
    assert g.exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    return g


@pytest.fixture(scope="module")
def scratch(g):
    s = g.copy_to("pytest_qualify_r3")
    yield s
    s.delete()


def _tenders() -> dict:
    return {t["id"]: t for t in json.load(open(config.DATA / "tenders" / "demo_tenders.json"))}


def _chain(g, case_id):
    nxt = {r["a"]: r["b"] for r in g.read("""MATCH (a:AgentRun)-[:WORKED_ON]->(:Case {id:$c}),
                                                   (a)-[:HANDED_OFF_TO]->(b:AgentRun)
                                             RETURN a.id AS a, b.id AS b""", {"c": case_id})}
    names = {r["id"]: r["agent"] for r in g.read("MATCH (r:AgentRun)-[:WORKED_ON]->(:Case {id:$c}) "
                                                 "RETURN r.id AS id, r.agent AS agent", {"c": case_id})}
    cur = (set(names) - set(nxt.values())).pop()
    out = [names[cur]]
    while cur in nxt:
        cur = nxt[cur]
        out.append(names[cur])
    return out


def _fake_llm(monkeypatch, clauses_json: list[dict]):
    monkeypatch.setattr(providers, "chat",
                        lambda *a, **k: SimpleNamespace(content=json.dumps({"clauses": clauses_json})))


# ------------------------------------------------------------------ 1. either/or in the engine

def test_qualify_clause_either_or_any_option_passes_and_names_it(g):
    base = {"categories": ["buildings"], "lookback_years": 7}
    r = qualify_clause(g, "similar_works", {**base, "options": OPTIONS}, "2026-06-30")["result"]
    assert r["verdict"] == "PASS" and r["option_met"].startswith("(b) 2 work(s)")
    assert len(r["satisfied_by"]) == 2 and {w["key"] for w in r["satisfied_by"]} <= BUILDINGS_40CR
    assert [c["passed"] for c in r["options_checked"]] == [False, True]
    # the first alternative alone (the old misparse) fails the same company
    first = qualify_clause(g, "similar_works", {**base, "min_value": 500_000_000, "count": 1}, "2026-06-30")
    assert first["result"]["verdict"] == "FAIL"
    # an alternative nobody meets: FAIL with the closest works shown
    none = qualify_clause(g, "similar_works", {**base, "options": [{"count": 1, "min_value": 900_000_000},
                                                                  {"count": 3, "min_value": 450_000_000}]})
    assert none["result"]["verdict"] == "FAIL" and none["result"]["near_misses"]
    # the CPWD 1/2/3 cost rule is unchanged
    cp = qualify_clause(g, "similar_works", {"categories": ["roads highways", "expressways"],
                                             "estimated_cost": 2_500_000_000}, "2026-06-30")["result"]
    assert cp["verdict"] == "PASS" and "of estimated cost" in cp["basis"]


def test_options_are_validated(g):
    for bad in ({"options": OPTIONS, "min_value": 500_000_000}, {"options": OPTIONS, "estimated_cost": 1},
                {"options": [{"count": 2}]}, {"options": [{"count": 1, "min_value": 5, "value": 9}]},
                {"options": []}):
        with pytest.raises(ValueError):
            qualify_clause(g, "similar_works", bad)


# ------------------------------------------------------------------ 2. the rule parser

def test_rule_parser_reads_either_or_as_options_not_two_clauses():
    out = clauses.parse_rules(EITHER_OR)
    assert len(out) == 1 and out[0]["kind"] == "similar_works"
    assert out[0]["params"]["options"] == OPTIONS and "min_value" not in out[0]["params"]
    # (a) / (b) lettered alternatives with absolute amounts
    lettered = clauses.parse_rules("1. Experience: (a) one bridge work of INR 75 Cr; or (b) two bridge works of "
                                   "INR 50 Cr each; or (c) three bridge works of INR 40 Cr each, last 7 years.")
    assert lettered[0]["params"]["options"] == [{"count": 1, "min_value": 750_000_000},
                                                {"count": 2, "min_value": 500_000_000},
                                                {"count": 3, "min_value": 400_000_000}]
    # CPWD written as percentages or as amounts is still the estimated-cost rule
    pct = clauses.parse_rules("Similar works (tunnels) completed in the last 5 years: one work of at least 80% of the "
                              "estimated cost of INR 40 Cr, or two works of 60%, or three works of 40%.")
    assert pct[0]["params"]["estimated_cost"] == 400_000_000
    amt = clauses.parse_rules("B. Similar works (buildings or small buildings): one completed work of at least INR 72 "
                              "Cr, or two of INR 54 Cr, or three of INR 36 Cr, in the last 7 years.")
    assert amt[0]["params"]["estimated_cost"] == 900_000_000
    # amounts joined by 'and' are not alternatives
    both = clauses.parse_rules("Similar works: one road work of at least INR 50 Cr and total road works of INR 120 Cr.")
    assert "options" not in both[0]["params"] and both[0]["params"]["min_value"] == 500_000_000


def test_rule_parser_reads_a_percentage_of_an_amount():
    t = clauses.parse_rules("Average annual turnover of the last 3 financial years shall be at least 30% of the "
                            "estimated cost of INR 500 Cr.")
    assert t[0]["params"]["min_average"] == 1_500_000_000
    s = clauses.parse_rules("One similar work of value at least 50% of the estimated cost of INR 100 Cr, last 5 years.")
    assert s[0]["params"]["min_value"] == 500_000_000


def test_cats_does_not_add_bridges_flyovers_to_a_large_bridge():
    assert clauses._cats("at least one large bridge work") == ["large bridges"]
    assert set(clauses._cats("bridge works (large bridges or bridges & flyovers)")) == {"large bridges",
                                                                                        "bridges flyovers"}


# ------------------------------------------------------------------ 3. LLM clauses: sanitize and reconcile

def test_llm_either_or_survives_and_is_restored_when_dropped(monkeypatch):
    _fake_llm(monkeypatch, [
        {"id": "A", "kind": "similar_works", "text": EITHER_OR,
         "params": {"categories": ["buildings"], "lookback_years": 7, "options": OPTIONS}},
        {"id": "B", "kind": "similar_works", "text": EITHER_OR.replace("2.1", "2.2"),    # kept only option (a)
         "params": {"categories": ["buildings"], "lookback_years": 7, "min_value": 500_000_000, "count": 1}},
    ])
    out = {c["id"]: c for c in clauses.parse_llm("ignored")}
    assert out["A"]["params"]["options"] == OPTIONS and not out["A"].get("corrected")
    assert out["B"]["params"]["options"] == OPTIONS and "min_value" not in out["B"]["params"]
    assert "either/or" in out["B"]["corrected"]


def test_llm_split_either_or_is_merged_into_one_clause(monkeypatch):
    _fake_llm(monkeypatch, [
        {"id": "1a", "kind": "similar_works", "text": "Either one building work of value at least INR 50 Cr,",
         "params": {"categories": ["buildings"], "min_value": 500_000_000, "count": 1}},
        {"id": "1b", "kind": "similar_works", "text": "or two building works each of value at least INR 40 Cr.",
         "params": {"categories": ["buildings"], "min_value": 400_000_000, "count": 2}},
    ])
    out = clauses.parse_llm("ignored")
    assert len(out) == 1 and out[0]["params"]["options"] == OPTIONS and "merged" in out[0]["corrected"]


def test_rule_parser_overrides_amounts_only_when_unambiguous(monkeypatch):
    pct_text = ("2.2 Average annual turnover of the last 3 financial years shall be at least 30% of the estimated "
                "cost of INR 500 Cr.")
    _fake_llm(monkeypatch, [
        {"id": "T1", "kind": "turnover", "text": pct_text, "params": {"min_average": 1_500_000_000, "years": 3}},
        {"id": "T2", "kind": "turnover", "text": "Average annual turnover of the last 3 financial years shall be at "
         "least INR 75 Cr.", "params": {"min_average": 7_500_000_000, "years": 3}},
        {"id": "T3", "kind": "turnover", "text": "Turnover of INR 40 Cr in FY 2024-25 and INR 60 Cr on average over "
         "the last 3 years.", "params": {"min_average": 600_000_000, "years": 3}},
    ])
    out = {c["id"]: c for c in clauses.parse_llm("ignored")}
    assert out["T1"]["params"]["min_average"] == 1_500_000_000 and not out["T1"].get("corrected")   # 30% of 500 Cr
    assert out["T2"]["params"]["min_average"] == 750_000_000 and "LLM 7,500,000,000" in out["T2"]["corrected"]
    assert out["T3"]["params"]["min_average"] == 600_000_000 and not out["T3"].get("corrected")    # two amounts


def test_sanitize_never_widens_a_clause():
    p = clauses._sanitize("similar_works", {"categories": ["large bridges"], "min_value": 1, "require_reference": True},
                          "One large bridge work of at least INR 70 Cr.")
    assert p["categories"] == ["large bridges"] and "require_reference" not in p
    keep = clauses._sanitize("similar_works", {"categories": ["roads", "highways", "expressways"], "min_value": 1,
                                               "require_reference": True},
                             "Roads work of INR 70 Cr, with a client reference letter.")
    assert set(keep["categories"]) == {"roads highways", "expressways"} and keep["require_reference"] is True
    assert clauses._sanitize("similar_works", {"options": OPTIONS}, EITHER_OR)["options"] == OPTIONS


# ------------------------------------------------------------------ 4. the Verifier, without monkeypatching

def _sample(clause: dict, title="Verifier test") -> dict:
    return {"id": "T-TEST-VER", "title": title, "bid_date": "2026-06-30", "category": "buildings", "clauses": [clause]}


def test_params_that_disagree_with_the_text_end_in_needs_review(scratch):
    """The round-2 failure, as a pre-parsed clause: the text is either/or, the params kept only the first
    alternative. The EvidenceHunter says FAIL; the Verifier re-reads the text, hands it back, the text's
    reading gives PASS, so the disagreement is material and the case is NEEDS REVIEW (not NO-BID)."""
    ev = []
    case = qualify_tender(scratch, _sample({"id": "2.1", "kind": "similar_works", "text": EITHER_OR,
                                            "params": {"categories": ["buildings"], "lookback_years": 7,
                                                       "min_value": 500_000_000, "count": 1}}),
                          emit=ev.append, prefer_llm=False)
    cid = case["case"]["id"]
    assert case["case"]["verdict"] == "NEEDS REVIEW"
    assert scratch.scalar("MATCH (c:Case {id:$c}) RETURN c.verified", {"c": cid}) is False
    assert _chain(scratch, cid)[-4:] == ["Verifier", "EvidenceHunter", "Verifier", "Assembler"]
    f = scratch.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->()-[:HAS_FINDING]->(f:Finding {kind:'verification_failed'})
                        RETURN f.status AS status, f.text AS text""", {"c": cid})
    assert f[0]["status"] == "persisted" and "text:" in f[0]["text"] and "reads PASS" in f[0]["text"]
    v = [e for e in ev if e["type"] == "verified"][0]
    assert not v["ok"] and v["mismatches"][0]["fresh"] == "PASS" and v["mismatches"][0]["stored"] == "FAIL"
    assert "Recommendation withheld" in case["case"]["memo"]


def test_immaterial_text_disagreement_is_resolved(scratch):
    """Params 70 Cr, text 75 Cr: both readings PASS, so the hand-back resolves it and the case stands."""
    ev = []
    case = qualify_tender(scratch, _sample({"id": "2.2", "kind": "turnover", "text": "2.2 Average annual turnover of "
                                            "the last 3 financial years shall be at least INR 75 Cr.",
                                            "params": {"min_average": 700_000_000, "years": 3}}),
                          emit=ev.append, prefer_llm=False)
    cid = case["case"]["id"]
    assert case["case"]["verdict"] == "BID" and [e for e in ev if e["type"] == "verified"][0]["ok"]
    assert "EvidenceHunter" in _chain(scratch, cid)[3:]
    f = scratch.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->()-[:HAS_FINDING]->(f:Finding {kind:'verification_failed'})
                        RETURN f.status AS status, f.text AS text""", {"c": cid})
    assert f[0]["status"] == "resolved" and "both readings give PASS" in f[0]["text"]


def test_verifier_recomputes_credited_values_from_the_winning_claims(scratch):
    """A stale materialized value (the property says 130 Cr, every claim still says 77.59 Cr): the
    EvidenceHunter credits the property, the Verifier recomputes from the claims, and the re-derivation
    cannot fix it, so the case is NEEDS REVIEW."""
    key = "extradosedbridgerajasthanpkg124"
    real = scratch.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": key})
    scratch.run("MATCH (w:Work {key:$k}) SET w.value = 1300000000", {"k": key})
    try:
        ev = []
        case = qualify_tender(scratch, _sample({"id": "1", "kind": "similar_works",
                                                "text": "1. One large bridge work of value at least INR 100 Cr in the "
                                                        "last 7 years.",
                                                "params": {"categories": ["large bridges"], "lookback_years": 7,
                                                           "min_value": 1_000_000_000, "count": 1,
                                                           "require_reference": True}}),
                              emit=ev.append, prefer_llm=False)
    finally:
        scratch.run("MATCH (w:Work {key:$k}) SET w.value = $v", {"k": key, "v": real})
    assert case["case"]["verdict"] == "NEEDS REVIEW"
    reasons = [e for e in ev if e["type"] == "verified"][0]["mismatches"][0]["reasons"]
    assert any(r.startswith("claims:") and "DOC-CC-124" in r for r in reasons)
    assert any("meet none of the clause's alternatives" in r for r in reasons)


# ------------------------------------------------------------------ 5. the Assembler memo

def test_memo_check_rejects_unknown_docs_and_amounts():
    facts = json.dumps({"evidence": [{"doc": "DOC-CC-124", "credited": "₹77.59 Cr"}],
                        "clauses": [{"basis": "need 1 work(s) >= ₹70.00 Cr"}]}, ensure_ascii=False)
    assert memo_problems("Relies on Pkg-124 (₹77.59 Cr, DOC-CC-124 p.1) against ₹70.00 Cr.", facts) == []
    bad = memo_problems("Pkg-124 is worth ₹775.90 Cr (DOC-CC-999).", facts)
    assert any("DOC-CC-999" in b for b in bad) and any("₹775.90 Cr" in b for b in bad)


def test_llm_memo_with_a_foreign_doc_falls_back_to_the_template(scratch, monkeypatch):
    monkeypatch.setattr(providers, "available", lambda: True)
    monkeypatch.setattr(orchestrator, "run_agent", lambda *a, **k: SimpleNamespace(
        final="BID. Turnover ₹253.53 Cr per DOC-FS-1999 p.4.", error=None, evidence=[],
        usage={"llm_calls": 1, "prompt": 1, "completion": 1, "tool_calls": 0}))
    ev = []
    t = _tenders()["T-NHX-2026-014"]
    case = qualify_tender(scratch, t, emit=ev.append, prefer_llm=True)
    memo = [e for e in ev if e["type"] == "memo"][0]
    assert memo["method"] == "template" and not memo["memo_check"]["ok"]
    assert any("DOC-FS-1999" in p for p in memo["memo_check"]["problems"])
    assert "₹" in case["case"]["memo"] and " Cr |" in case["case"]["memo"]          # amounts as ₹X.XX Cr


def test_ripple_rebuilds_the_memo(g):
    from bidbrain.conflicts.engine import apply_event
    from bidbrain.tools.case import apply_flips, blast_radius
    s = g.copy_to("pytest_qualify_r3_ripple")
    try:
        case = qualify_tender(s, _tenders()["T-NHX-2026-014"], prefer_llm=False)
        cid = case["case"]["id"]
        assert "recommendation: **BID**" in case["case"]["memo"]
        demo = {e["event_id"]: e for e in json.load(open(config.DATA / "conflicts" / "demo_events.json"))}
        for eid in ("DEMO-0", "DEMO-1"):
            for d in apply_event(s, demo[eid])["decisions"]:
                br = blast_radius(s, d["conflict"]["subject_label"], d["conflict"]["subject_key"])
                apply_flips(s, br["result"]["verdict_flips"], eid)
        row = s.read("MATCH (c:Case {id:$c}) RETURN c.verdict AS v, c.memo AS m, c.memo_method AS h", {"c": cid})[0]
        assert row["v"] == "NO-BID" and row["h"] == "template"
        assert row["m"].startswith("> **Updated after DEMO-") and "recommendation: **NO-BID**" in row["m"]
    finally:
        s.delete()


# ------------------------------------------------------------------ 6. gap text, and the new sample tender

def test_gap_finding_names_the_clause_category(scratch):
    case = qualify_tender(scratch, _tenders()["T-JN-LB-2026-09"], prefer_llm=False)
    gaps = [f["text"] for c in case["clauses"] for f in c["findings"] if f and f["kind"] == "gap"]
    assert gaps and all("larger large bridges / bridges flyovers credential" in t for t in gaps)


def test_sample_tender_shows_reselection_a_kept_handback_and_a_planner_skip(scratch):
    t = _tenders()["T-RBC-2026-31"]
    assert {"id", "title", "authority", "category", "estimated_cost", "bid_date", "story", "text"} <= set(t)
    # the vetted clauses are what the rule parser reads from the tender's own text
    rules = {r["id"].split("-")[0]: r for r in clauses.parse_rules(t["text"])}
    for c in t["clauses"]:
        assert (rules[c["id"]]["kind"], rules[c["id"]]["params"]) == (c["kind"], c["params"]), c["id"]
    ev = []
    case = qualify_tender(scratch, t, emit=ev.append, prefer_llm=False)
    cid = case["case"]["id"]
    # clause 2 still credits works with no reference letter (the hand-back kept them): a conditional BID with
    # the letter requests as open actions, never a plain BID (closeout P0-2)
    assert case["case"]["verdict"] == "BID WITH FIXES"
    assert any(a["kind"] == "request_reference_letter" for a in case["actions"])
    assert _chain(scratch, cid) == ["ClauseParser", "Planner", "EvidenceHunter", "AbsenceAuditor", "EvidenceHunter",
                                    "Verifier", "Assembler"]
    assert [e["agent"] for e in ev if e["type"] == "agent_done" and e.get("skipped")] == ["ConflictSentinel"]
    plan = json.loads(scratch.scalar("MATCH (c:Case {id:$c}) RETURN c.plan", {"c": cid}))
    assert plan["handoffs"][0]["outcomes"] == [{"clause": "1", "outcome": "reselected", "verdict": "PASS"},
                                               {"clause": "2", "outcome": "kept", "verdict": "PASS"}]
    by = {c["ref"]: c for c in case["clauses"]}
    assert [s["key"] for s in by["1"]["satisfied_by"]] == ["extradosedbridgerajasthanpkg124"]
    assert "option (b) met" in by["2"]["basis"]
    assert [e for e in ev if e["type"] == "verified"][0]["ok"]
