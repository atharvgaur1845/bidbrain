"""Multi-hop traversals written as one Cypher statement.

`trace_from_credential` is the README's hero question as a single query: from a person's credential, through
the work they led, to that work's client, to every work the client commissioned, to each completion
certificate. Six relationships, one round trip; the database (not Python) does the joins and the aggregation,
and the path itself comes back so the answer can be shown hop by hop.
"""
from __future__ import annotations

import re

from bidbrain.graph.client import Graph
from bidbrain.tools.read import _edge, _node, _resolve
from bidbrain.tools.registry import tool

TRACE = """
MATCH head = (cr:Credential)<-[:HOLDS]-(p:Person)<-[:LED_BY]-(anchor:Work)-[:FOR_CLIENT]->(c:Client)
WHERE ($person IS NULL OR p.key = $person) AND ($cred_id IS NULL OR cr.key = $cred_id)
  AND ($kind IS NULL OR toLower(cr.kind) CONTAINS toLower($kind)) AND ($work IS NULL OR anchor.key = $work)
MATCH tail = (c)<-[:FOR_CLIENT]-(w:Work)-[e:EVIDENCED_BY {kind:'completion_certificate'}]->(d:Document),
      (w)-[:LED_BY]->(lead:Person)
WITH cr, p, anchor, c, head, w, e, d, lead, length(head) + length(tail) AS hops
ORDER BY w.value DESC, w.key
RETURN cr.key AS credential_id, cr.kind AS credential_kind, cr.issued AS issued, cr.valid_through AS valid_through,
       p.key AS person_key, p.name AS person,
       anchor.key AS anchor_key, anchor.title AS anchor, anchor.completed AS anchor_completed,
       cr.issued_day <= anchor.completed_day AS held_when_anchor_completed,
       c.key AS client_key, c.name AS client,
       [n IN nodes(head) | {label: labels(n)[0], key: n.key, name: coalesce(n.title, n.name, n.kind + ' ' + n.key)}] AS chain,
       max(hops) AS hops, count(w) AS n_works, sum(w.value) AS total_value,
       collect({key: w.key, title: w.title, value: w.value, completed: w.completed, category: w.category,
                lead: lead.name, doc: d.doc_id, page: e.page}) AS works
ORDER BY total_value DESC"""

# only run when the trace is empty, to say which hop broke
WHY_EMPTY = """
OPTIONAL MATCH (p:Person {key:$person})-[:HOLDS]->(cr:Credential)
WHERE ($cred_id IS NULL OR cr.key = $cred_id) AND ($kind IS NULL OR toLower(cr.kind) CONTAINS toLower($kind))
WITH collect(DISTINCT cr.kind + ' ' + cr.key) AS creds
OPTIONAL MATCH (w:Work {key:$work})-[:LED_BY]->(lead:Person)
RETURN creds, lead.name AS work_led_by, lead.key AS work_lead_key,
       size([(:Person {key:$person})<-[:LED_BY]-(x:Work) | x]) AS led"""


@tool("trace_from_credential",
      """The 6-hop chain in ONE Cypher query: credential <-HOLDS- person <-LED_BY- work -FOR_CLIENT->
      client <-FOR_CLIENT- every work for that client -EVIDENCED_BY-> its completion certificate. For
      questions like "starting from Sunita Joshi's Six Sigma certificate on the Pkg-107 job, what is the
      combined value of every work for that client?". Give `person` and/or `credential` (an id like
      PMI-200039, or a kind like 'PMP' / 'Six Sigma'), and optionally the anchor `work`. Returns, per
      credential and anchor work: the chain, the client, its works with value, lead and certificate page,
      and the exact total. `held_when_anchor_completed` says whether the credential had been issued by the
      time the anchor work was completed.""",
      {"person": {"type": "string"}, "credential": {"type": "string"}, "work": {"type": "string"}})
def trace_from_credential(g: Graph, person: str | None = None, credential: str | None = None,
                          work: str | None = None) -> dict:
    if not (person or credential):
        raise ValueError("give a person, a credential, or both")
    resolved = {}
    pk = wk = None
    if person:
        pk, r = _resolve(g, person, "person")
        resolved["person"] = r or pk
    if work:
        wk, r = _resolve(g, work, "work")
        resolved["work"] = r or wk
    cred_id = credential if credential and re.fullmatch(r"[A-Z0-9]+-\d+", credential.strip()) else None
    kind = None if cred_id or not credential else credential.strip()
    params = {"person": pk, "cred_id": cred_id, "kind": kind, "work": wk}
    rows = g.read(TRACE, params)
    cypher = [TRACE]
    if not rows:
        why = []
        if pk:
            w = g.read(WHY_EMPTY, params)[0]
            cypher.append(WHY_EMPTY)
            if not w["creds"]:
                why.append(f"{pk} holds no credential matching {credential!r}" if credential
                           else f"{pk} holds no credential")
            if not w["led"]:
                why.append(f"{pk} led no completed work, so there is no client to trace to")
            if wk and w["work_lead_key"] and w["work_lead_key"] != pk:
                why.append(f"{wk} was led by {w['work_led_by']}, not {pk}")
        return {"result": {"chains": [], "why_empty": why or ["no credential holder led a work matching the scope"]},
                "resolved_as": resolved, "cypher": cypher}
    nodes, edges, ev, seen = [], [], [], set()

    def add(label, key, name, **kw):
        i = f"{label}:{key}"
        if i not in seen:
            seen.add(i)
            nodes.append(_node(label, key, name, **kw))
        return i

    for r in rows:
        ids = [add(n["label"], n["key"], n["name"]) for n in r["chain"]]
        for a, b, t in zip(ids, ids[1:], ["HOLDS", "LED_BY", "FOR_CLIENT"]):
            edges.append(_edge(b, a, t) if t != "FOR_CLIENT" else _edge(a, b, t))
        cid = f"Client:{r['client_key']}"
        for w in r["works"]:
            wid = add("Work", w["key"], w["title"], value=w["value"])
            did = add("Document", w["doc"], w["doc"], page=w["page"])
            edges += [_edge(wid, cid, "FOR_CLIENT"), _edge(wid, did, "EVIDENCED_BY")]
            ev.append({"doc_id": w["doc"], "page": w["page"],
                       "what": f"{w['title']}: value {w['value']:,}, completed {w['completed']}"})
        r["total_value"] = int(r["total_value"] or 0)
        r["path"] = " → ".join(n["name"] for n in r["chain"]) + f" → {r['n_works']} works → certificates"
    return {"result": {"chains": rows, "n_chains": len(rows)}, "resolved_as": resolved, "evidence": ev,
            "subgraph": {"nodes": nodes, "edges": edges}, "cypher": cypher}
