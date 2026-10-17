"use client";

import clsx from "clsx";
import { ShieldAlert, ShieldCheck, Building2, Droplets, Route, ScrollText, Waypoints } from "lucide-react";
import type { GData } from "@/components/GraphCanvas";
import { Badge, Empty, Spinner, Verdict, IconTile, type TileTone } from "@/components/ui";
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

const TENDER_TILE: [RegExp, TileTone, typeof Route][] = [
  [/expressway|road|highway/i, "peach", Route], [/water|sewer|drain|irrigation/i, "teal", Droplets],
  [/bridge|flyover/i, "lavender", Waypoints], [/building|quarters|complex/i, "sand", Building2],
];
function tenderTile(t: any): [TileTone, React.ReactNode] {
  const hit = TENDER_TILE.find(([rx]) => rx.test(`${t.category || ""} ${t.title || ""}`));
  const [tone, Icon] = hit ? [hit[1], hit[2]] : (["sage", ScrollText] as const);
  return [tone, <Icon key="i" size={16} strokeWidth={1.75} />];
}

export function TenderList({ tenders, pickedId, onPick }:
  { tenders: any[]; pickedId?: string; onPick: (t: any) => void }) {
  if (!tenders.length) return <div className="py-2"><Spinner label="Loading tenders" /></div>;
  return (
    <ul className="flex flex-col gap-1.5">
      {tenders.map((t) => {
        const active = pickedId === t.id;
        const [tone, icon] = tenderTile(t);
        return (
          <li key={t.id}>
            <button type="button" onClick={() => onPick(t)} aria-pressed={active}
              className={clsx("flex w-full items-start gap-3 rounded-[10px] p-2.5 text-left transition-[background-color,box-shadow] duration-150 ease-out",
                active ? "bg-surface-solid shadow-[0_0_0_1.5px_var(--kb),var(--shadow-border)]" : "hover:bg-well")}>
              <IconTile tone={tone} size={32}>{icon}</IconTile>
              <span className="min-w-0 flex-1">
                <span className="block text-[13px] font-medium leading-snug text-ink">{t.title}</span>
                <span className="mt-0.5 block truncate text-[12px] text-ink-3">
                  {t.authority} · due <span className="num">{t.bid_date}</span>
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

// ======================================================================= agent stepper

function StatusDot({ st }: { st: AgentStatus }) {
  return (
    <span className="grid h-4 w-4 place-items-center">
      <span className={clsx("h-2 w-2 rounded-full",
        st === "done" && "bg-ok",
        st === "running" && "pulse-dot bg-kb shadow-[0_0_0_4px_var(--kb-soft)]",
        st === "idle" && "bg-line-strong",
        st === "skipped" && "border border-line-strong bg-transparent")} />
    </span>
  );
}

export function AgentPipeline({ status, summaries, plan, handoffs = [] }:
  { status: Record<string, AgentStatus>; summaries: Record<string, string>; plan?: Plan | null; handoffs?: Handoff[] }) {
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
                  <span className={clsx("text-[13.5px] font-semibold", st === "idle" || st === "skipped" ? "text-ink-2" : "text-ink")}>{a.key}</span>
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
      {plan && (
        <p className="mt-4 border-t border-line pt-3 text-[12.5px] leading-relaxed text-ink-3">
          <span className="font-medium text-ink-2">Plan: </span>
          {plan.agents.join(" → ")}
          {Object.entries(plan.skip).map(([a, why]) => <span key={a} className="block">Skipped {a}: {why}</span>)}
        </p>
      )}
      {handoffs.length > 0 && (
        <ul className="mt-3 flex flex-col gap-1.5 text-[12.5px] leading-snug text-ink-2">
          {handoffs.map((h, i) => (
            <li key={i} className="rounded-r-md border-l-2 border-kb bg-well px-3 py-2">
              <span className="font-medium text-ink">{h.from} → {h.to}</span>
              <span className="text-ink-3"> · clause {h.clauses.join(", ")}</span>
              <span className="block">{h.reason}</span>
            </li>
          ))}
        </ul>
      )}
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
                  {c.corrected && (
                    <div className="mt-2 rounded-r-md border-l-2 border-warn bg-well px-3 py-2 text-[12.5px] leading-snug text-ink-2">
                      <span className="font-medium text-ink">ClauseParser</span>
                      <span className="text-ink-3"> · corrected</span>
                      <span className="block">{c.corrected}</span>
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
    <div role="status" className={clsx("flex items-start gap-2 rounded-[10px] px-4 py-3 text-[13.5px] leading-snug",
      ok ? "bg-ok-soft" : "bg-bad-soft")}>
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
