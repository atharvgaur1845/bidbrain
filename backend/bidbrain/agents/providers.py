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
* Pinning: `chat(..., prefer=bucket)` is a hard pin, so one agent run keeps one model. Only that
  bucket's cache entries count, and a parked pinned bucket is waited for up to the whole LLM_MAX_WAIT
  (Groq's per-minute token windows often reset 30-60 s out, so a shorter wait still mixed models).
  Another bucket serves only when the pinned one rejects the request, is not configured, or stays
  parked beyond the wait; the reply then says why in `Reply.switched`. `run_agent` pins the bucket
  that served its first step and moves the pin (once, recorded) only on such a switch.
  Why a pin used to break (round 3, qwen -> gpt-oss-20b mid-answer): Groq's free tier gives each model
  8,000 tokens a minute and a Q&A step sends 4-6k, so the second step's 429 asked for 45-60 s, just past
  the old 45 s wait; the default wait now covers a whole per-minute window (62 s). And a Groq 400
  `tool_use_failed` (the model wrote a tool call that failed the server's schema check) is the model's
  own reply, not a rejected request: it comes back as `Reply(malformed=True)` holding the failed
  generation, from the same bucket, so the loop recovers the call or re-asks that model instead of
  handing the answer to another one. An unpinned lookup (the first step) skips a cached reply from a
  bucket that cannot serve the next step (not configured, or parked beyond the wait).
* Errors that leave this module are redacted (`redact`): no organization / request ids, keys or URLs
  from a provider's error body reach a client, only the kind and a short message.
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
from pathlib import Path
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
# seconds a call may wait for a rate limit to reset: a whole per-minute token window (Groq: 8k TPM per model; a
# 4-6k token step after another one is told to retry in 45-60 s) so a pinned model is waited for, not swapped
MAX_WAIT = float(os.getenv("LLM_MAX_WAIT", "62"))
MIN_PARK = 1.0          # a 429 with 'retry-after: 0' must not turn into a tight retry loop
SHORT_PARK = 5.0        # 5xx, timeouts, connection errors: the endpoint is briefly unwell
AUTH_PARK = 3600.0      # 401/403: the key is wrong or lacks access; retrying soon will not help
GONE_PARK = 86400.0     # 404: model not offered on this account
CACHE_PATH = Path(os.getenv("LLM_CACHE_PATH") or (config.DATA / "llm_cache.sqlite"))   # a fresh path = a real re-run


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
    e.g. 400 invalid tool call / context too long), 'too_large' (HTTP 413: shrink and retry), 'rate_limited', 'unavailable' (timeouts, 5xx, auth),
    'not_configured', 'replay_miss'."""

    def __init__(self, msg: str, kind: str = "unavailable"):
        super().__init__(redact(msg))
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
    switched: str | None = None     # set when `prefer` was given and another bucket served: why
    malformed: bool = False         # the server refused the model's own tool call; `content` is what it wrote


# ── redaction ────────────────────────────────────────────────────────────────
# Provider error bodies carry account details (Groq: "in organization `org_01...`", billing URLs, request
# ids). Nothing of that may reach the browser, SSE or an MCP client.
_SECRETS = [
    (re.compile(r"\b(?:org|req|request|proj|user|sess)_[A-Za-z0-9]{6,}\b"), "[id]"),
    (re.compile(r"\b(?:gsk|csk|sk|pk|key|xai)[-_][A-Za-z0-9_\-]{10,}\b"), "[key]"),
    (re.compile(r"\bBearer\s+\S+", re.I), "Bearer [key]"),
    (re.compile(r"https?://\S+"), "[url]"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "[id]"),
    (re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|token|request[_ -]?id|x-request-id|org(?:anization)?[_ -]?id)"
                r"\s*[:=]\s*[`'\"]?[^\s`'\",;]+[`'\"]?", re.I), "[redacted]"),
    (re.compile(r"\b(organization|org|project|account)\s+[`'\"][^`'\"]+[`'\"]", re.I), r"\1 [id]"),
]


def redact(text) -> str:
    """Provider error text with ids, keys and URLs removed."""
    out = str(text or "")
    for rx, sub in _SECRETS:
        out = rx.sub(sub, out)
    return out


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


def _describe(bucket: str, exc, kind: str | None = None) -> str:
    """'bucket: kind (HTTP n): short message', redacted. A rate limit is reported only as when it resets."""
    status = getattr(exc, "status_code", None)
    kind = kind or _classify(exc)[0]
    if kind == "rate_limited":
        short = f"retry in {_reset_seconds(exc):.0f}s"
    else:
        body = getattr(exc, "body", None)
        msg = None
        if isinstance(body, dict):
            err = body.get("error") if isinstance(body.get("error"), dict) else body
            msg = err.get("message") if isinstance(err, dict) else None
        msg = redact(msg or getattr(exc, "message", None) or str(exc))
        msg = re.sub(r"^Error code: \d+ - ", "", msg)
        short = re.split(r"(?<=[.!?])\s", msg.strip(), maxsplit=1)[0][:140]
    return f"{bucket}: {kind}{f' (HTTP {status})' if status else ''}: {short}"


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


def _serviceable(bucket: str, now: float | None = None) -> bool:
    """Whether `bucket` could serve a live call within the wait: configured and not parked beyond MAX_WAIT."""
    prov = bucket.split(":", 1)[0]
    if prov not in ENDPOINTS or _client(prov) is None:
        return False
    return _parked.get(bucket, 0) <= (now or time.time()) + MAX_WAIT


def _hit(messages, tools, temperature, max_tokens, prefer: str | None = None) -> dict | None:
    """A cached completion. With `prefer`, only that bucket's entry counts (one model per answer); the
    rest of the pool is consulted only in replay mode, where nothing can go live instead. Without `prefer`
    (a run's first step, which picks the pin), a bucket that could not serve the *next* step live is skipped:
    pinning a run to it would force a switch one step later."""
    buckets = [f"{p}:{m}" for p, m in _prefer_first(POOL, prefer)]
    if prefer and not REPLAY:
        buckets = [b for b in buckets if b == prefer]
    elif not REPLAY:
        now = time.time()
        buckets = [b for b in buckets if _serviceable(b, now)]
    for b in buckets:
        if hit := _cache.get(_key(b, messages, tools, temperature, max_tokens)):
            return hit
    if REPLAY:
        return _cache.get(_legacy_key(messages, tools, temperature))
    return None


def _failed_generation(exc) -> str | None:
    """The model's own text when the server refused its tool call (Groq 400 `tool_use_failed`, with the text
    in `failed_generation`), else None. That is a reply from this model, not a request every model rejects."""
    if getattr(exc, "status_code", None) != 400:
        return None
    body = getattr(exc, "body", None)
    err = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else body
    if not isinstance(err, dict) or err.get("code") != "tool_use_failed":
        return None
    text = err.get("failed_generation")
    return text if isinstance(text, str) and text.strip() else None


def _fail(errors: list[tuple[str, str]], why: str) -> LLMUnavailable:
    kinds = {k for k, _ in errors}
    detail = "; ".join(e for _, e in errors[-4:])
    if kinds == {"rate_limited"}:
        return LLMUnavailable(f"all LLM endpoints are rate-limited ({why}); {detail}", "rate_limited")
    return LLMUnavailable(f"no LLM endpoint answered ({why}); {detail}" if detail else f"no LLM endpoint answered ({why})",
                          "rate_limited" if "rate_limited" in kinds else "unavailable")


def chat(messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.0,
         max_tokens: int = 1500, use_cache: bool = True, prefer: str | None = None,
         max_wait: float | None = None) -> Reply:
    """One completion from the pool.

    `prefer` ('provider:model') is a hard pin: only its cache entries count, and while it is parked the call
    waits for it (up to `max_wait`, default LLM_MAX_WAIT). Another bucket serves only when the pinned one
    rejects this request, is not configured, or stays parked beyond the wait; `Reply.switched` says why."""
    if use_cache and (hit := _hit(messages, tools, temperature, max_tokens, prefer)):
        return Reply(**{**hit, "cached": True, "ms": 0.0})
    if REPLAY:
        raise LLMUnavailable("replay mode: no recorded completion for this request", "replay_miss")
    budget = MAX_WAIT if max_wait is None else max_wait
    errors: list[tuple[str, str]] = []          # (kind, message) per failed attempt in this call
    rejected: dict[str, str] = {}               # bucket -> why it rejected *this* request (not parked)
    deadline = time.time() + budget
    switched: str | None = None                 # why the pinned bucket is not the one serving
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
            raise _fail(errors, f"gave up after {budget:.0f}s")
        names = [f"{p}:{m}" for p, m in cands]
        pinned = prefer if prefer in names else None
        if prefer and not pinned and not switched:
            switched = (f"{prefer} rejected this request ({rejected[prefer].split(': ', 1)[-1]})"
                        if prefer in rejected else f"{prefer} is not configured on this server")
        if pinned:
            wake = _parked.get(pinned, 0)
            if wake <= now:
                ready = [(p, m) for p, m in cands if f"{p}:{m}" == pinned]
            elif wake <= deadline:                    # one model per answer: wait for it, the whole budget
                time.sleep(max(0.2, wake - now))
                continue
            else:                                     # parked beyond the wait: hand over, and say why
                switched = (f"{pinned} {_park_kind.get(pinned, 'parked')} for {wake - now:.0f}s more, "
                            f"beyond the {budget:.0f}s wait")
                pinned = None
        if not pinned:
            ready = [(p, m) for p, m in cands if _parked.get(f"{p}:{m}", 0) <= now and f"{p}:{m}" != prefer]
            if not ready:
                others = [(p, m) for p, m in cands if f"{p}:{m}" != prefer] or cands
                wake = min(_parked.get(f"{p}:{m}", 0) for p, m in others)
                if wake > deadline:
                    why = f"next endpoint free in {wake - now:.0f}s, beyond the {budget:.0f}s wait limit"
                    if not errors:      # parked by other calls: say what they saw
                        errors = [(_park_kind.get(f"{p}:{m}", "parked"),
                                   f"{p}:{m} parked ({_park_kind.get(f'{p}:{m}', '?')}) for "
                                   f"{_parked.get(f'{p}:{m}', 0) - now:.0f}s more") for p, m in cands]
                    raise _fail(errors, why)
                time.sleep(max(0.2, wake - now))
                continue
        for prov, model in ready:
            bucket = f"{prov}:{model}"
            if prefer and bucket != prefer and use_cache:
                key = _key(bucket, messages, tools, temperature, max_tokens)
                if hit := _cache.get(key):          # the bucket taking over may already have this one
                    return Reply(**{**hit, "cached": True, "ms": 0.0, "switched": switched})
            msgs, extra = _shape(prov, model, messages)
            kw = dict(model=model, messages=msgs, temperature=temperature, max_tokens=max_tokens, **extra)
            if tools:
                kw.update(tools=tools, tool_choice="auto")
            t0 = time.perf_counter()
            try:
                resp = _client(prov).chat.completions.create(**kw)
            except Exception as exc:
                kind, wait = _classify(exc)
                if kind == "request" and tools and (text := _failed_generation(exc)):
                    # the model's malformed tool call: hand it back (never cached) so the caller re-asks the
                    # same model, instead of treating it as a request no model could serve
                    return Reply(content=redact(text).strip(), tool_calls=[], provider=bucket, model=model,
                                 usage={"prompt": 0, "completion": 0}, cached=False,
                                 ms=round((time.perf_counter() - t0) * 1000, 1),
                                 switched=switched if bucket != prefer else None, malformed=True)
                desc = _describe(bucket, exc, kind)
                if getattr(exc, "status_code", None) == 413 or re.search(r"request too large|request_too_large",
                                                                         str(exc), re.I):
                    # the request itself is too big for this model's limit: the caller shrinks the context and
                    # retries the SAME model, so the answer is not handed to another model over its size
                    raise LLMUnavailable(f"request too large: {desc}", "too_large")
                errors.append((kind, desc))
                if kind == "request":
                    rejected[bucket] = desc
                else:
                    with _lock:
                        _parked[bucket] = max(_parked.get(bucket, 0), time.time() + wait)
                        _park_kind[bucket] = kind
                if bucket == prefer or time.time() > deadline:
                    break                   # re-plan: wait for the pinned bucket, or hand over with a reason
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
            return Reply(**rep, cached=False, ms=round((time.perf_counter() - t0) * 1000, 1),
                         switched=switched if bucket != prefer else None)
