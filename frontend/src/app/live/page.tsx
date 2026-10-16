"use client";

import clsx from "clsx";
import { ArrowRight, Check, CircleAlert, CircleCheck, FileText, Gavel, Scale, ScanText, Upload, Waves, FileStack as FileStackIcon } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import GraphCanvas, { GData, mergeGraph } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { Badge, Button, Card, Empty, EvidenceList, Spinner, Tabs, Verdict, useOpenDoc } from "@/components/ui";
import { API_BASE, api, stream } from "@/lib/api";

const TYPE_LABEL: Record<string, string> = {
  corrigendum_value: "Corrigendum", revised_completion: "Revised certificate", bond_invoked: "Bank: invocation",
  bond_extended: "Bank: extension", hr_experience: "HR verification", llm_grading: "LLM extraction",
  jv_role_dispute: "JV partner certificate", letter_grading: "Client letter", rounding_restatement: "Brochure (rounded)",
  consistent_restatement: "Client letter (same value)", unrelated_field: "Corrigendum (DLP only)", demo: "Scenario",
};

type FeedItem = { kind: string; at: number; data: any };

/** Why the bid manager chose a claim, in words built from the card (the learned rule cites this). */
function rationaleFor(card: any, claim: string, similar: boolean): string {
  const win = (card.claims || []).find((c: any) => c.id === claim);
  const losers = (card.claims || []).filter((c: any) => c.id !== claim).map((c: any) => (c.source_kind || "").replace(/_/g, " "));
  const what = `${(card.predicate || "this fact").replace(/_/g, " ")} of ${card.subject || card.key}`;
  if (!win) return `bid manager decision on the ${what}`;
  const src = `${(win.source_kind || "source").replace(/_/g, " ")} from ${win.issuer || "its issuer"}`;
  return similar
    ? `For ${(card.predicate || "this fact").replace(/_/g, " ")}, the ${src} is the authoritative record over the ${[...new Set(losers)].join(", ")}`
    : `bid manager chose the ${src} for the ${what}`;
}

export default function LivePage() {
  const [events, setEvents] = useState<any[]>([]);
  const [samples, setSamples] = useState<any[]>([]);
  const [conf, setConf] = useState<any>({ cards: [], decisions: [], rules: [] });
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [graph, setGraph] = useState<GData>({ nodes: [], edges: [] });
  const [hl, setHl] = useState<Set<string>>(new Set());
  const [cases, setCases] = useState<any[]>([]);
  const [tab, setTab] = useState("scenarios");
  const [dragOver, setDragOver] = useState(false);
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
        body: JSON.stringify({ winner_claim: claim, rationale: rationaleFor(card, claim, similar), apply_to_similar: similar }) });
      push("decision", { ...out.decision, conflict: { subject_key: card.key, predicate: card.predicate }, human: true });
      push("ripple", { subject: `${card.label}:${card.key}`, result: out.ripple });
      if (out.subgraph) { setGraph((g) => mergeGraph(g, out.subgraph)); setHl(new Set(out.subgraph.nodes.map((n: any) => n.id))); }
    } finally { setBusy(false); refresh(); }
  }

  const demo = events.filter((e) => e.type === "demo");
  const rest = events.filter((e) => e.type !== "demo");
  const cards: any[] = conf.cards || [];
  const rules: any[] = conf.rules || [];

  return (
    <div>
      <PageHeader icon={<FileStackIcon size={19} strokeWidth={1.75} />} tone="peach" title="Live documents"
        subtitle="New paperwork becomes claims. Conflicts are settled by policy or by you, and every bid relying on the fact is re-checked."
        meta={<>
          <span><span className="num font-semibold text-ink-2">{conf.decisions?.length || 0}</span> decisions on record</span>
          {conf.restatements !== undefined && conf.restatements !== null && <>
            <span aria-hidden>·</span>
            <span><span className="num font-semibold text-ink-2">{conf.restatements}</span> financial restatements in the corpus</span>
          </>}
          <span aria-hidden>·</span>
          <Link href="/decisions/" className="inline-flex items-center gap-1 font-medium text-kb-ink hover:underline">
            Decision log <ArrowRight size={13} strokeWidth={1.75} />
          </Link>
        </>} />

      {cases.length === 0 && (
        <div className="well mb-5 px-4 py-3 text-[13.5px] text-ink-2">
          No qualified tenders yet. <Link href="/qualify/" className="font-semibold text-kb-ink hover:underline">Qualify a tender</Link> first,
          then a corrigendum here can flip its verdict live.
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-12">
        {/* ---------------- inbox */}
        <div className="flex min-w-0 flex-col gap-5 lg:col-span-5 xl:col-span-4">
          <Card title="Inbox" action={busy ? <Spinner label="Processing" /> : null}>
            <Tabs value={tab} onChange={setTab} tabs={[
              { id: "scenarios", label: "Scenarios", count: demo.length },
              { id: "upload", label: "Upload" },
              { id: "planted", label: "Planted events", count: rest.length },
            ]} />

            {tab === "scenarios" && (
              demo.length ? (
                <ul className="scroll-thin contain-scroll max-h-[62vh] divide-y divide-line overflow-y-auto">
                  {demo.map((e) => (
                    <li key={e.event_id} className="flex items-start gap-3 py-3">
                      <div className="min-w-0 flex-1">
                        <div className="text-[13.5px] font-semibold leading-snug text-ink">{e.title}</div>
                        <p className="mt-1 text-[12.5px] leading-snug text-ink-3">{e.narrative}</p>
                      </div>
                      <Button size="sm" disabled={busy} onClick={() => fire(e.event_id)}>Send</Button>
                    </li>
                  ))}
                </ul>
              ) : <Empty title="No scenarios" text="Scenarios load from the server when it starts." />
            )}

            {tab === "upload" && (
              <div className="pt-4">
                <div role="button" tabIndex={0} aria-label="Upload a PDF"
                  onClick={() => fileRef.current?.click()}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileRef.current?.click(); } }}
                  onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                  onDragLeave={() => setDragOver(false)}
                  onDrop={(e) => { e.preventDefault(); setDragOver(false); const f = e.dataTransfer.files[0]; if (f) upload(f, f.name); }}
                  className={clsx("grid cursor-pointer place-items-center rounded-xl border border-dashed px-4 py-8 text-center outline-none",
                    "transition-[border-color,background-color,box-shadow] duration-150 ease-out hover:border-kb hover:bg-well",
                    "focus-visible:border-kb focus-visible:shadow-[0_0_0_3px_var(--kb-soft)]",
                    dragOver ? "border-kb bg-kb-soft" : "border-line-strong")}>
                  <Upload size={16} strokeWidth={1.75} className="mb-2 text-ink-3" />
                  <div className="text-[13.5px] font-semibold text-ink">Drop a PDF, or click to choose</div>
                  <div className="mt-1 max-w-[280px] text-[12.5px] leading-snug text-ink-3">
                    Corrigenda, bank notices, certificates. Parsed by the same extractors as the corpus.
                  </div>
                  <input ref={fileRef} type="file" accept="application/pdf" className="hidden"
                    onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f, f.name); }} />
                </div>

                {samples.length > 0 && (
                  <div className="mt-5">
                    <div className="flex items-baseline gap-2">
                      <h3 className="text-[13.5px] font-semibold text-ink">Sample PDFs</h3>
                      <span className="num text-[12px] text-ink-3">{samples.length}</span>
                    </div>
                    <ul className="scroll-thin contain-scroll mt-1 max-h-[40vh] divide-y divide-line overflow-y-auto">
                      {samples.map((s) => (
                        <li key={s.name} className="flex items-center gap-2 py-2">
                          <span className="num min-w-0 flex-1 truncate text-[12.5px] text-ink" title={s.name}>{s.name}</span>
                          <Button size="sm" variant="ghost" onClick={() => openDoc({ doc_id: s.name.replace(".pdf", "") })}>View</Button>
                          <Button size="sm" disabled={busy} onClick={() => sendSample(s)}>Send</Button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}

            {tab === "planted" && (
              rest.length ? (
                <>
                  <p className="pt-3 text-[12.5px] text-ink-3">Includes hard negatives that should not raise a conflict.</p>
                  <ul className="scroll-thin contain-scroll max-h-[58vh] divide-y divide-line overflow-y-auto">
                    {rest.map((e) => (
                      <li key={e.event_id} className="flex items-start gap-3 py-3">
                        <div className="min-w-0 flex-1">
                          <Badge>{TYPE_LABEL[e.type] || e.type}</Badge>
                          <p className="mt-2 text-[12.5px] leading-snug text-ink-2">{e.narrative}</p>
                        </div>
                        <Button size="sm" disabled={busy} onClick={() => fire(e.event_id)}>Send</Button>
                      </li>
                    ))}
                  </ul>
                </>
              ) : <Empty title="No planted events" />
            )}
          </Card>
        </div>

        {/* ---------------- activity */}
        <div className="flex min-w-0 flex-col gap-5 lg:col-span-7 xl:col-span-5">
          <Card title="Activity" pad={false}
            action={feed.length ? <span className="num text-[12.5px] text-ink-3">{feed.length} events</span> : null}>
            <div className="scroll-thin contain-scroll max-h-[72vh] overflow-y-auto px-5 pb-3 pt-3">
              {feed.length === 0 ? (
                <Empty title="No activity yet"
                  text="Send a scenario or a PDF from the inbox. Claims, conflicts, decisions and re-checks appear here." />
              ) : (
                <ol>
                  {feed.map((f, i) => <FeedCard key={f.at + "-" + i} item={f} last={i === feed.length - 1} />)}
                </ol>
              )}
            </div>
          </Card>
        </div>

        {/* ---------------- judgement, rules, ripple */}
        <div className="grid min-w-0 grid-cols-1 content-start gap-5 lg:col-span-12 lg:grid-cols-2 xl:col-span-3 xl:grid-cols-1">
          <Card title="Needs your judgement" action={<span className="num text-[13px] text-ink-3">{cards.length}</span>}>
            {cards.length ? (
              <div className="-mt-1 divide-y divide-line">
                {cards.map((c: any) => (
                  <div key={c.id} className="py-4 first:pt-1 last:pb-0">
                    <div className="text-[13.5px] font-semibold leading-snug text-ink">{c.subject}</div>
                    <div className="num mt-1 text-[12px] text-ink-3">{c.predicate}</div>
                    <div className="mt-3 flex flex-col gap-2">
                      {c.claims.map((cl: any) => (
                        <div key={cl.id} className="well p-3">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="num text-[15px] font-semibold text-ink">{cl.value}</span>
                            {c.suggested === cl.id && <Badge tone="blue">Suggested</Badge>}
                          </div>
                          <div className="mt-1 text-[12px] leading-snug text-ink-3">
                            {cl.source_kind} · authority <span className="num">{cl.authority}</span> · {cl.issuer}
                          </div>
                          {cl.source_doc && (
                            <div className="mt-1">
                              <EvidenceList items={[{ doc_id: cl.source_doc, page: cl.page }]} max={1} />
                            </div>
                          )}
                          <div className="mt-2 flex flex-wrap gap-2">
                            <Button size="sm" disabled={busy} onClick={() => resolve(c, cl.id, false)}>Choose</Button>
                            <Button size="sm" variant="primary" disabled={busy} onClick={() => resolve(c, cl.id, true)}
                              title="Also learn a rule for conflicts of this shape">Choose and learn rule</Button>
                          </div>
                        </div>
                      ))}
                    </div>
                    {c.rationale && <p className="mt-3 text-[12.5px] leading-snug text-ink-3">{c.rationale}</p>}
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-[13px] leading-snug text-ink-3">
                No open conflicts. Disagreements between issuers of similar authority land here.
              </p>
            )}
          </Card>

          <Card title="Learned rules" action={rules.length ? <span className="num text-[13px] text-ink-3">{rules.length}</span> : null}>
            {rules.length ? (
              <ul className="-mt-1 divide-y divide-line">
                {rules.map((r: any) => (
                  <li key={r.id} className="py-3 first:pt-1 last:pb-0">
                    <div className="text-[13px] leading-snug text-ink">
                      Prefer <span className="font-semibold">{r.prefer}</span> <span className="text-ink-3">over</span> {r.over}
                    </div>
                    <div className="num mt-1 text-[12px] text-ink-3">{r.predicate}</div>
                    <div className="mt-1 text-[12px] text-ink-3">
                      From {r.learned_from} by {r.by} · applied <span className="num">{r.applied}</span>×
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-[13px] leading-snug text-ink-3">
                None yet. Use “Choose and learn rule” on a conflict and the next one of that shape resolves itself, citing your decision.
              </p>
            )}
          </Card>

          <Card title="Ripple" pad={false} className="overflow-hidden lg:col-span-2 xl:col-span-1">
            <GraphCanvas data={graph} highlight={hl} height={260} emptyHint="Cases and bids touched by a change appear here." />
          </Card>
        </div>
      </div>
    </div>
  );
}

// ======================================================================= activity feed

const fmtTime = (t: number) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function Entry({ icon, title, aside, at, last, children }:
  { icon: React.ReactNode; title: React.ReactNode; aside?: React.ReactNode; at: number; last?: boolean; children?: React.ReactNode }) {
  return (
    <li className="flex gap-3">
      <div className="flex w-4 shrink-0 flex-col items-center pt-0.5">
        {icon}
        {!last && <span aria-hidden className="mt-2 w-px flex-1 bg-line" />}
      </div>
      <div className={clsx("min-w-0 flex-1", last ? "pb-2" : "pb-5")}>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-[13.5px] font-semibold leading-snug text-ink">{title}</span>
          {aside}
          <time dateTime={new Date(at).toISOString()} className="num ml-auto text-[11.5px] text-ink-3">{fmtTime(at)}</time>
        </div>
        {children}
      </div>
    </li>
  );
}

const ICON = { size: 16, strokeWidth: 1.75 } as const;

function FeedCard({ item, last }: { item: FeedItem; last?: boolean }) {
  const d = item.data;
  const openDoc = useOpenDoc();
  const base = { at: item.at, last };

  if (item.kind === "document")
    return (
      <Entry {...base} icon={<FileText {...ICON} className="text-kb-ink" />} title={d.doc?.title || d.doc?.doc_id}>
        {d.narrative && <p className="mt-1 text-[13px] leading-snug text-ink-3">{d.narrative}</p>}
      </Entry>
    );

  if (item.kind === "extracted")
    return (
      <Entry {...base} icon={<ScanText {...ICON} className="text-ink-3" />} title={`Extracted ${d.kind ?? ""}`.trim()}>
        {d.error ? <p className="mt-1 text-[13px] text-bad">{d.error}</p> : (
          <>
            <p className="mt-1 text-[13px] text-ink-2">
              Subject <span className="num font-semibold text-ink">{d.subject?.key}</span>
              <span className="text-ink-3"> · resolved via {d.subject?.resolved_via}</span>
            </p>
            {(d.extracted || []).length > 0 && (
              <ul className="well mt-2 flex flex-col gap-1 px-3 py-2">
                {(d.extracted || []).map((x: any, i: number) => (
                  <li key={i} className="num flex flex-wrap gap-x-2 text-[12px] text-ink-2">
                    <span>{x.predicate} = <span className="font-semibold text-ink">{String(x.value)}</span></span>
                    <span className="text-ink-3">{x.source_kind}</span>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </Entry>
    );

  if (item.kind === "decision") {
    const c = d.conflict || {};
    return (
      <Entry {...base} icon={<Gavel {...ICON} className="text-ok" />}
        title={d.human ? "Your decision" : "Auto-resolved"}
        aside={d.policy ? <Badge>{d.policy}</Badge> : null}>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-2 text-[13px] text-ink-2">
          <span><span className="num">{c.subject_key}</span> · <span className="num">{c.predicate}</span>
            <ArrowRight size={13} strokeWidth={1.75} className="mx-1 inline align-[-2px] text-ink-3" />
            <span className="num font-semibold text-ink">{String(d.value)}</span></span>
          {d.decision_id && <span className="num ml-auto text-[11.5px] text-ink-3">{d.decision_id}</span>}
        </div>
        {c.claims && (
          <ul className="well mt-2 divide-y divide-line overflow-hidden">
            {c.claims.map((cl: any) => {
              const won = cl.id === d.winner;
              return (
                <li key={cl.id} className={clsx("flex items-center gap-2 px-3 py-2 text-[12.5px]", won && "bg-ok-soft")}>
                  <span className="w-4 shrink-0">{won && <Check size={14} strokeWidth={1.75} className="text-ok" />}</span>
                  <span className={clsx("num", won ? "font-semibold text-ink" : "text-ink-3 line-through")}>{cl.value_str}</span>
                  <span className="truncate text-ink-3">{cl.source_kind} ({cl.authority})</span>
                  <button className="num ml-auto shrink-0 text-[12px] text-kb-ink hover:underline"
                    onClick={() => openDoc({ doc_id: cl.source_doc, page: cl.page })}>{cl.source_doc}</button>
                </li>
              );
            })}
          </ul>
        )}
        {d.rationale && <p className="mt-2 text-[12.5px] leading-snug text-ink-3">{d.rationale}</p>}
      </Entry>
    );
  }

  if (item.kind === "card")
    return (
      <Entry {...base} icon={<Scale {...ICON} className="text-warn" />} title="Sent to you">
        <p className="num mt-1 text-[12.5px] text-ink-2">{d.conflict?.subject_key} · {d.conflict?.predicate}</p>
        {d.rationale && <p className="mt-1 text-[12.5px] leading-snug text-ink-3">{d.rationale}</p>}
      </Entry>
    );

  if (item.kind === "ripple") {
    const r = d.result || {};
    const flips: any[] = r.verdict_flips || [];
    const caseFlips = [...new Map(flips.filter((f: any) => f.case_from && f.case_from !== f.case_to)
      .map((f: any) => [f.case_id, f])).values()];
    const n = (v: React.ReactNode) => <span className="num font-semibold text-ink">{v}</span>;
    return (
      <Entry {...base} icon={<Waves {...ICON} className="text-kb-ink" />}
        title={<>Ripple from <span className="num">{d.subject}</span></>}>
        <p className="mt-1 text-[13px] leading-relaxed text-ink-2">
          {n(r.clauses_rechecked ?? 0)} open clauses re-checked · {n(r.cases_relying?.length || 0)} cases relied on it
          · {n(r.past_bids_citing?.length || 0)} past bids cited it
          {r.tenders_secured?.length ? <> · secures <span className="num">{r.tenders_secured.map((t: any) => t.tender).join(", ")}</span></> : null}
        </p>
        {flips.length > 0 && (
          <ul className="well mt-2 divide-y divide-line overflow-hidden">
            {flips.map((f: any, i: number) => (
              <li key={i} className="flex items-center gap-2 px-3 py-2 text-[12.5px]">
                <span className="min-w-0 truncate text-ink-2">{f.case} · clause <span className="num">{f.clause}</span></span>
                <span className="ml-auto flex shrink-0 items-center gap-1">
                  <Verdict v={f.from} size="sm" /><ArrowRight size={13} strokeWidth={1.75} className="text-ink-3" /><Verdict v={f.to} size="sm" />
                </span>
              </li>
            ))}
          </ul>
        )}
        {caseFlips.map((f: any) => (
          <div key={f.case_id} className="mt-2 flex flex-wrap items-center gap-2 rounded-[10px] bg-kb-soft px-3 py-2 text-[13px]">
            <span className="min-w-0 font-semibold text-ink">Recommendation for “{f.case}”</span>
            <span className="ml-auto flex shrink-0 items-center gap-1">
              <Verdict v={f.case_from} size="sm" /><ArrowRight size={13} strokeWidth={1.75} className="text-kb-ink" /><Verdict v={f.case_to} size="sm" />
            </span>
          </div>
        ))}
        {!flips.length && <p className="mt-1 text-[12.5px] text-ink-3">No verdict changed.</p>}
      </Entry>
    );
  }

  if (item.kind === "noconflict")
    return (
      <Entry {...base} icon={<CircleCheck {...ICON} className="text-ink-3" />} title="No contradiction">
        <p className="mt-1 text-[12.5px] leading-snug text-ink-3">
          The new claim agrees with the record within the source&apos;s precision, or concerns a field nothing else states.
          Stored, nothing overwritten.
        </p>
      </Entry>
    );

  if (item.kind === "error")
    return (
      <Entry {...base} icon={<CircleAlert {...ICON} className="text-bad" />} title="Failed">
        <p className="mt-1 text-[13px] text-bad">{d.message}</p>
      </Entry>
    );

  return null;
}
