"use client";

// Impact Room dependency graph: the real subgraph a document touched, laid out left to right
// (source documents -> claims -> decision -> requirement -> tender -> action), plus an inspector.
// Data: GET /api/views/impact-graph/{doc}; when that route is not deployed yet, the same chain is composed from
// /api/impact/{doc}, /api/decisions/{id}/trace and /api/cases/{id} (all read from the graph).

import clsx from "clsx";
import { ArrowRight, ExternalLink, FileText } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { Badge, Button, EmptyState, ErrorState, SkeletonRows, StatusBadge, useOpenDoc } from "@/components/ui";
import { api } from "@/lib/api";

export type VNode = { id: string; label: string; name: string; props: Record<string, any>; focus?: boolean };
export type VEdge = { id: string; source: string; target: string; type: string; props: Record<string, any> };
export type VGraph = { nodes: VNode[]; edges: VEdge[]; composed?: boolean };

// column per node kind: evidence | claims | decision | requirement | bid | action
const COL: Record<string, number> = { Document: 0, Claim: 1, Work: 2, Decision: 2, Agent: 2, PolicyRule: 2, OrgRuleRef: 2,
  Finding: 3, Clause: 3, Case: 4, Tender: 4, Action: 5 };
const COLS = ["Source documents", "Claims", "Fact and decision", "Requirement", "Bid", "Action"];
// role -> stripe colour (one meaning each: evidence grey, entity blue, decision violet, bid navy, action amber)
const ROLE: Record<string, { role: string; color: string }> = {
  Document: { role: "Evidence", color: "#8a8f98" }, Claim: { role: "Evidence", color: "#8a8f98" },
  Work: { role: "Entity", color: "#3d6fb6" }, Tender: { role: "Entity", color: "#3d6fb6" },
  Decision: { role: "Decision", color: "#6b5bb5" }, Agent: { role: "Decision", color: "#6b5bb5" }, PolicyRule: { role: "Decision", color: "#6b5bb5" },
  Finding: { role: "Decision", color: "#6b5bb5" }, Clause: { role: "Requirement", color: "#2f8a8a" },
  Case: { role: "Bid", color: "#2b4c9b" }, Action: { role: "Action", color: "#b7791f" },
};
const roleOf = (l: string) => ROLE[l] || { role: l, color: "#a3a7ae" };

const NODE_H = 42, VGAP = 10, TOP = 30, HGAP = 18, MIN_W = 112, MAX_W = 190;

// ------------------------------------------------------------------ fallback composition (real reads, no invention)

type Trace = { decision: Record<string, any>; actor?: string; actor_kind?: string; winner?: string;
  claims: { claim: string; value: string; kind: string; authority: number; issuer: string; doc: string; page?: number; status: string }[];
  subject_label?: string; subject_key?: string; subject?: string };

async function compose(r: any): Promise<VGraph> {
  const nodes = new Map<string, VNode>(), edges: VEdge[] = [];
  const N = (id: string, label: string, name: string, props: Record<string, any>, focus = false) => {
    if (!nodes.has(id)) nodes.set(id, { id, label, name, props, focus });
  };
  const E = (source: string, target: string, type: string, props: Record<string, any> = {}) => {
    const id = `${source}|${type}|${target}`;
    if (!edges.some((e) => e.id === id)) edges.push({ id, source, target, type, props });
  };
  const d = r.document;
  N(`doc:${d.doc_id}`, "Document", d.title || d.doc_id, d, true);
  for (const c of r.claims) {
    N(`claim:${c.claim}`, "Claim", `${c.predicate} = ${c.value}`, { id: c.claim, predicate: c.predicate, value_str: c.value, status: c.status,
      page: c.page, valid_from: c.valid_from, recorded: c.recorded });
    E(`claim:${c.claim}`, `doc:${d.doc_id}`, "FROM", { page: c.page });
    if (c.subject_key) {
      N(`${c.subject_label}:${c.subject_key}`, c.subject_label || "Entity", c.subject || c.subject_key, { key: c.subject_key });
      E(`claim:${c.claim}`, `${c.subject_label}:${c.subject_key}`, "ABOUT");
    }
  }
  const traces = await Promise.all((r.decisions as string[]).map((id) => api<Trace>(`/api/decisions/${encodeURIComponent(id)}/trace`).catch(() => null)));
  for (const t of traces) {
    if (!t) continue;
    const did = `dec:${t.decision.id}`;
    N(did, "Decision", t.decision.id, t.decision);
    if (t.actor) { N(`agent:${t.actor}`, t.actor_kind || "Agent", t.actor, { key: t.actor }); E(did, `agent:${t.actor}`, "DECIDED_BY"); }
    for (const c of t.claims) {
      if (!c.claim) continue;
      N(`claim:${c.claim}`, "Claim", `value = ${c.value}`, { id: c.claim, value_str: c.value, source_kind: c.kind, authority: c.authority,
        issuer: c.issuer, status: c.status, source_doc: c.doc, page: c.page });
      if (c.doc) { N(`doc:${c.doc}`, "Document", c.doc, { doc_id: c.doc }); E(`claim:${c.claim}`, `doc:${c.doc}`, "FROM", { page: c.page }); }
      E(did, `claim:${c.claim}`, c.claim === t.winner ? "SELECTS" : "BASED_ON");
      if (t.winner && c.claim !== t.winner) E(`claim:${t.winner}`, `claim:${c.claim}`, "PREVAILS_OVER", { decision: t.decision.id });
    }
  }
  const caseIds = [...new Set((r.affected as any[]).map((a) => a.case_id))];
  const cases = await Promise.all(caseIds.map((id) => api<any>(`/api/cases/${encodeURIComponent(id)}`).catch(() => null)));
  for (const a of r.affected) {
    const cs = cases.find((x) => x?.case?.id === a.case_id);
    const cl = cs?.clauses?.find((x: any) => x.ref === a.clause);
    const cid = `case:${a.case_id}`, clid = `clause:${a.case_id}:${a.clause}`;
    N(cid, "Case", a.case, { id: a.case_id, title: a.case, status: cs?.case?.status, bid_date: cs?.case?.bid_date, tender_ref: cs?.case?.tender_ref });
    N(clid, "Clause", `Clause ${a.clause} · ${(a.kind || "").replace(/_/g, " ")}`, cl ? { ref: cl.ref, kind: cl.kind, verdict: cl.verdict,
      basis: cl.basis, text: cl.text, params: cl.params } : { ref: a.clause, kind: a.kind, verdict: a.verdict_now });
    E(cid, clid, "HAS_CLAUSE");
    E(clid, `dec:${a.decision}`, "HAS_FINDING → CAUSED_BY", { what: a.what, severity: a.severity });
    if (cs?.case?.tender_ref) { N(`tender:${cs.case.tender_ref}`, "Tender", cs.case.tender_ref, { ref: cs.case.tender_ref }); E(cid, `tender:${cs.case.tender_ref}`, "FOR_TENDER"); }
  }
  for (const a of r.actions) {
    N(`act:${a.id}`, "Action", a.title, a);
    E(`act:${a.id}`, `dec:${a.decision}`, "TRIGGERED_BY");
    if (a.case_id) E(`act:${a.id}`, `case:${a.case_id}`, "FOR_CASE");
  }
  const ids = new Set(nodes.keys());
  return { nodes: [...nodes.values()], edges: edges.filter((e) => ids.has(e.source) && ids.has(e.target)), composed: true };
}

export function useImpactGraph(docId: string, result: any) {
  const [g, setG] = useState<VGraph | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setG(null); setErr(null);
    api<VGraph>(`/api/views/impact-graph/${encodeURIComponent(docId)}`)
      .then((x) => { if (!Array.isArray(x?.nodes)) throw new Error("not deployed"); return x; })
      .catch(() => compose(result))
      .then((x) => { if (live) setG(x); })
      .catch((e) => { if (live) setErr(String(e?.message || e)); });
    return () => { live = false; };
  }, [docId, result]);
  return { g, err };
}

// ------------------------------------------------------------------ helpers

const fmtVal = (v: any): string => {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "object") return JSON.stringify(v);
  if (typeof v === "number" && v > 1e12 && v < 4e12) return new Date(v).toISOString().replace("T", " ").slice(0, 19) + " UTC";
  return String(v);
};
const pageOf = (n: VNode, edges: VEdge[]) =>
  n.label === "Claim" ? (n.props.page ?? edges.find((e) => e.source === n.id && e.type === "FROM")?.props.page)
    : n.label === "Document" ? edges.find((e) => e.target === n.id && e.type === "FROM")?.props.page : undefined;
const docOf = (n: VNode, nodes: VNode[], edges: VEdge[]) => {
  if (n.label === "Document") return n.props.doc_id as string | undefined;
  if (n.label === "Claim") {
    const e = edges.find((x) => x.source === n.id && x.type === "FROM");
    return (e && nodes.find((m) => m.id === e.target)?.props.doc_id) || n.props.source_doc;
  }
};

// connected component along the chain from a node (both directions), for highlighting
function chainOf(id: string, edges: VEdge[]) {
  const up = new Set([id]), down = new Set([id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const e of edges) {
      if (down.has(e.source) && !down.has(e.target)) { down.add(e.target); grew = true; }
      if (up.has(e.target) && !up.has(e.source)) { up.add(e.source); grew = true; }
    }
  }
  return new Set([...up, ...down]);
}

// ------------------------------------------------------------------ graph + inspector

type Sel = { kind: "node"; id: string } | { kind: "edge"; id: string } | null;

export function ImpactGraph({ g }: { g: VGraph }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(900);
  const [sel, setSel] = useState<Sel>(null);
  const [hover, setHover] = useState<string | null>(null);
  useEffect(() => {
    const el = wrap.current; if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.floor(e.contentRect.width)));
    ro.observe(el); return () => ro.disconnect();
  }, []);

  const layout = useMemo(() => {
    const cols: VNode[][] = COLS.map(() => []);
    for (const n of g.nodes) cols[COL[n.label] ?? 2].push(n);
    // inside a column: focus doc first, then active before superseded, then name
    for (const c of cols) c.sort((a, b) => Number(!!b.focus) - Number(!!a.focus)
      || Number(a.props.status === "superseded") - Number(b.props.status === "superseded") || a.label.localeCompare(b.label) || a.name.localeCompare(b.name));
    const used = cols.map((c, i) => (c.length ? i : -1)).filter((i) => i >= 0);
    const nw = Math.max(MIN_W, Math.min(MAX_W, Math.floor((w - 24 - HGAP * (used.length - 1)) / Math.max(1, used.length))));
    const W = Math.max(w, 24 + used.length * nw + HGAP * (used.length - 1));
    const step = used.length > 1 ? (W - nw - 24) / (used.length - 1) : 0;
    const pos = new Map<string, { x: number; y: number }>();
    const rows = Math.max(1, ...cols.map((c) => c.length));
    const H = TOP + rows * (NODE_H + VGAP) + 8;
    used.forEach((ci, k) => {
      const col = cols[ci];
      const colH = col.length * (NODE_H + VGAP) - VGAP;
      const y0 = TOP + (H - TOP - 8 - colH) / 2;
      col.forEach((n, j) => pos.set(n.id, { x: 12 + k * step, y: y0 + j * (NODE_H + VGAP) }));
    });
    return { pos, used, step, H, nw, W };
  }, [g, w]);

  const selNode = sel?.kind === "node" ? g.nodes.find((n) => n.id === sel.id) : undefined;
  const selEdge = sel?.kind === "edge" ? g.edges.find((e) => e.id === sel.id) : undefined;
  const lit = useMemo(() => {
    const focus = hover || (selNode?.id) || (selEdge ? selEdge.source : null);
    return focus ? chainOf(focus, g.edges) : null;
  }, [hover, selNode, selEdge, g.edges]);

  const byId = useMemo(() => new Map(g.nodes.map((n) => [n.id, n])), [g.nodes]);

  return (
    <div className="grid min-h-0 min-[1400px]:grid-cols-[minmax(0,1fr)_300px]">
      <div ref={wrap} className="min-w-0 border-b border-line min-[1400px]:border-b-0 min-[1400px]:border-r">
        <div className="scroll-thin overflow-x-auto"><svg width={layout.W} height={layout.H} role="group" aria-label="Dependency graph" className="block select-none"
          onClick={(e) => { if (e.target === e.currentTarget) setSel(null); }}>
          <defs>
            <marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M0,0 L8,4 L0,8 z" fill="var(--line-strong, #c9ccd1)" />
            </marker>
            <marker id="arr-on" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M0,0 L8,4 L0,8 z" fill="var(--kb, #2b4c9b)" />
            </marker>
          </defs>
          {layout.used.map((ci, k) => (
            <text key={ci} x={12 + k * layout.step} y={16} className="fill-[var(--ink-3,#6b7079)] text-[11px] font-medium">{COLS[ci]}</text>
          ))}
          {/* edges, drawn left to right regardless of stored direction */}
          {g.edges.map((e) => {
            const a = layout.pos.get(e.source), b = layout.pos.get(e.target);
            if (!a || !b) return null;
            const fwd = a.x <= b.x;
            const [p, q] = fwd ? [a, b] : [b, a];
            const same = Math.abs(p.x - q.x) < 2;
            const x1 = same ? p.x + layout.nw : p.x + layout.nw, y1 = p.y + NODE_H / 2;
            const x2 = same ? q.x + layout.nw : q.x, y2 = q.y + NODE_H / 2;
            const dx = same ? 36 : Math.max(30, (x2 - x1) / 2);
            const d = same ? `M${x1},${y1} C${x1 + dx},${y1} ${x2 + dx},${y2} ${x2},${y2}` : `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
            const on = selEdge?.id === e.id || (lit ? lit.has(e.source) && lit.has(e.target) : false);
            const dim = lit && !on;
            const mx = (x1 + x2) / 2 + (same ? dx * 0.75 : 0), my = (y1 + y2) / 2;
            const showLabel = selEdge?.id === e.id;
            return (
              <g key={e.id} className="cursor-pointer" onClick={() => setSel({ kind: "edge", id: e.id })}>
                <path d={d} fill="none" stroke="transparent" strokeWidth={10} />
                <path d={d} fill="none" stroke={on ? "var(--kb, #2b4c9b)" : "var(--line-strong, #c9ccd1)"} strokeWidth={selEdge?.id === e.id ? 2 : on ? 1.5 : 1}
                  opacity={dim ? 0.35 : 1} markerEnd={fwd ? `url(#${on ? "arr-on" : "arr"})` : undefined}
                  markerStart={!fwd ? `url(#${on ? "arr-on" : "arr"})` : undefined} />
                <title>{`${byId.get(e.source)?.name} —${e.type}→ ${byId.get(e.target)?.name}`}</title>
                {showLabel && (
                  <g transform={`translate(${mx},${my})`} pointerEvents="none">
                    <rect x={-(e.type.length * 3.3 + 6)} y={-8} width={e.type.length * 6.6 + 12} height={16} rx={3} fill="#fff" stroke="var(--line, #e3e4e7)" />
                    <text textAnchor="middle" y={4} className="mono fill-[var(--ink-2,#3b3f46)] text-[10px]">{e.type}</text>
                  </g>
                )}
              </g>
            );
          })}
          {g.nodes.map((n) => {
            const p = layout.pos.get(n.id); if (!p) return null;
            const r = roleOf(n.label);
            const isSel = selNode?.id === n.id;
            const dim = lit && !lit.has(n.id);
            const sup = n.props.status === "superseded";
            const status = n.label === "Clause" ? n.props.verdict : n.label === "Case" ? n.props.status : n.label === "Action" ? n.props.status : n.label === "Claim" ? n.props.status : undefined;
            return (
              <g key={n.id} transform={`translate(${p.x},${p.y})`} tabIndex={0} role="button" aria-pressed={isSel}
                aria-label={`${n.label}: ${n.name}`} className="cursor-pointer outline-none [&:focus-visible>rect.frame]:stroke-[var(--kb)] [&:focus-visible>rect.frame]:stroke-2"
                opacity={dim ? 0.4 : 1}
                onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)}
                onFocus={() => setHover(n.id)} onBlur={() => setHover(null)}
                onClick={() => setSel({ kind: "node", id: n.id })}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSel({ kind: "node", id: n.id }); } }}>
                <rect className="frame" width={layout.nw} height={NODE_H} rx={5} fill={isSel ? "var(--kb-soft, #eef2fa)" : "#fff"}
                  stroke={isSel ? "var(--kb, #2b4c9b)" : n.focus ? "var(--ink-3, #6b7079)" : "var(--line, #e3e4e7)"} strokeWidth={isSel ? 1.5 : 1}
                  strokeDasharray={sup ? "3 2" : undefined} />
                <rect x={0} y={0} width={3} height={NODE_H} rx={1.5} fill={r.color} />
                <text x={10} y={15} className="fill-[var(--ink-3,#6b7079)] text-[10.5px]">
                  {n.label}{status ? ` · ${String(status).toLowerCase().replace(/_/g, " ")}` : ""}
                </text>
                <text x={10} y={31} className={clsx("text-[12px] font-medium", sup ? "fill-[var(--ink-3,#6b7079)]" : "fill-[var(--ink,#1b1c1f)]")}>
                  {(() => { const m = Math.floor((layout.nw - 16) / 6.6); return n.name.length > m ? n.name.slice(0, m - 1) + "…" : n.name; })()}
                </text>
                <title>{`${n.label}: ${n.name}`}</title>
              </g>
            );
          })}
        </svg></div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line px-3 py-2 text-[11.5px] text-ink-3">
          {["Evidence", "Entity", "Decision", "Requirement", "Bid", "Action"].map((role) => (
            <span key={role} className="flex items-center gap-1.5">
              <span className="h-2.5 w-[3px] rounded-sm" style={{ background: Object.values(ROLE).find((x) => x.role === role)?.color }} />{role}
            </span>
          ))}
          <span className="flex items-center gap-1.5"><span className="h-2.5 w-4 rounded-sm border border-dashed border-ink-3" />Superseded</span>
          <span className="ml-auto">{g.nodes.length} nodes · {g.edges.length} relationships{g.composed ? " · composed from impact, trace and case reads" : ""}</span>
        </div>
      </div>
      <Inspector g={g} node={selNode} edge={selEdge} onSelect={setSel} />
    </div>
  );
}

function Inspector({ g, node, edge, onSelect }: { g: VGraph; node?: VNode; edge?: VEdge; onSelect: (s: Sel) => void }) {
  const open = useOpenDoc();
  const byId = new Map(g.nodes.map((n) => [n.id, n]));
  if (!node && !edge) {
    return (
      <aside aria-label="Inspector" className="p-4 text-[12.5px] text-ink-3">
        <div className="font-medium text-ink-2">Inspector</div>
        <p className="mt-1 leading-relaxed">Select a node or a line. Every box is a stored graph node and every line a stored relationship: you get its properties, its neighbours and, for evidence, the source page.</p>
      </aside>
    );
  }
  if (edge) {
    const a = byId.get(edge.source), b = byId.get(edge.target);
    return (
      <aside aria-label="Inspector" className="flex flex-col gap-3 p-4">
        <div>
          <div className="text-[11px] text-ink-3">Relationship</div>
          <div className="mono text-[13px] font-semibold text-ink">{edge.type}</div>
        </div>
        <div className="flex flex-col gap-1 text-[12.5px]">
          {[a, b].map((n, i) => n && (
            <button key={i} type="button" onClick={() => onSelect({ kind: "node", id: n.id })}
              className="flex items-center gap-2 rounded-md border border-line px-2 py-1.5 text-left hover:border-line-strong hover:bg-surface-2">
              <span className="w-10 shrink-0 text-[11px] text-ink-3">{i ? "to" : "from"}</span>
              <span className="min-w-0 flex-1 truncate text-ink">{n.name}</span>
              <span className="text-[11px] text-ink-3">{n.label}</span>
            </button>
          ))}
        </div>
        <Props props={edge.props} empty="This relationship stores no properties." />
      </aside>
    );
  }
  const n = node!;
  const rels = g.edges.filter((e) => e.source === n.id || e.target === n.id);
  const doc = docOf(n, g.nodes, g.edges);
  const page = pageOf(n, g.edges);
  const link = n.label === "Decision" ? `/decisions/?id=${encodeURIComponent(n.props.id)}`
    : n.label === "Case" ? `/qualify/?case=${encodeURIComponent(n.props.id)}`
    : n.label === "Action" ? `/actions/?id=${encodeURIComponent(n.props.id)}`
    : n.label === "Work" || n.label === "Tender" ? `/explore/?label=${n.label}&key=${encodeURIComponent(n.props.key || n.props.ref || "")}` : null;
  return (
    <aside aria-label="Inspector" className="scroll-thin flex max-h-[460px] flex-col gap-3 overflow-y-auto p-4">
      <div>
        <div className="flex items-center gap-2 text-[11px] text-ink-3">
          <span className="h-2.5 w-[3px] rounded-sm" style={{ background: roleOf(n.label).color }} />{n.label} · {roleOf(n.label).role}
        </div>
        <div className="mt-0.5 text-[13.5px] font-semibold leading-snug text-ink">{n.name}</div>
      </div>
      <div className="flex flex-wrap gap-2">
        {doc && <Button size="sm" onClick={() => open({ doc_id: doc, page })}><FileText size={13} strokeWidth={1.75} aria-hidden /> Open {doc}{page ? ` p.${page}` : ""}</Button>}
        {link && <Link href={link} className="inline-flex items-center gap-1 text-[12.5px] text-kb-ink hover:underline">Open <ExternalLink size={12} aria-hidden /></Link>}
      </div>
      <Props props={n.props} />
      <div>
        <div className="mb-1 text-[11px] font-medium text-ink-3">Relationships ({rels.length})</div>
        <ul className="flex flex-col gap-1">
          {rels.map((e) => {
            const out = e.source === n.id;
            const m = byId.get(out ? e.target : e.source);
            return (
              <li key={e.id}>
                <button type="button" onClick={() => m && onSelect({ kind: "node", id: m.id })}
                  className="flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left text-[12px] hover:bg-surface-2">
                  <span className="mono shrink-0 text-[10.5px] text-ink-3">{out ? "→" : "←"} {e.type}</span>
                  <span className="min-w-0 flex-1 truncate text-ink">{m?.name}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </aside>
  );
}

function Clamp({ text }: { text: string }) {
  const [more, setMore] = useState(false);
  if (text.length <= 160) return <>{text}</>;
  return (
    <>
      {more ? text : text.slice(0, 150) + "…"}{" "}
      <button type="button" className="text-kb-ink hover:underline" onClick={() => setMore((m) => !m)}>{more ? "less" : "more"}</button>
    </>
  );
}

function Props({ props, empty }: { props: Record<string, any>; empty?: string }) {
  const rows = Object.entries(props || {}).filter(([k, v]) => v !== null && v !== undefined && v !== "" && k !== "key");
  if (!rows.length) return <p className="text-[12.5px] text-ink-3">{empty || "No stored properties."}</p>;
  return (
    <dl className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-2 gap-y-1 border-t border-line pt-2 text-[12px]">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="mono truncate text-[11px] text-ink-3" title={k}>{k}</dt>
          <dd className="min-w-0 break-words text-ink-2"><Clamp text={fmtVal(v)} /></dd>
        </div>
      ))}
    </dl>
  );
}

export function GraphPanel({ docId, result }: { docId: string; result: any }) {
  const { g, err } = useImpactGraph(docId, result);
  if (err) return <ErrorState compact text={err} />;
  if (!g) return <SkeletonRows rows={6} />;
  if (!g.nodes.length) return <EmptyState compact title="Nothing in the graph references this document yet" />;
  return <ImpactGraph g={g} />;
}

// ------------------------------------------------------------------ before / after

export function VerdictCompare({ r, clauses }: { r: any; clauses: Record<string, any> }) {
  const open = useOpenDoc();
  if (!r.affected.length) return null;
  return (
    <div className="flex flex-col gap-3">
      {r.affected.map((a: any, i: number) => {
        const mv = (a.what as string).match(/(PASS|FAIL|AT_RISK|NEEDS[_ ]REVIEW)\s*->\s*(PASS|FAIL|AT_RISK|NEEDS[_ ]REVIEW)/);
        const cl = clauses[`${a.case_id}:${a.clause}`];
        const claim = r.claims.find((c: any) => c.decision === a.decision) || r.claims[0];
        const act = r.actions.find((x: any) => x.case_id === a.case_id && x.kind === "review_bid") || r.actions.find((x: any) => x.decision === a.decision);
        return (
          <div key={i} className="overflow-hidden rounded-md border border-line">
            <div className="flex flex-wrap items-center gap-2 border-b border-line bg-surface-2 px-3 py-2 text-[12.5px]">
              <Link href={`/qualify/?case=${encodeURIComponent(a.case_id)}`} className="font-medium text-kb-ink hover:underline">{a.case}</Link>
              <span className="text-ink-3">· clause {a.clause} {String(a.kind).replace(/_/g, " ")}</span>
              {a.severity && <Badge tone={a.severity === "high" ? "bad" : "warn"} className="ml-auto">{a.severity} severity</Badge>}
            </div>
            <div className="grid grid-cols-[96px_1fr_24px_1fr] items-center gap-x-3 gap-y-2 px-3 py-2.5 text-[12.5px]">
              <span />
              <span className="text-[11px] font-medium text-ink-3">Before</span><span />
              <span className="text-[11px] font-medium text-ink-3">After</span>
              <span className="text-ink-3">Fact</span>
              <span className="num text-ink-3 line-through">{claim?.replaced?.join(", ") || "—"}</span>
              <ArrowRight size={13} className="text-ink-3" aria-hidden />
              <span className="num font-medium text-ink">{claim?.chosen || claim?.value}</span>
              <span className="text-ink-3">Clause</span>
              <span>{mv ? <StatusBadge status={mv[1]} size="sm" /> : "—"}</span>
              <ArrowRight size={13} className="text-ink-3" aria-hidden />
              <span><StatusBadge status={a.verdict_now} size="sm" /></span>
              <span className="text-ink-3">Bid</span>
              <span>{a.case_before ? <StatusBadge status={a.case_before} size="sm" /> : "—"}</span>
              <ArrowRight size={13} className="text-ink-3" aria-hidden />
              <span><StatusBadge status={a.case_after || a.case_now} size="sm" /></span>
            </div>
            <dl className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-3 gap-y-1.5 border-t border-line px-3 py-2.5 text-[12.5px]">
              <dt className="text-ink-3">Business rule</dt>
              <dd className="text-ink-2">{cl?.text || <span className="text-ink-3">clause text not loaded</span>}</dd>
              <dt className="text-ink-3">Basis now</dt>
              <dd className="num text-ink-2">{cl?.basis || "—"}</dd>
              <dt className="text-ink-3">Evidence</dt>
              <dd>
                <button type="button" className="mono text-[12px] text-kb-ink hover:underline"
                  onClick={() => open({ doc_id: r.document.doc_id, page: claim?.page })}>
                  {r.document.doc_id}{claim?.page ? ` · p.${claim.page}` : ""}
                </button>
                <span className="text-ink-3"> · decided by </span>
                <Link className="mono text-[12px] text-kb-ink hover:underline" href={`/decisions/?id=${encodeURIComponent(a.decision)}`}>{a.decision}</Link>
              </dd>
              <dt className="text-ink-3">Action</dt>
              <dd className="text-ink-2">{act ? <><StatusBadge status={act.status} size="sm" /> <Link href={`/actions/?id=${encodeURIComponent(act.id)}`} className="text-kb-ink hover:underline">{act.title}</Link></> : "none raised"}</dd>
            </dl>
          </div>
        );
      })}
    </div>
  );
}

export function useCaseClauses(r: any) {
  const [m, setM] = useState<Record<string, any>>({});
  useEffect(() => {
    let live = true;
    const ids = [...new Set((r.affected as any[]).map((a) => a.case_id))];
    Promise.all(ids.map((id) => api<any>(`/api/cases/${encodeURIComponent(id)}`).catch(() => null))).then((xs) => {
      if (!live) return;
      const out: Record<string, any> = {};
      for (const x of xs) for (const c of x?.clauses || []) out[`${x.case.id}:${c.ref}`] = c;
      setM(out);
    });
    return () => { live = false; };
  }, [r]);
  return m;
}
