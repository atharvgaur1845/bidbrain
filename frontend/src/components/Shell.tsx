"use client";

import clsx from "clsx";
import { BarChart3, Compass, FileStack, GitBranch, GitCompareArrows, History, LayoutDashboard, ListChecks, Menu,
  MessageSquare, RotateCcw, ScrollText, Search, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, getSession, resetSession } from "@/lib/api";
import { Button, DocProvider, type TileTone } from "./ui";

type NavItem = { href: string; label: string; icon: typeof LayoutDashboard };
const NAV: { label: string | null; items: NavItem[] }[] = [
  { label: "Operate", items: [
    { href: "/", label: "Control room", icon: LayoutDashboard },
    { href: "/impact/", label: "Change impact", icon: GitCompareArrows },
    { href: "/qualify/", label: "Tenders", icon: ScrollText },
    { href: "/live/", label: "Live documents", icon: FileStack },
    { href: "/decisions/", label: "Decisions", icon: GitBranch },
    { href: "/actions/", label: "Actions", icon: ListChecks },
  ] },
  { label: "Knowledge", items: [
    { href: "/memory/", label: "Memory", icon: History },
    { href: "/ask/", label: "Ask", icon: MessageSquare },
    { href: "/explore/", label: "Explore", icon: Compass },
  ] },
  { label: "Evaluate", items: [
    { href: "/benchmark/", label: "Benchmark", icon: BarChart3 },
  ] },
];

type Health = { ok?: boolean; llm?: string[]; replay?: boolean; tools?: number } | null;

function Wordmark() {
  return (
    <Link href="/" aria-label="BidBrain control room" className="flex min-w-0 items-center gap-2 rounded-md">
      <span aria-hidden className="grid h-6 w-6 shrink-0 place-items-center rounded-[5px] bg-kb text-[12px] font-bold text-white">B</span>
      <span className="truncate text-[14px] font-semibold tracking-[-0.01em] text-ink">BidBrain</span>
      <span className="truncate text-[11.5px] text-ink-3">on FalkorDB</span>
    </Link>
  );
}

function NavList({ isActive, onNavigate }: { isActive: (href: string) => boolean; onNavigate?: () => void }) {
  return (
    <nav aria-label="Main" className="flex flex-col gap-4">
      {NAV.map((group) => (
        <div key={group.label}>
          {group.label && <div className="mb-1 px-2 text-[11px] font-medium text-ink-3">{group.label}</div>}
          <ul className="flex flex-col gap-px">
            {group.items.map(({ href, label, icon: Icon }) => {
              const active = isActive(href);
              return (
                <li key={href}>
                  <Link href={href} aria-current={active ? "page" : undefined} onClick={onNavigate}
                    className={clsx("flex h-[30px] items-center gap-2.5 rounded-md px-2 text-[13px] transition-colors duration-100 ease-out",
                      active ? "bg-nav-active font-medium text-ink" : "text-ink-2 hover:bg-well hover:text-ink")}>
                    <Icon size={16} strokeWidth={1.75} aria-hidden className={active ? "text-kb" : "text-ink-3"} />
                    {label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

function StatusBlock({ health, sid }: { health: Health; sid: string }) {
  const model = health?.llm?.length ? String(health.llm[0]).split(":").slice(1).join(":").replace(/^.*\//, "") : null;
  const db = health === null ? "checking" : health.ok ? "online" : "offline";
  return (
    <div className="space-y-2 border-t border-line pt-3 text-[12px]">
      <div className="space-y-1 px-2" aria-live="polite">
        <div className="flex items-center gap-2 text-ink-2">
          <span aria-hidden className={clsx("h-1.5 w-1.5 shrink-0 rounded-full",
            db === "online" ? "bg-ok" : db === "offline" ? "bg-bad" : "bg-[var(--text-3)]")} />
          FalkorDB <span className="text-ink-3">{db}</span>
        </div>
        <div className="flex min-w-0 items-center gap-2 text-ink-2" title={health?.llm?.join(", ")}>
          <span aria-hidden className={clsx("h-1.5 w-1.5 shrink-0 rounded-full", model ? "bg-ok" : health?.replay ? "bg-info" : "bg-[var(--text-3)]")} />
          <span className="shrink-0">LLM</span>
          <span className="min-w-0 truncate text-ink-3">{model || (health?.replay ? "recorded answers" : health ? "none" : "checking")}</span>
        </div>
      </div>
      <div className="rounded-md border border-line bg-surface px-2 py-1.5" title="Your private copy of the company graph (GRAPH.COPY)">
        <div className="text-[12px] font-medium text-ink">Your graph</div>
        <div className="mono truncate text-[11px] text-ink-3">{sid ? `session_${sid.replace(/^session_/, "")}` : "connecting…"}</div>
      </div>
    </div>
  );
}

export default function Shell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const [sid, setSid] = useState("");
  const [health, setHealth] = useState<Health>(null);
  const [q, setQ] = useState("");
  const [drawer, setDrawer] = useState(false);
  const [resetting, setResetting] = useState(false);
  const drawerRef = useRef<HTMLDivElement>(null);
  const menuBtn = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    getSession().then(setSid).catch(() => {});
    const check = () => api("/api/health").then(setHealth).catch(() => setHealth({ ok: false }));
    check();
    const t = setInterval(check, 60_000);
    return () => clearInterval(t);
  }, []);

  // the drawer closes on navigation and on Escape, and returns focus to the menu button
  // eslint-disable-next-line react-hooks/set-state-in-effect -- route change is an external event
  useEffect(() => { setDrawer(false); }, [path]);
  useEffect(() => {
    if (!drawer) return;
    const btn = menuBtn.current;
    drawerRef.current?.querySelector<HTMLElement>("a, button, input")?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDrawer(false);
    window.addEventListener("keydown", onKey);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = overflow; btn?.focus(); };
  }, [drawer]);

  const isActive = (href: string) => (href === "/" ? path === "/" : !!path?.startsWith(href.replace(/\/$/, "")));

  async function reset() {
    setResetting(true);
    try { setSid(await resetSession()); location.reload(); } catch { setResetting(false); }
  }

  const search = (
    <form className="relative w-full" role="search"
      onSubmit={(e) => { e.preventDefault(); if (q.trim()) { router.push(`/explore/?q=${encodeURIComponent(q.trim())}`); setDrawer(false); } }}>
      <Search size={14} strokeWidth={1.75} aria-hidden className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-3" />
      <input value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search the company graph" type="search"
        placeholder="Search clients, people, works, tenders"
        className="field h-8 min-h-0 py-0 pl-8 pr-2.5 text-[13px]" />
    </form>
  );

  return (
    <DocProvider>
      <a href="#main" className="sr-only z-[60] rounded-md bg-surface px-3 py-2 text-[13px] font-medium text-ink shadow-[var(--shadow-pop)] focus:not-sr-only focus:fixed focus:left-3 focus:top-3">
        Skip to content
      </a>
      <div className="flex min-h-screen bg-canvas">
        {/* desktop sidebar */}
        <aside className="sticky top-0 hidden h-screen w-[var(--nav-w)] shrink-0 flex-col border-r border-line bg-sidebar lg:flex">
          <div className="flex h-12 shrink-0 items-center px-4"><Wordmark /></div>
          <div className="scroll-thin flex min-h-0 flex-1 flex-col overflow-y-auto px-2 pb-3 pt-2">
            <NavList isActive={isActive} />
            <div className="mt-auto pt-4"><StatusBlock health={health} sid={sid} /></div>
          </div>
        </aside>

        {/* mobile drawer */}
        {drawer && (
          <div className="fixed inset-0 z-50 lg:hidden">
            <div className="absolute inset-0 bg-[rgb(24_24_27/0.36)]" onClick={() => setDrawer(false)} aria-hidden />
            <div ref={drawerRef} id="nav-drawer" role="dialog" aria-modal="true" aria-label="Navigation"
              className="absolute inset-y-0 left-0 flex w-[min(280px,86vw)] flex-col border-r border-line bg-sidebar shadow-[var(--shadow-pop)]">
              <div className="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-line px-3">
                <Wordmark />
                <Button variant="ghost" size="sm" aria-label="Close navigation" onClick={() => setDrawer(false)}><X size={16} strokeWidth={1.75} /></Button>
              </div>
              <div className="scroll-thin flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-2 pb-3 pt-3">
                <div className="px-1 sm:hidden">{search}</div>
                <NavList isActive={isActive} onNavigate={() => setDrawer(false)} />
                <div className="mt-auto"><StatusBlock health={health} sid={sid} /></div>
              </div>
            </div>
          </div>
        )}

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-30 flex h-12 shrink-0 items-center gap-2 border-b border-line bg-surface px-3 sm:px-4 lg:px-6">
            <Button ref={menuBtn} variant="ghost" size="sm" className="-ml-1 lg:hidden" aria-label="Open navigation"
              aria-expanded={drawer} aria-controls="nav-drawer" onClick={() => setDrawer(true)}>
              <Menu size={17} strokeWidth={1.75} />
            </Button>
            <div className="min-w-0 lg:hidden"><Wordmark /></div>
            <div className="hidden w-full max-w-sm sm:block lg:max-w-md">{search}</div>
            <div className="ml-auto flex shrink-0 items-center gap-2">
              <span className="hidden items-center gap-1.5 text-[12px] text-ink-3 md:inline-flex lg:hidden" aria-hidden>
                <span className={clsx("h-1.5 w-1.5 rounded-full", health?.ok ? "bg-ok" : health ? "bg-bad" : "bg-[var(--text-3)]")} />
                FalkorDB
              </span>
              <Button variant="secondary" size="sm" loading={resetting} onClick={reset}
                title="Discard your changes and start from a fresh copy of the company graph" aria-label="Reset your graph">
                {!resetting && <RotateCcw size={13} strokeWidth={1.75} aria-hidden />}
                <span className="hidden sm:inline">Reset graph</span>
              </Button>
            </div>
          </header>
          <main id="main" tabIndex={-1} className="mx-auto w-full max-w-[1440px] flex-1 px-4 pb-16 pt-5 outline-none sm:px-5 lg:px-6">
            {children}
          </main>
        </div>
      </div>
    </DocProvider>
  );
}

/** Page title row. `icon`/`tone`/`eyebrow` are accepted for compatibility and not drawn (titles carry no icons). */
export function PageHeader({ title, subtitle, meta, actions, children }:
  { title: string; subtitle?: React.ReactNode; meta?: React.ReactNode; actions?: React.ReactNode; children?: React.ReactNode;
    eyebrow?: string; icon?: React.ReactNode; tone?: TileTone }) {
  return (
    <div className="mb-5 flex flex-wrap items-start gap-x-4 gap-y-3">
      <div className="min-w-0 flex-1 basis-[280px]">
        <h1 className="text-[20px] font-semibold leading-7 tracking-[-0.012em] text-ink">{title}</h1>
        {(subtitle || children) && <p className="mt-0.5 max-w-3xl text-[13.5px] leading-relaxed text-ink-2">{subtitle || children}</p>}
        {meta && <div className="mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[12px] text-ink-3">{meta}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
