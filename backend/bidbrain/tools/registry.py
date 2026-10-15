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


def schemas(names: list[str] | None = None) -> list[dict]:
    return [TOOLS[n].schema() for n in (names or sorted(TOOLS))]


def compact(out: dict, max_items: int = 40) -> str:
    """What the LLM sees: result + evidence, without the UI subgraph payload."""
    slim = {k: v for k, v in out.items() if k not in ("subgraph", "cypher", "ms")}
    if isinstance(slim.get("evidence"), list) and len(slim["evidence"]) > max_items:
        slim["evidence"] = slim["evidence"][:max_items] + [f"... {len(out['evidence']) - max_items} more"]
    return json.dumps(slim, ensure_ascii=False, default=str)
