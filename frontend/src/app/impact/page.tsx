"use client";

// Change impact: one document's effect, as an inspectable chain read from the graph (GET /api/impact[/{doc_id}]):
// document -> changed fact -> adjudication -> affected requirements -> bid verdict change -> accountable actions.

import clsx from "clsx";
import { ArrowRight, ChevronRight, FileText, GitCompareArrows, Play } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/Shell";
import { Badge, Button, Card, CodeCell, EmptyState, ErrorState, Notice, SkeletonRows, Spinner, StatusBadge, Table, Td,
  Th, useOpenDoc } from "@/components/ui";
import { ActionControls, ActionFull, ActionResult, kindLabel } from "@/lib/actions";
import { api, fmtINR, stream } from "@/lib/api";
import { moneyText } from "@/components/format";
import { GraphPanel, VerdictCompare, useCaseClauses } from "./graph";

type Claim = { claim: string; predicate: string; value: string; page?: number; status?: string; subject_label?: string;
  subject_key?: string; subject?: string; decision?: string | null; policy?: string | null; rationale?: string | null;
  decided_at?: number | null; chosen?: string | null; replaced?: string[] };
type Affected = { decision: string; case_id: string; case: string; clause: string; kind: string; what: string; severity?: string;
  verdict_now?: string; case_now?: string; approved?: string | null };
type ImpactAction = { id: string; kind: string; status: string; title: string; case_id?: string | null; decision: string };
type Impact = { document: { doc_id: string; title?: string; doc_type?: string; issued?: string | null }; claims: Claim[]; decisions: string[];
  affected: Affected[]; actions: ImpactAction[]; summary: string };
type ImpactDoc = { result: Impact; evidence: unknown[]; cypher: string[] };

const words = (s?: string | null) => (s || "").replace(/_/g, " ");
const sentence = (s?: string | null) => { const w = words(s); return w.charAt(0).toUpperCase() + w.slice(1); };
const PREDICATE: Record<string, string> = { value: "Contract value", completed: "Completion date", role: "Role",
  revenue_operations: "Revenue from operations", experience_years: "Experience (years)" };
const predicate = (p?: string) => PREDICATE[p || ""] || sentence(p);
const money = (v?: string | null) => {
  if (!v) return "—";
  const n = Number(v.replace(/,/g, ""));
  return /^[\d,]{7,}(\.\d+)?$/.test(v) && !isNaN(n) ? fmtINR(n) : v;
};
const POLICY: Record<string, string> = { authority: "Authority", recency_same_issuer: "Recency (same issuer)", learned_rule: "Learned rule",
  org_rule: "Company memory", human: "Human", company_record: "Company record" };
const sevTone = (s?: string) => (s === "high" ? "bad" : s === "medium" ? "warn" : "neutral");
const issuerOf = (rationale?: string | null) => rationale?.match(/^(.+?) amended its own statement/)?.[1];
/** "Re-approve X: BID → NO-BID" carries the case verdict before and after. */
/** A document this workspace started from (applied from the company record), not one it received. */
const fromRecord = (d: Impact) => d.claims.length > 0 && d.claims.every((c) => c.policy === "company_record");
const verdictMove = (title: string) => title.match(/:\s*([A-Z][A-Z -]*?)\s*→\s*([A-Z][A-Z -]*)$/);

// ======================================================================= chain step

function Step({ n, title, children, last, meta }: { n: number; title: string; children: React.ReactNode; last?: boolean; meta?: React.ReactNode }) {
  return (
    <li className="relative grid grid-cols-[24px_minmax(0,1fr)] gap-x-3 pb-5 last:pb-0">
      {!last && <span aria-hidden className="absolute bottom-0 left-[11.5px] top-7 w-px bg-line" />}
      <span className="num relative grid h-6 w-6 place-items-center rounded-md border border-line bg-surface-2 text-[12px] font-medium text-ink-2">{n}</span>
      <div className="min-w-0">
        <div className="flex min-h-6 flex-wrap items-center gap-x-2">
          <h3 className="text-[13.5px] font-semibold text-ink">{title}</h3>
          {meta && <div className="ml-auto text-[12px] text-ink-3">{meta}</div>}
        </div>
        <div className="mt-1.5">{children}</div>
      </div>
    </li>
  );
}

// ======================================================================= one document's chain

function Chain({ doc, onActionChange }: { doc: ImpactDoc; onActionChange: () => void }) {
  const open = useOpenDoc();
  const r = doc.result;
  const [full, setFull] = useState<Record<string, ActionFull>>({});
  const clauses = useCaseClauses(r);
  const issuer = r.claims.map((c) => issuerOf(c.rationale)).find(Boolean);
  const page = r.claims.find((c) => c.page)?.page;
  // case verdict before -> after, from the review action raised for each case
  const cases = useMemo(() => {
    const m = new Map<string, { id: string; title: string; now?: string; before?: string; approved?: string | null }>();
    for (const a of r.affected) if (!m.has(a.case_id)) m.set(a.case_id, { id: a.case_id, title: a.case, now: a.case_now, approved: a.approved });
    for (const a of r.actions) {
      const mv = a.kind === "review_bid" && a.case_id ? verdictMove(a.title) : null;
      const c = a.case_id ? m.get(a.case_id) : undefined;
      if (mv && c) c.before = mv[1];
    }
    return [...m.values()];
  }, [r]);
  // fetch full actions once so executed results show
  useEffect(() => {
    let live = true;
    Promise.all(r.actions.map((a) => api<ActionFull>(`/api/actions/${encodeURIComponent(a.id)}`).catch(() => null)))
      .then((xs) => { if (live) setFull(Object.fromEntries(xs.filter(Boolean).map((x) => [x!.id, x!]))); });
    return () => { live = false; };
  }, [r.actions]);

  return (
    <div className="flex flex-col gap-4">
      <Card title="Dependency graph" subtitle="Source document → claim → decision → requirement → bid → action, as stored in FalkorDB. Select a node or line."
        pad={false}>
        <GraphPanel docId={r.document.doc_id} result={r} />
      </Card>
      {r.affected.length > 0 && (
        <Card title="Verdict change" subtitle="Before and after, with the rule that decided it, the evidence and the action it raised">
          <VerdictCompare r={r} clauses={clauses} />
        </Card>
      )}
      <Card title={r.document.title || r.document.doc_id} subtitle={r.summary}
        action={<Button size="sm" onClick={() => open({ doc_id: r.document.doc_id, page })}><FileText size={14} strokeWidth={1.75} aria-hidden /> Open PDF</Button>}>
        <ol aria-label="Impact chain">
          {/* 1 document */}
          <Step n={1} title="Document">
            <dl className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-3 gap-y-1 text-[13px]">
              <dt className="text-ink-3">Source</dt>
              <dd className="min-w-0">
                <button type="button" onClick={() => open({ doc_id: r.document.doc_id, page })}
                  className="mono max-w-full truncate rounded-sm text-left text-[12px] text-kb-ink hover:underline">
                  {r.document.doc_id}{page ? ` · p.${page}` : ""}
                </button>
              </dd>
              <dt className="text-ink-3">Type</dt><dd className="text-ink">{sentence(r.document.doc_type)}</dd>
              <dt className="text-ink-3">Issuer</dt><dd className="text-ink">{issuer || <span className="text-ink-3">not stated</span>}</dd>
            </dl>
          </Step>

          {/* 2 changed fact */}
          <Step n={2} title="Changed fact" meta={`${r.claims.length} claim${r.claims.length === 1 ? "" : "s"}`}>
            {r.claims.length ? (
              <ul className="flex flex-col gap-2">
                {r.claims.map((c) => (
                  <li key={c.claim} className="well px-3 py-2">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
                      <span className="font-medium text-ink">{c.subject || c.subject_key}</span>
                      <span className="text-ink-3">·</span>
                      <span className="text-ink-2">{predicate(c.predicate)}</span>
                      <span className="ml-auto"><StatusBadge status={c.status === "active" ? "resolved" : c.status} label={sentence(c.status)} size="sm" /></span>
                    </div>
                    <div className="num mt-1 flex flex-wrap items-center gap-x-2 text-[14px]">
                      {(c.replaced || []).length > 0 ? (c.replaced || []).map((v) => (
                        <span key={v} className="text-ink-3 line-through decoration-ink-3/60">{money(v)}</span>
                      )) : <span className="text-[12.5px] text-ink-3">first value recorded</span>}
                      <ArrowRight size={14} strokeWidth={1.75} className="text-ink-3" aria-hidden />
                      <span className="font-semibold text-ink">{money(c.chosen || c.value)}</span>
                    </div>
                    <div className="mono mt-1 text-[11.5px] text-ink-3">{c.claim}{c.subject_label ? ` · ${c.subject_label}:${c.subject_key}` : ""}</div>
                  </li>
                ))}
              </ul>
            ) : <p className="text-[13px] text-ink-3">This document carried no claims about a known fact.</p>}
          </Step>

          {/* 3 adjudication */}
          <Step n={3} title="Adjudication" meta="policy, no LLM">
            {r.claims.some((c) => c.decision) ? (
              <ul className="flex flex-col gap-2">
                {r.claims.filter((c) => c.decision).map((c) => (
                  <li key={c.claim} className="text-[13px]">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone="info">{POLICY[c.policy || ""] || sentence(c.policy)}</Badge>
                      <Link href={`/decisions/?id=${encodeURIComponent(c.decision!)}`} className="mono text-[12px] text-kb-ink hover:underline">{c.decision}</Link>
                      <span className="text-[12px] text-ink-3">trace</span>
                    </div>
                    <p className="mt-1 leading-relaxed text-ink-2">{moneyText(c.rationale)}</p>
                  </li>
                ))}
              </ul>
            ) : r.claims.some((c) => c.status === "active") ? (
              <p className="text-[13px] text-ink-2">No adjudication needed: the value agrees with what this workspace already holds, so no rival claim was set aside.</p>
            ) : <p className="text-[13px] text-ink-3">No decision yet: the claim is waiting as an open conflict on <Link className="text-kb-ink hover:underline" href="/live/">Live documents</Link>.</p>}
          </Step>

          {/* 4 affected requirements */}
          <Step n={4} title="Affected requirements" meta="graph traversal">
            {r.affected.length ? (
              <div className="overflow-hidden rounded-md border border-line">
                <Table dense label="Affected clauses">
                  <thead><tr><Th>Case</Th><Th width={64}>Clause</Th><Th width={96}>Verdict now</Th><Th width={80}>Severity</Th><Th>What changed</Th></tr></thead>
                  <tbody>
                    {r.affected.map((a, i) => (
                      <tr key={i}>
                        <Td><Link href={`/qualify/?case=${encodeURIComponent(a.case_id)}`} className="line-clamp-2 text-kb-ink hover:underline">{a.case}</Link></Td>
                        <Td><span className="num">{a.clause}</span> <span className="block text-[11.5px] text-ink-3">{words(a.kind)}</span></Td>
                        <Td><StatusBadge status={a.verdict_now} size="sm" /></Td>
                        <Td><Badge tone={sevTone(a.severity)}>{sentence(a.severity)}</Badge></Td>
                        <Td muted className="min-w-[200px] text-[12.5px]">{moneyText(a.what)}</Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </div>
            ) : (
              <Notice tone="neutral" title="Unchanged">This document affected no open bid. The fact is updated; every open case still rests on the same verdicts.</Notice>
            )}
          </Step>

          {/* 5 bid verdict change */}
          <Step n={5} title="Bid verdict change" meta="deterministic requalification">
            {cases.length ? (
              <ul className="flex flex-col gap-1.5">
                {cases.map((c) => (
                  <li key={c.id} className="flex flex-wrap items-center gap-2 text-[13px]">
                    <Link href={`/qualify/?case=${encodeURIComponent(c.id)}`} className="min-w-0 flex-1 truncate font-medium text-kb-ink hover:underline">{c.title}</Link>
                    {c.before ? <StatusBadge status={c.before} size="sm" /> : <span className="text-[12px] text-ink-3">before unknown</span>}
                    <ArrowRight size={14} strokeWidth={1.75} className="text-ink-3" aria-hidden />
                    <StatusBadge status={c.now} size="sm" />
                    {c.approved && <span className="text-[12px] text-ink-3">approved {c.approved}</span>}
                  </li>
                ))}
              </ul>
            ) : <p className="text-[13px] text-ink-3">No bid recommendation changed.</p>}
          </Step>

          {/* 6 actions */}
          <Step n={6} title="Required actions" last meta={<Link href="/actions/" className="text-kb-ink hover:underline">Action queue</Link>}>
            {r.actions.length ? (
              <ul className="flex flex-col divide-y divide-line rounded-md border border-line">
                {r.actions.map((a0) => {
                  const a = { ...a0, ...(full[a0.id] || {}) } as ActionFull;
                  return (
                    <li key={a.id} className="flex flex-col gap-2 px-3 py-2.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <StatusBadge status={a.status} size="sm" />
                        <span className="text-[12px] text-ink-3">{kindLabel(a.kind)}</span>
                        <Link href={`/actions/?id=${encodeURIComponent(a.id)}`} className="mono ml-auto text-[11.5px] text-ink-3 hover:text-kb-ink hover:underline">{a.id}</Link>
                      </div>
                      <div className="text-[13px] font-medium text-ink">{moneyText(a.title)}</div>
                      <ActionControls action={a} onChange={(n) => { setFull((f) => ({ ...f, [n.id]: n })); onActionChange(); }} />
                      {a.status === "executed" && <ActionResult result={a.result} />}
                      {a.status === "rejected" && <p className="text-[12.5px] text-ink-3">Rejected; nothing was executed.</p>}
                    </li>
                  );
                })}
              </ul>
            ) : <p className="text-[13px] text-ink-3">No follow-up action was needed.</p>}
          </Step>
        </ol>
      </Card>

      <details className="glass group">
        <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 px-4 text-[13px] font-medium text-ink">
          <ChevronRight size={14} strokeWidth={1.75} className="text-ink-3 transition-transform group-open:rotate-90" aria-hidden />
          Graph query
          <span className="text-[12px] font-normal text-ink-3">the Cypher this chain was read with</span>
        </summary>
        <div className="flex flex-col gap-2 border-t border-line p-4">
          {doc.cypher.map((q, i) => <CodeCell key={i} code={q} />)}
        </div>
      </details>
    </div>
  );
}

// ======================================================================= empty state: deterministic demo

type DemoStep = { id: string; label: string; state: "idle" | "running" | "done" | "error"; note?: string };
const DEMO: DemoStep[] = [
  { id: "qualify", label: "Qualify T-NHX-2026-014 (rules only, no LLM)", state: "idle" },
  { id: "DEMO-0", label: "Send DEMO-0: revised completion certificate", state: "idle" },
  { id: "DEMO-1", label: "Send DEMO-1: corrigendum to a contract value", state: "idle" },
];

function DemoRunner({ onDone }: { onDone: () => void }) {
  const [steps, setSteps] = useState<DemoStep[]>(DEMO);
  const running = steps.some((s) => s.state === "running");
  const set = (id: string, p: Partial<DemoStep>) => setSteps((xs) => xs.map((s) => (s.id === id ? { ...s, ...p } : s)));

  async function runStep(id: string): Promise<boolean> {
    set(id, { state: "running", note: undefined });
    let err: string | null = null, note = "";
    if (id === "qualify") {
      await stream("/api/qualify", { tender_id: "T-NHX-2026-014", use_llm: false }, (ev) => {
        if (ev.type === "done") note = `recommendation ${ev.overall}`;
        if (ev.type === "error") err = ev.message;
      });
    } else {
      await stream(`/api/events/${id}/fire`, {}, (ev) => {
        if (ev.type === "actions") note = ev.summary;
        if (ev.type === "decision" && !note) note = "settled by policy";
        if (ev.type === "error") err = ev.message;
      });
    }
    set(id, err ? { state: "error", note: err } : { state: "done", note: note || "done" });
    return !err;
  }
  async function runAll() {
    for (const s of steps) {
      if (s.state === "done") continue;
      if (!(await runStep(s.id))) return;
    }
    onDone();
  }

  return (
    <Card>
      <EmptyState icon={<GitCompareArrows size={16} strokeWidth={1.75} />} title="No document has changed anything yet"
        text="BidBrain turns a new document into a claim, settles it by policy, follows the graph to every bid that relied on it, re-checks those bids and queues accountable actions." />
      <div className="mx-auto w-full max-w-lg pb-6">
        <ol className="flex flex-col divide-y divide-line rounded-md border border-line">
          {steps.map((s, i) => (
            <li key={s.id} className="flex items-center gap-3 px-3 py-2.5">
              <span className="num grid h-6 w-6 shrink-0 place-items-center rounded-md border border-line bg-surface-2 text-[12px] text-ink-2">{i + 1}</span>
              <div className="min-w-0 flex-1">
                <div className="text-[13px] text-ink">{s.label}</div>
                {s.note && <div className={clsx("text-[12px]", s.state === "error" ? "text-bad" : "text-ink-3")}>{s.note}</div>}
              </div>
              {s.state === "running" ? <Spinner label="Running" /> : <StatusBadge status={s.state === "done" ? "executed" : s.state === "error" ? "rejected" : "pending"}
                label={s.state === "done" ? "Done" : s.state === "error" ? "Failed" : "To do"} size="sm" />}
            </li>
          ))}
        </ol>
        <div className="mt-3 flex justify-center">
          <Button variant="primary" loading={running} onClick={runAll}><Play size={14} strokeWidth={1.75} aria-hidden /> Run the demo steps</Button>
        </div>
        <p className="mt-2 text-center text-[12px] text-ink-3">Deterministic: vetted clauses, fixed sample documents, the same verdicts every run.</p>
      </div>
    </Card>
  );
}

// ======================================================================= page

export default function ImpactPage() {
  const [docs, setDocs] = useState<Impact[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [doc, setDoc] = useState<ImpactDoc | null>(null);
  const [docErr, setDocErr] = useState<string | null>(null);

  const load = useCallback(async (pick?: "corrigendum") => {
    setFailed(false);
    try {
      const r = await api<{ documents: Impact[] }>("/api/impact");
      setDocs(r.documents);
      const want = new URLSearchParams(window.location.search).get("doc");
      setSel((cur) => {
        if (pick) {
          // after the demo: the corrigendum when it reached a bid, else the newest received document that did
          const own = r.documents.filter((d) => !fromRecord(d));
          const hit = own.find((d) => d.document.doc_type === pick && d.affected.length) || own.find((d) => d.affected.length) || own[0] || r.documents[0];
          return hit?.document.doc_id ?? null;
        }
        if (cur && r.documents.some((d) => d.document.doc_id === cur)) return cur;
        if (want && r.documents.some((d) => d.document.doc_id === want)) return want;
        // default: the newest document that still needs someone to act, else the newest
        return (r.documents.find((d) => d.actions.some((a) => a.status === "pending" || a.status === "approved")) || r.documents[0])?.document.doc_id ?? null;
      });
    } catch { setFailed(true); }
  }, []);
  // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch
  useEffect(() => { load(); }, [load]);

  const loadDoc = useCallback((id: string) => {
    setDocErr(null);
    api<ImpactDoc>(`/api/impact/${encodeURIComponent(id)}`).then(setDoc).catch((e) => { setDoc(null); setDocErr(String(e.message || e)); });
  }, []);
  useEffect(() => {
    if (!sel) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on selection
    setDoc(null);
    loadDoc(sel);
    try { const u = new URL(window.location.href); u.searchParams.set("doc", sel); window.history.replaceState(null, "", u); } catch {}
  }, [sel, loadDoc]);

  const pendingFor = (d: Impact) => d.actions.filter((a) => a.status === "pending" || a.status === "approved").length;

  return (
    <div>
      <PageHeader title="Change impact"
        subtitle="What each new document changed: the fact, the policy that settled it, the bids it reached and the actions it requires."
        meta={docs?.length ? <span><span className="num text-ink-2">{docs.length}</span> document{docs.length === 1 ? "" : "s"} received</span> : null} />

      {failed ? <Card><ErrorState onRetry={() => load()} /></Card>
        : docs === null ? <Card pad={false}><SkeletonRows rows={4} /></Card>
        : docs.length === 0 ? <DemoRunner onDone={() => load("corrigendum")} />
        : (
        <>
        {docs.every(fromRecord) && (
          <div className="mb-4"><DemoRunner onDone={() => load("corrigendum")} /></div>
        )}
        <div className="flex flex-col gap-4">
          <Card title="Event timeline" subtitle="Documents received in this session, newest first. Select one to trace what it changed." pad={false}>
            <ul aria-label="Documents" className="scroll-thin flex overflow-x-auto">
              {docs.map((d) => {
                const id = d.document.doc_id;
                const nCases = new Set(d.affected.map((a) => a.case_id)).size;
                const pend = pendingFor(d);
                return (
                  <li key={id} className="w-[320px] shrink-0 border-r border-line last:border-r-0">
                    <button type="button" aria-selected={sel === id} onClick={() => setSel(id)}
                      className={clsx("block h-full w-full px-4 py-2.5 text-left transition-colors duration-100 hover:bg-surface-2 focus-visible:outline-offset-[-2px]",
                        sel === id && "bg-kb-soft shadow-[inset_0_-2px_0_var(--kb)] hover:bg-kb-soft")}>
                      <div className="flex items-center gap-2">
                        <span className="min-w-0 truncate text-[12px] text-ink-3">{d.document.issued ? <span className="num">{d.document.issued} · </span> : null}{sentence(d.document.doc_type)}{fromRecord(d) ? " · from company record" : ""}</span>
                        <span className="ml-auto flex shrink-0 gap-1">
                          {nCases > 0 ? <StatusBadge status="ripple" label={`${nCases} bid${nCases > 1 ? "s" : ""}`} size="sm" />
                            : <Badge>Unchanged</Badge>}
                          {pend > 0 && <StatusBadge status="pending" label={`${pend} to act`} size="sm" />}
                        </span>
                      </div>
                      <div className="mt-0.5 line-clamp-2 text-[13px] font-medium leading-snug text-ink">{d.document.title || id}</div>
                      <div className="mono mt-0.5 truncate text-[11.5px] text-ink-3">{id}</div>
                    </button>
                  </li>
                );
              })}
            </ul>
          </Card>
          <div className="min-w-0">
            {docErr ? <Card><ErrorState text={docErr} onRetry={() => sel && loadDoc(sel)} /></Card>
              : doc ? <Chain key={doc.result.document.doc_id} doc={doc} onActionChange={() => { load(); if (sel) api<ImpactDoc>(`/api/impact/${encodeURIComponent(sel)}`).then(setDoc).catch(() => {}); }} />
              : <Card pad={false}><SkeletonRows rows={6} /></Card>}
          </div>
        </div>
        </>
      )}
    </div>
  );
}
