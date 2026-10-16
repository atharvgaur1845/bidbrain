"""The thin tool-calling loop every BidBrain agent runs on.

No framework: messages in, tool calls out, tool results back, until the model
answers or the step budget runs out. Only the tools offered to the agent can run:
a call to any other registered tool gets an error tool message instead. When the
budget runs out (or an endpoint rejects a mid-run request) the model gets one last
tool-free turn to answer from the results it already has, so gathered evidence is
never thrown away. Each step is emitted as an event so the UI
can stream the trace and animate the subgraph each tool touched. The loop also
accumulates evidence + subgraph across steps, which is what the answer card
shows as "the path behind this answer".
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from bidbrain.agents import providers
from bidbrain.graph.client import Graph
from bidbrain.tools import TOOLS, call, compact, schemas
from bidbrain.agents.prompts import BRIEF

Emit = Callable[[dict], None]


@dataclass
class RunResult:
    agent: str
    final: str
    steps: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    subgraph: dict = field(default_factory=lambda: {"nodes": [], "edges": []})
    usage: dict = field(default_factory=lambda: {"prompt": 0, "completion": 0, "llm_calls": 0, "tool_calls": 0})
    ms: float = 0.0
    providers: list[str] = field(default_factory=list)
    error: str | None = None
    note: str | None = None        # e.g. "step budget exhausted; answered from the results so far"


FINALIZE = ("You have no tool calls left. Do not call any tools. Using only the tool results above, "
            "reply now in the required final format.")


def _merge_subgraph(acc: dict, sg: dict) -> None:
    seen_n = {n["id"] for n in acc["nodes"]}
    for n in sg.get("nodes", []):
        if n["id"] not in seen_n:
            acc["nodes"].append(n)
            seen_n.add(n["id"])
    seen_e = {(e["source"], e["target"], e["type"]) for e in acc["edges"]}
    for e in sg.get("edges", []):
        k = (e["source"], e["target"], e["type"])
        if k not in seen_e:
            acc["edges"].append(e)
            seen_e.add(k)


def _finalize(messages: list[dict], res: RunResult, agent: str, emit: Emit, why: str) -> bool:
    """One tool-free call asking for the answer from what the tools already returned."""
    try:
        rep = providers.chat([*messages, {"role": "user", "content": FINALIZE}])
    except providers.LLMUnavailable as exc:
        emit({"type": "error", "agent": agent, "message": f"{why}; final answer call failed: {exc}"})
        return False
    _count(res, rep)
    if not rep.content:
        return False
    res.final, res.note = rep.content, f"{why}; answered from the results gathered so far"
    emit({"type": "final", "agent": agent, "text": rep.content, "provider": rep.provider, "note": res.note})
    return True


def _count(res: RunResult, rep) -> None:
    res.usage["llm_calls"] += 1
    res.usage["prompt"] += rep.usage.get("prompt", 0)
    res.usage["completion"] += rep.usage.get("completion", 0)
    res.providers.append(rep.provider + (" (cached)" if rep.cached else ""))



def _calls_from_text(text: str) -> list[dict]:
    calls = []
    for i, m in enumerate(re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text or "", re.S)):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("name"):
            args = obj.get("arguments", {})
            calls.append({"id": f"text-call-{i}", "name": obj["name"],
                          "arguments": args if isinstance(args, str) else json.dumps(args)})
    return calls

def run_agent(g: Graph, agent: str, system: str, user: str, tool_names: list[str],
              max_steps: int = 8, emit: Emit | None = None, history: list[dict] | None = None) -> RunResult:
    emit = emit or (lambda e: None)
    t0 = time.perf_counter()
    res = RunResult(agent=agent, final="")
    offered = [t for t in tool_names if t in TOOLS]
    tools = schemas(offered, brief=BRIEF)
    messages = [{"role": "system", "content": system}, *(history or []), {"role": "user", "content": user}]
    emit({"type": "agent_start", "agent": agent})
    for step in range(max_steps):
        try:
            rep = providers.chat(messages, tools=tools)
        except providers.LLMUnavailable as exc:
            res.error = str(exc)
            emit({"type": "error", "agent": agent, "message": str(exc)})
            # the endpoint rejected this particular request (e.g. a malformed generated tool call): a
            # tool-free request may still go through, and the evidence gathered so far deserves an answer
            if res.steps and getattr(exc, "kind", None) == "request" \
                    and _finalize(messages, res, agent, emit, f"LLM request failed mid-run ({exc})"):
                res.error = None
            break
        _count(res, rep)
        if not rep.tool_calls and "tool_call>" in (rep.content or ""):
            # Qwen sometimes writes its call as text the server's hermes parser misses: recover the call
            # from the text, or (once) ask for a well-formed one instead of taking "</tool_call>" as the answer
            rep.tool_calls = _calls_from_text(rep.content)
            if not rep.tool_calls and not res.note:
                res.note = "re-asked after a malformed tool call"
                messages.append({"role": "assistant", "content": rep.content})
                messages.append({"role": "user", "content": "That tool call was malformed. Call the tool again "
                                 "with valid JSON arguments, or give the final ANSWER line."})
                continue
        if not rep.tool_calls:
            res.final = rep.content
            emit({"type": "final", "agent": agent, "text": rep.content, "provider": rep.provider})
            break
        if rep.content:
            emit({"type": "thought", "agent": agent, "text": rep.content})
        messages.append({"role": "assistant", "content": rep.content or None,
                         "tool_calls": [{"id": tc["id"], "type": "function",
                                         "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                                        for tc in rep.tool_calls]})
        for tc in rep.tool_calls:
            try:
                args = json.loads(tc["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            emit({"type": "tool_call", "agent": agent, "tool": tc["name"], "args": args, "step": step})
            if tc["name"] in offered:
                out = call(g, tc["name"], args)
            else:       # registered or not, a tool this agent was not given must not run
                out = {"error": f"tool {tc['name']!r} is not available here", "available": offered}
            res.usage["tool_calls"] += 1
            res.evidence += [e for e in out.get("evidence", []) if e not in res.evidence]
            _merge_subgraph(res.subgraph, out.get("subgraph", {}))
            step_rec = {"tool": tc["name"], "args": args, "ms": out.get("ms"), "error": out.get("error"),
                        "cypher": out.get("cypher", [])}
            res.steps.append(step_rec)
            emit({"type": "tool_result", "agent": agent, "tool": tc["name"], "ms": out.get("ms"),
                  "error": out.get("error"), "summary": _summary(out), "subgraph": out.get("subgraph"),
                  "evidence": out.get("evidence", [])[:12], "cypher": out.get("cypher", [])})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": compact(out)[:9000]})
    else:
        if not _finalize(messages, res, agent, emit, "step budget exhausted"):
            res.error = "step budget exhausted"
            emit({"type": "error", "agent": agent, "message": res.error})
    res.ms = round((time.perf_counter() - t0) * 1000, 1)
    return res


def _summary(out: dict) -> str:
    if "error" in out:
        return f"error: {out['error']}"
    r = out.get("result")
    if isinstance(r, dict):
        for k in ("claim", "basis", "verdict"):
            if k in r:
                return str(r[k])[:200]
        if "n" in r and "total_value" in r:
            return f"{r['n']} works, total {r['total_value']:,}"
        if "value" in r and "op" in r:
            return f"{r['op']} = {r['value']}"
        return json.dumps(r, default=str)[:200]
    if isinstance(r, list):
        return f"{len(r)} rows"
    return str(r)[:200]
