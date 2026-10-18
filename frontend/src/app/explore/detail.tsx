"use client";

// Explore detail panel: the selected node, its relationships in view, its provenance (fact_history:
// claims -> source page -> decision) and a one-click "follow the chain" that adds claim, document and
// decision nodes to the canvas.

import { FileText, GitBranch, Route } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { GData, GNode, LABEL_COLORS } from "@/components/GraphCanvas";
import { Badge, Button, Card, EmptyState, SkeletonRows, StatusBadge, useOpenDoc } from "@/components/ui";
import { api } from "@/lib/api";

// labels fact_history accepts, and the kind name it wants
const FACT_KIND: Record<string, string> = { Work: "work", Person: "person", Bond: "bond", FinancialStatement: "financial_statement", Credential: "credential" };
export const PATH_KIND: Record<string, string> = { Client: "client", Person: "person", Work: "work", Tender: "tender" };

type Hist = { claims: { claim: string; predicate: string; value: string; source: string; issuer: string; authority: number; status: string; doc: string; page?: number }[];
  decisions: { decision: string; predicate: string; policy: string; rationale: string; chosen: string; winning_doc?: string; decided_by?: string }[] };

export function chainGraph(subjectId: string, h: Hist): GData {
  const nodes: GData["nodes"] = [], edges: GData["edges"] = [];
  const seen = new Set<string>();
  const N = (id: string, label: string, name: string, extra: Record<string, any> = {}) => { if (!seen.has(id)) { seen.add(id); nodes.push({ id, label, name, ...extra }); } };
  for (const c of h.claims) {
    const cid = `Claim:${c.claim}`;
    N(cid, "Claim", `${c.predicate} = ${c.value}`, { status: c.status });
    edges.push({ source: cid, target: subjectId, type: "ABOUT" });
    if (c.doc) { N(`Document:${c.doc}`, "Document", c.doc); edges.push({ source: cid, target: `Document:${c.doc}`, type: "FROM" }); }
  }
  for (const d of h.decisions) {
    const did = `Decision:${d.decision}`;
    N(did, "Decision", `${d.decision} · ${d.policy.replace(/_/g, " ")}`);
    for (const c of h.claims.filter((x) => x.predicate === d.predicate)) {
      edges.push({ source: did, target: `Claim:${c.claim}`, type: c.status === "active" && c.doc === d.winning_doc ? "SELECTS" : "BASED_ON" });
    }
  }
  return { nodes, edges };
}

export function Detail({ node, graph, onExpand, onChain, onPath }:
  { node?: GNode; graph: GData; onExpand: (label: string, key: string) => void; onChain: (g: GData, hl: string[]) => void;
    onPath: (end: "a" | "b", name: string, kind: string) => void }) {
  const open = useOpenDoc();
  const [hist, setHist] = useState<Hist | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const label = node?.label || "";
  const key = node ? node.id.slice(label.length + 1) : "";

  useEffect(() => {
    setHist(null); setErr(null);
    if (!node || !FACT_KIND[label]) return;
    let live = true;
    setLoading(true);
    api(`/api/tool/fact_history`, { method: "POST", body: JSON.stringify({ key, kind: FACT_KIND[label] }) })
      .then((r) => { if (live) { if (r.error) setErr(r.error); else setHist(r.result); } })
      .catch((e) => { if (live) setErr(e.message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [node, label, key]);

  if (!node) {
    return <Card title="Selection"><EmptyState compact title="Nothing selected" text="Click a node to see its relationships and where its facts came from." /></Card>;
  }
  const rels = graph.edges.filter((e) => e.source === node.id || e.target === node.id);
  const byType = new Map<string, { id: string; name: string; out: boolean }[]>();
  for (const e of rels) {
    const out = e.source === node.id;
    const oid = out ? e.target : e.source;
    const o = graph.nodes.find((n) => n.id === oid);
    const k = `${out ? "→" : "←"} ${e.type}`;
    byType.set(k, [...(byType.get(k) || []), { id: oid, name: o?.name || oid, out }]);
  }
  const contested = hist ? new Set(hist.decisions.map((d) => d.predicate)) : new Set<string>();

  return (
    <Card title="Selection" subtitle={undefined}
      action={<span className="flex items-center gap-1.5 text-[12px] text-ink-3"><span className="h-2 w-2 rounded-full" style={{ background: LABEL_COLORS[label] || "#8a8f98" }} />{label}</span>}>
      <div className="flex flex-col gap-3">
        <div>
          <div className="text-[14px] font-semibold leading-snug text-ink">{node.name || key}</div>
          <div className="mono text-[11.5px] text-ink-3">{node.id}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          {label === "Document" && <Button size="sm" onClick={() => open({ doc_id: key })}><FileText size={13} strokeWidth={1.75} aria-hidden /> Open source</Button>}
          {label === "Decision" && <Link className="text-[12.5px] text-kb-ink hover:underline" href={`/decisions/?id=${encodeURIComponent(key.split(" ")[0])}`}>Decision trace</Link>}
          {PATH_KIND[label] && <>
            <Button size="sm" variant="ghost" onClick={() => onPath("a", node.name || key, PATH_KIND[label])}><Route size={13} strokeWidth={1.75} aria-hidden /> Path from</Button>
            <Button size="sm" variant="ghost" onClick={() => onPath("b", node.name || key, PATH_KIND[label])}>Path to</Button>
          </>}
        </div>

        {FACT_KIND[label] && (
          <div className="border-t border-line pt-3">
            <div className="mb-1.5 flex items-center gap-2">
              <span className="text-[12px] font-medium text-ink-2" title="Claims about this entity, their source pages and the decisions that settled them">Provenance</span>
              {hist && hist.claims.length > 0 && (
                <Button size="xs" className="ml-auto" onClick={() => {
                  const g = chainGraph(node.id, hist);
                  onChain(g, g.nodes.map((n) => n.id).concat(node.id));
                }}><GitBranch size={12} strokeWidth={1.75} aria-hidden /> Follow chain</Button>
              )}
            </div>
            {loading ? <SkeletonRows rows={3} /> : err ? <p className="text-[12.5px] text-bad">{err}</p>
              : !hist || !hist.claims.length ? <p className="text-[12.5px] text-ink-3">No claims recorded about this {label.toLowerCase()}.</p> : (
              <ul className="scroll-thin flex max-h-64 flex-col divide-y divide-line overflow-y-auto rounded-md border border-line">
                {hist.claims.filter((c) => contested.has(c.predicate) || hist.claims.length <= 8).slice(0, 12).map((c) => (
                  <li key={c.claim} className="px-2.5 py-1.5 text-[12px]">
                    <div className="flex items-baseline gap-2">
                      <span className="shrink-0 text-ink-3">{c.predicate.replace(/_/g, " ")}</span>
                      <span className="num min-w-0 flex-1 truncate font-medium text-ink" title={c.value}>{c.value}</span>
                      <StatusBadge status={c.status === "active" ? "resolved" : c.status} label={c.status} size="sm" />
                    </div>
                    <div className="mt-0.5 flex items-center gap-2 text-[11.5px] text-ink-3">
                      <span className="min-w-0 flex-1 truncate">{c.source.replace(/_/g, " ")} · authority {c.authority}</span>
                      {c.doc && <button type="button" onClick={() => open({ doc_id: c.doc, page: c.page })} title="Open the source page"
                        className="mono shrink-0 text-[11px] text-kb-ink hover:underline">{c.doc}{c.page ? ` p.${c.page}` : ""}</button>}
                    </div>
                  </li>
                ))}
              </ul>
            )}
            {hist && hist.decisions.length > 0 && (
              <ul className="mt-2 flex flex-col gap-1.5">
                {hist.decisions.map((d) => (
                  <li key={d.decision} className="text-[12px] leading-snug">
                    <Badge tone="review">{d.policy.replace(/_/g, " ")}</Badge>{" "}
                    <Link href={`/decisions/?id=${encodeURIComponent(d.decision)}`} className="mono text-[11.5px] text-kb-ink hover:underline">{d.decision}</Link>
                    {d.winning_doc ? <> · won by <button type="button" onClick={() => open({ doc_id: d.winning_doc! })} className="mono text-[11.5px] text-kb-ink hover:underline">{d.winning_doc}</button></> : null}
                    <p className="mt-0.5 text-ink-3">{d.rationale}</p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        <div className="border-t border-line pt-3">
          <div className="mb-1 text-[12px] font-medium text-ink-2">Relationships in view <span className="num font-normal text-ink-3">{rels.length}</span></div>
          {rels.length === 0 ? <p className="text-[12.5px] text-ink-3">None loaded. Expand the node to load its neighbours.</p> : (
            <ul className="scroll-thin flex max-h-56 flex-col gap-1.5 overflow-y-auto">
              {[...byType.entries()].map(([t, xs]) => (
                <li key={t} className="text-[12px]">
                  <span className="mono text-[11px] text-ink-3">{t}</span> <span className="num text-ink-3">({xs.length})</span>
                  <div className="mt-0.5 flex flex-wrap gap-1">
                    {xs.slice(0, 8).map((x) => {
                      const [l, ...rest] = x.id.split(":");
                      return (
                        <button key={x.id} type="button" title={`Expand ${x.name}`} onClick={() => onExpand(l, rest.join(":"))}
                          className="max-w-[220px] truncate rounded border border-line px-1.5 py-0.5 text-left text-ink-2 hover:border-line-strong hover:bg-surface-2">{x.name}</button>
                      );
                    })}
                    {xs.length > 8 && <span className="px-1 text-ink-3">+{xs.length - 8}</span>}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Card>
  );
}
