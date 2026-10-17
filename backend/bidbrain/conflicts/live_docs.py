"""What a live document adds to the graph beyond its claims.

A reference letter that arrives live is evidence the same way a corpus letter is, so it gets the
same structure the corpus build gives one (ingest/load_graph.py):

    (Work)-[:HAS_REFERENCE {page}]->(Document {doc_type:'reference_letter'})   and  w.has_reference = true

which is what `prove_absence(evidence='reference_letter')` reads: the HAS_REFERENCE edge decides
present / absent, and the corpus-closure check counts reference_letter Documents with no such edge.
`w.views` is NOT touched: it lists the independent sources that attest the work *exists* (completion
certificate, company record, portfolio), and the proof requires exactly those three; a letter is not one.

Every live document also gets page Chunks (text + fulltext copy + embedding, windowed exactly like the
corpus by ingest/chunks.make_chunks), so fulltext and vector search find it.
"""
from __future__ import annotations

import types

from bidbrain.graph.client import Graph

REFERENCE_DOC_TYPES = {"reference_letter"}


def link_reference(g: Graph, doc_id: str, doc_type: str | None, claims: list[dict]) -> list[str]:
    """If the document is a reference letter, link every Work it makes a claim about. Returns the works."""
    if doc_type not in REFERENCE_DOC_TYPES:
        return []
    works = sorted({(c["subject_key"], int(c.get("page") or 1)) for c in claims if c.get("subject_label") == "Work"})
    linked = []
    for key, page in works:
        rows = g.run("""MATCH (w:Work {key:$k}), (d:Document {doc_id:$d})
                        MERGE (w)-[h:HAS_REFERENCE]->(d) ON CREATE SET h.page = $page, h.live = true
                        SET w.has_reference = true
                        RETURN w.key AS key""", {"k": key, "d": doc_id, "page": page})
        linked += [r["key"] for r in rows]
    return linked


def add_chunks(g: Graph, doc_id: str, text: str) -> int:
    """Chunk the document's text (pages split on form feeds) and store it like the corpus chunks."""
    from bidbrain.ingest.chunks import _ft, embed, make_chunks
    if not (text or "").strip():
        return 0
    chunks = make_chunks(types.SimpleNamespace(raw={doc_id: text}))
    if not chunks:
        return 0
    vecs = embed([c["text"] for c in chunks])
    rows = [{**c, "ft": _ft(c["text"]), "emb": v.tolist()} for c, v in zip(chunks, vecs)]
    g.run("""UNWIND $rows AS r MATCH (d:Document {doc_id:r.doc_id})
             MERGE (c:Chunk {id:r.id})
             SET c.key = r.id, c.doc_id = r.doc_id, c.page = r.page, c.text = r.text, c.ft = r.ft,
                 c.embedding = vecf32(r.emb)
             MERGE (c)-[:PART_OF]->(d)""", {"rows": rows})
    return len(rows)


def event_text(ev: dict, pdf_path=None) -> str:
    """The text of a fired event's document: its rendered PDF when there is one, else the event's own
    words (title, issuer, date and narrative), which is what the document says."""
    if pdf_path is not None and pdf_path.is_file():
        try:
            from bidbrain.conflicts import ingest_doc  # noqa: F401  (puts the vendored extractor on sys.path)
            import pdftext
            text = pdftext.extract_pdf(pdf_path)
            if text.strip():
                return text
        except Exception:
            pass
    d = ev.get("doc", {})
    return "\n".join(x for x in (d.get("title"), d.get("issuer"), d.get("issued"), ev.get("narrative")) if x)
