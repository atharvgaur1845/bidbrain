"""Temporal semantics and graph modelling after judging round 3.

`fact_as_of` uses two axes: valid time (the claim's document existed by T) and transaction time (the graph had
recorded the claim by T, no decision had set it aside by T, decisions count from their own time). The corpus is the
starting record: its claims, its own decisions and their set-aside stamps are at 0, so a rebuild never changes history.

Needs `bidbrain_base` loaded (`python -m bidbrain.ingest.load_graph`). Writes go to GRAPH.COPY scratch graphs only.
"""
from __future__ import annotations

import json
import time

import pytest

from bidbrain import config
from bidbrain.graph.client import Graph, day
from bidbrain.tools import call
from bidbrain.tools.impact import _ms, fact_as_of

PKG116 = "sixlanehighwaywestbengalpkg116"


def _copy(name: str) -> Graph:
    base = Graph()
    assert base.exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    for _ in range(60):          # FalkorDB forks for GRAPH.COPY and Redis allows one child at a time
        try:
            return base.copy_to(name)
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return base.copy_to(name)


@pytest.fixture(scope="module")
def gc():
    s = _copy("pytest_temporal_r4")
    yield s
    s.delete()


def _r(g, key, pred, as_of):
    return fact_as_of(g, key, pred, as_of)["result"]


# ------------------------------------------------------------------ valid time


def test_accounts_that_did_not_exist_yet_are_not_believed(gc):
    early = _r(gc, "FS-2024-25", "revenue_operations", "2021-01-01")
    assert early["value"] is None and early["alive_then"] == []
    assert {r["source_kind"] for r in early["not_yet_issued"]} == {"financial_statement", "comparative_column"}
    assert all(r["valid_from"] == "2026-03-31" and r["valid_from_basis"].startswith("document date")
               for r in early["not_yet_issued"])
    later = _r(gc, "FS-2024-25", "revenue_operations", "2026-04-01")
    assert later["value"] == "2,389,200,000" and later["claim"]["source_kind"] == "financial_statement"
    assert later["decision_then"]["policy"] == "authority" and later["decision_then"]["at"] == 0


def test_a_certificate_counts_from_the_completion_date_it_prints(gc):
    # the certificate (printed 2026-03-31) states the completion date 2020-02-11: the fact holds from then
    r = _r(gc, PKG116, "completed", "2020-02-10")
    assert r["value"] is None
    assert {x["valid_from"] for x in r["not_yet_issued"]} == {"2020-02-11"}
    assert all(x["document_date"] == "2026-03-31" for x in r["not_yet_issued"])
    assert _r(gc, PKG116, "completed", "2020-02-11")["value"] == "2020-02-11"   # inclusive on the day


def test_both_axes_are_reported_per_claim(gc):
    r = _r(gc, PKG116, "value", "2026-04-01")
    assert "Valid time" in r["valid_basis"] and "Transaction time" in r["valid_basis"]
    for c in r["alive_then"]:
        assert c["recorded_at"] == 0 and c["recorded"] == "corpus (starting record)"
        assert c["valid_from"] and c["valid_from_basis"]
    # the portfolio prints each work's completion date with it: dated by that, not by the snapshot
    pf = next(c for c in r["alive_then"] if c["source_kind"] == "portfolio")
    assert pf["valid_from"] == "2020-02-11" and pf["valid_from_basis"].startswith("effective date")
    assert r["undated_kinds"] == [] and r["note"] is None


def test_undated_cv_claims_are_flagged(gc):
    key = gc.scalar("MATCH (c:Claim {source_kind:'cv'}) RETURN c.subject_key ORDER BY c.subject_key LIMIT 1")
    r = _r(gc, key, "experience_years", "2026-06-01")
    assert r["value"] is not None and r["undated_kinds"] == ["cv"]
    assert _r(gc, key, "experience_years", "2026-03-30")["value"] is None


# ------------------------------------------------------------------ transaction time and decisions


def test_decisions_count_from_their_own_time(gc):
    key, pred = "FS-2023-24", "revenue_operations"
    claims = gc.read("""MATCH (c:Claim {subject_key:$k, predicate:$p}) RETURN c.id AS id, c.source_kind AS kind,
                        c.value_str AS v""", {"k": key, "p": pred})
    fs = next(c for c in claims if c["kind"] == "financial_statement")
    comp = next(c for c in claims if c["kind"] == "comparative_column")
    t1 = _ms("2026-07-01T00:00:00+00:00")
    # a later (hypothetical) decision reverses the corpus one at t1
    gc.run("""CREATE (d:Decision {id:'DEC-TEST-R4', key:'DEC-TEST-R4', subject_key:$k, predicate:$p, policy:'human',
                                  at:$t, chosen_value:$v})
              WITH d MATCH (w:Claim {id:$w}), (l:Claim {id:$l})
              CREATE (d)-[:SELECTS]->(w), (d)-[:BASED_ON]->(w), (d)-[:BASED_ON]->(l)
              SET w.invalidated_at = NULL, l.invalidated_at = $t, l.invalidated_by = 'DEC-TEST-R4'""",
           {"k": key, "p": pred, "t": t1, "v": comp["v"], "w": comp["id"], "l": fs["id"]})
    try:
        before = _r(gc, key, pred, "2026-06-30T23:59:59.999+00:00")
        after = _r(gc, key, pred, "2026-07-01T00:00:00.001+00:00")
        assert before["value"] == fs["v"] and before["decision_then"]["at"] == 0
        assert [d["decision"] for d in before["decisions_later"]] == ["DEC-TEST-R4"]
        assert after["value"] == comp["v"] and after["decision_then"]["decision"] == "DEC-TEST-R4"
        assert [c["claim"] for c in after["set_aside_by_then"]] == [fs["id"]]
    finally:
        gc.run("MATCH (d:Decision {id:'DEC-TEST-R4'}) DETACH DELETE d")
        gc.run("MATCH (c:Claim) WHERE c.id IN $ids SET c.invalidated_at = 0", {"ids": [comp["id"]]})
        gc.run("MATCH (c:Claim {id:$id}) SET c.invalidated_at = NULL, c.invalidated_by = NULL", {"id": fs["id"]})


def test_a_reactivated_claim_was_set_aside_only_during_its_interval(gc):
    c = gc.read("MATCH (c:Claim {source_kind:'comparative_column', predicate:'cash'}) RETURN c.id AS id, "
                "c.subject_key AS k LIMIT 1")[0]
    a, b = _ms("2026-05-01"), _ms("2026-06-01")
    gc.run("MATCH (c:Claim {id:$id}) SET c.status_history = [$h], c.invalidated_at = NULL",
           {"id": c["id"], "h": f"rejected:{a}-{b}:DEC-X"})
    try:
        ids = lambda as_of: [x["claim"] for x in _r(gc, c["k"], "cash", as_of)["alive_then"]]  # noqa: E731
        assert c["id"] in ids("2026-04-15") and c["id"] not in ids("2026-05-15") and c["id"] in ids("2026-06-15")
    finally:
        gc.run("MATCH (c:Claim {id:$id}) SET c.invalidated_at = 0 REMOVE c.status_history", {"id": c["id"]})


def test_the_corpus_is_the_starting_record():
    g = Graph()
    assert g.scalar("MATCH (c:Claim) WHERE c.recorded_at <> 0 AND c.company_record IS NULL "
                    "AND NOT (c)-[:FROM]->(:Document {live:true}) RETURN count(c)") == 0
    assert g.scalar("MATCH (d:Decision) WHERE d.at <> 0 RETURN count(d)") == 0
    assert g.scalar("MATCH (c:Claim) WHERE c.invalidated_at IS NOT NULL AND c.invalidated_at <> 0 RETURN count(c)") == 0
    snap = g.read("MATCH (m:Meta) RETURN m.corpus_snapshot AS s, m.corpus_snapshot_day AS d")[0]
    assert snap == {"s": "2026-03-31", "d": day("2026-03-31")}


def test_document_dates_are_the_printed_ones():
    g = Graph()
    rows = {r["t"]: r for r in g.read("""MATCH (d:Document) WHERE d.live IS NULL
        RETURN d.doc_type AS t, count(d) AS n, count(d.issued) AS dated, collect(DISTINCT d.issued_basis) AS basis""")}
    for t in ("completion_certificate", "company_completion_certificate", "reference_letter", "performance_bond",
              "personnel_certificate", "financial_statement", "final_ra_bill", "ra_bill", "tender_dossier",
              "compliance_matrix"):
        assert rows[t]["dated"] == rows[t]["n"] and rows[t]["basis"] == ["printed"], t
    for t in ("cv", "past_performance_portfolio", "iso_certificate", "general_ledger_book", "bank_statement"):
        assert rows[t]["dated"] == 0, t
    # a bond's printed issue date is the date its claims were observed with
    assert g.scalar("""MATCH (c:Claim {source_kind:'bank_instrument'})-[:FROM]->(d:Document)
                       WHERE c.observed_day <> d.issued_day RETURN count(c)""") == 0


# ------------------------------------------------------------------ modelling


def test_claim_and_decision_edges_are_not_overloaded():
    g = Graph()
    assert g.scalar("MATCH (:Claim)-[r:PREVAILS_OVER]->(:Claim) RETURN count(r)") == 114
    assert g.scalar("MATCH (:Claim)-[r:SUPERSEDES]->() RETURN count(r)") == 0
    assert g.scalar("MATCH ()-[r:SUPERSEDES]->() WHERE NOT (startNode(r):Decision AND endNode(r):Decision) "
                    "RETURN count(r)") == 0
    assert g.scalar("MATCH (a:Claim)-[r:PREVAILS_OVER]->(b:Claim) WHERE a.status <> 'active' OR "
                    "b.status = 'active' RETURN count(r)") == 0


def test_action_indexes_exist_and_are_copied(gc):
    idx = {(r["label"], p) for r in gc.read("CALL db.indexes() YIELD label, properties RETURN label, properties")
           for p in r["properties"]}
    assert {("Action", "id"), ("Action", "idem")} <= idx
    assert "Node By Index Scan" in str(gc.g.explain("MATCH (a:Action {idem:'x'}) RETURN a"))


def test_key_people_lists_client_names(gc):
    rows = call(gc, "key_people", {"limit": 5})["result"]["people"]
    for r in rows:
        assert len(r["client_names"]) == r["clients"] and all(isinstance(n, str) for n in r["client_names"])


def test_corpus_wcc_reaches_live_documents_and_qualified_tenders():
    from bidbrain.agents.orchestrator import qualify_tender
    from bidbrain.conflicts.events import apply_event
    s = _copy("pytest_temporal_r4_wcc")
    try:
        ev = next(e for e in json.loads((config.DATA / "conflicts" / "demo_events.json").read_text())
                  if e["event_id"] == "DEMO-1")
        apply_event(s, ev)
        t = {x["id"]: x for x in json.load(open(config.DATA / "tenders" / "demo_tenders.json"))}["T-NHX-2026-014"]
        qualify_tender(s, t, prefer_llm=False)
        r = call(s, "isolated_entities", {"scope": "corpus"})["result"]
        flagged = {i["key"] for i in r["isolated"]}
        assert ev["doc"]["doc_id"] not in flagged
        tender = s.scalar("MATCH (:Case)-[:FOR_TENDER]->(t:Tender) RETURN t.key")
        assert tender and tender not in flagged
        assert not any(f["label"] in ("Claim", "Case", "Clause") for f in r["findings"])   # connectors, not entities
    finally:
        s.delete()
