"""Baselines, sharing BidBrain's model (Qwen3-32B) and its FalkorDB instance.

vector_rag    chunk -> embed -> top-k from FalkorDB's own vector index -> LLM answers.
              The conventional RAG pipeline; it reasons only over what it retrieved,
              so set-boundary and absence questions are its known weakness.
text2cypher   LLM writes one Cypher query from the schema, it runs read-only, LLM answers
              from the rows (one repair attempt on error). Isolates the value of
              BidBrain's curated multi-hop tools versus raw NL->Cypher.

Both are what the live UI's "Compare" toggle runs, so the demo shows the real
baseline, not a strawman.
"""
from __future__ import annotations

import json
import re
import time

from bidbrain.agents import providers
from bidbrain.agents.qa import parse_answer
from bidbrain.graph.client import Graph
from bidbrain.tools.search import SCHEMA_DOC

RAG_SYSTEM = """You answer questions about a construction company's records using ONLY the document
excerpts provided. Amounts are in Indian rupees (1 Cr = 10,000,000; 1 Lakh = 100,000). Show brief
working, then end with a final line exactly: ANSWER: <number or short text>"""

T2C_SYSTEM = f"""You write ONE FalkorDB Cypher query that answers the user's question over this graph.
{SCHEMA_DOC}
Rules: read-only; return the final number(s) directly when possible; use exact names from the question
(names match Client.name / Person.name / Work.title; use CONTAINS for partial names).
Reply with only the query inside ```cypher ... ```."""

T2C_ANSWER = """Given a question, the Cypher that was run and its rows, answer the question.
End with a final line exactly: ANSWER: <number or short text>"""


def vector_rag(g: Graph, question: str, k: int = 8) -> dict:
    from bidbrain.ingest.chunks import embed_query
    t0 = time.perf_counter()
    rows = g.read("""CALL db.idx.vector.queryNodes('Chunk', 'embedding', $k, vecf32($v)) YIELD node, score
                     RETURN node.doc_id AS doc_id, node.page AS page, node.text AS text, 1 - score AS sim""",
                  {"k": k, "v": embed_query(question)})
    ctx = "\n\n".join(f"[{r['doc_id']} p.{r['page']}]\n{r['text']}" for r in rows)
    rep = providers.chat([{"role": "system", "content": RAG_SYSTEM},
                          {"role": "user", "content": f"Excerpts:\n{ctx}\n\nQuestion: {question}"}], max_tokens=900)
    val, raw = parse_answer(rep.content)
    return {"system": "vector_rag", "answer": val, "answer_text": raw, "explanation": rep.content,
            "retrieved": [{"doc_id": r["doc_id"], "page": r["page"], "sim": round(r["sim"], 3)} for r in rows],
            "usage": {**rep.usage, "llm_calls": 1}, "ms": round((time.perf_counter() - t0) * 1000, 1),
            "provider": rep.provider}


def _extract_cypher(text: str) -> str:
    m = re.search(r"```(?:cypher)?\s*(.+?)```", text, re.S)
    return (m.group(1) if m else text).strip().rstrip(";")


def text2cypher(g: Graph, question: str) -> dict:
    t0 = time.perf_counter()
    msgs = [{"role": "system", "content": T2C_SYSTEM}, {"role": "user", "content": question}]
    rep = providers.chat(msgs, max_tokens=700)
    calls, usage = 1, dict(rep.usage)
    q = _extract_cypher(rep.content)
    rows, err = None, None
    for attempt in range(2):
        try:
            rows = g.read(q)[:60]
            err = None
            break
        except Exception as exc:
            err = str(exc)[:300]
            if attempt == 0:
                msgs += [{"role": "assistant", "content": rep.content},
                         {"role": "user", "content": f"That query failed: {err}\nFix it. Reply with only the query."}]
                rep = providers.chat(msgs, max_tokens=700)
                calls += 1
                q = _extract_cypher(rep.content)
    rep2 = providers.chat([{"role": "system", "content": T2C_ANSWER},
                           {"role": "user", "content": json.dumps({"question": question, "cypher": q,
                                                                   "rows": rows, "error": err}, default=str)[:9000]}],
                          max_tokens=500)
    calls += 1
    val, raw = parse_answer(rep2.content)
    return {"system": "text2cypher", "answer": val, "answer_text": raw, "cypher": q, "error": err,
            "rows": (rows or [])[:10], "explanation": rep2.content, "usage": {"llm_calls": calls},
            "ms": round((time.perf_counter() - t0) * 1000, 1)}
