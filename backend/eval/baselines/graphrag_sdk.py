"""Baseline: FalkorDB's own GraphRAG-SDK (v1.4), automatic schema, same model.

Runs in its own venv (it pulls torch/transformers):  backend/.venv-sdk

    # 1. build its graph from the same 678 PDF texts (LLM extraction on vLLM)
    VLLM_BASE_URL=http://localhost:8011/v1 .venv-sdk/bin/python -m eval.baselines.graphrag_sdk ingest
    # 2. answer a set
    VLLM_BASE_URL=... .venv-sdk/bin/python -m eval.baselines.graphrag_sdk run --set S21

Fairness: same LLM (Qwen3-32B, thinking off), same FalkorDB instance (graph
`grsdk_bidbrain`), same embedding model as our vector-RAG baseline
(bge-small-en-v1.5 via fastembed), same text (the layout-preserving text cache).
The SDK chooses its own ontology, chunking, extraction and retrieval — that is
what is being compared: generic auto-extracted GraphRAG vs. a typed domain graph
with curated tools.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from graphrag_sdk import ConnectionConfig, Embedder, GraphRAG, LiteLLM  # noqa: E402

GRAPH = "grsdk_bidbrain"
VLLM = os.getenv("VLLM_BASE_URL", "http://localhost:8011/v1")
TEXT_CACHE = ROOT / "vendor_bidintel" / "results" / "raw" / "text_cache.json"
DATA = ROOT.parent / "data" / "eval"
PROMPT = ("<context>\n{context}\n</context>\n\nQuestion: {question}\n\n"
          "Amounts are Indian rupees (1 Cr = 10,000,000). Answer from the context. "
          "End with a final line exactly: ANSWER: <number or short text>")


class FastEmbedder(Embedder):
    """bge-small-en-v1.5 — the same embedder as the vector-RAG baseline."""

    def __init__(self):
        from fastembed import TextEmbedding
        self._m = TextEmbedding("BAAI/bge-small-en-v1.5")
        super().__init__(model_name="bge-small-en-v1.5") if "model_name" in Embedder.__init__.__code__.co_varnames else None

    @property
    def model_name(self) -> str:  # abstract in the SDK
        return "bge-small-en-v1.5"

    def embed_query(self, text: str, **kwargs) -> list[float]:
        return [float(x) for x in next(iter(self._m.query_embed([text])))]

    def embed_documents(self, texts: list[str], **kwargs) -> list[list[float]]:
        return [[float(x) for x in v] for v in self._m.embed(texts, batch_size=64)]


def make() -> GraphRAG:
    llm = LiteLLM(model="openai/Qwen/Qwen3-32B", api_base=VLLM, api_key=os.getenv("VLLM_API_KEY", "EMPTY"),
                  temperature=0.0, max_tokens=4096,
                  extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    return GraphRAG(connection=ConnectionConfig(host=os.getenv("FALKOR_HOST", "localhost"), graph_name=GRAPH,
                                                query_timeout_ms=60000),
                    llm=llm, embedder=FastEmbedder(), embedding_dimension=384)


async def ingest(limit: int | None = None, concurrency: int = 16):
    corpus = json.loads(TEXT_CACHE.read_text())
    items = sorted(corpus.items())[: limit or None]
    t0 = time.time()
    async with make() as rag:
        sem = asyncio.Semaphore(concurrency)
        done = 0

        async def one(doc_id, text):
            nonlocal done
            async with sem:
                try:
                    await rag.ingest(doc_id, text=text.replace("\f", "\n\n"), document_id=doc_id)
                except Exception as exc:
                    print("  !!", doc_id, type(exc).__name__, str(exc)[:160], flush=True)
                done += 1
                if done % 25 == 0:
                    print(f"  {done}/{len(items)} docs, {time.time() - t0:.0f}s", flush=True)
        await asyncio.gather(*(one(d, t) for d, t in items))
        await rag.finalize()
        print("stats:", await rag.get_statistics() if hasattr(rag, "get_statistics") else "")
    print(f"ingested {len(items)} docs in {time.time() - t0:.0f}s")


_RAG = None


def ask_sdk(question: str) -> dict:
    """Synchronous wrapper used by eval.run (one shared instance)."""
    from bidbrain.agents.qa import parse_answer
    global _RAG
    t0 = time.perf_counter()
    loop = asyncio.new_event_loop()
    try:
        rag = make()
        res = loop.run_until_complete(rag.completion(question, prompt_template=PROMPT))
        loop.run_until_complete(rag.close()) if hasattr(rag, "close") else None
    finally:
        loop.close()
    val, raw = parse_answer(res.answer)
    return {"answer": val, "raw": raw, "usage": {"llm_calls": 2}, "ms": round((time.perf_counter() - t0) * 1000, 1)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ingest", "run"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--set", default="S21")
    a = ap.parse_args()
    if a.cmd == "ingest":
        asyncio.run(ingest(a.limit))
    else:
        from eval.run import run
        print(json.dumps(run("graphrag_sdk", a.set, workers=4, limit=a.limit), indent=1))
