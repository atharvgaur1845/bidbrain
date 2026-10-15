"use client";

import clsx from "clsx";
import { BarChart3, Compass, FileStack, GitBranch, History, Home, MessageSquare, Moon, RotateCcw,
  ScrollText, Search, Sun } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, getSession, resetSession } from "@/lib/api";
import { Button, DocProvider } from "./ui";

const NAV = [
  { href: "/", label: "Home", icon: Home },
  { href: "/ask/", label: "Ask", icon: MessageSquare },
  { href: "/qualify/", label: "Qualify a tender", icon: ScrollText },
  { href: "/live/", label: "Live documents", icon: FileStack },
  { href: "/decisions/", label: "Decisions", icon: GitBranch },
  { href: "/explore/", label: "Explore", icon: Compass },
  { href: "/benchmark/", label: "Benchmark", icon: BarChart3 },
  { href: "/memory/", label: "Memory", icon: History },
];

function setThemeNoTransition(theme: "dark" | "light") {
  const style = document.createElement("style");
  style.append(document.createTextNode("*,*::before,*::after{transition:none !important}"));
  document.head.append(style);
  document.documentElement.dataset.theme = theme;
  void document.body.offsetHeight;
  requestAnimationFrame(() => requestAnimationFrame(() => style.remove()));
}

export default function Shell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const [sid, setSid] = useState("");
  const [health, setHealth] = useState<any>(null);
  const [theme, setTheme] = useState<"dark" | "light">("light");
  const [q, setQ] = useState("");

  useEffect(() => {
    getSession().then(setSid).catch(() => {});
    api("/api/health").then(setHealth).catch(() => setHealth({ ok: false }));
    try {
      const t = (localStorage.getItem("bidbrain.theme") as "dark" | "light") || "light";
      setTheme(t);
      setThemeNoTransition(t);
    } catch {}
  }, []);

  function toggleTheme() {
    const t = theme === "dark" ? "light" : "dark";
    setTheme(t);
    setThemeNoTransition(t);
    try { localStorage.setItem("bidbrain.theme", t); } catch {}
  }

  const model = health?.llm?.length ? String(health.llm[0]).split(":").slice(1).join(":").replace(/^.*\//, "") : null;

  return (
    <DocProvider>
      <div className="flex min-h-screen">
        <aside className="sticky top-0 hidden h-screen w-[232px] shrink-0 flex-col px-3 py-5 md:flex">
          <Link href="/" className="mb-7 px-3" aria-label="BidBrain home">
            <span className="text-[24px] font-extrabold tracking-[-0.04em] text-kb">bidbrain</span>
          </Link>
          <nav className="flex flex-col gap-0.5" aria-label="Main">
            {NAV.map(({ href, label, icon: Icon }) => {
              const active = href === "/" ? path === "/" : path?.startsWith(href.replace(/\/$/, ""));
              return (
                <Link key={href} href={href} aria-current={active ? "page" : undefined}
                  className={clsx("flex h-10 items-center gap-3.5 rounded-full px-3.5 text-[14px] transition-colors duration-150 ease-out",
                    active ? "bg-surface-solid font-semibold text-ink shadow-[var(--shadow-border)]" : "font-medium text-ink-2 hover:bg-well hover:text-ink")}>
                  <Icon size={18} strokeWidth={active ? 2.1 : 1.75} />
                  {label}
                </Link>
              );
            })}
          </nav>
          <div className="mt-auto space-y-3 px-3 text-[12.5px] text-ink-3">
            <div className="space-y-1.5">
              <div className="flex items-center gap-2">
                <span className="h-2 w-2 rounded-full" style={{ background: health?.ok ? "var(--ok)" : "var(--bad)" }} />
                FalkorDB {health?.ok ? "online" : "offline"}
              </div>
              <div className="flex items-center gap-2" title={health?.llm?.join(", ")}>
                <span className="h-2 w-2 rounded-full" style={{ background: model ? "var(--kb)" : "var(--text-3)" }} />
                <span className="truncate">{model || (health?.replay ? "Recorded answers" : "No model")}</span>
              </div>
              <div className="num truncate" title="Your private copy of the company graph">graph · {sid || "…"}</div>
            </div>
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-30 flex h-16 items-center gap-3 px-4 md:px-8"
            style={{ background: "color-mix(in oklab, var(--bg) 70%, transparent)", backdropFilter: "blur(14px) saturate(140%)", WebkitBackdropFilter: "blur(14px) saturate(140%)" }}>
            <Link href="/" className="text-[20px] font-extrabold tracking-[-0.04em] text-kb md:hidden">bidbrain</Link>
            <form className="relative w-full max-w-xl" role="search"
              onSubmit={(e) => { e.preventDefault(); if (q.trim()) router.push(`/explore/?q=${encodeURIComponent(q.trim())}`); }}>
              <Search size={17} strokeWidth={1.75} className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-ink-3" />
              <input value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search the company graph"
                placeholder="Search clients, people, works, tenders"
                className="h-11 w-full rounded-full border border-transparent bg-surface-solid pl-11 pr-4 text-[14px] text-ink shadow-[var(--shadow-border)] outline-none transition-[box-shadow] duration-150 ease-out placeholder:text-ink-3 focus:shadow-[0_0_0_3px_var(--kb-soft),var(--shadow-border-hover)]" />
            </form>
            <div className="ml-auto flex items-center gap-1">
              <Button variant="ghost" size="sm" title="Start over with a fresh copy of the graph"
                onClick={async () => { setSid(await resetSession()); location.reload(); }}>
                <RotateCcw size={15} strokeWidth={1.75} /> <span className="hidden sm:inline">Reset</span>
              </Button>
              <Button variant="ghost" size="sm" aria-label={theme === "dark" ? "Light theme" : "Dark theme"} onClick={toggleTheme}>
                {theme === "dark" ? <Sun size={16} strokeWidth={1.75} /> : <Moon size={16} strokeWidth={1.75} />}
              </Button>
            </div>
          </header>
          <nav className="scroll-thin flex gap-1 overflow-x-auto px-4 pb-2 md:hidden" aria-label="Main">
            {NAV.map(({ href, label }) => (
              <Link key={href} href={href} className={clsx("whitespace-nowrap rounded-full px-3 py-1.5 text-[13px]",
                (href === "/" ? path === "/" : path?.startsWith(href.replace(/\/$/, ""))) ? "bg-surface-solid font-semibold shadow-[var(--shadow-border)]" : "text-ink-2")}>{label}</Link>
            ))}
          </nav>
          <main className="mx-auto w-full max-w-[1360px] flex-1 px-4 pb-16 pt-4 md:px-8">{children}</main>
        </div>
      </div>
    </DocProvider>
  );
}

export function PageHeader({ title, subtitle, meta, actions, children }:
  { title: string; subtitle?: React.ReactNode; meta?: React.ReactNode; actions?: React.ReactNode; children?: React.ReactNode; eyebrow?: string }) {
  return (
    <div className="mb-6">
      <div className="flex flex-wrap items-start gap-4">
        <div className="min-w-0 flex-1">
          <h1 className="text-[28px] font-bold leading-tight tracking-[-0.02em] text-ink">{title}</h1>
          {(subtitle || children) && <p className="mt-1.5 max-w-3xl text-[15px] leading-relaxed text-ink-2">{subtitle || children}</p>}
          {meta && <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-ink-3">{meta}</div>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}
