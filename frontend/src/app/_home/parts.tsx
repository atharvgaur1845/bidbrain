"use client";

// Home page sections. `_home` is a private folder, so nothing here becomes a route.

import clsx from "clsx";
import { ArrowRight, ArrowUp, BarChart3, Check, Copy, FileStack, GitBranch, Network, ScrollText, SearchX, Sparkles, Users, Wallet } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import GraphCanvas, { GData, LABEL_COLORS } from "@/components/GraphCanvas";
import { Button, Card, CodeCell, IconTile, Spinner, type TileTone } from "@/components/ui";

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
        "inline-flex h-9 items-center justify-center gap-1.5 rounded-lg px-3.5 text-[13px] font-medium whitespace-nowrap",
        "transition-[scale,background-color,box-shadow] duration-150 ease-out motion-safe:active:scale-[0.96]",
        variant === "primary"
          ? "bg-ink text-surface-solid shadow-[0_1px_2px_rgb(0_0_0/0.18)] hover:bg-ink/88"
          : "border border-transparent bg-surface-solid text-ink shadow-[var(--shadow-border)] hover:shadow-[var(--shadow-border-hover)]")}>
      {children}
    </Link>
  );
}

// ======================================================================= ask panel (the AI entry point)

const IDEAS: { icon: React.ReactNode; text: string; q: string }[] = [
  { icon: <SearchX size={15} strokeWidth={1.75} />, text: "Which client works have no reference letter?",
    q: "Which of Trishakti Power Generation Corporation's completed works have no client reference letter on file?" },
  { icon: <Network size={15} strokeWidth={1.75} />, text: "Total value of a client's portfolio, from one credential",
    q: "Starting from Sunita Joshi's Six Sigma Black Belt on the Ring Road — Uttar Pradesh Pkg-107 project, what is the combined value of every completed work for that client?" },
  { icon: <Users size={15} strokeWidth={1.75} />, text: "Who knows this client, and who is our bus factor?",
    q: "Who on the team has worked with the National Expressway Development Authority before, and who is the company's biggest single point of failure across clients?" },
  { icon: <Wallet size={15} strokeWidth={1.75} />, text: "How much of what we invoiced has been collected?",
    q: "What percentage of what we invoiced the client behind the Ring Road — West Bengal Pkg-128 job has actually been collected?" },
];

export function AskPanel() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const go = (text: string) => text.trim() && router.push(`/ask/?q=${encodeURIComponent(text.trim())}`);
  return (
    <section className="ai-panel px-5 py-6 sm:px-8">
      <div className="mx-auto max-w-2xl text-center">
        <h2 className="text-[15px] font-semibold text-ink">Ask the company brain</h2>
        <p className="mt-1 text-[13px] text-ink-2">
          Multi-hop questions over certificates, people, clients and invoices. Every answer shows its path and the pages behind it.
        </p>
        <form className="mt-4 flex gap-2" onSubmit={(e) => { e.preventDefault(); go(q); }}>
          <input value={q} onChange={(e) => setQ(e.target.value)} aria-label="Ask a question"
            placeholder="e.g. Which expressway works did Sunita Joshi lead?" className="field h-10 bg-surface-solid" />
          <Button variant="primary" type="submit" className="h-10 shrink-0" disabled={!q.trim()}>
            <Sparkles size={15} strokeWidth={1.75} /> Ask
          </Button>
        </form>
      </div>
      <div className="mt-5 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
        {IDEAS.map((i) => (
          <button key={i.q} type="button" onClick={() => go(i.q)}
            className="glass interactive-card flex items-start gap-2.5 px-3 py-3 text-left text-[12.5px] leading-snug text-ink-2 hover:text-ink">
            <span className="mt-px shrink-0 text-kb-ink">{i.icon}</span>
            <span>{i.text}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

// ======================================================================= explore rows (numbered, with a visual panel)

function Visual({ tiles }: { tiles: [TileTone, React.ReactNode][] }) {
  return (
    <div className="grid h-full min-h-[120px] place-items-center"
      style={{ background: `linear-gradient(135deg, var(--tile-${tiles[0][0]}), var(--tile-${tiles[tiles.length - 1][0]}))` }}>
      <div className="flex -space-x-2">
        {tiles.map(([tone, icon], i) => (
          <span key={i} className="rounded-xl bg-surface-solid/70 p-1 shadow-[var(--shadow-border)]" style={{ rotate: `${(i - 1) * 6}deg` }}>
            <IconTile tone={tone} size={34}>{icon}</IconTile>
          </span>
        ))}
      </div>
    </div>
  );
}

function ExploreRow({ n, href, title, text, cta, metric, tiles }:
  { n: number; href: string; title: string; text: string; cta: string; metric?: React.ReactNode; tiles: [TileTone, React.ReactNode][] }) {
  return (
    <div className="glass grid overflow-hidden sm:grid-cols-[180px_minmax(0,1fr)]">
      <Visual tiles={tiles} />
      <div className="flex min-w-0 flex-col justify-center gap-1 px-5 py-4">
        <div className="flex items-center gap-2.5">
          <span className="num grid h-5 w-5 shrink-0 place-items-center rounded-full bg-well text-[11px] text-ink-2 shadow-[inset_0_0_0_1px_var(--line)]">{n}</span>
          <h3 className="text-[14px] font-semibold text-ink">{title}</h3>
        </div>
        <p className="text-[13px] leading-relaxed text-ink-2">{text}</p>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <PillLink href={href}>{cta}</PillLink>
          {metric && <span className="text-[12.5px] text-ink-3">{metric}</span>}
        </div>
      </div>
    </div>
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

  const ic = { size: 17, strokeWidth: 1.75 };
  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_260px]">
      <div className="flex min-w-0 flex-col gap-6">
        <AskPanel />

        <div>
          <h2 className="mb-3 text-[14.5px] font-semibold text-ink">Explore BidBrain</h2>
          <div className="flex flex-col gap-3">
            <ExploreRow n={1} href="/qualify/" title="Qualify a tender" cta="Qualify a tender"
              text="Six agents read the eligibility clauses, check each against the graph and write a bid memo that cites certificate pages."
              metric={<><span className="num text-ink-2">4</span> sample tenders{ov?.cases ? <> · <N v={ov.cases} /> cases on file</> : null}</>}
              tiles={[["teal", <ScrollText key="a" {...ic} />], ["lavender", <Sparkles key="b" {...ic} />], ["sage", <Check key="c" {...ic} />]]} />
            <ExploreRow n={2} href="/live/" title="Send a document that changes the facts" cta="Open live documents"
              text={`A corrigendum or revised certificate becomes claims; conflicts are settled by policy or by you, and every bid relying on the fact is re-checked.`}
              metric={<><N v={ov?.open_cards} /> open conflicts</>}
              tiles={[["peach", <FileStack key="a" {...ic} />], ["rose", <GitBranch key="b" {...ic} />], ["sand", <ScrollText key="c" {...ic} />]]} />
            <ExploreRow n={3} href="/decisions/" title="Trace any settled fact" cta="See decisions"
              text="Each decision records its policy, the decider and both source documents; your own choices become rules for the next conflict."
              metric={<><N v={ov?.decisions} /> decisions · <N v={ov?.rules} /> learned rules</>}
              tiles={[["rose", <GitBranch key="a" {...ic} />], ["lavender", <Users key="b" {...ic} />], ["teal", <Check key="c" {...ic} />]]} />
            <ExploreRow n={4} href="/benchmark/" title="Check the benchmark" cta="Open benchmark"
              text="The same model and database with four context strategies: graph tools, text-to-Cypher, vector RAG and GraphRAG-SDK."
              tiles={[["sage", <BarChart3 key="a" {...ic} />], ["peach", <BarChart3 key="b" {...ic} />], ["lavender", <BarChart3 key="c" {...ic} />]]} />
          </div>
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
