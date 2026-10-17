"""BidBrain HTTP API.

    uvicorn bidbrain.api.main:app --port 7860

Every request names a session (header `X-Session`); the session's own graph
copy is created on first use. Long-running agent work streams Server-Sent
Events (`text/event-stream`) so the UI can animate each step as it happens.
The MCP server is mounted at /mcp for any MCP client (Claude Desktop, etc.).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import queue
import re
import secrets
import threading
import time
import traceback

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from bidbrain import config
from bidbrain.agents import providers
from bidbrain.api import sessions
from bidbrain.graph.client import Graph
from bidbrain.tools import TOOLS, call

from contextlib import asynccontextmanager

try:
    from bidbrain.mcp.server import mcp_app, server as MCP
    _MCP_APP = mcp_app()
except Exception as _exc:  # MCP is additive; never block the API
    print("MCP disabled:", _exc)
    MCP, _MCP_APP = None, None


@asynccontextmanager
async def lifespan(_app):
    sessions.start_reaper()
    if MCP is not None:
        async with MCP.session_manager.run():
            yield
    else:
        yield


class _Guard:
    """Plain ASGI middleware (no BaseHTTPMiddleware, so SSE streams are untouched).

    * POST /mcp is routed to the MCP mount; Mount("/mcp") alone only matches "/mcp/...", and the request
      would otherwise fall through to the static UI and get a 405.
    * Uploads are refused from their Content-Length before the multipart body is spooled to disk.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] == "/mcp":
                scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
            elif scope["path"] == "/api/ingest" and scope["method"] == "POST":
                size = dict(scope["headers"]).get(b"content-length", b"")
                problem = (411, "Content-Length required") if not size.isdigit() else \
                    (413, "max 5 MB") if int(size) > MAX_UPLOAD + 64_000 else None
                if problem:
                    await JSONResponse({"detail": problem[1]}, status_code=problem[0])(scope, receive, send)
                    return
        await self.app(scope, receive, send)


app = FastAPI(title="BidBrain", version="1.0", lifespan=lifespan)
app.add_middleware(_Guard)
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Session"])

TENDERS = json.loads((config.DATA / "tenders" / "demo_tenders.json").read_text())
PDF_DIR = config.DATA / "conflicts" / "pdf"
UPLOADS = sessions.UPLOADS
MAX_UPLOAD = 5_000_000
DOC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_RUNS = threading.BoundedSemaphore(int(os.getenv("MAX_CONCURRENT_RUNS", "4")))   # agent runs across all sessions
_SESSION_LOCKS: dict[str, threading.Lock] = {}                                   # one run at a time per session


def G(sid: str | None) -> Graph:
    try:
        return sessions.graph_for(sid)
    except ValueError:
        raise HTTPException(400, "missing or invalid X-Session header (POST /api/session first)")


async def aG(sid: str | None) -> Graph:
    """G() for async handlers: creating a session copies the base graph, which must not block the loop."""
    return await asyncio.to_thread(G, sid)


class _Cancelled(BaseException):
    """Raised inside a run when its client has gone; BaseException so agent code's `except Exception` lets it pass."""


def stream(work, g: Graph) -> StreamingResponse:
    """Run `work(emit)` in a thread; stream every emitted event as SSE.

    Runs are bounded (a global semaphore plus one run per session, so two clicks never write to the same
    graph at once), the session cannot be evicted while its run is going, and a client that disconnects
    stops the run at its next step instead of leaving it to burn LLM quota.
    """
    q: queue.Queue = queue.Queue()
    cancel = threading.Event()

    def emit(ev: dict):
        if cancel.is_set():
            raise _Cancelled()
        q.put(ev)

    def acquire(lock) -> bool:
        while not lock.acquire(timeout=0.5):
            if cancel.is_set():
                return False
        return True

    def runner():
        mine = _SESSION_LOCKS.setdefault(g.name, threading.Lock())
        try:
            with sessions.busy(g):
                if not _RUNS.acquire(blocking=False):
                    q.put({"type": "status", "message": "queued behind other runs"})
                    if not acquire(_RUNS):
                        return
                try:
                    if not acquire(mine):
                        return
                    try:
                        q.put({"type": "result", "data": work(emit)})
                    finally:
                        mine.release()
                finally:
                    _RUNS.release()
        except _Cancelled:
            pass
        except Exception as exc:
            traceback.print_exc()
            q.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            q.put(None)

    threading.Thread(target=runner, daemon=True).start()

    async def gen():
        try:
            while True:
                try:
                    ev = q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.03)
                    continue
                if ev is None:
                    yield "event: end\ndata: {}\n\n"
                    break
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        finally:
            cancel.set()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ basics

@app.get("/api/health")
def health():
    from bidbrain.graph.client import db
    return {"ok": True, "base_graph": Graph().exists(),
            "sessions": sum(1 for n in db().list_graphs() if n.startswith("session_")),
            "llm": providers.available(), "replay": providers.REPLAY, "tools": len(TOOLS)}


@app.post("/api/session")
def new_session():
    sid = sessions.new_session()
    sessions.graph_for(sid)
    return JSONResponse({"session_id": sid}, headers={"X-Session": sid})


@app.get("/api/overview")
def overview(x_session: str | None = Header(None)):
    g = G(x_session)
    labels = g.read("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n ORDER BY n DESC")
    rels = g.read("MATCH ()-[e]->() RETURN type(e) AS type, count(e) AS n ORDER BY n DESC")
    meta = g.read("MATCH (m:Meta) RETURN m.version AS version")
    stats = g.read("""MATCH (d:Decision) WITH count(d) AS decisions
                      OPTIONAL MATCH (k:ConflictCard {status:'open'}) WITH decisions, count(k) AS open_cards
                      OPTIONAL MATCH (r:PolicyRule) WITH decisions, open_cards, count(r) AS rules
                      OPTIONAL MATCH (c:Case) RETURN decisions, open_cards, rules, count(c) AS cases""")[0]
    return {"session": g.name, "labels": labels, "relationships": rels, "version": (meta or [{}])[0].get("version"),
            **stats, "llm": providers.available(), "tools": sorted(TOOLS)}


@app.get("/api/schema")
def schema(x_session: str | None = Header(None)):
    g = G(x_session)
    rows = g.read("""MATCH (a)-[e]->(b) WHERE NOT a:Chunk AND NOT b:Chunk
                     RETURN labels(a)[0] AS src, type(e) AS rel, labels(b)[0] AS dst, count(*) AS n""")
    return {"triples": rows}


# ------------------------------------------------------------------ ask

@app.post("/api/ask")
async def ask(req: Request, x_session: str | None = Header(None)):
    body = await req.json()
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question required")
    g = await aG(x_session)

    def work(emit):
        from bidbrain.agents.qa import ask as qa_ask
        hist = sessions.history(g) if body.get("use_memory", True) else []
        sessions.remember_turn(g, "user", question)
        out = qa_ask(g, question, emit=emit, history=hist)
        sessions.remember_turn(g, "assistant", out["explanation"] or "",
                               [n["id"] for n in out["subgraph"]["nodes"]])
        return out
    return stream(work, g)


@app.post("/api/baseline/{system}")
async def baseline(system: str, req: Request, x_session: str | None = Header(None)):
    body = await req.json()
    g = await aG(x_session)
    from bidbrain.agents import baselines
    fn = {"vector_rag": baselines.vector_rag, "text2cypher": baselines.text2cypher}.get(system)
    if not fn:
        raise HTTPException(404, system)
    try:
        return await asyncio.to_thread(fn, g, body["question"])
    except providers.LLMUnavailable as exc:
        raise HTTPException(503, str(exc))


@app.get("/api/memory")
def memory(x_session: str | None = Header(None)):
    g = G(x_session)
    turns = g.read("""MATCH (:Conversation {id:'main'})-[:HAS_TURN]->(t:Turn)
                      OPTIONAL MATCH (t)-[:MENTIONS]->(e)
                      RETURN t.i AS i, t.role AS role, t.text AS text, t.at AS at,
                             collect(coalesce(e.name, e.title, e.key))[0..8] AS mentions ORDER BY i""")
    return {"turns": turns}


# ------------------------------------------------------------------ company memory (opt-in, shared)

async def _body(req: Request) -> dict:
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object expected")
    return body


@app.post("/api/memory/promote")
async def memory_promote(req: Request, x_session: str | None = Header(None)):
    """Copy one learned rule from this session into company memory (`bidbrain_org`), with provenance.
    The only way anything enters company memory: nothing a session does writes there implicitly."""
    from bidbrain.conflicts import engine
    body = await _body(req)
    g = await aG(x_session)
    try:
        out = await asyncio.to_thread(engine.promote, g, body.get("rule_id"), body.get("user") or "bid-manager")
    except engine.OrgMemoryError as exc:
        raise HTTPException(exc.status, str(exc))
    rule = next((r for r in engine.list_org(include_retired=True) if r["id"] == out["org_rule"]), None)
    return {**out, "rule": rule}


@app.get("/api/memory/org")
def memory_org(include_retired: bool = False):
    from bidbrain.conflicts import engine
    rules = engine.list_org(include_retired=include_retired)
    return {"graph": engine.ORG_GRAPH, "cap": engine.ORG_MAX_ACTIVE,
            "active": sum(1 for r in rules if not r.get("retired")), "rules": rules}


@app.post("/api/memory/org/{org_id}/retire")
async def memory_org_retire(org_id: str, req: Request):
    from bidbrain.conflicts import engine
    body = await _body(req)
    try:
        return await asyncio.to_thread(engine.retire_org, org_id, body.get("user") or "bid-manager")
    except engine.OrgMemoryError as exc:
        raise HTTPException(exc.status, str(exc))


# ------------------------------------------------------------------ tenders / cases

@app.get("/api/tenders")
def tenders():
    return [{k: t[k] for k in ("id", "title", "authority", "category", "estimated_cost", "bid_date", "story", "text")}
            for t in TENDERS]


@app.post("/api/qualify")
async def qualify(req: Request, x_session: str | None = Header(None)):
    body = await req.json()
    g = await aG(x_session)
    tender = next((t for t in TENDERS if t["id"] == body.get("tender_id")), None)
    if tender is None:
        if not body.get("text"):
            raise HTTPException(400, "tender_id or text required")
        tender = {"id": body.get("ref"), "title": body.get("title") or "Pasted tender", "text": body["text"],
                  "bid_date": body.get("bid_date") or config.AS_OF, "category": body.get("category")}
    else:
        tender = dict(tender)
        if body.get("text") and body["text"].strip() != (tender.get("text") or "").strip():
            tender["text"] = body["text"]
            tender.pop("clauses", None)       # edited text: the vetted clauses no longer describe it

    def work(emit):
        from bidbrain.agents.orchestrator import qualify_tender
        return qualify_tender(g, tender, emit=emit, prefer_llm=body.get("use_llm", True))
    return stream(work, g)


@app.get("/api/cases")
def cases(x_session: str | None = Header(None)):
    g = G(x_session)
    return g.read("""MATCH (c:Case) RETURN c.id AS id, c.title AS title, c.verdict AS verdict, c.bid_date AS bid_date,
                     c.status AS status, c.verified AS verified, c.created_at AS created_at ORDER BY created_at DESC""")


@app.get("/api/cases/{case_id}")
def case(case_id: str, x_session: str | None = Header(None)):
    out = call(G(x_session), "case_get", {"case_id": case_id})
    if "error" in out:
        raise HTTPException(404, out["error"])
    return out["result"]


# ------------------------------------------------------------------ conflicts / live documents

@app.get("/api/conflicts")
def conflicts(x_session: str | None = Header(None)):
    g = G(x_session)
    cards = g.read("""MATCH (k:ConflictCard {status:'open'})-[:ABOUT_CLAIM]->(c:Claim)
                      OPTIONAL MATCH (c)-[:ABOUT]->(s)
                      RETURN k.id AS id, k.subject_label AS label, k.subject_key AS key, k.predicate AS predicate,
                             k.suggested AS suggested, k.rationale AS rationale,
                             coalesce(s.title, s.name, s.key) AS subject,
                             collect({id: c.id, value: c.value_str, source_doc: c.source_doc, page: c.page,
                                      source_kind: c.source_kind, authority: c.authority, issuer: c.issuer}) AS claims""")
    decisions = g.read("""MATCH (d:Decision)-[:SELECTS]->(w:Claim) OPTIONAL MATCH (d)-[:DECIDED_BY]->(a)
                          OPTIONAL MATCH (w)-[:ABOUT]->(s)
                          RETURN d.id AS id, d.policy AS policy, d.rationale AS rationale, d.predicate AS predicate,
                                 d.subject_key AS key, coalesce(s.title, s.name, s.key) AS subject, d.chosen_value AS chosen,
                                 d.at AS at, a.key AS actor, labels(a)[0] AS actor_kind, w.source_doc AS source_doc,
                                 d.org_rule AS org_rule
                          ORDER BY at DESC LIMIT 300""")
    rules = g.read("""MATCH (r:PolicyRule) OPTIONAL MATCH (r)-[:LEARNED_FROM]->(d:Decision)
                      OPTIONAL MATCH (d2:Decision)-[:APPLIED_RULE]->(r)
                      RETURN r.id AS id, r.predicate AS predicate, r.prefer_kind AS prefer, r.over_kind AS over,
                             r.rationale AS rationale, r.created_by AS by, d.id AS learned_from, count(d2) AS applied,
                             coalesce(r.retired, false) AS retired""")
    from bidbrain.conflicts import engine
    in_org = {(o["predicate"], o["prefer_kind"], o["over_kind"]): o["id"] for o in engine.org_rules()}
    for r in rules:                     # is this shape already company memory (promoted here or elsewhere)?
        r["org_rule"] = in_org.get((r["predicate"], r["prefer"], r["over"]))
    return {"cards": cards, "decisions": decisions, "rules": rules,
            "restatements": sum(1 for d in decisions if d["predicate"] and d["policy"] == "authority"
                                and (d["key"] or "").startswith("FS-"))}


@app.post("/api/conflicts/{card_id}/resolve")
async def resolve(card_id: str, req: Request, x_session: str | None = Header(None)):
    body = await req.json()
    g = await aG(x_session)
    winner = body.get("winner_claim")
    if not isinstance(winner, str):
        raise HTTPException(400, "winner_claim required")

    def work():
        from bidbrain.conflicts import engine
        from bidbrain.tools.case import apply_flips, blast_radius
        card = g.read("""MATCH (k:ConflictCard {id:$id}) OPTIONAL MATCH (k)-[:ABOUT_CLAIM]->(c:Claim)
                         RETURN k.status AS status, k.subject_label AS l, k.subject_key AS k, collect(c.id) AS claims""",
                      {"id": card_id})
        if not card:
            raise HTTPException(404, "no such conflict card")
        card = card[0]
        if card["status"] != "open":
            raise HTTPException(409, f"card is already {card['status']}")
        if winner not in card["claims"]:
            raise HTTPException(400, "winner_claim is not one of this card's claims")
        try:
            out = engine.resolve_card(g, card_id, winner, body.get("rationale") or "resolved by bid manager",
                                      user=str(body.get("user") or "bid-manager")[:64],
                                      apply_to_similar=bool(body.get("apply_to_similar")))
        except ValueError as exc:      # card superseded or settled while the user was looking at it
            raise HTTPException(409, str(exc))
        ripple = blast_radius(g, card["l"], card["k"])
        apply_flips(g, ripple["result"]["verdict_flips"], f"decision {out['decision_id']}",
                    ripple["result"].get("evidence_changed"))
        return {"decision": out, "ripple": ripple["result"], "subgraph": ripple["subgraph"]}

    with sessions.busy(g):
        return await asyncio.to_thread(work)


def _all_events() -> list[dict]:
    demo = config.DATA / "conflicts" / "demo_events.json"
    evs = json.loads(demo.read_text()) if demo.exists() else []
    return evs + json.loads((config.DATA / "conflicts" / "events.json").read_text())


@app.get("/api/events")
def demo_events():
    evs = _all_events()
    pick, seen = [], {"demo": -100}
    for e in evs:
        if seen.get(e["type"], 0) < 3:
            seen[e["type"]] = seen.get(e["type"], 0) + 1
            pick.append({"event_id": e["event_id"], "type": e["type"], "narrative": e["narrative"],
                         "title": e["doc"]["title"], "has_pdf": (PDF_DIR / f"{e['doc']['doc_id']}.pdf").exists(),
                         "doc_id": e["doc"]["doc_id"]})
    return pick


def _ripple_after(g: Graph, emit, out: dict, cause: str) -> dict:
    from bidbrain.tools.case import apply_flips, blast_radius
    subjects = {(c["conflict"]["subject_label"], c["conflict"]["subject_key"])
                for c in out.get("decisions", []) + out.get("cards", [])}
    # a first-time fact (no rival claim) is written to its subject too, so it can flip clauses as well
    subjects |= {(c["conflict"]["subject_label"], c["conflict"]["subject_key"]) for c in out.get("first_claims", [])}
    ripples = []
    for label, key in subjects:
        br = blast_radius(g, label, key)
        apply_flips(g, br["result"]["verdict_flips"], cause, br["result"].get("evidence_changed"))
        ripples.append(br["result"])
        emit({"type": "ripple", "subject": f"{label}:{key}", "result": br["result"], "subgraph": br["subgraph"]})
    return {"ripples": ripples}


@app.post("/api/events/{event_id}/fire")
async def fire(event_id: str, x_session: str | None = Header(None)):
    g = await aG(x_session)
    evs = {e["event_id"]: e for e in _all_events()}
    if event_id not in evs:
        raise HTTPException(404, event_id)

    def work(emit):
        from bidbrain.conflicts.events import apply_event
        ev = dict(evs[event_id])
        ev["doc"] = {**ev["doc"], "doc_id": f"{ev['doc']['doc_id']}-{int(time.time()) % 100000}"}
        emit({"type": "document", "doc": ev["doc"], "narrative": ev["narrative"]})
        out = apply_event(g, ev)
        for d in out["decisions"]:
            emit({"type": "decision", **d})
        for c in out["cards"]:
            emit({"type": "card", **c})
        return {**out, **_ripple_after(g, emit, out, f"{ev['doc']['title']}")}
    return stream(work, g)


@app.post("/api/ingest")
async def ingest(file: UploadFile = File(...), x_session: str | None = Header(None)):
    g = await aG(x_session)
    name = pathlib.Path(file.filename or "upload.pdf").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF only")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "max 5 MB")
    # the stored name is a random doc id, never the session id or the client's filename
    doc_id = f"DOC-UP-{secrets.token_hex(5)}"
    folder = sessions.upload_dir(x_session)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{doc_id}.pdf"
    path.write_bytes(data)

    def work(emit):
        from bidbrain.conflicts.ingest_doc import ingest_pdf
        emit({"type": "document", "doc": {"doc_id": doc_id, "title": name}})
        out = ingest_pdf(g, path, doc_id=doc_id)
        emit({"type": "extracted", "kind": out.get("kind"), "subject": out.get("subject"),
              "extracted": out.get("extracted"), "error": out.get("error")})
        if out.get("error"):
            return out
        for d in out.get("decisions", []):
            emit({"type": "decision", **d})
        for c in out.get("cards", []):
            emit({"type": "card", **c})
        return {**out, **_ripple_after(g, emit, out, f"{name}")}
    return stream(work, g)


@app.get("/api/samples")
def samples():
    out = []
    for p in sorted(PDF_DIR.glob("DOC-EV-*.pdf"))[:40]:
        out.append({"name": p.name, "url": f"/api/samples/{p.name}"})
    return out


@app.get("/api/samples/{name}")
def sample(name: str):
    p = PDF_DIR / pathlib.Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")


# ------------------------------------------------------------------ documents, entities, decisions

_DOC_INDEX: dict[str, str] = {}


@app.get("/api/docs/{doc_id}")
def doc_pdf(doc_id: str):
    """Corpus documents, rendered event PDFs, and uploads, by exact id only.

    An upload's id is random (DOC-UP-<10 hex>), so its URL works like a capability link; the iframe
    viewer cannot send the X-Session header, and nothing here globs on user input.
    """
    import csv
    if not DOC_ID_RE.match(doc_id):
        raise HTTPException(404, "unknown document")
    if not _DOC_INDEX:
        with open(config.DATASET / "document_index.csv", newline="") as fh:
            _DOC_INDEX.update({r["doc_id"]: r["filename"] for r in csv.DictReader(fh)})
    if doc_id in _DOC_INDEX:
        p = config.DOCUMENTS / _DOC_INDEX[doc_id]
        if p.exists():
            return FileResponse(p, media_type="application/pdf" if p.suffix == ".pdf" else
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    # a fired event's document is "<rendered pdf id>-<n>", one per firing
    for cand in (doc_id, re.sub(r"-\d+$", "", doc_id)):
        if (PDF_DIR / f"{cand}.pdf").is_file():
            return FileResponse(PDF_DIR / f"{cand}.pdf", media_type="application/pdf")
    if doc_id.startswith("DOC-UP-") and UPLOADS.is_dir():
        for folder in UPLOADS.iterdir():
            p = folder / f"{doc_id}.pdf"
            if p.is_file():
                return FileResponse(p, media_type="application/pdf")
    raise HTTPException(404, "unknown document")


@app.get("/api/docs/{doc_id}/text")
def doc_text(doc_id: str, page: int | None = None, x_session: str | None = Header(None)):
    g = G(x_session)
    rows = g.read("MATCH (c:Chunk {doc_id:$d}) WHERE $p IS NULL OR c.page = $p "
                  "RETURN c.page AS page, c.text AS text ORDER BY c.page, c.id", {"d": doc_id, "p": page})
    return {"doc_id": doc_id, "chunks": rows}


@app.get("/api/entity/{kind}/{key}")
def entity(kind: str, key: str, x_session: str | None = Header(None)):
    out = call(G(x_session), "get_entity", {"kind": kind, "key": key})
    if "error" in out:
        raise HTTPException(404, out["error"])
    return out


@app.get("/api/neighborhood")
def neighborhood(label: str, key: str, x_session: str | None = Header(None)):
    g = G(x_session)
    if not label.isalpha():
        raise HTTPException(400)
    rows = g.read(f"""MATCH (n:{label} {{key:$k}})-[r]-(m) WHERE NOT m:Chunk AND NOT m:Claim
                      RETURN labels(m)[0] AS label, m.key AS key, coalesce(m.name, m.title, m.doc_id, m.key) AS name,
                             type(r) AS rel, startNode(r) = n AS out LIMIT 120""", {"k": key})
    me = g.read(f"MATCH (n:{label} {{key:$k}}) RETURN coalesce(n.name, n.title, n.key) AS name", {"k": key})
    nodes = [{"id": f"{label}:{key}", "label": label, "name": me[0]["name"] if me else key}]
    seen = {nodes[0]["id"]}
    edges = []
    for r in rows:
        nid = f"{r['label']}:{r['key']}"
        if nid not in seen:
            nodes.append({"id": nid, "label": r["label"], "name": r["name"]})
            seen.add(nid)
        edges.append({"source": nodes[0]["id"] if r["out"] else nid, "target": nid if r["out"] else nodes[0]["id"],
                      "type": r["rel"]})
    return {"nodes": nodes, "edges": edges}


@app.get("/api/decisions/{decision_id}/trace")
def decision_trace(decision_id: str, x_session: str | None = Header(None)):
    g = G(x_session)
    rows = g.read("""MATCH (d:Decision {id:$id})
                     OPTIONAL MATCH (d)-[:DECIDED_BY]->(a)
                     OPTIONAL MATCH (d)-[:SELECTS]->(w:Claim)
                     OPTIONAL MATCH (d)-[:BASED_ON]->(c:Claim)-[f:FROM]->(doc:Document)
                     OPTIONAL MATCH (d)-[:APPLIED_RULE]->(r:PolicyRule)
                     OPTIONAL MATCH (d)-[:APPLIED_ORG_RULE]->(o:OrgRuleRef)
                     OPTIONAL MATCH (d)-[:SUPERSEDES]->(old:Decision)
                     OPTIONAL MATCH (c)-[:ABOUT]->(s)
                     RETURN d {.*} AS decision, a.key AS actor, labels(a)[0] AS actor_kind, w.id AS winner,
                            collect(DISTINCT {claim: c.id, value: c.value_str, kind: c.source_kind, authority: c.authority,
                                              issuer: c.issuer, doc: doc.doc_id, page: f.page, status: c.status}) AS claims,
                            r.id AS rule, o {.*} AS org_rule, collect(DISTINCT old.id) AS supersedes,
                            labels(s)[0] AS subject_label, s.key AS subject_key, coalesce(s.title, s.name, s.key) AS subject""",
                  {"id": decision_id})
    if not rows:
        raise HTTPException(404)
    return rows[0]


@app.get("/api/algos/{name}")
def algos(name: str, x_session: str | None = Header(None)):
    if name not in ("key_people", "communities", "isolated_entities", "audit_bid", "who_knows"):
        raise HTTPException(404)
    return call(G(x_session), name, {})


@app.post("/api/tool/{name}")
async def tool_call(name: str, req: Request, x_session: str | None = Header(None)):
    if name not in TOOLS or TOOLS[name].writes:
        raise HTTPException(404, "unknown or write tool")
    args = await req.json()
    return await asyncio.to_thread(call, await aG(x_session), name, args)


@app.get("/api/eval")
def eval_results():
    out = {}
    d = config.DATA / "eval"
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text())
            out[p.stem] = data.get("summary", data) if isinstance(data, dict) else data
        except Exception:
            pass
    return out


# ------------------------------------------------------------------ MCP (streamable HTTP) + static UI

if _MCP_APP is not None:
    app.mount("/mcp", _MCP_APP)

STATIC = config.ROOT / "frontend" / "out"
if STATIC.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
