"""Graph algorithms as agent tools (FalkorDB's built-in `algo.*` procedures)."""
from __future__ import annotations

from bidbrain.graph.client import Graph
from bidbrain.tools.read import _edge, _node, _resolve
from bidbrain.tools.registry import tool

LABEL_KIND = {"Person": "person", "Client": "client", "Work": "work", "Tender": "tender"}


@tool("key_people",
      """Who the company depends on. algo='betweenness' (default) ranks people by how many
      shortest person<->client paths run through them — the knowledge brokers whose loss would
      disconnect client relationships ('bus factor'). algo='reach' ranks by distinct clients and
      works led. Optional client / category to restrict to people who worked there.""",
      {"algo": {"type": "string", "enum": ["betweenness", "reach"]},
       "client": {"type": "string"}, "category": {"type": "string"}, "limit": {"type": "integer"}})
def key_people(g: Graph, algo: str = "betweenness", client: str | None = None,
               category: str | None = None, limit: int = 10) -> dict:
    ck = _resolve(g, client, "client")[0] if client else None
    cat = _resolve(g, category, "category")[0] if category else None
    if algo == "betweenness":
        q = """CALL algo.betweenness({nodeLabels:['Person','Client'], relationshipTypes:['WORKED_FOR','ENGAGED']})
               YIELD node, score WITH node AS p, score WHERE p:Person
               MATCH (p)<-[:LED_BY]-(w:Work)-[:FOR_CLIENT]->(c:Client)
               WHERE ($c IS NULL OR c.key = $c) AND ($cat IS NULL OR w.category = $cat)
               RETURN p.key AS key, p.name AS name, p.designation AS designation, round(score) AS score,
                      count(DISTINCT c) AS clients, count(w) AS works, sum(w.value) AS value
               ORDER BY score DESC LIMIT $lim"""
    else:
        q = """MATCH (p:Person)<-[:LED_BY]-(w:Work)-[:FOR_CLIENT]->(c:Client)
               WHERE ($c IS NULL OR c.key = $c) AND ($cat IS NULL OR w.category = $cat)
               RETURN p.key AS key, p.name AS name, p.designation AS designation, count(DISTINCT c) AS score,
                      count(DISTINCT c) AS clients, count(w) AS works, sum(w.value) AS value
               ORDER BY score DESC, works DESC LIMIT $lim"""
    rows = g.read(q, {"c": ck, "cat": cat, "lim": int(limit)})
    sg = {"nodes": [_node("Person", r["key"], r["name"], score=r["score"]) for r in rows], "edges": []}
    return {"result": {"algo": algo, "people": rows}, "subgraph": sg, "cypher": [q]}


@tool("who_knows",
      """Who on the team has worked with a client, in a category, or in a state before —
      ranked by works led, with total value and most recent completion. The first question
      to ask when staffing a bid or picking a reference contact.""",
      {"client": {"type": "string"}, "category": {"type": "string"}, "state": {"type": "string"},
       "limit": {"type": "integer"}})
def who_knows(g: Graph, client: str | None = None, category: str | None = None,
              state: str | None = None, limit: int = 8) -> dict:
    ck = _resolve(g, client, "client")[0] if client else None
    cat = _resolve(g, category, "category")[0] if category else None
    q = """MATCH (p:Person)<-[:LED_BY]-(w:Work)-[:FOR_CLIENT]->(c:Client)
           WHERE ($c IS NULL OR c.key = $c) AND ($cat IS NULL OR w.category = $cat)
             AND ($st IS NULL OR toLower(w.state) = toLower($st))
           OPTIONAL MATCH (p)-[:HOLDS]->(cr:Credential)
           RETURN p.key AS key, p.name AS name, p.designation AS designation,
                  p.experience_years AS experience_years, count(DISTINCT w) AS works,
                  sum(DISTINCT w.value) AS value, max(w.completed) AS latest,
                  collect(DISTINCT cr.kind) AS credentials
           ORDER BY works DESC, value DESC LIMIT $lim"""
    rows = g.read(q, {"c": ck, "cat": cat, "st": state, "lim": int(limit)})
    nodes = [_node("Person", r["key"], r["name"]) for r in rows]
    if ck:
        nodes.append(_node("Client", ck, ck))
    edges = [_edge(f"Person:{r['key']}", f"Client:{ck}", "WORKED_FOR") for r in rows] if ck else []
    return {"result": rows, "subgraph": {"nodes": nodes, "edges": edges}, "cypher": [q]}


@tool("explain_path",
      """How two entities are connected: up to 3 shortest paths (algo.SPpaths, both directions,
      max 6 hops) over work/client/person/credential/tender edges. Use it to show a judge *why*
      two things are related.""",
      {"a": {"type": "string"}, "a_kind": {"type": "string", "enum": list(LABEL_KIND.values())},
       "b": {"type": "string"}, "b_kind": {"type": "string", "enum": list(LABEL_KIND.values())}},
      ["a", "a_kind", "b", "b_kind"])
def explain_path(g: Graph, a: str, a_kind: str, b: str, b_kind: str) -> dict:
    inv = {v: k for k, v in LABEL_KIND.items()}
    ak, bk = _resolve(g, a, a_kind)[0], _resolve(g, b, b_kind)[0]
    q = f"""MATCH (a:{inv[a_kind]} {{key:$a}}), (b:{inv[b_kind]} {{key:$b}})
            CALL algo.SPpaths({{sourceNode:a, targetNode:b, relTypes:['LED_BY','FOR_CLIENT','HOLDS','SECURES','REQUIRES','HAS_REFERENCE'],
                               relDirection:'both', maxLen:6, pathCount:3}})
            YIELD path, pathWeight
            RETURN [n IN nodes(path) | {{label: labels(n)[0], key: n.key, name: coalesce(n.name, n.title, n.key)}}] AS nodes,
                   [r IN relationships(path) | type(r)] AS rels, pathWeight AS hops
            ORDER BY hops"""
    rows = g.read(q, {"a": ak, "b": bk})
    nodes, edges, seen = [], [], set()
    for r in rows:
        ids = [f"{n['label']}:{n['key']}" for n in r["nodes"]]
        for n, i in zip(r["nodes"], ids):
            if i not in seen:
                nodes.append(_node(n["label"], n["key"], n["name"]))
                seen.add(i)
        edges += [_edge(ids[i], ids[i + 1], t) for i, t in enumerate(r["rels"])]
    return {"result": [{"hops": int(r["hops"]), "path": " → ".join(n["name"] for n in r["nodes"]),
                        "rels": r["rels"]} for r in rows],
            "subgraph": {"nodes": nodes, "edges": edges}, "cypher": [q]}


@tool("isolated_entities",
      """Weakly connected components (algo.WCC) of the person<->client graph: finds people who
      never led a completed work and clients we bill but hold no completion certificate for —
      gaps in the evidence base.""")
def isolated_entities(g: Graph) -> dict:
    q = """CALL algo.WCC({nodeLabels:['Person','Client'], relationshipTypes:['WORKED_FOR','ENGAGED']})
           YIELD node, componentId
           WITH componentId, collect(node) AS members
           RETURN componentId, size(members) AS size,
                  [m IN members | {label: labels(m)[0], key: m.key, name: m.name}] AS members
           ORDER BY size DESC"""
    rows = g.read(q)
    main = rows[0] if rows else None
    isolated = [m for r in rows[1:] for m in r["members"]]
    return {"result": {"main_component_size": main["size"] if main else 0, "isolated": isolated,
                       "n_components": len(rows)},
            "subgraph": {"nodes": [_node(m["label"], m["key"], m["name"]) for m in isolated], "edges": []},
            "cypher": [q]}


@tool("communities",
      """Community detection (algo.labelPropagation) over the people graph projected by shared
      clients (two people are linked if they served >= 2 of the same clients). Returns clusters
      of people who share client relationships — useful for staffing a bid team that already
      knows the client.""")
def communities(g: Graph) -> dict:
    q = """CALL algo.labelPropagation({nodeLabels:['Person'], relationshipTypes:['SHARES_CLIENTS'], maxIterations:50})
           YIELD node, communityId
           RETURN communityId, collect(node.name) AS people, size(collect(node)) AS size ORDER BY size DESC"""
    rows = g.read(q)
    return {"result": rows, "cypher": [q]}
