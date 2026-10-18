"""Server-verified identity for shared (company-wide) decisions.

A session id (X-Session) only opens a private workspace copy: anyone can mint one, so it cannot stand for a
person. Everything that changes what *other* workspaces see (promote / retire / contest a company rule;
approve / reject / execute a company-record publication; retire / contest a published fact) needs a
*principal*: a named account configured on the server, proved by a secret, carried in a signed token.

Configuration (env, read at call time):
    BIDBRAIN_PRINCIPALS   "name:role:sha256hex(secret),name:role:sha256hex(secret)"  or a path to a JSON file
                          [{"name": ..., "role": ..., "sha256": ...}]. Roles: bid_manager | approver | admin.
    BIDBRAIN_AUTH_KEY     HMAC key that signs login tokens (at least 16 characters).
    BIDBRAIN_AUTH_TTL     token lifetime in seconds (default 8 h).
    BIDBRAIN_AUTH_COOKIE_SAMESITE  lax (default) | strict | none  (none also sets Secure; for a cross-site UI).

With no principals (or no key) configured, shared publication and promotion are disabled (fail closed);
session-local work never needs a login. `scripts/make_demo_principals.py` writes two demo principals.

Token: base64url(json{n, iat, exp, j}) "." hex(HMAC-SHA256(key, payload)). The role is NOT taken from the
token: it is looked up in the current configuration, so removing or re-roling a principal takes effect at once.
Tokens are accepted from the HttpOnly cookie `bidbrain_auth` or an `Authorization: Bearer` header. A cookie is
accepted only when the request's Origin (if any) is this host or an explicitly configured CORS origin, so a
foreign page cannot ride a visitor's cookie (CSRF). Typed names (`user` in a body) are display labels only.
"""
from __future__ import annotations

import base64
import contextlib
import contextvars
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import threading
import time
from dataclasses import dataclass

ROLES = ("bid_manager", "approver", "admin")
COOKIE = "bidbrain_auth"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")

DISABLED_MSG = ("shared publication and promotion are disabled on this server: no principals are configured "
                "(set BIDBRAIN_PRINCIPALS and BIDBRAIN_AUTH_KEY; for a local demo run scripts/make_demo_principals.py). "
                "Session-local work (qualify, live documents, local decisions and actions) needs no login.")


@dataclass(frozen=True)
class Principal:
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def as_dict(self) -> dict:
        return {"name": self.name, "role": self.role}


class AuthError(Exception):
    def __init__(self, msg: str, status: int = 401):
        super().__init__(msg)
        self.status = status


# the principal acting in this request / call (set by the API and the MCP server, read by the company modules)
current: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("bidbrain_principal", default=None)
# why a token the request carried was refused (forged, expired, foreign origin), so `require` can say so
problem: contextvars.ContextVar[AuthError | None] = contextvars.ContextVar("bidbrain_auth_problem", default=None)

_cache: dict = {"src": None, "val": {}}
_revoked: dict[str, int] = {}          # token jti -> exp (logout); in-memory, bounded by expiry
_lock = threading.Lock()


def principals() -> dict[str, dict]:
    """{name: {role, sha256}} from BIDBRAIN_PRINCIPALS (inline list or JSON file). Malformed entries are skipped."""
    raw = (os.getenv("BIDBRAIN_PRINCIPALS") or "").strip()
    src = raw
    p = pathlib.Path(raw) if raw and not raw.count(":") >= 2 else None
    if p is not None and p.is_file():
        src = f"{raw}@{p.stat().st_mtime_ns}"
    if _cache["src"] == src:
        return _cache["val"]
    out: dict[str, dict] = {}
    entries: list = []
    if p is not None and p.is_file():
        try:
            entries = [(e.get("name"), e.get("role"), e.get("sha256")) for e in json.loads(p.read_text())]
        except Exception:
            entries = []
    elif raw:
        entries = [tuple(x.strip().split(":")) for x in re.split(r"[,\n;]", raw) if x.strip()]
    for e in entries:
        if len(e) != 3:
            continue
        name, role, digest = (str(v or "").strip() for v in e)
        name, digest = name.lower(), digest.lower()
        if NAME_RE.match(name) and role in ROLES and HEX_RE.match(digest):
            out[name] = {"role": role, "sha256": digest}
    _cache.update(src=src, val=out)
    return out


def _key() -> bytes | None:
    k = os.getenv("BIDBRAIN_AUTH_KEY") or ""
    return k.encode() if len(k) >= 16 else None


def enabled() -> bool:
    return bool(principals()) and _key() is not None


def ttl() -> int:
    try:
        return max(60, int(os.getenv("BIDBRAIN_AUTH_TTL") or 8 * 3600))
    except ValueError:
        return 8 * 3600


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def login(name: str, secret: str) -> tuple[Principal, str, int]:
    """Check a principal's secret (constant time) and return (principal, token, exp)."""
    if not enabled():
        raise AuthError(DISABLED_MSG, 503)
    name = str(name or "").strip().lower()
    entry = principals().get(name)
    # hash something either way, so an unknown name and a wrong secret take the same time
    given = hash_secret(str(secret or ""))
    ok = hmac.compare_digest(given, entry["sha256"] if entry else "0" * 64)
    if not entry or not ok:
        raise AuthError("unknown principal or wrong secret", 401)
    return Principal(name, entry["role"]), *issue(name)


def issue(name: str, lifetime: int | None = None) -> tuple[str, int]:
    key = _key()
    if key is None:
        raise AuthError(DISABLED_MSG, 503)
    now = int(time.time())
    exp = now + (lifetime if lifetime is not None else ttl())
    payload = _b64(json.dumps({"n": name, "iat": now, "exp": exp, "j": secrets.token_hex(8)},
                              separators=(",", ":")).encode())
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}", exp


def verify(token: str | None) -> Principal:
    """The principal a token names, or AuthError(401). Checks the signature, the expiry, revocation and that
    the principal is still configured (its role comes from the configuration, not from the token)."""
    if not enabled():
        raise AuthError(DISABLED_MSG, 503)
    if not token or not isinstance(token, str) or token.count(".") != 1 or len(token) > 512:
        raise AuthError("not signed in", 401)
    payload, sig = token.split(".")
    want = hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig):
        raise AuthError("invalid sign-in token (bad signature)", 401)
    try:
        data = json.loads(_unb64(payload))
        name, exp, jti = str(data["n"]), int(data["exp"]), str(data.get("j") or "")
    except Exception:
        raise AuthError("invalid sign-in token", 401)
    if exp <= time.time():
        raise AuthError("sign-in expired: sign in again", 401)
    if jti and jti in _revoked:
        raise AuthError("signed out: sign in again", 401)
    entry = principals().get(name)
    if entry is None:
        raise AuthError(f"principal {name!r} is no longer configured", 401)
    return Principal(name, entry["role"])


def revoke(token: str | None) -> None:
    try:
        data = json.loads(_unb64(token.split(".")[0]))
        now = time.time()
        with _lock:
            for j, e in list(_revoked.items()):
                if e <= now:
                    _revoked.pop(j, None)
            if data.get("j"):
                _revoked[str(data["j"])] = int(data["exp"])
    except Exception:
        pass


# ---- request plumbing (FastAPI-agnostic: headers as a dict)

def _allowed_origin(origin: str, host: str) -> bool:
    if not origin:
        return True
    o = re.sub(r"^https?://", "", origin.strip().rstrip("/")).lower()
    if host and o == host.lower():
        return True
    allowed = [x.strip().rstrip("/") for x in (os.getenv("CORS_ORIGINS") or "").split(",") if x.strip()]
    return origin.strip().rstrip("/") in allowed and "*" not in allowed


def token_from(headers, cookies) -> tuple[str | None, str]:
    """(token, how) from an Authorization: Bearer header, else the auth cookie."""
    authz = headers.get("authorization") or ""
    if authz.lower().startswith("bearer "):
        return authz[7:].strip(), "bearer"
    tok = cookies.get(COOKIE)
    return (tok, "cookie") if tok else (None, "")


def from_request(request, required: bool = False) -> Principal | None:
    """The request's verified principal. Without a token: None (or AuthError when `required`). A token that is
    present but invalid (forged, expired, revoked) is always an error, never silently anonymous."""
    tok, how = token_from(request.headers, request.cookies)
    if not tok:
        if required:
            raise AuthError(DISABLED_MSG if not enabled() else "sign in first (POST /api/auth/login)",
                            503 if not enabled() else 401)
        return None
    if how == "cookie" and not _allowed_origin(request.headers.get("origin") or "", request.headers.get("host") or ""):
        raise AuthError("sign-in cookie refused from a foreign origin; send Authorization: Bearer instead", 403)
    return verify(tok)


def resolve(headers: dict, cookies: dict) -> tuple[Principal | None, AuthError | None]:
    """(principal, problem) for a request's headers (lower-case keys) and cookies; never raises."""
    tok, how = token_from(headers, cookies)
    if not tok:
        return None, None
    try:
        if how == "cookie" and not _allowed_origin(headers.get("origin") or "", headers.get("host") or ""):
            raise AuthError("sign-in cookie refused from a foreign origin; send Authorization: Bearer instead", 403)
        return verify(tok), None
    except AuthError as exc:
        return None, exc


@contextlib.contextmanager
def acting_as(p: Principal | None):
    tok = current.set(p)
    try:
        yield p
    finally:
        current.reset(tok)


# ---- checks the company modules call (they raise the caller's error type)

def require(what: str) -> Principal:
    """The acting principal for a shared change, or AuthError: 503 when no principals exist, 401 if not signed in."""
    if not enabled():
        raise AuthError(f"{what}: {DISABLED_MSG}", 503)
    p = current.get()
    if p is None and problem.get() is not None:
        err = problem.get()
        raise AuthError(f"{what}: {err}", err.status)
    if p is None:
        raise AuthError(f"{what} needs a signed-in principal (POST /api/auth/login); "
                        "a session id or a typed name is not an identity", 401)
    return p


def actor(p: Principal | None, session_tag: str | None = None, label: str | None = None) -> str:
    """A human-readable actor string for audit trails: the verified principal first."""
    parts = []
    if p is not None:
        parts.append(f"principal:{p.name}")
    if session_tag:
        parts.append(session_tag)
    s = " ".join(parts) or "anonymous"
    return s + (f" ({label})" if label else "")
