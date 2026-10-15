"use client";

import clsx from "clsx";
import { ExternalLink, FileText, X } from "lucide-react";
import { createContext, useContext, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { docUrl } from "@/lib/api";

// ======================================================================= document viewer

type DocRef = { doc_id: string; page?: number | null; what?: string };
const DocCtx = createContext<(d: DocRef) => void>(() => {});
export const useOpenDoc = () => useContext(DocCtx);

export function DocProvider({ children }: { children: React.ReactNode }) {
  const [doc, setDoc] = useState<DocRef | null>(null);
  useEffect(() => {
    if (!doc) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDoc(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [doc]);
  return (
    <DocCtx.Provider value={setDoc}>
      {children}
      {doc && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/30 p-4 backdrop-blur-[2px]" onClick={() => setDoc(null)}>
          <div role="dialog" aria-modal="true" aria-label={`Document ${doc.doc_id}`}
            className="glass-pop flex h-[88vh] w-full max-w-4xl flex-col overflow-hidden" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-3 border-b border-line px-5 py-3">
              <FileText size={16} strokeWidth={1.75} className="text-kb-ink" />
              <div className="min-w-0">
                <div className="num text-[13px] font-semibold">{doc.doc_id}{doc.page ? ` · page ${doc.page}` : ""}</div>
                {doc.what && <div className="truncate text-[12.5px] text-ink-3">{doc.what}</div>}
              </div>
              <a className="ml-auto inline-flex items-center gap-1.5 text-[13px] font-medium text-kb-ink hover:underline"
                href={docUrl(doc.doc_id, doc.page)} target="_blank" rel="noreferrer">
                Open in new tab <ExternalLink size={13} strokeWidth={1.75} />
              </a>
              <Button variant="ghost" size="sm" aria-label="Close" onClick={() => setDoc(null)}><X size={16} strokeWidth={1.75} /></Button>
            </div>
            <iframe title={doc.doc_id} key={`${doc.doc_id}${doc.page}`} src={docUrl(doc.doc_id, doc.page)} className="h-full w-full bg-white" />
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
  if (!uniq.length) return <div className="text-[13px] text-ink-3">No documents cited yet.</div>;
  return (
    <ul className="-mx-2 flex flex-col">
      {uniq.slice(0, max).map((e, i) => (
        <li key={i}>
          <button onClick={() => open(e)}
            className="flex w-full items-start gap-2.5 rounded-lg px-2 py-2 text-left transition-colors duration-150 ease-out hover:bg-well">
            <FileText size={15} strokeWidth={1.75} className="mt-0.5 shrink-0 text-kb-ink" />
            <span className="min-w-0">
              <span className="num text-[12.5px] font-semibold text-ink">{e.doc_id}</span>
              {e.page ? <span className="num text-[12px] text-ink-3"> · p.{e.page}</span> : null}
              {e.what && <span className="block truncate text-[12.5px] text-ink-2">{e.what}</span>}
            </span>
          </button>
        </li>
      ))}
      {uniq.length > max && <li className="px-2 pt-1 text-[12.5px] text-ink-3">{uniq.length - max} more documents</li>}
    </ul>
  );
}

// ======================================================================= primitives

export function Card({ title, action, children, className, pad = true }:
  { title?: React.ReactNode; action?: React.ReactNode; children: React.ReactNode; className?: string; pad?: boolean }) {
  return (
    <section className={clsx("glass", className)}>
      {(title || action) && (
        <header className="flex min-h-[48px] items-center gap-3 px-5 pt-3.5 pb-1">
          {title && <h2 className="text-[15px] font-semibold tracking-[-0.005em] text-ink">{title}</h2>}
          {action && <div className="ml-auto flex items-center gap-2">{action}</div>}
        </header>
      )}
      <div className={pad ? "px-5 pb-5 pt-2" : ""}>{children}</div>
    </section>
  );
}
/** @deprecated use Card */
export const Panel = Card;

export function Section({ title, subtitle, action }: { title: string; subtitle?: string; action?: React.ReactNode }) {
  return (
    <div className="mb-3 flex items-end gap-3">
      <div>
        <h2 className="text-[16px] font-semibold tracking-[-0.01em] text-ink">{title}</h2>
        {subtitle && <p className="mt-0.5 text-[13px] text-ink-3">{subtitle}</p>}
      </div>
      {action && <div className="ml-auto">{action}</div>}
    </div>
  );
}

export function Tabs({ tabs, value, onChange }:
  { tabs: { id: string; label: string; count?: number | string }[]; value: string; onChange: (id: string) => void }) {
  return (
    <div role="tablist" className="flex gap-6 border-b border-line">
      {tabs.map((t) => {
        const active = t.id === value;
        return (
          <button key={t.id} role="tab" aria-selected={active} onClick={() => onChange(t.id)}
            className={clsx("relative -mb-px flex items-center gap-1.5 pb-2.5 pt-1 text-[14px] transition-colors duration-150 ease-out",
              active ? "font-semibold text-ink" : "font-medium text-ink-3 hover:text-ink-2")}>
            {t.label}
            {t.count !== undefined && <span className="num rounded-full bg-well px-1.5 text-[11.5px] text-ink-3">{t.count}</span>}
            <span className={clsx("absolute inset-x-0 bottom-0 h-[2px] rounded-full", active ? "bg-ink" : "bg-transparent")} />
          </button>
        );
      })}
    </div>
  );
}

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "accent" | "secondary" | "ghost"; size?: "sm" | "md"; static?: boolean;
};
export function Button({ variant = "secondary", size = "md", static: isStatic, className, ...props }: ButtonProps) {
  return (
    <button {...props}
      className={clsx(
        "inline-flex items-center justify-center gap-2 rounded-full font-semibold whitespace-nowrap",
        "transition-[scale,background-color,box-shadow,color] duration-150 ease-out disabled:cursor-not-allowed disabled:opacity-45",
        !isStatic && "motion-safe:enabled:active:scale-[0.96]",
        size === "sm" ? "h-8 px-3.5 text-[13px]" : "h-10 px-5 text-[14px]",
        variant === "primary" && "bg-ink text-surface-solid enabled:hover:bg-ink/85",
        variant === "accent" && "bg-kb text-[#062433] enabled:hover:bg-[#4fcbff]",
        variant === "secondary" && "border border-transparent bg-surface-solid text-ink shadow-[var(--shadow-border)] enabled:hover:shadow-[var(--shadow-border-hover)]",
        variant === "ghost" && "text-ink-2 enabled:hover:bg-well enabled:hover:text-ink",
        className)} />
  );
}

const TONES: Record<string, string> = {
  neutral: "bg-well text-ink-2", blue: "bg-kb-soft text-kb-ink", ok: "bg-ok-soft text-ok",
  bad: "bg-bad-soft text-bad", warn: "bg-warn-soft text-warn", violet: "bg-violet-soft text-violet",
};
export function Badge({ tone = "neutral", children, className }: { tone?: keyof typeof TONES | string; children: React.ReactNode; className?: string }) {
  return (
    <span className={clsx("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[12px] font-medium leading-5 whitespace-nowrap", TONES[tone] || TONES.neutral, className)}>
      {children}
    </span>
  );
}

const VERDICT_TONE: Record<string, string> = {
  PASS: "ok", BID: "ok", PROVEN: "ok", FAIL: "bad", "NO-BID": "bad", AT_RISK: "warn", "BID WITH FIXES": "warn", DECLARE: "blue",
};
export function Verdict({ v, size = "md" }: { v?: string; size?: "sm" | "md" | "lg" }) {
  const tone = VERDICT_TONE[v || ""] || "neutral";
  const label = (v || "pending").replace("_", " ");
  return (
    <span className={clsx("inline-flex items-center gap-1.5 rounded-full font-semibold whitespace-nowrap", TONES[tone],
      size === "sm" ? "px-2 py-0.5 text-[11.5px]" : size === "lg" ? "px-3.5 py-1.5 text-[14px]" : "px-2.5 py-0.5 text-[12.5px]")}>
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {label.charAt(0) + label.slice(1).toLowerCase()}
    </span>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[12.5px] font-medium text-ink-3">{label}</div>
      <div className="num mt-1 truncate text-[26px] font-semibold leading-none tracking-[-0.02em]" style={{ color: tone }}>{value}</div>
      {sub && <div className="mt-1.5 text-[12.5px] leading-snug text-ink-3">{sub}</div>}
    </div>
  );
}

const AVATAR_HUES = [205, 265, 150, 25, 330, 185, 45, 290];
export function Avatar({ name, size = 28 }: { name: string; size?: number }) {
  const initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const hue = AVATAR_HUES[h % AVATAR_HUES.length];
  return (
    <span aria-hidden className="inline-grid shrink-0 place-items-center rounded-full font-semibold"
      style={{ width: size, height: size, fontSize: size * 0.4, background: `oklch(0.93 0.05 ${hue})`, color: `oklch(0.42 0.12 ${hue})` }}>
      {initials}
    </span>
  );
}

export function Empty({ icon, title, text }: { icon?: React.ReactNode; title: string; text?: React.ReactNode }) {
  return (
    <div className="grid place-items-center px-6 py-10 text-center">
      {icon && <div className="mb-3 text-ink-3">{icon}</div>}
      <div className="text-[14px] font-semibold text-ink-2">{title}</div>
      {text && <div className="mt-1 max-w-sm text-[13px] text-ink-3">{text}</div>}
    </div>
  );
}

export function CodeCell({ code, label = "Cypher" }: { code: string; label?: string }) {
  return (
    <div className="well flex overflow-hidden">
      <div className="num shrink-0 border-r border-line px-2.5 py-2.5 text-[11px] text-ink-3">{label}</div>
      <pre className="num scroll-thin max-h-60 flex-1 overflow-auto px-3 py-2.5 text-[12px] leading-relaxed text-ink-2">
        {code.trim().replace(/\n\s{6,}/g, "\n  ")}
      </pre>
    </div>
  );
}
/** @deprecated use CodeCell */
export const CypherBlock = ({ q }: { q: string }) => <CodeCell code={q} />;

export function Markdown({ children }: { children: string }) {
  return <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown></div>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-[13px] text-ink-3" role="status">
      <span className="pulse-dot h-2 w-2 rounded-full bg-kb" />{label || "Working"}
    </span>
  );
}
