"use client";

// Home page sections. `_home` is a private folder, so nothing here becomes a route.

import clsx from "clsx";
import { ArrowRight, Check, Copy } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import GraphCanvas, { GData, LABEL_COLORS } from "@/components/GraphCanvas";
import { Button, Card, CodeCell, Spinner } from "@/components/ui";

// `.kt` is unlayered CSS, so cell padding overrides that line tables up with the card gutter need `!`.
const EDGE_CELLS = "[&_td:first-child]:pl-5! [&_th:first-child]:pl-5! [&_td:last-child]:pr-5! [&_th:last-child]:pr-5!";

export type Triple = { src: string; rel: string; dst: string; n: number };
export type Count = (label: string) => number | undefined;
export type Overview = {
  labels?: { label: string; n: number }[]; relationships?: { type: string; n: number }[]; version?: string | null;
  decisions?: number; open_cards?: number; rules?: number; cases?: number; tools?: string[];
};

export const fmt = (v: unknown) =>
  v === undefined || v === null || isNaN(Number(v)) ? "–" : Number(v).toLocaleString("en-US");

// ======================================================================= links

export function PillLink({ href, variant = "secondary", children }:
  { href: string; variant?: "primary" | "secondary"; children: React.ReactNode }) {
  return (
    <Link href={href}
      className={clsx(
        "inline-flex h-10 items-center justify-center gap-2 rounded-full px-5 text-[14px] font-semibold whitespace-nowrap",
        "transition-[scale,background-color,box-shadow] duration-150 ease-out motion-safe:active:scale-[0.96]",
        variant === "primary"
          ? "bg-ink text-surface-solid hover:bg-ink/85"
          : "border border-transparent bg-surface-solid text-ink shadow-[var(--shadow-border)] hover:shadow-[var(--shadow-border-hover)]")}>
      {children}
    </Link>
  );
}

function PageCard({ href, title, text, metric }: { href: string; title: string; text: string; metric: React.ReactNode }) {
  return (
    <Link href={href} className="glass interactive-card group flex flex-col p-5">
      <h2 className="text-[15px] font-semibold tracking-[-0.005em] text-ink">{title}</h2>
      <p className="mt-2 text-[13.5px] leading-relaxed text-ink-2">{text}</p>
      <div className="mt-auto flex items-center gap-3 pt-4 text-[13px]">
        <span className="min-w-0 truncate text-ink-3">{metric}</span>
        <span className="ml-auto inline-flex shrink-0 items-center gap-1 font-medium text-kb-ink group-hover:underline">
          Open <ArrowRight size={15} strokeWidth={1.75} />
        </span>
      </div>
    </Link>
  );
}

const N = ({ v }: { v: unknown }) => <span className="num text-ink-2">{fmt(v)}</span>;
const Code = ({ children }: { children: React.ReactNode }) => <code className="num text-[12.5px] text-ink">{children}</code>;

// ======================================================================= overview tab

export function OverviewTab({ ov, count, onData }: { ov: Overview | null; count: Count; onData: () => void }) {
  const docs = count("Document");
  const tools: number | undefined = ov?.tools?.length;
  const pages = [
    { href: "/qualify/", title: "Qualify a tender",
      text: "Agents check each eligibility clause and cite the certificate pages that prove it.",
      metric: <><span className="num text-ink-2">4</span> sample tenders{ov?.cases ? <> · <N v={ov.cases} /> cases on file</> : null}</> },
    { href: "/ask/", title: "Ask",
      text: "Multi-hop questions answered by traversal, including proof that something is missing.",
      metric: <><span className="num text-ink-2">5</span> suggested questions</> },
    { href: "/live/", title: "Live documents",
      text: "A corrigendum arrives, the conflict is settled, and every bid that relied on it is re-checked.",
      metric: <><N v={ov?.open_cards} /> open conflicts</> },
    { href: "/decisions/", title: "Decisions",
      text: "Each settled fact with its policy, rationale, decider and both source documents.",
      metric: <><N v={ov?.decisions} /> decisions · <N v={ov?.rules} /> policy rules</> },
  ];
  const facts: [string, unknown][] = [
    ["Completed works", count("Work")], ["Clients", count("Client")], ["People", count("Person")],
    ["Credentials", count("Credential")], ["Claims", count("Claim")], ["Indexed pages", count("Chunk")],
    ["Invoices", count("Invoice")],
  ];

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_280px]">
      <div className="flex min-w-0 flex-col gap-6">
        <p className="max-w-3xl text-[15px] leading-relaxed text-ink-2">
          Construction firms win public tenders by proving past performance. The proof is scattered across{" "}
          {docs !== undefined ? <span className="num text-ink">{fmt(docs)}</span> : "hundreds of"} documents (completion
          certificates, reference letters, CVs, bank guarantees, audited accounts) and they contradict each other over time.
          BidBrain joins them in one FalkorDB graph so agents can qualify tenders, prove what is missing and settle
          conflicts, citing the page behind each answer.
        </p>

        <div className="grid gap-5 sm:grid-cols-2">
          {pages.map((p) => <PageCard key={p.href} {...p} />)}
        </div>

        <Card title="How it works">
          <ol className="grid gap-x-8 gap-y-5 md:grid-cols-2">
            <Step i={1} title="Connected records">
              People, credentials, works, clients, letters, bonds and invoices are joined by edges. A typical question
              takes 4 to 6 hops.
            </Step>
            <Step i={2} title="Claims, not overwrites">
              Every contestable value is a <Code>Claim</Code> tied to its document and page. Disagreements become{" "}
              <Code>CONTRADICTS</Code> edges, settled by <Code>Decision</Code> nodes.
            </Step>
            <Step i={3} title="Tools, not guesses">
              <span className="num">{tools ?? 25}</span> graph tools over MCP: traversals, exact arithmetic, absence
              proofs, CPWD eligibility rules, and FalkorDB algorithms such as betweenness, WCC and shortest paths.
            </Step>
            <Step i={4} title="Isolated memory">
              Agents hand off through a case subgraph. Each visitor works in a <Code>GRAPH.COPY</Code> of the company
              graph, so nothing one person does reaches another.
            </Step>
          </ol>
        </Card>
      </div>

      <aside className="min-w-0">
        <Card title="Graph facts" className="xl:sticky xl:top-20">
          <dl className="-mt-1">
            {facts.map(([label, v]) => (
              <div key={label} className="flex items-baseline justify-between gap-3 border-b border-line py-2 text-[13.5px] last:border-b-0">
                <dt className="text-ink-2">{label}</dt>
                <dd className="num text-ink">{fmt(v)}</dd>
              </div>
            ))}
          </dl>
          {ov?.version && <div className="mt-2 text-[12.5px] text-ink-3">Graph version <span className="num">{ov.version}</span></div>}
          <Button size="sm" className="mt-4 w-full" onClick={onData}>See all labels</Button>
        </Card>
      </aside>
    </div>
  );
}

function Step({ i, title, children }: { i: number; title: string; children: React.ReactNode }) {
  return (
    <li className="flex gap-3">
      <span className="num w-4 shrink-0 text-[13px] leading-[21px] font-semibold text-ink-3">{i}</span>
      <div className="min-w-0">
        <div className="text-[14px] font-semibold text-ink">{title}</div>
        <p className="mt-1 text-[13.5px] leading-relaxed text-ink-2">{children}</p>
      </div>
    </li>
  );
}

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
    <div className="grid gap-5 lg:grid-cols-[260px_minmax(0,1fr)]">
      <Card title="Node labels" pad={false} className="lg:sticky lg:top-20 lg:self-start"
        action={labels.length ? <span className="num text-[12.5px] text-ink-3">{labels.length}</span> : null}>
        {!ov ? (
          <div className="px-5 pb-5 pt-2">{failed ? <span className="text-[13px] text-ink-3">Couldn&apos;t load the graph.</span> : <Spinner label="Loading labels" />}</div>
        ) : (
          <ul className="scroll-thin contain-scroll max-h-[560px] overflow-auto px-2 pb-2">
            {labels.map(({ label, n }) => {
              const active = sel === label;
              return (
                <li key={label}>
                  <button onClick={() => setSel(active ? null : label)} aria-pressed={active}
                    title={drawn.has(label) ? undefined : "Not drawn in the schema"}
                    className={clsx("flex h-8 w-full items-center gap-3 rounded-lg px-3 text-left text-[13.5px] transition-colors duration-150 ease-out",
                      active ? "bg-kb-soft font-semibold text-kb-ink" : "text-ink-2 hover:bg-well hover:text-ink")}>
                    <span className="h-2 w-2 shrink-0 rounded-full"
                      style={{ background: drawn.has(label) ? LABEL_COLORS[label] || "var(--text-3)" : "var(--line-strong)" }} />
                    <span className="min-w-0 truncate">{label}</span>
                    <span className="num ml-auto text-[12.5px] font-normal text-ink-3">{fmt(n)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      <div className="flex min-w-0 flex-col gap-5">
        <Card title={highlight ? `Schema around ${sel}` : "Schema"} pad={false} className="overflow-hidden"
          action={sel ? <Button variant="ghost" size="sm" onClick={() => setSel(null)}>Clear</Button> : null}>
          <p className="px-5 pb-1 text-[13px] text-ink-3">
            Labels and the relationships that join them. Chunk, Meta and bulk record labels are left out.
          </p>
          <GraphCanvas data={graph} height={420} showAllLabels highlight={highlight} emptyHint={schemaFailed ? "Couldn't load the schema." : "Loading schema"}
            onNodeClick={(n) => n.name && setSel(n.name === sel ? null : n.name)} />
        </Card>

        <Card title={sel ? `Relationships of ${sel}` : "Relationships"} pad={false}
          action={rows.length ? <span className="text-[13px] text-ink-3"><span className="num">{rows.length}</span> {sel ? "patterns" : "types"}</span> : null}>
          {!rows.length ? (
            <div className="px-5 pb-5 pt-2 text-[13px] text-ink-3">
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
                        <td className="num text-[12.5px] text-ink">{r.rel}</td>
                        <td className="text-[13px]">
                          {r.pair ? (
                            <span className="inline-flex flex-wrap items-center gap-x-2">
                              <LabelBtn l={r.pair.src} onPick={setSel} /><span className="text-ink-3">→</span><LabelBtn l={r.pair.dst} onPick={setSel} />
                              {r.more > 0 && <span className="text-[12.5px] text-ink-3">and {r.more} more</span>}
                            </span>
                          ) : <span className="text-ink-3">–</span>}
                        </td>
                        <td className="num text-right text-ink">{fmt(r.n)}</td>
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
    <div className="grid gap-5 xl:grid-cols-5">
      <Card title="MCP server" className="xl:col-span-2 xl:self-start"
        action={
          <Button size="sm" onClick={() => navigator.clipboard?.writeText(mcpConfig).then(() => setCopied(true), () => {})}>
            {copied ? <Check size={15} strokeWidth={1.75} /> : <Copy size={15} strokeWidth={1.75} />}
            {copied ? "Copied" : "Copy"}
          </Button>
        }>
        <p className="text-[13.5px] leading-relaxed text-ink-2">
          The agents&apos; graph tools, served over MCP (streamable HTTP) and read-only against your copy of the company
          graph. Add this to Claude Desktop or any MCP host.
        </p>
        <div className="mt-4 [tab-size:2]"><CodeCell code={display} label="JSON" /></div>
        <div className="mt-3 flex flex-wrap items-baseline gap-x-2 text-[13px]">
          <span className="text-ink-3">Endpoint</span>
          <span className="num break-all text-ink-2">{origin ? `${origin}/mcp/` : "–"}</span>
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
                  <td className="num text-[12.5px] whitespace-nowrap text-ink">{t}</td>
                  <td className="text-[13px] text-ink-2">{d}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {rest.length > 0 && (
          <p className="border-t border-line px-5 py-3 text-[12.5px] leading-relaxed text-ink-3">
            Also available: <span className="num">{rest.join(", ")}</span>.
          </p>
        )}
      </Card>
    </div>
  );
}
