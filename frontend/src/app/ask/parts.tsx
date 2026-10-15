"use client";

import clsx from "clsx";
import { ChevronRight } from "lucide-react";
import { useState } from "react";
import { Badge, CodeCell, Empty, Spinner } from "@/components/ui";

export type TraceStep = { tool: string; args?: any; ms?: number; summary?: string; error?: string; cypher?: string[];
  agent?: string; pending?: boolean };

/** Unique (doc, page) pairs, matching how EvidenceList de-duplicates. */
export function countDocs(items: { doc_id?: string; page?: number | null }[]) {
  return new Set(items.filter((e) => e.doc_id).map((e) => `${e.doc_id}|${e.page}`)).size;
}

/** Tool-call timeline, laid out like notebook cells: [n] counter, tool, args, result, Cypher on demand. */
export function Trace({ steps, running }: { steps: TraceStep[]; running?: boolean }) {
  const [open, setOpen] = useState<number | null>(null);
  if (!steps.length && !running)
    return <Empty title="No tool calls yet" text="Each step the agent takes shows up here with its arguments and the Cypher it ran." />;
  const waiting = running && !steps.some((s) => s.pending);
  return (
    <ol className="flex flex-col">
      {steps.map((s, i) => {
        const args = s.args && Object.keys(s.args).length > 0 ? JSON.stringify(s.args) : "";
        const n = s.cypher?.length || 0;
        const isOpen = open === i;
        const live = s.pending && running;
        return (
          <li key={i} className="flex gap-3 border-b border-line py-3 last:border-b-0">
            <span className="num w-9 shrink-0 text-right text-[12px] leading-5 text-ink-3">[{live ? "*" : i + 1}]</span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="num truncate text-[13px] font-semibold leading-5 text-ink">{s.tool}</span>
                {s.agent && s.agent !== "qa" && <Badge>{s.agent}</Badge>}
                <span className="num ml-auto shrink-0 text-[12px] text-ink-3">
                  {live ? "running" : s.ms !== undefined ? `${s.ms} ms` : ""}
                </span>
              </div>
              {args && <div className="num mt-0.5 truncate text-[12px] text-ink-3" title={args}>{args}</div>}
              {s.summary && (
                <div className={clsx("mt-1.5 text-[13px] leading-snug", s.error ? "text-bad" : "text-ink-2")}>{s.summary}</div>
              )}
              {n > 0 && (
                <>
                  <button type="button" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : i)}
                    className="mt-2 inline-flex items-center gap-1 text-[12.5px] font-medium text-kb-ink hover:underline">
                    <ChevronRight size={14} strokeWidth={1.75}
                      className={clsx("motion-safe:transition-transform motion-safe:duration-150 motion-safe:ease-out", isOpen && "rotate-90")} />
                    {isOpen ? "Hide Cypher" : n > 1 ? `Show Cypher (${n})` : "Show Cypher"}
                  </button>
                  {isOpen && (
                    <div className="mt-2 flex flex-col gap-2">
                      {s.cypher!.slice(0, 3).map((q, j) => <CodeCell key={j} code={q} />)}
                    </div>
                  )}
                </>
              )}
            </div>
          </li>
        );
      })}
      {waiting && (
        <li className="flex gap-3 py-3">
          <span className="num w-9 shrink-0 text-right text-[12px] leading-5 text-ink-3">[*]</span>
          <Spinner label="Planning the next step" />
        </li>
      )}
    </ol>
  );
}

/** BidBrain vs vector RAG on the same question and model. */
export function Comparison({ answer, toolCalls, baseline }: { answer: any; toolCalls: number; baseline: any }) {
  const retrieved: any[] = baseline.retrieved || [];
  const docs = [...new Set(retrieved.map((r) => r.doc_id))].slice(0, 4).join(", ");
  return (
    <div className="well mt-4 overflow-hidden">
      <div className="grid grid-cols-2 divide-x divide-line">
        <div className="min-w-0 p-4">
          <div className="text-[12.5px] font-medium text-ink-2">BidBrain</div>
          <div className="num mt-1 break-words text-[16px] font-semibold leading-snug text-ink">{String(answer)}</div>
          <div className="mt-1 text-[12px] text-ink-3">Graph agent, {toolCalls} tool calls</div>
        </div>
        <div className="min-w-0 p-4">
          <div className="text-[12.5px] font-medium text-ink-2">Vector RAG</div>
          {baseline.error ? (
            <div className="mt-1 text-[12.5px] text-ink-3">{baseline.error}</div>
          ) : (
            <>
              <div className="num mt-1 break-words text-[16px] font-semibold leading-snug text-bad">{String(baseline.answer_text)}</div>
              <div className="mt-1 text-[12px] text-ink-3">Same model, top-8 chunks</div>
            </>
          )}
        </div>
      </div>
      {!baseline.error && (
        <div className="border-t border-line px-4 py-3 text-[12.5px] leading-snug text-ink-3">
          Answered from {retrieved.length} retrieved chunks (<span className="num">{docs || "no documents"}</span>), so it
          cannot reason about anything outside them.
        </div>
      )}
    </div>
  );
}
