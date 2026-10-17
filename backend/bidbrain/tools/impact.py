"""Change impact and point-in-time facts: what a document changed, and what we believed when.

`change_impact(doc_id)` reconstructs the whole loop from the graph alone (no text search):

    (Document)<-[:FROM]-(Claim)<-[:BASED_ON]-(Decision)-[:SELECTS]->(winner)-[:ABOUT]->(subject)
    (Decision)<-[:CAUSED_BY]-(Finding {kind:'ripple'})<-[:HAS_FINDING]-(Clause)<-[:HAS_CLAUSE]-(Case)
    (Decision)<-[:TRIGGERED_BY]-(Action)

`fact_as_of(key, predicate, as_of)` answers "what did the company believe on date T" from the claims' transaction
time (`recorded_at`, when the graph learned the claim) and `invalidated_at` (when a decision set it aside), so a
superseded value is never lost.
"""
from __future__ import annotations

import datetime as dt

from bidbrain.graph.client import Graph
from bidbrain.tools.registry import tool


@tool("change_impact",
      """Everything one document changed, from the graph: the claims it carried, the decisions they triggered
      (policy, before -> after), the open bids whose clauses flipped because of each decision, and the follow-up
      actions created. Param doc_id (e.g. 'DOC-DEMO-1-20827').""",
      {"doc_id": {"type": "string"}}, ["doc_id"])
def change_impact(g: Graph, doc_id: str) -> dict:
    doc = g.read("MATCH (d:Document {doc_id:$d}) RETURN d.doc_id AS doc_id, d.title AS title, d.doc_type AS doc_type",
                 {"d": doc_id})
    if not doc:
        raise ValueError(f"no document {doc_id} in this workspace")
    q = """MATCH (d:Document {doc_id:$d})<-[:FROM]-(c:Claim)-[:ABOUT]->(s)
           OPTIONAL MATCH (dec:Decision)-[:BASED_ON]->(c)
           OPTIONAL MATCH (dec)-[:SELECTS]->(win:Claim)
           OPTIONAL MATCH (dec)-[:BASED_ON]->(other:Claim) WHERE other.id <> win.id
           RETURN c.id AS claim, c.predicate AS predicate, c.value_str AS value, c.page AS page, c.status AS status,
                  labels(s)[0] AS subject_label, s.key AS subject_key, coalesce(s.title, s.name, s.key) AS subject,
                  dec.id AS decision, dec.policy AS policy, dec.rationale AS rationale, dec.at AS decided_at,
                  win.value_str AS chosen, collect(DISTINCT other.value_str) AS replaced"""
    rows = g.read(q, {"d": doc_id})
    decisions = sorted({r["decision"] for r in rows if r["decision"]})
    flips = g.read("""MATCH (dec:Decision)<-[:CAUSED_BY]-(f:Finding {kind:'ripple'})<-[:HAS_FINDING]-(k:Clause)
                             <-[:HAS_CLAUSE]-(c:Case)
                      WHERE dec.id IN $ds
                      RETURN dec.id AS decision, c.id AS case_id, c.title AS case, k.ref AS clause, k.kind AS kind,
                             f.text AS what, f.severity AS severity, k.verdict AS verdict_now, c.verdict AS case_now,
                             c.approved_recommendation AS approved""", {"ds": decisions})
    acts = g.read("""MATCH (a:Action)-[:TRIGGERED_BY]->(dec:Decision) WHERE dec.id IN $ds
                     RETURN a.id AS id, a.kind AS kind, a.status AS status, a.title AS title, a.case_id AS case_id,
                            dec.id AS decision""", {"ds": decisions})
    cypher = [q]
    return {"result": {"document": doc[0], "claims": rows, "decisions": decisions, "affected": flips,
                       "actions": acts,
                       "summary": f"{len(rows)} claim(s), {len(decisions)} decision(s), "
                                  f"{len({f['case_id'] for f in flips})} open bid(s) affected, {len(acts)} action(s)"},
            "evidence": [{"doc_id": doc_id, "page": r["page"], "what": f"{r['subject']} {r['predicate']} = {r['value']}"}
                         for r in rows],
            "cypher": cypher}


def _ms(as_of: str) -> int:
    d = dt.datetime.fromisoformat(as_of)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


@tool("fact_as_of",
      """What the company believed about one fact at a point in time (transaction time): the claim that was
      active then, and the claims recorded later or set aside since. Params key (entity key), predicate
      (e.g. 'value'), as_of (ISO date or datetime).""",
      {"key": {"type": "string"}, "predicate": {"type": "string"}, "as_of": {"type": "string"}},
      ["key", "predicate", "as_of"])
def fact_as_of(g: Graph, key: str, predicate: str, as_of: str) -> dict:
    t = _ms(as_of)
    q = """MATCH (c:Claim {subject_key:$k, predicate:$p})
           OPTIONAL MATCH (c)-[:FROM]->(d:Document)
           OPTIONAL MATCH (dec:Decision)-[:SELECTS]->(c)
           RETURN c.id AS claim, c.value_str AS value, c.source_kind AS source_kind, c.authority AS authority,
                  d.doc_id AS doc, c.page AS page, coalesce(c.recorded_at, 0) AS recorded_at,
                  c.invalidated_at AS invalidated_at, c.status AS status_now, collect(dec.id) AS selected_by
           ORDER BY recorded_at"""
    rows = g.read(q, {"k": key, "p": predicate})
    if not rows:
        raise ValueError(f"no claims about {key}.{predicate}")
    known = [r for r in rows if r["recorded_at"] <= t]
    live = [r for r in known if r["invalidated_at"] is None or r["invalidated_at"] > t]
    # among claims alive at T, the one a decision selected wins; else the highest authority
    best = sorted(live, key=lambda r: (not r["selected_by"], -(r["authority"] or 0)))
    return {"result": {"as_of": as_of, "value": best[0]["value"] if best else None,
                       "claim": best[0] if best else None, "alive_then": live,
                       "learned_later": [r for r in rows if r["recorded_at"] > t],
                       "set_aside_since": [r for r in known if r["invalidated_at"] and r["invalidated_at"] > t]},
            "evidence": [{"doc_id": r["doc"], "page": r["page"], "what": f"{predicate} = {r['value']}"} for r in live],
            "cypher": [q]}
