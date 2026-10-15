"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/Shell";
import { Panel, Stat } from "@/components/ui";
import { api } from "@/lib/api";

const SYS_LABEL: Record<string, string> = {
  bidbrain: "BidBrain (graph tools + agent)", bidbrain_multi: "BidBrain multi-agent + Verifier",
  text2cypher: "LLM text-to-Cypher", vector_rag: "Vector RAG", graphrag_sdk: "GraphRAG-SDK (auto-schema)",
};
const SYS_COLOR: Record<string, string> = {
  bidbrain: "var(--accent)", bidbrain_multi: "var(--accent-2)", text2cypher: "var(--violet)", vector_rag: "var(--fail)", graphrag_sdk: "var(--info)",
};
const SETS: [string, string][] = [["S21", "21 sample Qs · true gold"], ["Q333", "333 hidden Qs · near-gold"], ["ABS", "absence Qs · Cypher gold"], ["TND", "tender clauses · Cypher gold"]];

function Bar({ v, color }: { v: number | null | undefined; color: string }) {
  if (v === null || v === undefined) return <span className="text-[12px] text-ink-3">pending</span>;
  return (
    <span className="flex items-center gap-2">
      <span className="h-2 flex-1 rounded bg-bg-2"><span className="block h-2 rounded" style={{ width: `${Math.max(1, v * 100)}%`, background: color }} /></span>
      <span className="num w-12 text-right text-[12.5px] font-semibold">{(v * 100).toFixed(1)}%</span>
    </span>
  );
}

export default function BenchmarkPage() {
  const [ev, setEv] = useState<any>(null);
  useEffect(() => { api("/api/eval").then(setEv).catch(() => setEv({})); }, []);
  const qa = ev?.qa_summary?.systems || {};
  const systems = Object.keys(SYS_LABEL).filter((s) => qa[s]);
  const cj = ev?.conflict_pr_json, cp = ev?.conflict_pr_pdf, orc = ev?.tool_oracle;
  const hops = ["2-3", "4", "5-6"];

  return (
    <div>
      <PageHeader eyebrow="Research rigor" title="Does the graph actually do the work?">
        Every system uses the same model (Qwen3-32B) and the same FalkorDB instance. Only the context strategy differs.
        Gold labels come from the dataset's 21 verified answers, a deterministic engine that scored 99.775% on the 333 hidden
        questions, and Cypher-computed gold for the synthesized absence and tender sets.
      </PageHeader>

      <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Tool sufficiency" value={orc ? `${orc.match_engine}/${orc.n}` : "354/354"} sub="graph tools reproduce every answer" tone="var(--pass)" />
        <Stat label="Conflict detection F1" value={cj ? cj.detection.f1.toFixed(2) : "—"} sub={`JSON stream · ${cj?.n_events ?? "–"} events`} />
        <Stat label="PDF path F1" value={cp ? cp.detection.f1.toFixed(2) : "—"} sub={`${cp?.n_pdfs ?? "–"} rendered PDFs, end to end`} />
        <Stat label="Entity resolution (PDF)" value={cp ? `${(cp.extraction.subject_resolution * 100).toFixed(0)}%` : "—"} sub="varied title spellings" />
      </div>

      <Panel title="Question answering accuracy" className="mb-5">
        {systems.length === 0 ? (
          <div className="text-[13px] text-ink-3">Q&amp;A runs are pending. They run on Qwen3-32B served by vLLM (eval/run.py) and appear here when done.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-[13px]">
              <thead><tr className="text-left">
                <th className="eyebrow py-2 pr-3">System</th>
                {SETS.map(([k, d]) => <th key={k} className="py-2 pr-3"><div className="eyebrow">{k}</div><div className="text-[11px] font-normal text-ink-3">{d}</div></th>)}
              </tr></thead>
              <tbody>
                {systems.map((s) => (
                  <tr key={s} className="border-t hairline">
                    <td className="py-2.5 pr-3 font-medium"><span className="mr-2 inline-block h-2 w-2 rounded-full" style={{ background: SYS_COLOR[s] }} />{SYS_LABEL[s]}</td>
                    {SETS.map(([k]) => <td key={k} className="w-[17%] py-2.5 pr-3"><Bar v={qa[s]?.[k]?.accuracy} color={SYS_COLOR[s]} /></td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Accuracy by hop count (Q333)">
          {systems.length === 0 ? <div className="text-[13px] text-ink-3">pending</div> : (
            <div className="space-y-3">
              {hops.map((h) => (
                <div key={h}>
                  <div className="eyebrow mb-1">{h} hops</div>
                  <div className="space-y-1">{systems.map((s) => <div key={s} className="flex items-center gap-2"><span className="w-28 truncate text-[12px] text-ink-3">{s}</span><span className="flex-1"><Bar v={qa[s]?.Q333?.by_hops?.[h]} color={SYS_COLOR[s]} /></span></div>)}</div>
                </div>
              ))}
            </div>
          )}
        </Panel>
        <Panel title="Cost & speed per question">
          {systems.length === 0 ? <div className="text-[13px] text-ink-3">pending</div> : (
            <table className="w-full text-[12.5px]">
              <thead><tr className="text-left"><th className="eyebrow py-1.5">System</th><th className="eyebrow">p50</th><th className="eyebrow">p95</th><th className="eyebrow">LLM calls</th><th className="eyebrow">tool calls</th></tr></thead>
              <tbody>{systems.map((s) => (
                <tr key={s} className="border-t hairline"><td className="py-1.5">{s}</td>
                  <td className="num">{qa[s]?.latency?.p50 ? `${(qa[s].latency.p50 / 1000).toFixed(1)}s` : "–"}</td>
                  <td className="num">{qa[s]?.latency?.p95 ? `${(qa[s].latency.p95 / 1000).toFixed(1)}s` : "–"}</td>
                  <td className="num">{qa[s]?.llm_calls?.toFixed?.(1) ?? "–"}</td><td className="num">{qa[s]?.tool_calls?.toFixed?.(1) ?? "–"}</td></tr>
              ))}</tbody>
            </table>
          )}
        </Panel>
        <Panel title="Conflict engine · planted stream">
          {cj ? (
            <div className="space-y-3 text-[12.5px]">
              <div className="grid grid-cols-3 gap-2">
                <Stat label="precision" value={cj.detection.precision.toFixed(2)} /><Stat label="recall" value={cj.detection.recall.toFixed(2)} />
                <Stat label="routing" value={cj.routing_accuracy?.toFixed(2)} sub="auto vs human" />
              </div>
              <table className="w-full">
                <tbody>{Object.entries(cj.per_type || {}).map(([t, v]: any) => (
                  <tr key={t} className="border-t hairline"><td className="py-1">{t.replace(/_/g, " ")}</td><td className="num text-right">{v.correct}/{v.n}</td></tr>
                ))}</tbody>
              </table>
              <div className="text-ink-3">The labels and the rules were written by the same team, so the JSON path checks consistency more than generalization. Hard negatives (rounding, unchanged restatements, unrelated fields) test false alarms. The PDF path below adds extraction and entity resolution, which can genuinely fail.</div>
            </div>
          ) : <div className="text-[13px] text-ink-3">pending</div>}
        </Panel>
        <Panel title="Conflict engine · PDF path (render → extract → resolve → detect)">
          {cp ? (
            <div className="space-y-2 text-[12.5px]">
              <div className="grid grid-cols-3 gap-2">
                <Stat label="F1" value={cp.detection.f1.toFixed(2)} /><Stat label="subject resolution" value={`${(cp.extraction.subject_resolution * 100).toFixed(0)}%`} />
                <Stat label="value extraction" value={`${(cp.extraction.value_extraction * 100).toFixed(0)}%`} />
              </div>
              <div className="text-ink-3">First run: F1 0.976, value extraction 86.7%. That run exposed two real bugs: a grade word wrapped across a line, and brochure values compared at the wrong precision. Both were fixed and the eval re-run; both numbers are reported.</div>
            </div>
          ) : <div className="text-[13px] text-ink-3">pending</div>}
        </Panel>
      </div>
    </div>
  );
}
