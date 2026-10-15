"""Page chunks + embeddings, stored in FalkorDB itself.

FalkorDB is the vector store too: `(:Chunk {embedding: vecf32(...)})` with an
HNSW index. The same chunks serve three jobs — the vector-RAG *baseline*, the
agent's `vector_search` fallback tool, and the fulltext orphan check inside
`prove_absence`. Embeddings are cached to data/chunk_embeddings.npz so the HF
Space boots without re-embedding.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from bidbrain import config

MODEL = "BAAI/bge-small-en-v1.5"
DIM = 384
WINDOW, OVERLAP = 1400, 200


def make_chunks(pages) -> list[dict]:
    out = []
    for doc_id, texts in pages.raw.items():
        for pno, text in enumerate(texts.split("\f"), start=1):
            text = text.strip()
            if not text:
                continue
            i = 0
            for start in range(0, max(len(text), 1), WINDOW - OVERLAP):
                piece = text[start:start + WINDOW]
                if not piece.strip():
                    break
                out.append({"id": f"{doc_id}#p{pno}#{i}", "doc_id": doc_id, "page": pno, "text": piece})
                i += 1
                if start + WINDOW >= len(text):
                    break
    return out


@lru_cache(maxsize=1)
def embedder():
    import os
    from fastembed import TextEmbedding
    return TextEmbedding(MODEL, cache_dir=os.getenv("FASTEMBED_CACHE_PATH"))


def embed(texts: list[str]) -> np.ndarray:
    return np.asarray(list(embedder().embed(texts, batch_size=64)), dtype=np.float32)


def embed_query(text: str) -> list[float]:
    return [float(x) for x in next(iter(embedder().query_embed([text])))]


def _cached_embeddings(chunks: list[dict]) -> np.ndarray:
    ids = [c["id"] for c in chunks]
    if config.EMBED_CACHE.exists():
        z = np.load(config.EMBED_CACHE, allow_pickle=False)
        if list(z["ids"]) == ids:
            return z["vecs"]
    vecs = embed([c["text"] for c in chunks])
    config.EMBED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(config.EMBED_CACHE, ids=np.array(ids), vecs=vecs)
    return vecs


def load_chunks(g, pages) -> int:
    chunks = make_chunks(pages)
    vecs = _cached_embeddings(chunks)
    g.run(f"CREATE VECTOR INDEX FOR (c:Chunk) ON (c.embedding) "
          f"OPTIONS {{dimension:{DIM}, similarityFunction:'cosine'}}")
    # RediSearch does not split tokens on newlines ("Work\nAnganwadi" indexes as
    # ONE token), which silently lost ~60% of fulltext hits. Index a
    # whitespace-folded copy; keep `text` with its line breaks for display.
    rows = [{**c, "ft": " ".join(c["text"].split()), "emb": v.tolist()} for c, v in zip(chunks, vecs)]
    for i in range(0, len(rows), 200):
        g.run("""UNWIND $rows AS r MATCH (d:Document {doc_id:r.doc_id})
                 CREATE (c:Chunk {key:r.id, id:r.id, doc_id:r.doc_id, page:r.page, text:r.text, ft:r.ft,
                         embedding: vecf32(r.emb)})
                 CREATE (c)-[:PART_OF]->(d)""", {"rows": rows[i:i + 200]})
    g.run("CALL db.idx.fulltext.createNodeIndex('Chunk', 'ft')")
    return len(rows)
