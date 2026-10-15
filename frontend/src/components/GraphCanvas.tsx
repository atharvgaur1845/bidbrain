"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState } from "react";

const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

export type GNode = { id: string; label: string; name?: string; [k: string]: any };
export type GEdge = { source: string; target: string; type: string };
export type GData = { nodes: GNode[]; edges: GEdge[] };

export const LABEL_COLORS: Record<string, string> = {
  Work: "#60a5fa", Client: "#f5b83d", Person: "#a78bfa", Document: "#8b9bb0", Credential: "#34d399",
  Claim: "#f472b6", Decision: "#fb7185", Tender: "#22d3ee", Bid: "#fbbf24", Case: "#e879f9",
  FinancialStatement: "#4ade80", Bond: "#fdba74", Category: "#64748b", Company: "#e6edf5",
  Certification: "#2dd4bf", Missing: "#fb7185", Clause: "#c084fc", Finding: "#f87171", Invoice: "#93c5fd",
};
const SIZE: Record<string, number> = { Client: 7, Person: 6, Work: 5, Case: 8, Company: 8, Tender: 6, Bid: 6, Missing: 5 };

type Props = {
  data: GData;
  highlight?: Set<string>;
  height?: number;
  onNodeClick?: (n: GNode) => void;
  emptyHint?: string;
};

export default function GraphCanvas({ data, highlight, height = 460, onNodeClick, emptyHint }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const fg = useRef<any>(null);
  const [width, setWidth] = useState(600);
  const store = useRef<Map<string, any>>(new Map());
  const [hover, setHover] = useState<string | null>(null);

  useEffect(() => {
    if (!wrap.current) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(280, Math.floor(e.contentRect.width))));
    ro.observe(wrap.current);
    return () => ro.disconnect();
  }, []);

  // keep node objects (and their simulated positions) stable as data grows
  const graph = useMemo(() => {
    const m = store.current;
    const ids = new Set(data.nodes.map((n) => n.id));
    for (const k of [...m.keys()]) if (!ids.has(k)) m.delete(k);
    const nodes = data.nodes.map((n) => {
      const prev = m.get(n.id);
      const obj = prev ? Object.assign(prev, n) : { ...n };
      m.set(n.id, obj);
      return obj;
    });
    const links = data.edges
      .filter((e) => ids.has(e.source as string) && ids.has(e.target as string))
      .map((e) => ({ source: e.source, target: e.target, type: e.type }));
    return { nodes, links };
  }, [data]);

  useEffect(() => {
    const t = setTimeout(() => fg.current?.zoomToFit(500, 50), 650);
    return () => clearTimeout(t);
  }, [graph.nodes.length]);

  useEffect(() => {
    const f = fg.current;
    if (!f) return;
    f.d3Force("charge")?.strength(-140);
    f.d3Force("link")?.distance(48);
  }, [graph]);

  const hl = highlight || new Set<string>();
  const labelsPresent = [...new Set(graph.nodes.map((n: any) => n.label))].slice(0, 9);
  const isHl = (id: string) => hl.has(id);
  const linkOnPath = (l: any) => {
    const s = typeof l.source === "object" ? l.source.id : l.source;
    const t = typeof l.target === "object" ? l.target.id : l.target;
    return hl.size > 0 && hl.has(s) && hl.has(t);
  };

  return (
    <div ref={wrap} className="relative w-full overflow-hidden rounded-xl" style={{ height, background: "var(--bg-2)" }}>
      {graph.nodes.length === 0 && (
        <div className="absolute inset-0 grid place-items-center text-[13px] text-ink-3">
          {emptyHint || "The graph the agent touches appears here."}
        </div>
      )}
      <ForceGraph2D
        ref={fg}
        width={width}
        height={height}
        graphData={graph}
        backgroundColor="rgba(0,0,0,0)"
        cooldownTicks={120}
        nodeRelSize={1}
        linkColor={(l: any) => (linkOnPath(l) ? "rgba(245,184,61,0.95)" : "rgba(140,155,175,0.28)")}
        linkWidth={(l: any) => (linkOnPath(l) ? 2.2 : 0.8)}
        linkDirectionalParticles={(l: any) => (linkOnPath(l) ? 3 : 0)}
        linkDirectionalParticleWidth={2.6}
        linkDirectionalParticleColor={() => "#ffd27a"}
        linkDirectionalArrowLength={(l: any) => (linkOnPath(l) ? 3.5 : 0)}
        linkDirectionalArrowRelPos={0.92}
        onNodeHover={(n: any) => setHover(n?.id ?? null)}
        onNodeClick={(n: any) => onNodeClick?.(n)}
        nodeCanvasObject={(node: any, ctx: CanvasRenderingContext2D, scale: number) => {
          const r = (SIZE[node.label] || 4) * (isHl(node.id) ? 1.35 : 1);
          const color = LABEL_COLORS[node.label] || "#94a3b8";
          const dim = hl.size > 0 && !isHl(node.id);
          ctx.globalAlpha = dim ? 0.35 : 1;
          if (node.label === "Missing") {
            ctx.setLineDash([2, 2]);
            ctx.strokeStyle = color;
            ctx.lineWidth = 1.4;
            ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 2 * Math.PI); ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = color; ctx.font = `bold ${r * 1.3}px sans-serif`; ctx.textAlign = "center";
            ctx.textBaseline = "middle"; ctx.fillText("?", node.x, node.y + 0.5);
          } else {
            if (isHl(node.id)) {
              ctx.beginPath(); ctx.arc(node.x, node.y, r + 3.5, 0, 2 * Math.PI);
              ctx.fillStyle = "rgba(245,184,61,0.18)"; ctx.fill();
            }
            ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
            ctx.fillStyle = color; ctx.fill();
            ctx.lineWidth = 1; ctx.strokeStyle = "rgba(10,13,18,0.9)"; ctx.stroke();
          }
          const showLabel = isHl(node.id) || hover === node.id || scale > 2.2 || ["Client", "Case"].includes(node.label);
          if (showLabel && node.name) {
            const fs = Math.max(3.2, 11 / scale);
            ctx.font = `${isHl(node.id) ? 600 : 500} ${fs}px Inter, sans-serif`;
            ctx.textAlign = "center"; ctx.textBaseline = "top";
            const text = String(node.name).length > 42 ? String(node.name).slice(0, 40) + "…" : String(node.name);
            ctx.fillStyle = dim ? "rgba(167,180,196,0.5)" : "rgba(230,237,245,0.95)";
            ctx.fillText(text, node.x, node.y + r + 2);
          }
          ctx.globalAlpha = 1;
        }}
        nodePointerAreaPaint={(node: any, color: string, ctx: CanvasRenderingContext2D) => {
          ctx.fillStyle = color; ctx.beginPath(); ctx.arc(node.x, node.y, (SIZE[node.label] || 4) + 3, 0, 2 * Math.PI); ctx.fill();
        }}
      />
      {labelsPresent.length > 0 && (
        <div className="pointer-events-none absolute bottom-2 left-2 flex flex-wrap gap-1.5">
          {labelsPresent.map((l) => (
            <span key={l} className="chip" style={{ background: "color-mix(in oklab, var(--panel) 85%, transparent)" }}>
              <span className="h-2 w-2 rounded-full" style={{ background: LABEL_COLORS[l] || "#94a3b8" }} />
              {l}
            </span>
          ))}
        </div>
      )}
      <div className="pointer-events-none absolute right-2 top-2 chip num" style={{ background: "color-mix(in oklab, var(--panel) 85%, transparent)" }}>
        {graph.nodes.length} nodes · {graph.links.length} edges
      </div>
    </div>
  );
}

export function mergeGraph(acc: GData, add?: Partial<GData> | null): GData {
  if (!add) return acc;
  const nodes = [...acc.nodes];
  const ids = new Set(nodes.map((n) => n.id));
  for (const n of add.nodes || []) if (!ids.has(n.id)) { nodes.push(n); ids.add(n.id); }
  const ek = new Set(acc.edges.map((e) => `${e.source}|${e.target}|${e.type}`));
  const edges = [...acc.edges];
  for (const e of add.edges || []) {
    const k = `${e.source}|${e.target}|${e.type}`;
    if (!ek.has(k)) { edges.push(e); ek.add(k); }
  }
  return { nodes, edges };
}
