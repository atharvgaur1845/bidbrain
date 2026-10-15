"use client";

import clsx from "clsx";
import { BookMarked, Bot, FileText, User } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import GraphCanvas, { GData } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { Panel, useOpenDoc } from "@/components/ui";
import { api } from "@/lib/api";

const POLICY_COLOR: Record<string, string> = {
  authority: "var(--info)", recency_same_issuer: "var(--pass)", learned_rule: "var(--violet)", human: "var(--accent)",
};

export default function DecisionsPage() {
  const [conf, setConf] = useState<any>({ decisions: [], rules: [] });
  const [filter, setFilter] = useState<string>("all");
  const [sel, setSel] = useState<string | null>(null);
  const [trace, setTrace] = useState<any>(null);
  const openDoc = useOpenDoc();

  useEffect(() => { api("/api/conflicts").then(setConf); }, []);
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

  return (
    <div>
      <PageHeader eyebrow="Tracing decisions back to their source" title="Every settled fact has a reason, a decider, and a paper trail">
        Nothing is overwritten. When sources disagree, the winning claim is selected by a Decision node that records the policy,
        the rationale, who decided, and the documents both sides came from. Decisions supersede earlier decisions, and learned rules cite the decision they came from.
      </PageHeader>
      <div className="mb-4 flex flex-wrap gap-2">
        {["all", "authority", "recency_same_issuer", "learned_rule", "human"].map((p) => (
          <button key={p} onClick={() => setFilter(p)} className={clsx("chip !px-3 !py-1 !text-[12px]", filter === p && "!border-accent !text-ink")}>
            <span className="h-2 w-2 rounded-full" style={{ background: POLICY_COLOR[p] || "var(--text-3)" }} />
            {p.replace(/_/g, " ")} <span className="num text-ink-3">{p === "all" ? conf.decisions.length : counts[p] || 0}</span>
          </button>
        ))}
      </div>
      <div className="grid gap-5 lg:grid-cols-12">
        <Panel title="Decision log" className="lg:col-span-5" pad={false}>
          <div className="scroll-thin max-h-[70vh] overflow-y-auto">
            {list.map((d: any) => (
              <button key={d.id} onClick={() => setSel(d.id)}
                className={clsx("flex w-full items-start gap-3 border-b hairline px-4 py-2.5 text-left hover:bg-bg-2", sel === d.id && "bg-bg-2")}>
                {d.actor_kind === "User" ? <User size={15} className="mt-0.5 text-accent" /> : <Bot size={15} className="mt-0.5 text-ink-3" />}
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-medium">{d.subject} <span className="num text-ink-3">· {d.predicate}</span></span>
                  <span className="block truncate text-[12px] text-ink-3">{d.rationale}</span>
                </span>
                <span className="chip !py-0 !text-[10.5px]" style={{ color: POLICY_COLOR[d.policy] }}>{d.policy.replace(/_/g, " ")}</span>
              </button>
            ))}
            {list.length === 0 && <div className="p-4 text-[13px] text-ink-3">No decisions of this kind yet.</div>}
          </div>
        </Panel>
        <div className="space-y-4 lg:col-span-7">
          <Panel title="Decision trace" pad={false}>
            <div className="p-2"><GraphCanvas data={graph} highlight={new Set(graph.nodes.map((n) => n.id))} height={320} emptyHint="Select a decision to trace it." /></div>
          </Panel>
          {trace && (
            <Panel title={trace.decision.id}>
              <div className="text-[13.5px]"><span className="font-semibold">{trace.subject}</span> · <span className="num">{trace.decision.predicate}</span> → <span className="num font-bold text-accent">{trace.decision.chosen_value}</span></div>
              <div className="mt-1 text-[12.5px] text-ink-2">{trace.decision.rationale}</div>
              <div className="mt-1 text-[12px] text-ink-3">decided by <span className="font-semibold">{trace.actor}</span> ({trace.actor_kind}) · policy {trace.decision.policy}{trace.rule ? ` · applied ${trace.rule}` : ""}</div>
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                {(trace.claims || []).filter((c: any) => c.claim).map((c: any) => (
                  <div key={c.claim} className={clsx("panel-2 px-3 py-2", c.claim === trace.winner && "!border-accent")}>
                    <div className="num text-[15px] font-bold">{c.value}</div>
                    <div className="text-[12px] text-ink-3">{c.kind} · authority {c.authority} · {c.issuer}</div>
                    <div className="text-[12px] text-ink-3">status: {c.status}</div>
                    {c.doc && <button className="num mt-1 inline-flex items-center gap-1 text-[12px] text-accent" onClick={() => openDoc({ doc_id: c.doc, page: c.page })}><FileText size={12} />{c.doc}{c.page ? ` p.${c.page}` : ""}</button>}
                  </div>
                ))}
              </div>
            </Panel>
          )}
          <Panel title="Learned rules">
            {conf.rules.length ? conf.rules.map((r: any) => (
              <div key={r.id} className="flex items-center gap-2 py-1 text-[12.5px]"><BookMarked size={13} className="text-violet" />
                <span className="num">{r.id}</span> prefer <b>{r.prefer}</b> over {r.over} for <span className="num">{r.predicate}</span>
                <span className="ml-auto text-ink-3">applied {r.applied}×</span></div>
            )) : <div className="text-[12.5px] text-ink-3">Resolve a conflict with “Choose + learn rule” on the Live documents page.</div>}
          </Panel>
        </div>
      </div>
    </div>
  );
}
