"""Read-only view endpoints for the UI (Impact Room dependency graph).

GET /api/views/impact-graph/{doc_id}: the real subgraph a live document touched, read from the session graph:
Document <-FROM- Claim -ABOUT-> subject, Claim -PREVAILS_OVER-> replaced Claim -FROM-> its Document,
Decision -BASED_ON/SELECTS-> Claim, Decision -APPLIED_RULE-> PolicyRule, Finding -CAUSED_BY-> Decision,
Clause -HAS_FINDING-> Finding, Case -HAS_CLAUSE-> Clause, Case -FOR_TENDER-> Tender,
Action -TRIGGERED_BY-> Decision, Action -FOR_CASE-> Case. Every node carries its stored properties.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

router = APIRouter()

_SKIP = {"embedding", "text", "markdown", "raw"}

# (pattern, params) — each returns a, r, b; `$d` = doc_id, `$decs` = decision ids found in step 1
_HOPS = [
    "MATCH (b:Document {doc_id:$d})<-[r:FROM]-(a:Claim) RETURN a, r, b",
    "MATCH (:Document {doc_id:$d})<-[:FROM]-(a:Claim)-[r:ABOUT]->(b) RETURN a, r, b",
    "MATCH (:Document {doc_id:$d})<-[:FROM]-(a:Claim)-[r:PREVAILS_OVER|CONTRADICTS]-(b:Claim) RETURN a, r, b",
    "MATCH (:Document {doc_id:$d})<-[:FROM]-(:Claim)-[:PREVAILS_OVER|CONTRADICTS]-(a:Claim)-[r:FROM]->(b:Document) RETURN a, r, b",
    "MATCH (a:Decision)-[r:BASED_ON|SELECTS]->(b:Claim) WHERE a.id IN $decs RETURN a, r, b",
    "MATCH (a:Decision)-[r:APPLIED_RULE|APPLIED_ORG_RULE|DECIDED_BY]->(b) WHERE a.id IN $decs RETURN a, r, b",
    "MATCH (a:Finding)-[r:CAUSED_BY]->(b:Decision) WHERE b.id IN $decs RETURN a, r, b",
    "MATCH (a:Clause)-[r:HAS_FINDING]->(b:Finding)-[:CAUSED_BY]->(d:Decision) WHERE d.id IN $decs RETURN a, r, b",
    "MATCH (a:Case)-[r:HAS_CLAUSE]->(b:Clause)-[:HAS_FINDING]->(:Finding)-[:CAUSED_BY]->(d:Decision) WHERE d.id IN $decs RETURN a, r, b",
    "MATCH (a:Case)-[r:FOR_TENDER]->(b:Tender) WHERE a.id IN $cases RETURN a, r, b",
    "MATCH (a:Action)-[r:TRIGGERED_BY]->(b:Decision) WHERE b.id IN $decs RETURN a, r, b",
    "MATCH (a:Action)-[r:FOR_CASE]->(b:Case) WHERE a.id IN $acts RETURN a, r, b",
]


def _props(p: dict) -> dict:
    out = {}
    for k, v in (p or {}).items():
        if k in _SKIP:
            continue
        if isinstance(v, str) and len(v) > (4000 if v.lstrip().startswith(("{", "[")) else 400):
            v = v[:400] + "…"
        if isinstance(v, list) and len(v) > 20:
            continue
        out[k] = v
    return out


def _name(label: str, p: dict) -> str:
    if label == "Claim":
        return f"{p.get('predicate', 'claim')} = {p.get('value_str', p.get('value', ''))}"
    if label == "Clause":
        return f"Clause {p.get('ref') or ''} · {(p.get('kind') or '').replace('_', ' ')}"
    for k in ("title", "name", "doc_id", "id", "key"):
        if p.get(k):
            return str(p[k])
    return label


def build_impact_graph(g, doc_id: str) -> dict:
    if not g.read("MATCH (d:Document {doc_id:$d}) RETURN d.doc_id LIMIT 1", {"d": doc_id}):
        raise HTTPException(404, f"no document {doc_id}")
    decs = [r["id"] for r in g.read(
        "MATCH (:Document {doc_id:$d})<-[:FROM]-(:Claim)<-[:BASED_ON]-(x:Decision) RETURN DISTINCT x.id AS id",
        {"d": doc_id}) if r["id"]]
    acts = [r["id"] for r in g.read(
        "MATCH (a:Action)-[:TRIGGERED_BY]->(x:Decision) WHERE x.id IN $decs RETURN DISTINCT a.id AS id",
        {"decs": decs}) if r["id"]]
    cases = [r["id"] for r in g.read(
        """MATCH (c:Case)-[:HAS_CLAUSE]->(:Clause)-[:HAS_FINDING]->(:Finding)-[:CAUSED_BY]->(x:Decision)
           WHERE x.id IN $decs RETURN DISTINCT c.id AS id""", {"decs": decs}) if r["id"]]
    params = {"d": doc_id, "decs": decs, "acts": acts, "cases": cases}
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    for q in _HOPS:
        q = q.replace("RETURN a, r, b", """RETURN toString(id(a)) AS ai, labels(a)[0] AS al, properties(a) AS ap,
                                                  type(r) AS rt, properties(r) AS rp, toString(id(r)) AS ri,
                                                  toString(id(startNode(r))) AS rs,
                                                  toString(id(b)) AS bi, labels(b)[0] AS bl, properties(b) AS bp LIMIT 60""")
        for row in g.read(q, params):
            for i, l, p in ((row["ai"], row["al"], row["ap"]), (row["bi"], row["bl"], row["bp"])):
                if i not in nodes:
                    pp = _props(p)
                    nodes[i] = {"id": i, "label": l, "name": _name(l, pp), "props": pp,
                                "focus": l == "Document" and pp.get("doc_id") == doc_id}
            src = row["rs"]
            dst = row["bi"] if src == row["ai"] else row["ai"]
            edges[row["ri"]] = {"id": row["ri"], "source": src, "target": dst, "type": row["rt"],
                                "props": _props(row["rp"])}
    return {"doc_id": doc_id, "nodes": list(nodes.values()), "edges": list(edges.values()),
            "decisions": decs, "actions": acts, "cases": cases}


@router.get("/api/views/impact-graph/{doc_id}")
def impact_graph(doc_id: str, x_session: str | None = Header(None)):
    from bidbrain.api.main import G
    return build_impact_graph(G(x_session), doc_id)
