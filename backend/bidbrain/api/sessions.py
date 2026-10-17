"""Graph-per-session tenancy.

Every visitor works in their own copy of the pristine base graph
(`GRAPH.COPY bidbrain_base -> session_<id>`), so one judge resolving a conflict
or approving a bid never changes what the next judge sees — FalkorDB's
multi-graph feature used as a real requirement. Copies expire after
SESSION_TTL of inactivity; at most MAX_SESSIONS live at once (oldest idle evicted).
Creating a session copy is rate-limited per client IP (SESSION_RATE_PER_MIN, default 10), on every
path that can create one, so a flood of new sessions cannot evict everyone else's.
Conversation turns are stored in the session graph itself, which is what lets
a visitor come back later (same session id in their browser) and continue.
"""
from __future__ import annotations

import collections
import contextvars
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
SESSION_RATE = int(os.getenv("SESSION_RATE_PER_MIN", "10"))     # new session copies per client IP per minute
_created: dict[str, collections.deque] = {}
# the requesting client's IP, set per request by the API's ASGI middleware (None outside a request:
# internal callers and tests that call graph_for directly are not rate-limited)
client_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar("client_ip", default=None)


class RateLimited(Exception):
    """Too many new sessions from one client IP in the last minute."""


def new_session() -> str:
    return secrets.token_hex(6)


def _allow_new(ip: str) -> bool:
    """Sliding one-minute window per IP (caller holds _lock)."""
    now = time.time()
    q = _created.setdefault(ip, collections.deque())
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= SESSION_RATE:
        return False
    q.append(now)
    if len(_created) > 10_000:          # bounded: forget IPs with nothing in the window
        for k in [k for k, v in _created.items() if not v or now - v[-1] > 60]:
            _created.pop(k, None)
    return True


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
            ip = client_ip.get()
            if ip is not None and not _allow_new(ip):
                raise RateLimited(f"too many new sessions from this address (max {SESSION_RATE} a minute)")
            _evict_if_full(live)
            copy_base(name)
            _dropped.discard(name)
        _last_seen[name] = time.time()
    g = Graph(name)
    try:                                # catch up with the company record (decided facts published by others)
        from bidbrain.company import record
        record.apply(g)
    except Exception:
        pass
    return g


def _wait_for_no_fork(limit_s: float = 15.0) -> None:
    """Let a running snapshot finish before copying (both need Redis's single fork slot)."""
    t0 = time.time()
    while time.time() - t0 < limit_s:
        try:
            info = db().connection.info("persistence")
        except Exception:
            return
        if not any(info.get(k) for k in ("rdb_bgsave_in_progress", "aof_rewrite_in_progress",
                                         "module_fork_in_progress")):
            return
        time.sleep(0.05)


def copy_base(name: str, tries: int = 40) -> Graph:
    """GRAPH.COPY the base graph. FalkorDB forks for the copy and Redis allows one child at a time, so a
    copy that coincides with a BGSAVE / AOF rewrite (or another process's copy) fails with "could not
    fork": wait and retry instead of failing the visitor's first request."""
    for i in range(tries):
        _wait_for_no_fork()
        try:
            return Graph(config.BASE_GRAPH).copy_to(name)
        except Exception as exc:
            if "fork" not in str(exc).lower() or i == tries - 1:
                raise
            time.sleep(0.05 * min(i + 1, 10))
    raise RuntimeError("unreachable")


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
    # session graphs that survived a restart (FalkorDB persistence) start their TTL now; without
    # this they would never be reaped, since only sessions seen by this process have a timestamp
    now = time.time()
    with _lock:
        for name in db().list_graphs():
            if name.startswith("session_"):
                _last_seen.setdefault(name, now)

    def loop():
        while True:
            time.sleep(600)
            try:
                reap()
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True).start()


# -------------------------------------------------------------- conversation memory
# (:Conversation)-[:HAS_TURN]->(:Turn)-[:MENTIONS]->(entity). A follow-up reads the last few turns
# (short-term) plus, when the question names an entity talked about earlier, up to 3 older turns
# that MENTION it (graph recall: the MENTIONS edges are the index). A turn MENTIONS only what it is
# about: the entities a question names, and the entities an answer's evidence rests on.

MENTIONABLE = ("Work", "Client", "Person", "Tender", "Bond", "Credential")
RECALLABLE = ("Client", "Person", "Work")
RECALL_K = 3
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_PKG = re.compile(r"\bpkg (\d+)\b")
# words that name a kind of organisation, not which one ("Public Works Department, Govt of Gujarat" is Gujarat's)
_GENERIC = {"public", "works", "department", "dept", "govt", "government", "corporation", "corp", "limited",
            "ltd", "pvt", "private", "office", "authority", "national", "company", "bureau", "board", "and",
            "the", "pkg", "package", "state", "central", "development", "infrastructure", "projects", "project",
            # what a company does, not which company it is ("Trishakti Power Generation Corporation")
            "power", "generation", "energy", "steel", "petroleum", "engineering", "construction",
            "constructions", "constructors", "industries", "services", "enterprises", "valley", "group",
            "buildings", "special"}
SINGLE_MIN = 6      # a client named by one distinctive word: that word has at least this many letters
# a refusal ("I can't answer that") is about nothing, whatever it happens to name
_REFUSAL = re.compile(
    r"\bi\s+(?:can\s*not|can'?t|cannot|am\s+unable\s+to|was\s+unable\s+to|could\s*n'?o?t|do\s+not\s+have|"
    r"don'?t\s+have)\s+(?:answer|find|determine|help|access|provide|tell|verify|enough)"
    r"|\bunable\s+to\s+(?:answer|determine)\b"
    r"|\b(?:not\s+enough|insufficient)\s+(?:data|information|evidence)\s+to\s+(?:answer|determine)", re.I)
_NO_ANSWER = {"unknown", "n/a", "na", "cannot determine", "cannot answer", "not available", "insufficient data",
              "insufficient information", "not found"}


def _norm(text: str | None) -> str:
    return " " + _NON_ALNUM.sub(" ", (text or "").lower()).strip() + " "


def _named(qn: str, compact: str, label: str, key: str, name: str, loose: bool,
           single: str | None = None) -> bool:
    """Does the normalised text `qn` name this entity? Strict: its full name, its key, (a work) its
    package number, or (a client) its one distinctive name word when the rest of its name is generic
    (`single`, from _single_words). Loose (only for entities already talked about): a person's first or
    last name, or most of the distinctive words of a work's or client's name."""
    nn = _norm(name)
    if len(nn.strip()) >= 4 and nn in qn:
        return True
    k = (key or "").lower()
    if len(k) >= 6 and (f" {k} " in qn or k in compact):
        return True
    m = _PKG.search(nn)
    if label == "Work" and m and re.search(rf" (?:pkg|package) {m.group(1)} ", qn):
        return True
    if single and f" {single} " in qn:
        return True
    if not loose:
        return False
    words = set(qn.split())
    toks = [t for t in nn.split() if not t.isdigit()]
    if label == "Person":
        return any(len(t) >= 3 and t in words for t in toks)
    toks = [t for t in toks if len(t) >= 4 and t not in _GENERIC]
    if not toks:
        return False
    need = len(toks) if len(toks) <= 2 else max(2, -(-3 * len(toks) // 5))
    return sum(t in words for t in toks) >= need


def _single_words(g: Graph, rows: list[dict]) -> dict[str, str]:
    """Client key -> the one word that names it, when every other word of its name is generic
    ("Trishakti Power Generation Corporation" -> "trishakti"). Only a word of >= SINGLE_MIN letters
    that is no state's or category's name and appears in no other client's, work's or person's name, so "Gujarat"
    never names "Gujarat Municipal Corporation" and "buildings" never names a bureau."""
    seen: collections.Counter = collections.Counter()
    for r in rows:
        seen.update(set(_norm(r["name"]).split()))
    # state and category names are never a client's name ("Gujarat", "buildings", "bridges")
    states = {t for r in g.read("MATCH (s) WHERE s:State OR s:Category RETURN coalesce(s.name, s.key) AS n")
              for t in _norm(r["n"]).split()}
    out = {}
    for r in rows:
        if r["label"] != "Client":
            continue
        toks = [t for t in _norm(r["name"]).split() if not t.isdigit() and len(t) >= 4 and t not in _GENERIC]
        if len(toks) == 1 and len(toks[0]) >= SINGLE_MIN and toks[0] not in states and seen[toks[0]] == 1:
            out[r["key"]] = toks[0]
    return out


def entities_named(g: Graph, text: str, labels: tuple[str, ...] = RECALLABLE, strict: bool = False) -> list[dict]:
    """Entities the text names: strictly against every Client, Person and Work, loosely against the ones
    this conversation already MENTIONS (never with strict=True)."""
    qn = _norm(text)
    if not qn.strip():
        return []
    compact = qn.replace(" ", "")
    talked = set() if strict else {(r["label"], r["key"]) for r in g.read(
        """MATCH (:Conversation {id:'main'})-[:HAS_TURN]->(:Turn)-[:MENTIONS]->(e)
           RETURN DISTINCT labels(e)[0] AS label, e.key AS key""")}
    rows = g.read("""MATCH (e) WHERE labels(e)[0] IN $labels AND e.key IS NOT NULL
                     RETURN labels(e)[0] AS label, e.key AS key, coalesce(e.name, e.title, e.key) AS name""",
                  {"labels": list(labels)})
    single = _single_words(g, rows) if "Client" in labels else {}
    return [{"id": f"{r['label']}:{r['key']}", **r} for r in rows
            if _named(qn, compact, r["label"], r["key"], r["name"], (r["label"], r["key"]) in talked,
                      single.get(r["key"]) if r["label"] == "Client" else None)]


def remember_turn(g: Graph, role: str, text: str, mentions: list[str] | None = None,
                  recalled: list[int] | None = None) -> int:
    """Append a turn; link it to what it MENTIONS (ids "Label:key") and the older turns it RECALLED."""
    rows = g.run("""MERGE (c:Conversation {id:'main'}) ON CREATE SET c.key = 'main', c.started_at = timestamp()
             WITH c OPTIONAL MATCH (c)-[:HAS_TURN]->(t:Turn)
             WITH c, count(t) AS n
             CREATE (c)-[:HAS_TURN]->(x:Turn {i:n, key:'turn-' + toString(n), role:$r, text:$t, at:timestamp()})
             RETURN x.i AS i""",
          {"r": role, "t": text[:4000]})
    i = rows[0]["i"]
    add_mentions(g, i, mentions or [])
    if recalled:
        g.run("""MATCH (:Conversation {id:'main'})-[:HAS_TURN]->(x:Turn {i:$i})
                 MATCH (:Conversation {id:'main'})-[:HAS_TURN]->(o:Turn) WHERE o.i IN $old
                 MERGE (x)-[:RECALLED]->(o)""", {"i": i, "old": recalled})
    return i


def add_mentions(g: Graph, i: int, mentions: list[str]) -> None:
    seen: list[str] = []
    for m in mentions:
        if m not in seen:
            seen.append(m)
    for m in seen[:20]:
        label, _, key = m.partition(":")
        if label in MENTIONABLE:
            g.run(f"""MATCH (:Conversation {{id:'main'}})-[:HAS_TURN]->(x:Turn {{i:$i}})
                      MATCH (e:{label} {{key:$k}}) MERGE (x)-[:MENTIONS]->(e)""", {"i": i, "k": key})


def is_refusal(out: dict) -> bool:
    """An answer that declines ("I can't answer that", ANSWER: unknown) or a run that failed outright."""
    text = out.get("explanation") or ""
    if out.get("error") and not text.strip():
        return True
    if str(out.get("answer") or "").strip().strip(".").lower() in _NO_ANSWER:
        return True
    return bool(_REFUSAL.search(text))


def answer_mentions(g: Graph, out: dict) -> list[str]:
    """What an answer is about: the entities of its subgraph that the answer text names, or that a
    document cited in its evidence is evidence for (a client through such a work). Not the whole
    subgraph, which also holds everything a tool merely looked at. A refusal, or an answer that rests
    on no evidence, is about nothing. An answer with evidence but no subgraph (a tool that returns
    pages only) MENTIONS what its text names strictly (full name, key, package number)."""
    if is_refusal(out):
        return []
    if not [e for e in out.get("evidence") or [] if isinstance(e, dict) and e.get("doc_id")]:
        return []
    nodes = [n for n in (out.get("subgraph") or {}).get("nodes", [])
             if isinstance(n, dict) and str(n.get("id", "")).partition(":")[0] in MENTIONABLE]
    if not nodes:
        return [e["id"] for e in entities_named(g, out.get("explanation") or "", strict=True)][:20]
    named = named_among(out.get("explanation") or "", [n["id"] for n in nodes],
                        {n["id"]: n.get("name") for n in nodes})
    docs = sorted({e.get("doc_id") for e in out.get("evidence") or [] if isinstance(e, dict) and e.get("doc_id")})
    linked: list[str] = []
    if docs:
        ids = [n["id"] for n in nodes]
        rows = g.read("""MATCH (e)-[:EVIDENCED_BY|HAS_REFERENCE]->(d:Document) WHERE d.doc_id IN $docs
                         WITH DISTINCT e WHERE (labels(e)[0] + ':' + e.key) IN $ids
                         OPTIONAL MATCH (e)-[:FOR_CLIENT]->(c:Client)
                         RETURN labels(e)[0] + ':' + e.key AS id, 'Client:' + c.key AS client""",
                      {"docs": docs, "ids": ids})
        for r in rows:
            linked.append(r["id"])
            if r["client"] in ids:
                linked.append(r["client"])
    return list(dict.fromkeys(named + linked))[:20]


def named_among(text: str, ids: list[str], names: dict[str, str] | None = None) -> list[str]:
    """The ids ("Label:key") among `ids` that the text names, loosely (they are already in context)."""
    qn = _norm(text)
    compact = qn.replace(" ", "")
    out = []
    for i in ids:
        label, _, key = i.partition(":")
        if _named(qn, compact, label, key, (names or {}).get(i) or key, loose=True):
            out.append(i)
    return out


def recall(g: Graph, question: str, before: int, k: int = RECALL_K, named: list[dict] | None = None) -> list[dict]:
    """Older turns (index < `before`) that MENTION an entity the question names, most overlap first;
    a recalled question brings the answer that followed it (`answers` = the question's turn index)."""
    named = entities_named(g, question) if named is None else named
    ids = [e["id"] for e in named if e["label"] in RECALLABLE]
    if not ids:
        return []
    rows = g.read("""MATCH (:Conversation {id:'main'})-[:HAS_TURN]->(t:Turn)-[:MENTIONS]->(e)
                     WHERE t.i < $before AND (labels(e)[0] + ':' + e.key) IN $ids
                     WITH t, collect(DISTINCT coalesce(e.name, e.title, e.key)) AS about
                     RETURN t.i AS i, t.role AS role, t.text AS text, about
                     ORDER BY size(about) DESC, i DESC LIMIT $k""", {"before": before, "ids": ids, "k": k})
    have = {r["i"] for r in rows}
    want = sorted({r["i"] + 1 for r in rows if r["role"] == "user"} - have)
    want = [i for i in want if i < before]
    if want:
        rows += [{**r, "answers": r["i"] - 1} for r in g.read(
            """MATCH (:Conversation {id:'main'})-[:HAS_TURN]->(t:Turn) WHERE t.i IN $want AND t.role = 'assistant'
               OPTIONAL MATCH (t)-[:MENTIONS]->(e)
               RETURN t.i AS i, t.role AS role, t.text AS text,
                      collect(DISTINCT coalesce(e.name, e.title, e.key)) AS about""", {"want": want})]
    return sorted(rows, key=lambda r: r["i"])


def context(g: Graph, question: str | None = None, n: int = 6,
            named: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Chat messages for a follow-up: recalled older turns (labelled as such) then the last `n` turns.
    Returns (messages, recalled turns)."""
    rows = g.read("""MATCH (:Conversation {id:'main'})-[:HAS_TURN]->(t:Turn)
                     RETURN t.i AS i, t.role AS role, t.text AS text ORDER BY t.i DESC LIMIT $n""", {"n": n})
    rows.reverse()
    old = recall(g, question, rows[0]["i"], named=named) if question and rows else []
    def label(r):
        what = f"about {', '.join(r['about'][:4])}" if r["about"] else f"the answer to turn {r.get('answers')}"
        return f"[Recalled from earlier in this conversation (turn {r['i']}, {what}); it may be out of date] "
    msgs = [{"role": r["role"] if r["role"] in ("user", "assistant") else "user",
             "content": label(r) + (r["text"] or "")[:1500]} for r in old]
    msgs += [{"role": r["role"], "content": r["text"]} for r in rows]
    return msgs, [{"i": r["i"], "role": r["role"], "about": r["about"]} for r in old]


def history(g: Graph, n: int = 6, question: str | None = None) -> list[dict]:
    return context(g, question, n)[0]
