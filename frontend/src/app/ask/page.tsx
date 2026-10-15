"use client";

import { ArrowUp, GitCompareArrows, Sparkles } from "lucide-react";
import { useRef, useState } from "react";
import AgentTrace, { TraceStep } from "@/components/AgentTrace";
import GraphCanvas, { GData, mergeGraph } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { EvidenceList, Markdown, Panel, Spinner } from "@/components/ui";
import { api, fmtINR, stream } from "@/lib/api";

const SUGGESTED = [
  { q: "Which of Trishakti Power Generation Corporation's completed works have no client reference letter on file?",
    tag: "proof of absence" },
  { q: "Starting from Sunita Joshi's Six Sigma Black Belt on the Ring Road — Uttar Pradesh Pkg-107 project, what is the combined value of every completed work for that client?",
    tag: "5-hop" },
  { q: "I'm fairly certain Meera's PMP for the Jharkhand Pkg-29 ROB job was issued on March 10th, 2021 — how many days passed from that certification to the work's completion?",
    tag: "verify a recollection" },
  { q: "Who on the team has worked with the National Expressway Development Authority before, and who is the company's biggest single point of failure across clients?",
    tag: "who knows / bus factor" },
  { q: "What percentage of what we invoiced the client behind the Ring Road — West Bengal Pkg-128 job has actually been collected?",
    tag: "work → client → invoices" },
];

type Turn = { q: string; answer?: any; explanation?: string; steps: TraceStep[]; evidence: any[]; graph: GData;
  highlight: Set<string>; running: boolean; error?: string; usage?: any; ms?: number; providers?: string[];
  baseline?: any; baselineLoading?: boolean };

export default function AskPage() {
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [sel, setSel] = useState<number>(-1);
  const busy = turns.some((t) => t.running);
  const endRef = useRef<HTMLDivElement>(null);
  const cur = sel >= 0 ? turns[sel] : turns[turns.length - 1];

  const update = (i: number, f: (t: Turn) => Turn) => setTurns((ts) => ts.map((t, j) => (j === i ? f(t) : t)));

  async function ask(q: string) {
    if (!q.trim() || busy) return;
    setInput("");
    const i = turns.length;
    setSel(-1);
    setTurns((ts) => [...ts, { q, steps: [], evidence: [], graph: { nodes: [], edges: [] }, highlight: new Set(), running: true }]);
    setTimeout(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), 50);
    await stream("/api/ask", { question: q }, (ev) => {
      if (ev.type === "tool_call") {
        update(i, (t) => ({ ...t, steps: [...t.steps, { tool: ev.tool, args: ev.args, pending: true }] }));
      } else if (ev.type === "tool_result") {
        update(i, (t) => {
          const steps = [...t.steps];
          const k = steps.map((s) => s.tool).lastIndexOf(ev.tool);
          if (k >= 0) steps[k] = { ...steps[k], ms: ev.ms, summary: ev.summary, error: ev.error, cypher: ev.cypher, pending: false };
          const g = mergeGraph(t.graph, ev.subgraph);
          const hl = new Set<string>((ev.subgraph?.nodes || []).map((n: any) => n.id as string));
          return { ...t, steps, graph: g, highlight: hl.size ? hl : t.highlight,
                   evidence: [...t.evidence, ...(ev.evidence || [])] };
        });
      } else if (ev.type === "result") {
        const d = ev.data;
        update(i, (t) => ({ ...t, running: false, answer: d.answer_text, explanation: d.explanation, usage: d.usage,
                            ms: d.ms, providers: d.providers, error: d.error,
                            graph: mergeGraph(t.graph, d.subgraph), evidence: d.evidence?.length ? d.evidence : t.evidence }));
      } else if (ev.type === "error") {
        update(i, (t) => ({ ...t, running: false, error: ev.message }));
      }
    });
    update(i, (t) => ({ ...t, running: false }));
  }

  async function compare(i: number) {
    update(i, (t) => ({ ...t, baselineLoading: true }));
    try {
      const b = await api(`/api/baseline/vector_rag`, { method: "POST", body: JSON.stringify({ question: turns[i].q }) });
      update(i, (t) => ({ ...t, baseline: b, baselineLoading: false }));
    } catch (e: any) {
      update(i, (t) => ({ ...t, baseline: { error: e.message }, baselineLoading: false }));
    }
  }

  return (
    <div>
      <PageHeader eyebrow="Ask the company brain" title="Multi-hop answers, with the path behind them">
        The agent plans over 20 graph tools — traversals, exact arithmetic, absence proofs and FalkorDB algorithms —
        and every answer comes with the documents and pages that prove it. Compare it with vector RAG on the same model.
      </PageHeader>

      <div className="grid gap-5 lg:grid-cols-12">
        <div className="flex flex-col gap-4 lg:col-span-5">
          <Panel title="Conversation" pad={false}>
            <div className="scroll-thin max-h-[58vh] space-y-4 overflow-y-auto p-4">
              {turns.length === 0 && (
                <div className="space-y-2">
                  <div className="text-[13px] text-ink-2">Try one of these — each needs several hops through the graph:</div>
                  {SUGGESTED.map((s) => (
                    <button key={s.q} onClick={() => ask(s.q)} className="panel-2 block w-full px-3 py-2.5 text-left hover:border-line-2">
                      <span className="chip mb-1.5"><Sparkles size={11} className="text-accent" />{s.tag}</span>
                      <span className="block text-[13px] leading-snug text-ink">{s.q}</span>
                    </button>
                  ))}
                </div>
              )}
              {turns.map((t, i) => (
                <div key={i} className="space-y-2" onClick={() => setSel(i)}>
                  <div className="ml-auto max-w-[92%] rounded-xl rounded-tr-sm bg-panel-2 px-3 py-2 text-[13.5px]">{t.q}</div>
                  <div className={`rounded-xl border px-3 py-3 ${cur === t ? "border-line-2" : "hairline"}`}>
                    {t.running && <Spinner label={`${t.steps.length} tool calls so far`} />}
                    {t.error && !t.answer && <div className="text-[13px]" style={{ color: "var(--fail)" }}>{t.error}</div>}
                    {t.answer !== undefined && !t.running && (
                      <>
                        <div className="eyebrow">Answer</div>
                        <div className="num mt-0.5 text-[22px] font-bold text-accent">{String(t.answer)}</div>
                        {/^\d{6,}$/.test(String(t.answer).replace(/,/g, "")) && (
                          <div className="num text-[12px] text-ink-3">{fmtINR(Number(String(t.answer).replace(/,/g, "")))}</div>
                        )}
                        <details className="mt-2">
                          <summary className="cursor-pointer text-[12px] text-ink-3">How the agent got there</summary>
                          <Markdown>{(t.explanation || "").replace(/ANSWER:.*$/im, "")}</Markdown>
                        </details>
                        <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11.5px] text-ink-3">
                          <span className="chip num">{t.usage?.tool_calls ?? t.steps.length} tool calls</span>
                          <span className="chip num">{t.usage?.llm_calls ?? "–"} LLM calls</span>
                          <span className="chip num">{t.ms ? `${(t.ms / 1000).toFixed(1)} s` : ""}</span>
                          {t.providers?.[0] && <span className="chip">{t.providers[0]}</span>}
                          <button className="btn ml-auto !py-1 !text-[12px]" disabled={t.baselineLoading}
                            onClick={(e) => { e.stopPropagation(); compare(i); }}>
                            <GitCompareArrows size={13} /> {t.baselineLoading ? "Running vector RAG…" : "Compare with vector RAG"}
                          </button>
                        </div>
                        {t.baseline && (
                          <div className="mt-3 rounded-lg border p-3" style={{ borderColor: "color-mix(in oklab, var(--fail) 35%, transparent)" }}>
                            <div className="eyebrow">Vector RAG · same model · top-8 chunks</div>
                            {t.baseline.error ? <div className="text-[12.5px] text-ink-3">{t.baseline.error}</div> : (
                              <>
                                <div className="num mt-0.5 text-[18px] font-bold" style={{ color: "var(--fail)" }}>{String(t.baseline.answer_text)}</div>
                                <div className="mt-1 text-[12px] text-ink-3">
                                  Saw {t.baseline.retrieved?.length} chunks from{" "}
                                  {[...new Set((t.baseline.retrieved || []).map((r: any) => r.doc_id))].slice(0, 4).join(", ")}
                                  {" "}— it can only reason over what it retrieved, never over the boundary of the set.
                                </div>
                              </>
                            )}
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </div>
              ))}
              <div ref={endRef} />
            </div>
            <form className="flex items-end gap-2 border-t hairline p-3" onSubmit={(e) => { e.preventDefault(); ask(input); }}>
              <textarea className="input min-h-[44px] resize-none" rows={2} value={input} placeholder="Ask about works, clients, people, credentials, invoices…"
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input); } }} />
              <button className="btn btn-primary !px-3" disabled={busy || !input.trim()} aria-label="Ask"><ArrowUp size={16} /></button>
            </form>
          </Panel>
        </div>

        <div className="flex flex-col gap-4 lg:col-span-7">
          <Panel title="Evidence path" right={<span className="text-[11.5px] text-ink-3">gold = the hops behind the latest step</span>} pad={false}>
            <div className="p-2">
              <GraphCanvas data={cur?.graph || { nodes: [], edges: [] }} highlight={cur?.highlight} height={430}
                emptyHint="Ask a question — the subgraph the agent traverses is drawn here." />
            </div>
          </Panel>
          <div className="grid gap-4 md:grid-cols-2">
            <Panel title="Agent trace">
              <div className="scroll-thin max-h-[360px] overflow-y-auto pr-1">
                <AgentTrace steps={cur?.steps || []} running={cur?.running} />
              </div>
            </Panel>
            <Panel title="Documents that prove it">
              <div className="scroll-thin max-h-[360px] overflow-y-auto pr-1">
                <EvidenceList items={cur?.evidence || []} />
              </div>
            </Panel>
          </div>
        </div>
      </div>
    </div>
  );
}
