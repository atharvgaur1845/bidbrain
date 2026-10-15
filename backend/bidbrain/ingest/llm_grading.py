"""LLM-assisted extraction, kept honest by the claim layer.

    LLM_PROVIDERS=vllm python -m bidbrain.ingest.llm_grading     -> data/llm_gradings.json

31 works have no grading the deterministic extractors could read. Qwen3 reads
each work's certificate, company record and reference letters and either quotes
the assessment or says NONE. Whatever it finds enters the graph as a
`llm_prose` claim (authority 30): visible, cited to the page it came from, and
automatically outranked by any document-backed source. The loader reads the
JSON, so booting never calls an LLM.
"""
from __future__ import annotations

import json
import re

from bidbrain import config
from bidbrain.agents import providers
from bidbrain.graph.client import Graph
from bidbrain.ingest.pages import page_index

GRADES = ["Outstanding", "Excellent", "Very Good", "Good", "Satisfactory", "Fair", "Poor"]
OUT = config.DATA / "llm_gradings.json"
PROMPT = """You read construction completion documents. Find the CLIENT's overall assessment of the work's
quality, if one is stated (in words such as "graded", "assessed as", "rated", "performance was").
Reply ONLY with JSON: {"grade": one of %s or "NONE", "quote": "<the exact sentence>", "doc": "<doc id>", "page": <int>}
Do not infer a grade from tone; if no grade is stated, use "NONE".""" % GRADES


def run(limit: int | None = None) -> list[dict]:
    g = Graph()
    pi = page_index()
    rows = g.read("""MATCH (w:Work) WHERE w.grading IS NULL
                     OPTIONAL MATCH (w)-[e:EVIDENCED_BY]->(d:Document) WHERE e.kind IN ['completion_certificate','company_record']
                     OPTIONAL MATCH (w)-[:HAS_REFERENCE]->(r:Document)
                     RETURN w.key AS key, w.title AS title, collect(DISTINCT d.doc_id) + collect(DISTINCT r.doc_id) AS docs""")
    out = []
    for r in rows[: limit or None]:
        parts = []
        for d in r["docs"]:
            for i, page in enumerate(pi.raw.get(d, "").split("\f"), start=1):
                parts.append(f"[{d} page {i}]\n{page[:2500]}")
        rep = providers.chat([{"role": "system", "content": PROMPT},
                              {"role": "user", "content": f"Work: {r['title']}\n\n" + "\n\n".join(parts)[:12000]}],
                             max_tokens=300)
        m = re.search(r"\{.*\}", rep.content, re.S)
        try:
            ans = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            ans = {}
        grade = ans.get("grade") if ans.get("grade") in GRADES else None
        # keep only answers whose quote really occurs in the cited document (no invented evidence)
        quote = (ans.get("quote") or "").strip()
        doc_text = " ".join(pi.raw.get(ans.get("doc") or "", "").split())
        verified = bool(grade and quote and " ".join(quote.split())[:60] in doc_text)
        out.append({"work_key": r["key"], "title": r["title"], "grade": grade if verified else None,
                    "raw_grade": ans.get("grade"), "quote": quote, "doc": ans.get("doc"), "page": ans.get("page"),
                    "verified_quote": verified, "provider": rep.provider})
        print(f"{r['title'][:45]:45s} -> {ans.get('grade')!s:12s} verified={verified}")
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return out


if __name__ == "__main__":
    res = run()
    print(f"{sum(1 for r in res if r['grade'])} of {len(res)} gradings recovered with a verified quote")
