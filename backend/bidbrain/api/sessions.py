"""Graph-per-session tenancy.

Every visitor works in their own copy of the pristine base graph
(`GRAPH.COPY bidbrain_base -> session_<id>`), so one judge resolving a conflict
or approving a bid never changes what the next judge sees — FalkorDB's
multi-graph feature used as a real requirement. Copies expire after
SESSION_TTL of inactivity; at most MAX_SESSIONS live at once (oldest evicted).
Conversation turns are stored in the session graph itself, which is what lets
a visitor come back later (same session id in their browser) and continue.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import contextmanager

from bidbrain import config
from bidbrain.graph.client import Graph, db

SESSION_TTL = int(os.getenv("SESSION_TTL", str(24 * 3600)))
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "60"))
UPLOADS = config.DATA / "uploads"
_last_seen: dict[str, float] = {}
_busy: dict[str, int] = {}             # session graph -> running streams; never evicted while > 0
_dropped: set[str] = set()             # evicted names; a stray late write may have re-created them
_lock = threading.Lock()
SID_RE = re.compile(r"^[a-z0-9]{8,32}$")


def new_session() -> str:
    return secrets.token_hex(6)


def upload_dir(sid_or_name: str):
    """Per-session upload folder, named by a hash so the session id never appears on disk."""
    sid = sid_or_name.removeprefix("session_")
    return UPLOADS / hashlib.sha256(sid.encode()).hexdigest()[:16]


def graph_for(sid: str | None) -> Graph:
    if not sid or not SID_RE.match(sid):
        raise ValueError("invalid session id")
    name = f"session_{sid}"
    with _lock:
        live = db().list_graphs()
        if name in _dropped and name in live:
            Graph(name).delete()       # half-empty graph left by a write that raced the eviction
            live = [n for n in live if n != name]
        if name not in live:
            _evict_if_full(live)
            Graph(config.BASE_GRAPH).copy_to(name)
            _dropped.discard(name)
        _last_seen[name] = time.time()
    return Graph(name)


@contextmanager
def busy(g: Graph):
    """Mark a session graph as in use for the duration of a stream."""
    with _lock:
        _busy[g.name] = _busy.get(g.name, 0) + 1
    try:
        yield
    finally:
        with _lock:
            _busy[g.name] -= 1
            _last_seen[g.name] = time.time()


def _drop(name: str) -> None:
    Graph(name).delete()
    _last_seen.pop(name, None)
    _dropped.add(name)
    shutil.rmtree(upload_dir(name), ignore_errors=True)


def _evict_if_full(live: list[str]) -> None:
    live = [n for n in live if n.startswith("session_")]
    if len(live) < MAX_SESSIONS:
        return
    idle = [n for n in live if not _busy.get(n)]
    for n in sorted(idle, key=lambda n: _last_seen.get(n, 0))[: len(live) - MAX_SESSIONS + 1]:
        _drop(n)


def reap() -> int:
    now, n = time.time(), 0
    with _lock:
        for name in [x for x in db().list_graphs() if x.startswith("session_")]:
            if not _busy.get(name) and now - _last_seen.get(name, now) > SESSION_TTL:
                _drop(name)
                n += 1
    return n


def start_reaper() -> None:
    def loop():
        while True:
            time.sleep(600)
            try:
                reap()
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True).start()


# -------------------------------------------------------------- conversation memory

def remember_turn(g: Graph, role: str, text: str, mentions: list[str] | None = None) -> None:
    g.run("""MERGE (c:Conversation {id:'main'}) ON CREATE SET c.key = 'main', c.started_at = timestamp()
             WITH c OPTIONAL MATCH (c)-[:HAS_TURN]->(t:Turn)
             WITH c, count(t) AS n
             CREATE (c)-[:HAS_TURN]->(x:Turn {i:n, key:'turn-' + toString(n), role:$r, text:$t, at:timestamp()})""",
          {"r": role, "t": text[:4000]})
    for m in (mentions or [])[:20]:
        label, _, key = m.partition(":")
        if label in ("Work", "Client", "Person", "Tender", "Bond", "Credential"):
            g.run(f"""MATCH (c:Conversation {{id:'main'}})-[:HAS_TURN]->(x:Turn) WITH x ORDER BY x.i DESC LIMIT 1
                      MATCH (e:{label} {{key:$k}}) MERGE (x)-[:MENTIONS]->(e)""", {"k": key})


def history(g: Graph, n: int = 6) -> list[dict]:
    rows = g.read("""MATCH (:Conversation {id:'main'})-[:HAS_TURN]->(t:Turn)
                     RETURN t.role AS role, t.text AS text ORDER BY t.i DESC LIMIT $n""", {"n": n})
    return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]
