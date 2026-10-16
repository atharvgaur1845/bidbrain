"use client";

import { BarChart3 as BarChart3Icon } from "lucide-react";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/Shell";
import { Card, Stat, Tabs } from "@/components/ui";
import { api } from "@/lib/api";
import { ConflictJson, ConflictPdf, CostTable, type EvalPayload, HopBars, Leaderboard, Method, SYSTEMS } from "./parts";

const TABS = [
  { id: "board", label: "Leaderboard" },
  { id: "hops", label: "By hop count" },
  { id: "conflict", label: "Conflict engine" },
  { id: "method", label: "Method" },
];

export default function BenchmarkPage() {
  const [ev, setEv] = useState<EvalPayload | null>(null);
  const [tab, setTab] = useState("board");
  useEffect(() => { api<EvalPayload>("/api/eval").then(setEv).catch(() => setEv({})); }, []);

  const loading = ev === null;
  const summary = ev?.qa_summary;
  const qa = summary?.systems || {};
  const systems = SYSTEMS.filter((s) => s.planned || qa[s.id]);
  const cj = ev?.conflict_pr_json, cp = ev?.conflict_pr_pdf, orc = ev?.tool_oracle;
  const model = summary?.model || "Qwen3-32B (vLLM)";

  return (
    <div>
      <PageHeader icon={<BarChart3Icon size={19} strokeWidth={1.75} />} tone="sage" title="Benchmark"
        subtitle="Same model, same database, different context strategy."
        meta={
          <>
            <span className="text-ink-2">{model}</span>
            <span aria-hidden>·</span>
            <span>Sets <span className="num text-ink-2">S21 · Q333 · ABS · TND</span></span>
            {summary?.generated_at && (
              <>
                <span aria-hidden>·</span>
                <span>Updated <span className="num text-ink-2">{summary.generated_at}</span></span>
              </>
            )}
          </>
        } />

      <div className="scroll-thin mb-6 overflow-x-auto [&_[role=tab]]:shrink-0 [&_[role=tab]]:whitespace-nowrap">
        <Tabs tabs={TABS} value={tab} onChange={setTab} />
      </div>

      {tab === "board" && (
        <div className="flex flex-col gap-5">
          <Card>
            <div className="grid grid-cols-2 gap-x-6 gap-y-5 pt-3 md:grid-cols-4">
              <Stat label="Tool sufficiency" value={orc ? `${orc.match_engine}/${orc.n}` : "354/354"}
                sub="Engine answers the graph tools reproduce" />
              <Stat label="Conflict F1, JSON" value={typeof cj?.detection?.f1 === "number" ? cj.detection.f1.toFixed(2) : "—"}
                sub={<><span className="num">{cj?.n_events ?? "–"}</span> planted events</>} />
              <Stat label="Conflict F1, PDF" value={typeof cp?.detection?.f1 === "number" ? cp.detection.f1.toFixed(2) : "—"}
                sub={<><span className="num">{cp?.n_pdfs ?? "–"}</span> rendered PDFs, end to end</>} />
              <Stat label="Entity resolution" value={typeof cp?.extraction?.subject_resolution === "number" ? `${(cp.extraction.subject_resolution * 100).toFixed(0)}%` : "—"}
                sub="PDF subjects with varied titles" />
            </div>
          </Card>
          <Leaderboard qa={qa} systems={systems} loading={loading} />
          <CostTable qa={qa} systems={systems} />
        </div>
      )}

      {tab === "hops" && <HopBars qa={qa} systems={systems} />}

      {tab === "conflict" && (
        <div className="grid items-start gap-5 lg:grid-cols-2">
          <ConflictJson cj={cj} loading={loading} />
          <ConflictPdf cp={cp} loading={loading} />
        </div>
      )}

      {tab === "method" && <Method ev={ev} model={model} />}
    </div>
  );
}
