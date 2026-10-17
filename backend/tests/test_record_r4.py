"""Round-4 company record fixes: opt-in, four-eyes publish, record facts as one participant (newer evidence can
win), contest, apply / retire ripple to cases, transaction-time hygiene, orphaned company rules, rate-limit key.

Throwaway graphs only: session copies made through the API and a per-test company graph (never `bidbrain_base`
or `bidbrain_org`). No LLM.

    cd backend && pytest -q tests/test_record_r4.py
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from bidbrain.api import main, sessions
from bidbrain.conflicts import engine
from bidbrain.graph.client import Graph
from tests.test_memory import _result, client, org  # noqa: F401  (fixtures)

PKG113 = "ringroaduttarpradeshpkg113"
NHX = "T-NHX-2026-014"
NSPO = "National Special Projects Office"


def _g(h) -> Graph:
    return Graph(f"session_{h['X-Session']}")


def _tag(h) -> str:
    return engine.session_tag(f"session_{h['X-Session']}")


def _opt_in(client, h, on=True) -> dict:
    r = client.post("/api/session/settings", json={"use_company_memory": on}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _publish_action(client, h) -> dict:
    return next(a for a in client.get("/api/actions", headers=h).json()["actions"]
                if a["kind"] == "publish_to_company_record")


def _publish(client, h, approver=None) -> str:
    """Fire DEMO-1 in h (if not yet) and publish its decision with a second workspace's approval."""
    if not _g(h).scalar("MATCH (a:Action {kind:'publish_to_company_record'}) RETURN count(a)"):
        _result(client.post("/api/events/DEMO-1/fire", headers=h))
    aid = _publish_action(client, h)["id"]
    r = client.post(f"/api/actions/{aid}/approve", json={}, headers=h)
    assert r.status_code == 403 and "PR-" in r.json()["detail"], r.text
    approver = approver or client.new_session()
    req = next(x for x in client.get("/api/company/record", headers=approver).json()["requests"] if not x["mine"])
    assert client.post(f"/api/company/record/requests/{req['id']}/approve", json={"user": "reviewer"},
                       headers=approver).status_code == 200
    assert client.post(f"/api/actions/{aid}/approve", json={}, headers=h).json()["status"] == "approved"
    done = client.post(f"/api/actions/{aid}/execute", json={}, headers=h).json()
    assert done["status"] == "executed", done
    return done["result"]["org_fact"]


def _value(g: Graph):
    return g.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113})


def _verdict(client, h, case_id):
    return next(c for c in client.get("/api/cases", headers=h).json() if c["id"] == case_id)["verdict"]


def _event(value, kind, issuer, observed, tag):
    return {"event_id": f"T-{tag}", "type": "demo",
            "doc": {"doc_id": f"DOC-T-{tag}", "doc_type": "corrigendum", "issuer": issuer, "issued": observed,
                    "title": f"Test {tag}"},
            "claims": [{"subject_label": "Work", "subject_key": PKG113, "predicate": "value", "value": value,
                        "source_kind": kind, "issuer": issuer, "observed": observed, "page": 1}]}


# ------------------------------------------------------------------ opt-in: a fresh workspace reproduces the demo

def test_fresh_session_reproduces_the_demo_after_another_published(client, org):
    h1 = client.new_session()
    of = _publish(client, h1)
    base = Graph().scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113})
    assert _value(_g(h1)) < base
    # a fresh visitor: nothing applied, the scripted chain runs exactly as in DEMO_SCRIPT
    h3 = client.new_session()
    g3 = _g(h3)
    assert client.get("/api/session/settings", headers=h3).json()["use_company_memory"] is False
    case = _result(client.post("/api/qualify", json={"tender_id": NHX, "use_llm": False}, headers=h3))["case"]
    assert case["verdict"] == "BID" and _value(g3) == base
    _result(client.post("/api/events/DEMO-0/fire", headers=h3))
    assert _verdict(client, h3, case["id"]) == "BID"
    out = _result(client.post("/api/events/DEMO-1/fire", headers=h3))
    assert [d["policy"] for d in out["decisions"]] == ["recency_same_issuer"]
    assert _verdict(client, h3, case["id"]) == "NO-BID"
    assert g3.scalar("MATCH (r:CompanyRecordRef) RETURN count(r)") == 0
    assert g3.scalar("MATCH (d:Decision {policy:'company_record'}) RETURN count(d)") == 0
    rec = client.get("/api/company/record", headers=h3).json()
    fact = next(f for f in rec["facts"] if f["id"] == of)
    assert fact["here"] == "not_applied" and fact["demo"] is True and rec["use_company_memory"] is False


# ------------------------------------------------------------------ four eyes

def test_publish_cannot_be_self_approved(client, org, monkeypatch):
    h1 = client.new_session()
    _result(client.post("/api/events/DEMO-1/fire", headers=h1))
    aid = _publish_action(client, h1)["id"]
    r = client.post(f"/api/actions/{aid}/approve", json={"user": "me"}, headers=h1)
    assert r.status_code == 403 and "second approver" in r.json()["detail"]
    assert client.get(f"/api/actions/{aid}", headers=h1).json()["status"] == "pending"
    assert client.post(f"/api/actions/{aid}/execute", json={}, headers=h1).status_code == 409
    mine = client.get("/api/company/record", headers=h1).json()["requests"]
    assert len(mine) == 1 and mine[0]["mine"] is True and mine[0]["status"] == "pending"
    rid = mine[0]["id"]
    # the requesting workspace cannot approve its own request; a retry does not file a second one
    assert client.post(f"/api/company/record/requests/{rid}/approve", json={}, headers=h1).status_code == 403
    assert client.post(f"/api/actions/{aid}/approve", json={}, headers=h1).status_code == 403
    assert len(client.get("/api/company/record", headers=h1).json()["requests"]) == 1
    assert client.post(f"/api/company/record/requests/{rid}/approve", json={}).status_code == 400   # no session
    assert client.post("/api/company/record/requests/PR-nope/approve", json={}, headers=h1).status_code == 400
    h2 = client.new_session()
    other = client.get("/api/company/record", headers=h2).json()["requests"][0]
    assert other["id"] == rid and other["mine"] is False and h1["X-Session"] not in json.dumps(other)
    assert client.post(f"/api/company/record/requests/{rid}/approve", json={"user": "bob"},
                       headers=h2).json()["status"] == "approved"
    assert client.post(f"/api/actions/{aid}/approve", json={}, headers=h1).json()["status"] == "approved"
    res = client.post(f"/api/actions/{aid}/execute", json={}, headers=h1).json()["result"]
    fact = next(f for f in client.get("/api/company/record", headers=h1).json()["facts"] if f["id"] == res["org_fact"])
    assert fact["approved_by"] == _tag(h2) and fact["published_by"].startswith(_tag(h1))
    assert fact["approved_by"] != fact["published_by"].split()[0] and fact["here"] == "own"

    # an operator with the admin token approves directly; a wrong token is refused
    monkeypatch.setenv("ORG_ADMIN_TOKEN", "t0ken-for-tests")
    h3 = client.new_session()
    _result(client.post("/api/events/DEMO-0/fire", headers=h3))
    a3 = _publish_action(client, h3)["id"]
    assert client.post(f"/api/actions/{a3}/approve", json={}, headers={**h3, "X-Admin-Token": "wrong"}).status_code == 403
    ok = client.post(f"/api/actions/{a3}/approve", json={}, headers={**h3, "X-Admin-Token": "t0ken-for-tests"})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    res = client.post(f"/api/actions/{a3}/execute", json={}, headers=h3).json()["result"]
    assert res["approved_by"] == "admin"
    # the admin flag does not leak into the next request
    h4 = client.new_session()
    _result(client.post("/api/events/DEMO-1/fire", headers=h4))
    assert client.post(f"/api/actions/{_publish_action(client, h4)['id']}/approve", json={}, headers=h4).status_code == 403


# ------------------------------------------------------------------ apply ripples to cases

def test_opting_in_applies_the_fact_and_ripples_cases(client, org):
    h1 = client.new_session()
    of = _publish(client, h1)
    h2 = client.new_session()
    g2 = _g(h2)
    case = _result(client.post("/api/qualify", json={"tender_id": NHX, "use_llm": False}, headers=h2))["case"]
    _result(client.post("/api/events/DEMO-0/fire", headers=h2))
    assert _verdict(client, h2, case["id"]) == "BID"
    on = _opt_in(client, h2)["company_record"]
    assert [a["org_fact"] for a in on["applied"]] == [of]
    assert [d["policy"] for d in on["decisions"]] == ["company_record"] and on["ripples"]
    assert _value(g2) == _value(_g(h1))
    assert _verdict(client, h2, case["id"]) == "NO-BID"                 # the case followed the fact
    assert g2.scalar("MATCH (a:Action {kind:'review_bid', case_id:$c}) RETURN count(a)", {"c": case["id"]}) == 1
    d = on["decisions"][0]
    # an applied fact is already published: no second publish action for it
    assert g2.scalar("MATCH (a:Action {kind:'publish_to_company_record', target:$d}) RETURN count(a)",
                     {"d": d["decision_id"]}) == 0
    trace = client.get(f"/api/decisions/{d['decision_id']}/trace", headers=h2).json()
    assert trace["company_record"] == of and trace["company_record_status"] == "active" and trace["contestable"]
    # transaction time: the claim was learned when the fact was published, not when this workspace applied it
    pub = next(f for f in client.get("/api/company/record", headers=h2).json()["facts"] if f["id"] == of)
    assert g2.scalar("MATCH (c:Claim {company_record:$f}) RETURN c.recorded_at", {"f": of}) == pub["published_at"]
    assert pub["here"] == "active" and pub["applied_here"]
    # applied once: more requests change nothing
    client.get("/api/cases", headers=h2)
    assert g2.scalar("MATCH (d:Decision {policy:'company_record'}) RETURN count(d)") == 1


# ------------------------------------------------------------------ newer evidence beats the record

def test_record_is_one_participant_not_a_trump_card():
    rec = {"id": "r", "source_kind": "client_corrigendum", "value": 188, "authority": 95, "issuer": NSPO,
           "observed_day": 100, "precision": None, "source_doc": "D-r", "company_record": "OF-0000000001",
           "record_ruled": ["cert"]}
    cert = {"id": "cert", "source_kind": "client_certificate", "value": 200, "authority": 90, "issuer": NSPO,
            "observed_day": 10, "precision": None, "source_doc": "D-c"}
    p = engine.propose({"predicate": "value", "claims": [rec, cert]}, [])
    assert p["policy"] == "company_record" and p["winner"]["id"] == "r" and p["record_fact"] == "OF-0000000001"
    # a later amendment by the same issuer, at least as formal: recency wins over the record
    newer = {**cert, "id": "n", "source_kind": "client_corrigendum", "authority": 95, "value": 195, "observed_day": 200}
    p = engine.propose({"predicate": "value", "claims": [rec, cert, newer]}, [])
    assert p["policy"] == "recency_same_issuer" and p["winner"]["id"] == "n"
    # a stronger source the record never weighed wins on authority
    strong = {"id": "s", "source_kind": "x", "value": 199, "authority": 120, "issuer": "Auditor", "observed_day": 5,
              "precision": None, "source_doc": "D-s"}
    p = engine.propose({"predicate": "value", "claims": [rec, cert, strong]}, [])
    assert p["policy"] == "authority" and p["winner"]["id"] == "s"
    # a newer but less formal statement by the same issuer: a person decides (never auto-rejected)
    letter = {**newer, "id": "l", "source_kind": "reference_letter", "authority": 80}
    p = engine.propose({"predicate": "value", "claims": [rec, cert, letter]}, [])
    assert p["policy"] == "needs_human" and not p["auto"]
    # an equal-authority claim from another issuer: the record breaks the tie
    other = {**newer, "id": "o", "issuer": "Someone Else", "observed_day": 300}
    p = engine.propose({"predicate": "value", "claims": [rec, cert, other]}, [])
    assert p["policy"] == "company_record" and p["winner"]["id"] == "r" and "breaks the tie" in p["rationale"]
    # a weaker newer claim from another issuer: the record outranks it
    weak = {**other, "id": "w", "authority": 60}
    assert engine.propose({"predicate": "value", "claims": [rec, cert, weak]}, [])["policy"] == "company_record"


def test_newer_corrigendum_beats_an_applied_record_fact(client, org):
    h1 = client.new_session()
    of = _publish(client, h1)
    h2 = client.new_session()
    g2 = _g(h2)
    _opt_in(client, h2)
    assert g2.scalar("MATCH (d:Decision {policy:'company_record'}) RETURN count(d)") == 1
    out = engine.apply_event(g2, _event(1_950_000_000, "client_corrigendum", NSPO, "2026-07-01", "NEWER"))
    assert [d["policy"] for d in out["decisions"]] == ["recency_same_issuer"]
    assert _value(g2) == 1_950_000_000
    assert g2.scalar("MATCH (c:Claim {company_record:$f}) RETURN c.status", {"f": of}) == "superseded"
    # and a newer, less formal letter by the same issuer opens a card instead of being auto-rejected
    h3 = client.new_session()
    g3 = _g(h3)
    _opt_in(client, h3)
    out = engine.apply_event(g3, _event(1_990_000_000, "reference_letter", NSPO, "2026-07-02", "LETTER"))
    assert out["cards"] and not out["decisions"]
    assert g3.scalar("MATCH (c:Claim {source_doc:'DOC-T-LETTER'}) RETURN c.status") == "contested"


# ------------------------------------------------------------------ contest

def test_contest_reopens_the_record_decision_and_fact_as_of_survives(client, org):
    h1 = client.new_session()
    of = _publish(client, h1)
    h2 = client.new_session()
    g2 = _g(h2)
    d = _opt_in(client, h2)["company_record"]["decisions"][0]
    assert client.post(f"/api/company/record/{of}/contest", json={}, headers=h1).status_code == 409    # own fact
    assert client.post("/api/company/record/OF-nope/contest", json={}, headers=h2).status_code == 400
    assert client.post("/api/company/record/OF-0000000000/contest", json={}, headers=h2).status_code == 404
    h0 = client.new_session()                                                       # never applied it
    assert client.post(f"/api/company/record/{of}/contest", json={}, headers=h0).status_code == 409
    r = client.post(f"/api/company/record/{of}/contest", json={"reason": "we doubt it"}, headers=h2)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["contested"] is True and out["contests"] == 1 and len(out["reopened"]) == 1
    card = client.get("/api/conflicts", headers=h2).json()["cards"][0]
    assert card["reopened_from"] == d["decision_id"] and of in card["rationale"]
    trace = client.get(f"/api/decisions/{d['decision_id']}/trace", headers=h2).json()
    assert trace["company_record_status"] == "contested" and not trace["contestable"]
    fact = next(f for f in client.get("/api/company/record", headers=h2).json()["facts"] if f["id"] == of)
    assert fact["here"] == "contested" and fact["contested_here"] and fact["contests"] == 1
    # the card resolves like any other: back to the certificate's value
    cert = next(c for c in card["claims"] if c["source_kind"] == "client_certificate")
    res = client.post(f"/api/conflicts/{card['id']}/resolve", headers=h2,
                      json={"winner_claim": cert["id"], "rationale": "our certificate"})
    assert res.status_code == 200, res.text
    base = Graph().scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113})
    assert _value(g2) == base
    # transaction time: the certificate is alive now (invalidated_at cleared), its set-aside interval kept
    row = g2.read("MATCH (c:Claim {id:$c}) RETURN c.invalidated_at AS t, c.status_history AS h, c.status AS s",
                  {"c": cert["id"]})[0]
    assert row["t"] is None and row["s"] == "active" and row["h"]
    tomorrow = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    now = client.post("/api/tool/fact_as_of", headers=h2,
                      json={"key": PKG113, "predicate": "value", "as_of": tomorrow}).json()["result"]
    assert now["value"] is not None and now["claim"]["claim"] == cert["id"]
    # a contested fact is not re-applied, and stops deciding here
    client.get("/api/cases", headers=h2)
    assert g2.scalar("MATCH (d:Decision {policy:'company_record'}) RETURN count(d)") == 1


# ------------------------------------------------------------------ retire propagates

def test_retire_withdraws_the_fact_everywhere_and_ripples(client, org):
    h1 = client.new_session()
    of = _publish(client, h1)
    h2 = client.new_session()
    g2 = _g(h2)
    case = _result(client.post("/api/qualify", json={"tender_id": NHX, "use_llm": False}, headers=h2))["case"]
    _result(client.post("/api/events/DEMO-0/fire", headers=h2))
    d = _opt_in(client, h2)["company_record"]["decisions"][0]
    assert _verdict(client, h2, case["id"]) == "NO-BID"
    assert client.post(f"/api/company/record/{of}/retire", json={}, headers=h2).status_code == 403
    r = client.post(f"/api/company/record/{of}/retire", json={"reason": "wrong corrigendum"}, headers=h1)
    assert r.status_code == 200 and r.json()["retired"] == of
    # h2 catches up on its next request: the fact is withdrawn, the value reverts, the case flips back
    assert _verdict(client, h2, case["id"]) == "BID"
    base = Graph().scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113})
    assert _value(g2) == base
    assert g2.scalar("MATCH (c:Claim {company_record_withdrawn:$f}) RETURN c.status", {"f": of}) == "withdrawn"
    trace = client.get(f"/api/decisions/{d['decision_id']}/trace", headers=h2).json()
    assert trace["company_record_status"] == "retired" and "retired" in trace["review"]
    head = g2.read("""MATCH (n:Decision {subject_key:$k, predicate:'value'}) WHERE NOT ()-[:SUPERSEDES]->(n)
                      RETURN n.policy AS p""", {"k": PKG113})
    assert [x["p"] for x in head] == ["record_withdrawn"]
    assert g2.scalar("MATCH (a:Action {kind:'review_bid', case_id:$c}) RETURN count(a)", {"c": case["id"]}) == 2
    fact = next(f for f in client.get("/api/company/record", headers=h2).json()["facts"] if f["id"] == of)
    assert fact["status"] == "retired" and fact["here"] == "retired"
    # the publisher's own decision stands, and a new opted-in workspace no longer applies the fact
    assert _value(_g(h1)) < base
    h3 = client.new_session()
    _opt_in(client, h3)
    assert _g(h3).scalar("MATCH (d:Decision {policy:'company_record'}) RETURN count(d)") == 0


# ------------------------------------------------------------------ governance gaps

def test_forwarded_for_is_ignored_unless_a_proxy_is_configured(client, monkeypatch):
    monkeypatch.setattr(sessions, "SESSION_RATE", 1)
    monkeypatch.setattr(sessions, "_created", {})
    assert main.FORWARDED_HOPS == 0
    r = client.post("/api/session", headers={"X-Forwarded-For": "203.0.113.9"})
    client.made.append(r.json()["session_id"])
    # a different spoofed address does not buy a new quota: the key is the socket peer
    assert client.post("/api/session", headers={"X-Forwarded-For": "198.51.100.9"}).status_code == 429


def test_orphaned_company_rules_expire(client, org):
    h1 = client.new_session()
    g1 = sessions.graph_for(h1["X-Session"])
    g1.run("""CREATE (:PolicyRule {id:'RULE-00000000cc', key:'RULE-00000000cc', predicate:'role',
                                   prefer_kind:'partner_certificate', over_kind:'portfolio', rationale:'t',
                                   created_by:'t', created_at:timestamp(), retired:false})""")
    oid = client.post("/api/memory/promote", json={"rule_id": "RULE-00000000cc"}, headers=h1).json()["org_rule"]
    assert engine.expire_orphan_rules() == []
    assert client.get("/api/memory/org").json()["rules"][0]["orphaned"] is False
    g1.delete()                                              # the promoting session is gone
    t0 = 1_000_000_000_000
    assert engine.expire_orphan_rules(t0) == []              # first seen orphaned: stamped
    rule = client.get("/api/memory/org").json()["rules"][0]
    assert rule["orphaned"] is True and rule["orphaned_at"] == t0
    assert engine.expire_orphan_rules(t0 + engine.ORG_ORPHAN_TTL * 1000 - 1) == []
    assert engine.expire_orphan_rules(t0 + engine.ORG_ORPHAN_TTL * 1000) == [oid]
    gone = client.get("/api/memory/org?include_retired=true").json()["rules"][0]
    assert gone["retired"] is True and gone["retired_by"] == "expired"
