"use client";

// The Bid Control Room: what needs attention in this session's graph, why, and what to do next.
// Everything here is read from existing endpoints: /api/overview, /api/cases(+/{id}), /api/conflicts, /api/events,
// /api/tenders, /api/memory/org, and the read-only cypher tool (/api/tool/cypher_readonly) for the documents
// this session ingested (Document {live: true}, written by every fired event and upload).

import clsx from "clsx";
import { ArrowRight, FileStack, FileText, ScrollText } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ButtonLink, Card, EmptyState, ErrorState, Skeleton, SkeletonRows, StatusBadge, TONE_DOT, Table, Td, Th,
  statusInfo, useOpenDoc } from "@/components/ui";
import { api, fmtINR } from "@/lib/api";
import { Overview, fmt } from "./parts";

// ======================================================================= data

type CaseRow = { id: string; title: string; verdict?: string; bid_date?: string; status?: string; verified?: boolean; created_at?: number };
type Finding = { kind?: string; text?: string; severity?: string };
type Clause = { id: string; ref?: string; text?: string; kind?: string; verdict?: string; basis?: string; findings?: Finding[] };
type CaseDetail = { case: { id: string; title?: string; tender_ref?: string; verdict?: string; bid_date?: string }; clauses: Clause[] };
type CardClaim = { id: string; value?: string; source_doc?: string; page?: number; source_kind?: string; authority?: number; issuer?: string };
type ConflictCard = { id: string; label?: string; key?: string; predicate?: string; suggested?: string; rationale?: string;
  reopened_from?: string | null; subject?: string; claims: CardClaim[] };
type Conflicts = { cards: ConflictCard[]; decisions: { id: string; at?: number; source_doc?: string }[]; rules: unknown[] };
type LiveDoc = { doc_id: string; title?: string; doc_type?: string; issued?: string; issuer?: string; claims: number;
  subjects: string[]; cards: string[]; decisions: number; at?: number | null };
type OrgMemory = { active: number; cap: number; use_company_memory?: boolean; rules: unknown[] };
type Tender = { id: string; title: string; authority?: string; category?: string; estimated_cost?: number; bid_date?: string };
type SampleEvent = { event_id: string; type: string; title: string; narrative?: string };
type PendingAction = { id: string; kind: string; title: string; case_id?: string | null; created_at?: number };
type ImpactClaim = { claim: string; predicate: string; subject_key?: string; subject?: string; rationale?: string | null; decision?: string | null;
  chosen?: string | null; replaced?: string[] };
type ImpactDocR = { document: { doc_id: string; title?: string; doc_type?: string; issued?: string | null }; claims: ImpactClaim[];
  affected: { case_id: string; case: string; clause: string; kind: string; what: string; case_before?: string; case_after?: string; case_now?: string }[];
  actions: { id: string; kind: string; status: string; title: string; decision: string }[] };
const ACTION_KIND: Record<string, string> = { review_bid: "Re-approve bid", request_reference_letter: "Request letter",
  renew_credential: "Renew credential", publish_to_company_record: "Publish fact" };

const LIVE_DOCS = `MATCH (d:Document) WHERE d.live = true
OPTIONAL MATCH (c:Claim {source_doc: d.doc_id})
OPTIONAL MATCH (c)-[:ABOUT]->(s)
OPTIONAL MATCH (k:ConflictCard {status: 'open'})-[:ABOUT_CLAIM]->(c)
OPTIONAL MATCH (dec:Decision)-[:SELECTS]->(c)
RETURN d.doc_id AS doc_id, d.title AS title, d.doc_type AS doc_type, d.issued AS issued, d.issuer AS issuer,
       count(DISTINCT c) AS claims, collect(DISTINCT coalesce(s.title, s.name, s.key))[0..3] AS subjects,
       collect(DISTINCT k.id) AS cards, count(DISTINCT dec) AS decisions, max(dec.at) AS at`;

type State = {
  ov: Overview | null; cases: CaseRow[] | null; details: Record<string, CaseDetail>; conflicts: Conflicts | null;
  docs: LiveDoc[] | null; org: OrgMemory | null; tenders: Tender[] | null; events: SampleEvent[] | null;
  actions: PendingAction[] | null; impact: ImpactDocR[] | null; failed: Set<string>; at: number | null;
};
const EMPTY: State = { ov: null, cases: null, details: {}, conflicts: null, docs: null, org: null, tenders: null, events: null, actions: null, impact: null,
  failed: new Set(), at: null };

function useControlRoom(ov: Overview | null, ovFailed: boolean) {
  const [s, setS] = useState<State>(EMPTY);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setLoading(true);
    const failed = new Set<string>();
    const grab = <T,>(name: string, p: Promise<T>): Promise<T | null> => p.catch(() => { failed.add(name); return null; });
    const [cases, conflicts, docs, org, tenders, events, actions, impact] = await Promise.all([
      grab("cases", api<CaseRow[]>("/api/cases")),
      grab("conflicts", api<Conflicts>("/api/conflicts")),
      grab("docs", api<{ result: LiveDoc[] }>("/api/tool/cypher_readonly", { method: "POST", body: JSON.stringify({ query: LIVE_DOCS }) })
        .then((r) => r.result || [])),
      grab("org", api<OrgMemory>("/api/memory/org")),
      grab("tenders", api<Tender[]>("/api/tenders")),
      grab("events", api<SampleEvent[]>("/api/events")),
      grab("actions", api<{ actions: PendingAction[] }>("/api/actions?status=pending").then((r) => r.actions || [])),
      grab("impact", api<{ documents: ImpactDocR[] }>("/api/impact").then((r) => r.documents || [])),
    ]);
    const details: Record<string, CaseDetail> = {};
    await Promise.all((cases || []).slice(0, 24).map((c) =>
      api<CaseDetail>(`/api/cases/${encodeURIComponent(c.id)}`).then((d) => { details[c.id] = d; }).catch(() => {})));
    setS({ ov: null, cases, details, conflicts, docs, org, tenders, events, actions, impact, failed, at: Date.now() });
    setLoading(false);
  }, []);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch
    load();
    const onVis = () => document.visibilityState === "visible" && load();
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [load]);
  return { ...s, ov, ovFailed, loading, reload: load };
}

// ======================================================================= helpers

const words = (s?: string) => (s || "").replace(/_/g, " ");
const sentence = (s?: string) => { const w = words(s); return w.charAt(0).toUpperCase() + w.slice(1); };
const PREDICATE: Record<string, string> = { value: "Contract value", completed: "Completion date", role: "Role",
  revenue_operations: "Revenue from operations" };
const predicate = (p?: string) => PREDICATE[p || ""] || sentence(p);
const claimValue = (v?: string) => (v && /^\d{6,}(\.\d+)?$/.test(v) ? fmtINR(Number(v)) : v || "—");
const docSuffix = (id: string) => Number(id.match(/-(\d+)$/)?.[1] || 0);

function ago(ms?: number | null) {
  if (!ms) return null;
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 0 || s > 86400 * 30) return new Date(ms).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

/** "verdict changed PASS -> FAIL because X" / "verdict PASS unchanged, but the evidence it rests on changed because X" */
function parseRipple(text = "") {
  const m = text.match(/verdict changed (\S+) -> (\S+) because (.+)$/);
  if (m) return { from: m[1], to: m[2], cause: m[3], flip: true };
  const e = text.match(/verdict (\S+) unchanged.*because (.+)$/);
  if (e) return { from: e[1], to: e[1], cause: e[2], flip: false };
  return { from: undefined, to: undefined, cause: text, flip: false };
}

const caseHref = (id: string) => `/qualify/?case=${encodeURIComponent(id)}`;
const cardHref = (id: string) => `/live/?card=${encodeURIComponent(id)}`;

// ======================================================================= attention queue

type Item = { key: string; rank: number; status: string; badge?: string; kind: string; title: string; why: React.ReactNode;
  next: string; href: string };

/** "Publish <key> <predicate> = <raw> to the company record" -> readable, with the subject's name from the impact read */
function actionTitle(t: string, names: Map<string, string>) {
  const m = t.match(/^Publish (\S+) (\w+) = (.+) to the company record$/);
  if (!m) return t;
  return `Publish ${predicate(m[2]).toLowerCase()} ${claimValue(m[3])} for ${names.get(m[1]) || m[1]} to the company record`;
}

function buildQueue(cards: ConflictCard[], cases: CaseRow[], details: Record<string, CaseDetail>, actions: PendingAction[] = [],
  impact: ImpactDocR[] = []): Item[] {
  const items: Item[] = [];
  const names = new Map(impact.flatMap((d) => d.claims.filter((c) => c.subject_key && c.subject).map((c) => [c.subject_key!, c.subject!] as [string, string])));
  for (const a of actions) {
    const src = impact.find((d) => d.actions.some((x) => x.id === a.id));
    const dec = src?.actions.find((x) => x.id === a.id)?.decision;
    const cl = src?.claims.find((c) => c.decision === dec);
    const aff = src?.affected.find((x) => x.case_id === a.case_id);
    items.push({ key: `action:${a.id}`, rank: 0.5, status: "pending", badge: "Approve", kind: "Action", title: actionTitle(a.title, names),
      why: src ? (
        <>Because <span className="text-ink">{src.document.title || src.document.doc_id}</span>
          {cl?.replaced?.length ? <> changed {predicate(cl.predicate).toLowerCase()} {claimValue(cl.replaced[0].replace(/,/g, ""))} → <span className="text-ink">{claimValue((cl.chosen || "").replace(/,/g, ""))}</span></> : null}
          {aff ? <>; clause {aff.clause} {words(aff.kind)} now {statusInfo(aff.what.match(/-> (\S+)/)?.[1]).label.toLowerCase()}</> : null}
          {" · "}<span className="mono text-[11.5px]">{a.id}</span></>
      ) : <>{ACTION_KIND[a.kind] || a.kind.replace(/_/g, " ")} · waiting for approval{a.created_at ? ` since ${ago(a.created_at)}` : ""} · <span className="mono text-[11.5px]">{a.id}</span></>,
      next: "Review and approve", href: `/actions/?id=${encodeURIComponent(a.id)}` });
  }
  for (const k of cards) {
    const [a, b] = k.claims;
    items.push({
      key: `card:${k.id}`, rank: 0, status: "NEEDS REVIEW", badge: "Needs decision", kind: "Conflict",
      title: `${predicate(k.predicate)} of ${k.subject || k.key}`,
      why: a && b ? (
        <>
          <span className="text-ink">{claimValue(a.value)}</span> ({words(a.source_kind)}, <span className="mono text-[11.5px]">{a.source_doc}</span>)
          {" vs "}
          <span className="text-ink">{claimValue(b.value)}</span> ({words(b.source_kind)}, <span className="mono text-[11.5px]">{b.source_doc}</span>)
          {k.reopened_from ? " · reopened" : ""}
        </>
      ) : k.rationale,
      next: "Choose the winner", href: cardHref(k.id),
    });
  }
  for (const c of cases) {
    const d = details[c.id];
    const clauses = d?.clauses || [];
    const fail = clauses.filter((x) => x.verdict === "FAIL");
    const risk = clauses.filter((x) => x.verdict === "AT_RISK");
    const review = clauses.filter((x) => x.verdict === "NEEDS REVIEW" || x.verdict === "NEEDS_REVIEW");
    const v = c.verdict || d?.case.verdict;
    const list = (xs: Clause[]) => xs.slice(0, 2).map((x) => `${x.ref} ${words(x.kind)}: ${x.basis || x.verdict}`).join(" · ")
      + (xs.length > 2 ? ` · ${xs.length - 2} more` : "");
    if (v === "NO-BID" || fail.length) {
      items.push({ key: `case:${c.id}`, rank: 1, status: v === "NO-BID" ? "NO-BID" : "FAIL", kind: "Case", title: c.title,
        why: fail.length ? `${fail.length} failing clause${fail.length > 1 ? "s" : ""}: ${list(fail)}` : "Recommendation is no-bid.",
        next: "Fix or drop the bid", href: caseHref(c.id) });
    } else if (v === "NEEDS REVIEW" || review.length) {
      items.push({ key: `case:${c.id}`, rank: 2, status: "NEEDS REVIEW", kind: "Case", title: c.title,
        why: review.length ? `Verifier could not confirm ${list(review)}` : "The verifier could not confirm every verdict.",
        next: "Review the flagged clauses", href: caseHref(c.id) });
    } else if (v === "BID WITH FIXES" || risk.length) {
      items.push({ key: `case:${c.id}`, rank: 3, status: v === "BID WITH FIXES" ? "BID WITH FIXES" : "AT_RISK", kind: "Case", title: c.title,
        why: risk.length ? `${risk.length} clause${risk.length > 1 ? "s" : ""} at risk: ${list(risk)}` : "Bid is possible once the listed fixes are made.",
        next: "Make the fixes", href: caseHref(c.id) });
    }
    for (const cl of clauses) {
      for (const f of cl.findings || []) {
        if (f.kind !== "ripple") continue;
        const r = parseRipple(f.text);
        items.push({
          key: `ripple:${cl.id}:${f.text}`, rank: r.flip ? 4 : 6, status: "ripple", kind: "Ripple",
          badge: r.flip ? "Verdict changed" : "Evidence changed",
          title: `Clause ${cl.ref} ${words(cl.kind)} · ${c.title}`,
          why: r.flip
            ? <>{statusInfo(r.from).label} → <span className="font-medium text-ink">{statusInfo(r.to).label}</span> after {r.cause}</>
            : <>Still {statusInfo(r.from).label.toLowerCase()}, but now rests on different evidence after {r.cause}</>,
          next: r.flip ? "Re-check the case" : "Confirm the new evidence", href: caseHref(c.id),
        });
      }
    }
  }
  return items.sort((x, y) => x.rank - y.rank);
}

function QueueRow({ it }: { it: Item }) {
  return (
    <li className="border-b border-line last:border-b-0">
      <Link href={it.href}
        className="group grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-3 gap-y-1 px-4 py-2.5 transition-colors duration-100 ease-out hover:bg-surface-2 focus-visible:outline-offset-[-2px] md:grid-cols-[124px_minmax(0,1fr)_170px]">
        <div className="col-span-2 flex items-center gap-2 md:col-span-1 md:pt-px">
          <StatusBadge status={it.status} label={it.badge} size="sm" />
          <span className="text-[11.5px] text-ink-3 md:hidden">{it.kind}</span>
        </div>
        <div className="min-w-0">
          <div className="flex items-baseline gap-2">
            <span className="hidden w-[52px] shrink-0 text-[11.5px] text-ink-3 md:inline">{it.kind}</span>
            <span className="truncate text-[13px] font-medium text-ink">{it.title}</span>
          </div>
          <div className="mt-0.5 line-clamp-2 text-[12.5px] leading-snug text-ink-2 md:pl-[60px]">{it.why}</div>
        </div>
        <span className="flex items-center justify-end gap-1 self-center text-[12.5px] font-medium text-kb-ink md:self-start md:justify-start md:pt-px">
          <span className="hidden sm:inline">{it.next}</span>
          <ArrowRight size={14} strokeWidth={1.75} aria-hidden className="shrink-0 transition-transform duration-100 group-hover:translate-x-0.5" />
        </span>
      </Link>
    </li>
  );
}

// ======================================================================= KPI strip

function Kpi({ href, label, value, sub, loading }: { href: string; label: string; value: React.ReactNode; sub?: React.ReactNode; loading?: boolean }) {
  return (
    <Link href={href} className="group block min-w-0 px-4 py-3 transition-colors duration-100 ease-out hover:bg-surface-2 focus-visible:outline-offset-[-2px]">
      <div className="flex items-center justify-between gap-2 text-[12px] text-ink-3">
        <span className="truncate">{label}</span>
        <ArrowRight size={13} strokeWidth={1.75} aria-hidden className="shrink-0 opacity-0 transition-opacity duration-100 group-hover:opacity-100" />
      </div>
      <div className="num mt-1 text-[22px] font-semibold leading-7 tracking-[-0.01em] text-ink">
        {loading ? <Skeleton w={40} h={22} className="my-[3px]" /> : value}
      </div>
      <div className="mt-0.5 min-h-[18px] text-[12px] leading-snug text-ink-3 md:truncate">{loading ? <Skeleton w="70%" h={10} className="mt-1" /> : sub}</div>
    </Link>
  );
}

function Breakdown({ parts }: { parts: [string, number][] }) {
  const shown = parts.filter(([, n]) => n > 0);
  if (!shown.length) return <>none open</>;
  return (
    <span className="inline-flex flex-wrap gap-x-2.5">
      {shown.map(([s, n]) => (
        <span key={s} className="inline-flex items-center gap-1">
          <span aria-hidden className={clsx("h-1.5 w-1.5 rounded-full", TONE_DOT[statusInfo(s).tone])} />
          <span className="num text-ink-2">{n}</span> {statusInfo(s).label.toLowerCase()}
        </span>
      ))}
    </span>
  );
}

// ======================================================================= page body

export function ControlRoom({ ov, ovFailed, onSchema, onRetryOverview }:
  { ov: Overview | null; ovFailed: boolean; onSchema: () => void; onRetryOverview: () => void }) {
  const d = useControlRoom(ov, ovFailed);
  const open = useOpenDoc();
  const cards = useMemo(() => d.conflicts?.cards || [], [d.conflicts]);
  const cases = useMemo(() => (d.cases || []).filter((c) => (c.status || "open") === "open"), [d.cases]);
  const docs = useMemo(() => [...(d.docs || [])].sort((a, b) => (b.at || 0) - (a.at || 0) || docSuffix(b.doc_id) - docSuffix(a.doc_id)), [d.docs]);
  const queue = useMemo(() => buildQueue(cards, cases, d.details, d.actions || [], d.impact || []), [cards, cases, d.details, d.actions, d.impact]);
  const impactOf = useMemo(() => new Map((d.impact || []).map((x) => [x.document.doc_id, x])), [d.impact]);
  const first = d.loading && d.at === null;
  const fresh = !first && !cases.length && !docs.length && !cards.length && !(d.actions || []).length;

  const byVerdict = (v: string) => cases.filter((c) => c.verdict === v).length;
  const docsSettled = docs.filter((x) => x.decisions > 0 && !x.cards.length).length;
  const docsOpen = docs.filter((x) => x.cards.length).length;

  // tender -> latest case for it
  const caseFor = useMemo(() => {
    const m = new Map<string, CaseRow>();
    for (const c of d.cases || []) {
      const ref = d.details[c.id]?.case.tender_ref;
      if (ref && !m.has(ref)) m.set(ref, c);
    }
    return m;
  }, [d.cases, d.details]);

  const count = (l: string) => ov?.labels?.find((x) => x.label === l)?.n;
  const totalNodes = ov?.labels?.reduce((a, l) => a + l.n, 0);
  const totalEdges = ov?.relationships?.reduce((a, l) => a + l.n, 0);

  if (ovFailed && !ov && d.failed.has("cases") && d.failed.has("conflicts")) {
    return (
      <Card>
        <ErrorState title="The API is not responding" text="Check that the backend and FalkorDB are running, then retry."
          onRetry={() => { onRetryOverview(); d.reload(); }} />
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {/* ---- KPIs */}
      <section aria-label="Key figures" className="glass grid grid-cols-2 divide-line overflow-hidden md:grid-cols-4 [&>*]:border-line max-md:[&>*:nth-child(odd)]:border-r max-md:[&>*:nth-child(-n+2)]:border-b md:[&>*:not(:last-child)]:border-r">
        <Kpi href="/qualify/" label="Open cases" value={fmt(cases.length)} loading={first}
          sub={<Breakdown parts={[["BID", byVerdict("BID")], ["BID WITH FIXES", byVerdict("BID WITH FIXES")], ["NEEDS REVIEW", byVerdict("NEEDS REVIEW")], ["NO-BID", byVerdict("NO-BID")]]} />} />
        <Kpi href="/live/" label="Open conflicts" value={fmt(d.conflicts ? cards.length : ov?.open_cards)} loading={first}
          sub={cards.length ? "awaiting your decision" : "nothing to decide"} />
        <Kpi href="/impact/" label="Documents received" value={fmt(d.docs?.length ?? (d.failed.has("docs") ? undefined : 0))} loading={first}
          sub={docs.length ? `${docsSettled} settled by policy · ${docsOpen} to decide` : "none ingested yet"} />
        <Kpi href="/memory/" label="Active company rules" value={fmt(d.org?.active)} loading={first}
          sub={d.org ? `of ${d.org.cap} · ${d.org.use_company_memory ? "applied in this session" : "not applied here"}` : "company memory unavailable"} />
      </section>

      {fresh ? <GetStarted events={d.events || []} tenders={d.tenders || []} /> : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
          {/* ---- attention queue */}
          <Card title="Needs attention" pad={false}
            subtitle="Actions to approve, open conflicts, failing cases and verdicts changed by new documents"
            action={!first && <span className="num text-[12.5px] text-ink-3">{queue.length} item{queue.length === 1 ? "" : "s"}</span>}>
            {first ? <SkeletonRows rows={4} /> : queue.length ? (
              <ul aria-label="Needs attention">{queue.slice(0, 30).map((it) => <QueueRow key={it.key} it={it} />)}</ul>
            ) : (
              <EmptyState compact title="Nothing needs attention"
                text="Every open case passes and no conflict is waiting. New documents that change a fact will show up here." />
            )}
          </Card>

          {/* ---- tenders and what happens next */}
          <Card title="Tenders" pad={false} subtitle="Latest case per sample tender"
            action={<Link href="/qualify/" className="rounded-sm text-[12.5px] font-medium text-kb-ink hover:underline">All tenders</Link>}>
            {!d.tenders ? (d.failed.has("tenders") ? <ErrorState compact onRetry={d.reload} /> : <SkeletonRows rows={4} />) : (
              <ul>
                {d.tenders.map((t) => {
                  const c = caseFor.get(t.id);
                  return (
                    <li key={t.id} className="border-b border-line last:border-b-0">
                      <Link href={c ? caseHref(c.id) : `/qualify/?tender=${encodeURIComponent(t.id)}`}
                        className="flex items-start gap-3 px-4 py-2.5 transition-colors duration-100 hover:bg-surface-2 focus-visible:outline-offset-[-2px]">
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-[13px] font-medium text-ink">{t.title}</div>
                          <div className="mt-0.5 truncate text-[12px] text-ink-3">
                            <span className="num">{fmtINR(t.estimated_cost)}</span> · due <span className="num">{t.bid_date}</span>
                            {t.authority ? ` · ${t.authority}` : ""}
                          </div>
                        </div>
                        <div className="shrink-0 pt-px">
                          {c ? <StatusBadge status={c.verdict} size="sm" /> : <span className="text-[12px] text-ink-3">Not qualified</span>}
                        </div>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            )}
          </Card>
        </div>
      )}

      {/* ---- recent document changes */}
      {!fresh && (
        <Card title="Recent document changes" pad={false} subtitle="What each document sent into this session changed"
          action={<Link href="/live/" className="rounded-md text-[12.5px] font-medium text-kb-ink hover:underline">Live documents</Link>}>
          {first ? <SkeletonRows rows={3} /> : d.failed.has("docs") ? (
            <ErrorState compact title="Couldn't list this session's documents" onRetry={d.reload} />
          ) : !docs.length ? (
            <EmptyState compact icon={<FileStack size={16} strokeWidth={1.75} />} title="No documents yet"
              text="Send a corrigendum or a revised certificate from Live documents to see what it changes."
              action={<ButtonLink href="/live/" size="sm">Send a document</ButtonLink>} />
          ) : (
            <Table label="Documents ingested this session">
              <thead>
                <tr><Th>Document</Th><Th className="max-md:hidden">Changed fact</Th><Th className="max-[1399px]:hidden">Outcome</Th><Th>Effect on bids</Th><Th align="right" className="max-sm:hidden">Issued</Th></tr>
              </thead>
              <tbody>
                {docs.slice(0, 12).map((x) => (
                  <tr key={x.doc_id}>
                    <Td className="max-w-[210px] pl-4! sm:max-w-[420px]">
                      <button type="button" onClick={() => open({ doc_id: x.doc_id, page: 1, what: x.title })}
                        className="group flex min-w-0 max-w-full items-start gap-2 rounded-sm text-left">
                        <FileText size={14} strokeWidth={1.75} aria-hidden className="mt-[3px] shrink-0 text-ink-3" />
                        <span className="min-w-0">
                          <span className="block truncate text-[13px] text-ink group-hover:text-kb-ink group-hover:underline">{x.title || x.doc_id}</span>
                          <span className="block truncate text-[11.5px] text-ink-3"><span className="mono">{x.doc_id}</span> · {words(x.doc_type)}</span>
                        </span>
                      </button>
                    </Td>
                    <Td className="max-w-[300px] text-[12.5px] max-md:hidden" muted>
                      {(() => {
                        const c = impactOf.get(x.doc_id)?.claims.find((y) => y.replaced?.length);
                        return c ? (
                          <span className="block truncate" title={`${c.subject}: ${predicate(c.predicate)}`}>
                            <span className="text-ink-2">{predicate(c.predicate)}</span> <span className="num line-through">{claimValue(c.replaced![0].replace(/,/g, ""))}</span> → <span className="num font-medium text-ink">{claimValue((c.chosen || "").replace(/,/g, ""))}</span>
                            <span className="block truncate text-[11.5px] text-ink-3">{c.subject}</span>
                          </span>
                        ) : <span className="block truncate">{x.subjects.join(", ") || "—"}</span>;
                      })()}
                    </Td>
                    <Td className="max-[1399px]:hidden">
                      {x.cards.length ? (
                        <Link href={cardHref(x.cards[0])} className="rounded-sm"><StatusBadge status="open" label="Needs decision" size="sm" /></Link>
                      ) : x.decisions ? <StatusBadge status="resolved" label="Settled by policy" size="sm" />
                        : <StatusBadge status="pending" label="Recorded" size="sm" />}
                    </Td>
                    <Td className="text-[12.5px]">
                      {(() => {
                        const im = impactOf.get(x.doc_id);
                        const a = im?.affected.find((y) => y.case_before && y.case_after && y.case_before !== y.case_after) || im?.affected[0];
                        return (
                          <Link href={`/impact/?doc=${encodeURIComponent(x.doc_id)}`} className="group inline-flex items-center gap-1.5 rounded-sm" title="Trace in the Impact Room">
                            {a?.case_before && a.case_after ? <><StatusBadge status={a.case_before} size="sm" /><ArrowRight size={12} className="text-ink-3" aria-hidden /><StatusBadge status={a.case_after} size="sm" /></>
                              : <span className="text-ink-3 group-hover:text-kb-ink">{im?.affected.length ? `${im.affected.length} clause(s) re-checked` : "no bid affected"}</span>}
                            <span className="text-kb-ink group-hover:underline">Trace</span>
                          </Link>
                        );
                      })()}
                    </Td>
                    <Td numeric muted className="pr-4! text-[12px] max-sm:hidden" title={x.at ? `Settled ${ago(x.at)}` : undefined}>{x.issued || "—"}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
      )}

      {/* ---- graph at a glance */}
      <section aria-label="Graph at a glance" className="glass flex flex-wrap items-center gap-x-5 gap-y-2 px-4 py-2.5 text-[12.5px]">
        <span className="font-medium text-ink">Graph at a glance</span>
        {([["Nodes", totalNodes], ["Edges", totalEdges], ["Documents", count("Document")], ["Works", count("Work")],
           ["Clients", count("Client")], ["People", count("Person")], ["Claims", count("Claim")], ["Decisions", ov?.decisions]] as [string, unknown][])
          .map(([l, v]) => (
            <span key={l} className="inline-flex items-baseline gap-1.5">
              <span className="text-ink-3">{l}</span>
              {ov ? <span className="num font-medium text-ink">{fmt(v)}</span> : ovFailed ? <span className="text-ink-3">—</span> : <Skeleton w={28} h={10} className="inline-block" />}
            </span>
          ))}
        <span className="ml-auto flex items-center gap-3">
          {ov?.version != null && <span className="text-ink-3">Version <span className="num text-ink-2">{ov.version}</span></span>}
          <button type="button" onClick={onSchema} className="rounded-sm font-medium text-kb-ink hover:underline">Schema</button>
          <Link href="/explore/" className="rounded-sm font-medium text-kb-ink hover:underline">Explore</Link>
        </span>
      </section>
      {d.at && <p className="text-[11.5px] text-ink-3" aria-live="polite">Updated {new Date(d.at).toLocaleTimeString("en-GB")}{d.loading ? " · refreshing" : ""}</p>}
    </div>
  );
}

// ======================================================================= first run

function GetStarted({ events, tenders }: { events: SampleEvent[]; tenders: Tender[] }) {
  return (
    <Card title="Start a session" subtitle="Your graph is a fresh copy of the company graph, so nothing needs attention yet">
      <ol className="grid gap-4 md:grid-cols-2">
        <li className="flex gap-3">
          <span className="num grid h-6 w-6 shrink-0 place-items-center rounded-md border border-line bg-surface-2 text-[12px] font-medium text-ink-2">1</span>
          <div className="min-w-0 flex-1">
            <h3 className="text-[13.5px] font-semibold text-ink">Qualify a sample tender</h3>
            <p className="mt-0.5 text-[13px] leading-relaxed text-ink-2">
              The agent team checks every eligibility clause against the graph and cites the pages behind each verdict. This opens a case.
            </p>
            {tenders.length > 0 && (
              <ul className="mt-2 space-y-0.5 text-[12.5px] text-ink-3">
                {tenders.slice(0, 4).map((t) => <li key={t.id} className="truncate"><span className="text-ink-2">{t.title}</span> · <span className="num">{fmtINR(t.estimated_cost)}</span></li>)}
              </ul>
            )}
            <ButtonLink href="/qualify/" variant="primary" className="mt-3"><ScrollText size={14} strokeWidth={1.75} aria-hidden /> Qualify a tender</ButtonLink>
          </div>
        </li>
        <li className="flex gap-3">
          <span className="num grid h-6 w-6 shrink-0 place-items-center rounded-md border border-line bg-surface-2 text-[12px] font-medium text-ink-2">2</span>
          <div className="min-w-0 flex-1">
            <h3 className="text-[13.5px] font-semibold text-ink">Send a document that changes a fact</h3>
            <p className="mt-0.5 text-[13px] leading-relaxed text-ink-2">
              A corrigendum or revised certificate is settled by policy or queued for you, and every open case relying on it is re-checked.
            </p>
            {events.length > 0 && (
              <ul className="mt-2 space-y-0.5 text-[12.5px] text-ink-3">
                {events.filter((e) => e.type === "demo").slice(0, 4).map((e) => <li key={e.event_id} className="truncate text-ink-2">{e.title}</li>)}
              </ul>
            )}
            <ButtonLink href="/live/" className="mt-3"><FileStack size={14} strokeWidth={1.75} aria-hidden /> Open live documents</ButtonLink>
          </div>
        </li>
      </ol>
      <p className="mt-4 border-t border-line pt-3 text-[12.5px] text-ink-3">
        Sample tenders use vetted clauses and sample documents are fixed, so the same steps give the same verdicts. Qualify first: a document only ripples into cases that exist.
      </p>
    </Card>
  );
}
