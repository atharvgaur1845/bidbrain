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

import os
import re
import secrets
import threading
import time

from bidbrain import config
from bidbrain.graph.client import Graph, db

SESSION_TTL = int(os.getenv("SESSION_TTL", str(24 * 3600)))
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "60"))
_last_seen: dict[str, float] = {}
_lock = threading.Lock()
SID_RE = re.compile(r"^[a-z0-9]{8,32}$")


def new_session() -> str:
    return secrets.token_hex(6)


def graph_for(sid: str | None) -> Graph:
    if not sid or not SID_RE.match(sid):
        raise ValueError("invalid session id")
    name = f"session_{sid}"
    with _lock:
        if name not in db().list_graphs():
            _evict_if_full()
            Graph(config.BASE_GRAPH).copy_to(name)
        _last_seen[name] = time.time()
    return Graph(name)


def _evict_if_full() -> None:
    live = [n for n in db().list_graphs() if n.startswith("session_")]
    if len(live) < MAX_SESSIONS:
        return
    oldest = sorted(live, key=lambda n: _last_seen.get(n, 0))[: len(live) - MAX_SESSIONS + 1]
    for n in oldest:
        Graph(n).delete()
        _last_seen.pop(n, None)


def reap() -> int:
    now, n = time.time(), 0
    for name in [x for x in db().list_graphs() if x.startswith("session_")]:
        if now - _last_seen.get(name, now) > SESSION_TTL:
            Graph(name).delete()
            _last_seen.pop(name, None)
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
