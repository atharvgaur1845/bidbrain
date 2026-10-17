"use client";

// BidBrain UI primitives. The contract (tokens, sizes, when to use what) is frontend/DESIGN.md.

import clsx from "clsx";
import { AlertTriangle, ExternalLink, FileText, Inbox, Loader2, RotateCcw, X } from "lucide-react";
import Link from "next/link";
import { createContext, useContext, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { docUrl } from "@/lib/api";

const IC = { size: 16, strokeWidth: 1.75 } as const;

// ======================================================================= document viewer

type DocRef = { doc_id: string; page?: number | null; what?: string };
const DocCtx = createContext<(d: DocRef) => void>(() => {});
export const useOpenDoc = () => useContext(DocCtx);

export function DocProvider({ children }: { children: React.ReactNode }) {
  const [doc, setDoc] = useState<DocRef | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!doc) return;
    const prev = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDoc(null);
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); prev?.focus?.(); };
  }, [doc]);
  return (
    <DocCtx.Provider value={setDoc}>
      {children}
      {doc && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-[rgb(24_24_27/0.42)] p-3 sm:p-6" onClick={() => setDoc(null)}>
          <div role="dialog" aria-modal="true" aria-label={`Document ${doc.doc_id}`}
            className="glass-pop flex h-[88vh] w-full max-w-4xl flex-col overflow-hidden" onClick={(e) => e.stopPropagation()}>
            <div className="flex h-12 shrink-0 items-center gap-3 border-b border-line px-4">
              <FileText {...IC} className="shrink-0 text-ink-3" />
              <div className="min-w-0 flex-1">
                <div className="mono truncate text-[12.5px] font-medium text-ink">{doc.doc_id}{doc.page ? ` · page ${doc.page}` : ""}</div>
                {doc.what && <div className="truncate text-[12px] text-ink-3">{doc.what}</div>}
              </div>
              <a className="hidden items-center gap-1.5 rounded-md px-2 py-1 text-[12.5px] font-medium text-kb-ink hover:bg-well sm:inline-flex"
                href={docUrl(doc.doc_id, doc.page)} target="_blank" rel="noreferrer">
                Open in new tab <ExternalLink size={13} strokeWidth={1.75} />
              </a>
              <Button ref={closeRef} variant="ghost" size="sm" aria-label="Close document" onClick={() => setDoc(null)}><X {...IC} /></Button>
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
            className="flex w-full items-start gap-2.5 rounded-md px-2 py-1.5 text-left transition-colors duration-100 ease-out hover:bg-well">
            <FileText size={14} strokeWidth={1.75} className="mt-[3px] shrink-0 text-ink-3" />
            <span className="min-w-0">
              <span className="mono text-[12px] font-medium text-kb-ink">{e.doc_id}</span>
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

// ======================================================================= card / panel

export function Card({ title, subtitle, icon, tone, action, footer, children, className, pad = true, id }:
  { title?: React.ReactNode; subtitle?: React.ReactNode; icon?: React.ReactNode; tone?: TileTone; action?: React.ReactNode;
    footer?: React.ReactNode; children: React.ReactNode; className?: string; pad?: boolean; id?: string }) {
  void tone;
  return (
    <section id={id} className={clsx("glass", className)}>
      {(title || action) && (
        <header className="flex min-h-11 items-center gap-x-2.5 border-b border-line px-4 py-2">
          {icon && <span aria-hidden className="shrink-0 text-ink-3">{icon}</span>}
          {title && (
            <div className="min-w-0 flex-1">
              <h2 className="text-[14px] font-semibold leading-5 text-ink">{title}</h2>
              {subtitle && <p className="text-[12px] leading-4 text-ink-3">{subtitle}</p>}
            </div>
          )}
          {action && <div className="ml-auto flex shrink-0 items-center gap-2">{action}</div>}
        </header>
      )}
      <div className={pad ? "p-4" : ""}>{children}</div>
      {footer && <footer className="border-t border-line bg-surface-2 px-4 py-2 text-[12.5px] text-ink-3 rounded-b-[7px]">{footer}</footer>}
    </section>
  );
}
/** Same as Card. */
export const Panel = Card;

export function Section({ title, subtitle, action }: { title: string; subtitle?: string; action?: React.ReactNode }) {
  return (
    <div className="mb-2.5 flex items-end gap-3">
      <div className="min-w-0">
        <h2 className="text-[14px] font-semibold text-ink">{title}</h2>
        {subtitle && <p className="mt-0.5 text-[12.5px] text-ink-3">{subtitle}</p>}
      </div>
      {action && <div className="ml-auto shrink-0">{action}</div>}
    </div>
  );
}

// ---- type marker: a neutral square with an icon. `tone` is accepted for compatibility and no longer colours it.
export type TileTone = "lavender" | "peach" | "teal" | "sage" | "rose" | "sand";
export function IconTile({ tone, size = 32, children }: { tone?: TileTone; size?: number; children: React.ReactNode }) {
  void tone;
  return (
    <span aria-hidden className="inline-grid shrink-0 place-items-center rounded-md border border-line bg-surface-2 text-ink-2"
      style={{ width: size, height: size }}>
      {children}
    </span>
  );
}

// ---- a result bar: "84% · Home ......... 26 responses" (thin, square-ended, accent first)
const BAR_TONES = ["var(--bar-1)", "var(--bar-2)", "var(--bar-3)", "var(--bar-4)"];
export function ResultBar({ label, value, max = 1, right, tone = 0, display }:
  { label: React.ReactNode; value: number | null | undefined; max?: number; right?: React.ReactNode; tone?: number | string;
    display?: React.ReactNode }) {
  const v = value == null ? null : Math.max(0, Math.min(1, value / (max || 1)));
  const color = typeof tone === "number" ? BAR_TONES[tone % BAR_TONES.length] : tone;
  return (
    <div className="min-w-0">
      <div className="flex items-baseline gap-2 text-[13px]">
        <span className="num shrink-0 font-medium text-ink">{display ?? (v == null ? "—" : `${Math.round(v * 100)}%`)}</span>
        <span className="min-w-0 truncate text-ink-2">{label}</span>
        {right && <span className="num ml-auto shrink-0 text-[12px] text-ink-3">{right}</span>}
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-[2px] bg-bar-track" role="presentation">
        {v != null && <div className="h-full rounded-[2px]" style={{ width: `${Math.max(v * 100, 1)}%`, background: color }} />}
      </div>
    </div>
  );
}

// ======================================================================= tabs (underline)

export type TabDef = { id: string; label: string; count?: number | string; icon?: React.ReactNode; disabled?: boolean };
export function Tabs({ tabs, value, onChange, className }:
  { tabs: TabDef[]; value: string; onChange: (id: string) => void; className?: string }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: React.KeyboardEvent, i: number) => {
    const on = tabs.flatMap((t, k) => (t.disabled ? [] : [k]));
    const at = Math.max(0, on.indexOf(i));
    const j = e.key === "ArrowRight" ? on[(at + 1) % on.length] : e.key === "ArrowLeft" ? on[(at - 1 + on.length) % on.length]
      : e.key === "Home" ? on[0] : e.key === "End" ? on[on.length - 1] : undefined;
    if (j === undefined) return;
    e.preventDefault();
    refs.current[j]?.focus();
    onChange(tabs[j].id);
  };
  return (
    <div role="tablist" className={clsx("scroll-thin flex gap-4 overflow-x-auto border-b border-line", className)}>
      {tabs.map((t, i) => {
        const active = t.id === value;
        return (
          <button key={t.id} ref={(el) => { refs.current[i] = el; }} role="tab" type="button" aria-selected={active}
            tabIndex={active ? 0 : -1} disabled={t.disabled} onClick={() => onChange(t.id)} onKeyDown={(e) => onKey(e, i)}
            className={clsx("relative -mb-px flex h-9 shrink-0 items-center gap-1.5 border-b-2 px-0.5 text-[13px] transition-colors duration-100 ease-out",
              "focus-visible:outline-offset-[-2px] disabled:cursor-not-allowed disabled:opacity-45",
              active ? "border-kb font-medium text-ink" : "border-transparent text-ink-2 enabled:hover:border-line-strong enabled:hover:text-ink")}>
            {t.icon && <span className={active ? "text-ink" : "text-ink-3"}>{t.icon}</span>}
            {t.label}
            {t.count !== undefined && (
              <span className={clsx("num rounded-[4px] px-1 text-[11px] leading-4", active ? "bg-kb-soft text-kb-ink" : "bg-well text-ink-3")}>{t.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

// ======================================================================= buttons

export type ButtonVariant = "primary" | "accent" | "secondary" | "ghost" | "danger";
export type ButtonSize = "xs" | "sm" | "md" | "lg";

export function buttonClass({ variant = "secondary", size = "md", className }:
  { variant?: ButtonVariant; size?: ButtonSize; className?: string } = {}) {
  return clsx(
    "inline-flex select-none items-center justify-center gap-1.5 rounded-md border font-medium whitespace-nowrap",
    "transition-[background-color,border-color,color,box-shadow] duration-100 ease-out",
    "disabled:cursor-not-allowed disabled:opacity-50 aria-disabled:cursor-not-allowed aria-disabled:opacity-50",
    size === "xs" && "h-6 px-2 text-[12px]",
    size === "sm" && "h-7 px-2.5 text-[12.5px]",
    size === "md" && "h-8 px-3 text-[13px]",
    size === "lg" && "h-9 px-3.5 text-[13.5px]",
    (variant === "primary" || variant === "accent") &&
      "border-kb bg-kb text-white shadow-[var(--shadow-sm)] enabled:hover:border-accent-hover enabled:hover:bg-accent-hover enabled:active:bg-[#1c3470] [a&]:hover:bg-accent-hover",
    variant === "secondary" &&
      "border-line-strong bg-surface text-ink shadow-[var(--shadow-xs)] enabled:hover:bg-surface-2 enabled:hover:border-[#b9b9b2] enabled:active:bg-well [a&]:hover:bg-surface-2",
    variant === "ghost" &&
      "border-transparent bg-transparent text-ink-2 enabled:hover:bg-well enabled:hover:text-ink enabled:active:bg-well-hover [a&]:hover:bg-well [a&]:hover:text-ink",
    variant === "danger" &&
      "border-bad bg-bad text-white shadow-[var(--shadow-sm)] enabled:hover:bg-[#9a1f18] enabled:hover:border-[#9a1f18] [a&]:hover:bg-[#9a1f18]",
    className);
}

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant; size?: ButtonSize; static?: boolean; loading?: boolean; ref?: React.Ref<HTMLButtonElement>;
};
/** `static` is accepted for compatibility (there is no press animation any more). */
export function Button({ variant = "secondary", size = "md", static: _static, loading, className, disabled, children, type, ...props }: ButtonProps) {
  void _static;
  return (
    <button type={type ?? "button"} {...props} disabled={disabled || loading} aria-busy={loading || undefined}
      className={buttonClass({ variant, size, className })}>
      {loading && <Loader2 size={14} strokeWidth={2} className="spin shrink-0" aria-hidden />}
      {children}
    </button>
  );
}

/** A Next link styled as a button (navigation, not an action). */
export function ButtonLink({ href, variant = "secondary", size = "md", className, children, ...props }:
  { href: string; variant?: ButtonVariant; size?: ButtonSize; className?: string; children: React.ReactNode } &
  Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, "href">) {
  return <Link href={href} {...props} className={buttonClass({ variant, size, className })}>{children}</Link>;
}

// ======================================================================= status (one map for every status in the app)

export type Tone = "neutral" | "ok" | "bad" | "warn" | "review" | "info" | "accent";
const TONE_CLASS: Record<Tone, string> = {
  neutral: "bg-[var(--neutral-soft)] text-ink-2 border-[var(--neutral-line)]",
  ok: "bg-ok-soft text-ok border-ok-line",
  bad: "bg-bad-soft text-bad border-bad-line",
  warn: "bg-warn-soft text-warn border-warn-line",
  review: "bg-violet-soft text-violet border-violet-line",
  info: "bg-info-soft text-info border-info-line",
  accent: "bg-kb-soft text-kb-ink border-info-line",
};
/** Legacy tone names map onto the semantic ones. */
const TONE_ALIAS: Record<string, Tone> = { blue: "info", violet: "review", gray: "neutral", grey: "neutral", slate: "neutral" };
const toTone = (t?: string): Tone => (t && (t in TONE_CLASS ? (t as Tone) : TONE_ALIAS[t])) || "neutral";

/** Solid dot colour per tone, for legends and inline counts. */
export const TONE_DOT: Record<Tone, string> = {
  neutral: "bg-[var(--text-3)]", ok: "bg-ok", bad: "bg-bad", warn: "bg-warn", review: "bg-violet", info: "bg-info", accent: "bg-kb",
};

/** Every status, verdict and recommendation the app shows, with its one tone and display label. */
export const STATUS: Record<string, { tone: Tone; label: string }> = {
  PASS: { tone: "ok", label: "Pass" },
  FAIL: { tone: "bad", label: "Fail" },
  AT_RISK: { tone: "warn", label: "At risk" },
  "NEEDS REVIEW": { tone: "review", label: "Needs review" },
  BID: { tone: "ok", label: "Bid" },
  "NO-BID": { tone: "bad", label: "No-bid" },
  "BID WITH FIXES": { tone: "warn", label: "Bid with fixes" },
  DECLARE: { tone: "info", label: "Declare" },
  PROVEN: { tone: "ok", label: "Proven" },
  pending: { tone: "neutral", label: "Pending" },
  approved: { tone: "info", label: "Approved" },
  rejected: { tone: "bad", label: "Rejected" },
  executed: { tone: "ok", label: "Executed" },
  open: { tone: "review", label: "Open" },
  resolved: { tone: "ok", label: "Resolved" },
  superseded: { tone: "neutral", label: "Superseded" },
  ripple: { tone: "warn", label: "Ripple" },
  conflict: { tone: "review", label: "Conflict" },
};
const STATUS_KEY = (s: string) => {
  if (s in STATUS) return s;
  const up = s.toUpperCase().replace(/_/g, " ").replace(/^NO BID$/, "NO-BID");
  if (up === "AT RISK") return "AT_RISK";
  if (up in STATUS) return up;
  const low = s.toLowerCase();
  return low in STATUS ? low : null;
};
export function statusInfo(s?: string | null): { tone: Tone; label: string } {
  const k = s ? STATUS_KEY(s) : "pending";
  if (k) return STATUS[k];
  const label = String(s).replace(/_/g, " ").toLowerCase();
  return { tone: "neutral", label: label.charAt(0).toUpperCase() + label.slice(1) };
}

export function StatusBadge({ status, size = "md", dot = true, label, className }:
  { status?: string | null; size?: "sm" | "md" | "lg"; dot?: boolean; label?: React.ReactNode; className?: string }) {
  const s = statusInfo(status);
  return (
    <span className={clsx("inline-flex items-center gap-1.5 rounded-[4px] border font-medium whitespace-nowrap", TONE_CLASS[s.tone],
      size === "sm" ? "h-5 px-1.5 text-[11.5px]" : size === "lg" ? "h-7 px-2.5 text-[13px]" : "h-[22px] px-2 text-[12px]", className)}>
      {dot && <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full bg-current" />}
      {label ?? s.label}
    </span>
  );
}

/** Verdict pill (PASS, FAIL, AT_RISK, DECLARE, BID, NO-BID, BID WITH FIXES, PROVEN, NEEDS REVIEW). */
export function Verdict({ v, size = "md" }: { v?: string; size?: "sm" | "md" | "lg" }) {
  return <StatusBadge status={v || "pending"} size={size} />;
}

/** A categorical label (category, policy, kind). Use sparingly; plain text is usually enough. */
export function Badge({ tone = "neutral", children, className }: { tone?: Tone | "blue" | "violet" | string; children: React.ReactNode; className?: string }) {
  return (
    <span className={clsx("inline-flex h-5 items-center gap-1 rounded-[4px] border px-1.5 text-[11.5px] font-medium whitespace-nowrap", TONE_CLASS[toTone(tone)], className)}>
      {children}
    </span>
  );
}

// ======================================================================= numbers, people

export function Stat({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[12px] text-ink-3">{label}</div>
      <div className="num mt-1 truncate text-[20px] font-semibold leading-7 tracking-[-0.01em] text-ink" style={{ color: tone }}>{value}</div>
      {sub && <div className="mt-0.5 text-[12px] leading-snug text-ink-3">{sub}</div>}
    </div>
  );
}

const AVATAR_HUES = [250, 220, 160, 30, 300, 190, 60, 270];
export function Avatar({ name, size = 28 }: { name: string; size?: number }) {
  const initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const hue = AVATAR_HUES[h % AVATAR_HUES.length];
  return (
    <span aria-hidden className="inline-grid shrink-0 place-items-center rounded-full font-semibold"
      style={{ width: size, height: size, fontSize: Math.round(size * 0.4), background: `oklch(0.94 0.02 ${hue})`, color: `oklch(0.40 0.06 ${hue})`,
               boxShadow: "inset 0 0 0 1px rgb(0 0 0 / 0.06)" }}>
      {initials}
    </span>
  );
}

/** Label/value pairs as a definition list. `cols` lays pairs out side by side on wide screens. */
export function KeyValue({ items, cols = 1, className }:
  { items: ([React.ReactNode, React.ReactNode] | { label: React.ReactNode; value: React.ReactNode; mono?: boolean })[];
    cols?: 1 | 2 | 3; className?: string }) {
  const rows = items.map((it) => (Array.isArray(it) ? { label: it[0], value: it[1], mono: false } : it));
  return (
    <dl className={clsx("grid gap-x-6", cols === 2 && "sm:grid-cols-2", cols === 3 && "sm:grid-cols-2 lg:grid-cols-3", className)}>
      {rows.map((r, i) => (
        <div key={i} className="flex min-w-0 items-baseline justify-between gap-3 border-b border-line py-1.5 text-[13px] last:border-b-0">
          <dt className="shrink-0 text-ink-3">{r.label}</dt>
          <dd className={clsx("min-w-0 truncate text-right text-ink", r.mono ? "mono text-[12px]" : "num")}>{r.value ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

// ======================================================================= tables

/** Scroll container + `.kt` table. `maxHeight` makes the header sticky inside the box. */
export function Table({ children, className, maxHeight, dense, label }:
  { children: React.ReactNode; className?: string; maxHeight?: number | string; dense?: boolean; label?: string }) {
  return (
    <div className={clsx("scroll-thin overflow-auto overscroll-x-contain", className)} style={maxHeight ? { maxHeight } : undefined}>
      <table className={clsx("kt", dense && "dense")} aria-label={label}>{children}</table>
    </div>
  );
}
type Align = "left" | "right" | "center";
export function Th({ children, align = "left", className, width, ...rest }:
  { children?: React.ReactNode; align?: Align; className?: string; width?: number | string } & React.ThHTMLAttributes<HTMLTableCellElement>) {
  return <th scope="col" {...rest} className={clsx(align === "right" && "text-right!", align === "center" && "text-center!", className)} style={width ? { width } : undefined}>{children}</th>;
}
export function Td({ children, align = "left", numeric, muted, className, ...rest }:
  { children?: React.ReactNode; align?: Align; numeric?: boolean; muted?: boolean; className?: string } & React.TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td {...rest} data-num={numeric || undefined}
      className={clsx(align === "right" && "text-right", align === "center" && "text-center", muted ? "text-ink-3" : "text-ink", className)}>
      {children}
    </td>
  );
}
export function Tr({ children, selected, onClick, className }:
  { children: React.ReactNode; selected?: boolean; onClick?: () => void; className?: string }) {
  return (
    <tr aria-selected={selected || undefined} onClick={onClick} className={clsx(onClick && "cursor-pointer", className)}>{children}</tr>
  );
}

// ======================================================================= timeline

export function Timeline({ children, className }: { children: React.ReactNode; className?: string }) {
  return <ol className={clsx("relative flex flex-col", className)}>{children}</ol>;
}
export function TimelineItem({ title, time, status, icon, children, last }:
  { title: React.ReactNode; time?: React.ReactNode; status?: string; icon?: React.ReactNode; children?: React.ReactNode; last?: boolean }) {
  const tone = status ? statusInfo(status).tone : "neutral";
  const dot = TONE_DOT;
  return (
    <li className="relative flex gap-3 pb-3 last:pb-0">
      {!last && <span aria-hidden className="absolute left-[7px] top-5 bottom-0 w-px bg-line" />}
      <span aria-hidden className="relative mt-[3px] grid h-[15px] w-[15px] shrink-0 place-items-center rounded-full border border-line bg-surface text-ink-3">
        {icon ?? <span className={clsx("h-[7px] w-[7px] rounded-full", dot[tone])} />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <div className="min-w-0 text-[13px] font-medium text-ink">{title}</div>
          {time && <div className="num ml-auto shrink-0 text-[12px] text-ink-3">{time}</div>}
        </div>
        {children && <div className="mt-0.5 text-[12.5px] leading-relaxed text-ink-2">{children}</div>}
      </div>
    </li>
  );
}

// ======================================================================= empty, error, loading

export function EmptyState({ icon, title, text, action, compact, className }:
  { icon?: React.ReactNode; title: string; text?: React.ReactNode; action?: React.ReactNode; compact?: boolean; className?: string }) {
  return (
    <div className={clsx("flex flex-col items-center px-6 text-center", compact ? "py-6" : "py-10", className)}>
      <div aria-hidden className="mb-3 grid h-9 w-9 place-items-center rounded-md border border-line bg-surface-2 text-ink-3">
        {icon ?? <Inbox {...IC} />}
      </div>
      <div className="text-[13.5px] font-semibold text-ink">{title}</div>
      {text && <div className="mt-1 max-w-sm text-[13px] leading-relaxed text-ink-3">{text}</div>}
      {action && <div className="mt-4 flex flex-wrap justify-center gap-2">{action}</div>}
    </div>
  );
}
/** Older name for EmptyState (same props). */
export function Empty(props: { icon?: React.ReactNode; title: string; text?: React.ReactNode; action?: React.ReactNode }) {
  return <EmptyState {...props} />;
}

export function ErrorState({ title = "Couldn't load this", text, onRetry, compact }:
  { title?: string; text?: React.ReactNode; onRetry?: () => void; compact?: boolean }) {
  return (
    <div role="alert" className={clsx("flex flex-col items-center px-6 text-center", compact ? "py-6" : "py-10")}>
      <div aria-hidden className="mb-3 grid h-9 w-9 place-items-center rounded-md border border-bad-line bg-bad-soft text-bad">
        <AlertTriangle {...IC} />
      </div>
      <div className="text-[13.5px] font-semibold text-ink">{title}</div>
      {text && <div className="mt-1 max-w-sm text-[13px] leading-relaxed text-ink-3">{text}</div>}
      {onRetry && <Button className="mt-4" size="sm" onClick={onRetry}><RotateCcw size={13} strokeWidth={1.75} /> Retry</Button>}
    </div>
  );
}

/** Inline banner for a message inside a page (error, warning, info). */
export function Notice({ tone = "info", title, children, action }:
  { tone?: Tone; title?: React.ReactNode; children?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div role={tone === "bad" ? "alert" : "status"} className={clsx("flex flex-wrap items-start gap-x-3 gap-y-1 rounded-md border px-3 py-2 text-[13px]", TONE_CLASS[tone])}>
      <div className="min-w-0 flex-1">
        {title && <div className="font-medium">{title}</div>}
        {children && <div className="text-ink-2">{children}</div>}
      </div>
      {action}
    </div>
  );
}

export function Skeleton({ className, w, h = 12 }: { className?: string; w?: number | string; h?: number | string }) {
  return <span aria-hidden className={clsx("skeleton block", className)} style={{ width: w, height: h }} />;
}
/** Placeholder rows for a list or table while it loads. */
export function SkeletonRows({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={clsx("flex flex-col", className)} role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-3 border-b border-line px-4 py-3 last:border-b-0">
          <Skeleton w={56} h={18} />
          <div className="flex-1 space-y-1.5"><Skeleton w={`${70 - i * 7}%`} /><Skeleton w={`${45 - i * 4}%`} h={10} /></div>
        </div>
      ))}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-[12.5px] text-ink-3" role="status">
      <Loader2 size={14} strokeWidth={2} className="spin text-kb" aria-hidden />{label || "Working"}
    </span>
  );
}

// ======================================================================= code, markdown

export function CodeCell({ code, label = "Cypher" }: { code: string; label?: string }) {
  return (
    <div className="flex overflow-hidden rounded-md border border-line bg-surface-2">
      <div className="mono shrink-0 border-r border-line px-2.5 py-2 text-[11px] text-ink-3">{label}</div>
      <pre className="mono scroll-thin max-h-60 flex-1 overflow-auto px-3 py-2 text-[12px] leading-relaxed text-ink-2">
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
