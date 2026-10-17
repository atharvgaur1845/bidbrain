"""Adaptive agents: the unasked-filter guard, one model per answer, and the qualification team acting on
each other's findings (Planner, reference re-selection, Verifier retry). No real LLM calls: providers.chat
is replaced by fakes. Mutating tests run on a GRAPH.COPY of bidbrain_base.

    cd backend && pytest -q tests/test_agents_adaptive.py
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bidbrain import config
from bidbrain.agents import loop, providers
from bidbrain.agents.orchestrator import qualify_tender
from bidbrain.graph.client import Graph
from bidbrain.tools import case as C


@pytest.fixture(scope="module")
def g():
    g = Graph()
    assert g.exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    return g


@pytest.fixture(scope="module")
def scratch(g):
    s = g.copy_to("pytest_adaptive")
    yield s
    s.delete()


def _demo(tid: str) -> dict:
    return {t["id"]: t for t in json.load(open(config.DATA / "tenders" / "demo_tenders.json"))}[tid]


def _reply(content="", calls=(), provider="groq:model-a"):
    return providers.Reply(content=content, tool_calls=list(calls), provider=provider, model=provider.split(":")[1],
                           usage={"prompt": 10, "completion": 5}, cached=False, ms=1.0)


def _call(name, args, i=0):
    return {"id": f"c{i}", "name": name, "arguments": json.dumps(args)}


# ------------------------------------------------------------------ 1. unasked filters

def test_unasked_filters_are_lexical_and_generic():
    q = "What is the total value of all works for the client of Pkg-59?"
    assert loop.unasked_filters("find_portfolio", {"via_work": "Pkg-59", "role": "Prime",
                                                   "years": list(range(2015, 2026))}, q) == \
        ['role="Prime"', "years=[2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]"]
    # each filter the question does ask for is left alone
    asked = "List our Prime water treatment (WTP) works for state clients completed since 2019 above 50 Cr with a reference letter"
    assert loop.unasked_filters("find_portfolio", {"role": "Prime", "categories": ["water treatment"],
                                                   "govt_levels": ["state"], "completed_after": "2019-01-01",
                                                   "min_value": 500000000, "has_reference": True}, asked) == []
    # a client name is not evidence of a category or government-level filter
    assert loop.unasked_filters("find_portfolio", {"client": "PHED Odisha", "categories": ["water supply"],
                                                   "govt_levels": ["state"]},
                                "Total value of works for PHED Odisha") == \
        ['govt_levels=["state"]', 'categories=["water supply"]']
    # excluding needs an exclusion word, not only the category
    assert loop.unasked_filters("find_portfolio", {"exclude_categories": ["buildings"]},
                                "total value of the buildings works") == ['exclude_categories=["buildings"]']
    assert loop.unasked_filters("receivables", {"measure": "outstanding", "year": 2024},
                                "How much does Jal Nigam owe us?") == ["year=2024"]
    assert loop.unasked_filters("prove_absence", {"evidence": "reference_letter", "category": "irrigation"},
                                "which canal works lack a letter") == []
    # no-op values are not filters, and tools without narrowing filters are never checked
    assert loop.unasked_filters("find_portfolio", {"min_value": 0, "govt_levels":
                                                   ["central", "state", "psu", "municipal", "private"]}, "all works") == []
    assert loop.unasked_filters("aggregate", {"op": "sum", "work_keys": []}, "x") == []


def test_unasked_filter_note_reaches_the_model(g, monkeypatch):
    seen = []

    def fake_chat(messages, tools=None, prefer=None, **kw):
        seen.append(messages)
        if len(seen) == 1:
            return _reply(calls=[_call("find_portfolio", {"via_work": "rccbridgemaharashtrapkg50", "role": "Prime",
                                                          "years": [2015, 2016]})])
        return _reply("path...\nANSWER: 1")

    monkeypatch.setattr(providers, "chat", fake_chat)
    ev = []
    res = loop.run_agent(g, "qa", "sys", "What is the total value of that client's works?", ["find_portfolio"],
                         emit=ev.append)
    tool_msg = [m for m in seen[1] if m["role"] == "tool"][-1]["content"]
    assert tool_msg.endswith("repeat the call without them unless the user asked for them.")
    assert "NOTE: you filtered by role=\"Prime\", years=[2015, 2016]" in tool_msg
    assert res.steps[0]["unasked_filters"] == ['role="Prime"', "years=[2015, 2016]"]
    assert [e for e in ev if e["type"] == "tool_result"][0]["unasked_filters"]

    # the same call is clean when the question asks for it (or did in an earlier turn)
    seen.clear()
    res = loop.run_agent(g, "qa", "sys", "And their total?", ["find_portfolio"],
                         history=[{"role": "user", "content": "Show Prime works of that client from 2015-2016"},
                                  {"role": "assistant", "content": "..."}])
    assert "NOTE:" not in [m for m in seen[1] if m["role"] == "tool"][-1]["content"]
    assert "unasked_filters" not in res.steps[0]


def test_qa_prompt_forbids_unasked_filters():
    from bidbrain.agents.prompts import QA_SYSTEM
    assert "Filter only on what the question states" in QA_SYSTEM and "NOTE" in QA_SYSTEM


# ------------------------------------------------------------------ 3. one model per answer

def test_run_agent_sticks_to_the_first_bucket(g, monkeypatch):
    prefs = []

    def fake_chat(messages, tools=None, prefer=None, **kw):
        prefs.append(prefer)
        if len(prefs) < 3:
            return _reply(calls=[_call("graph_schema", {}, len(prefs))], provider="groq:model-b")
        return _reply("ANSWER: 1", provider="groq:model-a")      # a fallback answer does not move the anchor

    monkeypatch.setattr(providers, "chat", fake_chat)
    res = loop.run_agent(g, "qa", "sys", "q", ["graph_schema"], max_steps=4)
    assert prefs == [None, "groq:model-b", "groq:model-b"]
    assert res.providers == ["groq:model-b", "groq:model-b", "groq:model-a"]


class _RateLimited(Exception):
    status_code = 429


def _fake_client(log, fail=()):
    def create(model, **kw):
        log.append(model)
        if model in fail:
            raise _RateLimited("rate limited, try again in 30s")
        msg = SimpleNamespace(content=f"from {model}", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_chat_prefers_the_sticky_bucket_and_falls_back_on_rate_limit(monkeypatch):
    log, fail = [], set()
    client = _fake_client(log, fail)
    monkeypatch.setattr(providers, "POOL", [("groq", "model-a"), ("groq", "model-b"), ("groq", "model-c")])
    monkeypatch.setattr(providers, "_client", lambda prov: client)
    monkeypatch.setattr(providers, "_parked", {})
    monkeypatch.setattr(providers, "_park_kind", {})
    monkeypatch.setattr(providers, "REPLAY", False)
    msgs = [{"role": "user", "content": "hi"}]

    assert providers.chat(msgs, use_cache=False).provider == "groq:model-a"          # no preference: pool order
    assert providers.chat(msgs, use_cache=False, prefer="groq:model-b").provider == "groq:model-b"
    # the preferred bucket hits a rate limit: this call falls back, and the bucket is parked
    fail.add("model-b")
    log.clear()
    rep = providers.chat(msgs, use_cache=False, prefer="groq:model-b")
    assert log == ["model-b", "model-a"] and rep.provider == "groq:model-a"
    assert providers._parked["groq:model-b"] > 0
    # while parked it is skipped outright; once free again it is preferred again
    log.clear()
    assert providers.chat(msgs, use_cache=False, prefer="groq:model-b").provider == "groq:model-a" and log == ["model-a"]
    fail.clear()
    providers._parked.clear()
    assert providers.chat(msgs, use_cache=False, prefer="groq:model-b").provider == "groq:model-b"


# ------------------------------------------------------------------ 2. adaptive team

def _bridge_tender(min_value: int) -> dict:
    return {"id": "T-TEST-LB", "title": "Large bridge (test)", "bid_date": "2026-06-30", "category": "large bridges",
            "clauses": [{"id": "1", "kind": "similar_works",
                         "text": "One large bridge work in the last 7 years, with client reference letter.",
                         "params": {"categories": ["large bridges"], "min_value": min_value, "count": 1,
                                    "lookback_years": 7}}]}


def _runs(g, case_id):
    return g.read("""MATCH (r:AgentRun)-[:WORKED_ON]->(:Case {id:$c})
                     OPTIONAL MATCH (r)-[:HANDED_OFF_TO]->(n:AgentRun)
                     RETURN r.agent AS agent, r.pass_no AS pass_no, r.requested_by AS requested_by,
                            n.agent AS to ORDER BY r.started_at, r.id""", {"c": case_id})


def _chain(g, case_id):
    """The agents in HANDED_OFF_TO order (started_at has millisecond ties)."""
    nxt = {r["a"]: r["b"] for r in g.read("""MATCH (a:AgentRun)-[:WORKED_ON]->(:Case {id:$c}),
                                                   (a)-[:HANDED_OFF_TO]->(b:AgentRun)
                                             RETURN a.id AS a, b.id AS b""", {"c": case_id})}
    names = {r["id"]: r["agent"] for r in g.read("MATCH (r:AgentRun)-[:WORKED_ON]->(:Case {id:$c}) "
                                                 "RETURN r.id AS id, r.agent AS agent", {"c": case_id})}
    cur = (set(names) - set(nxt.values())).pop()
    out = [names[cur]]
    while cur in nxt:
        cur = nxt[cur]
        out.append(names[cur])
    return out


def test_auditor_hands_back_and_hunter_reselects_referenced_works(scratch):
    ev = []
    case = qualify_tender(scratch, _bridge_tender(700_000_000), emit=ev.append, prefer_llm=False)
    cid = case["case"]["id"]
    # first pick (MP Pkg-127, no letter) -> the absence finding is resolved by re-selecting Rajasthan Pkg-124
    assert case["case"]["verdict"] == "BID"
    sat = scratch.read("MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k)-[:SATISFIED_BY]->(w:Work) "
                       "RETURN w.key AS k, w.has_reference AS ref, k.prefer_reference AS pref", {"c": cid})
    assert sat == [{"k": "extradosedbridgerajasthanpkg124", "ref": True, "pref": True}]
    f = scratch.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->()-[:HAS_FINDING]->(f:Finding {kind:'absence'})
                        OPTIONAL MATCH (r:AgentRun)-[:RESOLVED]->(f)
                        RETURN f.status AS status, f.severity AS sev, f.request AS req, r.agent AS by""", {"c": cid})
    assert f == [{"status": "resolved", "sev": "info", "req": "reselect_referenced", "by": "EvidenceHunter"}]
    # the hand-back is a second EvidenceHunter run, reached by HANDED_OFF_TO from the AbsenceAuditor
    assert _chain(scratch, cid) == ["ClauseParser", "Planner", "EvidenceHunter", "AbsenceAuditor", "EvidenceHunter",
                                    "Verifier", "Assembler"]
    second = [r for r in _runs(scratch, cid) if r["agent"] == "EvidenceHunter" and r["pass_no"] == 2]
    assert second and second[0]["requested_by"] == "AbsenceAuditor"
    assert any(e["type"] == "handoff" and e["from"] == "AbsenceAuditor" for e in ev)
    plan = json.loads(scratch.scalar("MATCH (c:Case {id:$c}) RETURN c.plan", {"c": cid}))
    assert plan["handoffs"][0]["outcomes"] == [{"clause": "1", "outcome": "reselected", "verdict": "PASS"}]
    # the Verifier re-derives with the same selection policy, so it agrees
    assert scratch.scalar("MATCH (c:Case {id:$c}) RETURN c.verified", {"c": cid}) is True


def test_reselection_keeps_the_original_when_referenced_works_fail(scratch):
    case = qualify_tender(scratch, _bridge_tender(1_200_000_000), prefer_llm=False)
    cid = case["case"]["id"]
    assert case["case"]["verdict"] == "BID WITH FIXES"          # the absence finding stays a live risk
    f = scratch.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->()-[:HAS_FINDING]->(f:Finding {kind:'absence'})
                        RETURN f.status AS status, f.severity AS sev""", {"c": cid})
    assert f == [{"status": "kept", "sev": "high"}]
    assert scratch.scalar("MATCH (:Case {id:$c})-[:HAS_CLAUSE]->()-[:SATISFIED_BY]->(w:Work) RETURN w.key",
                          {"c": cid}) == "extradosedbridgemadhyapradeshpkg127"


def _corrupt_turnover(monkeypatch, times: int):
    """A writer that records FAIL for the turnover clause's PASS `times` times (a stale / racing write)."""
    real, left = C.set_verdict, [times]

    def bad(g, clause_id, verdict, basis, satisfied, run_id=None):
        if verdict == "PASS" and basis.startswith("average of last") and left[0] > 0:
            left[0] -= 1
            verdict = "FAIL"
        return real(g, clause_id, verdict, basis, satisfied, run_id)

    monkeypatch.setattr(C, "set_verdict", bad)


def test_verifier_mismatch_is_rederived_once_and_resolved(scratch, monkeypatch):
    _corrupt_turnover(monkeypatch, times=1)
    ev = []
    case = qualify_tender(scratch, _demo("T-NHX-2026-014"), emit=ev.append, prefer_llm=False)
    cid = case["case"]["id"]
    assert case["case"]["verdict"] == "BID"
    assert scratch.scalar("MATCH (c:Case {id:$c}) RETURN c.verified", {"c": cid}) is True
    chain = _chain(scratch, cid)
    assert chain[-4:] == ["Verifier", "EvidenceHunter", "Verifier", "Assembler"]
    f = scratch.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k)-[:HAS_FINDING]->(f:Finding {kind:'verification_failed'})
                        RETURN k.verdict AS verdict, f.status AS status, f.severity AS sev""", {"c": cid})
    assert f == [{"verdict": "PASS", "status": "resolved", "sev": "info"}]
    verified = [e for e in ev if e["type"] == "verified"]
    assert len(verified) == 1 and verified[0]["ok"] and verified[0]["retried"][0]["stored"] == "FAIL"


def test_persistent_verifier_mismatch_needs_review(scratch, monkeypatch):
    _corrupt_turnover(monkeypatch, times=99)
    case = qualify_tender(scratch, _demo("T-NHX-2026-014"), prefer_llm=False)
    cid = case["case"]["id"]
    assert case["case"]["verdict"] == "NEEDS REVIEW"
    assert scratch.scalar("MATCH (c:Case {id:$c}) RETURN c.verified", {"c": cid}) is False
    assert [r["agent"] for r in _runs(scratch, cid)].count("EvidenceHunter") == 2      # exactly one retry
    assert scratch.scalar("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->()-[:HAS_FINDING]->(f:Finding
                             {kind:'verification_failed'}) RETURN f.status""", {"c": cid}) == "persisted"


def test_planner_routes_and_skips_by_clause_kind(scratch):
    t = {"id": "T-TEST-CERT", "title": "Certification only (test)", "bid_date": "2026-06-30",
         "clauses": [{"id": "1", "kind": "certification", "text": "ISO 9001", "params": {"standard": "ISO 9001"}},
                     {"id": "2", "kind": "other", "text": "EMD of Rs 5 lakh", "params": {}}]}
    ev = []
    case = qualify_tender(scratch, t, emit=ev.append, prefer_llm=False)
    cid = case["case"]["id"]
    plans = [e for e in ev if e["type"] == "plan"]
    assert plans[0]["stage"] == "initial" and plans[0]["skip"] == {"AbsenceAuditor": "no clause cites works or people"}
    assert plans[0]["routes"]["Verifier"] == ["1"] and plans[0]["routes"]["EvidenceHunter"] == ["1", "2"]
    # the ConflictSentinel's condition is checked once the evidence is chosen: an ISO certificate has no claim history
    assert plans[1]["stage"] == "checkpoint" and plans[1]["decision"]["ConflictSentinel"] == "skip"
    stored = json.loads(scratch.scalar("MATCH (c:Case {id:$c}) RETURN c.plan", {"c": cid}))
    assert set(stored["skip"]) == {"AbsenceAuditor", "ConflictSentinel"}
    assert _chain(scratch, cid) == ["ClauseParser", "Planner", "EvidenceHunter", "Verifier", "Assembler"]
    skipped = {e["agent"] for e in ev if e["type"] == "agent_done" and e.get("skipped")}
    assert skipped == {"AbsenceAuditor", "ConflictSentinel"}
    assert case["case"]["verdict"] == "BID"


def test_demo_tenders_keep_their_verdicts_with_the_planner(scratch):
    want = {"T-NHX-2026-014": "BID", "T-PHED-WTP-2027-03": "BID WITH FIXES", "T-JN-LB-2026-09": "NO-BID",
            "T-CWBB-2026-21": "NO-BID"}
    for tid, verdict in want.items():
        ev = []
        case = qualify_tender(scratch, _demo(tid), emit=ev.append, prefer_llm=False)
        assert case["case"]["verdict"] == verdict, tid
        assert ev[0]["type"] == "case_open" and any(e["type"] == "plan" for e in ev)
        assert _chain(scratch, case["case"]["id"]) == ["ClauseParser", "Planner", "EvidenceHunter", "AbsenceAuditor",
                                                       "ConflictSentinel", "Verifier", "Assembler"], tid
