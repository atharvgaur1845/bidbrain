"""Graph algorithms as agent tools (FalkorDB's built-in `algo.*` procedures)."""
from __future__ import annotations

from bidbrain.graph.client import Graph
from bidbrain.tools.read import _edge, _node, _resolve
from bidbrain.tools.registry import tool

LABEL_KIND = {"Person": "person", "Client": "client", "Work": "work", "Tender": "tender"}


SEEDS = 32           # seeded algo.betweenness runs averaged by key_people


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
        # algo.betweenness samples source nodes even when samplingSize exceeds the node count, so one call's
        # ranking past the top two depends on the seed. The score is the mean over SEEDS seeded runs: two disjoint
        # seed sets give the same top 6, and the top 3 match exact Brandes on this projection.
        bq = """CALL algo.betweenness({nodeLabels:['Person','Client'], relationshipTypes:['WORKED_FOR','ENGAGED'],
                                     samplingSize:100000, samplingSeed:$seed})
                YIELD node, score WITH node AS p, score WHERE p:Person RETURN p.key AS key, score"""
        mean: dict[str, float] = {}
        for seed in range(1, SEEDS + 1):
            for r in g.read(bq, {"seed": seed}):
                mean[r["key"]] = mean.get(r["key"], 0.0) + r["score"] / SEEDS
        q = """UNWIND $scores AS s MATCH (p:Person {key:s.key})
               MATCH (p)<-[:LED_BY]-(w:Work)-[:FOR_CLIENT]->(c:Client)
               WHERE ($c IS NULL OR c.key = $c) AND ($cat IS NULL OR w.category = $cat)
               RETURN p.key AS key, p.name AS name, p.designation AS designation, round(s.score) AS score,
                      count(DISTINCT c) AS clients, count(w) AS works, sum(w.value) AS value
               ORDER BY score DESC LIMIT $lim"""
        rows = g.read(q, {"scores": [{"key": k, "score": v} for k, v in mean.items()], "c": ck, "cat": cat,
                          "lim": int(limit)})
        sg = {"nodes": [_node("Person", r["key"], r["name"], score=r["score"]) for r in rows], "edges": []}
        return {"result": {"algo": algo, "runs_averaged": SEEDS, "people": rows}, "subgraph": sg, "cypher": [bq, q]}
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
           WITH p, count(w) AS works, sum(w.value) AS value, max(w.completed) AS latest
           OPTIONAL MATCH (p)-[:HOLDS]->(cr:Credential)
           RETURN p.key AS key, p.name AS name, p.designation AS designation,
                  p.experience_years AS experience_years, works, value, latest,
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


WCC_SCOPES = {
    # the evidence a similar-works or key-personnel clause stands on
    "provenance": {
        "labels": ["Document", "Work", "Client", "Person", "Credential"],
        "rels": ["EVIDENCED_BY", "HAS_REFERENCE", "FOR_CLIENT", "LED_BY", "HOLDS"],
        "doc_types": ["completion_certificate", "company_completion_certificate", "reference_letter", "cv",
                      "personnel_certificate", "past_performance_portfolio"]},
    # every document, and every entity that should be traceable to one. Hub edges that are not evidence
    # (Company-OWNS/EMPLOYS, FOR_FY, IN_STATE, IN_CATEGORY) are left out: they would join everything and
    # hide an entity that has no source document.
    "corpus": {
        "labels": ["Document", "Company", "Work", "Client", "Person", "Credential", "Certification", "Tender",
                   "Requirement", "Bond", "Bank", "Contract", "BOQItem", "Measurement", "Invoice",
                   "FinancialStatement", "AnnualReport", "TBLine", "LedgerAccount", "Asset", "Bid"],
        "rels": ["EVIDENCED_BY", "HAS_REFERENCE", "FOR_CLIENT", "LED_BY", "HOLDS", "SECURES", "ISSUED_BY",
                 "REQUIRES", "CITES_CERT", "BILLED_TO", "HAS_ITEM", "MEASURES", "FOR_TENDER", "CITES",
                 "PROPOSES", "TO_CLIENT", "SECURED_BY"],
        "doc_types": None},
}

WCC = """
CALL algo.WCC({nodeLabels:$labels, relationshipTypes:$rels}) YIELD node, componentId
WITH node, componentId WHERE NOT node:Document OR $doc_types IS NULL OR node.doc_type IN $doc_types
WITH componentId, labels(node)[0] AS label, node.doc_type AS doc_type, collect(node) AS ns
WITH componentId, sum(size(ns)) AS size,
     collect({label: label, doc_type: doc_type, n: size(ns),
              sample: [x IN ns[0..40] | {key: x.key, name: coalesce(x.name, x.title, x.doc_id, x.key)}]}) AS parts
RETURN componentId, size, parts
ORDER BY size DESC"""


def _classify(parts: list[dict]) -> tuple[str, str]:
    """What a component outside the main one means. Islands that hold both documents and the facts they
    evidence (e.g. one fiscal year's statement + ledgers in scope='corpus') are reported, not flagged."""
    labels = {p["label"] for p in parts}
    if labels == {"Document"}:
        types = ", ".join(sorted({p["doc_type"] or "?" for p in parts}))
        return "uncited_document", (f"{types}: in the corpus, but no fact in the graph cites it; evidence no "
                                    "tool can reach (content not modelled, or a lost link)")
    if "Client" in labels and "Work" not in labels and labels <= {"Client", "Document"}:
        return "client_without_work", ("client with no completed work on record (known from invoices or contracts "
                                       "only): nothing for them can be cited as experience")
    if "Document" not in labels:
        return "unprovenanced", f"{', '.join(sorted(labels))} with no path to any source document: its values cannot be cited"
    if "Person" in labels and "Work" not in labels:
        return "credentialed_no_work", ("staff with a CV and credentials but no led work on record: can be named as "
                                        "key personnel, but bring no similar-works evidence or client relationship")
    return "island", "documented, but not linked to the main evidence graph by an evidence edge"


@tool("isolated_entities",
      """Evidence-integrity check with weakly connected components (algo.WCC). scope='provenance'
      (default) runs over Document-Work-Client-Person-Credential, the evidence a similar-works or
      key-personnel clause stands on; scope='corpus' runs over every document and every entity that
      should be traceable to one. The main component is the connected evidence base; everything outside
      it is a finding: documents no fact cites, entities with no source document, credentialed staff
      who never led a work, clients with no completed work. Returns component sizes and the orphans,
      grouped, with what each group means.""",
      {"scope": {"type": "string", "enum": ["provenance", "corpus"]}})
def isolated_entities(g: Graph, scope: str = "provenance") -> dict:
    cfg = WCC_SCOPES[scope]
    rows = g.read(WCC, cfg)
    for r in rows:
        r["size"] = int(r["size"])
    main = rows[0] if rows else {"size": 0, "parts": []}
    findings: dict[tuple, dict] = {}
    islands: dict[str, dict] = {}
    isolated = []
    for r in rows[1:]:
        kind, meaning = _classify(r["parts"])
        if kind == "island":
            sig = " + ".join(sorted({p["label"] if p["label"] != "Document" else f"Document({p['doc_type']})"
                                     for p in r["parts"]}))
            isl = islands.setdefault(sig, {"members": sig, "components": 0, "nodes": 0})
            isl["components"] += 1
            isl["nodes"] += r["size"]
            continue
        for p in r["parts"]:
            gk = (kind, p["label"], p["doc_type"])
            f = findings.setdefault(gk, {"finding": kind, "label": p["label"], "doc_type": p["doc_type"],
                                         "count": 0, "components": 0, "meaning": meaning, "examples": []})
            f["count"] += p["n"]
            f["examples"] += [s["name"] for s in p["sample"]][: max(0, 8 - len(f["examples"]))]
            isolated += [{"label": p["label"], "key": s["key"], "name": s["name"], "finding": kind}
                         for s in p["sample"]]
        for gk in {(kind, p["label"], p["doc_type"]) for p in r["parts"]}:
            findings[gk]["components"] += 1
    sizes = [r["size"] for r in rows]
    out = {"scope": scope, "main_component_size": main["size"], "n_components": len(rows),
           "component_sizes": sizes[:12] + ([f"... {len(sizes) - 12} more"] if len(sizes) > 12 else []),
           "main_component": sorted(({"label": p["label"], "doc_type": p["doc_type"], "n": p["n"]}
                                     for p in main["parts"]), key=lambda x: -x["n"]),
           "outside_main": sum(sizes[1:]),
           "findings": sorted(findings.values(), key=lambda f: (f["finding"], -f["count"])),
           "islands": sorted(islands.values(), key=lambda i: -i["nodes"]),
           "isolated": isolated[:120]}
    if not findings:
        out["verdict"] = "every node in scope is in one component: no orphaned evidence"
    return {"result": out,
            "subgraph": {"nodes": [_node(m["label"], m["key"], m["name"]) for m in isolated[:60]], "edges": []},
            "cypher": [_wcc_text(cfg)]}


def _wcc_text(cfg: dict) -> str:
    """The WCC query with its parameters inlined, as shown in explain mode and docs/CYPHER.md."""
    q = WCC.replace("$labels", str(cfg["labels"])).replace("$rels", str(cfg["rels"]))
    if cfg["doc_types"]:
        return q.replace("$doc_types IS NULL OR ", "").replace("$doc_types", str(cfg["doc_types"]))
    return q.replace("WITH node, componentId WHERE NOT node:Document OR $doc_types IS NULL OR node.doc_type IN $doc_types\n", "")


COMMUNITY_PEOPLE = """
CALL algo.labelPropagation({nodeLabels:['Person'], relationshipTypes:['OVERLAPS_CLIENTS'], maxIterations:50})
YIELD node, communityId
WITH communityId, collect(node) AS members
UNWIND members AS p
OPTIONAL MATCH (p)-[r:WORKED_FOR]->(c:Client)
WITH communityId, members, c, count(DISTINCT p) AS served_by, sum(r.works) AS works
WITH communityId, members, c, served_by, works, size([(c)<-[:WORKED_FOR]-(x:Person) | x]) AS leads_total
ORDER BY served_by * served_by / toFloat(leads_total) DESC, works DESC, c.name
WITH communityId, members,
     collect(CASE WHEN c IS NULL THEN NULL ELSE {client: c.name, key: c.key, served_by: served_by,
                                                leads_total: leads_total, works: works} END) AS clients
RETURN communityId, size(members) AS size, [m IN members | m.name] AS people, [m IN members | m.key] AS keys,
       [x IN clients WHERE x.served_by >= 2][0..5] AS shared_clients, size(clients) AS clients_total
ORDER BY size DESC, communityId"""

COMMUNITY_CLIENTS = """
CALL algo.labelPropagation({nodeLabels:['Client'], relationshipTypes:['SHARES_LEADS'], maxIterations:50})
YIELD node, communityId
WITH communityId, collect(node) AS members
UNWIND members AS c
OPTIONAL MATCH (c)<-[r:WORKED_FOR]-(p:Person)
WITH communityId, members, p, count(DISTINCT c) AS serves, sum(r.works) AS works
WITH communityId, members, p, serves, works, size([(p)-[:WORKED_FOR]->(x:Client) | x]) AS clients_total
ORDER BY serves * serves / toFloat(clients_total) DESC, works DESC, p.name
WITH communityId, members,
     collect(CASE WHEN p IS NULL THEN NULL ELSE {person: p.name, key: p.key, serves: serves,
                                                clients_total: clients_total, works: works} END) AS leads
RETURN communityId, size(members) AS size, [m IN members | m.name] AS clients, [m IN members | m.key] AS keys,
       [x IN leads WHERE x.serves >= 2][0..6] AS shared_leads
ORDER BY size DESC, communityId"""

CO_GRAPH = {  # the full weighted graph the partition is scored against (modularity)
    "people": """MATCH (a:Person)-[:WORKED_FOR]->(c:Client)<-[:WORKED_FOR]-(b:Person) WHERE a.key < b.key
                 RETURN a.key AS a, b.key AS b, count(c) AS w""",
    "clients": """MATCH (a:Client)<-[:WORKED_FOR]-(p:Person)-[:WORKED_FOR]->(b:Client) WHERE a.key < b.key
                  RETURN a.key AS a, b.key AS b, count(p) AS w"""}


def modularity(edges: list[dict], part: dict[str, int]) -> float:
    """Newman modularity of a partition over a weighted undirected edge list ({a, b, w}). A node missing
    from `part` is its own community. 0 = no better than one block (or random); higher = real clusters."""
    m = sum(e["w"] for e in edges)
    if not m:
        return 0.0
    deg: dict[str, float] = {}
    for e in edges:
        deg[e["a"]] = deg.get(e["a"], 0) + e["w"]
        deg[e["b"]] = deg.get(e["b"], 0) + e["w"]
    inside = sum(e["w"] for e in edges if part.get(e["a"], e["a"]) == part.get(e["b"], e["b"]))
    tot: dict = {}
    for n, d in deg.items():
        tot[part.get(n, n)] = tot.get(part.get(n, n), 0) + d
    return round(inside / m - sum((d / (2 * m)) ** 2 for d in tot.values()), 3)


@tool("communities",
      """Community detection (algo.labelPropagation). of='people' (default): client circles — people
      linked when they led work for >= 2 of the same clients AND those make up >= 25% of their combined
      client lists (Jaccard), so a community is a group that keeps serving the same accounts; each comes
      with the clients that define it. Use it to staff a bid team that already knows the client. of='clients':
      accounts linked when >= 2 of the same project leads served both; a community is a set of clients
      that draws on one pool of people (lose a lead, and every client in the set is exposed). Size-1
      communities are people/clients with no strong overlap. `modularity` scores the split against the full
      co-client graph (0 = no better than one block).""",
      {"of": {"type": "string", "enum": ["people", "clients"]}})
def communities(g: Graph, of: str = "people") -> dict:
    q = COMMUNITY_PEOPLE if of == "people" else COMMUNITY_CLIENTS
    rows = g.read(q)
    part = {k: r["communityId"] for r in rows for k in r["keys"]}
    q_full = CO_GRAPH[of]
    edges = g.read(q_full)
    score = modularity(edges, part)
    label = "Person" if of == "people" else "Client"
    nodes, links = [], []
    for r in rows:
        if of == "people":
            names = [f"{c['client']} ({c['served_by']} of its {c['leads_total']} leads)" for c in r["shared_clients"][:3]]
            r["basis"] = (f"{r['size']} people; defined by " + ", ".join(names)) if names and r["size"] > 1 \
                else "no strong client overlap with anyone (fewer than 2 shared clients or Jaccard < 0.25)"
        else:
            names = [f"{p['person']} ({p['serves']} of these clients)" for p in r["shared_leads"][:3]]
            r["basis"] = (f"{r['size']} clients drawing on the same leads: " + ", ".join(names)) if names and r["size"] > 1 \
                else "no two project leads in common with any other client"
        r["modularity"] = score
        if r["size"] > 1:
            hub = f"Community:{of}-{r['communityId']}"
            nodes.append(_node("Community", f"{of}-{r['communityId']}", r["basis"].split(";")[0]))
            for k, n in zip(r["keys"], r["people"] if of == "people" else r["clients"]):
                nodes.append(_node(label, k, n))
                links.append(_edge(f"{label}:{k}", hub, "IN_COMMUNITY"))
    sizes = [r["size"] for r in rows]
    return {"result": rows,
            "summary": {"of": of, "sizes": sizes, "communities": sum(1 for s in sizes if s > 1),
                        "singletons": sum(1 for s in sizes if s == 1), "modularity": score,
                        "projection": "OVERLAPS_CLIENTS (shared >= 2, Jaccard >= 0.25)" if of == "people"
                        else "SHARES_LEADS (shared leads >= 2)"},
            "subgraph": {"nodes": nodes, "edges": links}, "cypher": [q, q_full]}
