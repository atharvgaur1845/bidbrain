"""One registry for every graph tool.

The same function is exposed three ways — to the MCP server, to the in-process
agent loop (OpenAI tool-calling schema), and to the eval oracle — so there is
exactly one implementation of each traversal. A tool returns:

    {"result": ...,            # what the agent reasons over
     "evidence": [{doc_id, page, what}],   # citations, rendered as PDF links
     "subgraph": {"nodes": [...], "edges": [...]},  # what the UI animates
     "cypher": [...]}          # the queries that ran, shown in "explain" mode
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import time
from typing import Any, Callable

TOOLS: dict[str, "Tool"] = {}


@dataclasses.dataclass
class Tool:
    name: str
    fn: Callable
    description: str
    params: dict          # JSON-schema "properties"
    required: list[str]
    writes: bool = False

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.params,
                           "required": self.required}}}

    def __call__(self, g, **kwargs) -> dict:
        allowed = set(inspect.signature(self.fn).parameters) - {"g"}
        clean = {k: v for k, v in kwargs.items() if k in allowed and v is not None and v != ""}
        t0 = time.perf_counter()
        out = self.fn(g, **clean)
        out.setdefault("evidence", [])
        out.setdefault("subgraph", {"nodes": [], "edges": []})
        out.setdefault("cypher", [])
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return out


def tool(name: str, description: str, params: dict | None = None,
         required: list[str] | None = None, writes: bool = False):
    def deco(fn):
        TOOLS[name] = Tool(name, fn, description.strip(), params or {}, required or [], writes)
        return fn
    return deco


def call(g, name: str, args: dict | str | None) -> dict:
    if isinstance(args, str):
        args = json.loads(args or "{}")
    if name not in TOOLS:
        return {"error": f"unknown tool {name}", "available": sorted(TOOLS)}
    try:
        return TOOLS[name](g, **(args or {}))
    except Exception as exc:  # the agent sees the error and can recover
        return {"error": f"{type(exc).__name__}: {exc}"}


def schemas(names: list[str] | None = None, brief: dict[str, str] | None = None) -> list[dict]:
    """Tool schemas for an LLM. With `brief`, descriptions are the one-line versions and parameter
    descriptions are dropped — free-tier rate limits are per token, and the full prose lives in MCP/docs."""
    out = []
    for n in (names or sorted(TOOLS)):
        s = TOOLS[n].schema()
        if brief and n in brief:
            f = s["function"]
            f["description"] = brief[n]
            f["parameters"]["properties"] = {k: {kk: vv for kk, vv in v.items() if kk != "description"}
                                             for k, v in f["parameters"]["properties"].items()}
        out.append(s)
    return out


def compact(out: dict) -> str:
    """What the LLM sees: the result (plus resolution notes / errors). Evidence, Cypher and the
    subgraph go to the UI through events; resending them to the model only burns rate limit."""
    slim = {k: v for k, v in out.items() if k in ("result", "resolved_as", "error", "missing_keys", "available")}
    return json.dumps(slim, ensure_ascii=False, default=str, separators=(",", ":"))
