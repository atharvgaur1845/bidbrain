"""Behaviour BidBrain depends on. Needs a running FalkorDB with `bidbrain_base` loaded
(`python -m bidbrain.ingest.load_graph`). Mutating tests work on a GRAPH.COPY.

    cd backend && pytest -q
"""
from __future__ import annotations

import json

import pytest

from bidbrain import config
from bidbrain.agents.clauses import parse_rules
from bidbrain.conflicts import engine
from bidbrain.conflicts.events import apply_event
from bidbrain.graph.client import Graph, day, iso
from bidbrain.ingest import aliases
from bidbrain.ingest.pages import date_renderings, indian_grouping, money_renderings
from bidbrain.tools import call
from bidbrain.tools.case import apply_flips, blast_radius


@pytest.fixture(scope="session")
def g():
    g = Graph()
    assert g.exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    return g


@pytest.fixture()
def scratch(g):
    s = g.copy_to("pytest_scratch")
    yield s
    s.delete()


# ------------------------------------------------------------------ ingestion invariants

def test_counts(g):
    n = {r["label"]: r["n"] for r in g.read("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n")}
    assert n["Work"] == 155 and n["Client"] == 29 and n["Person"] == 39 and n["Document"] == 687
    assert n["Bid"] == 6 and n["Credential"] == 48


def test_every_work_fully_linked(g):
    assert g.scalar("MATCH (w:Work) WHERE NOT (w)-[:LED_BY]->() OR NOT (w)-[:FOR_CLIENT]->() RETURN count(w)") == 0
    # all three independent views attest every work, each with a page
    assert g.scalar("MATCH (w:Work) WHERE size(w.views) <> 3 RETURN count(w)") == 0
    assert g.scalar("MATCH (:Work)-[e:EVIDENCED_BY]->() WHERE e.page IS NULL RETURN count(e)") == 0


def test_reference_letters_closed(g):
    assert g.scalar("MATCH (d:Document {doc_type:'reference_letter'}) WHERE NOT ()-[:HAS_REFERENCE]->(d) "
                    "RETURN count(d)") == 0
    assert g.scalar("MATCH (w:Work) WHERE w.has_reference RETURN count(w)") == 132


def test_money_and_dates_render_like_the_corpus():
    assert indian_grouping(333800000) == "33,38,00,000"
    r = money_renderings(333800000)
    assert "33.38 Cr" in r and "3,338.00 Lakh" in r and "3,338" in r
    assert "February 23, 2018" in date_renderings("2018-02-23")
    assert iso(day("2021-03-10")) == "2021-03-10"


def test_fulltext_survives_newlines(g):
    hits = g.scalar("CALL db.idx.fulltext.queryNodes('Chunk', 'Anganwadi') YIELD node RETURN count(node)")
    raw = g.scalar("MATCH (c:Chunk) WHERE c.text CONTAINS 'Anganwadi' RETURN count(c)")
    assert hits >= raw


# ------------------------------------------------------------------ resolution & tools

def test_informal_client_names(g):
    r = call(g, "resolve_entity", {"text": "phed odisha", "kind": "client"})["result"]
    assert r[0]["name"] == "Public Health Engineering Dept, Odisha" and r[0]["score"] == 100
    assert aliases.govt_level("Jal Nigam, Gujarat") == "state"
    assert aliases.govt_level("National Expressway Development Authority") == "central"


def test_via_work_hop_scopes_the_client_portfolio(g):
    r = call(g, "find_portfolio", {"via_work": "Ring Road — Uttar Pradesh Pkg-107"})["result"]
    assert r["n"] == 9 and all(w["client"] == "National Expressway Development Authority" for w in r["works"])


def test_date_span_hops_through_the_lead(g):
    # "Meera's PMP on the Pkg-29 ROB job": the person is whoever LED that work
    r = call(g, "date_span", {"work": "ROB — Jharkhand Pkg-29"})["result"]
    assert r["days"] == 854 and r["person_via"] == "work LED_BY"


def test_aggregate_is_exact_and_rejects_nothing_invented(g):
    keys = [w["key"] for w in call(g, "find_portfolio", {"client": "Jal Nigam, Jharkhand"})["result"]["works"]]
    r = call(g, "aggregate", {"work_keys": keys + ["not-a-work"], "op": "average"})
    assert r["result"]["value"] == 537933333 and r["missing_keys"] == ["not-a-work"]


def test_prove_absence_is_a_proof(g):
    r = call(g, "prove_absence", {"evidence": "reference_letter", "client": "Trishakti Power Generation Corporation"})
    res = r["result"]
    assert res["verdict"] == "PROVEN" and res["absent_count"] == 2 and res["universe"] == 19
    assert res["checks"]["corpus_closure"] == "132/132 letters linked"


def test_cpwd_rule_and_bid_date_validity(g):
    sw = call(g, "qualify_clause", {"kind": "similar_works", "bid_date": "2026-06-30",
                                    "params": {"categories": ["roads highways", "expressways"],
                                               "estimated_cost": 2500000000, "govt_levels": ["central", "state", "psu"]}})
    assert sw["result"]["verdict"] == "PASS" and "80%" in sw["result"]["basis"]
    kp = call(g, "qualify_clause", {"kind": "key_personnel", "bid_date": "2027-08-15",
                                    "params": {"credential": "PMP", "min_experience_years": 12, "count": 2,
                                               "categories": ["water treatment", "water supply"]}})
    assert kp["result"]["verdict"] == "AT_RISK" and kp["result"]["safe_alternates"]


def test_turnover_fails_by_the_restated_year(g):
    r = call(g, "qualify_clause", {"kind": "turnover", "params": {"min_each_year": 2400000000, "years": 3}})["result"]
    assert r["verdict"] == "FAIL" and "2024-25" in r["basis"]
    assert any(x["fy"] == "2024-25" for x in r["resolved_restatements"])


def test_readonly_cypher_cannot_write(g):
    out = call(g, "cypher_readonly", {"query": "CREATE (:Hack {x:1}) RETURN 1"})
    assert "error" in out


# ------------------------------------------------------------------ conflict engine

def test_base_restatements_resolved_and_rounding_ignored(g):
    assert g.scalar("MATCH (d:Decision {policy:'authority'}) RETURN count(d)") == 114
    # 193,299,999 (certificate) vs 193,300,000 (portfolio, 0.01 Cr precision) is NOT a conflict
    assert g.scalar("MATCH (c:Claim {subject_key:'roadwideningmaharashtrapkg21', predicate:'value'}) "
                    "WHERE c.status <> 'active' RETURN count(c)") == 0


def test_policy_order():
    mk = lambda **k: {"id": k.pop("id"), "value": k.pop("value"), "precision": 1, "source_doc": "DOC-T", **k}
    a = mk(id="a", value=1, source_kind="client_certificate", authority=90, issuer="X", observed_day=10)
    b = mk(id="b", value=2, source_kind="client_corrigendum", authority=95, issuer="X", observed_day=20)
    assert engine.propose({"claims": [a, b], "predicate": "value"}, [])["policy"] == "recency_same_issuer"
    letter = mk(id="c", value=3, source_kind="reference_letter", authority=80, issuer="X", observed_day=30)
    assert engine.propose({"claims": [a, letter], "predicate": "grading"}, [])["auto"] is False
    cv = mk(id="d", value=4, source_kind="cv", authority=40, issuer="P", observed_day=None)
    hr = mk(id="e", value=5, source_kind="company_record", authority=70, issuer="company", observed_day=None)
    assert engine.propose({"claims": [cv, hr], "predicate": "experience_years"}, [])["winner"]["id"] == "e"


def test_corrigendum_ripples_to_open_case(scratch):
    from bidbrain.agents.orchestrator import qualify_tender
    t = {x["id"]: x for x in json.load(open(config.DATA / "tenders" / "demo_tenders.json"))}["T-NHX-2026-014"]
    case = qualify_tender(scratch, t, prefer_llm=False)
    assert case["case"]["verdict"] == "BID"
    demo = {e["event_id"]: e for e in json.load(open(config.DATA / "conflicts" / "demo_events.json"))}
    for eid in ("DEMO-0", "DEMO-1"):
        out = apply_event(scratch, demo[eid])
        for d in out["decisions"]:
            br = blast_radius(scratch, d["conflict"]["subject_label"], d["conflict"]["subject_key"])
            apply_flips(scratch, br["result"]["verdict_flips"], eid)
    assert scratch.scalar("MATCH (c:Case) RETURN c.verdict") == "NO-BID"


def test_learned_rule_resolves_the_next_conflict(scratch):
    demo = {e["event_id"]: e for e in json.load(open(config.DATA / "conflicts" / "demo_events.json"))}
    first = apply_event(scratch, demo["DEMO-3"])
    assert first["cards"] and not first["decisions"]
    engine.resolve_card(scratch, first["cards"][0]["card_id"], first["claims"][0], "JV lead is authoritative",
                        apply_to_similar=True)
    second = apply_event(scratch, demo["DEMO-4"])
    assert second["decisions"] and second["decisions"][0]["policy"] == "learned_rule"


# ------------------------------------------------------------------ agents (no LLM needed)

def test_rule_clause_parser_matches_gold_kinds():
    for t in json.load(open(config.DATA / "tenders" / "demo_tenders.json")):
        got = [c["kind"] for c in parse_rules(t["text"])]
        assert sorted(got) == sorted(c["kind"] for c in t["clauses"]), t["id"]


def test_tool_oracle_sample():
    from eval.oracle import oracle_answer, _close
    rows = [json.loads(l) for l in open(config.DATA / "eval" / "questions_with_engine.jsonl")][:60]
    g = Graph()
    assert all(_close(oracle_answer(g, r), r["engine"]) for r in rows)


def test_prove_absence_credential_and_bond_run_closure_checks(g):
    r = call(g, "prove_absence", {"evidence": "valid_credential", "credential_kind": "PMP"})["result"]
    assert r["verdict"] == "PROVEN" and r["checks"]["credential_closure"].split("/")[0] == r["checks"]["credential_closure"].split("/")[1].split()[0]
    b = call(g, "prove_absence", {"evidence": "performance_bond"})["result"]
    assert b["verdict"] == "PROVEN" and b["checks"]["bond_closure"] == "60/60 bonds in scope secure a tender"
