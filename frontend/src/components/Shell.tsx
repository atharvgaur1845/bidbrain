"use client";

import clsx from "clsx";
import { Activity, BarChart3, BrainCircuit, FileStack, GitBranch, Home, MessageSquareText, Moon,
  RotateCcw, ScrollText, Sun, Network } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, getSession, resetSession } from "@/lib/api";
import { DocProvider } from "./ui";

const NAV = [
  { href: "/", label: "Overview", icon: Home },
  { href: "/ask/", label: "Ask", icon: MessageSquareText },
  { href: "/qualify/", label: "Qualify a tender", icon: ScrollText },
  { href: "/live/", label: "Live documents", icon: FileStack },
  { href: "/decisions/", label: "Decisions", icon: GitBranch },
  { href: "/explore/", label: "Explore graph", icon: Network },
  { href: "/benchmark/", label: "Benchmark", icon: BarChart3 },
  { href: "/memory/", label: "Memory", icon: Activity },
];

export default function Shell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const [sid, setSid] = useState<string>("");
  const [health, setHealth] = useState<any>(null);
  const [theme, setTheme] = useState<"dark" | "light">("dark");

  useEffect(() => {
    getSession().then(setSid).catch(() => {});
    api("/api/health").then(setHealth).catch(() => setHealth({ ok: false }));
    try {
      const t = (localStorage.getItem("bidbrain.theme") as "dark" | "light") || "dark";
      setTheme(t);
    } catch {}
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("bidbrain.theme", theme); } catch {}
  }, [theme]);

  const llm = health?.llm?.length ? health.llm.join(" → ") : health?.replay ? "replay" : "no LLM (deterministic mode)";

  return (
    <DocProvider>
      <div className="flex min-h-screen">
        <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r hairline bg-bg-2 px-3 py-4 md:flex">
          <Link href="/" className="mb-6 flex items-center gap-2.5 px-2">
            <span className="grid h-8 w-8 place-items-center rounded-lg" style={{ background: "var(--accent)" }}>
              <BrainCircuit size={18} color="#1a1205" />
            </span>
            <span>
              <span className="block text-[15px] font-bold leading-tight">BidBrain</span>
              <span className="block text-[11px] text-ink-3">company brain · FalkorDB</span>
            </span>
          </Link>
          <nav className="flex flex-col gap-0.5">
            {NAV.map(({ href, label, icon: Icon }) => {
              const active = href === "/" ? path === "/" : path?.startsWith(href.replace(/\/$/, ""));
              return (
                <Link key={href} href={href}
                  className={clsx("flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13.5px] font-medium transition-colors",
                    active ? "bg-panel-2 text-ink" : "text-ink-2 hover:bg-panel hover:text-ink")}
                  style={active ? { boxShadow: "inset 2px 0 0 var(--accent)" } : undefined}>
                  <Icon size={16} className={active ? "text-accent" : ""} />
                  {label}
                </Link>
              );
            })}
          </nav>
          <div className="mt-auto space-y-2 px-1 text-[11.5px] text-ink-3">
            <div className="panel-2 space-y-1 px-3 py-2">
              <div className="flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: health?.ok ? "var(--pass)" : "var(--fail)" }} />
                graph {health?.ok ? "online" : "offline"}
              </div>
              <div className="truncate" title={llm}>LLM: {llm}</div>
              <div className="num truncate" title="Your private copy of the graph (GRAPH.COPY)">session_{sid || "…"}</div>
            </div>
            <div className="flex gap-1.5">
              <button className="btn flex-1 justify-center !py-1.5 !text-[12px]" title="Start over with a fresh copy of the graph"
                onClick={async () => { setSid(await resetSession()); location.reload(); }}>
                <RotateCcw size={13} /> Reset
              </button>
              <button className="btn !py-1.5" aria-label="Toggle theme" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
                {theme === "dark" ? <Sun size={13} /> : <Moon size={13} />}
              </button>
            </div>
          </div>
        </aside>
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex items-center gap-2 border-b hairline px-4 py-2 md:hidden">
            <BrainCircuit size={18} className="text-accent" />
            <span className="font-bold">BidBrain</span>
            <div className="scroll-thin ml-2 flex gap-1 overflow-x-auto">
              {NAV.map(({ href, label }) => (
                <Link key={href} href={href} className="chip">{label}</Link>
              ))}
            </div>
          </div>
          <main className="mx-auto w-full max-w-[1500px] flex-1 px-4 py-6 md:px-8">{children}</main>
        </div>
      </div>
    </DocProvider>
  );
}

export function PageHeader({ eyebrow, title, children }: { eyebrow: string; title: string; children?: React.ReactNode }) {
  return (
    <div className="mb-6">
      <div className="eyebrow">{eyebrow}</div>
      <h1 className="mt-1 text-[26px] font-bold tracking-tight">{title}</h1>
      {children && <p className="mt-1.5 max-w-3xl text-[14px] leading-relaxed text-ink-2">{children}</p>}
    </div>
  );
}
