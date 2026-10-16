"""Baseline: FalkorDB's own GraphRAG-SDK (v1.4), automatic schema, same model.

Runs in its own venv (it pulls torch/transformers):  backend/.venv-sdk

    # 1. build its graph from the same 678 PDF texts (LLM extraction on vLLM)
    VLLM_BASE_URL=http://localhost:8011/v1 .venv-sdk/bin/python -m eval.baselines.graphrag_sdk ingest
    # 2. answer a set (refuses unless step 1 finished with >= 90% of the documents; --allow-partial overrides)
    VLLM_BASE_URL=... .venv-sdk/bin/python -m eval.baselines.graphrag_sdk run --set S21

The ingest writes data/eval/results/graphrag_sdk_ingest.json (documents attempted / ingested / failed,
whether finalize() completed); eval.run checks it before scoring and the summary reports the coverage.

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
import contextvars
import json
import os
import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from graphrag_sdk import ConnectionConfig, Embedder, GraphRAG, LiteLLM  # noqa: E402

GRAPH = "grsdk_bidbrain"
VLLM = os.getenv("VLLM_BASE_URL", "http://localhost:8011/v1")
TEXT_CACHE = ROOT / "vendor_bidintel" / "results" / "raw" / "text_cache.json"
DATA = ROOT.parent / "data" / "eval"
MODEL = "Qwen/Qwen3-32B"


def _prompt() -> str:
    # the same answer-format rules BidBrain and the other baselines get (taken from QA_SYSTEM)
    from bidbrain.agents.baselines import ANSWER_FORMAT
    fmt = ANSWER_FORMAT.replace("{", "{{").replace("}", "}}")
    return ("<context>\n{context}\n</context>\n\nQuestion: {question}\n\n"
            "Amounts are Indian rupees (1 Cr = 10,000,000; 1 Lakh = 100,000). Answer from the context. "
            "End with a final line exactly:\n" + fmt)


PROMPT = _prompt()
_CALLS: contextvars.ContextVar[list | None] = contextvars.ContextVar("sdk_llm_calls", default=None)


def _bump() -> None:
    c = _CALLS.get()
    if c is not None:
        c[0] += 1


class CountingLiteLLM(LiteLLM):
    """LiteLLM that counts the SDK's real LLM calls per question (retries inside a call count once)."""

    def invoke(self, *a, **k):
        _bump()
        return super().invoke(*a, **k)

    async def ainvoke(self, *a, **k):
        _bump()
        return await super().ainvoke(*a, **k)

    async def ainvoke_messages(self, *a, **k):
        _bump()
        return await super().ainvoke_messages(*a, **k)


class FastEmbedder(Embedder):
    """bge-small-en-v1.5 — the same embedder as the vector-RAG baseline."""

    def __init__(self):
        from fastembed import TextEmbedding
        self._m = TextEmbedding("BAAI/bge-small-en-v1.5")

    @property
    def model_name(self) -> str:  # abstract in the SDK
        return "bge-small-en-v1.5"

    def embed_query(self, text: str, **kwargs) -> list[float]:
        return [float(x) for x in next(iter(self._m.query_embed([text])))]

    def embed_documents(self, texts: list[str], **kwargs) -> list[list[float]]:
        return [[float(x) for x in v] for v in self._m.embed(texts, batch_size=64)]


_EMBEDDER = None
_EMB_LOCK = threading.Lock()


def _embedder() -> FastEmbedder:
    """One ONNX model per process (onnxruntime inference is thread-safe), not one per question."""
    global _EMBEDDER
    with _EMB_LOCK:
        if _EMBEDDER is None:
            _EMBEDDER = FastEmbedder()
    return _EMBEDDER


def make() -> GraphRAG:
    llm = CountingLiteLLM(model=f"openai/{MODEL}", api_base=VLLM, api_key=os.getenv("VLLM_API_KEY", "EMPTY"),
                          temperature=0.0, max_tokens=4096,
                          extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    return GraphRAG(connection=ConnectionConfig(host=os.getenv("FALKOR_HOST", "localhost"), graph_name=GRAPH,
                                                query_timeout_ms=60000),
                    llm=llm, embedder=_embedder(), embedding_dimension=384)


def _marker() -> pathlib.Path:
    from eval.run import SDK_INGEST
    SDK_INGEST.parent.mkdir(parents=True, exist_ok=True)
    return SDK_INGEST


def _write_marker(rec: dict) -> None:
    p = _marker()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1))
    tmp.replace(p)


async def ingest(limit: int | None = None, concurrency: int = 16):
    corpus = json.loads(TEXT_CACHE.read_text())
    items = sorted(corpus.items())[: limit or None]
    t0 = time.time()
    rec = {"graph": GRAPH, "n_docs": len(corpus), "attempted": len(items), "ok": 0, "failed_n": 0, "failed": [],
           "coverage": 0.0, "finalized": False, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "finished_at": None}
    _write_marker(rec)          # an interrupted ingest leaves finalized=false, so eval.run refuses to score it
    ok: list[str] = []
    failed: list[dict] = []
    async with make() as rag:
        sem = asyncio.Semaphore(concurrency)
        done = 0

        async def one(doc_id, text):
            nonlocal done
            async with sem:
                try:
                    await rag.ingest(text=text.replace("\f", "\n\n"), document_id=doc_id)
                    ok.append(doc_id)
                except Exception as exc:
                    failed.append({"doc_id": doc_id, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
                    print("  !!", doc_id, type(exc).__name__, str(exc)[:160], flush=True)
                done += 1
                if done % 25 == 0:
                    print(f"  {done}/{len(items)} docs ({len(failed)} failed), {time.time() - t0:.0f}s", flush=True)
        await asyncio.gather(*(one(d, t) for d, t in items))
        rec.update(ok=len(ok), failed_n=len(failed), failed=failed, coverage=round(len(ok) / max(1, len(corpus)), 4))
        _write_marker(rec)
        await rag.finalize()
        rec.update(finalized=True, finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        _write_marker(rec)
        print("stats:", await rag.get_statistics() if hasattr(rag, "get_statistics") else "")
    print(f"ingested {len(ok)}/{len(corpus)} docs ({len(failed)} failed) in {time.time() - t0:.0f}s; "
          f"record: {_marker()}")


_LOCAL = threading.local()


def _worker():
    """One event loop + GraphRAG per worker thread, reused across that thread's questions (the SDK's
    async clients are bound to the loop that created them); the embedder is shared process-wide."""
    if getattr(_LOCAL, "rag", None) is None:
        _LOCAL.loop = asyncio.new_event_loop()
        _LOCAL.rag = make()
    return _LOCAL.loop, _LOCAL.rag


def ask_sdk(question: str) -> dict:
    """Synchronous wrapper used by eval.run."""
    from bidbrain.agents.qa import parse_answer
    t0 = time.perf_counter()
    loop, rag = _worker()
    calls = [0]
    tok = _CALLS.set(calls)
    try:
        res = loop.run_until_complete(rag.completion(question, prompt_template=PROMPT))
    except Exception:
        _LOCAL.rag = None          # rebuild this worker's client on the next question
        raise
    finally:
        _CALLS.reset(tok)
    val, raw = parse_answer(res.answer)
    return {"answer": val, "raw": raw, "usage": {"llm_calls": calls[0]}, "providers": [f"vllm:{MODEL}"],
            "ms": round((time.perf_counter() - t0) * 1000, 1)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ingest", "run"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--set", default="S21")
    # same default concurrency as the other systems in scripts/bench_when_ready.sh (WORKERS=16)
    ap.add_argument("--workers", type=int, default=int(os.getenv("WORKERS", "16")))
    ap.add_argument("--allow-partial", action="store_true", help="score even if the ingest covered < 90%% of docs")
    a = ap.parse_args()
    if a.cmd == "ingest":
        asyncio.run(ingest(a.limit))
    else:
        from eval.run import run
        print(json.dumps(run("graphrag_sdk", a.set, workers=a.workers, limit=a.limit,
                             allow_partial=a.allow_partial), indent=1))
