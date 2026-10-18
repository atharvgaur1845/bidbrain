// API client: session handling, JSON calls, and SSE-over-fetch streaming
// (EventSource cannot POST, so streams are read from fetch's body reader).

export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE || "").replace(/\/$/, "");
const SID_KEY = "bidbrain.session";

let sessionPromise: Promise<string> | null = null;

export function getSession(): Promise<string> {
  if (sessionPromise) return sessionPromise;
  sessionPromise = (async () => {
    try {
      const saved = typeof window !== "undefined" ? window.localStorage.getItem(SID_KEY) : null;
      if (saved) return saved;
    } catch {}
    const r = await fetch(`${API_BASE}/api/session`, { method: "POST" });
    const { session_id } = await r.json();
    try { window.localStorage.setItem(SID_KEY, session_id); } catch {}
    return session_id as string;
  })();
  return sessionPromise;
}

export async function resetSession(): Promise<string> {
  try { window.localStorage.removeItem(SID_KEY); } catch {}
  sessionPromise = null;
  return getSession();
}

// ---- sign-in (server-verified principals; see backend/bidbrain/api/auth.py)
// The server sets an HttpOnly cookie (works when the UI is served by the API). The token is also kept in
// memory only, and sent as a Bearer header, so a UI on another origin works too. A session id is a workspace,
// never an identity: promoting, publishing, approving and retiring shared facts need a principal.
export type Principal = { name: string; role: "bid_manager" | "approver" | "admin" | string };
export type AuthState = { principal: Principal | null; enabled: boolean; message?: string | null };
let authToken: string | null = null;
let authState: AuthState | null = null;
const authListeners = new Set<(s: AuthState) => void>();

function authHeaders(): Record<string, string> {
  return authToken ? { Authorization: `Bearer ${authToken}` } : {};
}
function setAuth(s: AuthState) {
  authState = s;
  authListeners.forEach((f) => f(s));
}
export function onAuth(f: (s: AuthState) => void): () => void {
  authListeners.add(f);
  if (authState) f(authState);
  return () => { authListeners.delete(f); };
}
export async function authMe(): Promise<AuthState> {
  const r = await fetch(`${API_BASE}/api/auth/me`, { headers: authHeaders() });
  const j = await r.json();
  const s = { principal: j.principal || null, enabled: !!j.enabled, message: j.message };
  setAuth(s);
  return s;
}
export async function authLogin(name: string, secret: string): Promise<Principal> {
  const r = await fetch(`${API_BASE}/api/auth/login`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name, secret }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `${r.status}`);
  authToken = j.token || null;
  setAuth({ principal: j.principal, enabled: true });
  return j.principal;
}
export async function authLogout(): Promise<void> {
  try { await fetch(`${API_BASE}/api/auth/logout`, { method: "POST", headers: authHeaders() }); } catch {}
  authToken = null;
  setAuth({ principal: null, enabled: authState?.enabled ?? true, message: authState?.message });
}

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const sid = await getSession();
  const r = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", "X-Session": sid, ...authHeaders(), ...(init.headers || {}) },
  });
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

export type StreamEvent = { type: string; [k: string]: any };

export async function stream(path: string, body: any, onEvent: (e: StreamEvent) => void,
                             opts: { form?: FormData; signal?: AbortSignal } = {}): Promise<void> {
  const sid = await getSession();
  const r = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: opts.form ? { "X-Session": sid, ...authHeaders() }
      : { "content-type": "application/json", "X-Session": sid, ...authHeaders() },
    body: opts.form ?? JSON.stringify(body),
    signal: opts.signal,
  });
  if (!r.ok || !r.body) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch {}
    onEvent({ type: "error", message: msg });
    return;
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      if (chunk.startsWith("event: end")) return;
      const line = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (line) {
        try { onEvent(JSON.parse(line.slice(6))); } catch {}
      }
    }
  }
}

export const docUrl = (docId: string, page?: number | null) =>
  `${API_BASE}/api/docs/${encodeURIComponent(docId)}${page ? `#page=${page}` : ""}`;

export const fmtINR = (v: number | null | undefined) => {
  if (v === null || v === undefined || isNaN(Number(v))) return "—";
  const n = Number(v);
  if (Math.abs(n) >= 1e7) return `₹${(n / 1e7).toLocaleString("en-IN", { maximumFractionDigits: 2 })} Cr`;
  if (Math.abs(n) >= 1e5) return `₹${(n / 1e5).toLocaleString("en-IN", { maximumFractionDigits: 2 })} L`;
  return `₹${n.toLocaleString("en-IN")}`;
};
