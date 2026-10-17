"""BidBrain as an MCP server: every read-only graph tool, plus the session's action loop, for any MCP client.

    stdio:  python -m bidbrain.mcp.server
    HTTP:   mounted by the API at /mcp (streamable HTTP)

Claude Desktop / any MCP host can then ask the company brain the same
multi-hop questions the in-app agents do — tool selection through MCP, on the
same registry (one implementation per tool). Read tools are read-only (every query
runs as GRAPH.RO_QUERY) and annotated readOnlyHint. By default they read the pristine
base graph; with `session_id` they read that existing session's graph instead (its
cases, decisions and actions), so `case_get` / `blast_radius` / `list_cases` work on
what `qualify_tender` wrote. A read never creates a session. The registry's write
tools (case graph internals) are not exposed.

Acting tools (readOnlyHint=False), all on a *session's* own graph copy (`session_<id>`):
* `qualify_tender` runs the whole tender team (ClauseParser, Planner, EvidenceHunter,
  AbsenceAuditor, ConflictSentinel, Verifier, Assembler) exactly like the Qualify page and
  returns the case, its verdicts and the follow-up actions it created;
* `transition_action` approves, rejects or executes one of those actions (company/actions.py:
  compare-and-set lifecycle, audit trail, four-eyes rule on publishing to the company record).
`list_actions` reads the session's action queue. Nothing an MCP client does can write to the
base graph: every acting tool refuses a graph that is not a `session_` graph.

Errors are MCP errors: a failing tool call comes back with isError=true and a readable
message (raised as ToolError), never as a normal result holding an "error" key. Unknown
arguments are errors too (every tool's argument model forbids extra fields), so a misspelt
filter cannot be silently dropped.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import threading
from typing import Any, Literal

from datetime import date

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from bidbrain import config
from bidbrain.graph.client import Graph, db
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
                  "team on your session's own graph copy (pass session_id='new' to get one) and returns the case and "
                  "its follow-up actions; list_actions / transition_action (approve, reject, execute) work that queue. "
                  "Pass the same session_id to a read tool (list_cases, case_get, blast_radius, ...) to read that "
                  "session's graph instead of the shared base."))


def _read_graph() -> Graph:
    return Graph(os.getenv("MCP_GRAPH", config.BASE_GRAPH))


SESSION_HELP = "session_id must be the 8-32 character [a-z0-9] id of a session (the web app's, or one qualify_tender made)"


def _session_graph(session_id: str | None) -> Graph:
    """An EXISTING session's graph (reads and actions never create one), never the base graph."""
    from bidbrain.api import sessions
    sid = (session_id or "").strip()
    if not sessions.SID_RE.match(sid):
        raise ToolError(f"invalid session_id {session_id!r}: {SESSION_HELP}")
    name = f"session_{sid}"
    if name not in db().list_graphs():
        raise ToolError(f"no session {sid!r} (it may have expired); create one with qualify_tender(session_id='new')")
    try:
        g = sessions.graph_for(sid)
    except (ValueError, sessions.RateLimited) as exc:
        raise ToolError(str(exc)) from exc
    if g.name == config.BASE_GRAPH or not g.name.startswith("session_"):
        raise ToolError("refusing to use a graph that is not a session graph")
    return g


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

    params.append(inspect.Parameter("session_id", inspect.Parameter.KEYWORD_ONLY, annotation=str | None, default=None))

    def fn(**kwargs) -> dict:
        sid = kwargs.pop("session_id", None)
        g = _session_graph(sid) if sid else _read_graph()
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


SESSION_NOTE = ("\nOptional session_id: read that existing session's graph (its cases, decisions, actions) "
                "instead of the shared base graph.")

for _t in TOOLS.values():
    if not _t.writes:
        server.add_tool(_wrap(_t), name=_t.name, description=_t.description + SESSION_NOTE,
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
    basis, the memo, and the follow-up actions the run created (reference-letter requests, credential
    renewals; work them with list_actions / transition_action on the same session_id)."""
    from bidbrain.agents.orchestrator import qualify_tender as run_team
    from bidbrain.api import sessions

    if not tender_id and not (text or "").strip():
        raise ToolError("give tender_id (one of " + ", ".join(t["id"] for t in _demo_tenders()) + ") or text")
    # everything is validated before a session graph exists: a bad call must not create one
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
        try:
            date.fromisoformat(bid_date[:10])
        except ValueError as exc:
            raise ToolError(f"bid_date {bid_date!r} is not an ISO date (YYYY-MM-DD)") from exc
        tender["bid_date"] = bid_date
    if title:
        tender["title"] = title
    new = (session_id or "").strip().lower() in ("", "new")
    sid = sessions.new_session() if new else session_id.strip()
    if not sessions.SID_RE.match(sid):
        raise ToolError(f"invalid session_id {session_id!r}: {SESSION_HELP}, or 'new'")
    try:
        g = sessions.graph_for(sid)
    except (ValueError, sessions.RateLimited) as exc:
        raise ToolError(f"{exc}: session_id must be 8-32 lowercase letters/digits, or 'new'") from exc
    if g.name == config.BASE_GRAPH or not g.name.startswith("session_"):
        raise ToolError("refusing to write outside a session graph")      # never the shared base

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
            "actions": [{k: a.get(k) for k in ("id", "kind", "status", "title", "owner", "assignee", "case_id")}
                        for a in out.get("actions") or [] if isinstance(a, dict)],
            "plan": {k: (out.get("plan") or {}).get(k) for k in ("agents", "skip", "handoffs")},
            "agents": [{k: r.get(k) for k in ("agent", "status", "llm_calls", "tool_calls", "ms")}
                       for r in out.get("agents") or [] if isinstance(r, dict)],
            "usage": out.get("usage"), "ms": out.get("ms")}


server.add_tool(qualify_tender, name="qualify_tender", description=inspect.getdoc(qualify_tender),
                annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                            openWorldHint=False))


def list_actions(session_id: str, status: str | None = None, case_id: str | None = None) -> dict:
    """The accountable follow-up actions in a session's graph (review a changed bid, request a reference
    letter, confirm a credential renewal, publish a decided fact), pending first. Optional filters: status
    (pending | approved | executing | executed | rejected | superseded | fulfilled) and case_id. Read-only."""
    from bidbrain.company import actions as A
    g = _session_graph(session_id)
    return {"session_id": session_id, "actions": A.list_actions(g, status, case_id), "kinds": A.KINDS}


def transition_action(session_id: str, action_id: str, op: Literal["approve", "reject", "execute"],
                      note: str | None = None) -> dict:
    """Move one action in a session's graph through its lifecycle: approve (pending -> approved), reject
    (pending/approved -> rejected) or execute (approved -> executed; runs the action's deterministic
    executor, e.g. records an internal reference-letter request; nothing is emailed). Transitions are
    compare-and-set and audited; a repeat returns the same result. Publishing to the shared company record
    needs a second approver from another workspace. Returns the action with its audit trail."""
    from bidbrain.company import actions as A
    from bidbrain.api import sessions
    from bidbrain.conflicts.engine import session_tag
    g = _session_graph(session_id)
    by = f"{session_tag(g.name)} (via MCP)"
    lock = _session_lock(g.name)
    if not lock.acquire(timeout=60):
        raise ToolError(f"session {session_id} is busy with another run; try again shortly")
    try:
        with sessions.busy(g):
            if op == "execute":
                return A.execute(g, action_id, by)
            return A.transition(g, action_id, op, by, note or "")
    except A.ActionError as exc:
        raise ToolError(f"{exc} (HTTP {getattr(exc, 'status', 409)})") from exc
    except ToolError:
        raise
    except Exception as exc:
        from bidbrain.agents.providers import redact
        raise ToolError(redact(f"{type(exc).__name__}: {exc}")) from exc
    finally:
        lock.release()


server.add_tool(list_actions, name="list_actions", description=inspect.getdoc(list_actions),
                annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
server.add_tool(transition_action, name="transition_action", description=inspect.getdoc(transition_action),
                annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                            openWorldHint=False))


def _forbid_unknown_arguments() -> None:
    """Every tool rejects arguments it does not take (isError), instead of silently ignoring them."""
    for t in server._tool_manager.list_tools():
        model = t.fn_metadata.arg_model
        model.model_config = {**model.model_config, "extra": "forbid"}
        model.model_rebuild(force=True)
        if isinstance(t.parameters, dict):
            t.parameters["additionalProperties"] = False


_forbid_unknown_arguments()


def mcp_app():
    return server.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                      host="0.0.0.0")


if __name__ == "__main__":
    server.run("stdio")
