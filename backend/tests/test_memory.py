"""Company memory (opt-in promotion to a shared graph) and conversation recall.

Runs on throwaway graphs only: a per-test company graph (never `bidbrain_org`) and session copies
that are deleted afterwards. No LLM calls.

    cd backend && pytest -q tests/test_memory.py
"""
from __future__ import annotations

import json
import secrets

import pytest
from fastapi.testclient import TestClient

from bidbrain.agents import providers
from bidbrain.api import main, sessions
from bidbrain.conflicts import engine
from bidbrain.graph.client import Graph, db


@pytest.fixture()
def org(monkeypatch):
    """A private company graph for this test; the real `bidbrain_org` is never touched."""
    assert Graph().exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    name = f"pytest_org_{secrets.token_hex(4)}"
    monkeypatch.setattr(engine, "ORG_GRAPH", name)

    def no_llm(*a, **k):
        raise AssertionError("no LLM calls in these tests")
    monkeypatch.setattr(providers, "chat", no_llm)
    yield name
    Graph(name).delete()


@pytest.fixture()
def client(org, monkeypatch):
    monkeypatch.setattr(sessions, "MAX_SESSIONS", 10_000)     # never evict someone else's session
    made: list[str] = []
    c = TestClient(main.app)

    def new_session() -> dict:
        sid = c.post("/api/session").json()["session_id"]
        made.append(sid)
        return {"X-Session": sid}
    c.new_session = new_session
    yield c
    for sid in made:
        Graph(f"session_{sid}").delete()
        sessions._last_seen.pop(f"session_{sid}", None)


def _events(resp) -> list[dict]:
    assert resp.status_code == 200, resp.text
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def _result(resp) -> dict:
    evs = _events(resp)
    errs = [e for e in evs if e.get("type") == "error"]
    assert not errs, errs
    return next(e["data"] for e in evs if e.get("type") == "result")


# ------------------------------------------------------------------ policy order

def _claim(i, kind, value, authority, issuer, day=20000):
    return {"id": i, "source_kind": kind, "value": value, "authority": authority, "issuer": issuer,
            "observed_day": day, "precision": None, "source_doc": f"DOC-{i}"}


def test_org_rules_come_after_session_rules_and_before_recency_and_authority():
    a = _claim("a", "client_certificate", "Prime", 90, "Client A")
    b = _claim("b", "partner_certificate", "JV Partner", 80, "Partner B")
    cf = {"predicate": "role", "claims": [a, b]}
    org_rule = {"id": "ORG-0123456789", "predicate": "role", "prefer_kind": "partner_certificate",
                "over_kind": "client_certificate", "rationale": "JV lead's certificate is authoritative",
                "promoted_by": "alice", "source_decision": "DEC-x"}
    assert engine.propose(cf, [])["policy"] == "needs_human"
    p = engine.propose(cf, [], [org_rule])
    assert p["policy"] == "org_rule" and p["winner"]["id"] == "b" and "ORG-0123456789" in p["rationale"]
    # a session's own rule (here the opposite preference) is consulted first
    mine = {"id": "RULE-1", "predicate": "role", "prefer_kind": "client_certificate",
            "over_kind": "partner_certificate", "rationale": "ours"}
    p = engine.propose(cf, [mine], [org_rule])
    assert p["policy"] == "learned_rule" and p["winner"]["id"] == "a"
    # an org rule beats authority: a 20+ point gap would otherwise decide for the certificate
    a2 = {**a, "authority": 100}
    assert engine.propose({"predicate": "role", "claims": [a2, b]}, [])["policy"] == "authority"
    assert engine.propose({"predicate": "role", "claims": [a2, b]}, [], [org_rule])["policy"] == "org_rule"
    # and recency: the same issuer amending itself would otherwise win
    c1 = _claim("c1", "client_certificate", "Prime", 90, "X", 20000)
    c2 = _claim("c2", "revised_certificate", "JV Partner", 95, "X", 20100)
    rule = {**org_rule, "prefer_kind": "client_certificate", "over_kind": "revised_certificate"}
    assert engine.propose({"predicate": "role", "claims": [c1, c2]}, [])["policy"] == "recency_same_issuer"
    assert engine.propose({"predicate": "role", "claims": [c1, c2]}, [], [rule])["policy"] == "org_rule"


# ------------------------------------------------------------------ the demo chain, end to end

def test_demo_chain_promote_then_new_session_auto_resolves_with_org_rule(client, org):
    h1 = client.new_session()
    case = _result(client.post("/api/qualify", json={"tender_id": "T-NHX-2026-014", "use_llm": False}, headers=h1))
    case = case["case"]
    assert case["verdict"] == "BID"
    _result(client.post("/api/events/DEMO-0/fire", headers=h1))
    _result(client.post("/api/events/DEMO-1/fire", headers=h1))
    cases = client.get("/api/cases", headers=h1).json()
    assert next(c for c in cases if c["id"] == case["id"])["verdict"] == "NO-BID"

    out = _result(client.post("/api/events/DEMO-3/fire", headers=h1))
    assert out["cards"] and not out["decisions"]
    card = client.get("/api/conflicts", headers=h1).json()["cards"][0]
    partner = next(c for c in card["claims"] if c["source_kind"] == "partner_certificate")
    res = client.post(f"/api/conflicts/{card['id']}/resolve", headers=h1,
                      json={"winner_claim": partner["id"], "apply_to_similar": True, "user": "judge-1",
                            "rationale": "the JV lead's certificate is the record of our role"})
    assert res.status_code == 200, res.text
    rule_id = res.json()["decision"]["learned_rule"]
    assert rule_id

    # nothing reaches company memory until someone promotes it
    assert client.get("/api/memory/org").json()["rules"] == []
    assert org not in db().list_graphs()

    pr = client.post("/api/memory/promote", json={"rule_id": rule_id, "user": "judge-1"}, headers=h1)
    assert pr.status_code == 200, pr.text
    oid = pr.json()["org_rule"]
    listed = client.get("/api/memory/org").json()
    assert listed["active"] == 1
    rule = listed["rules"][0]
    assert rule["id"] == oid and rule["promoted_by"] == "judge-1" and rule["source_rule"] == rule_id
    assert rule["source_session"].startswith("s-") and h1["X-Session"] not in json.dumps(listed)
    learned = rule["learned_from"]
    assert learned["id"] == res.json()["decision"]["decision_id"]
    assert learned["rationale"] == "the JV lead's certificate is the record of our role"
    assert learned["decided_by"] == "judge-1" and rule["promoted_at"]
    # promoting the same rule again is idempotent; the session's rule list shows it as company memory
    again = client.post("/api/memory/promote", json={"rule_id": rule_id}, headers=h1).json()
    assert again == {"org_rule": oid, "already": True, "rule": again["rule"]}
    assert client.get("/api/conflicts", headers=h1).json()["rules"][0]["org_rule"] == oid

    # a different visitor: a fresh session never saw DEMO-3, yet DEMO-4 resolves by the company rule
    h2 = client.new_session()
    assert client.get("/api/conflicts", headers=h2).json()["rules"] == []
    out = _result(client.post("/api/events/DEMO-4/fire", headers=h2))
    assert not out["cards"] and len(out["decisions"]) == 1
    d = out["decisions"][0]
    assert d["policy"] == "org_rule" and oid in d["rationale"]
    trace = client.get(f"/api/decisions/{d['decision_id']}/trace", headers=h2).json()
    assert trace["org_rule"]["id"] == oid and trace["org_rule"]["source_decision"] == learned["id"]
    assert client.get("/api/conflicts", headers=h2).json()["decisions"][0]["org_rule"] == oid

    # retiring makes the demo resettable: the next fresh session gets a card again
    assert client.post(f"/api/memory/org/{oid}/retire", json={"user": "judge-1"}).json()["retired"] is True
    assert client.get("/api/memory/org").json()["rules"] == []
    assert client.get("/api/memory/org?include_retired=true").json()["rules"][0]["retired_by"] == "judge-1"
    h3 = client.new_session()
    out = _result(client.post("/api/events/DEMO-4/fire", headers=h3))
    assert out["cards"] and not out["decisions"]


def test_promote_and_retire_validate_input_and_cap(client, org, monkeypatch):
    h = client.new_session()
    assert client.post("/api/memory/promote", json={"rule_id": "x' OR 1=1"}, headers=h).status_code == 400
    assert client.post("/api/memory/promote", json={}, headers=h).status_code == 400
    assert client.post("/api/memory/promote", json={"rule_id": "RULE-0000000000"}, headers=h).status_code == 404
    assert client.post("/api/memory/promote", json={"rule_id": "RULE-0000000000"}).status_code == 400   # no session
    assert client.post("/api/memory/org/nope/retire", json={}).status_code == 400
    assert client.post("/api/memory/org/ORG-0000000000/retire", json={}).status_code == 404

    g = sessions.graph_for(h["X-Session"])
    for i, (pk, ok) in enumerate([("partner_certificate", "client_certificate"),
                                  ("client_certificate", "partner_certificate"),
                                  ("hr_record", "cv")]):
        g.run("""CREATE (:PolicyRule {id:$id, key:$id, predicate:'role', prefer_kind:$pk, over_kind:$ok,
                                     rationale:'t', created_by:'t', created_at:timestamp(), retired:false})""",
              {"id": f"RULE-00000000{i:02d}", "pk": pk, "ok": ok})
    assert client.post("/api/memory/promote", json={"rule_id": "RULE-0000000000", "user": "<b>x</b>"},
                       headers=h).status_code == 200
    assert client.get("/api/memory/org").json()["rules"][0]["promoted_by"] == "bxb"    # markup stripped
    # the opposite preference cannot sit beside it in company memory
    r = client.post("/api/memory/promote", json={"rule_id": "RULE-0000000001"}, headers=h)
    assert r.status_code == 409 and "retire it first" in r.json()["detail"]
    monkeypatch.setattr(engine, "ORG_MAX_ACTIVE", 1)
    r = client.post("/api/memory/promote", json={"rule_id": "RULE-0000000002"}, headers=h)
    assert r.status_code == 409 and "full" in r.json()["detail"]


def test_org_rules_never_apply_outside_sessions(org):
    """Building the base graph and the evals run engine.run on non-session graphs: hermetic."""
    base = Graph()
    s = base.copy_to(f"pytest_hermetic_{secrets.token_hex(3)}")
    try:
        org_g = Graph(org)
        org_g.run("""CREATE (:OrgRule {id:'ORG-0000000000', key:'ORG-0000000000', predicate:'role',
                                       prefer_kind:'partner_certificate', over_kind:'client_certificate',
                                       rationale:'t', retired:false, promoted_at:timestamp()})""")
        demo = {e["event_id"]: e for e in main._all_events()}["DEMO-4"]
        from bidbrain.conflicts.events import apply_event
        out = apply_event(s, json.loads(json.dumps(demo)))
        assert out["cards"] and not out["decisions"]
    finally:
        s.delete()
