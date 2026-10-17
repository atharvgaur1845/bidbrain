"""One OpenAI-compatible chat client over a pool of (provider, model) endpoints.

Free tiers rate-limit per model (Groq: 8k tokens/minute each), so every
(provider, model) pair is its own quota bucket and the pool rotates through
them. A 429 parks that bucket until the reset time the server reports; if every
bucket is parked, the call waits for the earliest one (bounded by LLM_MAX_WAIT,
which is enforced on every attempt) instead of failing — a judge clicking twice
must not see an error. Errors caused by the request itself (400/413/422: bad
generated tool call, context too long) never park a bucket: the call moves on
to the next bucket and, if every bucket rejects it, raises with the real reason.

    LLM_PROVIDERS="groq:openai/gpt-oss-120b,groq:qwen/qwen3.8-27b,groq:openai/gpt-oss-20b,cerebras:gpt-oss-120b"
    LLM_PROVIDERS="vllm"                     # benchmark: one local model, no rate limits

* Cache: every completion is stored by a hash of (bucket = provider:model, messages,
  full tool schemas, temperature, max_tokens), so a completion from one model is
  never replayed as another's. A lookup checks the key of every configured
  bucket. With BIDBRAIN_REPLAY=1 only the cache is used (offline demo of
  recorded runs); replay also accepts entries recorded under the older
  model-agnostic key. Entries written before this key change are otherwise
  unreachable (they are simply re-generated).
* Stickiness: `chat(..., prefer=bucket)` tries that bucket first (cache and live) while it is not
  parked, so one agent run keeps one model; it falls back to the rest of the pool only when the
  preferred bucket is rate-limited, unwell or rejects the request. `run_agent` passes the bucket
  that served its first step.
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
STICKY_WAIT = float(os.getenv("LLM_STICKY_WAIT", "20"))  # seconds worth waiting for the model that began the answer
MIN_PARK = 1.0          # a 429 with 'retry-after: 0' must not turn into a tight retry loop
SHORT_PARK = 5.0        # 5xx, timeouts, connection errors: the endpoint is briefly unwell
AUTH_PARK = 3600.0      # 401/403: the key is wrong or lacks access; retrying soon will not help
GONE_PARK = 86400.0     # 404: model not offered on this account
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
    """No completion could be obtained. `kind` says why: 'request' (every endpoint rejected this request,
    e.g. 400 invalid tool call / context too long), 'rate_limited', 'unavailable' (timeouts, 5xx, auth),
    'not_configured', 'replay_miss'."""

    def __init__(self, msg: str, kind: str = "unavailable"):
        super().__init__(msg)
        self.kind = kind


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
_park_kind: dict[str, str] = {}        # bucket -> why it was parked (for honest error messages)
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


def _key(bucket: str, messages, tools, temperature, max_tokens) -> str:
    blob = json.dumps({"b": bucket, "m": messages, "t": tools or [], "temp": temperature, "max": max_tokens},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _legacy_key(messages, tools, temperature) -> str:
    """The pre-Oct-16 model-agnostic key; consulted only in replay mode so recorded demo runs still play."""
    blob = json.dumps({"m": messages, "t": [t["function"]["name"] for t in (tools or [])], "temp": temperature},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


_DUR = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?")


def _duration(v: str) -> float | None:
    """'20', '7.66s', '1m2.5s', '350ms', '1h2m', '2m59.56s' -> seconds."""
    v = str(v).strip().lower()
    try:
        return float(v)
    except ValueError:
        pass
    m = _DUR.fullmatch(v)
    if not m or not any(m.groups()):
        return None
    h, mi, sec, ms = (float(x) if x else 0.0 for x in m.groups())
    return h * 3600 + mi * 60 + sec + ms / 1000


def _reset_seconds(exc) -> float:
    """How long the server asked us to back off (Retry-After / x-ratelimit-reset-*), else a default."""
    resp = getattr(exc, "response", None)
    h = getattr(resp, "headers", None) or {}
    for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        v = h.get(name)
        if v and (d := _duration(v)) is not None:
            return d
    m = re.search(r"try again in ([\d.hms]+)", str(exc))
    d = _duration(m.group(1).rstrip(".")) if m else None
    return d if d is not None else 20.0


def _classify(exc) -> tuple[str, float]:
    """(kind, park seconds) for a failed request. kind 'request' = this request is at fault: do not park."""
    import openai
    status = getattr(exc, "status_code", None)
    if status == 429:
        return "rate_limited", max(MIN_PARK, _reset_seconds(exc))
    if status in (401, 403):
        return "auth", AUTH_PARK
    if status == 404:
        return "gone", GONE_PARK
    if status in (408, 409) or isinstance(exc, openai.APITimeoutError):
        return "timeout", SHORT_PARK
    if status is not None and status >= 500 or isinstance(exc, openai.APIConnectionError):
        return "server", SHORT_PARK
    return "request", 0.0       # 400 / 413 / 422 / other 4xx / client-side failure


def _describe(bucket: str, exc) -> str:
    status = getattr(exc, "status_code", None)
    msg = getattr(exc, "message", None) or str(exc)
    return f"{bucket}: {('HTTP ' + str(status)) if status else type(exc).__name__} {msg}"[:300]


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


def _prefer_first(buckets: list[tuple[str, str]], prefer: str | None) -> list[tuple[str, str]]:
    """Pool order, with the preferred bucket (if any) moved to the front."""
    return sorted(buckets, key=lambda pm: f"{pm[0]}:{pm[1]}" != prefer) if prefer else list(buckets)


def _hit(messages, tools, temperature, max_tokens, prefer: str | None = None) -> dict | None:
    for prov, model in _prefer_first(POOL, prefer):
        if hit := _cache.get(_key(f"{prov}:{model}", messages, tools, temperature, max_tokens)):
            return hit
    if REPLAY:
        return _cache.get(_legacy_key(messages, tools, temperature))
    return None


def _fail(errors: list[tuple[str, str]], why: str) -> LLMUnavailable:
    kinds = {k for k, _ in errors}
    detail = "; ".join(e for _, e in errors[-4:])
    if kinds == {"rate_limited"}:
        return LLMUnavailable(f"all LLM endpoints are rate-limited ({why}); {detail}", "rate_limited")
    return LLMUnavailable(f"no LLM endpoint answered ({why}); {detail}" if detail else f"no LLM endpoint answered ({why})",
                          "rate_limited" if "rate_limited" in kinds else "unavailable")


def chat(messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.0,
         max_tokens: int = 1500, use_cache: bool = True, prefer: str | None = None) -> Reply:
    """One completion from the pool. `prefer` ('provider:model') is tried first while it is not parked."""
    if use_cache and (hit := _hit(messages, tools, temperature, max_tokens, prefer)):
        return Reply(**{**hit, "cached": True, "ms": 0.0})
    if REPLAY:
        raise LLMUnavailable("replay mode: no recorded completion for this request", "replay_miss")
    errors: list[tuple[str, str]] = []          # (kind, message) per failed attempt in this call
    rejected: dict[str, str] = {}               # bucket -> why it rejected *this* request (not parked)
    deadline = time.time() + MAX_WAIT
    while True:
        now = time.time()
        live = [(p, m) for p, m in POOL if _client(p) is not None]
        if not live:
            raise LLMUnavailable("no LLM configured: set GROQ_API_KEY / CEREBRAS_API_KEY or VLLM_BASE_URL",
                                 "not_configured")
        cands = [(p, m) for p, m in live if f"{p}:{m}" not in rejected]
        if not cands:
            raise LLMUnavailable("every LLM endpoint rejected this request: " + "; ".join(rejected.values()),
                                 "request")
        if now > deadline:
            raise _fail(errors, f"gave up after {MAX_WAIT:.0f}s")
        # one model per answer: a preferred bucket that is only briefly rate-limited is waited for, not swapped out
        if prefer and prefer not in rejected and any(f"{p}:{m}" == prefer for p, m in cands):
            w = _parked.get(prefer, 0) - now
            if 0 < w <= STICKY_WAIT and now + w < deadline:
                time.sleep(w)
                continue
        ready = _prefer_first([(p, m) for p, m in cands if _parked.get(f"{p}:{m}", 0) <= now], prefer)
        if not ready:
            wake = min(_parked[f"{p}:{m}"] for p, m in cands)
            if wake > deadline:
                why = f"next endpoint free in {wake - now:.0f}s, beyond the {MAX_WAIT:.0f}s wait limit"
                if not errors:      # parked by other calls: say what they saw
                    errors = [(_park_kind.get(f"{p}:{m}", "parked"),
                               f"{p}:{m} parked ({_park_kind.get(f'{p}:{m}', '?')}) for "
                               f"{_parked[f'{p}:{m}'] - now:.0f}s more") for p, m in cands]
                raise _fail(errors, why)
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
                kind, wait = _classify(exc)
                desc = _describe(bucket, exc)
                errors.append((kind, desc))
                if kind == "request":
                    rejected[bucket] = desc
                else:
                    with _lock:
                        _parked[bucket] = max(_parked.get(bucket, 0), time.time() + wait)
                        _park_kind[bucket] = kind
                if time.time() > deadline:
                    break
                continue
            choice = resp.choices[0]
            m = choice.message
            content = m.content or ""
            if "</think>" in content:
                content = content.split("</think>", 1)[1]
            calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                     for tc in (m.tool_calls or [])]
            usage = {"prompt": getattr(resp.usage, "prompt_tokens", 0) or 0,
                     "completion": getattr(resp.usage, "completion_tokens", 0) or 0}
            rep = dict(content=content.strip(), tool_calls=calls, provider=bucket, model=model, usage=usage)
            # a reply cut off before producing anything (e.g. reasoning used up max_tokens) is not worth keeping
            truncated_empty = getattr(choice, "finish_reason", None) == "length" and not rep["content"] and not calls
            if use_cache and not truncated_empty:
                _cache.put(_key(bucket, messages, tools, temperature, max_tokens), rep)
            return Reply(**rep, cached=False, ms=round((time.perf_counter() - t0) * 1000, 1))
