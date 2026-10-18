"""Thin access layer over FalkorDB.

Every other module goes through `Graph` so that (a) read-only paths really run
as GRAPH.RO_QUERY, (b) results come back as plain dicts keyed by the RETURN
aliases, and (c) a session can be pointed at its own copy of the base graph.
"""
from __future__ import annotations

import datetime as _dt
from functools import lru_cache
from typing import Any

from falkordb import FalkorDB

from bidbrain import config


@lru_cache(maxsize=1)
def db() -> FalkorDB:
    # FALKOR_URL (redis://user:pass@host:port, or rediss:// for TLS) is how a managed FalkorDB Cloud
    # instance is reached; plain host/port stays the default for the bundled local container.
    if config.FALKOR_URL:
        return FalkorDB.from_url(config.FALKOR_URL)
    return FalkorDB(host=config.FALKOR_HOST, port=config.FALKOR_PORT)


def day(iso: str | None) -> int | None:
    """ISO date -> integer day number (None stays None: absence is data)."""
    if not iso:
        return None
    return (_dt.date.fromisoformat(iso[:10]) - _dt.date(1970, 1, 1)).days


def iso(day_no: int | None) -> str | None:
    if day_no is None:
        return None
    return (_dt.date(1970, 1, 1) + _dt.timedelta(days=int(day_no))).isoformat()


def _plain(v: Any) -> Any:
    """FalkorDB Node/Edge/Path objects -> JSON-friendly dicts."""
    if hasattr(v, "labels") and hasattr(v, "properties"):          # Node
        return {"_id": v.id, "_labels": list(v.labels), **v.properties}
    if hasattr(v, "relation") and hasattr(v, "properties"):        # Edge
        return {"_id": v.id, "_type": v.relation, "_src": v.src_node,
                "_dst": v.dest_node, **v.properties}
    if hasattr(v, "nodes") and hasattr(v, "edges") and callable(v.nodes):  # Path
        return {"nodes": [_plain(n) for n in v.nodes()],
                "edges": [_plain(e) for e in v.edges()]}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


class Graph:
    """One named graph: the pristine base, or a per-session copy."""

    def __init__(self, name: str = config.BASE_GRAPH, timeout_ms: int = 10_000):
        self.name = name
        self.g = db().select_graph(name)
        self.timeout_ms = timeout_ms

    def run(self, cypher: str, params: dict | None = None) -> list[dict]:
        res = self.g.query(cypher, params or {}, timeout=self.timeout_ms)
        return self._rows(res)

    def read(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Read-only: the server rejects any write, whatever the text says."""
        res = self.g.ro_query(cypher, params or {}, timeout=self.timeout_ms)
        return self._rows(res)

    @staticmethod
    def _rows(res) -> list[dict]:
        if not res.header:
            return []
        cols = [h[1] if isinstance(h, (list, tuple)) else h for h in res.header]
        cols = [c.decode() if isinstance(c, bytes) else c for c in cols]
        return [{c: _plain(v) for c, v in zip(cols, row)} for row in res.result_set]

    def scalar(self, cypher: str, params: dict | None = None) -> Any:
        rows = self.read(cypher, params)
        return next(iter(rows[0].values())) if rows else None

    def exists(self) -> bool:
        return self.name in db().list_graphs()

    def delete(self) -> None:
        if self.exists():
            self.g.delete()

    def copy_to(self, target: str) -> "Graph":
        """GRAPH.COPY — how every visitor gets an isolated tenant graph."""
        db().connection.execute_command("GRAPH.COPY", self.name, target)
        return Graph(target, self.timeout_ms)
