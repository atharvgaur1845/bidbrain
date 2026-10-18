"use client";
// Header sign-in: a small popover that signs in a server-configured principal (name + secret).
// Shared changes (promote, publish, approve, retire) need one; workspace-local work does not.

import { LogIn, LogOut, UserRound } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { authLogin, authLogout, authMe, onAuth, type AuthState } from "@/lib/api";
import { Button } from "./ui";

const ROLE: Record<string, string> = { bid_manager: "Bid manager", approver: "Approver", admin: "Admin" };

export function useAuth(): AuthState | null {
  const [s, setS] = useState<AuthState | null>(null);
  useEffect(() => onAuth(setS), []);
  return s;
}

export default function SignIn() {
  const auth = useAuth();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => { authMe().catch(() => {}); }, []);
  useEffect(() => {
    if (!open) return;
    box.current?.querySelector<HTMLElement>("input, button")?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onDown = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("mousedown", onDown); };
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setErr("");
    try { await authLogin(name.trim(), secret); setSecret(""); setOpen(false); }
    catch (x) { setErr(x instanceof Error ? x.message : String(x)); }
    finally { setBusy(false); }
  }

  const p = auth?.principal;
  return (
    <div className="relative" ref={box}>
      <Button variant="secondary" size="sm" onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-haspopup="dialog"
        title={p ? `Signed in as ${p.name} (${ROLE[p.role] || p.role})` : "Sign in to approve or publish shared facts"}>
        {p ? <UserRound size={13} strokeWidth={1.75} aria-hidden /> : <LogIn size={13} strokeWidth={1.75} aria-hidden />}
        <span className="hidden sm:inline">{p ? p.name : "Sign in"}</span>
      </Button>
      {open && (
        <div role="dialog" aria-label="Sign in"
          className="absolute right-0 top-[calc(100%+6px)] z-40 w-[min(300px,calc(100vw-32px))] rounded-lg border border-line bg-surface p-3 text-[13px] shadow-[var(--shadow-pop)]">
          {p ? (
            <div className="space-y-2">
              <div className="text-ink">Signed in as <span className="font-medium">{p.name}</span></div>
              <div className="text-[12px] text-ink-3">{ROLE[p.role] || p.role}. Approvals and publications are recorded under this name.</div>
              <Button variant="secondary" size="sm" onClick={() => { authLogout(); setOpen(false); }}>
                <LogOut size={13} strokeWidth={1.75} aria-hidden /> Sign out
              </Button>
            </div>
          ) : auth && !auth.enabled ? (
            <p className="text-[12.5px] text-ink-2">{auth.message || "Sign-in is not configured on this server. Shared publication is disabled; workspace work needs no login."}</p>
          ) : (
            <form className="space-y-2" onSubmit={submit}>
              <p className="text-[12px] text-ink-3">Needed only to promote, publish, approve or retire shared company facts.</p>
              <input className="field h-8 min-h-0 py-0 text-[13px]" placeholder="Name" aria-label="Principal name"
                autoComplete="username" value={name} onChange={(e) => setName(e.target.value)} />
              <input className="field h-8 min-h-0 py-0 text-[13px]" placeholder="Secret" aria-label="Secret" type="password"
                autoComplete="current-password" value={secret} onChange={(e) => setSecret(e.target.value)} />
              {err && <p className="text-[12px] text-bad">{err}</p>}
              <Button variant="primary" size="sm" type="submit" loading={busy} disabled={!name.trim() || !secret}>Sign in</Button>
            </form>
          )}
        </div>
      )}
    </div>
  );
}
