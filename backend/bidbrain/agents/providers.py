"""One OpenAI-compatible chat client for vLLM (Sharanga), Groq and Cerebras.

All three serve Qwen3-32B, so the benchmark (run on vLLM) and the live demo
(Groq, failing over to Cerebras) exercise the same model.

* Failover: a provider that rate-limits (429) or errors is skipped for a
  cooldown and the next one in `LLM_PROVIDERS` is tried.
* Cache: every completion is stored by a hash of (model family, messages,
  tools). With BIDBRAIN_REPLAY=1 only the cache is used — the canned demo
  tenders replay a recorded run instantly and cost nothing.
* Qwen3 "thinking" is switched off (`/no_think`): tool routing does not need a
  hidden chain of thought, and it burns free-tier tokens.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

from openai import OpenAI

from bidbrain import config

PROVIDERS = {
    "vllm": dict(base_url=os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1"),
                 key_env="VLLM_API_KEY", model=os.getenv("VLLM_MODEL", "Qwen/Qwen3-32B")),
    "groq": dict(base_url="https://api.groq.com/openai/v1", key_env="GROQ_API_KEY",
                 model=os.getenv("GROQ_MODEL", "qwen/qwen3-32b")),
    "cerebras": dict(base_url="https://api.cerebras.ai/v1", key_env="CEREBRAS_API_KEY",
                     model=os.getenv("CEREBRAS_MODEL", "qwen-3-32b")),
}
ORDER = [p.strip() for p in os.getenv("LLM_PROVIDERS", "groq,cerebras").split(",") if p.strip()]
REPLAY = os.getenv("BIDBRAIN_REPLAY", "0") == "1"
CACHE_PATH = config.DATA / "llm_cache.sqlite"


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
_cooldown: dict[str, float] = {}
_clients: dict[str, OpenAI] = {}


def _client(name: str) -> OpenAI | None:
    p = PROVIDERS[name]
    key = os.getenv(p["key_env"]) or ("EMPTY" if name == "vllm" else None)
    if not key:
        return None
    if name not in _clients:
        _clients[name] = OpenAI(api_key=key, base_url=p["base_url"], timeout=90, max_retries=0)
    return _clients[name]


def available() -> list[str]:
    return [n for n in ORDER if _client(n) is not None]


def _key(messages, tools, temperature) -> str:
    blob = json.dumps({"m": messages, "t": [t["function"]["name"] for t in (tools or [])],
                       "temp": temperature, "family": "qwen3-32b"}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _no_think(messages: list[dict]) -> list[dict]:
    out = [dict(m) for m in messages]
    if out and out[0]["role"] == "system" and "/no_think" not in out[0]["content"]:
        out[0]["content"] = out[0]["content"] + "\n/no_think"
    return out


def chat(messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.0,
         max_tokens: int = 1500, use_cache: bool = True) -> Reply:
    k = _key(messages, tools, temperature)
    if use_cache and (hit := _cache.get(k)):
        return Reply(**{**hit, "cached": True, "ms": 0.0})
    if REPLAY:
        raise LLMUnavailable("replay mode: no cached completion for this request")
    errors = []
    for name in ORDER:
        if _cooldown.get(name, 0) > time.time():
            continue
        cli = _client(name)
        if cli is None:
            continue
        p = PROVIDERS[name]
        t0 = time.perf_counter()
        try:
            kw = dict(model=p["model"], messages=_no_think(messages), temperature=temperature,
                      max_tokens=max_tokens)
            if tools:
                kw.update(tools=tools, tool_choice="auto")
            if name == "vllm":
                kw["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            resp = cli.chat.completions.create(**kw)
        except Exception as exc:  # 429 / 5xx / network: cool down this provider, try the next
            msg = f"{name}: {type(exc).__name__}: {str(exc)[:200]}"
            errors.append(msg)
            status = getattr(exc, "status_code", None)
            _cooldown[name] = time.time() + (30 if status == 429 else 10)
            continue
        m = resp.choices[0].message
        content = m.content or ""
        if "</think>" in content:  # some servers still emit an (empty) think block
            content = content.split("</think>", 1)[1]
        calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                 for tc in (m.tool_calls or [])]
        usage = {"prompt": getattr(resp.usage, "prompt_tokens", 0), "completion": getattr(resp.usage, "completion_tokens", 0)}
        rep = dict(content=content.strip(), tool_calls=calls, provider=name, model=p["model"], usage=usage)
        if use_cache:
            _cache.put(k, rep)
        return Reply(**rep, cached=False, ms=round((time.perf_counter() - t0) * 1000, 1))
    raise LLMUnavailable("no LLM provider available: " + ("; ".join(errors) or
                         "set GROQ_API_KEY / CEREBRAS_API_KEY or VLLM_BASE_URL"))
