"use client";

import clsx from "clsx";
import { FileText, X } from "lucide-react";
import { createContext, useContext, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { docUrl } from "@/lib/api";

// ------------------------------------------------------------ document viewer (global)

type DocRef = { doc_id: string; page?: number | null; what?: string };
const DocCtx = createContext<(d: DocRef) => void>(() => {});
export const useOpenDoc = () => useContext(DocCtx);

export function DocProvider({ children }: { children: React.ReactNode }) {
  const [doc, setDoc] = useState<DocRef | null>(null);
  return (
    <DocCtx.Provider value={setDoc}>
      {children}
      {doc && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onClick={() => setDoc(null)}>
          <div className="panel flex h-[86vh] w-full max-w-4xl flex-col overflow-hidden" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-3 border-b hairline px-4 py-3">
              <FileText size={16} className="text-accent" />
              <div className="min-w-0">
                <div className="num text-[13px] font-semibold">{doc.doc_id}{doc.page ? ` · page ${doc.page}` : ""}</div>
                {doc.what && <div className="truncate text-[12px] text-ink-3">{doc.what}</div>}
              </div>
              <a className="btn ml-auto" href={docUrl(doc.doc_id, doc.page)} target="_blank" rel="noreferrer">Open</a>
              <button className="btn" onClick={() => setDoc(null)} aria-label="Close"><X size={14} /></button>
            </div>
            <iframe key={`${doc.doc_id}${doc.page}`} src={docUrl(doc.doc_id, doc.page)} className="h-full w-full bg-white" />
          </div>
        </div>
      )}
    </DocCtx.Provider>
  );
}

export function EvidenceList({ items, max = 12 }: { items: DocRef[]; max?: number }) {
  const open = useOpenDoc();
  const seen = new Set<string>();
  const uniq = items.filter((e) => {
    const k = `${e.doc_id}|${e.page}`;
    if (!e.doc_id || seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  if (!uniq.length) return <div className="text-[12.5px] text-ink-3">No document evidence yet.</div>;
  return (
    <div className="flex flex-col gap-1.5">
      {uniq.slice(0, max).map((e, i) => (
        <button key={i} onClick={() => open(e)}
          className="group flex items-start gap-2 rounded-lg border border-transparent px-2 py-1.5 text-left hover:border-line-2 hover:bg-bg-2">
          <FileText size={14} className="mt-0.5 shrink-0 text-accent" />
          <span className="min-w-0">
            <span className="num text-[12px] font-semibold text-ink">{e.doc_id}</span>
            {e.page ? <span className="num text-[11.5px] text-ink-3"> · p.{e.page}</span> : null}
            {e.what && <span className="block truncate text-[12px] text-ink-2">{e.what}</span>}
          </span>
        </button>
      ))}
      {uniq.length > max && <div className="px-2 text-[12px] text-ink-3">+ {uniq.length - max} more documents</div>}
    </div>
  );
}

// ------------------------------------------------------------ small pieces

export function Verdict({ v, size = "md" }: { v?: string; size?: "sm" | "md" | "lg" }) {
  const c = v === "PASS" || v === "BID" || v === "PROVEN" ? "var(--pass)"
    : v === "FAIL" || v === "NO-BID" ? "var(--fail)"
    : v === "AT_RISK" || v === "BID WITH FIXES" ? "var(--risk)"
    : v === "DECLARE" ? "var(--info)" : "var(--text-3)";
  return (
    <span className={clsx("inline-flex items-center gap-1.5 rounded-md font-bold tracking-wide",
      size === "sm" ? "px-1.5 py-0.5 text-[10.5px]" : size === "lg" ? "px-3 py-1.5 text-[14px]" : "px-2 py-0.5 text-[11.5px]")}
      style={{ color: c, background: `color-mix(in oklab, ${c} 14%, transparent)`, border: `1px solid color-mix(in oklab, ${c} 35%, transparent)` }}>
      {v || "…"}
    </span>
  );
}

export function Panel({ title, right, children, className, pad = true }:
  { title?: React.ReactNode; right?: React.ReactNode; children: React.ReactNode; className?: string; pad?: boolean }) {
  return (
    <section className={clsx("panel", className)}>
      {(title || right) && (
        <header className="flex items-center gap-2 border-b hairline px-4 py-2.5">
          <div className="eyebrow">{title}</div>
          <div className="ml-auto">{right}</div>
        </header>
      )}
      <div className={pad ? "p-4" : ""}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: string }) {
  return (
    <div className="panel-2 px-4 py-3">
      <div className="eyebrow">{label}</div>
      <div className="num mt-1 text-[22px] font-bold" style={{ color: tone }}>{value}</div>
      {sub && <div className="mt-0.5 text-[12px] text-ink-3">{sub}</div>}
    </div>
  );
}

export function Markdown({ children }: { children: string }) {
  return <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown></div>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-[12.5px] text-ink-3">
      <span className="h-2 w-2 rounded-full bg-accent pulse-dot" />{label || "working"}
    </span>
  );
}

export function CypherBlock({ q }: { q: string }) {
  return (
    <pre className="num scroll-thin max-h-56 overflow-auto rounded-lg border hairline bg-bg-2 p-3 text-[11.5px] leading-relaxed text-ink-2">
      {q.trim().replace(/\n\s{6,}/g, "\n  ")}
    </pre>
  );
}
