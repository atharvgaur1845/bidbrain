"""Round-3 Q&A agent and MCP robustness: answers survive rate limits, provider errors are redacted,
unknown entities are reported as not found, the agent's tool set covers 'what breaks', and MCP tool
errors are MCP errors. No real LLM calls (providers.chat is faked).

    cd backend && pytest -q tests/test_qa_r3.py
"""
from __future__ import annotations

import asyncio
import json

import pytest
from mcp.types import CallToolRequestParams

from bidbrain import config
from bidbrain.agents import loop, providers
from bidbrain.agents.prompts import BRIEF, QA_SYSTEM, QA_TOOLS
from bidbrain.agents.qa import parse_answer
from bidbrain.graph.client import Graph
from bidbrain.tools import TOOLS, call

ORG = "org_01hzyxwvutsrqp9876"
GROQ_429 = (f"Error code: 429 - {{'error': {{'message': 'Rate limit reached for model `openai/gpt-oss-120b` in "
            f"organization `{ORG}` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 7000, "
            f"Requested 2400. Please try again in 31.2s. Upgrade at https://console.groq.com/settings/billing'}}}}")


@pytest.fixture(scope="module")
def g():
    g = Graph()
    assert g.exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    return g


def _reply(content="", calls=(), provider="groq:model-a"):
    return providers.Reply(content=content, tool_calls=list(calls), provider=provider, model=provider.split(":")[1],
                           usage={"prompt": 10, "completion": 5}, cached=False, ms=1.0)


def _call(name, args, i=0):
    return {"id": f"c{i}", "name": name, "arguments": json.dumps(args)}


def _portfolio_then(monkeypatch, after):
    """Step 1 calls find_portfolio + aggregate; every later chat call is `after(n, messages)`."""
    n = [0]

    def fake_chat(messages, tools=None, prefer=None, **kw):
        n[0] += 1
        if n[0] == 1:
            return _reply(calls=[_call("aggregate", {"work_keys": ["stormwaterdrainageodishapkg59"], "op": "sum"})])
        return after(n[0], messages)
    monkeypatch.setattr(providers, "chat", fake_chat)
    return n


# ------------------------------------------------------------------ rate limits never discard the answer

def test_rate_limit_mid_run_finalizes_from_the_results(g, monkeypatch):
    def after(n, messages):
        if n == 2:
            raise providers.LLMUnavailable(GROQ_429, "rate_limited")
        assert messages[-1]["content"] == loop.FINALIZE          # the tool-free answer call
        return _reply("summed the work.\nANSWER: 225800000")
    _portfolio_then(monkeypatch, after)
    ev = []
    res = loop.run_agent(g, "qa", "sys", "What is the value of Pkg-59?", ["aggregate"], emit=ev.append)
    assert res.error is None and parse_answer(res.final)[0] == 225800000
    assert res.note.startswith("LLM rate limited mid-run")
    assert not [e for e in ev if e["type"] == "error"]               # a warning, not a failed answer
    assert ORG not in json.dumps(ev) and "http" not in json.dumps(ev)


def test_finalize_failure_falls_back_to_the_last_tool_result(g, monkeypatch):
    def after(n, messages):
        raise providers.LLMUnavailable(GROQ_429, "rate_limited")
    _portfolio_then(monkeypatch, after)
    ev = []
    res = loop.run_agent(g, "qa", "sys", "What is the value of Pkg-59?", ["aggregate"], emit=ev.append)
    assert parse_answer(res.final)[0] == 225800000                     # the tool's own value
    assert "not interpreted by a model" in res.final and "Last step: aggregate" in res.final
    assert "deterministic" in res.note and res.error                   # the Assembler still sees a failure
    final = [e for e in ev if e["type"] == "final"][0]
    assert final["deterministic"] and final["provider"] == "deterministic"
    blob = json.dumps(ev) + res.error + res.final
    assert ORG not in blob and "console.groq.com" not in blob and "31.2s" in res.error


def test_a_failure_on_the_first_step_is_an_error_without_an_answer(g, monkeypatch):
    def fake_chat(messages, tools=None, prefer=None, **kw):
        raise providers.LLMUnavailable(GROQ_429, "rate_limited")
    monkeypatch.setattr(providers, "chat", fake_chat)
    ev = []
    res = loop.run_agent(g, "qa", "sys", "q", ["aggregate"], emit=ev.append)
    assert res.final == "" and res.error and ORG not in res.error
    assert [e["type"] for e in ev] == ["agent_start", "error"]


def test_fallback_answer_names_the_missing_items():
    out = {"result": {"claim": "2 of 3 works in scope have no reference letter on file", "verdict": "UNPROVEN",
                      "absent_count": 2, "absent": [{"key": "a", "title": "Work A"}, {"key": "b", "title": "Work B"}]},
           "evidence": [{"doc_id": "DOC-1", "page": 2}]}
    text = loop.fallback_answer({"tool": "prove_absence", "args": {}, "out": out}, "rate limited")
    assert "Missing: Work A; Work B" in text and "(verdict UNPROVEN)" in text and "DOC-1 p.2" in text
    assert parse_answer(text)[0] == 2
    assert loop.fallback_answer(None, "x") is None


def test_redact_strips_ids_keys_and_urls():
    s = providers.redact(GROQ_429 + " x-request-id: req_01abcdefghij key gsk_ABCDEFGHIJKLMNOPQRST "
                         "Bearer sk-live-123 9b2f6a1c-0d4e-4b7a-9c3e-1234567890ab")
    for leak in (ORG, "req_01", "gsk_", "sk-live", "https://", "9b2f6a1c"):
        assert leak not in s, leak
    assert "tokens per minute" in s
    assert ORG not in str(providers.LLMUnavailable(GROQ_429, "rate_limited"))

    class E(Exception):
        status_code = 429
    assert providers._describe("groq:openai/gpt-oss-120b", E(GROQ_429)) == \
        "groq:openai/gpt-oss-120b: rate_limited (HTTP 429): retry in 31s"


# ------------------------------------------------------------------ resolve_entity says not found

def test_resolve_entity_reports_not_found(g):
    out = call(g, "resolve_entity", {"text": "ISRO", "kind": "client"})
    assert out["result"] == [] and out["not_found"] is True and len(out["nearest"]) == 3
    assert all(n["score"] < 70 for n in out["nearest"])
    # a two-letter alias ('ai') inside the query is not a match either
    assert call(g, "resolve_entity", {"text": "NHAI", "kind": "client"})["not_found"]
    assert call(g, "resolve_entity", {"text": "Pkg-999", "kind": "work"})["not_found"]
    # informal forms that do exist still resolve
    r = call(g, "resolve_entity", {"text": "phed odisha", "kind": "client"})["result"]
    assert r[0]["name"] == "Public Health Engineering Dept, Odisha" and r[0]["score"] == 100
    assert call(g, "resolve_entity", {"text": "Trishakti", "kind": "client"})["result"][0]["name"] == \
        "Trishakti Power Generation Corporation"
    assert call(g, "resolve_entity", {"text": "neda", "kind": "client"})["result"][0]["score"] == 100
    # the other tools refuse an unknown name with the same words
    err = call(g, "find_portfolio", {"client": "NHAI"})["error"]
    assert "no client named like 'NHAI' in the graph" in err


def test_the_model_sees_not_found(g):
    out = call(g, "resolve_entity", {"text": "ISRO", "kind": "client"})
    seen = json.loads(loop._for_model(out))
    assert seen["not_found"] is True and seen["nearest"] and seen["result"] == []
    assert loop._summary(out).startswith("not found in the graph")


def test_fact_history_still_works(g):
    out = call(g, "fact_history", {"key": "Pkg-113", "kind": "work"})
    assert "error" not in out and out["result"]["subject"].startswith("Work:")


# ------------------------------------------------------------------ prompt rules and tool set

def test_qa_tools_cover_what_breaks_and_case_reads():
    assert {"blast_radius", "case_get", "fact_history"} <= set(QA_TOOLS)
    assert all(t in TOOLS and not TOOLS[t].writes for t in QA_TOOLS)
    assert all(t in BRIEF for t in QA_TOOLS)
    for rule in ("quote it verbatim", "UNPROVEN", "name the missing items", "fact_history", "blast_radius",
                 "not in the company graph", "ANSWER: <number or short text>"):
        assert rule in QA_SYSTEM, rule


def test_blast_radius_reads_only(g):
    # run on the base graph itself: every query in it goes through Graph.read (GRAPH.RO_QUERY), which the server
    # would reject if it wrote anything
    key = call(g, "resolve_entity", {"text": "Pkg-113", "kind": "work"})["result"][0]["key"]
    before = g.scalar("MATCH (n) RETURN count(n)")
    out = call(g, "blast_radius", {"label": "Work", "key": key})
    assert "error" not in out and out["result"]["subject"] == f"Work:{key}"
    assert g.scalar("MATCH (n) RETURN count(n)") == before


def test_agent_rejects_an_unknown_doc_type(g, monkeypatch):
    seen = []

    def fake_chat(messages, tools=None, prefer=None, **kw):
        seen.append(messages)
        if len(seen) == 1:
            return _reply(calls=[_call("vector_search", {"text": "safety", "doc_type": "letters"})])
        return _reply("ANSWER: none")
    monkeypatch.setattr(providers, "chat", fake_chat)
    res = loop.run_agent(g, "qa", "sys", "q", ["vector_search"])
    msg = [m for m in seen[1] if m["role"] == "tool"][-1]["content"]
    assert "unknown doc_type 'letters'" in msg and "reference_letter" in msg and res.steps[0]["error"]


def test_parse_answer_unchanged():
    assert parse_answer("path\n**ANSWER:** 2,942,400,000 (₹294.24 Cr)")[0] == 2942400000
    assert parse_answer("ANSWER: DOES NOT QUALIFY")[0] == "DOES NOT QUALIFY"
    assert parse_answer("ANSWER: ISRO is not in the company graph")[0] == "ISRO is not in the company graph"


# ------------------------------------------------------------------ MCP

def _mcp(name, args):
    from bidbrain.mcp.server import server
    return asyncio.run(server._handle_call_tool(None, CallToolRequestParams(name=name, arguments=args)))


def _payload(res):
    return res.structured_content or json.loads(res.content[0].text)


def test_mcp_tool_list_and_annotations():
    from bidbrain.mcp.server import server
    tools = asyncio.run(server.list_tools())
    acting = [t.name for t in tools if not t.annotations.read_only_hint]
    assert acting == ["qualify_tender"]
    assert len(tools) == sum(1 for t in TOOLS.values() if not t.writes) + 1


def test_mcp_errors_are_mcp_errors():
    res = _mcp("get_entity", {"key": "no such work", "kind": "work"})
    assert res.is_error and "no work named like" in res.content[0].text
    res = _mcp("vector_search", {"text": "safety", "doc_type": "letters"})
    assert res.is_error and "unknown doc_type 'letters'" in res.content[0].text and "reference_letter" in res.content[0].text
    res = _mcp("aggregate", {"work_keys": ["nope"], "op": "frobnicate"})
    assert res.is_error and "unknown op" in res.content[0].text


def test_mcp_passes_warnings_through():
    out = _payload(_mcp("aggregate", {"work_keys": ["nope", "stormwaterdrainageodishapkg59"], "op": "sum"}))
    assert out["missing_keys"] == ["nope"] and out["result"]["value"] == 225800000
    assert "subgraph" not in out and "cypher" not in out
    out = _payload(_mcp("resolve_entity", {"text": "ISRO", "kind": "client"}))
    assert out["not_found"] is True and out["nearest"]


def test_mcp_qualify_tender_acts_on_a_session_graph_only(g):
    sid = "pytestmcpr3"
    base_cases = g.scalar("MATCH (c:Case) RETURN count(c)")
    try:
        res = _mcp("qualify_tender", {"session_id": sid, "tender_id": "T-JN-LB-2026-09", "use_llm": False})
        assert not res.is_error, res.content
        out = _payload(res)
        assert out["session_id"] == sid and out["graph"] == f"session_{sid}" and out["overall"] == "NO-BID"
        assert out["case_id"] and out["clauses"] and all(c["verdict"] for c in out["clauses"])
        assert Graph(f"session_{sid}").scalar("MATCH (c:Case {id:$c}) RETURN count(c)", {"c": out["case_id"]}) == 1
        assert g.scalar("MATCH (c:Case) RETURN count(c)") == base_cases           # base graph untouched
    finally:
        Graph(f"session_{sid}").delete()
    # a session id that is not one (e.g. the base graph's name) is refused before anything runs
    for bad in (config.BASE_GRAPH, "../x", "ABC"):
        res = _mcp("qualify_tender", {"session_id": bad, "tender_id": "T-JN-LB-2026-09", "use_llm": False})
        assert res.is_error and "session_id" in res.content[0].text, bad
    res = _mcp("qualify_tender", {"session_id": sid, "tender_id": "T-NOPE"})
    assert res.is_error and "unknown tender_id" in res.content[0].text
    assert not Graph(f"session_{sid}").exists() or Graph(f"session_{sid}").delete() is None
