"""BidBrain as an MCP server: every read-only graph tool, plus one acting tool, for any MCP client.

    stdio:  python -m bidbrain.mcp.server
    HTTP:   mounted by the API at /mcp (streamable HTTP)

Claude Desktop / any MCP host can then ask the company brain the same
multi-hop questions the in-app agents do — tool selection through MCP, on the
same registry (one implementation per tool). Read tools run on the pristine base
graph, read-only (every query runs as GRAPH.RO_QUERY), and are annotated
readOnlyHint. The registry's write tools (case graph internals) are not exposed.

One tool acts: `qualify_tender` runs the whole tender team (ClauseParser, Planner,
EvidenceHunter, AbsenceAuditor, ConflictSentinel, Verifier, Assembler) on a
*session's* own graph copy (`session_<id>`, created from the base on first use),
exactly like the Qualify page, and is annotated readOnlyHint=False. Nothing an MCP
client does can write to the base graph.

Errors are MCP errors: a failing tool call comes back with isError=true and a readable
message (raised as ToolError), never as a normal result holding an "error" key.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import threading
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from bidbrain import config
from bidbrain.graph.client import Graph
from bidbrain.tools import TOOLS
from bidbrain.tools.read import doc_type_error

PY = {"string": str, "integer": int, "boolean": bool, "number": float, "array": list, "object": dict}
HIDDEN = ("subgraph", "cypher")     # UI payload (graph animation, Cypher panel); everything else passes through

server = MCPServer(
    name="bidbrain",
    instructions=("BidBrain: a FalkorDB company brain for a construction firm's bid desk — works, clients, "
                  "people, credentials, documents, invoices, bonds, tenders, financial statements, claims and "
                  "decisions. Use find_portfolio + aggregate for multi-hop totals, prove_absence for 'which X has "
                  "no Y', qualify_clause for tender eligibility, explain_path to show how two entities connect, "
                  "fact_history for 'which document / why do we believe'. qualify_tender acts: it runs the tender "
                  "team on your session's own graph copy (pass session_id='new' to get one)."))


def _read_graph() -> Graph:
    return Graph(os.getenv("MCP_GRAPH", config.BASE_GRAPH))


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
        g = _read_graph()
        # an unknown doc_type matches no document: say so instead of answering with an empty, confident list
        if t.name in ("vector_search", "fulltext_search") and (bad := doc_type_error(g, kwargs.get("doc_type"))):
            raise ToolError(bad)
        try:
            out = t(g, **kwargs)
        except ToolError:
            raise
        except Exception as exc:   # a client agent can only self-correct if it sees why the call failed
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc
        if out.get("error"):
            raise ToolError(str(out["error"]))
        # result, evidence, resolved_as, scope, ms and every warning a tool adds (missing_keys, not_found, nearest, note)
        return {k: v for k, v in out.items() if k not in HIDDEN}

    fn.__signature__ = inspect.Signature(params, return_annotation=dict)
    fn.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict}
    fn.__name__ = t.name
    fn.__doc__ = t.description
    return fn


for _t in TOOLS.values():
    if not _t.writes:
        server.add_tool(_wrap(_t), name=_t.name, description=_t.description,
                        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))


# ── the acting tool ─────────────────────────────────────────────────────────
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _demo_tenders() -> list[dict]:
    return json.loads((config.DATA / "tenders" / "demo_tenders.json").read_text())


def _session_lock(name: str) -> threading.Lock:
    """One run per session graph. Shares the HTTP API's per-session locks when the API is running in this
    process (so an MCP run and a Qualify-page run never write the same graph at once)."""
    api = sys.modules.get("bidbrain.api.main")
    locks = getattr(api, "_SESSION_LOCKS", None)
    if isinstance(locks, dict):
        return locks.setdefault(name, threading.Lock())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(name, threading.Lock())


def qualify_tender(session_id: str, tender_id: str | None = None, text: str | None = None,
                   bid_date: str | None = None, title: str | None = None, use_llm: bool = True) -> dict:
    """Run the tender qualification team on a session's own graph copy and record the case there.

    Give either `tender_id` (a sample tender: T-NHX-2026-014, T-PHED-WTP-2027-03, T-JN-LB-2026-09,
    T-CWBB-2026-21) or the eligibility `text` of any tender; `bid_date` (ISO) is the date every
    clause is checked on (default: the sample's own date, else the data's as-of date). `session_id`
    is an 8-32 character [a-z0-9] id (the web app's session, so the case appears on its Cases page),
    or 'new' to create one. Writes a Case subgraph (clauses, verdicts, SATISFIED_BY evidence,
    findings, agent runs) to that session's graph only; the shared base graph is never written.
    Returns the session_id, the case id, the overall recommendation, each clause's verdict and
    basis, and the memo."""
    from bidbrain.agents.orchestrator import qualify_tender as run_team
    from bidbrain.api import sessions

    if not tender_id and not (text or "").strip():
        raise ToolError("give tender_id (one of " + ", ".join(t["id"] for t in _demo_tenders()) + ") or text")
    sid = sessions.new_session() if (session_id or "").strip().lower() in ("", "new") else session_id.strip()
    try:
        g = sessions.graph_for(sid)
    except ValueError as exc:
        raise ToolError(f"{exc}: session_id must be 8-32 lowercase letters/digits, or 'new'") from exc
    if g.name == config.BASE_GRAPH or not g.name.startswith("session_"):
        raise ToolError("refusing to write outside a session graph")      # never the shared base

    if tender_id:
        tender = next((dict(t) for t in _demo_tenders() if t["id"] == tender_id), None)
        if tender is None:
            raise ToolError(f"unknown tender_id {tender_id!r}; use one of "
                            + ", ".join(t["id"] for t in _demo_tenders()) + ", or pass text")
        if text and text.strip() != (tender.get("text") or "").strip():
            tender["text"] = text
            tender.pop("clauses", None)       # edited text: the vetted clauses no longer describe it
    else:
        tender = {"id": None, "title": title or "Tender (via MCP)", "text": text, "bid_date": config.AS_OF}
    if bid_date:
        tender["bid_date"] = bid_date
    if title:
        tender["title"] = title

    lock = _session_lock(g.name)
    if not lock.acquire(timeout=120):
        raise ToolError(f"session {sid} is busy with another run; try again shortly")
    try:
        with sessions.busy(g):
            out = run_team(g, tender, prefer_llm=use_llm)
    except ToolError:
        raise
    except Exception as exc:
        from bidbrain.agents.providers import redact
        raise ToolError(redact(f"{type(exc).__name__}: {exc}")) from exc
    finally:
        lock.release()

    case = out.get("case") or {}
    return {"session_id": sid, "graph": g.name, "case_id": case.get("id"), "overall": case.get("verdict"),
            "bid_date": case.get("bid_date"), "title": case.get("title"),
            "clauses": [{k: c.get(k) for k in ("ref", "text", "kind", "verdict", "basis", "satisfied_by", "findings")}
                        for c in out.get("clauses") or []],
            "memo": case.get("memo"),
            "plan": {k: (out.get("plan") or {}).get(k) for k in ("agents", "skip", "handoffs")},
            "agents": [{k: r.get(k) for k in ("agent", "status", "llm_calls", "tool_calls", "ms")}
                       for r in out.get("agents") or [] if isinstance(r, dict)],
            "usage": out.get("usage"), "ms": out.get("ms")}


server.add_tool(qualify_tender, name="qualify_tender", description=inspect.getdoc(qualify_tender),
                annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                            openWorldHint=False))


def mcp_app():
    return server.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                      host="0.0.0.0")


if __name__ == "__main__":
    server.run("stdio")
