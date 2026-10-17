"use client";

import clsx from "clsx";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import type { GData } from "@/components/GraphCanvas";
import { Empty, Spinner, Verdict } from "@/components/ui";
import { fmtINR } from "@/lib/api";

export const AGENTS = [
  { key: "ClauseParser", desc: "Reads the tender into typed clauses", llm: true },
  { key: "Planner", desc: "Decides which agents run on which clauses", llm: false },
  { key: "EvidenceHunter", desc: "Qualifies each clause by graph traversal", llm: false },
  { key: "AbsenceAuditor", desc: "Finds missing letters, lapsing credentials, gaps", llm: false },
  { key: "ConflictSentinel", desc: "Checks the conflict history of every relied-on fact", llm: false },
  { key: "Verifier", desc: "Re-reads clause text, recomputes from claims, re-derives", llm: false },
  { key: "Assembler", desc: "Writes the bid memo with citations", llm: true },
];

export type AgentStatus = "idle" | "running" | "done" | "skipped";
export type Plan = { agents: string[]; skip: Record<string, string>; routes: Record<string, string[]>; stage: string };
export type Handoff = { from: string; to: string; reason: string; clauses: string[] };

export type Finding = { kind: string; text: string; severity?: string; agent: string };
export type Clause = { clause_id: string; ref: string; kind: string; text: string; params?: any; verdict?: string; basis?: string;
  corrected?: string; evidence?: any[]; subgraph?: GData; findings: Finding[] };

const humanize = (s: string) => (s || "").replace(/_/g, " ");

// ======================================================================= tender picker

export function TenderList({ tenders, pickedId, onPick }:
  { tenders: any[]; pickedId?: string; onPick: (t: any) => void }) {
  if (!tenders.length) return <div className="py-2"><Spinner label="Loading tenders" /></div>;
  return (
    <ul className="-mx-4 -mt-4 divide-y divide-line border-b border-line">
      {tenders.map((t) => {
        const active = pickedId === t.id;
        return (
          <li key={t.id}>
            <button type="button" onClick={() => onPick(t)} aria-pressed={active}
              className={clsx("flex w-full items-start gap-3 px-4 py-2.5 text-left transition-colors duration-100 ease-out focus-visible:outline-offset-[-2px]",
                active ? "bg-kb-soft shadow-[inset_2px_0_0_var(--kb)]" : "hover:bg-surface-2")}>
              <span className="min-w-0 flex-1">
                <span className="block text-[13px] font-medium leading-snug text-ink">{t.title}</span>
                <span className="mt-0.5 block truncate text-[12px] text-ink-3">
                  <span className="mono">{t.id}</span> · {t.authority} · due <span className="num">{t.bid_date}</span>
                </span>
              </span>
              <span className="num shrink-0 pt-px text-[12px] text-ink-2">{fmtINR(t.estimated_cost)}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

// ======================================================================= agent pipeline (dense)

function StatusDot({ st }: { st: AgentStatus }) {
  return (
    <span className="grid h-4 w-4 shrink-0 place-items-center" aria-hidden>
      <span className={clsx("h-2 w-2 rounded-full",
        st === "done" && "bg-ok",
        st === "running" && "pulse-dot bg-kb",
        st === "idle" && "bg-line-strong",
        st === "skipped" && "border border-line-strong bg-transparent")} />
    </span>
  );
}

export function AgentPipeline({ status, summaries, plan, handoffs = [], verified }:
  { status: Record<string, AgentStatus>; summaries: Record<string, string>; plan?: Plan | null; handoffs?: Handoff[]; verified?: any }) {
  return (
    <div className="-mx-4 -my-4">
      <table className="kt dense table-fixed">
        <thead><tr><th className="w-[132px] pl-4!">Agent</th><th>Output</th><th className="w-[52px] pr-4! text-right!">Uses</th></tr></thead>
        <tbody>
          {AGENTS.map((a) => {
            const st = status[a.key] || "idle";
            return (
              <tr key={a.key} aria-current={st === "running" ? "step" : undefined}>
                <td className="pl-4!">
                  <span className="flex items-center gap-1.5">
                    <StatusDot st={st} />
                    <span className={clsx("truncate text-[12.5px] font-medium", st === "idle" || st === "skipped" ? "text-ink-3" : "text-ink")}>{a.key}</span>
                    <span className="sr-only">{st}</span>
                  </span>
                </td>
                <td><span className={clsx("block truncate text-[12.5px]", summaries[a.key] ? "text-ink-2" : "text-ink-3")} title={summaries[a.key] || a.desc}>
                  {st === "skipped" ? `skipped: ${plan?.skip?.[a.key] || ""}` : summaries[a.key] || a.desc}</span></td>
                <td className="pr-4! text-right! text-[11.5px] text-ink-3">{a.llm ? "LLM" : "graph"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {plan && (
        <div className="border-t border-line px-4 py-3">
          <h3 className="text-[12px] font-medium text-ink-3">Plan <span className="font-normal">({plan.stage})</span></h3>
          <dl className="mt-1 grid grid-cols-[112px_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-[12px]">
            {Object.entries(plan.routes || {}).map(([agent, cl]) => (
              <div key={agent} className="contents">
                <dt className="truncate text-ink-2">{agent}</dt>
                <dd className="num truncate text-ink-3" title={cl.join(", ")}>{cl.join(", ")}</dd>
              </div>
            ))}
            {Object.entries(plan.skip || {}).map(([agent, why]) => (
              <div key={agent} className="contents">
                <dt className="truncate text-ink-3">{agent}</dt><dd className="text-ink-3">skipped: {why}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {handoffs.length > 0 && (
        <div className="border-t border-line px-4 py-3">
          <h3 className="text-[12px] font-medium text-ink-3">Hand-backs <span className="num font-normal">{handoffs.length}</span></h3>
          <ul className="mt-1 flex flex-col gap-1.5">
            {handoffs.map((h, i) => (
              <li key={i} className="text-[12px] leading-snug">
                <span className="font-medium text-ink">{h.from} → {h.to}</span>
                <span className="num text-ink-3"> · {h.clauses.join(", ")}</span>
                <span className="block text-ink-2">{h.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {verified && (
        <div className="flex items-start gap-2 border-t border-line px-4 py-3 text-[12.5px]">
          {verified.ok ? <ShieldCheck size={14} strokeWidth={1.75} className="mt-px shrink-0 text-ok" aria-hidden />
            : <ShieldAlert size={14} strokeWidth={1.75} className="mt-px shrink-0 text-bad" aria-hidden />}
          <div className="min-w-0">
            <span className="font-medium text-ink">Verifier: </span>
            <span className="text-ink-2">
              {verified.ok ? <><span className="num">{verified.checked}</span> verdicts re-derived, all agree{verified.retried?.length ? <> (<span className="num">{verified.retried.length}</span> after a retry)</> : null}</>
                : <><span className="num text-bad">{verified.mismatches.length}</span> disagree; case flagged for review</>}
            </span>
            {!verified.ok && (
              <ul className="mt-1 text-ink-2">
                {verified.mismatches.map((m: any, i: number) => <li key={i}><span className="num">{m.clause}</span>: {m.note || (m.reasons || []).join("; ")}</li>)}
              </ul>
            )}
          </div>
        </div>
      )}

      <p className="border-t border-line px-4 py-2 text-[12px] text-ink-3">
        Each hand-off is a graph write: <span className="mono text-[11.5px] text-ink-2">(:AgentRun)-[:HANDED_OFF_TO]-&gt;(:AgentRun)</span>
      </p>
    </div>
  );
}

// ======================================================================= verdict table

const SEVERITY_BORDER = (sev?: string) => (sev === "high" ? "border-bad" : sev === "info" ? "border-kb" : "border-warn");

function FindingNote({ f }: { f: Finding }) {
  return (
    <div className={clsx("mt-1.5 border-l-2 pl-2.5 text-[12px] leading-snug text-ink-2", SEVERITY_BORDER(f.severity))}>
      <span className="font-medium text-ink">{f.agent || "Finding"}</span>
      <span className="text-ink-3"> · {humanize(f.kind)}: </span>{f.text}
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
                  {c.corrected && (
                    <div className="mt-1.5 border-l-2 border-warn pl-2.5 text-[12px] leading-snug text-ink-2">
                      <span className="font-medium text-ink">ClauseParser corrected: </span>{c.corrected}
                    </div>
                  )}
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
    <div role="status" className={clsx("flex items-start gap-2 rounded-md border px-3 py-2 text-[13px] leading-snug",
      ok ? "border-ok-line bg-ok-soft" : "border-bad-line bg-bad-soft")}>
      {ok
        ? <ShieldCheck size={16} strokeWidth={1.75} className="mt-px shrink-0 text-ok" />
        : <ShieldAlert size={16} strokeWidth={1.75} className="mt-px shrink-0 text-bad" />}
      <div className="text-ink-2">
        <span className="font-semibold text-ink">Verifier, no LLM. </span>
        {ok
          ? <>Checked all <span className="num font-semibold text-ink">{verified.checked}</span> verdicts three ways (clause text re-read by the rule parser, credited values recomputed from the winning claims, verdict re-derived from the graph). Every one agrees
              {verified.retried?.length ? <> after the EvidenceHunter re-derived <span className="num font-semibold text-ink">{verified.retried.length}</span> that first disagreed</> : null}.</>
          : <><span className="num font-semibold text-bad">{verified.mismatches.length}</span> verdicts disagree after one re-derivation. The case is flagged.
              <ul className="mt-1 list-disc pl-4 text-[12.5px]">
                {verified.mismatches.map((m: any, i: number) => (
                  <li key={i}>Clause <span className="num">{m.clause}</span>: {m.note || (m.reasons || []).join("; ")}</li>
                ))}
              </ul></>}
      </div>
    </div>
  );
}
