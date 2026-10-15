"""One OpenAI-compatible chat client over a pool of (provider, model) endpoints.

Free tiers rate-limit per model (Groq: 8k tokens/minute each), so every
(provider, model) pair is its own quota bucket and the pool rotates through
them. A 429 parks that bucket until the reset time the server reports; if every
bucket is parked, the call waits for the earliest one (bounded) instead of
failing — a judge clicking twice must not see an error.

    LLM_PROVIDERS="groq:openai/gpt-oss-120b,groq:qwen/qwen3.8-27b,groq:openai/gpt-oss-20b,cerebras:gpt-oss-120b"
    LLM_PROVIDERS="vllm"                     # benchmark: one local model, no rate limits

* Cache: every completion is stored by a hash of (messages, tools). With
  BIDBRAIN_REPLAY=1 only the cache is used (offline demo of recorded runs).
* Reasoning is kept short: gpt-oss gets reasoning_effort=low, Qwen gets
  thinking disabled — tool routing does not need a long hidden chain of thought.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass

from openai import OpenAI

from bidbrain import config

ENDPOINTS = {
    "vllm": dict(base_url=os.getenv("VLLM_BASE_URL", "http://localhost:8011/v1"), key_env="VLLM_API_KEY",
                 default_model=os.getenv("VLLM_MODEL", "Qwen/Qwen3-32B")),
    "groq": dict(base_url="https://api.groq.com/openai/v1", key_env="GROQ_API_KEY",
                 default_model="openai/gpt-oss-120b"),
    "cerebras": dict(base_url="https://api.cerebras.ai/v1", key_env="CEREBRAS_API_KEY",
                     default_model="gpt-oss-120b"),
}
DEFAULT_POOL = "groq:openai/gpt-oss-120b,groq:qwen/qwen3.8-27b,groq:openai/gpt-oss-20b,cerebras:gpt-oss-120b"
REPLAY = os.getenv("BIDBRAIN_REPLAY", "0") == "1"
MAX_WAIT = float(os.getenv("LLM_MAX_WAIT", "45"))      # seconds a call may wait for a rate limit to reset
CACHE_PATH = config.DATA / "llm_cache.sqlite"


def _parse_pool(spec: str) -> list[tuple[str, str]]:
    out = []
    for item in [s.strip() for s in spec.split(",") if s.strip()]:
        prov, _, model = item.partition(":")
        if prov in ENDPOINTS:
            out.append((prov, model or ENDPOINTS[prov]["default_model"]))
    return out


POOL = _parse_pool(os.getenv("LLM_PROVIDERS", DEFAULT_POOL))
ORDER = [f"{p}:{m}" for p, m in POOL]          # kept for callers that print the pool


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class Reply:
    content: str
    tool_calls: list[dict]
    provider: str
    model: str
    usage: dict
    cached: bool
    ms: float


class _Cache:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS c (k TEXT PRIMARY KEY, v TEXT)")
        self.lock = threading.Lock()

    def get(self, k):
        with self.lock:
            r = self.db.execute("SELECT v FROM c WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None

    def put(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO c VALUES (?,?)", (k, json.dumps(v)))
            self.db.commit()


_cache = _Cache(CACHE_PATH)
_parked: dict[str, float] = {}         # bucket -> unix time it may be used again
_clients: dict[str, OpenAI] = {}
_lock = threading.Lock()


def _client(prov: str) -> OpenAI | None:
    ep = ENDPOINTS[prov]
    key = os.getenv(ep["key_env"]) or ("EMPTY" if prov == "vllm" else None)
    if not key:
        return None
    if prov not in _clients:
        _clients[prov] = OpenAI(api_key=key, base_url=ep["base_url"], timeout=120, max_retries=0)
    return _clients[prov]


def available() -> list[str]:
    return [f"{p}:{m}" for p, m in POOL if _client(p) is not None]


def _key(messages, tools, temperature) -> str:
    blob = json.dumps({"m": messages, "t": [t["function"]["name"] for t in (tools or [])], "temp": temperature},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _reset_seconds(exc) -> float:
    """How long the server asked us to back off (Retry-After / x-ratelimit-reset-*), else a default."""
    resp = getattr(exc, "response", None)
    h = getattr(resp, "headers", None) or {}
    for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        v = h.get(name)
        if v:
            m = re.match(r"(?:(\d+)m)?([\d.]+)s?$", str(v).strip())
            if m:
                return float(m.group(1) or 0) * 60 + float(m.group(2))
    m = re.search(r"try again in ([\d.]+)s", str(exc))
    return float(m.group(1)) if m else 20.0


def _shape(prov: str, model: str, messages: list[dict]) -> tuple[list[dict], dict]:
    """Per-family request shaping: keep hidden reasoning short."""
    msgs = [dict(m) for m in messages]
    extra: dict = {}
    if "gpt-oss" in model:
        extra["reasoning_effort"] = "low"
    elif "qwen" in model.lower():
        if prov == "vllm":
            extra["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        elif msgs and msgs[0]["role"] == "system" and "/no_think" not in msgs[0]["content"]:
            msgs[0]["content"] += "\n/no_think"
    return msgs, extra


def chat(messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.0,
         max_tokens: int = 1500, use_cache: bool = True) -> Reply:
    k = _key(messages, tools, temperature)
    if use_cache and (hit := _cache.get(k)):
        return Reply(**{**hit, "cached": True, "ms": 0.0})
    if REPLAY:
        raise LLMUnavailable("replay mode: no recorded completion for this request")
    errors: list[str] = []
    deadline = time.time() + MAX_WAIT
    while True:
        now = time.time()
        live = [(p, m) for p, m in POOL if _client(p) is not None]
        if not live:
            raise LLMUnavailable("no LLM configured: set GROQ_API_KEY / CEREBRAS_API_KEY or VLLM_BASE_URL")
        ready = [(p, m) for p, m in live if _parked.get(f"{p}:{m}", 0) <= now]
        if not ready:
            wake = min(_parked[f"{p}:{m}"] for p, m in live)
            if wake > deadline:
                raise LLMUnavailable("all LLM endpoints are rate-limited; " + "; ".join(errors[-3:]))
            time.sleep(max(0.2, wake - now))
            continue
        for prov, model in ready:
            bucket = f"{prov}:{model}"
            msgs, extra = _shape(prov, model, messages)
            kw = dict(model=model, messages=msgs, temperature=temperature, max_tokens=max_tokens, **extra)
            if tools:
                kw.update(tools=tools, tool_choice="auto")
            t0 = time.perf_counter()
            try:
                resp = _client(prov).chat.completions.create(**kw)
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                wait = _reset_seconds(exc) if status == 429 else (5.0 if status and status >= 500 else 60.0)
                if status == 404:      # model not offered on this account: park it for good
                    wait = 86400.0
                with _lock:
                    _parked[bucket] = time.time() + wait
                errors.append(f"{bucket}: {status or type(exc).__name__}")
                continue
            m = resp.choices[0].message
            content = m.content or ""
            if "</think>" in content:
                content = content.split("</think>", 1)[1]
            calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                     for tc in (m.tool_calls or [])]
            usage = {"prompt": getattr(resp.usage, "prompt_tokens", 0) or 0,
                     "completion": getattr(resp.usage, "completion_tokens", 0) or 0}
            rep = dict(content=content.strip(), tool_calls=calls, provider=bucket, model=model, usage=usage)
            if use_cache:
                _cache.put(k, rep)
            return Reply(**rep, cached=False, ms=round((time.perf_counter() - t0) * 1000, 1))
