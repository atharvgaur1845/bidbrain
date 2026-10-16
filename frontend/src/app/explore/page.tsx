"use client";

import { Route, Search, Compass as CompassIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import GraphCanvas, { GData, GNode, mergeGraph } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { Button, Card, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
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
  const [pa, setPa] = useState({ a: "Sunita Joshi", ak: "person", b: "National Expressway Development Authority", bk: "client" });

  async function open(label: string, key: string) {
    const nb = await api(`/api/neighborhood?label=${label}&key=${encodeURIComponent(key)}`);
    setGraph((g) => mergeGraph(g, nb));
    setHl(new Set([`${label}:${key}`]));
    setFocus(`${label}:${key}`);
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
    if (text) lookup(text);
    else if (first) expand(HOME.label, HOME.key);
  });

  const focusNode = focus ? graph.nodes.find((n) => n.id === focus) : undefined;

  return (
    <div>
      <PageHeader icon={<CompassIcon size={19} strokeWidth={1.75} />} tone="sand" title="Explore"
        subtitle="Search any entity, expand its neighbourhood, run FalkorDB's built-in algorithms." />
      <div className="grid gap-5 lg:grid-cols-12">
        <div className="flex min-w-0 flex-col gap-5 lg:col-span-4">
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
              <Button variant="primary" className="mt-1 w-full" disabled={busy}>
                <Route size={16} strokeWidth={1.75} />
                {running === "explain_path" ? "Finding paths" : "Find paths"}
              </Button>
            </form>
          </Card>
        </div>

        <div className="flex min-w-0 flex-col gap-5 lg:col-span-8">
          <Card title="Graph" pad={false} className="overflow-hidden"
            action={
              <Button variant="ghost" size="sm" disabled={!graph.nodes.length}
                onClick={() => { setGraph({ nodes: [], edges: [] }); setHl(new Set()); }}>
                Clear
              </Button>
            }>
            <GraphCanvas data={graph} highlight={hl} height={520} emptyHint="Find an entity or run an algorithm."
              onNodeClick={(n: GNode) => { const [label, ...rest] = n.id.split(":"); expand(label, rest.join(":")); }} />
            <div className="flex items-center gap-3 border-t border-line px-5 py-3 text-[12.5px] text-ink-3">
              <span className="shrink-0">Click a node to expand its neighbourhood.</span>
              {focusNode && (
                <span className="ml-auto min-w-0 truncate">
                  Focused on <span className="font-medium text-ink-2">{focusNode.name || focusNode.id}</span>
                </span>
              )}
            </div>
          </Card>

          {err && <p role="alert" className="rounded-[10px] bg-bad-soft px-4 py-3 text-[13px] text-bad">{err}</p>}

          <div className="grid gap-5 2xl:grid-cols-2">
            <Properties entity={entity} label={entityLabel} />
            <AlgoResult a={algo} onOpen={expand} />
          </div>
        </div>
      </div>
    </div>
  );
}
