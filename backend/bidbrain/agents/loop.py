"""The thin tool-calling loop every BidBrain agent runs on.

No framework: messages in, tool calls out, tool results back, until the model
answers or the step budget runs out. Each step is emitted as an event so the UI
can stream the trace and animate the subgraph each tool touched. The loop also
accumulates evidence + subgraph across steps, which is what the answer card
shows as "the path behind this answer".
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable

from bidbrain.agents import providers
from bidbrain.graph.client import Graph
from bidbrain.tools import TOOLS, call, compact, schemas

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


def run_agent(g: Graph, agent: str, system: str, user: str, tool_names: list[str],
              max_steps: int = 8, emit: Emit | None = None, history: list[dict] | None = None) -> RunResult:
    emit = emit or (lambda e: None)
    t0 = time.perf_counter()
    res = RunResult(agent=agent, final="")
    tools = schemas([t for t in tool_names if t in TOOLS])
    messages = [{"role": "system", "content": system}, *(history or []), {"role": "user", "content": user}]
    emit({"type": "agent_start", "agent": agent})
    for step in range(max_steps):
        try:
            rep = providers.chat(messages, tools=tools)
        except providers.LLMUnavailable as exc:
            res.error = str(exc)
            emit({"type": "error", "agent": agent, "message": str(exc)})
            break
        res.usage["llm_calls"] += 1
        res.usage["prompt"] += rep.usage.get("prompt", 0)
        res.usage["completion"] += rep.usage.get("completion", 0)
        res.providers.append(rep.provider + (" (cached)" if rep.cached else ""))
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
            out = call(g, tc["name"], args)
            res.usage["tool_calls"] += 1
            res.evidence += [e for e in out.get("evidence", []) if e not in res.evidence]
            _merge_subgraph(res.subgraph, out.get("subgraph", {}))
            step_rec = {"tool": tc["name"], "args": args, "ms": out.get("ms"), "error": out.get("error"),
                        "cypher": out.get("cypher", [])}
            res.steps.append(step_rec)
            emit({"type": "tool_result", "agent": agent, "tool": tc["name"], "ms": out.get("ms"),
                  "error": out.get("error"), "summary": _summary(out), "subgraph": out.get("subgraph"),
                  "evidence": out.get("evidence", [])[:12], "cypher": out.get("cypher", [])})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": compact(out)[:12000]})
    else:
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
