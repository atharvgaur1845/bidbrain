"""BidBrain as an MCP server: every read-only graph tool, for any MCP client.

    stdio:  python -m bidbrain.mcp.server
    HTTP:   mounted by the API at /mcp (streamable HTTP)

Claude Desktop / any MCP host can then ask the company brain the same
multi-hop questions the in-app agents do — tool selection through MCP, on the
same registry (one implementation per tool). Write tools (case graph,
conflict resolution) are not exposed: MCP clients get the pristine base graph,
read-only (every query runs as GRAPH.RO_QUERY).
"""
from __future__ import annotations

from mcp.types import ToolAnnotations
import inspect
import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from bidbrain import config
from bidbrain.graph.client import Graph
from bidbrain.tools import TOOLS

PY = {"string": str, "integer": int, "boolean": bool, "number": float, "array": list, "object": dict}

server = MCPServer(
    name="bidbrain",
    instructions=("BidBrain: a FalkorDB company brain for a construction firm's bid desk — works, clients, "
                  "people, credentials, documents, invoices, bonds, tenders, financial statements, claims and "
                  "decisions. Use find_portfolio + aggregate for multi-hop totals, prove_absence for 'which X has "
                  "no Y', qualify_clause for tender eligibility, explain_path to show how two entities connect."))


def _wrap(t):
    orig = inspect.signature(t.fn)
    params = []
    for name, p in orig.parameters.items():
        if name == "g":
            continue
        typ = PY.get((t.params.get(name) or {}).get("type"), Any)
        required = name in t.required
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY,
                                        annotation=typ if required else (typ | None),
                                        default=inspect.Parameter.empty if required else
                                        (p.default if p.default is not inspect.Parameter.empty else None)))

    def fn(**kwargs) -> dict:
        g = Graph(os.getenv("MCP_GRAPH", config.BASE_GRAPH))
        try:
            out = t(g, **kwargs)
        except Exception as exc:   # a client agent can only self-correct if it sees why the call failed
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {k: v for k, v in out.items() if k in ("result", "evidence", "resolved_as", "scope", "error", "ms")}

    fn.__signature__ = inspect.Signature(params, return_annotation=dict)
    fn.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict}
    fn.__name__ = t.name
    fn.__doc__ = t.description
    return fn


for _t in TOOLS.values():
    if not _t.writes:
        server.add_tool(_wrap(_t), name=_t.name, description=_t.description,
                        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))


def mcp_app():
    return server.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                      host="0.0.0.0")


if __name__ == "__main__":
    server.run("stdio")
