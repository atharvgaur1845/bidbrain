"use client";

import { ChevronRight, Database, Wrench } from "lucide-react";
import { useState } from "react";
import { CypherBlock } from "./ui";

export type TraceStep = { tool: string; args?: any; ms?: number; summary?: string; error?: string; cypher?: string[];
  agent?: string; pending?: boolean };

export default function AgentTrace({ steps, running }: { steps: TraceStep[]; running?: boolean }) {
  const [open, setOpen] = useState<number | null>(null);
  if (!steps.length && !running) return <div className="text-[12.5px] text-ink-3">Tool calls appear here as the agent works.</div>;
  return (
    <ol className="relative ml-2 border-l hairline">
      {steps.map((s, i) => (
        <li key={i} className="relative pb-3 pl-4">
          <span className="absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full border-2"
            style={{ borderColor: s.error ? "var(--fail)" : "var(--accent)", background: "var(--bg)" }} />
          <button className="flex w-full items-center gap-2 text-left" onClick={() => setOpen(open === i ? null : i)}>
            <Wrench size={13} className="shrink-0 text-ink-3" />
            <span className="num text-[12.5px] font-semibold text-ink">{s.tool}</span>
            {s.agent && s.agent !== "qa" && <span className="chip !py-0">{s.agent}</span>}
            {s.ms !== undefined && <span className="num ml-auto text-[11px] text-ink-3">{s.ms} ms</span>}
            <ChevronRight size={13} className={`shrink-0 text-ink-3 transition-transform ${open === i ? "rotate-90" : ""}`} />
          </button>
          {s.args && Object.keys(s.args).length > 0 && (
            <div className="num mt-0.5 truncate text-[11.5px] text-ink-3">{JSON.stringify(s.args)}</div>
          )}
          {s.summary && <div className="mt-1 text-[12.5px]" style={{ color: s.error ? "var(--fail)" : "var(--text-2)" }}>{s.summary}</div>}
          {open === i && s.cypher && s.cypher.length > 0 && (
            <div className="mt-2 space-y-2">
              <div className="flex items-center gap-1.5 text-[11px] text-ink-3"><Database size={12} /> Cypher executed on FalkorDB</div>
              {s.cypher.slice(0, 3).map((q, j) => <CypherBlock key={j} q={q} />)}
            </div>
          )}
        </li>
      ))}
      {running && (
        <li className="relative pl-4">
          <span className="absolute -left-[5px] top-1 h-2.5 w-2.5 rounded-full bg-accent pulse-dot" />
          <span className="text-[12.5px] text-ink-3">thinking…</span>
        </li>
      )}
    </ol>
  );
}
