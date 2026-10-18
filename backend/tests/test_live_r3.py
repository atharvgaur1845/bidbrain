"""Round-3 live-document fixes: a live reference letter updates the absence proof and is searchable;
projections follow a corrected value; company memory survives a FalkorDB restart via its JSON mirror.

Session copies and a per-test company graph only (never `bidbrain_base` or `bidbrain_org`). No LLM.

    cd backend && pytest -q tests/test_live_r3.py
"""
from __future__ import annotations

import json
import secrets

import pytest
from fastapi.testclient import TestClient

from bidbrain.agents import providers
from bidbrain.api import main, sessions
from bidbrain.conflicts import engine
from bidbrain.graph.client import Graph
from bidbrain.tools import call

TRISHAKTI = "Trishakti Power Generation Corporation"
PKG43 = "flyoveruttarpradeshpkg43"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    assert Graph().exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    monkeypatch.setattr(engine, "ORG_GRAPH", f"pytest_org_{secrets.token_hex(4)}")
    monkeypatch.setattr(engine, "ORG_EXPORT_PATH", str(tmp_path / "org_memory.json"))
    monkeypatch.setattr(sessions, "MAX_SESSIONS", 10_000)
    monkeypatch.setattr(sessions, "SESSION_RATE", 10_000)

    def no_llm(*a, **k):
        raise AssertionError("no LLM calls in these tests")
    monkeypatch.setattr(providers, "chat", no_llm)
    from tests.test_memory import bearer, set_test_principals
    set_test_principals(monkeypatch)
    made: list[str] = []
    c = TestClient(main.app)

    def new_session() -> tuple[dict, Graph]:
        sid = c.post("/api/session").json()["session_id"]
        made.append(sid)
        return {"X-Session": sid, **bearer("manager")}, Graph(f"session_{sid}")
    c.new_session = new_session
    yield c
    for sid in made:
        Graph(f"session_{sid}").delete()
        sessions._last_seen.pop(f"session_{sid}", None)
    Graph(engine.ORG_GRAPH).delete()


def _result(resp) -> dict:
    assert resp.status_code == 200, resp.text
    evs = [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]
    assert not [e for e in evs if e.get("type") == "error"], evs
    return next(e["data"] for e in evs if e.get("type") == "result")


def _proof(g: Graph) -> dict:
    return call(g, "prove_absence", {"evidence": "reference_letter", "client": TRISHAKTI})["result"]


def test_live_reference_letter_updates_the_absence_proof_and_is_searchable(client):
    h, g = client.new_session()
    before = _proof(g)
    assert before["verdict"] == "PROVEN" and PKG43 in {a["key"] for a in before["absent"]}
    letters = int(before["checks"]["corpus_closure"].split("/")[1].split()[0])

    # EV-0152: Trishakti's letter for Flyover Pkg-43 (its grading also disagrees: a card)
    out = _result(client.post("/api/events/EV-0152/fire", headers=h))
    assert out["references"] == [PKG43] and out["chunks"] >= 1
    doc = out["claims"] and g.scalar("MATCH (c:Claim {id:$id})-[:FROM]->(d:Document) RETURN d.doc_id",
                                     {"id": out["claims"][0]})
    row = g.read("""MATCH (w:Work {key:$k})-[h:HAS_REFERENCE]->(d:Document {doc_id:$d})
                    RETURN w.has_reference AS has, w.views AS views, h.page AS page""", {"k": PKG43, "d": doc})
    assert row and row[0]["has"] is True and row[0]["page"] == 1
    assert row[0]["views"] == ["cc", "ccc", "pf"]          # existence attestations are not touched
    after = _proof(g)
    assert PKG43 not in {a["key"] for a in after["absent"]} and after["absent_count"] == before["absent_count"] - 1
    assert after["verdict"] == "PROVEN"
    assert after["checks"]["corpus_closure"] == f"{letters + 1}/{letters + 1} letters linked"
    # the letter's text is chunked, embedded and indexed like a corpus page
    chunks = g.read("MATCH (c:Chunk {doc_id:$d})-[:PART_OF]->(:Document {doc_id:$d}) RETURN c.text AS t, "
                    "c.embedding IS NOT NULL AS emb", {"d": doc})
    assert chunks and all(c["emb"] for c in chunks) and "Trishakti" in " ".join(c["t"] for c in chunks)
    hits = g.read("CALL db.idx.fulltext.queryNodes('Chunk', 'Trishakti Flyover') YIELD node "
                  "RETURN collect(DISTINCT node.doc_id) AS d")[0]["d"]
    assert doc in hits
    vec = call(g, "vector_search", {"text": "Trishakti letter of recommendation flyover Pkg-43", "k": 5,
                                    "doc_type": "reference_letter"})
    assert doc in json.dumps(vec)

    # EV-0151: an unrelated client's letter keeps the Trishakti proof PROVEN (it is linked to its own work)
    out = _result(client.post("/api/events/EV-0151/fire", headers=h))
    assert out["references"] == ["pumpingstationrajasthanpkg100"]
    last = _proof(g)
    assert last["verdict"] == "PROVEN" and last["checks"]["corpus_closure"] == f"{letters + 2}/{letters + 2} letters linked"
    # never written: the base graph still has Pkg-43 without a letter
    assert Graph().scalar("MATCH (w:Work {key:$k}) RETURN w.has_reference", {"k": PKG43}) is False


def test_uploaded_letter_pdf_is_a_linked_reference_letter(client):
    from bidbrain.conflicts.ingest_doc import ingest_pdf
    pdf = main.PDF_DIR / "DOC-EV-0152.pdf"
    if not pdf.exists():
        pytest.skip("demo PDFs not rendered (python -m bidbrain.conflicts.demo)")
    _, g = client.new_session()
    out = ingest_pdf(g, pdf, doc_id="DOC-UP-pytest00001")
    assert out["kind"] == "client_letter" and out["references"] == [PKG43] and out["chunks"] >= 1
    assert g.scalar("MATCH (d:Document {doc_id:'DOC-UP-pytest00001'}) RETURN d.doc_type") == "reference_letter"
    assert PKG43 not in {a["key"] for a in _proof(g)["absent"]} and _proof(g)["verdict"] == "PROVEN"


def _worked_for_is_fresh(g: Graph) -> list[dict]:
    return g.read("""MATCH (p:Person)-[r:WORKED_FOR]->(c:Client)
                     OPTIONAL MATCH (p)<-[:LED_BY]-(w:Work)-[:FOR_CLIENT]->(c)
                     WITH p, c, r, sum(w.value) AS v, count(w) AS n
                     WHERE r.value <> v OR r.works <> n
                     RETURN p.key AS person, c.key AS client, r.value AS stale, v AS fresh""")


def test_projections_follow_a_corrected_work_value(client):
    h, g = client.new_session()
    key = "ringroaduttarpradeshpkg113"
    old = g.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": key})
    out = _result(client.post("/api/events/DEMO-1/fire", headers=h))       # corrigendum on Pkg-113's value
    assert out["decisions"] and g.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": key}) != old
    assert _worked_for_is_fresh(g) == []
    lead = g.read("""MATCH (w:Work {key:$k})-[:LED_BY]->(p)-[r:WORKED_FOR]->(c)<-[:FOR_CLIENT]-(w)
                     RETURN r.value AS v, r.works AS n""", {"k": key})
    assert lead and lead[0]["v"] >= g.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": key})
    # ENGAGED is the reverse projection and carries the same numbers
    assert g.scalar("""MATCH (p)-[a:WORKED_FOR]->(c), (c)-[b:ENGAGED]->(p) WHERE a.value <> b.value
                       RETURN count(*)""") == 0


def test_company_memory_survives_losing_the_graph_via_its_json_mirror(client):
    """What a restart without a persistent FalkorDB dir looks like: bidbrain_org is gone, the mirror is not."""
    h1, g1 = client.new_session()
    g1.run("""CREATE (:PolicyRule {id:'RULE-00000000bb', key:'RULE-00000000bb', predicate:'role',
                                   prefer_kind:'partner_certificate', over_kind:'portfolio',
                                   rationale:'JV lead certifies the role', created_by:'t', created_at:timestamp(),
                                   retired:false})""")
    oid = client.post("/api/memory/promote", json={"rule_id": "RULE-00000000bb"}, headers=h1).json()["org_rule"]
    Graph(engine.ORG_GRAPH).delete()
    with TestClient(main.app) as c:          # lifespan: import at startup, export at shutdown
        assert [r["id"] for r in c.get("/api/memory/org").json()["rules"]] == [oid]
    assert json.load(open(engine.ORG_EXPORT_PATH))["rules"][0]["rule"]["id"] == oid
    h2, g2 = client.new_session()
    client.post("/api/session/settings", json={"use_company_memory": True}, headers=h2)
    d = _result(client.post("/api/events/DEMO-4/fire", headers=h2))["decisions"]
    assert d and d[0]["policy"] == "org_rule" and oid in d[0]["rationale"]
