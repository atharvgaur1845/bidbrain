"""The whole company loop, end to end through the API, with no LLM:

new document -> claim -> conflict adjudication -> graph impact -> deterministic requalification ->
accountable actions (approval gate, execution, audit) -> company record -> a clean second workspace starts from
the new fact (policy company_record, citing its origin) without seeing the first workspace's private state.

Throwaway graphs only (a per-test company graph; session copies deleted afterwards).
"""
from __future__ import annotations

import pytest

from bidbrain.graph.client import Graph
from tests.test_memory import _result, client, org  # noqa: F401  (fixtures)

PKG113 = "ringroaduttarpradeshpkg113"


def _act(client, h, aid, verb, **body):
    r = client.post(f"/api/actions/{aid}/{verb}", json=body or {}, headers=h)
    return r


def test_corrigendum_to_action_to_company_record(client):
    h1 = client.new_session()
    case = _result(client.post("/api/qualify", json={"tender_id": "T-NHX-2026-014", "use_llm": False}, headers=h1))["case"]
    assert case["verdict"] == "BID"
    g1 = Graph(f"session_{h1['X-Session']}")
    before = g1.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113})

    _result(client.post("/api/events/DEMO-0/fire", headers=h1))
    out = _result(client.post("/api/events/DEMO-1/fire", headers=h1))       # the corrigendum
    after = g1.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113})
    assert after < before                                                     # the fact changed (by policy)

    # graph impact: the corrigendum's claim -> decision -> flipped clause on the open bid -> review action
    doc = out["doc"]["doc_id"]
    imp = client.get(f"/api/impact/{doc}", headers=h1).json()["result"]
    assert imp["decisions"] and any(a["case_id"] == case["id"] and a["verdict_now"] == "FAIL" for a in imp["affected"])
    review = next(a for a in imp["actions"] if a["kind"] == "review_bid")
    publish = next(a for a in imp["actions"] if a["kind"] == "publish_to_company_record")
    assert g1.scalar("MATCH (c:Case {id:$c}) RETURN c.verdict", {"c": case["id"]}) == "NO-BID"

    # approval gate: nothing executes before approval; transitions are audited and idempotent
    assert _act(client, h1, review["id"], "execute").status_code == 409
    assert _act(client, h1, review["id"], "approve", user="bid manager").json()["status"] == "approved"
    done = _act(client, h1, review["id"], "execute").json()
    assert done["status"] == "executed" and done["result"]["recommendation"] == "NO-BID"
    again = _act(client, h1, review["id"], "execute").json()
    assert again["result"] == done["result"] and len(again["events"]) == len(done["events"])
    assert g1.scalar("MATCH (c:Case {id:$c}) RETURN c.approved_recommendation", {"c": case["id"]}) == "NO-BID"

    # a rejected action can never run
    _act(client, h1, publish["id"], "approve")
    pub = _act(client, h1, publish["id"], "execute").json()
    assert pub["status"] == "executed" and pub["result"]["org_fact"].startswith("OF-")
    of = pub["result"]["org_fact"]

    # a clean second workspace starts from the published fact, decided by policy company_record
    h2 = client.new_session()
    g2 = Graph(f"session_{h2['X-Session']}")
    client.get("/api/health", headers=h2)
    assert g2.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113}) == after
    dec = g2.read("""MATCH (d:Decision {subject_key:$k, policy:'company_record'}) RETURN d.rationale AS why""",
                  {"k": PKG113})
    assert dec and of in dec[0]["why"]
    rec = client.get("/api/company/record", headers=h2).json()["facts"]
    assert any(f["id"] == of and f["applied_here"] for f in rec)
    # ... and sees nothing private from the first workspace
    assert g2.scalar("MATCH (c:Case) RETURN count(c)") == 0
    assert g2.scalar("MATCH (a:Action) RETURN count(a)") == 0
    assert g2.scalar("MATCH (t:Turn) RETURN count(t)") == 0

    # point in time: before the corrigendum was recorded, the company believed the certificate's value
    hist = client.post("/api/tool/fact_as_of", headers=h1,
                       json={"key": PKG113, "predicate": "value", "as_of": "2000-01-01"}).json()
    assert hist["result"]["learned_later"]


def test_reject_and_stale_transitions(client):
    h = client.new_session()
    case = _result(client.post("/api/qualify", json={"tender_id": "T-PHED-WTP-2027-03", "use_llm": False}, headers=h))["case"]
    acts = client.get(f"/api/actions?case_id={case['id']}", headers=h).json()["actions"]
    assert acts and all(a["status"] == "pending" for a in acts)
    a = acts[0]["id"]
    assert _act(client, h, a, "reject", note="not needed").json()["status"] == "rejected"
    assert _act(client, h, a, "approve").status_code == 409
    assert _act(client, h, a, "execute").status_code == 409
    assert _act(client, h, "ACT-nope", "approve").status_code == 404
    assert _act(client, h, a, "launch").status_code == 400


def test_published_fact_can_only_be_retired_by_its_publisher(client):
    h1 = client.new_session()
    _result(client.post("/api/events/DEMO-1/fire", headers=h1))
    pub = next(a for a in client.get("/api/actions", headers=h1).json()["actions"]
               if a["kind"] == "publish_to_company_record")
    client.post(f"/api/actions/{pub['id']}/approve", json={}, headers=h1)
    of = client.post(f"/api/actions/{pub['id']}/execute", json={}, headers=h1).json()["result"]["org_fact"]
    h2 = client.new_session()
    assert client.post(f"/api/company/record/{of}/retire", json={}, headers=h2).status_code == 403
    assert client.post(f"/api/company/record/{of}/retire", json={"reason": "test"}, headers=h1).json()["retired"] == of
    h3 = client.new_session()
    client.get("/api/cases", headers=h3)
    g3 = Graph(f"session_{h3['X-Session']}")
    assert g3.scalar("MATCH (d:Decision {policy:'company_record'}) RETURN count(d)") == 0
