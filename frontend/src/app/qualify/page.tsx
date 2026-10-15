"use client";

import clsx from "clsx";
import { CheckCircle2, Circle, FileSearch, Gavel, Loader2, PenLine, Scale, ShieldAlert, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import GraphCanvas, { GData, mergeGraph } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { EvidenceList, Markdown, Panel, Verdict } from "@/components/ui";
import { api, fmtINR, stream } from "@/lib/api";

const AGENTS = [
  { key: "ClauseParser", icon: PenLine, desc: "reads the tender into typed clauses", llm: true },
  { key: "EvidenceHunter", icon: FileSearch, desc: "qualifies each clause by graph traversal", llm: false },
  { key: "AbsenceAuditor", icon: Scale, desc: "proves missing letters, lapsing credentials, gaps", llm: false },
  { key: "ConflictSentinel", icon: ShieldAlert, desc: "checks every relied-on fact's conflict history", llm: false },
  { key: "Verifier", icon: ShieldCheck, desc: "re-derives every verdict — no LLM", llm: false },
  { key: "Assembler", icon: Gavel, desc: "writes the bid memo with citations", llm: true },
];

type Clause = { clause_id: string; ref: string; kind: string; text: string; params?: any; verdict?: string; basis?: string;
  evidence?: any[]; subgraph?: GData; findings: { kind: string; text: string; severity?: string; agent: string }[] };

export default function QualifyPage() {
  const [tenders, setTenders] = useState<any[]>([]);
  const [pick, setPick] = useState<any>(null);
  const [text, setText] = useState("");
  const [status, setStatus] = useState<Record<string, "idle" | "running" | "done">>({});
  const [summaries, setSummaries] = useState<Record<string, string>>({});
  const [clauses, setClauses] = useState<Clause[]>([]);
  const [graph, setGraph] = useState<GData>({ nodes: [], edges: [] });
  const [focus, setFocus] = useState<string | null>(null);
  const [memo, setMemo] = useState<{ markdown: string; overall: string; method: string } | null>(null);
  const [verified, setVerified] = useState<any>(null);
  const [running, setRunning] = useState(false);
  const [ms, setMs] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { api("/api/tenders").then((t) => { setTenders(t); setPick(t[0]); setText(t[0]?.text || ""); }); }, []);

  async function run() {
    setRunning(true); setClauses([]); setGraph({ nodes: [], edges: [] }); setMemo(null); setVerified(null); setErr(null);
    setStatus({}); setSummaries({}); setFocus(null); setMs(null);
    const body = pick && text === pick.text ? { tender_id: pick.id } : { tender_id: pick?.id, text };
    await stream("/api/qualify", body, (ev) => {
      if (ev.type === "agent_start") setStatus((s) => ({ ...s, [ev.agent]: "running" }));
      if (ev.type === "agent_done") {
        setStatus((s) => ({ ...s, [ev.agent]: "done" }));
        if (ev.summary) setSummaries((s) => ({ ...s, [ev.agent]: ev.summary }));
      }
      if (ev.type === "clause") setClauses((cs) => [...cs, { clause_id: ev.clause_id, ref: ev.ref, kind: ev.kind, text: ev.text, params: ev.params, findings: [] }]);
      if (ev.type === "verdict") {
        setClauses((cs) => cs.map((c) => (c.clause_id === ev.clause_id ? { ...c, verdict: ev.verdict, basis: ev.basis, evidence: ev.evidence, subgraph: ev.subgraph } : c)));
        if (ev.subgraph) setGraph((g) => mergeGraph(g, ev.subgraph));
      }
      if (ev.type === "finding") {
        setClauses((cs) => cs.map((c) => (c.clause_id === ev.clause_id ? { ...c, findings: [...c.findings, { kind: ev.kind, text: ev.text, severity: ev.severity, agent: ev.agent }] } : c)));
        if (ev.subgraph) setGraph((g) => mergeGraph(g, ev.subgraph));
      }
      if (ev.type === "verified") { setVerified(ev); setStatus((s) => ({ ...s, Verifier: "done" })); setSummaries((s) => ({ ...s, Verifier: ev.ok ? `${ev.checked} verdicts re-derived, all agree` : `${ev.mismatches.length} mismatches!` })); }
      if (ev.type === "memo") { setMemo({ markdown: ev.markdown, overall: ev.overall, method: ev.method }); setStatus((s) => ({ ...s, Assembler: "done" })); setSummaries((s) => ({ ...s, Assembler: `memo (${ev.method})` })); }
      if (ev.type === "done") setMs(ev.ms);
      if (ev.type === "error") setErr(ev.message);
    });
    setRunning(false);
  }

  const focused = clauses.find((c) => c.clause_id === focus);
  const hl = new Set((focused?.subgraph?.nodes || []).map((n) => n.id));

  return (
    <div>
      <PageHeader eyebrow="Agents that act on connected data" title="Qualify a tender, clause by clause">
        Six agents share one case graph in FalkorDB. Each writes its conclusions as nodes and edges the next one reads, so
        the whole decision trail stays queryable, and a later document that changes a relied-on fact ripples back to this verdict.
      </PageHeader>

      <div className="grid gap-5 xl:grid-cols-12">
        <div className="space-y-4 xl:col-span-4">
          <Panel title="1 · Pick a tender">
            <div className="grid gap-2">
              {tenders.map((t) => (
                <button key={t.id} onClick={() => { setPick(t); setText(t.text); }}
                  className={clsx("panel-2 px-3 py-2.5 text-left transition-colors", pick?.id === t.id && "!border-accent")}>
                  <div className="flex items-center gap-2">
                    <span className="num text-[11px] text-ink-3">{t.id}</span>
                    <span className="num ml-auto text-[11.5px] text-ink-2">{fmtINR(t.estimated_cost)}</span>
                  </div>
                  <div className="mt-0.5 text-[13.5px] font-semibold leading-snug">{t.title}</div>
                  <div className="text-[12px] text-ink-3">{t.authority} · bid due {t.bid_date}</div>
                </button>
              ))}
            </div>
            <div className="mt-3">
              <div className="eyebrow mb-1.5">Eligibility text (editable)</div>
              <textarea className="input num scroll-thin h-40 resize-y !text-[12px] leading-relaxed" value={text} onChange={(e) => setText(e.target.value)} />
            </div>
            <button className="btn btn-primary mt-3 w-full justify-center" onClick={run} disabled={running || !text.trim()}>
              {running ? <Loader2 size={15} className="animate-spin" /> : <Gavel size={15} />} {running ? "Agents working…" : "Qualify this tender"}
            </button>
          </Panel>

          <Panel title="2 · The agent team" right={ms ? <span className="num text-[11.5px] text-ink-3">{ms.toFixed(0)} ms total</span> : null}>
            <ol className="space-y-1">
              {AGENTS.map((a, i) => {
                const st = status[a.key] || "idle";
                const Icon = a.icon;
                return (
                  <li key={a.key} className="flex items-start gap-3 rounded-lg px-2 py-2" style={st === "running" ? { background: "var(--bg-2)" } : undefined}>
                    <span className="mt-0.5">
                      {st === "done" ? <CheckCircle2 size={17} style={{ color: "var(--pass)" }} /> : st === "running" ? <Loader2 size={17} className="animate-spin text-accent" /> : <Circle size={17} className="text-ink-3" />}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2">
                        <Icon size={13} className="text-ink-3" />
                        <span className="text-[13.5px] font-semibold">{a.key}</span>
                        {a.llm ? <span className="chip !py-0 !text-[10px]">LLM</span> : <span className="chip !py-0 !text-[10px]">graph</span>}
                      </span>
                      <span className="block text-[12px] text-ink-3">{summaries[a.key] || a.desc}</span>
                    </span>
                    {i < AGENTS.length - 1 && <span className="sr-only">hands off to {AGENTS[i + 1].key}</span>}
                  </li>
                );
              })}
            </ol>
            <div className="mt-2 text-[11.5px] text-ink-3">Handoffs are graph writes: <span className="num">(:AgentRun)-[:HANDED_OFF_TO]-&gt;(:AgentRun)</span></div>
          </Panel>
        </div>

        <div className="space-y-4 xl:col-span-8">
          {err && <div className="panel px-4 py-3 text-[13px]" style={{ color: "var(--fail)" }}>{err}</div>}
          <Panel title="3 · Clause verdicts" right={memo && <Verdict v={memo.overall} size="lg" />} pad={false}>
            {clauses.length === 0 ? (
              <div className="p-6 text-[13px] text-ink-3">Clauses appear here as the ClauseParser reads the tender.</div>
            ) : (
              <div className="divide-y" style={{ borderColor: "var(--line)" }}>
                {clauses.map((c) => (
                  <button key={c.clause_id} onClick={() => setFocus(c.clause_id === focus ? null : c.clause_id)}
                    className={clsx("block w-full px-4 py-3 text-left transition-colors hover:bg-bg-2", focus === c.clause_id && "bg-bg-2")}>
                    <div className="flex items-start gap-3">
                      <span className="num w-14 shrink-0 pt-0.5 text-[12px] text-ink-3">{c.ref}</span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2">
                          <span className="chip !py-0">{c.kind.replace("_", " ")}</span>
                          <span className="truncate text-[13px] text-ink">{c.text}</span>
                        </span>
                        {c.basis && <span className="num mt-1 block text-[12px] text-ink-2">{c.basis}</span>}
                        {c.findings.map((f, k) => (
                          <span key={k} className="mt-1.5 block rounded-md border-l-2 bg-bg-2 px-2 py-1 text-[12px] leading-snug text-ink-2"
                            style={{ borderColor: f.severity === "high" ? "var(--fail)" : f.severity === "info" ? "var(--info)" : "var(--risk)" }}>
                            <span className="font-semibold text-ink">{f.agent} · {f.kind}</span> — {f.text}
                          </span>
                        ))}
                      </span>
                      <Verdict v={c.verdict} />
                    </div>
                  </button>
                ))}
              </div>
            )}
          </Panel>

          <div className="grid gap-4 lg:grid-cols-5">
            <Panel title={focused ? `Evidence · clause ${focused.ref}` : "Evidence graph"} className="lg:col-span-3" pad={false}>
              <div className="p-2"><GraphCanvas data={graph} highlight={hl} height={380} emptyHint="Works, people and statements the agents rely on." /></div>
            </Panel>
            <Panel title="Documents" className="lg:col-span-2">
              <div className="scroll-thin max-h-[380px] overflow-y-auto">
                <EvidenceList items={focused ? focused.evidence || [] : clauses.flatMap((c) => c.evidence || [])} />
              </div>
            </Panel>
          </div>

          {verified && (
            <div className="panel flex items-center gap-3 px-4 py-3">
              <ShieldCheck size={18} style={{ color: verified.ok ? "var(--pass)" : "var(--fail)" }} />
              <div className="text-[13px]">
                <span className="font-semibold">Verifier (no LLM): </span>
                {verified.ok ? `re-derived all ${verified.checked} verdicts from the graph — every one agrees.` : `${verified.mismatches.length} verdicts disagree — case flagged.`}
              </div>
            </div>
          )}
          {memo && (
            <Panel title={`Bid memo · ${memo.method === "llm" ? "written by the Assembler agent" : "assembled from the verified record"}`}>
              <Markdown>{memo.markdown}</Markdown>
            </Panel>
          )}
        </div>
      </div>
    </div>
  );
}
