"use client";

import { Route, Search, Compass as CompassIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import GraphCanvas, { GData, GNode, LABEL_COLORS, mergeGraph } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { Button, Card, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import { Detail } from "./detail";
import { AlgoList, AlgoResult, Candidates, Endpoint, KINDS, Kind, LABEL_OF, Properties, Segmented } from "./parts";

const HOME = { label: "Client", key: "nationalexpresswaydevelopmentauthority" };
// ?q= lookups try kinds in this order and stop at the first match scoring at least STRONG
const LOOKUP_ORDER: Kind[] = ["client", "person", "work", "tender"];
const STRONG = 70;

export default function ExplorePage() {
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<Kind>("client");
  const [cands, setCands] = useState<any[]>([]);
  const [found, setFound] = useState<{ kind: Kind; text: string } | null>(null);
  const [searching, setSearching] = useState(false);
  const [graph, setGraph] = useState<GData>({ nodes: [], edges: [] });
  const [hl, setHl] = useState<Set<string>>(new Set());
  const [focus, setFocus] = useState<string | null>(null);
  const [entity, setEntity] = useState<any>(null);
  const [entityLabel, setEntityLabel] = useState<string | null>(null);
  const [algo, setAlgo] = useState<{ name: string; data: any } | null>(null);
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [pa, setPa] = useState({ a: "Sunita Joshi", ak: "person", b: "National Expressway Development Authority", bk: "client" });

  async function open(label: string, key: string) {
    const nb = await api(`/api/neighborhood?label=${label}&key=${encodeURIComponent(key)}`);
    setGraph((g) => mergeGraph(g, nb));
    setHl(new Set([`${label}:${key}`]));
    setFocus(`${label}:${key}`);
    setSel(`${label}:${key}`);
    const k = Object.entries(LABEL_OF).find(([, v]) => v === label)?.[0];
    if (k) api(`/api/entity/${k}/${encodeURIComponent(key)}`)
      .then((e) => { setEntity(e.result?.entity); setEntityLabel(label); })
      .catch(() => setEntity(null));
  }
  function expand(label: string, key: string) {
    setErr(null);
    open(label, key).catch((e) => setErr(`Could not load the neighbourhood: ${e.message}`));
  }

  async function resolve(text: string, k: Kind): Promise<any[]> {
    const r = await api(`/api/tool/resolve_entity`, { method: "POST", body: JSON.stringify({ text, kind: k }) });
    return r.result || [];
  }
  async function search() {
    if (!q.trim()) return;
    setSearching(true);
    setErr(null);
    try {
      const r = await resolve(q, kind);
      setCands(r);
      setFound({ kind, text: q.trim() });
    } catch (e: any) {
      setErr(`Search failed: ${e.message}`);
    } finally { setSearching(false); }
  }
  // header search lands here as /explore/?q=…: open the best match across kinds
  async function lookup(text: string) {
    setQ(text);
    setSearching(true);
    setErr(null);
    try {
      let best: { kind: Kind; cands: any[] } | null = null;
      for (const k of LOOKUP_ORDER) {
        const c = await resolve(text, k).catch(() => [] as any[]);
        if (c.length && (!best || c[0].score > best.cands[0].score)) best = { kind: k, cands: c };
        if ((c[0]?.score ?? 0) >= STRONG) break;
      }
      setKind(best?.kind ?? "client");
      setCands(best?.cands ?? []);
      setFound({ kind: best?.kind ?? "client", text });
      if (best && best.cands[0].score >= STRONG) await open(LABEL_OF[best.kind], best.cands[0].key);
    } catch (e: any) {
      setErr(`Search failed: ${e.message}`);
    } finally { setSearching(false); }
  }

  // start where this session's latest change landed (the subject of the newest received document), else the demo client
  async function startRelevant() {
    try {
      const r = await api<{ documents: any[] }>("/api/impact");
      const c = r.documents.flatMap((d) => d.claims || []).find((x: any) => x.subject_label && x.subject_key && /^[A-Za-z]+$/.test(x.subject_label));
      if (c) { await open(c.subject_label, c.subject_key); return; }
    } catch { /* fall through */ }
    expand(HOME.label, HOME.key);
  }

  async function runAlgo(name: string) {
    setBusy(true);
    setRunning(name);
    setErr(null);
    try {
      const r = await api(`/api/algos/${name}`);
      setAlgo({ name, data: r });
      if (r.subgraph?.nodes?.length) { setGraph((g) => mergeGraph(g, r.subgraph)); setHl(new Set(r.subgraph.nodes.map((n: any) => n.id))); }
    } catch (e: any) {
      setErr(`Could not run ${name.replace(/_/g, " ")}: ${e.message}`);
    } finally { setBusy(false); setRunning(null); }
  }
  async function path() {
    setBusy(true);
    setRunning("explain_path");
    setErr(null);
    try {
      const r = await api(`/api/tool/explain_path`, { method: "POST", body: JSON.stringify({ a: pa.a, a_kind: pa.ak, b: pa.b, b_kind: pa.bk }) });
      setAlgo({ name: "explain_path", data: r });
      if (r.subgraph) { setGraph(r.subgraph); setHl(new Set(r.subgraph.nodes.map((n: any) => n.id))); }
    } catch (e: any) {
      setErr(`Could not find paths: ${e.message}`);
    } finally { setBusy(false); setRunning(null); }
  }

  // Runs after every render but acts only when ?q= changes, so a header search issued while
  // already on this page (same route, new query string) is picked up as well as the first load.
  const handledQ = useRef<string | null>(null);
  useEffect(() => {
    const text = new URLSearchParams(window.location.search).get("q")?.trim() || "";
    if (text === handledQ.current) return;
    const first = handledQ.current === null;
    handledQ.current = text;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- once per ?q= change; the URL is external state
    const sp = new URLSearchParams(window.location.search);
    const L = sp.get("label"), K = sp.get("key");
    if (text) lookup(text);
    else if (first && L && K && /^[A-Za-z]+$/.test(L)) expand(L, K);
    else if (first) startRelevant();
  });

  const focusNode = focus ? graph.nodes.find((n) => n.id === focus) : undefined;
  const selNode = sel ? graph.nodes.find((n) => n.id === sel) : undefined;
  const labelsIn = useMemo(() => [...new Set(graph.nodes.map((n) => n.label))].sort(), [graph.nodes]);
  const shown: GData = useMemo(() => {
    if (!hidden.size) return graph;
    const nodes = graph.nodes.filter((n) => !hidden.has(n.label) || n.id === sel);
    const ids = new Set(nodes.map((n) => n.id));
    return { nodes, edges: graph.edges.filter((e) => ids.has(e.source as string) && ids.has(e.target as string)) };
  }, [graph, hidden, sel]);

  return (
    <div>
      <PageHeader icon={<CompassIcon size={19} strokeWidth={1.75} />} tone="sand" title="Explore"
        subtitle="Search any entity, expand its neighbourhood, run FalkorDB's built-in algorithms." />
      <div className="grid gap-4 lg:grid-cols-12">
        <div className="flex min-w-0 flex-col gap-4 lg:col-span-4 xl:col-span-3">
          <Card title="Find" action={searching ? <Spinner label="Searching" /> : null}>
            <Segmented label="Entity kind" value={kind} onChange={setKind}
              options={KINDS.map((k) => ({ id: k, label: LABEL_OF[k] }))} />
            <form role="search" className="relative mt-3" onSubmit={(e) => { e.preventDefault(); search(); }}>
              <Search size={16} strokeWidth={1.75} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-3" />
              <input type="search" enterKeyHint="search" aria-label="Search entities" className="field pl-9!"
                value={q} onChange={(e) => setQ(e.target.value)} placeholder="phed odisha, Meera Bane, Pkg-113" />
            </form>
            {found && <Candidates cands={cands} kind={found.kind} text={found.text} focus={focus} onOpen={expand} />}
          </Card>

          <Card title="Algorithms">
            <AlgoList busy={busy} running={running} onRun={runAlgo} />
          </Card>

          <Card title="Explain a connection" action={<span className="num text-[12px] text-ink-3">algo.SPpaths</span>}>
            <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); path(); }}>
              <Endpoint label="From" text={pa.a} kind={pa.ak}
                onText={(v) => setPa({ ...pa, a: v })} onKind={(v) => setPa({ ...pa, ak: v })} />
              <Endpoint label="To" text={pa.b} kind={pa.bk}
                onText={(v) => setPa({ ...pa, b: v })} onKind={(v) => setPa({ ...pa, bk: v })} />
              <Button type="submit" variant="primary" className="mt-1 w-full" disabled={busy}>
                <Route size={16} strokeWidth={1.75} />
                {running === "explain_path" ? "Finding paths" : "Find paths"}
              </Button>
            </form>
          </Card>
        </div>

        <div className="flex min-w-0 flex-col gap-4 lg:col-span-8 xl:col-span-6">
          <Card title="Graph" pad={false} className="overflow-hidden"
            action={
              <Button variant="ghost" size="sm" disabled={!graph.nodes.length}
                onClick={() => { setGraph({ nodes: [], edges: [] }); setHl(new Set()); setSel(null); setFocus(null); }}>
                Clear
              </Button>
            }>
            {labelsIn.length > 1 && (
              <div role="group" aria-label="Filter node types" className="flex flex-wrap items-center gap-1.5 border-b border-line px-4 py-2 text-[12px]">
                <span className="mr-1 text-ink-3">Show</span>
                {labelsIn.map((l) => {
                  const on = !hidden.has(l);
                  const n = graph.nodes.filter((x) => x.label === l).length;
                  return (
                    <button key={l} type="button" aria-pressed={on} title={on ? `Hide ${l} nodes` : `Show ${l} nodes`}
                      onClick={() => setHidden((h) => { const x = new Set(h); if (on) x.add(l); else x.delete(l); return x; })}
                      className={"inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 transition-colors duration-100 " + (on ? "border-line-strong bg-surface text-ink-2 hover:bg-surface-2" : "border-line text-ink-3 line-through hover:bg-surface-2")}>
                      <span className="h-2 w-2 rounded-full" style={{ background: on ? (LABEL_COLORS[l] || "#8a8f98") : "transparent", border: `1px solid ${LABEL_COLORS[l] || "#8a8f98"}` }} />
                      {l} <span className="num text-ink-3">{n}</span>
                    </button>
                  );
                })}
              </div>
            )}
            <GraphCanvas data={shown} highlight={hl} height={520} emptyHint="Find an entity or run an algorithm."
              onNodeClick={(n: GNode) => { const [label, ...rest] = n.id.split(":"); expand(label, rest.join(":")); }} />
            <div className="flex items-center gap-3 border-t border-line px-5 py-3 text-[12.5px] text-ink-3">
              <span className="shrink-0">Click a node to select it and expand its neighbourhood. Scroll to zoom, drag to pan.</span>
              {focusNode && (
                <span className="ml-auto min-w-0 truncate">
                  Focused on <span className="font-medium text-ink-2">{focusNode.name || focusNode.id}</span>
                </span>
              )}
            </div>
          </Card>

          {err && <p role="alert" className="rounded-[10px] bg-bad-soft px-4 py-3 text-[13px] text-bad">{err}</p>}

          <AlgoResult a={algo} onOpen={expand} />
        </div>
        <div className="flex min-w-0 flex-col gap-4 lg:col-span-12 xl:col-span-3">
          <div className="contents">
            <Detail node={selNode} graph={graph} onExpand={expand}
              onChain={(g, ids) => { setGraph((x) => mergeGraph(x, g)); setHl(new Set(ids)); }}
              onPath={(end, name, k) => setPa((p) => (end === "a" ? { ...p, a: name, ak: k } : { ...p, b: name, bk: k }))} />
            <Properties entity={entity} label={entityLabel} />
          </div>
        </div>
      </div>
    </div>
  );
}
