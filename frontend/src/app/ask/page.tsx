"use client";

import clsx from "clsx";
import { ArrowUp, ChevronRight, GitCompareArrows } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import GraphCanvas, { GData, mergeGraph } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { Badge, Button, Card, EvidenceList, Markdown, Spinner, Tabs } from "@/components/ui";
import { api, fmtINR, stream } from "@/lib/api";
import { Comparison, Trace, TraceStep, countDocs } from "./parts";

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

const EMPTY_GRAPH: GData = { nodes: [], edges: [] };
/** Smooth scrolling is motion: skip it when the viewer asks for reduced motion. */
const scrollBehavior = (): ScrollBehavior =>
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
const PANEL_H = 500;
const TAB_HINT: Record<string, string> = {
  path: "Blue marks the nodes returned by the latest step.",
  trace: "Every tool call in order, with the Cypher it ran on FalkorDB.",
  docs: "Open a document at the cited page.",
};

export default function AskPage() {
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [sel, setSel] = useState<number>(-1);
  const [tab, setTab] = useState("path");
  const [llm, setLlm] = useState<{ ok: boolean; replay: boolean } | null>(null);
  useEffect(() => { api("/api/health").then((h) => setLlm({ ok: (h.llm || []).length > 0, replay: !!h.replay })).catch(() => {}); }, []);
  const busy = turns.some((t) => t.running);
  const endRef = useRef<HTMLDivElement>(null);
  const cur = sel >= 0 ? turns[sel] : turns[turns.length - 1];
  const curIdx = sel >= 0 ? sel : turns.length - 1;

  const update = (i: number, f: (t: Turn) => Turn) => setTurns((ts) => ts.map((t, j) => (j === i ? f(t) : t)));

  async function ask(q: string) {
    if (!q.trim() || busy) return;
    setInput("");
    const i = turns.length;
    setSel(-1);
    setTurns((ts) => [...ts, { q, steps: [], evidence: [], graph: { nodes: [], edges: [] }, highlight: new Set(), running: true }]);
    setTimeout(() => endRef.current?.scrollIntoView({ behavior: scrollBehavior(), block: "nearest" }), 50);
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

  const tabs = [
    { id: "path", label: "Evidence path", count: cur ? cur.graph.nodes.length : undefined },
    { id: "trace", label: "Trace", count: cur ? cur.steps.length : undefined },
    { id: "docs", label: "Documents", count: cur ? countDocs(cur.evidence) : undefined },
  ];
  const link = "font-medium text-kb-ink hover:underline";

  return (
    <div>
      <PageHeader title="Ask"
        subtitle="Multi-hop questions answered from the graph, with the documents that prove each answer." />

      {llm && !llm.ok && (
        <div className="well mb-5 px-4 py-3 text-[13px] leading-relaxed text-ink-2">
          {llm.replay
            ? "Replay mode: suggested questions answer from a recorded run of the agent. New questions need a live model."
            : <>No LLM is configured on this server, so free-text questions are off. These run on the graph alone:{" "}
                <Link className={link} href="/qualify/">qualify a tender</Link>,{" "}
                <Link className={link} href="/live/">live documents</Link> and{" "}
                <Link className={link} href="/explore/">explore</Link>.</>}
        </div>
      )}

      <div className="grid items-start gap-5 lg:grid-cols-12">
        {/* ------------------------------------------------------------ conversation */}
        <Card className="lg:col-span-5" title="Conversation" pad={false}
          action={turns.length > 0 && (
            <span className="num text-[12.5px] text-ink-3">{turns.length} {turns.length === 1 ? "question" : "questions"}</span>
          )}>
          <div className="scroll-thin contain-scroll flex max-h-[62vh] flex-col gap-7 overflow-y-auto px-5 pb-5 pt-1">
            {turns.length === 0 && (
              <div>
                <p className="text-[13px] text-ink-3">Start with a suggested question. Each one needs several hops.</p>
                <ul className="-mx-2 mt-2 flex flex-col">
                  {SUGGESTED.map((s) => (
                    <li key={s.q}>
                      <button type="button" onClick={() => ask(s.q)}
                        className="w-full rounded-lg px-2 py-3 text-left transition-colors duration-150 ease-out hover:bg-well">
                        <span className="block text-[13.5px] leading-snug text-ink">{s.q}</span>
                        <Badge className="mt-1.5">{s.tag}</Badge>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {turns.map((t, i) => {
              const selected = cur === t && turns.length > 1;
              const ans = String(t.answer);
              const plain = ans.replace(/,/g, "");
              const explanation = (t.explanation || "").replace(/ANSWER:.*$/im, "").trim();
              const meta = [
                `${t.usage?.tool_calls ?? t.steps.length} tool calls`,
                `${t.usage?.llm_calls ?? "–"} LLM calls`,
                t.ms ? `${(t.ms / 1000).toFixed(1)} s` : "",
                t.providers?.[0] || "",
              ].filter(Boolean);
              return (
                <div key={i} className={clsx(turns.length > 1 && "cursor-pointer")} onClick={() => setSel(i)}>
                  <button type="button" aria-pressed={turns.length > 1 ? selected : undefined}
                    className="ml-auto block w-fit max-w-[88%] rounded-2xl rounded-br-md bg-well px-4 py-2 text-left text-[13.5px] leading-snug text-ink">
                    {t.q}
                  </button>

                  <div className={clsx("mt-3 border-l-2 pl-4 transition-colors duration-150 ease-out",
                    selected ? "border-kb" : "border-line")}>
                    {t.running && <Spinner label={`${t.steps.length} tool calls so far`} />}
                    {t.error && !t.answer && <div className="text-[13px] text-bad">{t.error}</div>}
                    {!t.running && t.answer === undefined && !t.error && (
                      <div className="text-[13px] text-ink-3">No answer returned.</div>
                    )}
                    {t.answer !== undefined && !t.running && (
                      <>
                        <div className="text-[12.5px] font-medium text-ink-3">Answer</div>
                        <div className={clsx("num mt-1 break-words font-semibold leading-tight tracking-[-0.02em] text-ink",
                          ans.length > 24 ? "text-[16px]" : "text-[26px]")}>{ans}</div>
                        {/^\d{6,}$/.test(plain) && (
                          <div className="num mt-1 text-[13px] text-ink-2">{fmtINR(Number(plain))}</div>
                        )}
                        <div className="mt-2 text-[12.5px] tabular-nums text-ink-3">{meta.join(" · ")}</div>

                        {explanation && (
                          <details className="group mt-3">
                            <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-[13px] font-medium text-kb-ink hover:underline [&::-webkit-details-marker]:hidden">
                              <ChevronRight size={15} strokeWidth={1.75}
                                className="motion-safe:transition-transform motion-safe:duration-150 motion-safe:ease-out group-open:rotate-90" />
                              How it got there
                            </summary>
                            <div className="mt-2 [&_ol]:my-1.5 [&_ol]:list-decimal [&_ol]:pl-5 [&_ol_li]:my-0.5">
                              <Markdown>{explanation}</Markdown>
                            </div>
                          </details>
                        )}

                        <div className="mt-3">
                          <Button variant="secondary" size="sm" disabled={t.baselineLoading}
                            onClick={(e) => { e.stopPropagation(); compare(i); }}>
                            <GitCompareArrows size={15} strokeWidth={1.75} />
                            {t.baselineLoading ? "Running vector RAG…" : "Compare with vector RAG"}
                          </Button>
                        </div>

                        {t.baseline && (
                          <Comparison answer={t.answer} baseline={t.baseline}
                            toolCalls={t.usage?.tool_calls ?? t.steps.length} />
                        )}
                      </>
                    )}
                  </div>
                </div>
              );
            })}
            <div ref={endRef} />
          </div>

          <form className="border-t border-line p-4" onSubmit={(e) => { e.preventDefault(); ask(input); }}>
            <textarea className="field min-h-[64px] resize-none leading-snug" rows={2} value={input}
              aria-label="Your question" placeholder="Ask about works, clients, people, credentials, invoices…"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input); } }} />
            <div className="mt-3 flex items-center gap-3">
              <span className="hidden text-[12.5px] text-ink-3 sm:inline">Enter to send, Shift+Enter for a new line</span>
              <Button type="submit" variant="primary" size="sm" className="ml-auto" disabled={busy || !input.trim()}>
                Ask <ArrowUp size={15} strokeWidth={1.75} />
              </Button>
            </div>
          </form>
        </Card>

        {/* ------------------------------------------------------------ evidence */}
        <Card className="overflow-hidden lg:col-span-7" pad={false}>
          <div className="flex items-end px-5 pt-3">
            <div className="min-w-0 flex-1"><Tabs tabs={tabs} value={tab} onChange={setTab} /></div>
            {turns.length > 1 && (
              <div className="num hidden border-b border-line pb-2.5 pt-1 text-[12.5px] leading-[21px] text-ink-3 sm:block">
                Question {curIdx + 1} of {turns.length}
              </div>
            )}
          </div>

          {tab === "path" && (
            <GraphCanvas data={cur?.graph || EMPTY_GRAPH} highlight={cur?.highlight} height={PANEL_H}
              emptyHint="Ask a question to draw the subgraph the agent traverses." />
          )}
          {tab === "trace" && (
            <div className="scroll-thin contain-scroll overflow-y-auto px-5 py-1" style={{ height: PANEL_H }}>
              <Trace key={curIdx} steps={cur?.steps || []} running={cur?.running} />
            </div>
          )}
          {tab === "docs" && (
            <div className="scroll-thin contain-scroll overflow-y-auto px-5 py-3" style={{ height: PANEL_H }}>
              <EvidenceList items={cur?.evidence || []} max={40} />
            </div>
          )}

          <div className="border-t border-line px-5 py-3 text-[12.5px] text-ink-3">{TAB_HINT[tab]}</div>
        </Card>
      </div>
    </div>
  );
}
