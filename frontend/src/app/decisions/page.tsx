"use client";

import clsx from "clsx";
import { FileText, GitBranch, GitBranch as GitBranchIcon } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import GraphCanvas, { GData } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { Avatar, Badge, Card, Empty, Spinner, Tabs, useOpenDoc } from "@/components/ui";
import { api } from "@/lib/api";

const POLICIES: { id: string; label: string; tone: string }[] = [
  { id: "authority", label: "Authority", tone: "blue" },
  { id: "recency_same_issuer", label: "Recency", tone: "ok" },
  { id: "learned_rule", label: "Learned rule", tone: "violet" },
  { id: "org_rule", label: "Company memory", tone: "violet" },
  { id: "human", label: "Human", tone: "warn" },
];

const STATUS_TONE: Record<string, string> = { active: "ok", contested: "warn" };

const sentence = (s: string) => {
  const t = String(s || "").replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
};

function PolicyBadge({ policy }: { policy: string }) {
  const p = POLICIES.find((x) => x.id === policy);
  return <Badge tone={p?.tone || "neutral"}>{p?.label || sentence(policy)}</Badge>;
}

function Actor({ name, kind, size = 24 }: { name?: string; kind?: string; size?: number }) {
  if (!name) return <span className="text-ink-3">—</span>;
  if (kind === "User") return <span title={name} className="inline-flex"><Avatar name={name} size={size} /><span className="sr-only">{name}</span></span>;
  return <span title={name}><Badge>Policy</Badge></span>;
}

// align the first and last table columns with the card title
const EDGE_CELLS = "[&_td:first-child]:pl-5 [&_th:first-child]:pl-5 [&_td:last-child]:pr-5 [&_th:last-child]:pr-5";

export default function DecisionsPage() {
  const [conf, setConf] = useState<any>({ decisions: [], rules: [] });
  const [loaded, setLoaded] = useState(false);
  const [filter, setFilter] = useState<string>("all");
  const [sel, setSel] = useState<string | null>(null);
  const [trace, setTrace] = useState<any>(null);
  const openDoc = useOpenDoc();

  useEffect(() => { api("/api/conflicts").then(setConf).finally(() => setLoaded(true)); }, []);
  useEffect(() => {
    if (!sel) return;
    api(`/api/decisions/${sel}/trace`).then(setTrace).catch(() => setTrace(null));
  }, [sel]);

  const list = conf.decisions.filter((d: any) => filter === "all" || d.policy === filter);
  const counts = conf.decisions.reduce((a: any, d: any) => ({ ...a, [d.policy]: (a[d.policy] || 0) + 1 }), {});

  const graph: GData = useMemo(() => {
    if (!trace) return { nodes: [], edges: [] };
    const d = `Decision:${trace.decision.id}`;
    const nodes: any[] = [{ id: d, label: "Decision", name: `${trace.decision.policy}` },
      { id: `${trace.subject_label}:${trace.subject_key}`, label: trace.subject_label, name: trace.subject }];
    const edges: any[] = [];
    if (trace.actor) { nodes.push({ id: `Actor:${trace.actor}`, label: trace.actor_kind === "User" ? "Person" : "Company", name: trace.actor }); edges.push({ source: d, target: `Actor:${trace.actor}`, type: "DECIDED_BY" }); }
    for (const c of trace.claims || []) {
      if (!c.claim) continue;
      nodes.push({ id: `Claim:${c.claim}`, label: "Claim", name: `${c.value} (${c.kind})` });
      edges.push({ source: d, target: `Claim:${c.claim}`, type: c.claim === trace.winner ? "SELECTS" : "BASED_ON" });
      edges.push({ source: `Claim:${c.claim}`, target: `${trace.subject_label}:${trace.subject_key}`, type: "ABOUT" });
      if (c.doc) { nodes.push({ id: `Document:${c.doc}`, label: "Document", name: c.doc }); edges.push({ source: `Claim:${c.claim}`, target: `Document:${c.doc}`, type: "FROM" }); }
    }
    if (trace.rule) { nodes.push({ id: `Rule:${trace.rule}`, label: "Finding", name: trace.rule }); edges.push({ source: d, target: `Rule:${trace.rule}`, type: "APPLIED_RULE" }); }
    for (const o of trace.supersedes || []) { nodes.push({ id: `Decision:${o}`, label: "Decision", name: o }); edges.push({ source: d, target: `Decision:${o}`, type: "SUPERSEDES" }); }
    const seen = new Set(); return { nodes: nodes.filter((n) => !seen.has(n.id) && seen.add(n.id)), edges };
  }, [trace]);

  const claims = (trace?.claims || []).filter((c: any) => c.claim);
  const byPeople = counts.human || 0;

  return (
    <div>
      <PageHeader icon={<GitBranchIcon size={19} strokeWidth={1.75} />} tone="rose" title="Decisions"
        subtitle="Every settled fact records the policy, the decider and both source documents."
        meta={loaded ? (
          <>
            <span><span className="num text-ink-2">{conf.decisions.length}</span> decisions</span>
            <span aria-hidden>·</span>
            <span><span className="num text-ink-2">{conf.rules.length}</span> learned rules</span>
            <span aria-hidden>·</span>
            <span><span className="num text-ink-2">{byPeople}</span> settled by people</span>
          </>
        ) : undefined} />

      <div className="scroll-thin contain-scroll mb-6 overflow-x-auto [&>[role=tablist]]:w-max [&>[role=tablist]]:min-w-full">
        <Tabs value={filter} onChange={setFilter}
          tabs={[{ id: "all", label: "All", count: conf.decisions.length },
            ...POLICIES.map((p) => ({ id: p.id, label: p.label, count: counts[p.id] || 0 }))]} />
      </div>

      <div className="grid items-start gap-5 xl:grid-cols-12">
        {/* ---------------------------------------------------------------- decision log */}
        <Card title="Decision log" pad={false} className="overflow-hidden xl:col-span-5"
          action={loaded ? <span className="num text-[12.5px] text-ink-3">{list.length}</span> : undefined}>
          <div className="scroll-thin contain-scroll max-h-[72vh] overflow-y-auto">
            {!loaded ? (
              <div className="px-5 py-8"><Spinner label="Loading decisions" /></div>
            ) : list.length === 0 ? (
              <Empty title="No decisions of this kind yet" text="Decisions appear here once conflicting claims are settled." />
            ) : (
              <table className={clsx("kt table-fixed", EDGE_CELLS,
                "[&_th]:sticky [&_th]:top-0 [&_th]:z-10 [&_th]:bg-surface [&_th]:backdrop-blur-md")}>
                <thead>
                  <tr>
                    <th>Decision</th>
                    <th className="w-[124px]">Policy</th>
                    <th className="w-[84px]">By</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((d: any) => {
                    const active = sel === d.id;
                    return (
                      <tr key={d.id} onClick={() => setSel(d.id)}
                        className={clsx("cursor-pointer", active && "[&>td]:bg-kb-soft")}>
                        <td className={clsx(active && "shadow-[inset_2px_0_0_var(--kb)]")}>
                          <button type="button" onClick={(e) => { e.stopPropagation(); setSel(d.id); }}
                            aria-pressed={active}
                            className="block w-full min-w-0 rounded-md text-left outline-none focus-visible:ring-2 focus-visible:ring-kb">
                            <span className="block truncate text-[13.5px] font-medium text-ink">
                              {d.subject || d.key}
                              <span className="num ml-2 text-[12px] font-normal text-ink-3">{d.predicate}</span>
                            </span>
                            <span className="mt-1 block truncate text-[12.5px] text-ink-3">{d.rationale}</span>
                          </button>
                        </td>
                        <td><PolicyBadge policy={d.policy} /></td>
                        <td><Actor name={d.actor} kind={d.actor_kind} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </Card>

        {/* ---------------------------------------------------------------- trace */}
        <div className="flex min-w-0 flex-col gap-5 xl:col-span-7">
          <Card title="Trace" pad={false} className="overflow-hidden"
            action={trace && sel ? <span className="num truncate text-[12.5px] text-ink-3">{trace.decision.id}</span> : undefined}>
            {!sel ? (
              <Empty icon={<GitBranch size={20} strokeWidth={1.75} />} title="No decision selected"
                text="Pick a row in the log to see the claims it compared and the documents behind them." />
            ) : (
              <>
                <GraphCanvas data={graph} showAllLabels height={320} emptyHint="Tracing this decision" />
                {trace && (
                  <div className="space-y-4 border-t border-line px-5 pb-5 pt-4">
                    <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-[15px] font-semibold text-ink">{trace.subject}</div>
                        <div className="num mt-1 text-[12.5px] text-ink-3">{trace.decision.predicate}</div>
                      </div>
                      <div className="text-right">
                        <div className="text-[12.5px] text-ink-3">Settled value</div>
                        <div className="num mt-1 text-[20px] font-semibold leading-tight text-kb-ink">{trace.decision.chosen_value}</div>
                      </div>
                    </div>

                    {claims.length > 0 && (
                      <div className="grid gap-3 sm:grid-cols-2">
                        {claims.map((c: any) => {
                          const won = c.claim === trace.winner;
                          return (
                            <div key={c.claim} className={clsx("well p-4", won && "ring-2 ring-kb")}>
                              <div className="flex items-center gap-2">
                                <span className={clsx("text-[12.5px] font-medium", won ? "text-kb-ink" : "text-ink-3")}>
                                  {won ? "Selected" : "Set aside"}
                                </span>
                                {c.status && <span className="ml-auto"><Badge tone={STATUS_TONE[c.status] || "neutral"}>{sentence(c.status)}</Badge></span>}
                              </div>
                              <div className={clsx("num mt-2 truncate text-[22px] font-semibold leading-tight tracking-[-0.01em]",
                                won ? "text-ink" : "text-ink-3")}>{c.value}</div>
                              <div className={clsx("mt-2 text-[12.5px]", won ? "text-ink-2" : "text-ink-3")}>
                                {[c.kind, c.authority !== null && c.authority !== undefined ? `authority ${c.authority}` : null, c.issuer]
                                  .filter(Boolean).join(" · ")}
                              </div>
                              {c.doc && (
                                <button type="button" onClick={() => openDoc({ doc_id: c.doc, page: c.page })}
                                  className="num mt-3 inline-flex max-w-full items-center gap-2 rounded-md text-[12.5px] text-kb-ink outline-none hover:underline focus-visible:ring-2 focus-visible:ring-kb">
                                  <FileText size={15} strokeWidth={1.75} className="shrink-0" />
                                  <span className="truncate">{c.doc}{c.page ? ` · p.${c.page}` : ""}</span>
                                </button>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}

                    {trace.decision.rationale && (
                      <p className="text-[13.5px] leading-relaxed text-ink-2">{trace.decision.rationale}</p>
                    )}

                    <div className="flex flex-wrap items-center gap-x-2 gap-y-2 text-[12.5px] text-ink-3">
                      {trace.actor_kind === "User" && trace.actor && <Avatar name={trace.actor} size={20} />}
                      <span>Decided by <span className="font-medium text-ink-2">{trace.actor || "unknown"}</span>
                        {trace.actor_kind && trace.actor_kind !== "User" ? ` (${trace.actor_kind.toLowerCase()})` : ""}</span>
                      <span aria-hidden>·</span>
                      <PolicyBadge policy={trace.decision.policy} />
                      {trace.rule && (<><span aria-hidden>·</span><span>Applied <span className="num text-ink-2">{trace.rule}</span></span></>)}
                      {(trace.supersedes || []).length > 0 && (
                        <><span aria-hidden>·</span><span>Supersedes <span className="num text-ink-2">{trace.supersedes.join(", ")}</span></span></>
                      )}
                    </div>
                  </div>
                )}
              </>
            )}
          </Card>

          {/* -------------------------------------------------------------- learned rules */}
          <Card title="Learned rules" pad={false} className="overflow-hidden"
            action={conf.rules.length ? <span className="num text-[12.5px] text-ink-3">{conf.rules.length}</span> : undefined}>
            {conf.rules.length ? (
              <div className="scroll-thin contain-scroll overflow-x-auto pb-1">
                <table className={clsx("kt", EDGE_CELLS)}>
                  <thead>
                    <tr>
                      <th>Rule</th>
                      <th>Prefer</th>
                      <th>Over</th>
                      <th>Predicate</th>
                      <th className="text-right">Applied</th>
                    </tr>
                  </thead>
                  <tbody>
                    {conf.rules.map((r: any) => (
                      <tr key={r.id} title={r.rationale || undefined}>
                        <td className="num text-[12.5px] text-ink-2">{r.id}</td>
                        <td className="font-medium text-ink">{r.prefer}</td>
                        <td className="text-ink-2">{r.over}</td>
                        <td className="num text-[12.5px] text-ink-2">{r.predicate}</td>
                        <td className="num text-right font-semibold text-ink">{r.applied}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty title="No learned rules yet"
                text={<>Resolve a conflict with “Choose and learn rule” on <Link href="/live/" className="font-medium text-kb-ink hover:underline">Live documents</Link>.</>} />
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}
