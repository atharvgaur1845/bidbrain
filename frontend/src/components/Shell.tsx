"use client";

import clsx from "clsx";
import { BarChart3, Compass, FileStack, GitBranch, History, Home, MessageSquare, Moon, Plus, RotateCcw,
  ScrollText, Search, Sun } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, getSession, resetSession } from "@/lib/api";
import { Avatar, Button, DocProvider, IconTile, type TileTone } from "./ui";

const NAV = [
  [
    { href: "/", label: "Home", icon: Home },
    { href: "/ask/", label: "Ask", icon: MessageSquare },
    { href: "/qualify/", label: "Tenders", icon: ScrollText },
    { href: "/live/", label: "Live documents", icon: FileStack },
    { href: "/decisions/", label: "Decisions", icon: GitBranch },
  ],
  [
    { href: "/explore/", label: "Explore", icon: Compass },
    { href: "/benchmark/", label: "Benchmark", icon: BarChart3 },
    { href: "/memory/", label: "Memory", icon: History },
  ],
];
const FLAT = NAV.flat();

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

  const isActive = (href: string) => (href === "/" ? path === "/" : !!path?.startsWith(href.replace(/\/$/, "")));

  return (
    <DocProvider>
      <div className="min-h-screen p-0 md:p-3">
        <div className="frame flex min-h-screen overflow-hidden md:min-h-[calc(100vh-24px)] md:rounded-[18px]">
          <aside className="sticky top-0 hidden h-[calc(100vh-24px)] w-[220px] shrink-0 flex-col px-3 pb-3 pt-4 md:flex">
            <div className="mb-4 flex items-center gap-2 px-2">
              <Link href="/" aria-label="BidBrain home" className="text-[17px] font-semibold tracking-[-0.03em] text-ink">bidbrain</Link>
              <span className="rounded-md px-1.5 py-px text-[11px] font-medium"
                style={{ background: "var(--tile-peach)", color: "var(--tile-peach-ink)" }}>FalkorDB</span>
            </div>
            <Link href="/qualify/"
              className="mb-4 inline-flex h-9 items-center gap-2 rounded-lg bg-ink px-3 text-[13px] font-medium text-surface-solid shadow-[0_1px_2px_rgb(0_0_0/0.18)] transition-[scale,background-color] duration-150 ease-out hover:bg-ink/88 motion-safe:active:scale-[0.97]">
              <Plus size={15} strokeWidth={2} /> Qualify a tender
            </Link>
            <nav aria-label="Main" className="flex flex-col">
              {NAV.map((group, gi) => (
                <div key={gi} className={clsx("flex flex-col gap-px", gi > 0 && "mt-2 border-t border-line pt-2")}>
                  {group.map(({ href, label, icon: Icon }) => {
                    const active = isActive(href);
                    return (
                      <Link key={href} href={href} aria-current={active ? "page" : undefined}
                        className={clsx("flex h-8 items-center gap-2.5 rounded-lg px-2.5 text-[13px] transition-colors duration-150 ease-out",
                          active ? "bg-well-hover font-medium text-ink shadow-[inset_0_0_0_1px_var(--line)]" : "text-ink-2 hover:bg-well hover:text-ink")}>
                        <Icon size={16} strokeWidth={1.75} className={active ? "text-ink" : "text-ink-3"} />
                        {label}
                      </Link>
                    );
                  })}
                </div>
              ))}
            </nav>
            <div className="mt-auto space-y-2.5 px-1 text-[12px] text-ink-3">
              <div className="space-y-1 px-1.5">
                <div className="flex items-center gap-2">
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: health?.ok ? "var(--ok)" : "var(--bad)" }} />
                  FalkorDB {health?.ok ? "online" : "offline"}
                </div>
                <div className="flex items-center gap-2" title={health?.llm?.join(", ")}>
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: model ? "var(--kb)" : "var(--text-3)" }} />
                  <span className="truncate">{model || (health?.replay ? "Recorded answers" : "No model")}</span>
                </div>
              </div>
              <div className="flex items-center gap-2 rounded-lg border-t border-line px-1.5 pt-2.5">
                <Avatar name={sid ? `Session ${sid}` : "Session"} size={24} />
                <div className="min-w-0 flex-1" title="Your private copy of the company graph (GRAPH.COPY)">
                  <div className="truncate text-[12.5px] font-medium text-ink">Your graph</div>
                  <div className="num truncate text-[11px] text-ink-3">{sid || "…"}</div>
                </div>
                <button aria-label={theme === "dark" ? "Light theme" : "Dark theme"} onClick={toggleTheme}
                  className="grid h-7 w-7 place-items-center rounded-md text-ink-3 transition-colors duration-150 ease-out hover:bg-well hover:text-ink">
                  {theme === "dark" ? <Sun size={15} strokeWidth={1.75} /> : <Moon size={15} strokeWidth={1.75} />}
                </button>
              </div>
            </div>
          </aside>

          <div className="main-panel flex min-w-0 flex-1 flex-col md:rounded-l-[14px]">
            <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-line px-4 md:px-7"
              style={{ background: "color-mix(in oklab, var(--surface-solid) 70%, transparent)", backdropFilter: "blur(14px)", WebkitBackdropFilter: "blur(14px)" }}>
              <Link href="/" className="text-[16px] font-semibold tracking-[-0.03em] text-ink md:hidden">bidbrain</Link>
              <form className="relative w-full max-w-md" role="search"
                onSubmit={(e) => { e.preventDefault(); if (q.trim()) router.push(`/explore/?q=${encodeURIComponent(q.trim())}`); }}>
                <Search size={15} strokeWidth={1.75} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-3" />
                <input value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search the company graph"
                  placeholder="Search clients, people, works, tenders"
                  className="h-9 w-full rounded-lg border border-line bg-surface-solid/80 pl-9 pr-3 text-[13px] text-ink outline-none transition-[box-shadow,border-color] duration-150 ease-out placeholder:text-ink-3 focus:border-kb focus:shadow-[0_0_0_3px_var(--kb-soft)]" />
              </form>
              <div className="ml-auto flex items-center gap-1">
                <Button variant="secondary" size="sm" title="Start over with a fresh copy of the graph"
                  onClick={async () => { setSid(await resetSession()); location.reload(); }}>
                  <RotateCcw size={14} strokeWidth={1.75} /> <span className="hidden sm:inline">Reset</span>
                </Button>
                <Button variant="ghost" size="sm" className="md:hidden" aria-label={theme === "dark" ? "Light theme" : "Dark theme"} onClick={toggleTheme}>
                  {theme === "dark" ? <Sun size={15} strokeWidth={1.75} /> : <Moon size={15} strokeWidth={1.75} />}
                </Button>
              </div>
            </header>
            <nav className="scroll-thin flex gap-1 overflow-x-auto border-b border-line px-4 py-2 md:hidden" aria-label="Main">
              {FLAT.map(({ href, label }) => (
                <Link key={href} href={href} className={clsx("whitespace-nowrap rounded-lg px-2.5 py-1 text-[12.5px]",
                  isActive(href) ? "bg-well-hover font-medium text-ink" : "text-ink-2")}>{label}</Link>
              ))}
            </nav>
            <main className="mx-auto w-full max-w-[1320px] flex-1 px-4 pb-16 pt-6 md:px-7">{children}</main>
          </div>
        </div>
      </div>
    </DocProvider>
  );
}

export function PageHeader({ title, subtitle, meta, actions, children, icon, tone = "lavender" }:
  { title: string; subtitle?: React.ReactNode; meta?: React.ReactNode; actions?: React.ReactNode; children?: React.ReactNode;
    eyebrow?: string; icon?: React.ReactNode; tone?: TileTone }) {
  return (
    <div className="mb-5">
      <div className="flex flex-wrap items-start gap-3">
        {icon && <IconTile tone={tone} size={38}>{icon}</IconTile>}
        <div className="min-w-0 flex-1">
          <h1 className="text-[19px] font-semibold leading-tight tracking-[-0.015em] text-ink">{title}</h1>
          {(subtitle || children) && <p className="mt-1 max-w-3xl text-[13.5px] leading-relaxed text-ink-2">{subtitle || children}</p>}
          {meta && <div className="mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[12.5px] text-ink-3">{meta}</div>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}
