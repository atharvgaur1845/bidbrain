"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState } from "react";

const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

export type GNode = { id: string; label: string; name?: string; [k: string]: any };
export type GEdge = { source: string; target: string; type: string };
export type GData = { nodes: GNode[]; edges: GEdge[] };

// restrained categorical palette: mid-saturation hues that stay apart on white; the accent navy marks a Case,
// red is kept for things that are wrong or missing (Missing, Finding, Decision overrides)
export const LABEL_COLORS: Record<string, string> = {
  Work: "#3d6fb6", Client: "#b7791f", Person: "#6b5bb5", Document: "#8a8f98", Credential: "#4f8a5b",
  Claim: "#a35a7a", Decision: "#b3483d", Tender: "#2f8a8a", Bid: "#c08a2e", Case: "#2b4c9b",
  FinancialStatement: "#5e8c4f", Bond: "#b8763a", Category: "#a3a7ae", Company: "#1b1c1f",
  Certification: "#3f7f6e", Missing: "#b3261e", Clause: "#4f63b0", Finding: "#b3483d", Invoice: "#5b86b0",
  AgentRun: "#2f8a8a", Conversation: "#8a8f98", Turn: "#8a8f98", Requirement: "#7a7f88", Contract: "#6d8bb8",
};
const SIZE: Record<string, number> = { Client: 7, Person: 6, Work: 5, Case: 8, Company: 8, Tender: 6, Bid: 6, Missing: 5 };

type Props = {
  data: GData;
  highlight?: Set<string>;
  height?: number;
  onNodeClick?: (n: GNode) => void;
  emptyHint?: string;
  showAllLabels?: boolean;
};

export default function GraphCanvas({ data, highlight, height = 460, onNodeClick, emptyHint, showAllLabels }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const fg = useRef<any>(null);
  const [width, setWidth] = useState(600);
  const store = useRef<Map<string, any>>(new Map());
  const [hover, setHover] = useState<string | null>(null);
  const [pal, setPal] = useState({ edge: "rgba(27,28,31,0.16)", label: "#1b1c1f", dim: "rgba(27,28,31,0.45)", stroke: "#ffffff", path: "#2b4c9b", halo: "rgba(43,76,155,0.16)" });
  useEffect(() => {
    const read = () => {
      const cs = getComputedStyle(document.documentElement);
      const v = (n: string, d: string) => cs.getPropertyValue(n).trim() || d;
      setPal({ edge: v("--graph-edge", "rgba(27,28,31,0.16)"), label: v("--graph-label", "#1b1c1f"),
               dim: v("--graph-label-dim", "rgba(27,28,31,0.45)"), stroke: v("--graph-node-stroke", "#fff"),
               path: v("--graph-path", "#2b4c9b"), halo: v("--graph-halo", "rgba(43,76,155,0.16)") });
    };
    read();
    const mo = new MutationObserver(read);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => mo.disconnect();
  }, []);

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

  // fit once the layout has settled for the current node set (fitting earlier zooms into a clump)
  const fitted = useRef<number>(-1);
  const fitNow = () => {
    if (fitted.current !== graph.nodes.length) {
      fitted.current = graph.nodes.length;
      fg.current?.zoomToFit(450, 40);
    }
  };
  useEffect(() => {
    const t = setTimeout(() => fg.current?.zoomToFit(400, 40), 900);
    return () => clearTimeout(t);
  }, [graph.nodes.length]);

  useEffect(() => {
    const f = fg.current;
    if (!f) return;
    f.d3Force("charge")?.strength(graph.nodes.length > 15 ? -260 : -140);
    f.d3Force("link")?.distance(graph.nodes.length > 15 ? 64 : 48);
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
    <div ref={wrap} className="relative w-full overflow-hidden" style={{ height }}>
      {graph.nodes.length === 0 && (
        <div className="absolute inset-0 grid place-items-center px-6 text-center text-[13px] text-ink-3">
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
        onEngineStop={fitNow}
        nodeRelSize={1}
        linkColor={(l: any) => (linkOnPath(l) ? pal.path : pal.edge)}
        linkWidth={(l: any) => (linkOnPath(l) ? 2.2 : 0.8)}
        linkDirectionalParticles={(l: any) => (linkOnPath(l) ? 2 : 0)}
        linkDirectionalParticleWidth={2.6}
        linkDirectionalParticleColor={() => pal.path}
        linkDirectionalArrowLength={(l: any) => (linkOnPath(l) ? 3.5 : 0)}
        linkDirectionalArrowRelPos={0.92}
        onNodeHover={(n: any) => setHover(n?.id ?? null)}
        onNodeClick={(n: any) => onNodeClick?.(n)}
        nodeCanvasObject={(node: any, ctx: CanvasRenderingContext2D, scale: number) => {
          const r = (SIZE[node.label] || 4) * (isHl(node.id) ? 1.35 : 1);
          const color = LABEL_COLORS[node.label] || "#8a8f98";
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
              ctx.fillStyle = pal.halo; ctx.fill();
            }
            ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
            ctx.fillStyle = color; ctx.fill();
            ctx.lineWidth = 1.2; ctx.strokeStyle = pal.stroke; ctx.stroke();
          }
          const crowded = graph.nodes.length > 24;
          const showLabel = showAllLabels || isHl(node.id) || hover === node.id || (scale > (crowded ? 4 : 2.2)) || ["Client", "Case"].includes(node.label);
          if (showLabel && node.name) {
            const fs = Math.max(3.2, 11 / scale);
            ctx.font = `${isHl(node.id) ? 600 : 500} ${fs}px Inter, ui-sans-serif, sans-serif`;
            ctx.textAlign = "center"; ctx.textBaseline = "top";
            const text = String(node.name).length > 42 ? String(node.name).slice(0, 40) + "…" : String(node.name);
            ctx.lineWidth = Math.max(1, 3 / scale); ctx.strokeStyle = "rgba(255,255,255,0.9)"; ctx.lineJoin = "round";
            ctx.strokeText(text, node.x, node.y + r + 2);
            ctx.fillStyle = dim ? pal.dim : pal.label;
            ctx.fillText(text, node.x, node.y + r + 2);
          }
          ctx.globalAlpha = 1;
        }}
        nodePointerAreaPaint={(node: any, color: string, ctx: CanvasRenderingContext2D) => {
          ctx.fillStyle = color; ctx.beginPath(); ctx.arc(node.x, node.y, (SIZE[node.label] || 4) + 3, 0, 2 * Math.PI); ctx.fill();
        }}
      />
      {labelsPresent.length > 0 && (
        <div className="pointer-events-none absolute bottom-3 left-4 flex max-w-[calc(100%-2rem)] flex-wrap gap-x-3.5 gap-y-1 rounded-md border border-line bg-surface/95 px-2 py-1">
          {labelsPresent.map((l) => (
            <span key={l} className="inline-flex items-center gap-1.5 text-[12px] text-ink-2">
              <span className="h-2 w-2 rounded-full" style={{ background: LABEL_COLORS[l] || "#8a8f98" }} />
              {l}
            </span>
          ))}
        </div>
      )}
      <div className="num pointer-events-none absolute right-4 top-3 rounded-md border border-line bg-surface/95 px-2 py-0.5 text-[12px] text-ink-3">
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
