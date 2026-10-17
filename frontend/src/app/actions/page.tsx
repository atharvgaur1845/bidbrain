"use client";

// Actions: the operational queue. Every follow-up a changed fact requires, behind an approval gate, with its audit trail.

import { FileText, ListChecks } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { PageHeader } from "@/components/Shell";
import { Card, EmptyState, ErrorState, KeyValue, SkeletonRows, StatusBadge, Table, Tabs, Td, Th, Timeline, TimelineItem, Tr,
  useOpenDoc } from "@/components/ui";
import { ActionControls, ActionFull, ActionResult, ActionRow, ago, kindLabel, stamp } from "@/lib/actions";
import { api } from "@/lib/api";

const FILTERS = ["pending", "approved", "executed", "rejected", "all"] as const;
type Filter = (typeof FILTERS)[number];
const LABEL: Record<Filter, string> = { pending: "Pending", approved: "Approved", executed: "Executed", rejected: "Rejected", all: "All" };

function triggerHref(t?: { label: string; id: string } | null) {
  if (!t) return null;
  if (t.label === "Decision") return `/decisions/?id=${encodeURIComponent(t.id)}`;
  if (t.label === "Case") return `/qualify/?case=${encodeURIComponent(t.id)}`;
  return null;
}

function Detail({ id, onChange }: { id: string; onChange: () => void }) {
  const open = useOpenDoc();
  const [a, setA] = useState<ActionFull | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api<ActionFull>(`/api/actions/${encodeURIComponent(id)}`).then((x) => live && setA(x)).catch((e) => live && setErr(String(e.message || e)));
    return () => { live = false; };
  }, [id]);
  if (err) return <Card><ErrorState compact text={err} /></Card>;
  if (!a) return <Card pad={false}><SkeletonRows rows={5} /></Card>;
  const th = triggerHref(a.trigger);
  return (
    <Card title={a.title} subtitle={<span className="mono">{a.id}</span>} action={<StatusBadge status={a.status} />}>
      <div className="flex flex-col gap-4">
        {a.detail && <p className="text-[13px] leading-relaxed text-ink-2">{a.detail}</p>}
        <ActionControls action={a} withNote onChange={(n) => { setA(n); onChange(); }} />
        {a.status === "executed" && <ActionResult result={a.result} />}
        <KeyValue items={[
          ["Kind", kindLabel(a.kind)],
          ["Owner", a.owner || "—"],
          ["Assignee", a.assignee || "unassigned"],
          ["Triggered by", a.trigger ? (th ? <Link href={th} className="mono text-[12px] text-kb-ink hover:underline">{a.trigger.label} {a.trigger.id}</Link>
            : <span className="mono text-[12px]">{a.trigger.label} {a.trigger.id}</span>) : "—"],
          ["Case", a.case_id ? <Link href={`/qualify/?case=${encodeURIComponent(a.case_id)}`} className="mono text-[12px] text-kb-ink hover:underline">{a.case_id}</Link> : "—"],
          ["Created", stamp(a.created_at)],
        ]} />
        <div>
          <h3 className="mb-1.5 text-[12px] font-medium text-ink-3">Cited documents</h3>
          {a.cites?.length ? (
            <ul className="flex flex-wrap gap-1.5">
              {a.cites.map((d) => (
                <li key={d}>
                  <button type="button" onClick={() => open({ doc_id: d })}
                    className="mono inline-flex h-6 items-center gap-1.5 rounded-[4px] border border-line bg-surface-2 px-2 text-[11.5px] text-kb-ink hover:border-line-strong">
                    <FileText size={12} strokeWidth={1.75} aria-hidden />{d}
                  </button>
                </li>
              ))}
              <li><Link href={`/impact/?doc=${encodeURIComponent(a.cites[0])}`} className="inline-flex h-6 items-center text-[12px] text-kb-ink hover:underline">See the change impact</Link></li>
            </ul>
          ) : <p className="text-[12.5px] text-ink-3">None</p>}
        </div>
        <div>
          <h3 className="mb-2 text-[12px] font-medium text-ink-3">Audit trail</h3>
          <Timeline>
            {(a.events || []).map((e, i, xs) => (
              <TimelineItem key={i} last={i === xs.length - 1} status={e.to} time={stamp(e.at)}
                title={<>{e.from ? <>{e.from} → </> : null}<span className="font-semibold">{e.to}</span> <span className="font-normal text-ink-3">by <span className="mono text-[11.5px]">{e.by}</span></span></>}>
                {e.note}
              </TimelineItem>
            ))}
          </Timeline>
        </div>
      </div>
    </Card>
  );
}

export default function ActionsPage() {
  const [filter, setFilter] = useState<Filter>("pending");
  const [all, setAll] = useState<ActionRow[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [sel, setSel] = useState<string | null>(null);

  const load = useCallback(() => {
    setFailed(false);
    api<{ actions: ActionRow[] }>("/api/actions").then((r) => setAll(r.actions)).catch(() => setFailed(true));
  }, []);
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("id");
    // eslint-disable-next-line react-hooks/set-state-in-effect -- read the deep link once
    if (id) { setSel(id); setFilter("all"); }
    load();
  }, [load]);
  useEffect(() => {
    if (!sel) return;
    try { const u = new URL(window.location.href); u.searchParams.set("id", sel); window.history.replaceState(null, "", u); } catch {}
  }, [sel]);

  const count = (f: Filter) => (all || []).filter((a) => f === "all" || a.status === f).length;
  const rows = (all || []).filter((a) => filter === "all" || a.status === filter);

  return (
    <div>
      <PageHeader title="Actions" subtitle="Follow-ups a changed fact requires. Each is approved by a person, executed by deterministic code and kept with its audit trail."
        meta={all ? <span><span className="num text-ink-2">{count("pending")}</span> pending · <span className="num text-ink-2">{count("approved")}</span> approved, not executed</span> : null} />
      <Tabs className="mb-4" value={filter} onChange={(f) => setFilter(f as Filter)}
        tabs={FILTERS.map((f) => ({ id: f, label: LABEL[f], count: all ? count(f) : undefined }))} />
      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_440px]">
        <Card pad={false}>
          {failed ? <ErrorState onRetry={load} /> : all === null ? <SkeletonRows rows={5} /> : rows.length === 0 ? (
            <EmptyState icon={<ListChecks size={16} strokeWidth={1.75} />} title={filter === "all" ? "No actions yet" : `No ${LABEL[filter].toLowerCase()} actions`}
              text={filter === "all" || filter === "pending"
                ? <>Actions are raised when a document changes a fact a bid relies on. See <Link href="/impact/" className="text-kb-ink hover:underline">Change impact</Link>.</>
                : undefined} />
          ) : (
            <Table label="Actions" maxHeight="72vh">
              <thead><tr><Th width={150}>Kind</Th><Th>Title</Th><Th width={130} className="max-md:hidden">Owner</Th><Th width={100}>Status</Th><Th width={90} align="right" className="max-sm:hidden">Age</Th></tr></thead>
              <tbody>
                {rows.map((a) => (
                  <Tr key={a.id} selected={sel === a.id} onClick={() => setSel(a.id)}>
                    <Td muted className="text-[12.5px]">{kindLabel(a.kind)}</Td>
                    <Td>
                      <button type="button" className="block min-w-0 text-left" onClick={(e) => { e.stopPropagation(); setSel(a.id); }}>
                        <span className="line-clamp-2 text-[13px] text-ink">{a.title}</span>
                        <span className="mono block truncate text-[11.5px] text-ink-3">{a.case_id || a.id}</span>
                      </button>
                    </Td>
                    <Td muted className="text-[12.5px] max-md:hidden">{a.owner || "—"}{a.assignee ? ` / ${a.assignee}` : ""}</Td>
                    <Td><StatusBadge status={a.status} size="sm" /></Td>
                    <Td numeric muted align="right" className="text-[12.5px] max-sm:hidden">{ago(a.created_at)}</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
        <div className="xl:sticky xl:top-16">
          {sel ? <Detail key={sel} id={sel} onChange={() => { load(); }} />
            : <Card><EmptyState compact title="No action selected" text="Pick a row to see its trigger, cited documents and audit trail." /></Card>}
        </div>
      </div>
    </div>
  );
}
