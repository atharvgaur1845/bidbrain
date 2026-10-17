"""Provenance as a variable-length traversal: everything that depends on one document.

Every dependency edge in the graph points from the thing that relies to the thing relied on, ending at a
Document:

    (:Claim)-[:FROM]->(:Document)            (:Work|Bond|Credential|...)-[:EVIDENCED_BY|HAS_REFERENCE]->(:Document)
    (:Decision)-[:BASED_ON|SELECTS]->(:Claim)     (:Person)-[:HOLDS]->(:Credential)
    (:Bid)-[:CITES|SECURED_BY]->(:Work|Bond)      (:Requirement)-[:CITES_CERT]->(:Certification)
    (:Clause)-[:SATISFIED_BY]->(...)  (:Clause)-[:HAS_FINDING]->(:Finding)-[:CITES]->(...)  (:Case)-[:HAS_CLAUSE]->(:Clause)

so "what depends on document X" is one MATCH over `[:...*1..4]` into X. The answer is grouped by how far each
dependent is from the document, and the direct facts are split into those X is the only source for (withdraw X
and they lose their evidence) and those another document also supports.
"""
from __future__ import annotations

from bidbrain.graph.client import Graph
from bidbrain.tools.read import _edge, _node
from bidbrain.tools.registry import tool

DEP_RELS = ["FROM", "EVIDENCED_BY", "HAS_REFERENCE", "HOLDS", "BASED_ON", "SELECTS", "CITES", "SECURED_BY",
            "CITES_CERT", "SATISFIED_BY", "HAS_FINDING", "HAS_CLAUSE"]
SOURCE_RELS = ["FROM", "EVIDENCED_BY", "HAS_REFERENCE"]

DEPENDS_ON = """
MATCH (d:Document {doc_id:$doc})
MATCH p = (x)-[:%s*1..%d]->(d)
WHERE NOT x:Document
WITH d, x, p ORDER BY length(p)
WITH d, x, collect(p)[0] AS p
WITH d, x, length(p) AS depth, [r IN relationships(p) | type(r)] AS via, nodes(p) AS chain
OPTIONAL MATCH (x)-[:FROM|EVIDENCED_BY|HAS_REFERENCE]->(other:Document) WHERE depth = 1 AND other <> d
WITH x, depth, via, chain, count(DISTINCT other) AS other_sources
RETURN labels(x)[0] AS label, coalesce(x.key, x.id) AS key,
       coalesce(x.title, x.name, x.predicate + ' of ' + x.subject_key, x.text, x.kind, x.key, x.id) AS name,
       depth, via, other_sources, x.status AS status,
       [n IN chain | {label: labels(n)[0], key: coalesce(n.key, n.id, n.doc_id)}] AS chain
ORDER BY depth, label, key"""


def _query(depth: int) -> str:
    return DEPENDS_ON % ("|".join(DEP_RELS), depth)


@tool("depends_on",
      """Provenance: everything in the graph that depends on one document, found with one
      variable-length Cypher match (up to 4 hops into the document over FROM, EVIDENCED_BY,
      HAS_REFERENCE, HOLDS, BASED_ON, SELECTS, CITES, SECURED_BY, CITES_CERT, SATISFIED_BY,
      HAS_FINDING, HAS_CLAUSE). Returns the dependents grouped by distance and label: the facts
      read from the document (claims, works, bonds, credentials ...), then what relies on those
      (decisions, past bids, people holding a credential, compliance rows, open-case clauses).
      `sole_source` lists the direct facts no other document supports: what loses its evidence
      if the document is withdrawn or found forged. Read-only.""",
      {"doc_id": {"type": "string"}, "depth": {"type": "integer"}}, ["doc_id"])
def depends_on(g: Graph, doc_id: str, depth: int = 4) -> dict:
    depth = max(1, min(int(depth), 4))
    doc = g.read("MATCH (d:Document) WHERE d.doc_id = $id OR toUpper(d.doc_id) = toUpper($id) "
                 "RETURN d.doc_id AS doc_id, d.doc_type AS doc_type, d.filename AS filename", {"id": doc_id.strip()})
    if not doc:
        near = g.read("MATCH (d:Document) WHERE toUpper(d.doc_id) CONTAINS toUpper($id) RETURN d.doc_id AS id "
                      "ORDER BY id LIMIT 8", {"id": doc_id.strip()})
        raise ValueError(f"no document {doc_id!r}" + (f"; did you mean {[r['id'] for r in near]}" if near else ""))
    d = doc[0]
    q = _query(depth)
    rows = g.read(q, {"doc": d["doc_id"]})
    groups: dict[tuple, dict] = {}
    for r in rows:
        k = (r["depth"], r["label"], " -> ".join(r["via"]))
        grp = groups.setdefault(k, {"depth": r["depth"], "label": r["label"], "via": " -> ".join(r["via"]),
                                    "n": 0, "examples": []})
        grp["n"] += 1
        if len(grp["examples"]) < 6:
            grp["examples"].append(r["name"])
    direct = [r for r in rows if r["depth"] == 1]
    sole = [{"label": r["label"], "key": r["key"], "name": r["name"]} for r in direct
            if not r["other_sources"] and r["status"] != "rejected"]
    nodes = [_node("Document", d["doc_id"], d["doc_id"])]
    edges = []
    for r in rows[:150]:
        nodes.append(_node(r["label"], r["key"], r["name"]))
        ch = r["chain"]
        edges += [_edge(f"{a['label']}:{a['key']}", f"{b['label']}:{b['key']}", t)
                  for a, b, t in zip(ch, ch[1:], r["via"])]
    by_depth: dict[int, int] = {}
    for r in rows:
        by_depth[r["depth"]] = by_depth.get(r["depth"], 0) + 1
    return {"result": {"document": d, "dependents": len(rows),
                       "by_depth": {str(k): v for k, v in sorted(by_depth.items())},
                       "groups": sorted(groups.values(), key=lambda x: (x["depth"], -x["n"])),
                       "sole_source": sole[:60], "sole_source_count": len(sole),
                       "corroborated_count": len(direct) - len(sole)},
            "evidence": [{"doc_id": d["doc_id"], "page": 1, "what": f"{d['doc_type']}: {d['filename']}"}],
            "subgraph": {"nodes": nodes, "edges": edges}, "cypher": [q]}
