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

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const sid = await getSession();
  const r = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", "X-Session": sid, ...(init.headers || {}) },
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
    headers: opts.form ? { "X-Session": sid } : { "content-type": "application/json", "X-Session": sid },
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
