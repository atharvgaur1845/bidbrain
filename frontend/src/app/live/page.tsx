"use client";

import clsx from "clsx";
import { ArrowRight, BookMarked, FileUp, Gavel, GitBranch, Radio, Upload, Waves, Zap } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import GraphCanvas, { GData, mergeGraph } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { Panel, Spinner, Verdict, useOpenDoc } from "@/components/ui";
import { API_BASE, api, stream } from "@/lib/api";

const TYPE_LABEL: Record<string, string> = {
  corrigendum_value: "Corrigendum", revised_completion: "Revised certificate", bond_invoked: "Bank: invocation",
  bond_extended: "Bank: extension", hr_experience: "HR verification", llm_grading: "LLM extraction",
  jv_role_dispute: "JV partner certificate", letter_grading: "Client letter", rounding_restatement: "Brochure (rounded)",
  consistent_restatement: "Client letter (same value)", unrelated_field: "Corrigendum (DLP only)", demo: "Scenario",
};

type FeedItem = { kind: string; at: number; data: any };

export default function LivePage() {
  const [events, setEvents] = useState<any[]>([]);
  const [samples, setSamples] = useState<any[]>([]);
  const [conf, setConf] = useState<any>({ cards: [], decisions: [], rules: [] });
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [graph, setGraph] = useState<GData>({ nodes: [], edges: [] });
  const [hl, setHl] = useState<Set<string>>(new Set());
  const [cases, setCases] = useState<any[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);
  const openDoc = useOpenDoc();

  const refresh = useCallback(() => {
    api("/api/conflicts").then(setConf).catch(() => {});
    api("/api/cases").then(setCases).catch(() => {});
  }, []);
  useEffect(() => {
    api("/api/events").then(setEvents).catch(() => {});
    api("/api/samples").then(setSamples).catch(() => {});
    refresh();
  }, [refresh]);

  const push = (kind: string, data: any) => setFeed((f) => [{ kind, at: Date.now(), data }, ...f].slice(0, 60));

  const onEv = (ev: any) => {
    if (ev.type === "document") push("document", ev);
    if (ev.type === "extracted") push("extracted", ev);
    if (ev.type === "decision") push("decision", ev);
    if (ev.type === "card") push("card", ev);
    if (ev.type === "ripple") {
      push("ripple", ev);
      if (ev.subgraph) { setGraph((g) => mergeGraph(g, ev.subgraph)); setHl(new Set(ev.subgraph.nodes.map((n: any) => n.id))); }
    }
    if (ev.type === "result" && ev.data && !ev.data.decisions?.length && !ev.data.cards?.length && !ev.data.error)
      push("noconflict", ev.data);
    if (ev.type === "error") push("error", ev);
  };

  async function fire(id: string) {
    setBusy(true);
    await stream(`/api/events/${id}/fire`, {}, onEv);
    setBusy(false); refresh();
  }
  async function upload(file: File | Blob, name: string) {
    setBusy(true);
    const fd = new FormData(); fd.append("file", file, name);
    await stream("/api/ingest", null, onEv, { form: fd });
    setBusy(false); refresh();
  }
  async function sendSample(s: any) {
    const b = await fetch(`${API_BASE}${s.url}`).then((r) => r.blob());
    upload(b, s.name);
  }
  async function resolve(card: any, claim: string, similar: boolean) {
    setBusy(true);
    try {
      const out = await api(`/api/conflicts/${card.id}/resolve`, { method: "POST",
        body: JSON.stringify({ winner_claim: claim, rationale: similar ? "JV lead partner's certificate is the authoritative record of our role" : "bid manager decision", apply_to_similar: similar }) });
      push("decision", { ...out.decision, conflict: { subject_key: card.key, predicate: card.predicate }, human: true });
      push("ripple", { subject: `${card.label}:${card.key}`, result: out.ripple });
      if (out.subgraph) { setGraph((g) => mergeGraph(g, out.subgraph)); setHl(new Set(out.subgraph.nodes.map((n: any) => n.id))); }
    } finally { setBusy(false); refresh(); }
  }

  const demo = events.filter((e) => e.type === "demo");
  const rest = events.filter((e) => e.type !== "demo");

  return (
    <div>
      <PageHeader eyebrow="Keeping knowledge current" title="Live documents: conflicts, decisions, ripple">
        New paperwork keeps arriving: corrigenda, re-issued certificates, bank notices, partner certificates. Each becomes claims.
        Disagreements are detected and settled by an authority/recency policy, or sent to you. Every settlement is a Decision node,
        and every open bid that relied on the fact is re-checked.
      </PageHeader>

      {cases.length === 0 && (
        <div className="panel mb-4 flex items-center gap-3 px-4 py-3 text-[13px]">
          <Zap size={16} className="text-accent" />
          <span>Tip: first <Link href="/qualify/" className="font-semibold text-accent underline">qualify a tender</Link> — then a corrigendum here can flip its verdict live.</span>
        </div>
      )}

      <div className="grid gap-5 xl:grid-cols-12">
        <div className="space-y-4 xl:col-span-4">
          <Panel title="Incoming documents" right={busy ? <Spinner label="processing" /> : null}>
            {demo.length > 0 && (
              <div className="mb-3 space-y-2">
                <div className="eyebrow">Scenarios</div>
                {demo.map((e) => (
                  <button key={e.event_id} disabled={busy} onClick={() => fire(e.event_id)}
                    className="panel-2 block w-full px-3 py-2.5 text-left hover:border-accent disabled:opacity-60">
                    <div className="flex items-center gap-2 text-[13px] font-semibold"><Radio size={13} className="text-accent" />{e.title}</div>
                    <div className="mt-0.5 text-[12px] leading-snug text-ink-3">{e.narrative}</div>
                  </button>
                ))}
              </div>
            )}
            <div className="eyebrow mb-1.5">Drop a PDF — parsed by the same extractors as the corpus</div>
            <div onClick={() => fileRef.current?.click()} onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) upload(f, f.name); }}
              className="grid cursor-pointer place-items-center rounded-xl border border-dashed px-3 py-5 text-center text-[12.5px] text-ink-3 hover:border-accent"
              style={{ borderColor: "var(--line-2)" }}>
              <Upload size={18} className="mb-1 text-accent" />Drop a corrigendum, bank notice, certificate… or click
              <input ref={fileRef} type="file" accept="application/pdf" className="hidden"
                onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f, f.name); }} />
            </div>
            {samples.length > 0 && (
              <details className="mt-2">
                <summary className="cursor-pointer text-[12px] text-ink-3">Sample PDFs ({samples.length})</summary>
                <div className="scroll-thin mt-1 max-h-40 space-y-1 overflow-y-auto">
                  {samples.map((s) => (
                    <div key={s.name} className="flex items-center gap-2 text-[12px]">
                      <button className="num text-ink-2 hover:text-accent" onClick={() => openDoc({ doc_id: s.name.replace(".pdf", "") })}>{s.name}</button>
                      <button className="btn ml-auto !px-2 !py-0.5 !text-[11px]" disabled={busy} onClick={() => sendSample(s)}><FileUp size={11} />send</button>
                    </div>
                  ))}
                </div>
              </details>
            )}
            <details className="mt-3">
              <summary className="cursor-pointer text-[12px] text-ink-3">Planted events ({rest.length}) — including hard negatives</summary>
              <div className="scroll-thin mt-1 max-h-56 space-y-1 overflow-y-auto">
                {rest.map((e) => (
                  <button key={e.event_id} disabled={busy} onClick={() => fire(e.event_id)}
                    className="block w-full rounded-md px-2 py-1.5 text-left hover:bg-bg-2">
                    <span className="chip !py-0 !text-[10.5px]">{TYPE_LABEL[e.type] || e.type}</span>
                    <span className="mt-0.5 block text-[12px] leading-snug text-ink-2">{e.narrative}</span>
                  </button>
                ))}
              </div>
            </details>
          </Panel>

          <Panel title="Learned rules (procedural memory)">
            {conf.rules?.length ? conf.rules.map((r: any) => (
              <div key={r.id} className="panel-2 mb-2 px-3 py-2 text-[12.5px]">
                <div className="flex items-center gap-2"><BookMarked size={13} className="text-violet" />
                  <span className="font-semibold">prefer {r.prefer}</span><span className="text-ink-3">over {r.over} · {r.predicate}</span></div>
                <div className="mt-0.5 text-ink-3">learned from {r.learned_from} by {r.by} · applied {r.applied}×</div>
              </div>
            )) : <div className="text-[12.5px] text-ink-3">None yet. Resolve a card with "apply to similar" and the next conflict of that shape resolves itself, citing your decision.</div>}
          </Panel>
        </div>

        <div className="space-y-4 xl:col-span-5">
          <Panel title="What happened" pad={false}>
            <div className="scroll-thin max-h-[72vh] space-y-2 overflow-y-auto p-3">
              {feed.length === 0 && <div className="p-3 text-[13px] text-ink-3">Send a document to watch it flow through claims → conflicts → decisions → ripple.</div>}
              {feed.map((f, i) => <FeedCard key={f.at + "-" + i} item={f} />)}
            </div>
          </Panel>
        </div>

        <div className="space-y-4 xl:col-span-3">
          <Panel title={`Needs your judgement (${conf.cards?.length || 0})`}>
            {conf.cards?.length ? conf.cards.map((c: any) => (
              <div key={c.id} className="panel-2 mb-3 px-3 py-2.5">
                <div className="text-[13px] font-semibold leading-snug">{c.subject}</div>
                <div className="num text-[11.5px] text-ink-3">{c.predicate}</div>
                <div className="mt-2 space-y-1.5">
                  {c.claims.map((cl: any) => (
                    <div key={cl.id} className="rounded-md border hairline p-2">
                      <div className="flex items-center gap-2">
                        <span className="num text-[13.5px] font-bold">{cl.value}</span>
                        {c.suggested === cl.id && <span className="chip !py-0 !text-[10px]">suggested</span>}
                      </div>
                      <div className="text-[11.5px] text-ink-3">{cl.source_kind} · authority {cl.authority} · {cl.issuer}</div>
                      <button className="num text-[11.5px] text-accent" onClick={() => openDoc({ doc_id: cl.source_doc, page: cl.page })}>{cl.source_doc}</button>
                      <div className="mt-1.5 flex gap-1.5">
                        <button className="btn !px-2 !py-1 !text-[11.5px]" disabled={busy} onClick={() => resolve(c, cl.id, false)}>Choose</button>
                        <button className="btn !px-2 !py-1 !text-[11.5px]" disabled={busy} onClick={() => resolve(c, cl.id, true)} title="Also learn a rule for conflicts of this shape">Choose + learn rule</button>
                      </div>
                    </div>
                  ))}
                </div>
                <div className="mt-2 text-[11.5px] leading-snug text-ink-3">{c.rationale}</div>
              </div>
            )) : <div className="text-[12.5px] text-ink-3">No open conflicts. Close-authority disagreements from different issuers land here.</div>}
          </Panel>
          <Panel title="Ripple" pad={false}>
            <div className="p-2"><GraphCanvas data={graph} highlight={hl} height={260} emptyHint="Cases & bids touched by a change." /></div>
          </Panel>
          <Panel title="Decision log" right={<Link href="/decisions/" className="text-[12px] text-accent">all <ArrowRight size={11} className="inline" /></Link>}>
            <div className="text-[12.5px] text-ink-2">{conf.decisions?.length || 0} decisions on record ·{" "}
              <span className="text-ink-3">{conf.restatements} are financial restatements the corpus itself contains</span></div>
          </Panel>
        </div>
      </div>
    </div>
  );
}

function FeedCard({ item }: { item: FeedItem }) {
  const d = item.data;
  const openDoc = useOpenDoc();
  if (item.kind === "document")
    return (
      <div className="flex items-start gap-2 rounded-lg px-2 py-1.5">
        <FileUp size={15} className="mt-0.5 text-info" />
        <div className="text-[12.5px]"><span className="font-semibold">{d.doc?.title || d.doc?.doc_id}</span>
          {d.narrative && <div className="text-ink-3">{d.narrative}</div>}</div>
      </div>
    );
  if (item.kind === "extracted")
    return (
      <div className="panel-2 px-3 py-2 text-[12.5px]">
        <div className="eyebrow">Extracted · {d.kind}</div>
        {d.error ? <div style={{ color: "var(--fail)" }}>{d.error}</div> : (
          <>
            <div className="mt-0.5">subject <span className="num font-semibold">{d.subject?.key}</span> <span className="text-ink-3">(resolved via {d.subject?.resolved_via})</span></div>
            {(d.extracted || []).map((x: any, i: number) => <div key={i} className="num text-ink-2">{x.predicate} = {String(x.value)} <span className="text-ink-3">· {x.source_kind}</span></div>)}
          </>
        )}
      </div>
    );
  if (item.kind === "decision") {
    const c = d.conflict || {};
    return (
      <div className="rounded-lg border px-3 py-2.5" style={{ borderColor: "color-mix(in oklab, var(--pass) 35%, transparent)" }}>
        <div className="flex items-center gap-2 text-[12.5px]"><Gavel size={14} style={{ color: "var(--pass)" }} />
          <span className="font-semibold">{d.human ? "Your decision" : "Auto-resolved"}</span>
          <span className="chip !py-0">{d.policy}</span><span className="num ml-auto text-[11px] text-ink-3">{d.decision_id}</span></div>
        <div className="mt-1 text-[12.5px]"><span className="num">{c.subject_key}</span> · <span className="num">{c.predicate}</span> → <span className="num font-bold">{String(d.value)}</span></div>
        {c.claims && (
          <div className="mt-1.5 grid gap-1">
            {c.claims.map((cl: any) => (
              <div key={cl.id} className={clsx("flex items-center gap-2 rounded px-2 py-1 text-[11.5px]", cl.id === d.winner ? "bg-bg-2" : "opacity-60 line-through")}>
                <span className="num font-semibold">{cl.value_str}</span><span className="text-ink-3">{cl.source_kind} ({cl.authority})</span>
                <button className="num ml-auto text-accent" onClick={() => openDoc({ doc_id: cl.source_doc, page: cl.page })}>{cl.source_doc}</button>
              </div>
            ))}
          </div>
        )}
        <div className="mt-1.5 text-[12px] leading-snug text-ink-3">{d.rationale}</div>
      </div>
    );
  }
  if (item.kind === "card")
    return (
      <div className="rounded-lg border px-3 py-2.5 text-[12.5px]" style={{ borderColor: "color-mix(in oklab, var(--risk) 45%, transparent)" }}>
        <div className="flex items-center gap-2"><GitBranch size={14} style={{ color: "var(--risk)" }} /><span className="font-semibold">Conflict needs a human</span></div>
        <div className="mt-1 num">{d.conflict?.subject_key} · {d.conflict?.predicate}</div>
        <div className="mt-1 text-ink-3">{d.rationale}</div>
      </div>
    );
  if (item.kind === "ripple") {
    const r = d.result || {};
    return (
      <div className="rounded-lg border px-3 py-2.5 text-[12.5px]" style={{ borderColor: "color-mix(in oklab, var(--accent) 40%, transparent)" }}>
        <div className="flex items-center gap-2"><Waves size={14} className="text-accent" /><span className="font-semibold">Ripple from {d.subject}</span></div>
        <div className="mt-1 text-ink-2">{r.clauses_rechecked} open clauses re-checked · {r.cases_relying?.length || 0} cases relied on it · {r.past_bids_citing?.length || 0} past bids cited it{r.tenders_secured?.length ? ` · secures ${r.tenders_secured.map((t: any) => t.tender).join(", ")}` : ""}</div>
        {(r.verdict_flips || []).map((f: any, i: number) => (
          <div key={i} className="mt-1.5 flex items-center gap-2 rounded bg-bg-2 px-2 py-1.5">
            <span className="truncate">{f.case} · clause {f.clause}</span>
            <span className="ml-auto flex items-center gap-1"><Verdict v={f.from} size="sm" /><ArrowRight size={12} /><Verdict v={f.to} size="sm" /></span>
          </div>
        ))}
        {[...new Map((r.verdict_flips || []).filter((f: any) => f.case_from && f.case_from !== f.case_to)
            .map((f: any) => [f.case_id, f])).values()].map((f: any) => (
          <div key={f.case_id} className="mt-1.5 flex items-center gap-2 rounded px-2 py-1.5" style={{ background: "color-mix(in oklab, var(--accent) 10%, transparent)" }}>
            <span className="font-semibold">Recommendation for “{f.case}”</span>
            <span className="ml-auto flex items-center gap-1"><Verdict v={f.case_from} size="sm" /><ArrowRight size={12} /><Verdict v={f.case_to} size="sm" /></span>
          </div>
        ))}
        {!(r.verdict_flips || []).length && <div className="mt-1 text-ink-3">No verdict changed.</div>}
      </div>
    );
  }
  if (item.kind === "noconflict")
    return <div className="rounded-lg px-3 py-2 text-[12.5px] text-ink-3">No contradiction: the new claim agrees with the record (within the source's precision) or concerns a field nothing else states. Stored, nothing overwritten.</div>;
  if (item.kind === "error") return <div className="px-3 py-2 text-[12.5px]" style={{ color: "var(--fail)" }}>{d.message}</div>;
  return null;
}
