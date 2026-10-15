"""Search and escape hatches: vector / fulltext over page chunks, read-only Cypher."""
from __future__ import annotations

import re

from bidbrain.graph.client import Graph
from bidbrain.tools.registry import tool

SCHEMA_DOC = """
Node labels (key property `key` on every entity):
  Work{title,value:int rupees,completed:'YYYY-MM-DD',completed_day:int,completed_year,category,grading,role:'Prime'|'JV Partner',state,has_reference}
  Client{name,govt_level:central|state|psu|municipal|private,state,aliases}
  Person{name,designation,experience_years,qualification}
  Credential{kind:'PMP'|'Six Sigma Black Belt',issued,valid_through,valid_through_day}
  Document{doc_id,doc_type,pages}   Chunk{doc_id,page,text}
  Tender{ref,category}  Requirement{text,kind,status,threshold_value,threshold_unit}
  Bond{amount,issued,expiry,status}  Bank{name}  Certification{standard,body,valid_until}
  Contract{awarded_value,billed_value,period_from,period_to}  BOQItem  Measurement
  Invoice{invoice_date,year,invoiced,received,outstanding,status}
  FinancialStatement{fy,revenue_operations,pat,...}  FiscalYear{fy,start_year}  Asset{asset_type,cost,location}
  Claim{predicate,value,source_doc,page,source_kind,authority,status}  Decision  Case  Clause  Finding
Relationships:
  (Work)-[:FOR_CLIENT]->(Client)  (Work)-[:LED_BY]->(Person)  (Work)-[:IN_CATEGORY]->(Category)
  (Work)-[:HAS_REFERENCE {page}]->(Document)  (*)-[:EVIDENCED_BY {kind,page}]->(Document)
  (Person)-[:HOLDS]->(Credential)  (Tender)-[:REQUIRES]->(Requirement)  (Bond)-[:SECURES]->(Tender)
  (Invoice)-[:BILLED_TO]->(Client)  (Contract)-[:FOR_CLIENT]->(Client)  (FinancialStatement)-[:FOR_FY]->(FiscalYear)
  (Claim)-[:ABOUT]->(subject)  (Claim)-[:FROM]->(Document)  (Claim)-[:CONTRADICTS|SUPERSEDES]->(Claim)
Dialect notes (FalkorDB): no EXISTS{} subqueries (use pattern predicates); shortestPath only in
WITH/RETURN and directed; dates compare as strings 'YYYY-MM-DD' or via *_day ints.
"""


@tool("graph_schema", "The graph's labels, properties and relationships, with FalkorDB dialect notes.")
def graph_schema(g: Graph) -> dict:
    return {"result": SCHEMA_DOC.strip()}


@tool("vector_search",
      """Semantic search over document page chunks (FalkorDB HNSW vector index). Use for prose
      ('which letter praised safety?') or when an entity can't be resolved; NOT for counting or
      summing — use find_portfolio/aggregate for that. Optional doc_type filter.""",
      {"text": {"type": "string"}, "k": {"type": "integer"}, "doc_type": {"type": "string"}}, ["text"])
def vector_search(g: Graph, text: str, k: int = 6, doc_type: str | None = None) -> dict:
    from bidbrain.ingest.chunks import embed_query
    q = """CALL db.idx.vector.queryNodes('Chunk', 'embedding', $k, vecf32($v)) YIELD node, score
           MATCH (node)-[:PART_OF]->(d:Document) WHERE $dt IS NULL OR d.doc_type = $dt
           RETURN node.doc_id AS doc_id, node.page AS page, d.doc_type AS doc_type,
                  1 - score AS similarity, substring(node.text, 0, 600) AS text"""
    kk = max(int(k) * 15, 80) if doc_type else int(k)
    rows = g.read(q, {"k": kk, "v": embed_query(text), "dt": doc_type})[: int(k)]
    return {"result": rows,
            "evidence": [{"doc_id": r["doc_id"], "page": r["page"], "what": f"semantic match ({r['similarity']:.2f})"} for r in rows],
            "cypher": [q]}


@tool("fulltext_search",
      """Keyword search over document page text (FalkorDB fulltext index; prefix with * e.g.
      'Anganwadi*', fuzzy with %term%). Optional doc_type filter.""",
      {"text": {"type": "string"}, "doc_type": {"type": "string"}, "limit": {"type": "integer"}}, ["text"])
def fulltext_search(g: Graph, text: str, doc_type: str | None = None, limit: int = 10) -> dict:
    q = """CALL db.idx.fulltext.queryNodes('Chunk', $q) YIELD node, score
           MATCH (node)-[:PART_OF]->(d:Document) WHERE $dt IS NULL OR d.doc_type = $dt
           RETURN node.doc_id AS doc_id, node.page AS page, d.doc_type AS doc_type, score,
                  substring(node.text, 0, 400) AS text
           ORDER BY score DESC LIMIT $lim"""
    clean = re.sub(r"[^\w\s*%\"|-]", " ", text)
    rows = g.read(q, {"q": clean, "dt": doc_type, "lim": int(limit)})
    return {"result": rows,
            "evidence": [{"doc_id": r["doc_id"], "page": r["page"], "what": "keyword match"} for r in rows],
            "cypher": [q]}


@tool("cypher_readonly",
      """Run a read-only Cypher query (executed as GRAPH.RO_QUERY: the server rejects any write).
      Escape hatch for questions no other tool covers; call graph_schema first. Max 50 rows.""",
      {"query": {"type": "string"}, "params": {"type": "object"}}, ["query"])
def cypher_readonly(g: Graph, query: str, params: dict | None = None) -> dict:
    # RO_QUERY blocks writes but still lets LOAD CSV fetch https:// URLs, and this tool is public (API + MCP).
    # Keywords are checked with string literals and comments removed, so they cannot hide behind either.
    bare = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|/\*.*?\*/|//[^\n]*", " ", query, flags=re.S)
    if re.search(r"\b(LOAD|CSV)\b", bare, re.I):
        raise ValueError("LOAD CSV is not allowed in cypher_readonly")
    rows = Graph(g.name, timeout_ms=5_000).read(query, params or {})
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, dict):
                v.pop("embedding", None)
    return {"result": rows[:50], "truncated": len(rows) > 50, "cypher": [query]}
