"""Round-4 fixes to accountable actions and loop closure (judging round 3, track B).

* execute / reject race: the executor runs only after the CAS approved -> executing; a side effect happens at
  most once and never under a rejected action; reject is refused while an action is executing.
* concurrent approve: every caller gets the same success.
* a stale review_bid (the case moved on) is superseded, not executed.
* a ripple re-runs the AbsenceAuditor's checks and the action generator on the clauses it changed: the same
  findings and follow-ups a fresh qualification would produce; findings that no longer apply close their actions.
* good news: a live reference letter resolves the absence finding (citing the document), fulfils the letter
  request (and its DocumentRequest), and shows in the ripple's evidence_changed.

Throwaway graphs only (GRAPH.COPY of the base graph, session copies, a per-test company graph). No LLM.

    cd backend && pytest -q tests/test_actions_r4.py
"""
from __future__ import annotations

import json
import secrets
import threading
import time

import pytest

from bidbrain import config
from bidbrain.agents import providers
from bidbrain.company import actions
from bidbrain.graph.client import Graph
from tests.test_memory import _result, client, org  # noqa: F401  (fixtures)

TENDERS = {t["id"]: t for t in json.loads((config.DATA / "tenders" / "demo_tenders.json").read_text())}
DEMO = {e["event_id"]: e for e in json.loads((config.DATA / "conflicts" / "demo_events.json").read_text())}
EVENTS = {e["event_id"]: e for e in json.loads((config.DATA / "conflicts" / "events.json").read_text())}
ROB71 = "robgujaratpkg71"          # credited by T-RBC-2026-31 clause 2, no reference letter; EV-0167 is its letter


def _copy(src: str = config.BASE_GRAPH, prefix: str = "pytest_r4") -> Graph:
    """GRAPH.COPY with a short retry (a concurrent fork elsewhere on the server makes it fail transiently)."""
    name = f"{prefix}_{secrets.token_hex(4)}"
    for i in range(20):
        try:
            return Graph(src).copy_to(name)
        except Exception as exc:          # "could not fork"
            if "fork" not in str(exc) or i == 19:
                raise
            time.sleep(0.5)
    raise AssertionError("unreachable")


@pytest.fixture(scope="module")
def prepared():
    """A base copy with NHX, PHED and RBC qualified (rules only, no LLM); each test copies it again."""
    assert Graph().exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    from bidbrain.agents.orchestrator import qualify_tender
    real = providers.available
    providers.available = lambda: False
    g = _copy(prefix="pytest_r4_prep")
    try:
        cases = {tid: qualify_tender(g, dict(TENDERS[tid]), prefer_llm=False)["case"]["id"]
                 for tid in ("T-NHX-2026-014", "T-PHED-WTP-2027-03", "T-RBC-2026-31")}
        yield g.name, cases
    finally:
        providers.available = real
        g.delete()


@pytest.fixture()
def scratch(prepared, monkeypatch):
    def no_llm(*a, **k):
        raise AssertionError("no LLM calls in these tests")
    monkeypatch.setattr(providers, "chat", no_llm)
    monkeypatch.setattr(providers, "available", lambda: False)
    g = _copy(prepared[0])
    yield g, prepared[1]
    g.delete()


def _fire(g: Graph, ev: dict) -> dict:
    """What POST /api/events/{id}/fire does, minus the stream: the document, the engine, then the ripple."""
    from bidbrain.api import main
    from bidbrain.conflicts.events import apply_event
    ev = json.loads(json.dumps(ev))
    ev["doc"]["doc_id"] = f"{ev['doc']['doc_id']}-{secrets.token_hex(2)}"
    out = apply_event(g, ev)
    out.setdefault("doc", ev["doc"])
    return {**out, **main._ripple_after(g, lambda e: None, out, ev["doc"]["title"])}


def _letter_action(g: Graph, case_id: str, work: str = ROB71) -> str:
    return g.scalar("MATCH (a:Action {kind:'request_reference_letter', case_id:$c, target:$w}) RETURN a.id",
                    {"c": case_id, "w": work})


def _side_effects(g: Graph, aid: str) -> dict:
    return {"created": g.scalar("MATCH (:Action {id:$a})-[e:CREATED]->() RETURN count(e)", {"a": aid}),
            "requests": g.scalar("MATCH (r:DocumentRequest {action_id:$a}) RETURN count(r)", {"a": aid}),
            "executed_events": g.scalar("MATCH (:Action {id:$a})-[:HAS_EVENT]->(e:ActionEvent {to:'executed'}) "
                                        "RETURN count(e)", {"a": aid}),
            "status": g.scalar("MATCH (a:Action {id:$a}) RETURN a.status", {"a": aid})}


def _race(fns: list) -> list:
    """Run the callables at the same instant; returns each one's result or raised exception."""
    out: list = [None] * len(fns)
    gate = threading.Barrier(len(fns))

    def run(i, fn):
        gate.wait()
        try:
            out[i] = fn()
        except Exception as exc:          # noqa: BLE001  (the outcome is what is asserted)
            out[i] = exc
    ts = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    return out


# ------------------------------------------------------------------ 1. execute / reject race

def test_execute_reject_race_direct_side_effect_at_most_once_never_when_rejected(scratch):
    g, cases = scratch
    rbc = cases["T-RBC-2026-31"]
    work = g.scalar("MATCH (w:Work {key:$k}) RETURN w.key", {"k": ROB71})
    assert work
    outcomes = set()
    for i in range(12):
        # a fresh approved letter action each round (a distinct target, so ensure() creates a new one)
        aid = actions.ensure(g, "request_reference_letter", case_id=rbc, target=f"{ROB71}#race{i}", title=f"race {i}",
                             detail="race", trigger=None, payload={"work": ROB71})["id"]
        actions.transition(g, aid, "approve", "tester")
        name = g.name
        ex = lambda: actions.execute(Graph(name), aid, "t-exec")
        rj = lambda: actions.transition(Graph(name), aid, "reject", "t-reject")
        res = _race([ex, rj, ex, rj])
        se = _side_effects(g, aid)
        outcomes.add(se["status"])
        assert se["status"] in ("executed", "rejected"), se
        if se["status"] == "rejected":
            assert se == {"created": 0, "requests": 0, "executed_events": 0, "status": "rejected"}, se
            assert all(isinstance(r, actions.ActionError) or r["status"] == "rejected" for r in res[1::2])
            assert all(isinstance(r, actions.ActionError) for r in res[0::2]), res   # no execute "succeeded"
        else:
            assert se == {"created": 1, "requests": 1, "executed_events": 1, "status": "executed"}, se
            assert all(isinstance(r, actions.ActionError) for r in res[1::2]), res    # reject came too late
            assert all(not isinstance(r, Exception) and r["status"] == "executed" for r in res[0::2]), res
        # whoever won, a request for this action exists only if it executed
        g.run("MATCH (a:Action {id:$a})-[:CREATED]->(r:DocumentRequest) DETACH DELETE r", {"a": aid})
    assert outcomes                      # the race was run (which side wins is the scheduler's business)


def test_reject_is_refused_while_the_executor_runs(scratch, monkeypatch):
    g, cases = scratch
    aid = _letter_action(g, cases["T-RBC-2026-31"])
    actions.transition(g, aid, "approve", "tester")
    slow, started = actions.EXECUTORS["request_reference_letter"], threading.Event()

    def slow_exec(g_, a, by):
        started.set()
        time.sleep(0.5)
        return slow(g_, a, by)
    monkeypatch.setitem(actions.EXECUTORS, "request_reference_letter", slow_exec)
    name = g.name
    res = {}
    t = threading.Thread(target=lambda: res.setdefault("x", actions.execute(Graph(name), aid, "exec")))
    t.start()
    assert started.wait(5)
    assert g.scalar("MATCH (a:Action {id:$a}) RETURN a.status", {"a": aid}) == "executing"
    with pytest.raises(actions.ActionError, match="being executed"):
        actions.transition(g, aid, "reject", "late")
    second = actions.execute(Graph(name), aid, "exec-2")      # waits for the running one, same outcome
    t.join(10)
    assert res["x"]["status"] == second["status"] == "executed"
    assert res["x"]["result"] == second["result"]
    assert _side_effects(g, aid) == {"created": 1, "requests": 1, "executed_events": 1, "status": "executed"}


def test_failed_executor_hands_the_action_back_and_a_retry_does_not_duplicate(scratch, monkeypatch):
    g, cases = scratch
    aid = _letter_action(g, cases["T-RBC-2026-31"])
    actions.transition(g, aid, "approve", "tester")
    real = actions.EXECUTORS["request_reference_letter"]

    def flaky(g_, a, by):
        real(g_, a, by)                  # the side effect was written ...
        raise RuntimeError("network blip")   # ... but the executor did not return
    monkeypatch.setitem(actions.EXECUTORS, "request_reference_letter", flaky)
    with pytest.raises(RuntimeError):
        actions.execute(g, aid, "exec")
    assert g.scalar("MATCH (a:Action {id:$a}) RETURN a.status", {"a": aid}) == "approved"
    monkeypatch.setitem(actions.EXECUTORS, "request_reference_letter", real)
    assert actions.execute(g, aid, "exec")["status"] == "executed"
    assert _side_effects(g, aid) == {"created": 1, "requests": 1, "executed_events": 1, "status": "executed"}


def test_execute_reject_race_through_the_api(client, monkeypatch):
    h = client.new_session()
    case = _result(client.post("/api/qualify", json={"tender_id": "T-RBC-2026-31", "use_llm": False}, headers=h))["case"]
    g = Graph(f"session_{h['X-Session']}")
    aid = _letter_action(g, case["id"])
    assert client.post(f"/api/actions/{aid}/approve", json={}, headers=h).status_code == 200
    real = actions.EXECUTORS["request_reference_letter"]

    def slower(g_, a, by):               # widen the window the judge hit
        time.sleep(0.2)
        return real(g_, a, by)
    monkeypatch.setitem(actions.EXECUTORS, "request_reference_letter", slower)
    post = lambda verb: (lambda: client.post(f"/api/actions/{aid}/{verb}", json={}, headers=h))
    res = _race([post("execute"), post("reject"), post("execute"), post("reject")])
    se = _side_effects(g, aid)
    codes = [r.status_code for r in res]
    if se["status"] == "executed":
        assert se == {"created": 1, "requests": 1, "executed_events": 1, "status": "executed"}
        assert [codes[0], codes[2]] == [200, 200] and all(res[i].json()["status"] == "executed" for i in (0, 2))
        assert [codes[1], codes[3]] == [409, 409]
    else:
        assert se == {"created": 0, "requests": 0, "executed_events": 0, "status": "rejected"}
        assert [codes[0], codes[2]] == [409, 409]


# ------------------------------------------------------------------ 2. concurrent approve

def test_concurrent_approve_gives_every_caller_the_same_success(scratch, client):
    g, cases = scratch
    aid = _letter_action(g, cases["T-RBC-2026-31"])
    name = g.name
    res = _race([lambda: actions.transition(Graph(name), aid, "approve", "a")] * 8)
    assert all(not isinstance(r, Exception) and r["status"] == "approved" for r in res), res
    assert g.scalar("MATCH (:Action {id:$a})-[:HAS_EVENT]->(e:ActionEvent {to:'approved'}) RETURN count(e)",
                    {"a": aid}) == 1
    # the same through the API
    h = client.new_session()
    case = _result(client.post("/api/qualify", json={"tender_id": "T-PHED-WTP-2027-03", "use_llm": False},
                               headers=h))["case"]
    sg = Graph(f"session_{h['X-Session']}")
    a2 = sg.scalar("MATCH (a:Action {kind:'renew_credential', case_id:$c}) RETURN a.id LIMIT 1", {"c": case["id"]})
    res = _race([lambda: client.post(f"/api/actions/{a2}/approve", json={}, headers=h)] * 6)
    assert [r.status_code for r in res] == [200] * 6
    assert {r.json()["status"] for r in res} == {"approved"}


# ------------------------------------------------------------------ 3. stale review

def test_stale_review_is_superseded_not_executed(scratch):
    g, cases = scratch
    nhx = cases["T-NHX-2026-014"]
    _fire(g, DEMO["DEMO-1"])
    _fire(g, DEMO["DEMO-0"])                             # NHX 2.1 PASS -> FAIL: the case is NO-BID
    assert g.scalar("MATCH (c:Case {id:$c}) RETURN c.verdict", {"c": nhx}) == "NO-BID"
    current = g.scalar("MATCH (a:Action {kind:'review_bid', case_id:$c, target:'BID->NO-BID'}) RETURN a.id", {"c": nhx})
    assert current
    # a review raised earlier for a recommendation the case no longer has
    old = actions.ensure(g, "review_bid", case_id=nhx, target="NO-BID->BID", title="old review", detail="old")["id"]
    actions.transition(g, old, "approve", "bid manager")
    out = actions.execute(g, old, "bid manager")
    assert out["status"] == "superseded"
    assert out["result"]["superseded"] and "now NO-BID" in out["result"]["reason"]
    assert out["result"]["replaced_by"] == current
    assert g.scalar("MATCH (c:Case {id:$c}) RETURN c.approved_recommendation", {"c": nhx}) is None
    assert [e["to"] for e in out["events"]] == ["pending", "approved", "superseded"]
    assert actions.execute(g, old, "again")["events"] == out["events"]          # idempotent
    with pytest.raises(actions.ActionError):
        actions.transition(g, old, "approve", "x")
    # the current one still executes and approves the current recommendation
    actions.transition(g, current, "approve", "bid manager")
    assert actions.execute(g, current, "bid manager")["result"]["recommendation"] == "NO-BID"


def test_a_new_flip_supersedes_the_older_pending_review(scratch):
    g, cases = scratch
    nhx = cases["T-NHX-2026-014"]
    old = actions.ensure(g, "review_bid", case_id=nhx, target="NO-BID->BID", title="old", detail="old")["id"]
    new = actions.from_ripple(g, [{"case_id": nhx, "from": "BID", "to": "NO-BID"}], None, "a test flip", None)
    assert new and new[0]["id"] != old
    a = actions.get(g, old)
    assert a["status"] == "superseded" and new[0]["id"] in a["events"][-1]["note"]


# ------------------------------------------------------------------ 4. findings and actions after a ripple

def _audited(g: Graph, case_id: str, ref: str) -> list[tuple]:
    rows = g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause {ref:$r})-[:HAS_FINDING]->(f:Finding)
                     WHERE f.kind IN ['absence', 'gap', 'expiry'] AND coalesce(f.status, 'open') <> 'resolved'
                     OPTIONAL MATCH (f)-[:CITES]->(x) WHERE x:Work OR x:Person
                     RETURN f.kind AS kind, f.text AS text, collect(x.key) AS cites ORDER BY kind""",
                  {"c": case_id, "r": ref})
    return [(r["kind"], r["text"].split(" (proof across")[0], tuple(sorted(r["cites"]))) for r in rows]


def _follow_ups(g: Graph, case_id: str) -> set:
    return {(r["k"], r["t"]) for r in g.read("""MATCH (a:Action {case_id:$c}) WHERE a.kind <> 'review_bid'
                                                  AND a.status IN ['pending', 'approved', 'executed']
                                                RETURN a.kind AS k, a.target AS t""", {"c": case_id})}


def test_a_ripple_produces_the_findings_and_actions_a_fresh_qualify_would(scratch):
    from bidbrain.agents.orchestrator import qualify_tender
    g, cases = scratch
    nhx = cases["T-NHX-2026-014"]
    _fire(g, DEMO["DEMO-1"])
    out = _fire(g, DEMO["DEMO-0"])
    changed = [c for r in out["ripples"] for c in r["cases_changed"] if c["case_id"] == nhx]
    assert changed and changed[0]["to"] == "NO-BID"
    assert [f["kind"] for f in changed[0]["findings_opened"]] == ["gap"]
    rippled = _audited(g, nhx, "2.1")
    assert rippled and rippled[0][0] == "gap" and "Consider a JV" in rippled[0][1]
    fresh = qualify_tender(g, dict(TENDERS["T-NHX-2026-014"]), prefer_llm=False)
    assert fresh["case"]["verdict"] == "NO-BID"
    assert _audited(g, fresh["case"]["id"], "2.1") == rippled               # same kind, same words, same citations
    assert _follow_ups(g, fresh["case"]["id"]) == _follow_ups(g, nhx)


def test_reaudit_closes_moot_findings_and_queues_the_new_follow_ups(scratch):
    """A ripple moves PHED clause ii off one of the two people whose PMP lapses: the old expiry finding (both) is
    resolved and its renewal actions superseded; the new one (one person) gets its renewal action."""
    from bidbrain.tools.case import apply_flips
    g, cases = scratch
    phed = cases["T-PHED-WTP-2027-03"]
    k = g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause {ref:'ii'}) RETURN k.id AS id, k.verdict AS v,
                  k.basis AS b, k.detail AS d""", {"c": phed})[0]
    people = g.read("MATCH (:Clause {id:$k})-[s:SATISFIED_BY]->(p:Person) RETURN p.key AS key, p.name AS name, "
                    "s.credited AS cr", {"k": k["id"]})
    detail = json.loads(k["d"])
    lapsing = [e["name"] for e in detail["expiring_within_90d"] if e["name"] in {p["name"] for p in people}]
    assert len(lapsing) == 2
    before = {r["t"]: r["id"] for r in g.read("MATCH (a:Action {kind:'renew_credential', case_id:$c}) "
                                              "RETURN a.target AS t, a.id AS id", {"c": phed})}
    assert len(before) == 2
    keep = [p for p in people if p["name"] != lapsing[0]]
    entry = {"case": "PHED", "case_id": phed, "clause": "ii", "kind": "key_personnel", "from": k["v"], "to": k["v"],
             "basis": k["b"], "clause_id": k["id"], "detail": detail,
             "satisfied": [("Person", p["key"], {"credited": p["cr"]}) for p in keep]}
    changed = apply_flips(g, [], "a test ripple", [entry])
    assert {a["status"] for a in map(lambda x: actions.get(g, x), before.values())} == {"superseded"}
    assert sorted(changed[0]["closed_actions"]) == sorted(before.values())
    remaining = [n for n in lapsing if n != lapsing[0]]
    new = [actions.get(g, a) for a in changed[0]["follow_ups"]]
    assert [a["kind"] for a in new] == ["renew_credential"] and remaining[0] in new[0]["title"]
    assert [f["kind"] for f in changed[0]["findings_resolved"]] == ["expiry"]
    assert [f["kind"] for f in changed[0]["findings_opened"]] == ["expiry"]


# ------------------------------------------------------------------ 5. good news closes the loop

def _absence(g: Graph, case_id: str, work: str) -> dict:
    rows = g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(:Clause)-[:HAS_FINDING]->(f:Finding {kind:'absence'})
                     -[:CITES]->(:Work {key:$w})
                     OPTIONAL MATCH (f)-[:RESOLVED_BY]->(d:Document)
                     RETURN f.id AS id, coalesce(f.status, 'open') AS status, f.resolution AS resolution,
                            collect(d.doc_id) AS resolved_by""", {"c": case_id, "w": work})
    assert len(rows) == 1, rows
    return rows[0]


def test_letter_arrival_resolves_the_finding_and_fulfils_the_request(client):
    h = client.new_session()
    case = _result(client.post("/api/qualify", json={"tender_id": "T-RBC-2026-31", "use_llm": False}, headers=h))["case"]
    g = Graph(f"session_{h['X-Session']}")
    assert _absence(g, case["id"], ROB71)["status"] != "resolved"
    aid = _letter_action(g, case["id"])
    # the request was already sent (approved + executed): the letter answers it
    client.post(f"/api/actions/{aid}/approve", json={}, headers=h)
    req = client.post(f"/api/actions/{aid}/execute", json={}, headers=h).json()["result"]["request_id"]
    verdict = g.scalar("MATCH (c:Case {id:$c}) RETURN c.verdict", {"c": case["id"]})

    out = _result(client.post("/api/events/EV-0167/fire", headers=h))
    doc = out["doc"]["doc_id"]
    assert ROB71 in out["references"]
    rip = next(r for r in out["ripples"] if r["subject"] == f"Work:{ROB71}")
    letters = [x for e in rip["evidence_changed"] if e["case_id"] == case["id"] for x in e.get("reference_letters", [])]
    assert [(x["doc_id"], x["work"]) for x in letters] == [(doc, ROB71)]
    f = _absence(g, case["id"], ROB71)
    assert f["status"] == "resolved" and f["resolved_by"] == [doc] and doc in f["resolution"]
    a = actions.get(g, aid)
    assert a["status"] == "fulfilled" and a["result"]["request_id"] == req      # the executed result is kept
    assert a["closure"]["fulfilled_by"] == doc and a["events"][-1]["to"] == "fulfilled"
    assert g.read("MATCH (r:DocumentRequest {id:$r})-[:FULFILLED_BY]->(d:Document) RETURN r.status AS s, d.doc_id AS d",
                  {"r": req}) == [{"s": "fulfilled", "d": doc}]
    changed = next(c for c in rip["cases_changed"] if c["case_id"] == case["id"])
    assert aid in changed["closed_actions"]
    # a letter never makes the case worse, and the finding no longer holds it back
    after = g.scalar("MATCH (c:Case {id:$c}) RETURN c.verdict", {"c": case["id"]})
    assert after == verdict or (verdict == "BID WITH FIXES" and after == "BID")
    # firing it again changes nothing more
    again = _result(client.post("/api/events/EV-0167/fire", headers=h))
    assert not [e for r in again["ripples"] for e in r["evidence_changed"] if e.get("reference_letters")]
    assert actions.get(g, aid)["events"] == a["events"]


def test_a_letter_the_engine_has_nothing_to_say_about_still_closes_the_loop(scratch):
    """A consistent restatement (no decision, no card, no first claim) is not rippled by the API; the document
    path closes the loop itself. The pending request is fulfilled without being executed."""
    from bidbrain.conflicts.events import apply_event
    g, cases = scratch
    rbc = cases["T-RBC-2026-31"]
    aid = _letter_action(g, rbc)
    w = g.read("MATCH (w:Work {key:$k})-[:FOR_CLIENT]->(c:Client) RETURN w.value AS v, c.name AS client, "
               "w.title AS title", {"k": ROB71})[0]
    ev = {"event_id": "R4-LETTER", "type": "consistent_restatement",
          "doc": {"doc_id": "DOC-R4-LETTER", "doc_type": "reference_letter", "issuer": w["client"],
                  "issued": "2026-06-01", "title": f"Letter of Appreciation — {w['title']}"},
          "claims": [{"subject_label": "Work", "subject_key": ROB71, "predicate": "value", "value": w["v"],
                      "source_kind": "reference_letter", "issuer": w["client"], "observed": "2026-06-01", "page": 1}],
          "narrative": "restates the certified value"}
    out = apply_event(g, ev)
    assert not out["decisions"] and not out["cards"] and not out["first_claims"]
    assert out["reference_ripples"] and out["reference_ripples"][0]["evidence_changed"]
    f = _absence(g, rbc, ROB71)
    assert f["status"] == "resolved" and f["resolved_by"] == ["DOC-R4-LETTER"]
    a = actions.get(g, aid)
    assert a["status"] == "fulfilled" and [e["to"] for e in a["events"]] == ["pending", "fulfilled"]
    assert not g.read("MATCH (:Action {id:$a})-[:CREATED]->(r) RETURN r", {"a": aid})       # nothing was sent
