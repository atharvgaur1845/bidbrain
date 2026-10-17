"""Generate docs/CYPHER.md from the queries the tools actually execute.

    cd backend && python ../scripts/gen_cypher_doc.py

Every tool is run once with representative arguments; the Cypher it reports
(`out["cypher"]`) is written verbatim, so the catalog cannot drift from the code.
"""
from __future__ import annotations

import json
import pathlib
import re

from bidbrain.graph.client import Graph
from bidbrain.ingest.load_graph import PROJECTIONS
from bidbrain.tools import TOOLS, call

ROOT = pathlib.Path(__file__).resolve().parents[1]

SAMPLES = {
    "resolve_entity": {"text": "phed odisha", "kind": "client"},
    "get_entity": {"key": "Ring Road — Uttar Pradesh Pkg-113", "kind": "work"},
    "find_portfolio": {"via_work": "Pkg-107", "exclude_categories": ["irrigation"]},
    "aggregate": {"work_keys": ["ringroaduttarpradeshpkg113"], "op": "sum"},
    "credential_timeline": {"person": "Sunita Joshi"},
    "date_span": {"work": "ROB — Jharkhand Pkg-29", "credential_kind": "pmp"},
    "receivables": {"client": "Arunodaya Infrastructure", "measure": "outstanding"},
    "collection_rate": {"work": "Ring Road — West Bengal Pkg-128"},
    "awarded_vs_billed": {"client": "Jal Nigam, Jharkhand"},
    "prove_absence": {"evidence": "reference_letter", "client": "Trishakti Power Generation Corporation"},
    "qualify_clause": {"kind": "similar_works", "params": {"categories": ["expressways"], "estimated_cost": 2500000000}},
    "key_people": {},
    "who_knows": {"client": "National Expressway Development Authority"},
    "explain_path": {"a": "Sunita Joshi", "a_kind": "person", "b": "Rahul Menon", "b_kind": "person"},
    "isolated_entities": {"scope": "corpus"},
    "communities": {},
    "trace_from_credential": {"person": "Sunita Joshi", "credential": "Six Sigma", "work": "Pkg-107"},
    "compliance_check": {"tender": "RFP-132000970"},
    "depends_on": {"doc_id": "DOC-CERT-001"},
    "fact_history": {"key": "Pkg-113", "kind": "work"},
    "vector_search": {"text": "client praised the safety record", "k": 3},
    "fulltext_search": {"text": "Anganwadi"},
    "graph_schema": {},
    "cypher_readonly": {"query": "MATCH (w:Work) RETURN count(w) AS works"},
    "precedents": {"category": "expressways"},
    "audit_bid": {},
    "blast_radius": {"label": "Work", "key": "ringroaduttarpradeshpkg113", "rerun": False},
    "case_get": None,
}

EXTRA = {
    "Conflict detection & resolution (bidbrain/conflicts/engine.py)": [
        ("Claims about the same fact", "MATCH (c:Claim)-[:ABOUT]->(s) WHERE c.status <> 'superseded' AND c.status <> 'rejected'\n"
         "  AND ($keys IS NULL OR c.subject_key IN $keys)\nRETURN c.id, c.subject_key, c.predicate, c.value, c.source_kind, c.authority, c.issuer, c.observed_day"),
        ("Record a contradiction", "MATCH (a:Claim {id:$a}), (b:Claim {id:$b}) WHERE NOT (a)-[:CONTRADICTS]-(b)\n"
         "CREATE (a)-[:CONTRADICTS {rule:'value_mismatch', detected_at:$t}]->(b)"),
        ("Write a Decision (actor label is Agent or User)", "MERGE (a:Agent {key:$actor}) ON CREATE SET a.name = $actor\n"
         "CREATE (d:Decision {id:$id, key:$id, conflict_id:$cid, subject_key:$sk, predicate:$pred,\n"
         "                    policy:$policy, rationale:$why, at:timestamp(), chosen_value:$val})\n"
         "CREATE (d)-[:DECIDED_BY]->(a)\nWITH d MATCH (w:Claim {id:$win}) CREATE (d)-[:SELECTS]->(w)\n"
         "WITH d UNWIND $ids AS cid MATCH (c:Claim {id:cid}) CREATE (d)-[:BASED_ON]->(c)"),
        ("A newer decision supersedes older ones on the same fact", "MATCH (old:Decision {subject_key:$sk, predicate:$pred}) WHERE old.id <> $d\n"
         "  AND NOT ()-[:SUPERSEDES]->(old)\nWITH old MATCH (d:Decision {id:$d}) CREATE (d)-[:SUPERSEDES]->(old)"),
        ("Learn a rule from a human decision (procedural memory)", "MERGE (r:PolicyRule {id:$id}) SET r.predicate = $p, r.prefer_kind = $pk, r.over_kind = $ok\n"
         "WITH r MATCH (d:Decision {id:$d}) MERGE (r)-[:LEARNED_FROM]->(d)"),
    ],
    "Tenancy & memory (bidbrain/api/sessions.py)": [
        ("Per-visitor graph", "GRAPH.COPY bidbrain_base session_<id>      -- Redis command, one isolated graph per visitor"),
        ("Conversation turn", "MERGE (c:Conversation {id:'main'}) WITH c OPTIONAL MATCH (c)-[:HAS_TURN]->(t:Turn)\n"
         "WITH c, count(t) AS n CREATE (c)-[:HAS_TURN]->(:Turn {i:n, role:$r, text:$t, at:timestamp()})"),
    ],
    "Analysis projections (bidbrain/ingest/load_graph.py)": [
        ("Person <-> client projection for centrality", "MATCH (p:Person)<-[:LED_BY]-(w:Work)-[:FOR_CLIENT]->(c:Client)\n"
         "WITH p, c, count(w) AS n, sum(w.value) AS v\nCREATE (p)-[:WORKED_FOR {works:n, value:v}]->(c), (c)-[:ENGAGED {works:n, value:v}]->(p)"),
        *[(f"{name} projection (communities: shared clients >= 1.5x the configuration-model expectation)",
           "\n".join(l.strip() for l in q.strip().splitlines())) for name, q in PROJECTIONS.items()],
        ("Workbook provenance (found by isolated_entities scope='corpus')",
         "UNWIND $rows AS r MATCH (n:Asset {key:r.key}), (d:Document {doc_id:r.doc})\n"
         "CREATE (n)-[:EVIDENCED_BY {kind:'asset_register'}]->(d)      -- also TBLine, BOQItem, Measurement"),
        ("When each compliance claim was made", "UNWIND $rows AS r MATCH (t:Tender {key:r.tender})\n"
         "SET t.bid_date = r.bid_date, t.bid_date_basis = r.basis, t.bid_value = r.bid_value, t.matrix_doc = r.doc"),
        ("Indexes", "CREATE INDEX FOR (n:Work) ON (n.key)            -- range index on every entity key\n"
         "CALL db.idx.fulltext.createNodeIndex('Chunk', 'ft')\n"
         "CREATE VECTOR INDEX FOR (c:Chunk) ON (c.embedding) OPTIONS {dimension:384, similarityFunction:'cosine'}"),
    ],
}


def norm(q: str) -> str:
    lines = [l.rstrip() for l in q.strip("\n").splitlines()]
    ind = min((len(l) - len(l.lstrip()) for l in lines[1:] if l.strip()), default=0)
    return "\n".join([lines[0].strip()] + [l[ind:] for l in lines[1:]])


def main():
    g = Graph()
    case_id = call(g.copy_to("doc_tmp"), "case_open", {"title": "doc"})  # ensure case tools work on a copy
    Graph("doc_tmp").delete()
    out = ["# Cypher & graph algorithms BidBrain depends on", "",
           "Generated by `scripts/gen_cypher_doc.py`: every tool was executed once against `bidbrain_base` and the",
           "queries it actually ran are listed verbatim. FalkorDB dialect notes: no `EXISTS {}` subqueries (pattern",
           "predicates instead), `shortestPath` only in WITH/RETURN, dates compared as `*_day` integers.", ""]
    algos = set()
    for name in sorted(TOOLS):
        t = TOOLS[name]
        args = SAMPLES.get(name)
        if args is None:
            continue
        res = call(g, name, args)
        qs = []
        for q in res.get("cypher", []):
            q = norm(q)
            if q not in qs:
                qs.append(q)
        algos |= set(re.findall(r"algo\.\w+|db\.idx\.\w+\.\w+", " ".join(qs)))
        out += [f"## `{name}`", "", " ".join(t.description.split()), "",
                f"Example call: `{json.dumps(args, ensure_ascii=False)}` — {res.get('ms', '?')} ms", ""]
        for q in qs:
            out += ["```cypher", q, "```", ""]
    for section, items in EXTRA.items():
        out += [f"## {section}", ""]
        for title, q in items:
            out += [f"**{title}**", "", "```cypher", q, "```", ""]
    out[5:5] = ["", "**Built-in procedures used:** " + ", ".join(f"`{a}`" for a in sorted(algos)) + ", `GRAPH.COPY`, `GRAPH.RO_QUERY`", ""]
    (ROOT / "docs" / "CYPHER.md").write_text("\n".join(out))
    print("wrote docs/CYPHER.md", len(out), "lines; procedures:", sorted(algos))


if __name__ == "__main__":
    main()
