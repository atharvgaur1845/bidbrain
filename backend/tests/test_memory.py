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
def org(monkeypatch, tmp_path):
    """A private company graph (and JSON mirror) for this test; the real `bidbrain_org` is never touched."""
    assert Graph().exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    name = f"pytest_org_{secrets.token_hex(4)}"
    monkeypatch.setattr(engine, "ORG_GRAPH", name)
    monkeypatch.setattr(engine, "ORG_EXPORT_PATH", str(tmp_path / "org_memory.json"))

    def no_llm(*a, **k):
        raise AssertionError("no LLM calls in these tests")
    monkeypatch.setattr(providers, "chat", no_llm)
    set_test_principals(monkeypatch)
    yield name
    Graph(name).delete()


# test principals (throwaway secrets, generated per process; never the real .env ones)
TEST_SECRETS = {n: secrets.token_urlsafe(12) for n in ("manager", "manager2", "approver", "approver2", "boss")}
TEST_ROLES = {"manager": "bid_manager", "manager2": "bid_manager", "approver": "approver",
              "approver2": "approver", "boss": "admin"}


def set_test_principals(monkeypatch) -> None:
    from bidbrain.api import auth
    monkeypatch.setenv("BIDBRAIN_PRINCIPALS", ",".join(f"{n}:{TEST_ROLES[n]}:{auth.hash_secret(s)}"
                                                       for n, s in TEST_SECRETS.items()))
    monkeypatch.setenv("BIDBRAIN_AUTH_KEY", "pytest-auth-key-" + "k" * 16)


def bearer(name: str) -> dict:
    """An Authorization header for a test principal (a real signed token)."""
    from bidbrain.api import auth
    return {"Authorization": f"Bearer {auth.issue(name)[0]}"}


@pytest.fixture()
def client(org, monkeypatch):
    monkeypatch.setattr(sessions, "MAX_SESSIONS", 10_000)     # never evict someone else's session
    monkeypatch.setattr(sessions, "SESSION_RATE", 10_000)
    made: list[str] = []
    c = TestClient(main.app)

    def new_session(principal: str | None = "manager") -> dict:
        """A new workspace; by default signed in as the test bid manager (principal=None: anonymous)."""
        sid = c.post("/api/session").json()["session_id"]
        made.append(sid)
        return {"X-Session": sid, **(bearer(principal) if principal else {})}
    c.new_session = new_session
    c.made = made
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

def _learn_demo3_rule(client, h) -> tuple[str, dict]:
    out = _result(client.post("/api/events/DEMO-3/fire", headers=h))
    assert out["cards"] and not out["decisions"]
    card = next(c for c in client.get("/api/conflicts", headers=h).json()["cards"]
                if c["key"] == "residentialquartersuttarpradeshpkg25")
    partner = next(c for c in card["claims"] if c["source_kind"] == "partner_certificate")
    res = client.post(f"/api/conflicts/{card['id']}/resolve", headers=h,
                      json={"winner_claim": partner["id"], "apply_to_similar": True, "user": "judge-1",
                            "rationale": "the JV lead's certificate is the record of our role"})
    assert res.status_code == 200, res.text
    assert res.json()["decision"]["learned_rule"]
    return res.json()["decision"]["learned_rule"], res.json()["decision"]


def _tag(h) -> str:
    return engine.session_tag(f"session_{h['X-Session']}")


def _opt_in(client, h, on=True) -> dict:
    r = client.post("/api/session/settings", json={"use_company_memory": on}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_demo_chain_promote_then_opted_in_session_auto_resolves_with_org_rule(client, org):
    h1 = client.new_session()
    case = _result(client.post("/api/qualify", json={"tender_id": "T-NHX-2026-014", "use_llm": False}, headers=h1))
    case = case["case"]
    assert case["verdict"] == "BID"
    _result(client.post("/api/events/DEMO-0/fire", headers=h1))
    _result(client.post("/api/events/DEMO-1/fire", headers=h1))
    cases = client.get("/api/cases", headers=h1).json()
    assert next(c for c in cases if c["id"] == case["id"])["verdict"] == "NO-BID"

    rule_id, decision = _learn_demo3_rule(client, h1)
    # nothing reaches company memory until someone promotes it
    assert client.get("/api/memory/org").json()["rules"] == []
    assert org not in db().list_graphs()

    pr = client.post("/api/memory/promote", json={"rule_id": rule_id, "user": "judge-1"}, headers=h1)
    assert pr.status_code == 200, pr.text
    oid = pr.json()["org_rule"]
    listed = client.get("/api/memory/org", headers=h1).json()
    assert listed["active"] == 1
    rule = listed["rules"][0]
    # provenance is the promoting session's tag; the typed name is only an unverified label
    assert rule["id"] == oid and rule["promoted_by"] == _tag(h1) and rule["promoted_by_label"] == "judge-1"
    assert rule["source_rule"] == rule_id and rule["mine"] is True and rule["status"] == "active"
    assert rule["source_session"] == _tag(h1) and h1["X-Session"] not in json.dumps(listed)
    learned = rule["learned_from"]
    assert learned["id"] == decision["decision_id"]
    assert learned["rationale"] == "the JV lead's certificate is the record of our role"
    assert learned["decided_by"] == "judge-1" and rule["promoted_at"]
    # mirrored to JSON for hosts whose FalkorDB data dir does not survive a restart
    assert oid in json.dumps(json.load(open(engine.ORG_EXPORT_PATH)))
    # promoting the same rule again is idempotent; the session's rule list shows it as company memory
    again = client.post("/api/memory/promote", json={"rule_id": rule_id}, headers=h1).json()
    assert again["org_rule"] == oid and again["already"] is True
    assert client.get("/api/conflicts", headers=h1).json()["rules"][0]["org_rule"] == oid

    # another visitor who did not opt in: their scripted DEMO-3 still opens its card
    h3 = client.new_session("manager2")
    assert client.get("/api/session/settings", headers=h3).json()["use_company_memory"] is False
    out = _result(client.post("/api/events/DEMO-3/fire", headers=h3))
    assert out["cards"] and not out["decisions"]

    # a second browser turns company memory on: DEMO-4 resolves by the company rule
    h2 = client.new_session("manager2")
    assert client.get("/api/conflicts", headers=h2).json()["rules"] == []
    assert _opt_in(client, h2)["use_company_memory"] is True
    out = _result(client.post("/api/events/DEMO-4/fire", headers=h2))
    assert not out["cards"] and len(out["decisions"]) == 1
    d = out["decisions"][0]
    assert d["policy"] == "org_rule" and oid in d["rationale"]
    trace = client.get(f"/api/decisions/{d['decision_id']}/trace", headers=h2).json()
    assert trace["org_rule"]["id"] == oid and trace["org_rule"]["source_decision"] == learned["id"]
    assert trace["org_rule_status"] == "active" and trace["review"] is None
    row = client.get("/api/conflicts", headers=h2).json()["decisions"][0]
    assert row["org_rule"] == oid and row["org_rule_status"] == "active"

    # only the promoting session may retire it
    r = client.post(f"/api/memory/org/{oid}/retire", json={}, headers=h2)
    assert r.status_code == 403 and "contest" in r.json()["detail"]
    assert client.post(f"/api/memory/org/{oid}/retire", json={}).status_code == 400          # no session
    r = client.post(f"/api/memory/org/{oid}/retire", json={"user": "judge-1"}, headers=h1)
    assert r.status_code == 200 and r.json()["retired"] is True, r.text
    assert client.get("/api/memory/org").json()["rules"] == []
    gone = client.get("/api/memory/org?include_retired=true").json()["rules"][0]
    assert gone["retired_by"] == "manager" and gone["retired_by_label"] == "judge-1" and gone["status"] == "retired"
    # the other session's decision now says its rule is retired, and asks for a review
    trace = client.get(f"/api/decisions/{d['decision_id']}/trace", headers=h2).json()
    assert trace["org_rule_status"] == "retired" and trace["org_rule"]["status"] == "retired"
    assert "retired" in trace["review"]
    assert client.get("/api/conflicts", headers=h2).json()["decisions"][0]["org_rule_status"] == "retired"

    # the demo is resettable: the next opted-in session gets a card again
    h4 = client.new_session("manager2")
    _opt_in(client, h4)
    out = _result(client.post("/api/events/DEMO-4/fire", headers=h4))
    assert out["cards"] and not out["decisions"]


def _seed_org_rule(client, h) -> str:
    """Promote 'prefer partner_certificate over portfolio for role' from session h."""
    g = sessions.graph_for(h["X-Session"])
    g.run("""CREATE (:PolicyRule {id:'RULE-00000000aa', key:'RULE-00000000aa', predicate:'role',
                                  prefer_kind:'partner_certificate', over_kind:'portfolio',
                                  rationale:'JV lead certifies the role', created_by:'t', created_at:timestamp(),
                                  retired:false})""")
    r = client.post("/api/memory/promote", json={"rule_id": "RULE-00000000aa"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["org_rule"]


def test_contest_reopens_this_sessions_decisions_and_stops_the_rule_here(client, org):
    h1 = client.new_session()
    oid = _seed_org_rule(client, h1)
    h2 = client.new_session("manager2")
    _opt_in(client, h2)
    d = _result(client.post("/api/events/DEMO-4/fire", headers=h2))["decisions"][0]
    assert d["policy"] == "org_rule"

    assert client.post(f"/api/memory/org/{oid}/contest", json={}, headers=h1).status_code == 409   # own rule
    assert client.post(f"/api/memory/org/{oid}/contest", json={}).status_code == 400              # no session
    r = client.post(f"/api/memory/org/{oid}/contest", json={"reason": "our JV agreement says Prime"}, headers=h2)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["contested"] is True and out["contests"] == 1 and len(out["reopened"]) == 1
    conf = client.get("/api/conflicts", headers=h2).json()
    card = conf["cards"][0]
    assert card["reopened_from"] == d["decision_id"] and oid in card["rationale"]
    assert d["winner"] in {c["id"] for c in card["claims"]}
    row = next(x for x in conf["decisions"] if x["id"] == d["decision_id"])
    assert row["org_rule_status"] == "contested" and row["reopened"] is True
    listed = client.get("/api/memory/org", headers=h2).json()["rules"][0]
    assert listed["contests"] == 1 and listed["contested_here"] is True and listed["mine"] is False
    assert client.get("/api/memory/org", headers=h1).json()["rules"][0]["contested_here"] is False

    # the card resolves like any other (to our portfolio this time), superseding the rule's decision
    client_claim = next(c for c in card["claims"] if c["source_kind"] == "portfolio")
    res = client.post(f"/api/conflicts/{card['id']}/resolve", headers=h2,
                      json={"winner_claim": client_claim["id"], "rationale": "our records"})
    assert res.status_code == 200, res.text
    trace = client.get(f"/api/decisions/{res.json()['decision']['decision_id']}/trace", headers=h2).json()
    assert d["decision_id"] in trace["supersedes"]
    # and the contested rule no longer applies in this session (it still does in other opted-in ones)
    assert engine.session_org_rules(sessions.graph_for(h2["X-Session"])) == []
    h3 = client.new_session("manager2")
    _opt_in(client, h3)
    assert [r["id"] for r in engine.session_org_rules(sessions.graph_for(h3["X-Session"]))] == [oid]


def test_promote_and_opt_in_recheck_open_cards(client, org):
    # promote: a card already open in the promoting session, of the rule's shape, auto-resolves
    h1 = client.new_session()
    assert _result(client.post("/api/events/DEMO-4/fire", headers=h1))["cards"]
    rule_id, _ = _learn_demo3_rule(client, h1)
    assert len(client.get("/api/conflicts", headers=h1).json()["cards"]) == 1      # DEMO-4 still waits
    pr = client.post("/api/memory/promote", json={"rule_id": rule_id}, headers=h1)
    assert pr.status_code == 200, pr.text
    rc = pr.json()["rechecked"]
    assert len(rc["decisions"]) == 1 and rc["decisions"][0]["policy"] == "learned_rule"
    assert client.get("/api/conflicts", headers=h1).json()["cards"] == []
    # opting in: an open card a company rule settles is settled at once, citing the rule
    h2 = client.new_session("manager2")
    assert _result(client.post("/api/events/DEMO-4/fire", headers=h2))["cards"]
    on = _opt_in(client, h2)
    assert len(on["decisions"]) == 1 and on["decisions"][0]["policy"] == "org_rule"
    assert client.get("/api/conflicts", headers=h2).json()["cards"] == []
    assert _opt_in(client, h2, False)["use_company_memory"] is False
    assert client.post("/api/session/settings", json={"use_company_memory": "yes"}, headers=h2).status_code == 400


def test_admin_retire_reopens_the_acting_sessions_decisions(client, org, monkeypatch):
    h1 = client.new_session()
    oid = _seed_org_rule(client, h1)
    h2 = client.new_session("manager2")
    _opt_in(client, h2)
    d = _result(client.post("/api/events/DEMO-4/fire", headers=h2))["decisions"][0]
    monkeypatch.setenv("ORG_ADMIN_TOKEN", "t0ken-for-tests")
    bad = client.post(f"/api/memory/org/{oid}/retire", json={}, headers={**h2, "X-Admin-Token": "wrong"})
    assert bad.status_code == 403
    r = client.post(f"/api/memory/org/{oid}/retire", json={}, headers={**h2, "X-Admin-Token": "t0ken-for-tests"})
    assert r.status_code == 200, r.text
    assert len(r.json()["reopened"]) == 1
    card = client.get("/api/conflicts", headers=h2).json()["cards"][0]
    assert card["reopened_from"] == d["decision_id"] and "retired" in card["rationale"]
    assert client.get("/api/memory/org?include_retired=true").json()["rules"][0]["retired_by"] == "admin"


def test_org_memory_json_mirror_round_trip(client, org, tmp_path):
    h1 = client.new_session()
    oid = _seed_org_rule(client, h1)
    h2 = client.new_session("manager2")
    assert client.post(f"/api/memory/org/{oid}/contest", json={"user": "bob"}, headers=h2).status_code == 200
    before = engine.list_org(include_retired=True)
    path = engine.export_org(str(tmp_path / "mirror.json"))
    assert engine.import_org(path) == 0                     # the live graph wins: nothing imported
    Graph(org).delete()
    assert engine.export_org(str(tmp_path / "empty.json"), only_if_present=True) is None
    assert engine.import_org(path) == 1
    after = engine.list_org(include_retired=True)
    assert after == before and after[0]["contests"] == 1 and after[0]["promoted_by"] == _tag(h1)


def test_new_sessions_are_rate_limited_per_client_ip(client, monkeypatch):
    monkeypatch.setattr(sessions, "SESSION_RATE", 2)
    monkeypatch.setattr(sessions, "_created", {})
    monkeypatch.setattr(main, "FORWARDED_HOPS", 1)          # behind one proxy (HF Spaces); the default is 0
    ip = {"X-Forwarded-For": "203.0.113.7"}
    ok = [client.post("/api/session", headers=ip) for _ in range(2)]
    assert all(r.status_code == 200 for r in ok)
    client.made.extend(r.json()["session_id"] for r in ok)
    r = client.post("/api/session", headers=ip)
    assert r.status_code == 429 and r.headers.get("retry-after") == "60"
    # an unknown session id would create a graph too: the same limit applies there
    sid = secrets.token_hex(6)
    r = client.get("/api/overview", headers={"X-Session": sid, **ip})
    assert r.status_code == 429 and f"session_{sid}" not in db().list_graphs()
    # a spoofed left-most entry does not help: the proxy-appended (right-most) address is the key
    assert client.post("/api/session", headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.7"}).status_code == 429
    r = client.post("/api/session", headers={"X-Forwarded-For": "198.51.100.2"})
    assert r.status_code == 200
    client.made.append(r.json()["session_id"])
    # an existing session is never limited
    assert client.get("/api/overview", headers={"X-Session": ok[0].json()["session_id"], **ip}).status_code == 200


def test_promote_and_retire_validate_input_and_cap(client, org, monkeypatch):
    h = client.new_session()
    assert client.post("/api/memory/promote", json={"rule_id": "x' OR 1=1"}, headers=h).status_code == 400
    assert client.post("/api/memory/promote", json={}, headers=h).status_code == 400
    assert client.post("/api/memory/promote", json={"rule_id": "RULE-0000000000"}, headers=h).status_code == 404
    assert client.post("/api/memory/promote", json={"rule_id": "RULE-0000000000"}).status_code == 400   # no session
    assert client.post("/api/memory/org/nope/retire", json={}, headers=h).status_code == 400
    assert client.post("/api/memory/org/ORG-0000000000/retire", json={}, headers=h).status_code == 404
    assert client.post("/api/memory/org/ORG-0000000000/contest", json={}, headers=h).status_code == 404

    g = sessions.graph_for(h["X-Session"])
    for i, (pk, ok) in enumerate([("partner_certificate", "client_certificate"),
                                  ("client_certificate", "partner_certificate"),
                                  ("hr_record", "cv")]):
        g.run("""CREATE (:PolicyRule {id:$id, key:$id, predicate:'role', prefer_kind:$pk, over_kind:$ok,
                                     rationale:'t', created_by:'t', created_at:timestamp(), retired:false})""",
              {"id": f"RULE-00000000{i:02d}", "pk": pk, "ok": ok})
    assert client.post("/api/memory/promote", json={"rule_id": "RULE-0000000000", "user": "<b>x</b>"},
                       headers=h).status_code == 200
    got = client.get("/api/memory/org").json()["rules"][0]
    assert got["promoted_by_label"] == "bxb" and got["promoted_by"] == _tag(h)     # markup stripped
    # the opposite preference cannot sit beside it in company memory
    r = client.post("/api/memory/promote", json={"rule_id": "RULE-0000000001"}, headers=h)
    assert r.status_code == 409 and "retire it first" in r.json()["detail"]
    monkeypatch.setattr(engine, "ORG_MAX_ACTIVE", 1)
    r = client.post("/api/memory/promote", json={"rule_id": "RULE-0000000002"}, headers=h)
    assert r.status_code == 409 and "full" in r.json()["detail"]


def test_org_rules_never_apply_outside_sessions(org):
    """Building the base graph and the evals run engine.run on non-session graphs: hermetic."""
    base = Graph()
    s = sessions.copy_base(f"pytest_hermetic_{secrets.token_hex(3)}")
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


# ------------------------------------------------------------------ conversation recall over MENTIONS

RING = "Work:ringroaduttarpradeshpkg113"
OTHER = "Work:anganwadicentregujaratpkg151"


@pytest.fixture()
def convo():
    s = sessions.copy_base(f"pytest_recall_{secrets.token_hex(3)}")
    yield s
    s.delete()


def _fake_answer(text: str, nodes: list[tuple[str, str]], docs: list[str]) -> dict:
    return {"explanation": text, "evidence": [{"doc_id": d, "page": 1, "what": "t"} for d in docs],
            "subgraph": {"nodes": [{"id": i, "label": i.partition(":")[0], "name": n} for i, n in nodes],
                         "edges": []}}


def test_answer_mentions_only_what_the_evidence_is_about(convo):
    out = _fake_answer("Priya Patel led it; the certificate gives the value.",
                       [(RING, "Ring Road — Uttar Pradesh Pkg-113"), (OTHER, "Anganwadi Centre — Gujarat Pkg-151"),
                        ("Person:priyapatel", "Priya Patel"), ("Person:nehachopra", "Neha Chopra"),
                        ("Client:nationalspecialprojectsoffice", "National Special Projects Office"),
                        ("Category:expressways", "expressways")],
                       ["DOC-CC-113"])
    got = sessions.answer_mentions(convo, out)
    # named in the answer, evidenced by the cited document, and that work's client; not the rest
    assert set(got) == {"Person:priyapatel", RING, "Client:nationalspecialprojectsoffice"}


def test_question_names_entities_strictly_and_loosely(convo):
    ids = lambda q: {e["id"] for e in sessions.entities_named(convo, q)}
    assert RING in ids("What was the value of Ring Road — Uttar Pradesh Pkg-113?")
    assert RING in ids("and pkg 113?") and "Person:priyapatel" in ids("Is Priya Patel free?")
    assert ids("which ring road in uttar pradesh?") == set()         # loose only once talked about
    sessions.remember_turn(convo, "user", "x", [RING, "Person:priyapatel"])
    assert RING in ids("which ring road in uttar pradesh?") and "Person:priyapatel" in ids("what about Priya?")
    assert ids("total revenue last year") == set()


def test_follow_up_recalls_older_turns_that_mention_the_same_entity(convo):
    q0 = "Who led Ring Road — Uttar Pradesh Pkg-113?"
    u0 = sessions.remember_turn(convo, "user", q0, [e["id"] for e in sessions.entities_named(convo, q0)])
    sessions.remember_turn(convo, "assistant", "Priya Patel led Ring Road — Uttar Pradesh Pkg-113.",
                           [RING, "Person:priyapatel"])
    for k in range(8):                                   # push them out of the 6-turn window
        sessions.remember_turn(convo, "user" if k % 2 == 0 else "assistant", f"filler about turnover {k}",
                               [OTHER] if k == 3 else [])
    assert u0 == 0

    msgs, recalled = sessions.context(convo, "Remind me, what was the value of the ring road in Uttar Pradesh?")
    assert [r["i"] for r in recalled] == [0, 1]
    assert all(m["content"].startswith("[Recalled from earlier") for m in msgs[:2])
    assert "Ring Road" in msgs[1]["content"] and len(msgs) == 8 and set(msgs[0]) == {"role", "content"}
    assert [m["content"] for m in msgs[2:]] == [f"filler about turnover {k}" for k in range(2, 8)]
    # recall is bounded, and a question naming nothing talked about recalls nothing
    assert len(sessions.recall(convo, "Pkg-113 and Priya Patel and Pkg-151", before=100)) <= sessions.RECALL_K
    assert sessions.context(convo, "what is our turnover?")[1] == []
    assert len(sessions.history(convo)) == 6


def test_ask_endpoint_records_mentions_and_recall_edges(client, monkeypatch):
    from bidbrain.agents import qa

    def fake_ask(g, question, emit=None, history=None, max_steps=10):
        fake_ask.history = history
        return {**_fake_answer("Priya Patel led Ring Road — Uttar Pradesh Pkg-113.",
                               [(RING, "Ring Road — Uttar Pradesh Pkg-113"), (OTHER, "Anganwadi Centre"),
                                ("Person:priyapatel", "Priya Patel")], ["DOC-CC-113"]),
                "answer": "Priya Patel", "steps": [], "usage": {}}
    monkeypatch.setattr(qa, "ask", fake_ask)
    h = client.new_session()
    _result(client.post("/api/ask", json={"question": "Did Priya lead the Pkg-113 ring road?"}, headers=h))
    for k in range(3):
        _result(client.post("/api/ask", json={"question": f"Anything else {k}?"}, headers=h))
    res = _result(client.post("/api/ask", json={"question": "And what did Priya lead before that?"}, headers=h))
    assert [r["i"] for r in res["recalled"]] == [0, 1]
    assert fake_ask.history[0]["content"].startswith("[Recalled")
    turns = client.get("/api/memory", headers=h).json()["turns"]
    # the user turn: Pkg-113 named strictly; "Priya" loosely, once the answer showed who that is
    assert set(turns[0]["mentions"]) == {"Ring Road — Uttar Pradesh Pkg-113", "Priya Patel"}
    assert set(turns[1]["mentions"]) == {"Ring Road — Uttar Pradesh Pkg-113", "Priya Patel"}   # not Anganwadi
    assert sorted(turns[8]["recalled"]) == [0, 1] and turns[2]["recalled"] == []


# ------------------------------------------------------------------ recall: round-3 fixes

TRISHAKTI = "Client:trishaktipowergenerationcorporation"


def test_answer_without_subgraph_mentions_what_its_text_names_strictly(convo):
    out = _fake_answer("Priya Patel led Ring Road — Uttar Pradesh Pkg-113; the ring road in UP was big.", [],
                       ["DOC-CC-113"])
    assert set(sessions.answer_mentions(convo, out)) == {RING, "Person:priyapatel"}


def test_refusals_and_unevidenced_answers_mention_nothing(convo):
    nodes = [(RING, "Ring Road — Uttar Pradesh Pkg-113"), ("Person:priyapatel", "Priya Patel")]
    for text in ("I can't answer that from the graph: Priya Patel and Pkg-113 have no such record.",
                 "I was unable to determine the value of Ring Road — Uttar Pradesh Pkg-113.\nANSWER: unknown"):
        assert sessions.answer_mentions(convo, _fake_answer(text, nodes, ["DOC-CC-113"])) == []
    assert sessions.answer_mentions(convo, {**_fake_answer("Priya Patel", nodes, []), "answer": "unknown"}) == []
    assert sessions.answer_mentions(convo, _fake_answer("Priya Patel led Pkg-113.", nodes, [])) == []
    # an absence answer that says "no letter was found" is not a refusal
    ok = _fake_answer("Priya Patel led it; no reference letter was found for Pkg-113.", nodes, ["DOC-CC-113"])
    assert "Person:priyapatel" in sessions.answer_mentions(convo, ok)


def test_one_distinctive_word_names_a_client_whose_other_words_are_generic(convo):
    ids = lambda q: {e["id"] for e in sessions.entities_named(convo, q)}
    assert TRISHAKTI in ids("Which Trishakti works have no reference letter?")
    assert TRISHAKTI in ids("and trishakti's?")
    assert "Client:mahanadisteelcorporation" in ids("what did Mahanadi pay us?")
    # a state, or a word that is also a category or another entity's name, never names a client alone
    assert not {i for i in ids("works in Gujarat") if i.startswith("Client:")}
    assert not {i for i in ids("school buildings in Delhi") if i.startswith("Client:")}
    assert not {i for i in ids("which power plants?") if i.startswith("Client:")}


def test_recalled_question_brings_its_answer(convo):
    q = "What did we build for Trishakti?"
    sessions.remember_turn(convo, "user", q, [e["id"] for e in sessions.entities_named(convo, q)])
    sessions.remember_turn(convo, "assistant", "Nineteen works, mostly flyovers.", [])     # names nothing
    for k in range(8):
        sessions.remember_turn(convo, "user" if k % 2 == 0 else "assistant", f"filler {k}", [])
    msgs, recalled = sessions.context(convo, "Remind me about Trishakti")
    assert [r["i"] for r in recalled] == [0, 1]
    assert "the answer to turn 0" in msgs[1]["content"] and "Nineteen works" in msgs[1]["content"]
