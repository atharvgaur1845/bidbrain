"use client";

import clsx from "clsx";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import type { GData } from "@/components/GraphCanvas";
import { Badge, Empty, Spinner, Verdict } from "@/components/ui";
import { fmtINR } from "@/lib/api";

export const AGENTS = [
  { key: "ClauseParser", desc: "Reads the tender into typed clauses", llm: true },
  { key: "EvidenceHunter", desc: "Qualifies each clause by graph traversal", llm: false },
  { key: "AbsenceAuditor", desc: "Finds missing letters, lapsing credentials, gaps", llm: false },
  { key: "ConflictSentinel", desc: "Checks the conflict history of every relied-on fact", llm: false },
  { key: "Verifier", desc: "Re-derives every verdict without an LLM", llm: false },
  { key: "Assembler", desc: "Writes the bid memo with citations", llm: true },
];

export type AgentStatus = "idle" | "running" | "done";

export type Finding = { kind: string; text: string; severity?: string; agent: string };
export type Clause = { clause_id: string; ref: string; kind: string; text: string; params?: any; verdict?: string; basis?: string;
  evidence?: any[]; subgraph?: GData; findings: Finding[] };

const humanize = (s: string) => (s || "").replace(/_/g, " ");

// ======================================================================= tender picker

export function TenderList({ tenders, pickedId, onPick }:
  { tenders: any[]; pickedId?: string; onPick: (t: any) => void }) {
  if (!tenders.length) return <div className="py-2"><Spinner label="Loading tenders" /></div>;
  return (
    <ul className="-mx-2 flex flex-col gap-1">
      {tenders.map((t) => {
        const active = pickedId === t.id;
        return (
          <li key={t.id}>
            <button type="button" onClick={() => onPick(t)} aria-pressed={active}
              className={clsx("relative flex w-full items-start gap-3 rounded-lg py-3 pl-4 pr-3 text-left transition-colors duration-150 ease-out",
                active ? "bg-kb-soft" : "hover:bg-well")}>
              <span aria-hidden className={clsx("absolute bottom-3 left-0 top-3 w-[2px] rounded-full", active ? "bg-kb" : "bg-transparent")} />
              <span className="min-w-0 flex-1">
                <span className="block text-[13.5px] font-semibold leading-snug text-ink">{t.title}</span>
                <span className="mt-0.5 block truncate text-[12.5px] text-ink-3">
                  {t.authority} · due <span className="num">{t.bid_date}</span>
                </span>
              </span>
              <span className={clsx("num shrink-0 pt-px text-[12.5px]", active ? "font-semibold text-kb-ink" : "text-ink-2")}>
                {fmtINR(t.estimated_cost)}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

// ======================================================================= agent stepper

function StatusDot({ st }: { st: AgentStatus }) {
  return (
    <span className="grid h-4 w-4 place-items-center">
      <span className={clsx("h-2 w-2 rounded-full",
        st === "done" && "bg-ok",
        st === "running" && "pulse-dot bg-kb shadow-[0_0_0_4px_var(--kb-soft)]",
        st === "idle" && "bg-line-strong")} />
    </span>
  );
}

export function AgentPipeline({ status, summaries }:
  { status: Record<string, AgentStatus>; summaries: Record<string, string> }) {
  return (
    <>
      <ol className="flex flex-col">
        {AGENTS.map((a, i) => {
          const st = status[a.key] || "idle";
          const last = i === AGENTS.length - 1;
          const summary = summaries[a.key] || a.desc;
          return (
            <li key={a.key} className="relative flex gap-3 pb-4 last:pb-0" aria-current={st === "running" ? "step" : undefined}>
              {!last && <span aria-hidden className="absolute bottom-0 left-[7.5px] top-5 w-px bg-line" />}
              <span className="pt-[3px]"><StatusDot st={st} /></span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2">
                  <span className={clsx("text-[13.5px] font-semibold", st === "idle" ? "text-ink-2" : "text-ink")}>{a.key}</span>
                  <Badge tone="neutral">{a.llm ? "LLM" : "Graph"}</Badge>
                  <span className="sr-only">{st}</span>
                </span>
                <span className={clsx("block truncate text-[12.5px]", summaries[a.key] ? "text-ink-2" : "text-ink-3")} title={summary}>
                  {summary}
                </span>
              </span>
              {!last && <span className="sr-only">hands off to {AGENTS[i + 1].key}</span>}
            </li>
          );
        })}
      </ol>
      <p className="mt-4 border-t border-line pt-3 text-[12.5px] leading-relaxed text-ink-3">
        Each handoff is a graph write:{" "}
        <span className="num text-[12px] text-ink-2">(:AgentRun)-[:HANDED_OFF_TO]-&gt;(:AgentRun)</span>
      </p>
    </>
  );
}

// ======================================================================= verdict table

const SEVERITY_BORDER = (sev?: string) => (sev === "high" ? "border-bad" : sev === "info" ? "border-kb" : "border-warn");

function FindingNote({ f }: { f: Finding }) {
  return (
    <div className={clsx("mt-2 rounded-r-md border-l-2 bg-well px-3 py-2 text-[12.5px] leading-snug text-ink-2", SEVERITY_BORDER(f.severity))}>
      <span className="font-medium text-ink">{f.agent}</span>
      <span className="text-ink-3"> · {humanize(f.kind)}</span>
      <span className="block">{f.text}</span>
    </div>
  );
}

export function VerdictTable({ clauses, focus, onFocus, running }:
  { clauses: Clause[]; focus: string | null; onFocus: (id: string) => void; running: boolean }) {
  if (!clauses.length) {
    return running
      ? <div className="grid place-items-center px-6 py-12"><Spinner label="ClauseParser is reading the tender" /></div>
      : <Empty title="No clauses yet" text="Pick a tender and run the agents. Clauses appear here as they are parsed." />;
  }
  return (
    <div className="scroll-thin contain-scroll overflow-x-auto">
      <table className="kt table-fixed">
        <thead>
          <tr>
            <th className="w-[76px] pl-5!">Ref</th>
            <th>Requirement</th>
            <th className="hidden w-[136px] md:table-cell">Type</th>
            <th className="w-[124px] pr-5! text-right!">Verdict</th>
          </tr>
        </thead>
        <tbody>
          {clauses.map((c) => {
            const active = focus === c.clause_id;
            return (
              <tr key={c.clause_id} tabIndex={0} aria-current={active || undefined}
                onClick={() => onFocus(c.clause_id)}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onFocus(c.clause_id); } }}
                className={clsx("cursor-pointer outline-none focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-kb",
                  active && "[&>td]:bg-kb-soft [&>td:first-child]:shadow-[inset_2px_0_0_var(--kb)]")}>
                <td className="num pl-5! align-top! text-[12.5px] text-ink-2">{c.ref}</td>
                <td className="align-top!">
                  <div className={clsx("text-[13.5px] text-ink", active ? "whitespace-normal" : "truncate")} title={c.text}>{c.text}</div>
                  {c.basis && <div className="num mt-1 text-[12px] leading-snug text-ink-3">{c.basis}</div>}
                  {c.findings.map((f, k) => <FindingNote key={k} f={f} />)}
                </td>
                <td className="hidden align-top! text-[12.5px] text-ink-2 md:table-cell">
                  <span className="block truncate" title={humanize(c.kind)}>{humanize(c.kind)}</span>
                </td>
                <td className="pr-5! text-right! align-top!"><Verdict v={c.verdict} size="sm" /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ======================================================================= memo

export function VerifierBanner({ verified }: { verified: any }) {
  const ok = !!verified.ok;
  return (
    <div role="status" className={clsx("flex items-start gap-2 rounded-[10px] px-4 py-3 text-[13.5px] leading-snug",
      ok ? "bg-ok-soft" : "bg-bad-soft")}>
      {ok
        ? <ShieldCheck size={16} strokeWidth={1.75} className="mt-px shrink-0 text-ok" />
        : <ShieldAlert size={16} strokeWidth={1.75} className="mt-px shrink-0 text-bad" />}
      <div className="text-ink-2">
        <span className="font-semibold text-ink">Verifier, no LLM. </span>
        {ok
          ? <>Re-derived all <span className="num font-semibold text-ink">{verified.checked}</span> verdicts from the graph. Every one agrees.</>
          : <><span className="num font-semibold text-bad">{verified.mismatches.length}</span> verdicts disagree. The case is flagged.</>}
      </div>
    </div>
  );
}
