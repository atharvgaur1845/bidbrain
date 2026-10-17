"""Graph depth: communities, evidence-integrity WCC, single-query multi-hop traversal, compliance audit.

Needs `bidbrain_base` loaded (`python -m bidbrain.ingest.load_graph`). Every test reads a GRAPH.COPY, never
the base graph, so a concurrent rebuild or another test cannot change what it sees.
"""
from __future__ import annotations

import json

import pytest

from bidbrain import config
from bidbrain.graph.client import Graph
from bidbrain.tools import TOOLS, call


@pytest.fixture(scope="module")
def gc():
    base = Graph()
    assert base.exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    s = base.copy_to("pytest_graph_depth")
    yield s
    s.delete()


def ok(out: dict) -> dict:
    assert "error" not in out, out.get("error")
    return out


# ------------------------------------------------------------------ communities


def test_people_communities_are_several_explained_blocks(gc):
    out = ok(call(gc, "communities", {}))
    rows, summ = out["result"], out["summary"]
    teams = [r for r in rows if r["size"] > 1]
    assert sum(r["size"] for r in rows) == 39                       # every person is placed
    assert len(teams) >= 3 and max(r["size"] for r in teams) <= 20   # not one block of 31 any more
    assert summ["modularity"] > 0.05
    for r in teams:   # each team is explained by clients most of its members served
        assert r["shared_clients"] and r["shared_clients"][0]["served_by"] >= 2
        assert "defined by" in r["basis"]
    # frontend contract: communityId, people, size
    assert {"communityId", "people", "size"} <= set(rows[0])


def test_client_communities(gc):
    out = ok(call(gc, "communities", {"of": "clients"}))
    teams = [r for r in out["result"] if r["size"] > 1]
    assert sum(r["size"] for r in out["result"]) == 29
    assert len(teams) >= 2 and all(r["shared_leads"] for r in teams)


def test_projection_edges_exist(gc):
    assert gc.scalar("MATCH ()-[r:OVERLAPS_CLIENTS]->() WHERE r.shared >= 2 AND r.jaccard >= 0.25 RETURN count(r)") > 0
    assert gc.scalar("MATCH ()-[r:OVERLAPS_CLIENTS]->() WHERE r.shared < 2 OR r.jaccard < 0.25 RETURN count(r)") == 0
    assert gc.scalar("MATCH ()-[r:SHARES_LEADS]->() WHERE r.leads < 2 RETURN count(r)") == 0


# ------------------------------------------------------------------ WCC evidence integrity


def test_provenance_wcc_finds_the_real_gaps(gc):
    r = ok(call(gc, "isolated_entities", {}))["result"]
    assert r["scope"] == "provenance" and r["main_component_size"] > 700
    f = {(x["finding"], x["label"]): x for x in r["findings"]}
    assert set(f[("credentialed_no_work", "Person")]["examples"]) == {"Asha Bose", "Manoj Kapoor"}
    assert f[("client_without_work", "Client")]["count"] == 1
    assert not any(x["finding"] == "uncited_document" for x in r["findings"])   # every reference letter is linked


def test_corpus_wcc_every_entity_has_a_source(gc):
    r = ok(call(gc, "isolated_entities", {"scope": "corpus"}))["result"]
    kinds = {(x["finding"], x["label"], x["doc_type"]): x["count"] for x in r["findings"]}
    # the workbook rows (assets, TB lines, BOQ) are linked to their spreadsheets at load
    assert not any(k[0] == "unprovenanced" for k in kinds), kinds
    assert kinds == {("uncited_document", "Document", "bank_statement"): 8,
                     ("uncited_document", "Document", "ra_bill"): 6}
    assert gc.scalar("MATCH (a:Asset) WHERE NOT (a)-[:EVIDENCED_BY]->(:Document) RETURN count(a)") == 0


# ------------------------------------------------------------------ one-query multi-hop


def test_trace_from_credential_is_one_query_and_matches_find_portfolio(gc):
    out = ok(call(gc, "trace_from_credential", {"person": "Sunita Joshi", "credential": "Six Sigma", "work": "Pkg-107"}))
    assert len(out["cypher"]) == 1
    ch = out["result"]["chains"]
    assert len(ch) == 1 and ch[0]["hops"] == 5
    assert [n["label"] for n in ch[0]["chain"]] == ["Credential", "Person", "Work", "Client"]
    assert ch[0]["held_when_anchor_completed"] is False      # Six Sigma 2023, Pkg-107 completed 2012
    pf = ok(call(gc, "find_portfolio", {"via_work": "Pkg-107"}))
    assert len(pf["cypher"]) == 1                             # via_work is a single query too
    assert ch[0]["total_value"] == pf["result"]["total_value"] and ch[0]["n_works"] == pf["result"]["n"]
    assert {w["key"] for w in ch[0]["works"]} == {w["key"] for w in pf["result"]["works"]}
    assert all(w["doc"] and w["page"] for w in ch[0]["works"])


def test_trace_explains_an_empty_chain(gc):
    r = ok(call(gc, "trace_from_credential", {"person": "Sunita Joshi", "credential": "Six Sigma", "work": "Pkg-29"}))
    assert r["result"]["chains"] == [] and "led by" in r["result"]["why_empty"][0]


def test_find_portfolio_via_work_with_empty_filter_keeps_scope(gc):
    out = ok(call(gc, "find_portfolio", {"via_work": "Pkg-107", "min_value": 10**13}))
    assert out["result"]["n"] == 0 and out["scope"]["client"]


# ------------------------------------------------------------------ compliance audit


def test_compliance_check_audits_every_met_row(gc):
    r = ok(call(gc, "compliance_check", {}))["result"]
    s = r["summary"]
    assert s["requirements"] == 491 and s["claimed_met"] == 491
    assert s["checked"] + s["not_checkable"] == 491
    assert s["agree"] + s["disagree"] + s["unconfirmed"] == s["checked"] > 250
    # ISO certificates were first issued in 2022: a 2019-21 claim of ISO 9001 cannot have been true
    early_iso = [d for d in r["disagreements"] if d["kind"] == "certification"]
    assert early_iso and all(d["bid_date"] < "2023-01-01" for d in early_iso)
    assert r["by_kind"]["years_in_business"]["disagree"] == 0


def test_compliance_check_one_tender(gc):
    r = ok(call(gc, "compliance_check", {"tender": "RFP-132000970"}))["result"]
    reqs = r["requirements"]
    assert len(reqs) == 17 and {x["bid_date"] for x in reqs} == {"2020-02-09"}
    pmp = next(x for x in reqs if x["kind"] == "key_personnel")
    assert pmp["outcome"] == "disagree"                       # every PMP on file was issued 2021-03-10
    assert any(x["outcome"] == "not_checkable" for x in reqs)


def test_new_tools_are_read_only_and_registered():
    for n in ("trace_from_credential", "compliance_check", "communities", "isolated_entities"):
        assert n in TOOLS and not TOOLS[n].writes


# ------------------------------------------------------------------ the demo tenders are unchanged


def test_demo_tender_verdicts_unchanged(gc):
    from bidbrain.agents.orchestrator import qualify_tender
    want = {"NHX": "BID", "PHED": "BID WITH FIXES", "JN-LB": "NO-BID", "CWBB": "NO-BID"}
    tenders = json.load(open(config.DATA / "tenders" / "demo_tenders.json"))
    got = {}
    for t in tenders:
        tag = next((k for k in want if k in t["id"]), None)
        if not tag:
            continue
        s = gc.copy_to("pytest_graph_depth_case")
        try:
            got[tag] = qualify_tender(s, t, prefer_llm=False)["case"]["verdict"]
        finally:
            s.delete()
    assert got == want
