"use client";

import { AlertTriangle, Route, Search, Share2, Unplug, Users } from "lucide-react";
import { useEffect, useState } from "react";
import GraphCanvas, { GData, GNode, mergeGraph } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { CypherBlock, Panel, Spinner } from "@/components/ui";
import { api, fmtINR } from "@/lib/api";

const KINDS = ["client", "person", "work", "tender"] as const;
const LABEL_OF: Record<string, string> = { client: "Client", person: "Person", work: "Work", tender: "Tender" };

export default function ExplorePage() {
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<(typeof KINDS)[number]>("client");
  const [cands, setCands] = useState<any[]>([]);
  const [graph, setGraph] = useState<GData>({ nodes: [], edges: [] });
  const [hl, setHl] = useState<Set<string>>(new Set());
  const [entity, setEntity] = useState<any>(null);
  const [algo, setAlgo] = useState<{ name: string; data: any } | null>(null);
  const [busy, setBusy] = useState(false);
  const [pa, setPa] = useState({ a: "Sunita Joshi", ak: "person", b: "National Expressway Development Authority", bk: "client" });

  async function open(label: string, key: string) {
    const nb = await api(`/api/neighborhood?label=${label}&key=${encodeURIComponent(key)}`);
    setGraph((g) => mergeGraph(g, nb));
    setHl(new Set([`${label}:${key}`]));
    const k = Object.entries(LABEL_OF).find(([, v]) => v === label)?.[0];
    if (k) api(`/api/entity/${k}/${encodeURIComponent(key)}`).then((e) => setEntity(e.result?.entity)).catch(() => setEntity(null));
  }
  async function search() {
    if (!q.trim()) return;
    const r = await api(`/api/tool/resolve_entity`, { method: "POST", body: JSON.stringify({ text: q, kind }) });
    setCands(r.result || []);
  }
  async function runAlgo(name: string) {
    setBusy(true);
    try {
      const r = await api(`/api/algos/${name}`);
      setAlgo({ name, data: r });
      if (r.subgraph?.nodes?.length) { setGraph((g) => mergeGraph(g, r.subgraph)); setHl(new Set(r.subgraph.nodes.map((n: any) => n.id))); }
    } finally { setBusy(false); }
  }
  async function path() {
    setBusy(true);
    try {
      const r = await api(`/api/tool/explain_path`, { method: "POST", body: JSON.stringify({ a: pa.a, a_kind: pa.ak, b: pa.b, b_kind: pa.bk }) });
      setAlgo({ name: "explain_path", data: r });
      if (r.subgraph) { setGraph(r.subgraph); setHl(new Set(r.subgraph.nodes.map((n: any) => n.id))); }
    } finally { setBusy(false); }
  }
  useEffect(() => { open("Client", "nationalexpresswaydevelopmentauthority"); }, []);

  return (
    <div>
      <PageHeader eyebrow="Graph algorithms as agent tools" title="Explore the company brain">
        Search any client, person, work or tender and expand its neighbourhood. The buttons run FalkorDB's built-in
        algorithms, the same tools the agents call: betweenness for the bus factor, WCC for isolated entities, SPpaths to explain how two things connect.
      </PageHeader>
      <div className="grid gap-5 lg:grid-cols-12">
        <div className="space-y-4 lg:col-span-4">
          <Panel title="Find">
            <div className="flex gap-1.5">
              {KINDS.map((k) => <button key={k} onClick={() => setKind(k)} className={`chip !py-1 ${kind === k ? "!border-accent !text-ink" : ""}`}>{k}</button>)}
            </div>
            <form className="mt-2 flex gap-2" onSubmit={(e) => { e.preventDefault(); search(); }}>
              <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. phed odisha, Meera Bane, Pkg-113" />
              <button className="btn !px-3"><Search size={15} /></button>
            </form>
            <div className="mt-2 space-y-1">
              {cands.map((c) => (
                <button key={c.key} onClick={() => open(LABEL_OF[kind], c.key)} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] hover:bg-bg-2">
                  <span className="truncate">{c.name}</span><span className="num ml-auto text-[11px] text-ink-3">{c.score}{c.ambiguous ? " ?" : ""}</span>
                </button>
              ))}
            </div>
          </Panel>
          <Panel title="Algorithms" right={busy ? <Spinner /> : null}>
            <div className="grid gap-2">
              <button className="btn justify-start" onClick={() => runAlgo("key_people")}><Users size={14} /> Bus factor · algo.betweenness</button>
              <button className="btn justify-start" onClick={() => runAlgo("isolated_entities")}><Unplug size={14} /> Isolated entities · algo.WCC</button>
              <button className="btn justify-start" onClick={() => runAlgo("communities")}><Share2 size={14} /> Teams by shared clients · algo.labelPropagation</button>
              <button className="btn justify-start" onClick={() => runAlgo("audit_bid")}><AlertTriangle size={14} /> Audit past bids (integrity)</button>
            </div>
            <div className="mt-3 border-t hairline pt-3">
              <div className="eyebrow mb-1.5">Explain a connection · algo.SPpaths</div>
              <div className="grid gap-1.5">
                <input className="input !py-1.5 !text-[13px]" value={pa.a} onChange={(e) => setPa({ ...pa, a: e.target.value })} />
                <select className="input !py-1.5 !text-[13px]" value={pa.ak} onChange={(e) => setPa({ ...pa, ak: e.target.value })}>{KINDS.map((k) => <option key={k}>{k}</option>)}</select>
                <input className="input !py-1.5 !text-[13px]" value={pa.b} onChange={(e) => setPa({ ...pa, b: e.target.value })} />
                <select className="input !py-1.5 !text-[13px]" value={pa.bk} onChange={(e) => setPa({ ...pa, bk: e.target.value })}>{KINDS.map((k) => <option key={k}>{k}</option>)}</select>
                <button className="btn btn-primary justify-center" onClick={path}><Route size={14} /> Find paths</button>
              </div>
            </div>
          </Panel>
        </div>
        <div className="space-y-4 lg:col-span-8">
          <Panel title="Graph" right={<button className="text-[12px] text-ink-3 hover:text-ink" onClick={() => { setGraph({ nodes: [], edges: [] }); setHl(new Set()); }}>clear</button>} pad={false}>
            <div className="p-2">
              <GraphCanvas data={graph} highlight={hl} height={520}
                onNodeClick={(n: GNode) => { const [label, ...rest] = n.id.split(":"); open(label, rest.join(":")); }} />
            </div>
            <div className="px-4 pb-3 text-[12px] text-ink-3">Click a node to expand its neighbourhood.</div>
          </Panel>
          <div className="grid gap-4 md:grid-cols-2">
            {entity && (
              <Panel title="Entity">
                <div className="scroll-thin max-h-72 space-y-1 overflow-y-auto">
                  {Object.entries(entity).filter(([k]) => !k.startsWith("_") && !["aliases", "aliases_text", "views"].includes(k)).map(([k, v]) => (
                    <div key={k} className="flex gap-3 text-[12.5px]"><span className="w-36 shrink-0 text-ink-3">{k}</span>
                      <span className="num min-w-0 break-words">{typeof v === "number" && v > 1e6 ? `${v.toLocaleString("en-IN")} (${fmtINR(v)})` : String(v)}</span></div>
                  ))}
                </div>
              </Panel>
            )}
            {algo && <AlgoResult a={algo} />}
          </div>
        </div>
      </div>
    </div>
  );
}

function AlgoResult({ a }: { a: { name: string; data: any } }) {
  const r = a.data.result;
  return (
    <Panel title={a.name.replace(/_/g, " ")}>
      <div className="scroll-thin max-h-80 space-y-1.5 overflow-y-auto text-[12.5px]">
        {a.name === "key_people" && r.people.map((p: any) => (
          <div key={p.key} className="flex items-center gap-2"><span className="w-36 truncate font-medium">{p.name}</span>
            <span className="h-1.5 flex-1 rounded bg-bg-2"><span className="block h-1.5 rounded bg-accent" style={{ width: `${(p.score / r.people[0].score) * 100}%` }} /></span>
            <span className="num w-28 text-right text-ink-3">{p.clients} clients · {p.works} works</span></div>
        ))}
        {a.name === "isolated_entities" && (<>
          <div className="text-ink-2">Main component: <b className="num">{r.main_component_size}</b> people & clients. Outside it:</div>
          {r.isolated.map((m: any) => <div key={m.key} className="chip">{m.label}: {m.name}</div>)}
          <div className="text-ink-3">A client we invoice with no completed work on record, and people who never led a completed work.</div>
        </>)}
        {a.name === "communities" && r.map((c: any) => <div key={c.communityId}><span className="num text-ink-3">#{c.communityId} ({c.size})</span> {c.people.slice(0, 8).join(", ")}{c.size > 8 ? "…" : ""}</div>)}
        {a.name === "audit_bid" && r.map((b: any) => (
          <div key={b.bid} className="panel-2 px-2.5 py-2"><div className="font-semibold">{b.bid} <span className="text-ink-3">· submitted {b.submitted}</span></div>
            <div style={{ color: b.n_violations ? "var(--fail)" : "var(--pass)" }}>{b.cited_before_completion.length} cited works were not yet complete at submission</div></div>
        ))}
        {a.name === "explain_path" && (r.length ? r.map((p: any, i: number) => <div key={i}><span className="num text-ink-3">{p.hops} hops</span> {p.path}</div>) : <div className="text-ink-3">No path within 6 hops.</div>)}
      </div>
      {a.data.cypher?.[0] && <details className="mt-2"><summary className="cursor-pointer text-[12px] text-ink-3">Cypher</summary><CypherBlock q={a.data.cypher[0]} /></details>}
    </Panel>
  );
}
