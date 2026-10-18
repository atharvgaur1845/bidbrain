"""P0-1 closeout: approvals tied to a server-verified identity (api/auth.py).

A session id is a workspace, not a person: a second browser tab must not satisfy "four eyes". Throwaway graphs only
(per-test company graph, session copies deleted afterwards); test principals with random secrets. No LLM.

    cd backend && pytest -q tests/test_auth_closeout.py
"""
from __future__ import annotations

import threading

import pytest

from bidbrain.api import auth
from bidbrain.graph.client import Graph
from tests.test_memory import TEST_SECRETS, _result, bearer, client, org  # noqa: F401  (fixtures)


def _pending_request(client, h) -> tuple[str, dict]:
    """Fire DEMO-1 in h; its signed-in principal's approve files a PublishRequest (403). Returns (action, request)."""
    _result(client.post("/api/events/DEMO-1/fire", headers=h))
    aid = next(a["id"] for a in client.get("/api/actions", headers=h).json()["actions"]
               if a["kind"] == "publish_to_company_record")
    r = client.post(f"/api/actions/{aid}/approve", json={}, headers=h)
    assert r.status_code == 403 and "PR-" in r.json()["detail"], r.text
    req = next(x for x in client.get("/api/company/record", headers=h).json()["requests"] if x["mine"])
    assert req["requested_by_principal"] == "manager" and req["version"] == 1
    return aid, req


def _approve(client, rid, h, version=1, **body):
    return client.post(f"/api/company/record/requests/{rid}/approve", json={"version": version, **body}, headers=h)


# ------------------------------------------------------------------ login, me, logout

def test_login_me_logout_and_wrong_secret(client):
    r = client.post("/api/auth/login", json={"name": "approver", "secret": TEST_SECRETS["approver"]})
    assert r.status_code == 200 and r.json()["principal"] == {"name": "approver", "role": "approver"}
    assert "httponly" in r.headers["set-cookie"].lower() and auth.COOKIE in r.headers["set-cookie"]
    assert client.get("/api/auth/me").json()["principal"]["name"] == "approver"       # via the cookie
    tok = r.json()["token"]
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/auth/me").json()["principal"] is None
    # the logged-out token is revoked, not merely forgotten by the browser
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()["principal"] is None
    assert client.post("/api/auth/login", json={"name": "approver", "secret": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"name": "ghost", "secret": "x"}).status_code == 401


def test_cookie_from_a_foreign_origin_is_refused(client):
    tok = auth.issue("approver")[0]
    me = client.get("/api/auth/me", headers={"Origin": "https://evil.example"}, cookies={auth.COOKIE: tok}).json()
    assert me["principal"] is None and "foreign origin" in me["problem"]
    client.cookies.clear()


# ------------------------------------------------------------------ the sybil: a second tab is not a second person

def test_cross_session_approval_without_login_is_refused(client):
    aid, req = _pending_request(client, client.new_session())
    anon = client.new_session(None)                   # another tab, nobody signed in
    r = _approve(client, req["id"], anon)
    assert r.status_code == 401 and "principal" in r.json()["detail"]
    assert client.get("/api/company/record", headers=anon).json()["requests"][0]["status"] == "pending"


def test_spoofed_actor_name_is_only_a_label(client):
    _, req = _pending_request(client, client.new_session())
    anon = client.new_session(None)
    for body in ({"user": "approver"}, {"user": "approver", "principal": "approver", "name": "approver"}):
        assert _approve(client, req["id"], anon, **body).status_code == 401
    r = _approve(client, req["id"], {**anon, "X-Principal": "approver", "X-User": "approver"})
    assert r.status_code == 401
    # signed in as the bid manager, typing the approver's name changes nothing either
    r = _approve(client, req["id"], client.new_session("manager2"), user="approver")
    assert r.status_code == 403 and "approver role" in r.json()["detail"]


def test_forged_and_expired_tokens(client):
    _, req = _pending_request(client, client.new_session())
    h = client.new_session(None)
    good = auth.issue("approver")[0]
    payload, sig = good.split(".")
    forged_sig = f"{payload}.{'0' * len(sig)}"
    import base64, json
    body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    body["n"] = "boss"
    swapped = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=") + "." + sig
    expired = auth.issue("approver", lifetime=-5)[0]
    for tok, why in ((forged_sig, "signature"), (swapped, "signature"), (expired, "expired"), ("garbage", "")):
        r = _approve(client, req["id"], {**h, "Authorization": f"Bearer {tok}"})
        assert r.status_code == 401 and why in r.json()["detail"], (why, r.text)
    # a token signed with another key is forged too
    other = auth.hmac.new(b"some-other-key-0123456789", payload.encode(), auth.hashlib.sha256).hexdigest()
    assert _approve(client, req["id"], {**h, "Authorization": f"Bearer {payload}.{other}"}).status_code == 401


def test_initiator_cannot_self_approve_from_another_tab(client):
    aid, req = _pending_request(client, client.new_session())
    second_tab = client.new_session("manager")         # same principal, new workspace
    r = _approve(client, req["id"], second_tab)
    assert r.status_code == 403            # a bid manager lacks the approver role anyway
    # and an initiator who *is* an approver is refused as the initiator (see the next test)


def test_approver_cannot_also_approve_the_publish_action(client, monkeypatch):
    # an approver who files the request themselves cannot then approve it, and a principal that approved the
    # request cannot also approve the action
    h = client.new_session("approver")
    _result(client.post("/api/events/DEMO-1/fire", headers=h))
    aid = next(a["id"] for a in client.get("/api/actions", headers=h).json()["actions"]
               if a["kind"] == "publish_to_company_record")
    assert client.post(f"/api/actions/{aid}/approve", json={}, headers=h).status_code == 403
    rid = client.get("/api/company/record", headers=h).json()["requests"][0]["id"]
    assert _approve(client, rid, h).status_code == 403
    assert _approve(client, rid, client.new_session("approver2")).status_code == 200
    assert client.post(f"/api/actions/{aid}/approve", json={},
                       headers={**h, **bearer("approver2")}).status_code == 403
    assert client.post(f"/api/actions/{aid}/approve", json={}, headers=h).json()["status"] == "approved"
    res = client.post(f"/api/actions/{aid}/execute", json={}, headers=h).json()["result"]
    fact = next(f for f in client.get("/api/company/record", headers=h).json()["facts"] if f["id"] == res["org_fact"])
    assert fact["owner_principal"] == "approver" and fact["approved_by"] == "approver2"


def test_wrong_role_cannot_approve(client):
    _, req = _pending_request(client, client.new_session())
    r = _approve(client, req["id"], client.new_session("manager2"))
    assert r.status_code == 403 and "approver role" in r.json()["detail"]


def test_execute_and_approve_of_a_publish_need_a_principal(client):
    h = client.new_session(None)
    _result(client.post("/api/events/DEMO-1/fire", headers=h))
    aid = next(a["id"] for a in client.get("/api/actions", headers=h).json()["actions"]
               if a["kind"] == "publish_to_company_record")
    assert client.post(f"/api/actions/{aid}/approve", json={}, headers=h).status_code == 401
    assert client.post(f"/api/actions/{aid}/execute", json={}, headers=h).status_code == 401
    # workspace-local actions stay session-scoped: no login needed
    case = _result(client.post("/api/qualify", json={"tender_id": "T-PHED-WTP-2027-03", "use_llm": False},
                               headers=h))["case"]
    local = client.get(f"/api/actions?case_id={case['id']}", headers=h).json()["actions"]
    assert local and client.post(f"/api/actions/{local[0]['id']}/approve", json={}, headers=h).status_code == 200


# ------------------------------------------------------------------ exactly one transition

def test_replayed_approve_is_idempotent_and_stale_version_is_409(client):
    _, req = _pending_request(client, client.new_session())
    ap = client.new_session("approver")
    assert _approve(client, req["id"], ap, version=2).status_code == 409             # not the version reviewed
    assert client.post(f"/api/company/record/requests/{req['id']}/approve", json={}, headers=ap).status_code == 400
    first = _approve(client, req["id"], ap)
    assert first.status_code == 200 and first.json()["already"] is False and first.json()["version"] == 2
    again = _approve(client, req["id"], ap)                                           # replay
    assert again.status_code == 200 and again.json()["already"] is True
    assert again.json()["decided_at"] == first.json()["decided_at"]
    # another approver, or a reject after the approval, cannot produce a conflicting state
    assert _approve(client, req["id"], client.new_session("approver2")).status_code == 409
    rej = client.post(f"/api/company/record/requests/{req['id']}/reject", json={"version": 1}, headers=ap)
    assert rej.status_code == 409
    audit = Graph(org_name()).read("MATCH (a:OrgAudit {target:$t}) RETURN a.action AS a, a.principal AS p",
                                   {"t": req["id"]})
    assert sorted((r["a"], r["p"]) for r in audit) == [("publish_request", "manager"),
                                                      ("publish_request_approved", "approver")]


def org_name() -> str:
    from bidbrain.conflicts import engine
    return engine.ORG_GRAPH


def test_concurrent_approvals_make_exactly_one_transition(client):
    _, req = _pending_request(client, client.new_session())
    heads = [client.new_session("approver" if i % 2 else "approver2") for i in range(8)]
    out: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def go(i):
        verb = "approve" if i % 3 else "reject"
        r = client.post(f"/api/company/record/requests/{req['id']}/{verb}", json={"version": 1}, headers=heads[i])
        with lock:
            out.append((r.status_code, r.json()))
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    winners = [b for s, b in out if s == 200 and b.get("already") is False]
    assert len(winners) == 1, out
    assert all(s in (200, 409) for s, _ in out)
    final = next(r for r in client.get("/api/company/record?include_decided=true", headers=heads[0]).json()["requests"]
                 if r["id"] == req["id"])
    assert final["status"] == winners[0]["status"] and final["version"] == 2
    # every 200 reports the same outcome as the winner
    assert {b["status"] for s, b in out if s == 200} == {final["status"]}


# ------------------------------------------------------------------ retire / promote / contest ownership

def test_only_the_owning_principal_or_admin_can_retire(client):
    from tests.test_record_r4 import _publish
    h1 = client.new_session()
    of = _publish(client, h1)
    other = client.new_session("manager2")
    r = client.post(f"/api/company/record/{of}/retire", json={}, headers=other)
    assert r.status_code == 403 and "owns" in r.json()["detail"]
    assert client.post(f"/api/company/record/{of}/retire", json={}, headers=client.new_session(None)).status_code == 401
    # the owner may retire from a different workspace: ownership is the principal, not the tab
    assert client.post(f"/api/company/record/{of}/retire", json={}, headers=client.new_session("manager")).json()["retired"] == of


def test_rule_promote_retire_contest_need_principals_and_ownership(client):
    rule = "RULE-00000000cc"
    def mk(h):
        Graph(f"session_{h['X-Session']}").run(
            """CREATE (:PolicyRule {id:$id, key:$id, predicate:'role', prefer_kind:'partner_certificate',
                                    over_kind:'portfolio', rationale:'t', created_by:'t', created_at:timestamp(),
                                    retired:false})""", {"id": rule})
    anon = client.new_session(None)
    mk(anon)
    assert client.post("/api/memory/promote", json={"rule_id": rule, "user": "manager"}, headers=anon).status_code == 401
    h1 = client.new_session("manager")
    mk(h1)
    r = client.post("/api/memory/promote", json={"rule_id": rule}, headers=h1)
    assert r.status_code == 200, r.text
    oid = r.json()["org_rule"]
    assert r.json()["rule"]["promoted_by_principal"] == "manager"
    h2 = client.new_session("manager2")
    assert client.post(f"/api/memory/org/{oid}/retire", json={}, headers=h2).status_code == 403
    assert client.post(f"/api/memory/org/{oid}/contest", json={}, headers=anon).status_code == 401
    assert client.post(f"/api/memory/org/{oid}/contest", json={"reason": "x"}, headers=h2).status_code == 200
    assert client.post(f"/api/memory/org/{oid}/retire", json={}, headers=client.new_session("boss")).json()["retired"]


# ------------------------------------------------------------------ fail closed

def test_fail_closed_without_principals(client, monkeypatch):
    h = client.new_session()                     # carries a token, but the server has no principals now
    monkeypatch.delenv("BIDBRAIN_PRINCIPALS")
    assert client.get("/api/auth/me").json()["enabled"] is False
    assert client.post("/api/auth/login", json={"name": "manager", "secret": TEST_SECRETS["manager"]}).status_code == 503
    _result(client.post("/api/events/DEMO-1/fire", headers=h))            # session-local work still runs
    aid = next(a["id"] for a in client.get("/api/actions", headers=h).json()["actions"]
               if a["kind"] == "publish_to_company_record")
    r = client.post(f"/api/actions/{aid}/approve", json={}, headers=h)
    assert r.status_code == 503 and "disabled" in r.json()["detail"]
    assert client.post("/api/memory/promote", json={"rule_id": "RULE-0000000000"}, headers=h).status_code == 503
    assert client.post("/api/company/record/requests/PR-0000000000/approve", json={"version": 1},
                       headers=h).status_code == 503
    case = _result(client.post("/api/qualify", json={"tender_id": "T-PHED-WTP-2027-03", "use_llm": False},
                               headers=h))["case"]
    assert case["id"]
    # no key at all: also disabled
    monkeypatch.setenv("BIDBRAIN_PRINCIPALS", "x:approver:" + "0" * 64)
    monkeypatch.delenv("BIDBRAIN_AUTH_KEY")
    assert auth.enabled() is False


# ------------------------------------------------------------------ MCP

def test_mcp_creator_cannot_approve_its_own_actions(client):
    from mcp.server.mcpserver.exceptions import ToolError
    from bidbrain.mcp import server
    res = server.qualify_tender("new", tender_id="T-PHED-WTP-2027-03", use_llm=False)
    client.made.append(res["session_id"])
    aid = res["actions"][0]["id"]
    with pytest.raises(ToolError, match="created by an MCP call"):
        server.transition_action(res["session_id"], aid, "approve")
    with pytest.raises(ToolError, match="principal_token refused"):
        server.transition_action(res["session_id"], aid, "approve", principal_token="forged.token")
    out = server.transition_action(res["session_id"], aid, "approve", principal_token=auth.issue("manager")[0])
    assert out["status"] == "approved" and out["events"][-1]["principal"] == "manager"
