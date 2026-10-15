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
        # a misspelt filter must fail loudly: silently dropping `category` (vs `categories`) would answer
        # an unscoped question as if it were scoped
        unknown = sorted(k for k in kwargs if k not in allowed)
        if unknown:
            raise TypeError(f"{self.name} has no argument(s) {unknown}; it takes {sorted(allowed)}")
        clean = {k: _coerce(self.params.get(k, {}), v) for k, v in kwargs.items() if v is not None and v != ""}
        t0 = time.perf_counter()
        out = self.fn(g, **clean)
        out.setdefault("evidence", [])
        out.setdefault("subgraph", {"nodes": [], "edges": []})
        out.setdefault("cypher", [])
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return out


def _coerce(spec: dict, v):
    """Convert a JSON-ish argument to the declared type: LLMs often send '2023' for an integer, and in
    Cypher an int compared with a string is null, which empties the result instead of erroring."""
    t = spec.get("type")
    try:
        if t == "integer" and not isinstance(v, bool) and isinstance(v, (str, float)):
            return int(float(str(v).replace(",", "")))
        if t == "number" and isinstance(v, str):
            return float(v.replace(",", ""))
        if t == "boolean" and isinstance(v, str):
            return {"true": True, "false": False, "yes": True, "no": False}[v.strip().lower()]
        if t == "array" and isinstance(v, str):
            return [v]
    except (ValueError, KeyError):
        raise ValueError(f"cannot read {v!r} as {t}")
    # enumerated values are checked too: an out-of-range filter (a client name passed as a govt level)
    # matches nothing and would come back as a confident 0
    allowed = spec.get("enum") or (spec.get("items") or {}).get("enum")
    if allowed:
        bad = [x for x in (v if isinstance(v, list) else [v]) if x not in allowed]
        if bad:
            raise ValueError(f"{bad} not allowed; use one of {allowed}")
    return v


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
