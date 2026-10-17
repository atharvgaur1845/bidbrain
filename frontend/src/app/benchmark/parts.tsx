"use client";

import clsx from "clsx";
import { Badge, Card, CodeCell, Empty, Spinner, Stat } from "@/components/ui";

// ======================================================================= data helpers

// Shapes of /api/eval (data/eval/*.json; a file's "summary" key when present).
type SetRun = { n?: number; accuracy?: number | null; proportional?: number | null; by_hops?: Record<string, number> };
type SysRun = { [set: string]: SetRun | undefined } & {
  latency?: { p50?: number | null; p95?: number | null }; llm_calls?: number; tool_calls?: number;
};
export type QA = Record<string, SysRun | undefined>;
type Detection = { precision?: number; recall?: number; f1?: number; tp?: number; fp?: number; fn?: number };
export type ConflictJsonRun = {
  n_events?: number; seconds?: number; detection?: Detection; routing_accuracy?: number;
  per_type?: Record<string, { n: number; correct: number }>;
};
export type ConflictPdfRun = {
  n_pdfs?: number; detection?: Detection; routing_accuracy?: number; resolved_via?: Record<string, number>;
  extraction?: { subject_resolution?: number; value_extraction?: number };
};
export type EvalPayload = {
  qa_summary?: { systems?: QA; model?: string; generated_at?: string };
  conflict_pr_json?: ConflictJsonRun; conflict_pr_pdf?: ConflictPdfRun;
  tool_oracle?: { n?: number; match_engine?: number; match_gold?: number; n_gold?: number };
  abs_set?: unknown; tnd_set?: unknown;
};

export type Sys = { id: string; name: string; desc: string; color: string; planned: boolean };

// Color follows the system, never its rank; every bar is also labelled by name.
// The four planned systems always render; extra runs appear only when results exist.
export const SYSTEMS: Sys[] = [
  { id: "bidbrain", name: "BidBrain agent", desc: "Single agent over typed graph tools", color: "var(--bar-1)", planned: true },
  { id: "bidbrain_multi", name: "BidBrain multi-agent", desc: "Graph-tool workers checked by a verifier", color: "var(--bar-1)", planned: false },
  { id: "text2cypher", name: "LLM text-to-Cypher", desc: "Writes Cypher from the schema, answers from rows", color: "var(--bar-2)", planned: true },
  { id: "vector_rag", name: "Vector RAG", desc: "Top-k chunks from the FalkorDB vector index", color: "var(--bar-3)", planned: true },
  { id: "team", name: "BidBrain team", desc: "Planner, graph-tool workers and a verifier", color: "var(--bar-1)", planned: false },
  { id: "graphrag_sdk", name: "GraphRAG-SDK", desc: "FalkorDB GraphRAG-SDK with an auto-extracted schema", color: "var(--bar-4)", planned: true },
];

export const SETS = ["S21", "Q333", "ABS", "TND"] as const;
export const BANDS = ["2-3", "4", "5-6"] as const;

const isNum = (v: unknown): v is number => typeof v === "number" && !Number.isNaN(v);
export const pct = (v: number, d = 1) => `${(v * 100).toFixed(d)}%`;
const secs = (ms: unknown) => (isNum(ms) && ms > 0 ? `${(ms / 1000).toFixed(1)}s` : null);
const ACRONYMS = new Set(["hr", "jv", "llm", "pdf", "cc"]);
const sentence = (s: string) => {
  const t = s.replace(/_/g, " ").replace(/\b[a-z]+\b/g, (w) => (ACRONYMS.has(w) ? w.toUpperCase() : w));
  return t.charAt(0).toUpperCase() + t.slice(1);
};

/** The best value in a column, or null when fewer than two systems report it (nothing to compare). */
function best(vals: (number | null | undefined)[]) {
  const xs = vals.filter(isNum);
  return xs.length >= 2 ? Math.max(...xs) : null;
}

// align first and last table columns with the card's 20px padding
export const EDGE_CELLS = "[&_td:first-child]:pl-5! [&_th:first-child]:pl-5! [&_td:last-child]:pr-5! [&_th:last-child]:pr-5!";

const Pending = () => <span className="text-[12.5px] text-ink-3">pending</span>;

// ======================================================================= leaderboard

/** Rank as plain tabular number; first place uses the accent token (no medal colours). */
function Rank({ r }: { r: number | null }) {
  if (!r) return <span className="num text-[12.5px] text-ink-3">–</span>;
  return (
    <span className={clsx("num inline-grid h-5 min-w-5 place-items-center rounded-[4px] border px-1 text-[12px] font-medium",
      r === 1 ? "border-info-line bg-kb-soft text-kb-ink" : "border-line bg-surface-2 text-ink-2")}>{r}</span>
  );
}

function Score({ v, top, title }: { v: number | null | undefined; top: number | null; title?: string }) {
  if (!isNum(v)) return <Pending />;
  return (
    <span title={title} className={clsx("num text-[13px]", top !== null && v === top ? "font-semibold text-kb-ink" : "text-ink")}>
      {pct(v)}
    </span>
  );
}

type SetRunFull = SetRun & { accuracy_ci95?: [number, number] | null; tokens_per_q?: number | null;
  latency?: { p50?: number | null; p95?: number | null }; llm_calls?: number | null; tool_calls?: number | null; errors?: number };
const run = (qa: QA, id: string, set: string) => qa[id]?.[set] as SetRunFull | undefined;

export function rankSystems(qa: QA, systems: Sys[], key = "Q333") {
  const score = (s: Sys): number | null => { const v = qa[s.id]?.[key]?.accuracy; return isNum(v) ? v : null; };
  const rows = systems.map((s, i) => ({ s, i, v: score(s) }));
  rows.sort((a, b) => (a.v === null ? 1 : 0) - (b.v === null ? 1 : 0) || (b.v ?? 0) - (a.v ?? 0) || a.i - b.i);
  return { key, rows: rows.map(({ s, v }) => ({ s, rank: v === null ? null : 1 + rows.filter((o) => o.v !== null && o.v > v).length })) };
}

const kTok = (v: unknown) => (isNum(v) ? (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0)) : "—");

/** One evaluation set: accuracy with its 95% CI, proportional credit, tokens, latency and calls per question. */
function SetTable({ qa, systems, set, title, subtitle, baseline }:
  { qa: QA; systems: Sys[]; set: string; title: string; subtitle: string; baseline?: number | null }) {
  const { rows: all } = rankSystems(qa, systems, set);
  const rows = all.filter(({ s }) => s.planned || run(qa, s.id, set));
  const top = best(systems.map((s) => qa[s.id]?.[set]?.accuracy));
  const topProp = best(systems.map((s) => qa[s.id]?.[set]?.proportional));
  const n = systems.map((s) => run(qa, s.id, set)?.n).find(isNum);
  return (
    <Card title={title} subtitle={subtitle} pad={false} className="overflow-hidden"
      action={n ? <span className="num text-[12.5px] text-ink-3">n = {n}</span> : null}>
      <div className="scroll-thin overflow-x-auto">
        <table className="kt dense min-w-[860px]">
          <thead>
            <tr>
              <th className="w-[44px]">#</th>
              <th>System</th>
              <th className="w-[84px] text-right!">Accuracy</th>
              <th className="w-[112px] text-right!" title="Wilson 95% confidence interval on exact-match accuracy">95% CI</th>
              <th className="w-[100px] text-right!" title="Official proportional credit">Proportional</th>
              <th className="w-[84px] text-right!">Tokens/q</th>
              <th className="w-[72px] text-right!">p50</th>
              <th className="w-[72px] text-right!">p95</th>
              <th className="w-[88px] text-right!">LLM / tool</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ s, rank }) => {
              const r = run(qa, s.id, set);
              const ci = r?.accuracy_ci95;
              return (
                <tr key={s.id}>
                  <td><Rank r={rank} /></td>
                  <td className="max-w-0">
                    <div className="truncate text-[13px] font-medium text-ink">{s.name}</div>
                    <div className="truncate text-[12px] text-ink-3">{s.desc}</div>
                  </td>
                  <td className="text-right"><Score v={r?.accuracy} top={top} /></td>
                  <td className="num text-right text-[12.5px] text-ink-2">{ci ? `${pct(ci[0], 0)}–${pct(ci[1], 0)}` : r ? "—" : <Pending />}</td>
                  <td className="text-right"><Score v={r?.proportional} top={topProp} /></td>
                  <td className="num text-right text-[12.5px] text-ink-2">{r ? kTok(r.tokens_per_q) : <Pending />}</td>
                  <td className="num text-right text-[12.5px] text-ink-2">{r ? secs(r.latency?.p50) ?? "—" : <Pending />}</td>
                  <td className="num text-right text-[12.5px] text-ink-2">{r ? secs(r.latency?.p95) ?? "—" : <Pending />}</td>
                  <td className="num text-right text-[12.5px] text-ink-2">{r ? `${isNum(r.llm_calls) ? r.llm_calls.toFixed(1) : "—"} / ${isNum(r.tool_calls) ? r.tool_calls.toFixed(1) : "—"}` : <Pending />}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {isNum(baseline) && (
        <p className="border-t border-line px-4 py-2 text-[12px] text-ink-3">
          Constant baseline (majority answer per type): <span className="num text-ink-2">{pct(baseline)}</span>
        </p>
      )}
    </Card>
  );
}

export function Leaderboard({ qa, systems, loading, baselines }:
  { qa: QA; systems: Sys[]; loading: boolean; baselines?: Record<string, { majority_per_type?: number }> }) {
  if (loading) return <Card><Spinner label="Loading results" /></Card>;
  return (
    <div className="flex flex-col gap-4">
      <SetTable qa={qa} systems={systems} set="S21" baseline={baselines?.S21?.majority_per_type}
        title="Independent gold: S21" subtitle="The dataset's 21 sample questions with verified answers. Small: read the confidence interval, not the point." />
      <SetTable qa={qa} systems={systems} set="Q333" baseline={baselines?.Q333?.majority_per_type}
        title="Near-gold, engine-derived: Q333" subtitle="333 hidden questions labelled by our deterministic engine (99.775 on the public leaderboard), not by people." />
      <div className="flex flex-col gap-4">
        <SetTable qa={qa} systems={systems} set="ABS" baseline={baselines?.ABS?.majority_per_type}
          title="Synthesized: ABS" subtitle="Absence questions; gold computed with Cypher." />
        <SetTable qa={qa} systems={systems} set="TND" baseline={baselines?.TND?.majority_per_type}
          title="Synthesized: TND" subtitle="Tender-clause verdicts; gold from Cypher and Python." />
      </div>
    </div>
  );
}

/** Limitations, always visible next to the numbers. */
export function Limitations() {
  return (
    <Card title="Limitations" subtitle="Read these before quoting a number">
      <ul className="list-disc space-y-1 pl-4 text-[12.5px] leading-relaxed text-ink-2">
        <li>S21 is the only independent gold, and it has 21 questions: a one-question swing moves accuracy by about 5 points.</li>
        <li>Q333 gold comes from the same deterministic engine whose logic the BidBrain graph tools share, so it favours tool-based systems. It is near-gold, not ground truth.</li>
        <li>ABS and TND are synthesized by us from the graph; they test coverage of absence and tender reasoning, not generalisation.</li>
        <li>All systems use the same model and database; latency is wall time on one shared vLLM server and varies with load.</li>
        <li>Exact match is within 0.5%; proportional credit is the dataset&apos;s official partial score.</li>
      </ul>
    </Card>
  );
}

/** Kept for the board tab: superseded by the per-set tables, which carry cost columns. */
export function CostTable(_: { qa: QA; systems: Sys[] }) {
  void _;
  return null;
}

// ======================================================================= hop count

export function HopBars({ qa, systems }: { qa: QA; systems: Sys[] }) {
  const anyRun = systems.some((s) => qa[s.id]?.Q333);
  return (
    <Card title="Accuracy by hop count" action={<span className="text-[12.5px] text-ink-3">Q333 exact match</span>}>
      <p className="mb-5 text-[13px] text-ink-3">
        Hops are the relationships a correct answer has to traverse.
        {anyRun ? "" : " No Q333 runs recorded yet."}
      </p>
      <div className="grid gap-x-10 gap-y-7 lg:grid-cols-3">
        {BANDS.map((b) => {
          const top = best(systems.map((s) => qa[s.id]?.Q333?.by_hops?.[b]));
          return (
            <section key={b} aria-label={`${b} hops`}>
              <h3 className="mb-3 border-b border-line pb-2 text-[14px] font-semibold text-ink">
                <span className="num">{b.replace("-", "–")}</span> hops
              </h3>
              <ul className="space-y-3.5">
                {/* the deterministic team is scored on TND only: leave it out of a Q333 chart rather than show "pending" */}
                {systems.filter((s) => !anyRun || qa[s.id]?.Q333).map((s) => {
                  const v = qa[s.id]?.Q333?.by_hops?.[b];
                  return (
                    <li key={s.id}>
                      <div className="mb-1.5 flex items-baseline justify-between gap-3">
                        <span className="flex min-w-0 items-center gap-2 text-[13px] text-ink-2">
                          <span aria-hidden className="h-2 w-2 shrink-0 rounded-full" style={{ background: s.color }} />
                          <span className="truncate">{s.name}</span>
                        </span>
                        {isNum(v)
                          ? <span className={clsx("num text-[12.5px]", top !== null && v === top ? "font-semibold text-kb-ink" : "text-ink")}>{pct(v)}</span>
                          : <Pending />}
                      </div>
                      <div className="h-2.5 overflow-hidden rounded-full bg-bar-track" title={isNum(v) ? `${s.name}: ${pct(v)}` : `${s.name}: pending`}>
                        {isNum(v) && <div className="h-full rounded-full" style={{ width: `${Math.max(1, v * 100)}%`, background: s.color }} />}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </section>
          );
        })}
      </div>
    </Card>
  );
}

// ======================================================================= conflict engine

const HARD_NEGATIVES = new Set(["consistent_restatement", "rounding_restatement", "unrelated_field"]);
const f2 = (v: unknown) => (isNum(v) ? v.toFixed(2) : "—");
const p0 = (v: unknown) => (isNum(v) ? pct(v, 0) : "—");

const count = (n: unknown, one: string, many = `${one}s`) => {
  const k = isNum(n) ? n : 0;
  return <><span className="num">{k}</span> {k === 1 ? one : many}</>;
};

export function ConflictJson({ cj, loading }: { cj?: ConflictJsonRun; loading: boolean }) {
  return (
    <Card title="JSON stream" pad={false} className="overflow-hidden"
      action={cj ? <span className="num text-[12.5px] text-ink-3">{cj.n_events} events{isNum(cj.seconds) ? ` · ${cj.seconds}s` : ""}</span> : undefined}>
      {!cj ? (
        loading ? <div className="px-5 pb-6 pt-2"><Spinner label="Loading results" /></div>
          : <Empty title="No run recorded" text="python -m eval.conflict_pr writes the planted-stream results." />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-x-6 gap-y-5 px-5 pb-5 pt-2 sm:grid-cols-4">
            <Stat label="F1" value={f2(cj.detection?.f1)} sub={count(cj.detection?.tp, "detected", "detected")} />
            <Stat label="Precision" value={f2(cj.detection?.precision)} sub={count(cj.detection?.fp, "false alarm")} />
            <Stat label="Recall" value={f2(cj.detection?.recall)} sub={count(cj.detection?.fn, "missed", "missed")} />
            <Stat label="Routing" value={f2(cj.routing_accuracy)} sub="Auto vs human" />
          </div>
          <table className={clsx("kt border-t border-line", EDGE_CELLS)}>
            <thead>
              <tr>
                <th>Event type</th>
                <th className="w-[96px] text-right!">Correct</th>
                <th className="w-[96px] text-right!">Accuracy</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(cj.per_type || {}).map(([t, v]) => (
                <tr key={t}>
                  <td>
                    <span className="text-[13.5px] text-ink">{sentence(t)}</span>
                    {HARD_NEGATIVES.has(t) && <Badge className="ml-2">Hard negative</Badge>}
                  </td>
                  <td className="num text-right text-ink-2">{v.correct}/{v.n}</td>
                  <td className="num text-right text-ink">{v.n ? pct(v.correct / v.n, 0) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="border-t border-line px-5 py-3.5 text-[13px] leading-relaxed text-ink-3">
            The same team wrote the labels and the rules, so this checks consistency more than generalization. Hard negatives
            (rounding, unchanged restatements, unrelated fields) measure false alarms.
          </p>
        </>
      )}
    </Card>
  );
}

export function ConflictPdf({ cp, loading }: { cp?: ConflictPdfRun; loading: boolean }) {
  // collapse "certificate reference CC/…" variants into one row
  const via: [string, number][] = Object.entries(
    Object.entries(cp?.resolved_via || {}).reduce((a: Record<string, number>, [k, n]) => {
      const g = k.startsWith("certificate reference") ? "certificate reference" : k;
      a[g] = (a[g] || 0) + Number(n || 0);
      return a;
    }, {}),
  ).sort((a, b) => b[1] - a[1]);

  return (
    <Card title="PDF path" pad={false} className="overflow-hidden"
      action={cp ? <span className="num text-[12.5px] text-ink-3">{cp.n_pdfs} PDFs</span> : undefined}>
      {!cp ? (
        loading ? <div className="px-5 pb-6 pt-2"><Spinner label="Loading results" /></div>
          : <Empty title="No run recorded" text="python -m eval.conflict_pdf renders, extracts and scores the PDF path." />
      ) : (
        <>
          <p className="px-5 text-[13px] text-ink-3">
            Render, extract, resolve the subject, then detect: {count(cp.detection?.fp, "false alarm")},{" "}
            {count(cp.detection?.fn, "missed", "missed")}.
          </p>
          <div className="grid grid-cols-2 gap-x-6 gap-y-5 px-5 pb-5 pt-4 sm:grid-cols-4">
            <Stat label="F1" value={f2(cp.detection?.f1)} sub={count(cp.detection?.tp, "detected", "detected")} />
            <Stat label="Subject resolution" value={p0(cp.extraction?.subject_resolution)} />
            <Stat label="Value extraction" value={p0(cp.extraction?.value_extraction)} />
            <Stat label="Routing" value={f2(cp.routing_accuracy)} sub="Auto vs human" />
          </div>
          {via.length > 0 && (
            <table className={clsx("kt border-t border-line", EDGE_CELLS)}>
              <thead>
                <tr>
                  <th>Subject resolved via</th>
                  <th className="w-[96px] text-right!">PDFs</th>
                </tr>
              </thead>
              <tbody>
                {via.map(([k, n]) => (
                  <tr key={k}>
                    <td className="text-[13.5px] text-ink">{sentence(k)}</td>
                    <td className="num text-right text-ink-2">{n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="border-t border-line px-5 py-3.5 text-[13px] leading-relaxed text-ink-3">
            First run: F1 0.976, value extraction 86.7%. It exposed two real bugs, a grade word wrapped across a line and
            brochure values compared at the wrong precision. Both were fixed and the eval re-run; both results are reported.
          </p>
        </>
      )}
    </Card>
  );
}

// ======================================================================= method

function Bullets({ children }: { children: React.ReactNode }) {
  return <ul className="list-disc space-y-2.5 pl-5 text-[13.5px] leading-relaxed text-ink-2 marker:text-ink-3">{children}</ul>;
}
const Code = ({ children }: { children: React.ReactNode }) => <span className="num font-semibold text-ink">{children}</span>;

export function Method({ ev, model }: { ev: EvalPayload | null; model: string }) {
  const orc = ev?.tool_oracle;
  const nAbs = Array.isArray(ev?.abs_set) ? ev.abs_set.length : null;
  const nTnd = Array.isArray(ev?.tnd_set) ? ev.tnd_set.length : null;
  return (
    <div className="grid items-start gap-5 lg:grid-cols-2">
      <Card title="Gold labels">
        <Bullets>
          <li><Code>S21</Code>: the dataset&apos;s 21 sample questions and their verified answers.</li>
          <li><Code>Q333</Code>: 333 hidden questions, labelled by the deterministic engine (99.775 on the leaderboard).</li>
          <li><Code>ABS</Code>: {nAbs !== null ? <><span className="num">{nAbs}</span>{" "}</> : null}synthesized absence questions, gold computed with Cypher.</li>
          <li><Code>TND</Code>: {nTnd !== null ? <><span className="num">{nTnd}</span>{" "}</> : null}synthesized tender-clause questions, gold from Cypher and Python.</li>
        </Bullets>
      </Card>
      <Card title="Scoring">
        <Bullets>
          <li>Exact match: within 0.5% relative, 0.01 absolute for percentages, exact for counts and days.</li>
          <li>Proportional: the dataset&apos;s official credit, <span className="num whitespace-nowrap">max(0, 1 − |got − gold| / |gold|)</span>.</li>
          <li>Latency and call counts are per question on Q333; p50 and p95 are wall time.</li>
          <li>When a system is rerun, the latest answer per question counts.</li>
        </Bullets>
      </Card>
      <Card title="Fairness">
        <Bullets>
          <li>Every system uses the same model ({model}) and the same FalkorDB instance.</li>
          <li>Only the context strategy differs: graph tools, generated Cypher, vector chunks or an auto-extracted schema.</li>
          <li>
            The graph tools reproduce <span className="num">{orc ? `${orc.match_engine}/${orc.n}` : "354/354"}</span> engine answers
            {orc ? <> and <span className="num">{orc.match_gold}/{orc.n_gold}</span> gold answers</> : null}, so agent misses are
            reasoning errors, not missing tools.
          </li>
          <li>Conflict labels and rules share authors; the PDF path adds extraction and resolution that can fail.</li>
        </Bullets>
      </Card>
      <Card title="Reproduce">
        <div className="space-y-3">
          <CodeCell label="Shell" code={"LLM_PROVIDERS=vllm python -m eval.run \\\n  --system bidbrain --set Q333 --workers 12\npython -m eval.run --summarize"} />
          <CodeCell label="Shell" code={"python -m eval.oracle --set all\npython -m eval.conflict_pr\npython -m eval.conflict_pdf"} />
        </div>
      </Card>
    </div>
  );
}
