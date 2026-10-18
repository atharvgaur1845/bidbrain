"""Closeout Q&A checks: a negative ("none", "not affected", "0") is stated only when this run proved it, every
answer gets `checks.answer_status`, prove_absence's list replaces the answer's own, repeated calls are free,
an HTTP 413 shrinks the request and keeps the model, the fallback is a sentence, and a past fact_as_of gets
a now-vs-then line. The LLM is always stubbed; some tool results are stubbed to build the failure case.
Every test checks the FINAL natural-language answer.

    cd backend && pytest -q tests/test_qa_closeout.py
"""
from __future__ import annotations

import json
import re

import pytest

from bidbrain.agents import absence as A
from bidbrain.agents import loop, providers
from bidbrain.agents.prompts import QA_SYSTEM, QA_TOOLS
from bidbrain.agents.qa import parse_answer
from bidbrain.graph.client import Graph
from bidbrain.tools import call as real_call


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


def _script(monkeypatch, replies):
    seen = []

    def fake_chat(messages, tools=None, prefer=None, **kw):
        seen.append({"messages": [dict(m) for m in messages], "prefer": prefer})
        r = replies[min(len(seen), len(replies)) - 1]
        return r(messages, prefer) if callable(r) else r
    monkeypatch.setattr(providers, "chat", fake_chat)
    return seen


def _tools(monkeypatch, stubs):
    """loop.call answers from `stubs` (name -> out or callable(args)); other tools hit the real graph."""
    def fake(g, name, args):
        if name in stubs:
            s = stubs[name]
            return s(args) if callable(s) else json.loads(json.dumps(s))
        return real_call(g, name, args)
    monkeypatch.setattr(loop, "call", fake)


def _qa(g, q, **kw):
    return loop.run_agent(g, "qa", QA_SYSTEM, q, QA_TOOLS, ground=True, **kw)


def _no_negative(text):
    assert not A.negatives(text), A.negatives(text)


# ── 1. empty retrieval ─────────────────────────────────────────────────────

def test_empty_retrieval_is_no_match_not_none(g, monkeypatch):
    _tools(monkeypatch, {"vector_search": {"result": []}})
    neg = _reply("Searched the documents: there are no documents about tunnel ventilation.\nANSWER: none")
    seen = _script(monkeypatch, [_reply(calls=[_call("vector_search", {"query": "tunnel ventilation"})]), neg, neg])
    res = _qa(g, "Do we have any documents on tunnel ventilation?")
    assert res.checks["reasked"] == ["absence"] and "prove_absence" in seen[2]["messages"][-1]["content"]
    _no_negative(res.final)
    assert "Not established" in res.final and "empty result from vector_search" in res.final
    assert parse_answer(res.final)[0] == "NOT ESTABLISHED"
    assert res.checks["answer_status"] == A.NO_MATCH and "UNPROVEN NEGATIVE" in res.note


# ── 2. orphan document (prove_absence UNPROVEN: an unlinked letter mentions the work) ──

def _absence(verdict, absent, checks, universe=5):
    return {"result": {"claim": f"{len(absent)} of {universe} works in scope have no reference letter on file",
                       "verdict": verdict, "universe": universe, "present": universe - len(absent),
                       "absent_count": len(absent), "absent": absent, "checks": checks,
                       "scope": {"client": "trishaktipowergenerationcorporation"}},
            "evidence": [], "subgraph": {"nodes": [], "edges": []}}


ORPHAN = _absence("UNPROVEN", [{"key": "w7", "title": "Substation Civil Works Pkg-7", "orphan_search_hits": 1}],
                  {"boundary_attested_by_3_views": "5/5", "corpus_closure": "42/42 letters linked",
                   "orphan_search": "possible unlinked letter found"})


def test_orphan_document_keeps_the_negative_unproven(g, monkeypatch):
    _tools(monkeypatch, {"prove_absence": ORPHAN})
    args = {"evidence": "reference_letter", "client": "Trishakti"}
    ans = _reply("Pkg-7 has no reference letter on file.\nANSWER: 1")
    _script(monkeypatch, [_reply(calls=[_call("prove_absence", args)]), ans, ans])
    res = _qa(g, "Which Trishakti works have no reference letter?")
    assert "has no reference letter" not in res.final
    assert "prove_absence returned UNPROVEN" in res.final and "possible unlinked letter found" in res.final
    assert "Substation Civil Works Pkg-7" in res.final                    # still listed, from the proof
    assert parse_answer(res.final)[0] == 1
    assert res.checks["answer_status"] == A.INCOMPLETE


# ── 3. incomplete corpus coverage ──────────────────────────────────────────

def test_incomplete_corpus_coverage_is_not_a_verified_absence(g, monkeypatch):
    out = _absence("UNPROVEN", [], {"boundary_attested_by_3_views": "5/5", "corpus_closure": "40/42 letters linked",
                                    "orphan_search": "no unlinked letter mentions a missing work"})
    _tools(monkeypatch, {"prove_absence": out})
    ans = _reply("Every work has its letter; none are missing.\nANSWER: 0")
    _script(monkeypatch, [_reply(calls=[_call("prove_absence", {"evidence": "reference_letter", "client": "Trishakti"})]),
                          ans, ans])
    res = _qa(g, "How many Trishakti works lack a reference letter?")
    _no_negative(res.final)
    assert "40/42 letters linked" in res.final and parse_answer(res.final)[0] == "NOT ESTABLISHED"
    assert res.checks["answer_status"] == A.INCOMPLETE


# ── 4. ambiguous alias ─────────────────────────────────────────────────────

def test_ambiguous_alias_is_entity_unresolved(g, monkeypatch):
    ans = _reply("Jal Nigam has no works with us.\nANSWER: 0")
    _script(monkeypatch, [_reply(calls=[_call("resolve_entity", {"text": "Jal Nigam", "kind": "client"})]), ans, ans])
    res = _qa(g, "How many works did we do for Jal Nigam?")
    _no_negative(res.final)
    assert "'jal nigam' (ambiguous)" in res.final and "did not resolve to one graph entity" in res.final
    assert res.checks["answer_status"] == A.ENTITY_UNRESOLVED


def test_narrowed_alias_resolves(g, monkeypatch):
    _script(monkeypatch, [_reply(calls=[_call("resolve_entity", {"text": "PHED", "kind": "client"})]),
                          _reply(calls=[_call("find_portfolio", {"client": "PHED Odisha"}, 1)]),
                          lambda m, p: _reply("Counted the works.\nANSWER: " + str(
                              json.loads(m[-1]["content"])["result"]["n"]))])
    res = _qa(g, "How many works did we do for PHED Odisha?")
    assert res.checks["answer_status"] == A.GROUNDED and "Not resolved" not in res.final


# ── 5. disconnected record (a credential with no holder: closure fails) ────

def test_disconnected_record_blocks_a_credential_absence(g, monkeypatch):
    out = {"result": {"claim": "2 of 6 project leads in scope hold no PMP valid on 2026-03-31", "verdict": "UNPROVEN",
                      "universe": 6, "present": 4, "absent_count": 2,
                      "absent": [{"key": "p1", "name": "Ravi Kumar"}, {"key": "p2", "name": "Anita Rao"}],
                      "checks": {"credential_closure": "41/42 PMP credentials linked to a holder",
                                 "dated": "42/42 credentials dated"}}, "evidence": []}
    _tools(monkeypatch, {"prove_absence": out})
    ans = _reply("Ravi Kumar and Anita Rao hold no valid PMP.\nANSWER: 2")
    _script(monkeypatch, [_reply(calls=[_call("prove_absence", {"evidence": "valid_credential", "client": "Trishakti"})]),
                          ans, ans])
    res = _qa(g, "Which Trishakti leads have no valid PMP?")
    assert "hold no valid PMP" not in res.final and "41/42 PMP credentials linked" in res.final
    assert "Ravi Kumar" in res.final and "Anita Rao" in res.final
    assert res.checks["answer_status"] == A.INCOMPLETE


# ── 6. unsupported entity name ─────────────────────────────────────────────

def test_unknown_entity_says_not_in_graph(g, monkeypatch):
    ans = _reply("Acme Tunnels Pvt is not in the company graph.\nANSWER: Acme Tunnels Pvt is not in the company graph")
    _script(monkeypatch, [_reply(calls=[_call("resolve_entity", {"text": "Acme Tunnels Pvt", "kind": "client"})]), ans])
    res = _qa(g, "What did we invoice Acme Tunnels Pvt?")
    assert "is not in the company graph" in res.final and "Not established" not in res.final
    assert "Not resolved: 'acme tunnels pvt' (not found)" in res.final
    assert res.checks["answer_status"] == A.ENTITY_UNRESOLVED and "reasked" not in res.checks


# ── 7. Person blast_radius empty while past bids exist ─────────────────────

def test_person_blast_radius_empty_is_not_none(g, monkeypatch):
    person = real_call(g, "resolve_entity", {"text": "a", "kind": "person"})
    who = g.read("MATCH (p:Person)<-[:LED_BY]-(:Work)<-[:CITES]-(b:Bid) RETURN p.key AS k, p.name AS n LIMIT 1")
    assert who, "the base graph has a lead whose work a past bid cites"
    key, name = who[0]["k"], who[0]["n"]
    _tools(monkeypatch, {"blast_radius": {"result": {"subject": f"Person:{key}", "cases_relying": [],
                                                     "past_bids_citing": [], "verdict_flips": [],
                                                     "clauses_rechecked": 0, "tenders_secured": []}}})
    ans = _reply("blast_radius shows nothing relies on them: no open tenders would be hurt.\nANSWER: none")
    seen = _script(monkeypatch, [_reply(calls=[_call("resolve_entity", {"text": name, "kind": "person"})]),
                                 _reply(calls=[_call("blast_radius", {"label": "Person", "key": key}, 1)]), ans, ans])
    res = _qa(g, f"If {name} left, which open tenders would be hurt?")
    assert person is not None and res.checks["reasked"] == ["absence"]
    _no_negative(res.final)
    assert not re.search(r"\bnone\b|no open tenders", res.final, re.I)
    assert "Not established" in res.final and res.checks["answer_status"] == A.NO_MATCH
    assert "prove_absence" in seen[3]["messages"][-1]["content"]


def test_an_unrelated_scoped_set_does_not_back_a_tender_negative(g, monkeypatch):
    """Seen live: blast_radius empty, then find_portfolio for the person; 'no open tenders' is still unproven,
    and only list_cases (the workspace's cases) can back it."""
    who = g.read("MATCH (p:Person)<-[:LED_BY]-(:Work)<-[:CITES]-(b:Bid) RETURN p.key AS k, p.name AS n LIMIT 1")[0]
    _tools(monkeypatch, {"blast_radius": {"result": {"cases_relying": [], "past_bids_citing": [], "verdict_flips": []}}})
    ans = _reply("No open tenders would be hurt.\nANSWER: none")
    _script(monkeypatch, [_reply(calls=[_call("blast_radius", {"label": "Person", "key": who["k"]})]),
                          _reply(calls=[_call("find_portfolio", {"person": who["n"]}, 1)]), ans, ans])
    res = _qa(g, f"If {who['n']} left, which open tenders would be hurt?")
    assert "No open tenders" not in res.final and res.checks["answer_status"] == A.NO_MATCH
    _script(monkeypatch, [_reply(calls=[_call("blast_radius", {"label": "Person", "key": who["k"]})]),
                          _reply(calls=[_call("list_cases", {}, 1)]), ans])
    res = _qa(g, f"If {who['n']} left, which open tenders would be hurt?")
    assert "No open tenders" in res.final and "scoped count via list_cases" in res.final
    assert "this workspace's bid cases" in res.final and res.checks["answer_status"] == A.VERIFIED_ABSENCE


# ── 8. contradictory absence table ─────────────────────────────────────────

def test_absence_table_is_rebuilt_from_the_proof(g, monkeypatch):
    out = _absence("PROVEN", [{"key": "wa", "title": "Canal Lining Pkg-11", "orphan_search_hits": 0},
                              {"key": "wb", "title": "Pump House Pkg-12", "orphan_search_hits": 0}],
                   {"boundary_attested_by_3_views": "5/5", "corpus_closure": "42/42 letters linked",
                    "orphan_search": "no unlinked letter mentions a missing work"})
    _tools(monkeypatch, {"prove_absence": out})
    table = ("Two works have no reference letter:\n| Work | Letter |\n|---|---|\n| Canal Lining Pkg-11 | missing |\n"
             "| Barrage Gates Pkg-13 | missing |\nANSWER: 2")
    _script(monkeypatch, [_reply(calls=[_call("prove_absence", {"evidence": "reference_letter", "client": "Trishakti"})]),
                          _reply(table)])
    res = _qa(g, "Which Trishakti works have no reference letter?")
    assert "Barrage Gates" not in res.final and "| Canal Lining" not in res.final
    assert "Missing (listed by prove_absence): Canal Lining Pkg-11 (wa); Pump House Pkg-12 (wb)" in res.final
    assert res.checks["absence_list_rewritten"] and parse_answer(res.final)[0] == 2
    assert "Absence verified (prove_absence PROVEN)" in res.final and "42/42 letters linked" in res.final
    assert "Limits:" in res.final and res.checks["answer_status"] == A.VERIFIED_ABSENCE


# ── 9. repeated tool call ──────────────────────────────────────────────────

def test_repeated_call_returns_the_cached_result_and_costs_no_step(g, monkeypatch):
    args = {"client": "Trishakti"}
    n = real_call(g, "find_portfolio", args)["result"]["n"]
    calls = []
    _tools(monkeypatch, {"find_portfolio": lambda a: (calls.append(a), real_call(g, "find_portfolio", a))[1]})
    seen = _script(monkeypatch, [_reply(calls=[_call("find_portfolio", args)]),
                                 _reply(calls=[_call("find_portfolio", args, 1)]),
                                 _reply(f"Counted the Trishakti portfolio.\nANSWER: {n}")])
    res = _qa(g, "How many works did we complete for Trishakti?", max_steps=2)
    assert len(calls) == 1 and res.usage["tool_calls"] == 1 and len(res.steps) == 1
    assert res.checks["repeated_calls"] == ["find_portfolio"]
    assert "already ran in this run" in seen[2]["messages"][-1]["content"]
    assert parse_answer(res.final)[0] == n and "budget" not in (res.note or "")      # answered, not finalized


# ── 10. 413: shrink and retry the same model ───────────────────────────────

def test_413_shrinks_and_keeps_the_model(g, monkeypatch):
    big = {"result": {"rows": ["x" * 200] * 60}}
    _tools(monkeypatch, {"vector_search": big})
    state = {"n": 0}

    def third(messages, prefer):
        state["n"] += 1
        if state["n"] == 1:
            raise providers.LLMUnavailable("request too large: groq:model-a request (HTTP 413)", "too_large")
        return _reply("Found 60 matching pages.\nANSWER: 60")
    seen = _script(monkeypatch, [_reply(calls=[_call("vector_search", {"query": "pile"})]),
                                 _reply(calls=[_call("vector_search", {"query": "piles"}, 1)]), third, third])
    res = _qa(g, "How many pages mention piles?")
    assert not res.switches and res.checks["shrunk_413"] == 1 and "HTTP 413" in res.note
    assert seen[2]["prefer"] == seen[3]["prefer"] == "groq:model-a"
    size = lambda s: sum(len(str(m.get("content") or "")) for m in s["messages"])
    assert size(seen[3]) < size(seen[2])
    assert parse_answer(res.final)[0] == 60


def test_providers_raise_too_large_on_413(monkeypatch):
    class E(Exception):
        status_code = 413
        response = None

    class C:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise E("Request too large for model on tokens per minute (TPM)")
    monkeypatch.setattr(providers, "POOL", [("groq", "a"), ("cerebras", "b")])
    monkeypatch.setattr(providers, "_client", lambda p: C)
    with pytest.raises(providers.LLMUnavailable) as ei:
        providers.chat([{"role": "user", "content": "x"}], use_cache=False, prefer="groq:a")
    assert ei.value.kind == "too_large"


def test_tool_output_is_capped(g, monkeypatch):
    _tools(monkeypatch, {"vector_search": {"result": {"rows": ["y" * 300] * 100}}})
    seen = _script(monkeypatch, [_reply(calls=[_call("vector_search", {"query": "q"})]), _reply("Done.\nANSWER: 100")])
    _qa(g, "How many pages mention q?")
    tool_msg = [m for m in seen[1]["messages"] if m["role"] == "tool"][0]
    assert len(tool_msg["content"]) <= loop.TOOL_CAP + 100 and "truncated" in tool_msg["content"]


# ── 11. fallback: a sentence, never raw JSON ───────────────────────────────

def test_fallback_is_a_sentence(g, monkeypatch):
    args = {"client": "Trishakti"}
    r = real_call(g, "find_portfolio", args)["result"]

    def fail(messages, prefer):
        raise providers.LLMUnavailable("all LLM endpoints are rate-limited", "rate_limited")
    _script(monkeypatch, [_reply(calls=[_call("find_portfolio", args)]), fail])
    res = _qa(g, "What is the total value of our Trishakti works?")
    assert "{" not in res.final and '"' not in res.final.split("ANSWER:")[0]
    assert f"Last step: find_portfolio (client=Trishakti) returned {r['n']} work(s), total ₹{r['total_value']:,}." \
        in res.final
    assert res.checks["answer_status"] == A.INCOMPLETE


# ── 12. now vs then ────────────────────────────────────────────────────────

def test_past_fact_as_of_gets_a_now_vs_then_line(g, monkeypatch):
    def fact(a):
        past = a["as_of"].startswith("2024")
        return {"result": {"as_of": a["as_of"], "value": 410000000 if past else 450000000}}
    _tools(monkeypatch, {"fact_as_of": fact})
    args = {"key": "w1", "predicate": "value", "as_of": "2024-06-01"}
    _script(monkeypatch, [_reply(calls=[_call("fact_as_of", args)]),
                          _reply("On 2024-06-01 we believed 410000000.\nANSWER: 410000000")])
    res = _qa(g, "What did we think the value of w1 was on 2024-06-01, and is it the same now?")
    line = next(ln for ln in res.final.splitlines() if ln.startswith("Now vs then:"))
    assert "410000000 as of 2024-06-01" in line and "450000000 now" in line and line.endswith("changed.")
    assert res.final.index("Now vs then") < res.final.index("ANSWER:")
    assert parse_answer(res.final)[0] == 410000000 and res.checks["now_vs_then"] == 1


# ── unit: detection ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["None.", "There are no open tenders.", "ANSWER: 0", "0 items found",
                                  "The bid would not be affected.", "He has never led a job.", "Record not found.",
                                  "zero cases rely on it"])
def test_negatives_detected(text):
    assert A.negatives(text)


@pytest.mark.parametrize("text", ["ANSWER: 12", "It DOES NOT QUALIFY.", "Nothing was changed.", "No, Pkg-7 is ours: ANSWER: 3"])
def test_non_negatives(text):
    assert not A.negatives(text)


def test_blast_radius_dependents_contradict_an_impact_negative(g, monkeypatch):
    dep = {"kind": "past_bid", "key": "RFP-1", "paths": [{"nodes": ["Person:p", "Work:w", "Bid:RFP-1"],
                                                          "rels": [{"type": "LED_BY", "dir": "<-"},
                                                                   {"type": "CITES", "dir": "<-"}]}]}
    _tools(monkeypatch, {"blast_radius": {"result": {"dependents": [dep], "dependents_by_kind": {"past_bid": 1},
                                                     "cases_relying": [], "past_bids_citing": [], "verdict_flips": []}}})
    ans = _reply("Nothing would be affected.\nANSWER: none")
    _script(monkeypatch, [_reply(calls=[_call("blast_radius", {"label": "Person", "key": "p"})]),
                          _reply(calls=[_call("list_cases", {}, 1)]), ans, ans])
    res = _qa(g, "If p left, what would be affected?")
    _no_negative(res.final)
    assert "1 dependent(s) with paths" in res.final and res.checks["answer_status"] == A.INCOMPLETE


def test_list_cases_surfaces_tender_status(g):
    r = real_call(g, "list_cases", {})["result"]
    assert "open_tenders" in r and "tender_status_counts" in r
