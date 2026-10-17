"use client";

// Home page: the schema and MCP tabs, plus shared types. The control room itself is ./control.tsx.
// `_home` is a private folder, so nothing here becomes a route.

import clsx from "clsx";
import { Check, Copy } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import GraphCanvas, { GData, LABEL_COLORS } from "@/components/GraphCanvas";
import { Button, Card, CodeCell, Spinner } from "@/components/ui";

// `.kt` is unlayered CSS, so cell padding overrides that line tables up with the card gutter need `!`.
const EDGE_CELLS = "[&_td:first-child]:pl-4! [&_th:first-child]:pl-4! [&_td:last-child]:pr-4! [&_th:last-child]:pr-4!";

export type Triple = { src: string; rel: string; dst: string; n: number };
export type Overview = {
  labels?: { label: string; n: number }[]; relationships?: { type: string; n: number }[]; version?: string | number | null;
  decisions?: number; open_cards?: number; rules?: number; cases?: number; tools?: string[]; session?: string;
};

export const fmt = (v: unknown) =>
  v === undefined || v === null || isNaN(Number(v)) ? "–" : Number(v).toLocaleString("en-US");

// ======================================================================= data tab

const TOP = 12;

function LabelBtn({ l, onPick }: { l: string; onPick: (l: string) => void }) {
  return (
    <button onClick={() => onPick(l)} className="text-ink-2 transition-colors duration-150 ease-out hover:text-kb-ink">{l}</button>
  );
}

export function DataTab({ ov, failed, schemaFailed, triples, graph }:
  { ov: Overview | null; failed: boolean; schemaFailed: boolean; triples: Triple[]; graph: GData }) {
  const [sel, setSelRaw] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const setSel = (l: string | null) => { setSelRaw(l); setAll(false); };

  const labels = ov?.labels || [];
  const drawn = useMemo(() => new Set(graph.nodes.map((n) => n.name as string)), [graph]);

  // label -> neighbours in the drawn schema, for highlighting
  const highlight = useMemo(() => {
    if (!sel || !drawn.has(sel)) return undefined;
    const s = new Set([`L:${sel}`]);
    for (const e of graph.edges) {
      if (e.source === `L:${sel}`) s.add(e.target);
      if (e.target === `L:${sel}`) s.add(e.source);
    }
    return s;
  }, [sel, drawn, graph]);

  // relationship type -> (src, dst) pairs ordered by count
  const pairs = useMemo(() => {
    const m = new Map<string, Triple[]>();
    for (const t of triples) m.set(t.rel, [...(m.get(t.rel) || []), t]);
    for (const v of m.values()) v.sort((a, b) => b.n - a.n);
    return m;
  }, [triples]);

  const rows = useMemo(() => {
    if (sel) {
      return triples.filter((t) => t.src === sel || t.dst === sel).sort((a, b) => b.n - a.n)
        .map((t) => ({ key: `${t.src}|${t.rel}|${t.dst}`, rel: t.rel, pair: t as Triple | undefined, more: 0, n: t.n }));
    }
    return (ov?.relationships || []).map((r) => {
      const p = pairs.get(r.type) || [];
      return { key: r.type, rel: r.type, pair: p[0], more: Math.max(0, p.length - 1), n: r.n };
    });
  }, [sel, triples, pairs, ov]);
  const shown = all ? rows : rows.slice(0, TOP);

  return (
    <div className="grid gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
      <Card title="Node labels" pad={false} className="lg:sticky lg:top-16 lg:self-start"
        action={labels.length ? <span className="num text-[12.5px] text-ink-3">{labels.length}</span> : null}>
        {!ov ? (
          <div className="p-4">{failed ? <span className="text-[13px] text-ink-3">Couldn&apos;t load the graph.</span> : <Spinner label="Loading labels" />}</div>
        ) : (
          <ul className="scroll-thin contain-scroll max-h-[560px] overflow-auto p-2">
            {labels.map(({ label, n }) => {
              const active = sel === label;
              return (
                <li key={label}>
                  <button onClick={() => setSel(active ? null : label)} aria-pressed={active}
                    title={drawn.has(label) ? undefined : "Not drawn in the schema"}
                    className={clsx("flex h-7 w-full items-center gap-2.5 rounded-md px-2 text-left text-[13px] transition-colors duration-100 ease-out",
                      active ? "bg-kb-soft font-medium text-kb-ink" : "text-ink-2 hover:bg-well hover:text-ink")}>
                    <span className="h-2 w-2 shrink-0 rounded-full"
                      style={{ background: drawn.has(label) ? LABEL_COLORS[label] || "var(--text-3)" : "var(--line-strong)" }} />
                    <span className="min-w-0 truncate">{label}</span>
                    <span className="num ml-auto text-[12px] font-normal text-ink-3">{fmt(n)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      <div className="flex min-w-0 flex-col gap-4">
        <Card title={highlight ? `Schema around ${sel}` : "Schema"} pad={false} className="overflow-hidden"
          subtitle="Labels and the relationships that join them; Chunk, Meta and bulk record labels are left out"
          action={sel ? <Button variant="ghost" size="sm" onClick={() => setSel(null)}>Clear</Button> : null}>
          <GraphCanvas data={graph} height={420} showAllLabels highlight={highlight} emptyHint={schemaFailed ? "Couldn't load the schema." : "Loading schema"}
            onNodeClick={(n) => n.name && setSel(n.name === sel ? null : n.name)} />
        </Card>

        <Card title={sel ? `Relationships of ${sel}` : "Relationships"} pad={false}
          action={rows.length ? <span className="text-[13px] text-ink-3"><span className="num">{rows.length}</span> {sel ? "patterns" : "types"}</span> : null}>
          {!rows.length ? (
            <div className="p-4 text-[13px] text-ink-3">
              {!ov && !failed ? <Spinner label="Loading relationships" /> : sel ? `No relationships recorded for ${sel} in the schema.` : "No relationships."}
            </div>
          ) : (
            <>
              <div className="overflow-x-auto overscroll-x-contain">
                <table className={clsx("kt", EDGE_CELLS)}>
                  <thead>
                    <tr><th>Relationship</th><th>Joins</th><th className="text-right!">Count</th></tr>
                  </thead>
                  <tbody>
                    {shown.map((r) => (
                      <tr key={r.key}>
                        <td className="mono text-[12px] text-ink">{r.rel}</td>
                        <td className="text-[13px]">
                          {r.pair ? (
                            <span className="inline-flex flex-wrap items-center gap-x-2">
                              <LabelBtn l={r.pair.src} onPick={setSel} /><span className="text-ink-3">→</span><LabelBtn l={r.pair.dst} onPick={setSel} />
                              {r.more > 0 && <span className="text-[12.5px] text-ink-3">and {r.more} more</span>}
                            </span>
                          ) : <span className="text-ink-3">–</span>}
                        </td>
                        <td className="n text-ink">{fmt(r.n)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {rows.length > TOP && (
                <div className="flex justify-center border-t border-line py-2">
                  <Button variant="ghost" size="sm" onClick={() => setAll(!all)}>{all ? "Show fewer" : `Show all ${rows.length}`}</Button>
                </div>
              )}
            </>
          )}
        </Card>
      </div>
    </div>
  );
}

// ======================================================================= connect tab

const USEFUL: [string, string][] = [
  ["find_portfolio", "Completed works filtered by client, person, category, value or date, each with its certificate."],
  ["prove_absence", "Proves a set is empty, such as works with no reference letter, and returns the boundary it checked."],
  ["qualify_clause", "Checks one tender clause against CPWD rules: similar works, turnover, key personnel, certifications."],
  ["explain_path", "Shortest paths between two entities, up to six hops."],
  ["key_people", "Betweenness over people and clients: who the bid desk depends on."],
  ["cypher_readonly", "Any read-only Cypher query, run as GRAPH.RO_QUERY."],
];

export function ConnectTab({ ov, mcpConfig, origin }: { ov: Overview | null; mcpConfig: string; origin: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(t);
  }, [copied]);

  // CodeCell collapses runs of 6+ spaces, so display with tabs (rendered 2 wide); Copy keeps the 2-space JSON.
  const display = useMemo(() => {
    try { return JSON.stringify(JSON.parse(mcpConfig), null, "\t"); } catch { return mcpConfig; }
  }, [mcpConfig]);

  const all = ov?.tools || [];
  const useful = all.length ? USEFUL.filter(([t]) => all.includes(t)) : USEFUL;
  const rest = all.filter((t) => !USEFUL.some(([u]) => u === t));

  return (
    <div className="grid gap-4 xl:grid-cols-5">
      <Card title="MCP server" className="xl:col-span-2 xl:self-start"
        action={
          <Button size="sm" onClick={() => navigator.clipboard?.writeText(mcpConfig).then(() => setCopied(true), () => {})}>
            {copied ? <Check size={14} strokeWidth={1.75} /> : <Copy size={14} strokeWidth={1.75} />}
            {copied ? "Copied" : "Copy"}
          </Button>
        }>
        <p className="text-[13px] leading-relaxed text-ink-2">
          The agents&apos; graph tools, served over MCP (streamable HTTP) and read-only against your copy of the company
          graph. Add this to Claude Desktop or any MCP host.
        </p>
        <div className="mt-4 [tab-size:2]"><CodeCell code={display} label="JSON" /></div>
        <div className="mt-3 flex flex-wrap items-baseline gap-x-2 text-[13px]">
          <span className="text-ink-3">Endpoint</span>
          <span className="mono break-all text-[12px] text-ink-2">{origin ? `${origin}/mcp/` : "–"}</span>
        </div>
      </Card>

      <Card title="Most useful tools" pad={false} className="xl:col-span-3"
        action={all.length ? <span className="text-[13px] text-ink-3"><span className="num">{all.length}</span> in total</span> : null}>
        <div className="overflow-x-auto overscroll-x-contain">
          <table className={clsx("kt", EDGE_CELLS)}>
            <thead><tr><th>Tool</th><th>What it does</th></tr></thead>
            <tbody>
              {useful.map(([t, d]) => (
                <tr key={t}>
                  <td className="mono text-[12px] whitespace-nowrap text-ink">{t}</td>
                  <td className="text-[13px] text-ink-2">{d}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {rest.length > 0 && (
          <p className="border-t border-line px-4 py-3 text-[12.5px] leading-relaxed text-ink-3">
            Also available: <span className="mono text-[11.5px]">{rest.join(", ")}</span>.
          </p>
        )}
      </Card>
    </div>
  );
}
