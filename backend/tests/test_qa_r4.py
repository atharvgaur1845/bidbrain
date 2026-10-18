"""Round-4 Q&A agent and MCP: tool-free answers are re-asked, invented names and amounts are listed back,
prove_absence's missing items are always named, one model per answer survives cache hits and a server-refused
tool call, a finalize that answers nothing falls back to the deterministic answer, and MCP works a session's
cases and actions. No real LLM calls (providers.chat or the provider client is faked).

    cd backend && pytest -q tests/test_qa_r4.py
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolRequestParams

from bidbrain import config
from bidbrain.agents import loop, providers
from bidbrain.agents.prompts import BRIEF, QA_SYSTEM, QA_TOOLS
from bidbrain.agents.qa import parse_answer
from bidbrain.graph.client import Graph, db
from bidbrain.tools import TOOLS, call

PKG59 = "stormwaterdrainageodishapkg59"
TRISHAKTI = "Trishakti Power Generation Corporation"
Q_TRISHAKTI = "How many of the Trishakti project leads hold a PMP that is valid today?"


@pytest.fixture(scope="module")
def g():
    g = Graph()
    assert g.exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    return g


def _reply(content="", calls=(), provider="groq:model-a", malformed=False):
    return providers.Reply(content=content, tool_calls=list(calls), provider=provider, model=provider.split(":")[1],
                           usage={"prompt": 10, "completion": 5}, cached=False, ms=1.0, malformed=malformed)


def _call(name, args, i=0):
    return {"id": f"c{i}", "name": name, "arguments": json.dumps(args)}


def _script(monkeypatch, replies):
    """providers.chat returns `replies` in order (a callable gets the messages); records every request."""
    seen = []

    def fake_chat(messages, tools=None, prefer=None, **kw):
        seen.append([dict(m) for m in messages])
        r = replies[min(len(seen), len(replies)) - 1]
        return r(messages) if callable(r) else r
    monkeypatch.setattr(providers, "chat", fake_chat)
    return seen


def _qa(g, question, tools=QA_TOOLS, **kw):
    return loop.run_agent(g, "qa", QA_SYSTEM, question, tools, ground=True, **kw)


# ------------------------------------------------------------------ 1. tool-free answers

def test_needs_tools_is_about_entities_and_numbers():
    assert loop.needs_tools(Q_TRISHAKTI)
    assert loop.needs_tools("Trishakti leads with valid PMP")            # acronym
    assert loop.needs_tools("what is the total value of works for the water board")
    assert loop.needs_tools("Tell me about Pkg-59")
    assert not loop.needs_tools("hello")
    assert not loop.needs_tools("What can you do?")


def test_tool_free_answer_is_reasked_once(g, monkeypatch):
    seen = _script(monkeypatch, [
        _reply("Nobody qualifies.\nANSWER: 0"),
        _reply(calls=[_call("resolve_entity", {"text": "Trishakti", "kind": "client"})]),
        _reply(f"Resolved Trishakti to {TRISHAKTI}.\nANSWER: 2"),
    ])
    res = _qa(g, Q_TRISHAKTI)
    assert seen[1][-1]["content"] == loop.TOOL_FREE_REASK and seen[1][-2]["content"].endswith("ANSWER: 0")
    assert res.checks["reasked"] == ["tool_free"] and res.usage["tool_calls"] == 1
    assert parse_answer(res.final)[0] == 2 and "UNGROUNDED" not in (res.note or "")


def test_still_tool_free_is_marked_ungrounded(g, monkeypatch):
    seen = _script(monkeypatch, [_reply("ANSWER: 0")])
    res = _qa(g, Q_TRISHAKTI)
    assert len(seen) == 2 and res.usage["tool_calls"] == 0                # one re-ask, no more
    assert res.note.startswith("UNGROUNDED") and res.checks["ungrounded"] is True
    # a tool-free "0" is an unproven negative: flagged, and replaced by the deterministic uncertainty statement
    assert parse_answer(res.final)[0] == "NOT ESTABLISHED" and res.checks["answer_status"] == "NEEDS_REVIEW"
    # a question that names nothing and asks for no number is not re-asked
    seen = _script(monkeypatch, [_reply("I answer questions about the company graph.")])
    res = _qa(g, "hello")
    assert len(seen) == 1 and not res.note and not res.checks


def test_other_agents_are_not_checked(g, monkeypatch):
    seen = _script(monkeypatch, [_reply("ANSWER: 0")])
    res = loop.run_agent(g, "Assembler", "sys", Q_TRISHAKTI, ["who_knows"])
    assert len(seen) == 1 and res.final == "ANSWER: 0" and not res.note


# ------------------------------------------------------------------ 2. grounding

def test_ungrounded_is_lexical_and_generous():
    corpus = json.dumps({"result": {"works": [{"title": "Stormwater Drainage — Odisha Pkg-59", "value": 225800000,
                                               "client": "Public Health Engineering Dept, Odisha", "lead": "Meera Nair"}]}},
                        ensure_ascii=False) + "\nWhat is the value of works above ₹50 Cr for PHED Odisha?"
    ok = ("The PHED Odisha work Stormwater Drainage — Odisha Pkg-59 (₹22.58 Cr, i.e. ₹225,800,000) was led by "
          "Meera Nair for the Public Health Engineering Dept. Summing Meera Nair's works above ₹50 Cr gives "
          "nothing.\nANSWER: 225800000")
    assert loop.ungrounded(ok, corpus) == []
    bad = ("Other clients include State Power Corp and Central Grid Authority; the total is ₹99.99 Cr "
           "(DOC-FAKE-1).\nANSWER: 3")
    assert loop.ungrounded(bad, corpus) == ["State Power Corp", "Central Grid Authority", "DOC-FAKE-1", "₹99.99 Cr"]
    # verdict words, acronyms and capitalised concepts are not names
    assert loop.ungrounded("The verdict is DOES NOT QUALIFY. Open Case, Past Bid.\nANSWER: DOES NOT QUALIFY",
                           corpus) == []


def _portfolio(i=0):
    return _reply(calls=[_call("find_portfolio", {"client": "Trishakti"}, i)])


def test_invented_names_are_listed_back_once(g, monkeypatch):
    seen = _script(monkeypatch, [
        _portfolio(),
        _reply(f"{TRISHAKTI}, State Power Corp and Central Grid are the clients.\nANSWER: 19"),
        _reply(f"All 19 works are for {TRISHAKTI}.\nANSWER: 19"),
    ])
    res = _qa(g, "How many works did we complete for Trishakti?")
    ask = seen[2][-1]["content"]
    assert "do not appear in any tool result" in ask and "State Power Corp" in ask and "Central Grid" in ask
    assert TRISHAKTI not in ask
    assert res.checks["reasked"] == ["grounding"] and "UNVERIFIED" not in (res.note or "")
    assert parse_answer(res.final)[0] == 19


def test_invented_names_that_survive_are_flagged(g, monkeypatch):
    bad = f"{TRISHAKTI} and State Power Corp, total ₹999.99 Cr.\nANSWER: 19"
    seen = _script(monkeypatch, [_portfolio(), _reply(bad), _reply(bad)])
    res = _qa(g, "How many works did we complete for Trishakti?")
    assert len(seen) == 3                                                 # one grounding re-ask only
    assert "UNVERIFIED" in res.note and "State Power Corp" in res.note and "₹999.99 Cr" in res.note
    assert res.checks["unverified"] == ["State Power Corp", "₹999.99 Cr"]
    assert parse_answer(res.final)[0] == 19                               # the answer format is untouched


def test_a_failed_reask_keeps_the_answer(g, monkeypatch):
    def fail(messages):
        raise providers.LLMUnavailable("all LLM endpoints are rate-limited", "rate_limited")
    _script(monkeypatch, [_portfolio(), _reply("Then Central Grid Authority did it.\nANSWER: 19"), fail])
    res = _qa(g, "How many works did we complete for Trishakti?")
    assert res.error is None and parse_answer(res.final)[0] == 19
    assert "the re-ask failed" in res.note and "UNVERIFIED" in res.note


# ------------------------------------------------------------------ 3. absence answers name the items

def test_absence_answers_always_name_the_missing_items(g, monkeypatch):
    args = {"evidence": "reference_letter", "client": "Trishakti"}
    absent = call(g, "prove_absence", args)["result"]
    assert absent["absent_count"] > 0
    _script(monkeypatch, [_reply(calls=[_call("prove_absence", args)]),
                          _reply(f"{absent['absent_count']} works have no letter.\nANSWER: {absent['absent_count']}")])
    res = _qa(g, "Which Trishakti works have no reference letter?")
    lines = res.final.splitlines()
    block = next(i for i, ln in enumerate(lines) if ln.startswith("Missing (listed by prove_absence):"))
    assert block < max(i for i, ln in enumerate(lines) if "ANSWER:" in ln)          # above the ANSWER line
    assert all(a["title"] in res.final for a in absent["absent"])
    assert parse_answer(res.final)[0] == absent["absent_count"]
    assert res.checks["absent_named"] == absent["absent_count"]
    # an answer that already names them is left alone
    named = "; ".join(a["title"] for a in absent["absent"]) + f"\nANSWER: {absent['absent_count']}"
    assert loop.name_absent(named, {"result": absent}) == (named, 0)


def test_prompt_rules_and_tools():
    assert {"list_cases", "fact_as_of", "change_impact"} <= set(QA_TOOLS)
    assert "trace_from_credential" not in QA_TOOLS
    assert all(t in TOOLS and not TOOLS[t].writes for t in QA_TOOLS) and all(t in BRIEF for t in QA_TOOLS)
    for rule in ("list_cases", "past_bid", "PAST submission", "fact_as_of", "change_impact", "`absent` result by name",
                 "must come from a tool result", "without calling at least one tool", "ANSWER: <number or short text>"):
        assert rule in QA_SYSTEM, rule


# ------------------------------------------------------------------ 4. past bids, blast_radius summary

def test_bids_are_labelled_past_submissions(g):
    links = call(g, "get_entity", {"key": "railtunnelwestbengalpkg123", "kind": "work"})["result"]["links"]
    bids = [l for l in links if l["label"] == "Bid"]
    assert bids and all(l["kind"] == "past_bid" for l in bids)
    seen = json.loads(loop._for_model({"result": [{"hops": 2, "nodes": [{"label": "Bid", "key": "RFP-1"},
                                                                        {"label": "Work", "key": "w"}]}]}))
    assert seen["result"][0]["nodes"] == [{"label": "Bid", "key": "RFP-1", "kind": "past_bid"},
                                          {"label": "Work", "key": "w"}]


def test_blast_radius_gets_a_summary_line():
    r = {"subject": "Work:w", "cases_relying": [{"case_id": "CASE-1", "case": "Institutional Buildings", "clause_id": "k"}],
         "past_bids_citing": [{"bid": "RFP-1", "submitted": "2021-03-01"}], "tenders_secured": [],
         "clauses_rechecked": 4, "evidence_changed": [],
         "verdict_flips": [{"case": "Institutional Buildings", "case_id": "CASE-1", "clause": "C2", "from": "PASS",
                            "to": "FAIL"}]}
    seen = json.loads(loop._for_model({"result": r}, "blast_radius"))
    assert list(seen["result"])[0] == "summary"
    s = seen["result"]["summary"]
    assert s.startswith("1 open case(s) affected (verdict would flip): Institutional Buildings (C2 PASS -> FAIL)")
    assert "1 past bid(s) cited it (past submissions, not open bids)" in s and "4 open clause(s) re-checked" in s
    empty = loop.blast_summary({"verdict_flips": [], "cases_relying": [], "past_bids_citing": [], "clauses_rechecked": 0})
    assert empty.startswith("0 open case(s) affected")


# ------------------------------------------------------------------ 5. one model per answer

def _fake_client(log, answers, fail=None):
    def create(model, **kw):
        log.append(model)
        if fail and (exc := fail(model)):
            raise exc
        content, calls = answers.pop(0)
        tcs = [SimpleNamespace(id=c["id"], function=SimpleNamespace(name=c["name"], arguments=c["arguments"]))
               for c in calls] or None
        msg = SimpleNamespace(content=content, tool_calls=tcs)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _pool(monkeypatch, client, cached_for=()):
    """Three buckets; the cache answers only for the listed (bucket, number of messages) pairs."""
    monkeypatch.setattr(providers, "POOL", [("groq", "model-a"), ("groq", "model-b"), ("groq", "model-c")])
    monkeypatch.setattr(providers, "_client", lambda prov: client)
    monkeypatch.setattr(providers, "_parked", {})
    monkeypatch.setattr(providers, "_park_kind", {})
    monkeypatch.setattr(providers, "REPLAY", False)
    monkeypatch.setattr(providers, "MAX_WAIT", 10.0)
    monkeypatch.setattr(providers, "_key", lambda bucket, messages, *a: f"{bucket}|{len(messages)}")

    def get(k):
        b, n = k.split("|")
        if (b, int(n)) in cached_for:
            return dict(content="ANSWER: from the cache", tool_calls=[], provider=b, model=b.split(":")[1], usage={})
        return None
    monkeypatch.setattr(providers, "_cache", SimpleNamespace(get=get, put=lambda k, v: None))


def test_pin_holds_under_cache_hits_from_other_buckets(g, monkeypatch):
    log = []
    client = _fake_client(log, [("", [_call("graph_schema", {})]), ("Read the schema.\nANSWER: 1", [])])
    _pool(monkeypatch, client, cached_for={("groq:model-a", 2), ("groq:model-c", 4)})
    # model-a has a cached first step, but it is out of service beyond the wait: pinning the run to it would force a
    # switch at step 2, so its cache entry is skipped and the run starts live on model-b
    providers._parked["groq:model-a"], providers._park_kind["groq:model-a"] = providers.time.time() + 3600, "auth"
    res = loop.run_agent(g, "qa", "sys", "q", ["graph_schema"])
    assert log == ["model-b", "model-b"]                                  # model-c's cached step 2 is never used
    assert res.providers == ["groq:model-b", "groq:model-b"] and res.switches == [] and not res.note
    assert parse_answer(res.final)[0] == 1


def test_a_server_refused_tool_call_stays_on_the_pinned_model(g, monkeypatch):
    class ToolUseFailed(Exception):
        status_code = 400
        body = {"message": "Failed to call a function. Please adjust your prompt.", "type": "invalid_request_error",
                "code": "tool_use_failed", "failed_generation": "<tool_call>{\"name\": graph_schema}</tool_call>"}

    log, refused = [], []

    def fail(model):
        if model == "model-b" and len(log) == 2 and not refused:
            refused.append(1)
            return ToolUseFailed("400")
    client = _fake_client(log, [("", [_call("graph_schema", {})]), ("", [_call("graph_schema", {}, 1)]),
                                ("Done.\nANSWER: 2", [])], fail)
    _pool(monkeypatch, client)
    providers._parked["groq:model-a"] = providers.time.time() + 3600
    res = loop.run_agent(g, "qa", "sys", "q", ["graph_schema"])
    # step 2's tool call was refused by the server: the same model is re-asked, no other model serves
    assert log == ["model-b", "model-b", "model-b", "model-b"] and res.switches == []
    assert "re-asked after a malformed tool call" in res.note and parse_answer(res.final)[0] == 2
    assert {p for p in res.providers} == {"groq:model-b"}


def test_every_switch_is_in_the_note(g, monkeypatch):
    replies = iter([_reply(calls=[_call("graph_schema", {})], provider="groq:model-b"),
                    providers.Reply(content="ANSWER: 1", tool_calls=[], provider="groq:model-a", model="model-a",
                                    usage={}, cached=False, ms=1.0, switched="groq:model-b rate_limited for 90s more")])
    monkeypatch.setattr(providers, "chat", lambda *a, **k: next(replies))
    res = loop.run_agent(g, "qa", "sys", "q", ["graph_schema"])
    assert res.switches[0]["why"] == "groq:model-b rate_limited for 90s more"
    assert res.note == "model switch at step 2: groq:model-b -> groq:model-a (groq:model-b rate_limited for 90s more)"


# ------------------------------------------------------------------ 6. finalize fallback

def _aggregate_until_finalize(monkeypatch, finalize):
    def fake_chat(messages, tools=None, prefer=None, **kw):
        if messages[-1].get("content") == loop.FINALIZE:
            return finalize(messages)
        return _reply(calls=[_call("aggregate", {"work_keys": [PKG59], "op": "sum"})])
    monkeypatch.setattr(providers, "chat", fake_chat)


def test_a_finalize_that_answers_nothing_falls_back_to_the_tool_result(g, monkeypatch):
    _aggregate_until_finalize(monkeypatch, lambda m: _reply("I don't have enough information to answer.\nANSWER: unknown"))
    ev = []
    res = loop.run_agent(g, "qa", "sys", "What is the value of Pkg-59?", ["aggregate"], max_steps=2, emit=ev.append)
    assert parse_answer(res.final)[0] == 225800000 and "not interpreted by a model" in res.final
    assert "deterministic answer" in res.note and res.error == "step budget exhausted"
    assert [e for e in ev if e["type"] == "final"][-1]["deterministic"]


def test_a_failing_finalize_falls_back_too(g, monkeypatch):
    def fail(messages):
        raise providers.LLMUnavailable("no LLM endpoint answered", "unavailable")
    _aggregate_until_finalize(monkeypatch, fail)
    res = _qa(g, "What is the value of Pkg-59?", tools=["aggregate"], max_steps=2)
    assert parse_answer(res.final)[0] == 225800000 and "deterministic" in res.note


def test_a_real_finalize_answer_is_kept_and_checked(g, monkeypatch):
    _aggregate_until_finalize(monkeypatch, lambda m: _reply("Summed Pkg-59.\nANSWER: 225800000"))
    res = _qa(g, "What is the value of Pkg-59?", tools=["aggregate"], max_steps=2)
    assert parse_answer(res.final)[0] == 225800000 and res.error is None
    assert res.note.startswith("step budget exhausted; answered from the results")
    assert loop.non_answer("Not enough information.") and loop.non_answer("ANSWER: unknown")
    assert loop.non_answer("<tool_call>{}</tool_call>") and not loop.non_answer("ANSWER: 0")
    assert not loop.non_answer("ANSWER: ISRO is not in the company graph")


# ------------------------------------------------------------------ 7. MCP

def _mcp(name, args):
    from bidbrain.mcp.server import server
    return asyncio.run(server._handle_call_tool(None, CallToolRequestParams(name=name, arguments=args)))


def _payload(res):
    return res.structured_content or json.loads(res.content[0].text)


def test_mcp_unknown_arguments_are_errors():
    res = _mcp("get_entity", {"key": "Pkg-113", "kind": "work", "bogus": 1})
    assert res.is_error and "bogus" in res.content[0].text
    res = _mcp("find_portfolio", {"client": "Trishakti", "category": "roads"})      # 'categories' is the argument
    assert res.is_error and "category" in res.content[0].text
    res = _mcp("qualify_tender", {"session_id": "new", "tender_id": "T-JN-LB-2026-09", "dry_run": True})
    assert res.is_error and "dry_run" in res.content[0].text
    assert not _mcp("get_entity", {"key": "Pkg-113", "kind": "work"}).is_error


def test_mcp_bad_tender_id_creates_no_session():
    before = set(db().list_graphs())
    for sid in ("pytestmcpr4bad", "new"):
        res = _mcp("qualify_tender", {"session_id": sid, "tender_id": "T-NOPE"})
        assert res.is_error and "unknown tender_id" in res.content[0].text
    res = _mcp("qualify_tender", {"session_id": "new", "tender_id": "T-JN-LB-2026-09", "bid_date": "soon"})
    assert res.is_error and "bid_date" in res.content[0].text
    assert set(db().list_graphs()) == before


def test_mcp_session_reads_and_actions(g):
    from bidbrain.company import actions as A
    sid = "pytestmcpr4"
    base_cases = g.scalar("MATCH (c:Case) RETURN count(c)")
    try:
        out = _payload(_mcp("qualify_tender", {"session_id": sid, "tender_id": "T-CWBB-2026-21", "use_llm": False}))
        assert out["graph"] == f"session_{sid}" and isinstance(out["actions"], list)
        case_id = out["case_id"]
        # reads on the session: the case qualify_tender wrote
        cases = _payload(_mcp("list_cases", {"session_id": sid}))["result"]["cases"]
        assert [c["case_id"] for c in cases] == [case_id] and cases[0]["recommendation"] == out["overall"]
        assert cases[0]["kind"] == "case" and "open_findings" in cases[0]
        got = _payload(_mcp("case_get", {"session_id": sid, "case_id": case_id}))["result"]
        assert got["case"]["id"] == case_id and got["clauses"]
        assert _mcp("case_get", {"case_id": case_id}).is_error                       # not on the base graph
        work = next(s["key"] for c in got["clauses"] for s in c["satisfied_by"] if s["label"] == "Work")
        br = _payload(_mcp("blast_radius", {"session_id": sid, "label": "Work", "key": work}))["result"]
        assert case_id in {c["case_id"] for c in br["cases_relying"]}
        # the action queue: list, approve, execute (an internal request; nothing is sent)
        sg = Graph(f"session_{sid}")
        if not out["actions"]:
            A.ensure(sg, "request_reference_letter", case_id=case_id, target=work, title="Request a letter",
                     detail="test", payload={"work": work})
        acts = _payload(_mcp("list_actions", {"session_id": sid}))["actions"]
        act = next(a for a in acts if a["kind"] in ("request_reference_letter", "renew_credential"))
        assert act["status"] == "pending"
        res = _mcp("transition_action", {"session_id": sid, "action_id": act["id"], "op": "frobnicate"})
        assert res.is_error
        res = _mcp("transition_action", {"session_id": sid, "action_id": act["id"], "op": "execute"})
        assert res.is_error and "only an approved action" in res.content[0].text
        a = _payload(_mcp("transition_action", {"session_id": sid, "action_id": act["id"], "op": "approve"}))
        assert a["status"] == "approved" and "(via MCP)" in a["decided_by"]
        a = _payload(_mcp("transition_action", {"session_id": sid, "action_id": act["id"], "op": "execute"}))
        assert a["status"] == "executed" and [e["to"] for e in a["events"]][-1] == "executed"
        assert _mcp("transition_action", {"session_id": sid, "action_id": "ACT-nope", "op": "approve"}).is_error
        assert g.scalar("MATCH (c:Case) RETURN count(c)") == base_cases                 # base graph untouched
        assert g.scalar("MATCH (a:Action) RETURN count(a)") == 0
    finally:
        Graph(f"session_{sid}").delete()
    # reads and actions never create a session, and never take the base graph's name
    for bad in ("pytestmcpr4none", config.BASE_GRAPH, "../x"):
        res = _mcp("list_cases", {"session_id": bad})
        assert res.is_error and "session" in res.content[0].text, bad
        assert _mcp("list_actions", {"session_id": bad}).is_error
    assert not Graph("session_pytestmcpr4none").exists()


def test_qa_ask_reports_its_checks(g, monkeypatch):
    from bidbrain.agents import qa
    _script(monkeypatch, [_reply("ANSWER: 0")])
    out = qa.ask(g, Q_TRISHAKTI)
    assert out["checks"]["ungrounded"] is True and out["note"].startswith("UNGROUNDED") and out["answer"] == "NOT ESTABLISHED"
    assert out["checks"]["answer_status"] == "NEEDS_REVIEW"
