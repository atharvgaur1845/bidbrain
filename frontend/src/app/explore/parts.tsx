"use client";

import clsx from "clsx";
import { ArrowRight, ChevronDown, ChevronRight, FileText } from "lucide-react";
import { Fragment, useId } from "react";
import { Avatar, Badge, Button, Card, CodeCell, Empty, Spinner, Stat, Verdict, useOpenDoc } from "@/components/ui";
import { fmtINR } from "@/lib/api";

export const KINDS = ["client", "person", "work", "tender"] as const;
export type Kind = (typeof KINDS)[number];
export const LABEL_OF: Record<string, string> = { client: "Client", person: "Person", work: "Work", tender: "Tender" };

export const ALGOS = [
  { id: "key_people", label: "Bus factor", proc: "algo.betweenness", hint: "People that most client relationships run through" },
  { id: "isolated_entities", label: "Isolated entities", proc: "algo.WCC", hint: "People and clients outside the main component" },
  { id: "communities", label: "Teams by shared clients", proc: "algo.labelPropagation", hint: "People who served two or more of the same clients" },
  { id: "audit_bid", label: "Audit past bids", proc: "Cypher integrity check", hint: "Works cited before completion, people proposed before certification" },
];
const RESULT: Record<string, { label: string; proc: string }> = {
  ...Object.fromEntries(ALGOS.map((a) => [a.id, a])),
  explain_path: { label: "Connection paths", proc: "algo.SPpaths" },
};

type OpenFn = (label: string, key: string) => void;
const count = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

const ROW = "flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left transition-colors duration-150 ease-out hover:bg-well";

// ======================================================================= controls

export function Segmented<T extends string>({ value, options, onChange, label }:
  { value: T; options: { id: T; label: string }[]; onChange: (v: T) => void; label: string }) {
  return (
    <div role="radiogroup" aria-label={label} className="flex w-full rounded-lg bg-well p-0.5 shadow-[inset_0_0_0_1px_var(--line)]">
      {options.map((o) => {
        const on = o.id === value;
        return (
          <button key={o.id} type="button" role="radio" aria-checked={on} onClick={() => onChange(o.id)}
            className={clsx("h-7 flex-1 rounded-md px-2 text-[12.5px] transition-[background-color,box-shadow,color] duration-150 ease-out",
              on ? "bg-surface-solid font-medium text-ink shadow-[var(--shadow-border)]" : "font-medium text-ink-3 hover:text-ink-2")}>
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function Endpoint({ label, text, kind, onText, onKind }:
  { label: string; text: string; kind: string; onText: (v: string) => void; onKind: (v: string) => void }) {
  const id = useId();
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 block text-[12.5px] font-medium text-ink-2">{label}</label>
      <div className="grid grid-cols-[minmax(0,1fr)_7.5rem] gap-2">
        <input id={id} className="field" value={text} onChange={(e) => onText(e.target.value)} />
        <div className="relative">
          <select aria-label={`${label} kind`} className="field appearance-none pr-8!" value={kind} onChange={(e) => onKind(e.target.value)}>
            {KINDS.map((k) => <option key={k} value={k}>{LABEL_OF[k]}</option>)}
          </select>
          <ChevronDown size={16} strokeWidth={1.75} className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-ink-3" />
        </div>
      </div>
    </div>
  );
}

// ======================================================================= search results

export function Candidates({ cands, kind, text, focus, onOpen }:
  { cands: any[]; kind: Kind; text: string; focus: string | null; onOpen: OpenFn }) {
  if (!cands.length) return <p className="mt-4 text-[13px] text-ink-3">No {LABEL_OF[kind].toLowerCase()} matches “{text}”.</p>;
  const label = LABEL_OF[kind];
  return (
    <div className="mt-4">
      <div className="flex pb-1.5 text-[11.5px] font-semibold uppercase tracking-[0.04em] text-ink-3">
        <span>{label}</span><span className="ml-auto">Score</span>
      </div>
      <ul className="-mx-2 flex flex-col gap-0.5">
        {cands.map((c) => {
          const on = focus === `${label}:${c.key}`;
          return (
            <li key={c.key}>
              <button type="button" onClick={() => onOpen(label, c.key)} aria-current={on || undefined}
                className={clsx(ROW, on && "bg-kb-soft hover:bg-kb-soft")}>
                {kind === "person" && <Avatar name={c.name} size={24} />}
                <span className={clsx("min-w-0 flex-1 truncate text-[13.5px]", on ? "font-semibold text-ink" : "text-ink")}>{c.name}</span>
                <span className={clsx("num shrink-0 text-[12.5px]", on ? "text-kb-ink" : "text-ink-3")}>{c.score}</span>
              </button>
            </li>
          );
        })}
      </ul>
      {cands.some((c) => c.ambiguous) && (
        <p className="mt-2 text-[12.5px] text-ink-3">Top scores are close. Add detail to narrow it down.</p>
      )}
    </div>
  );
}

// ======================================================================= algorithms

export function AlgoList({ busy, running, onRun }: { busy: boolean; running: string | null; onRun: (id: string) => void }) {
  return (
    <ul className="-mt-1 divide-y divide-line">
      {ALGOS.map((a) => (
        <li key={a.id} className="flex min-h-[60px] items-center gap-3 py-2.5 last:pb-0" title={a.hint}>
          <div className="min-w-0 flex-1">
            <div className="text-[13.5px] font-medium text-ink">{a.label}</div>
            <div className="num mt-0.5 truncate text-[12px] text-ink-3">{a.proc}</div>
          </div>
          {running === a.id
            ? <Spinner label="Running" />
            : <Button size="sm" disabled={busy} onClick={() => onRun(a.id)} aria-label={`Run ${a.label}`}>Run</Button>}
        </li>
      ))}
    </ul>
  );
}

// ======================================================================= entity properties

const HIDDEN = ["aliases", "aliases_text", "views"];
const FIRST = ["name", "title", "key"];

function Value({ v }: { v: unknown }) {
  if (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) return <span className="text-ink-3">—</span>;
  if (typeof v === "number") {
    if (v > 1e6) return (
      <span className="flex flex-wrap items-baseline gap-x-2">
        <span className="num">{v.toLocaleString("en-IN")}</span>
        <span className="text-[12.5px] text-ink-3">{fmtINR(v)}</span>
      </span>
    );
    return <span className="num">{String(v)}</span>;
  }
  if (Array.isArray(v)) return <>{v.map(String).join(", ")}</>;
  if (typeof v === "object") return <span className="num text-[12.5px]">{JSON.stringify(v)}</span>;
  return <>{String(v)}</>;
}

export function Properties({ entity, label }: { entity: any; label: string | null }) {
  if (!entity) return (
    <Card title="Properties" className="min-w-0">
      <Empty title="Nothing selected" text="Click a client, person, work or tender to see its fields." />
    </Card>
  );
  const rank = (k: string) => (FIRST.includes(k) ? FIRST.indexOf(k) : FIRST.length);
  const rows = Object.entries(entity)
    .filter(([k]) => !k.startsWith("_") && !HIDDEN.includes(k))
    .sort(([a], [b]) => rank(a) - rank(b));
  return (
    <Card title="Properties" className="min-w-0" action={label ? <Badge>{label}</Badge> : null}>
      <div className="scroll-thin contain-scroll -mx-3 max-h-[26rem] overflow-y-auto">
        <table className="kt">
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={k}>
                <td className="num w-2/5 text-[12.5px] text-ink-3">{k}</td>
                <td className="wrap-anywhere text-ink"><Value v={v} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ======================================================================= algorithm results

export function AlgoResult({ a, onOpen }: { a: { name: string; data: any } | null; onOpen: OpenFn }) {
  if (!a) return (
    <Card title="Results" className="min-w-0">
      <Empty title="No results yet" text="Run an algorithm, or find paths between two entities." />
    </Card>
  );
  const meta = RESULT[a.name];
  const r = a.data?.result;
  const cypher: string | undefined = a.data?.cypher?.[0];
  return (
    <Card title={meta?.label ?? a.name.replace(/_/g, " ")} className="min-w-0"
      action={meta ? <span className="num text-[12px] text-ink-3">{meta.proc}</span> : null}>
      <div className="scroll-thin contain-scroll -mx-2 max-h-[26rem] overflow-y-auto px-2">
        {r == null ? <p className="text-[13px] text-ink-3">{a.data?.error || "No result."}</p>
          : a.name === "key_people" ? <KeyPeople people={r.people || []} onOpen={onOpen} />
          : a.name === "isolated_entities" ? <Isolated r={r} onOpen={onOpen} />
          : a.name === "communities" ? <Communities rows={r} />
          : a.name === "audit_bid" ? <Audit rows={r} evidence={a.data?.evidence || []} onOpen={onOpen} />
          : a.name === "explain_path" ? <Paths rows={r} />
          : <CodeCell code={JSON.stringify(r, null, 2)} label="JSON" />}
      </div>
      {cypher && (
        <details className="group mt-4 border-t border-line pt-3">
          <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-[13px] font-medium text-ink-2 transition-colors duration-150 ease-out hover:text-ink [&::-webkit-details-marker]:hidden">
            <ChevronRight size={16} strokeWidth={1.75} className="group-open:rotate-90 motion-safe:transition-transform motion-safe:duration-150" />
            Cypher
          </summary>
          <div className="mt-2.5"><CodeCell code={cypher} /></div>
        </details>
      )}
    </Card>
  );
}

function KeyPeople({ people, onOpen }: { people: any[]; onOpen: OpenFn }) {
  if (!people.length) return <p className="text-[13px] text-ink-3">No people found.</p>;
  const max = Math.max(...people.map((p) => Number(p.score) || 0));
  return (
    <ol className="-mx-2 flex flex-col gap-0.5">
      {people.map((p, i) => (
        <li key={p.key}>
          <button type="button" onClick={() => onOpen("Person", p.key)} className={ROW}
            title={[p.designation, `betweenness ${p.score}`].filter(Boolean).join(" · ")}>
            <span className="num w-4 shrink-0 text-right text-[12.5px] text-ink-3">{i + 1}</span>
            <Avatar name={p.name} size={28} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13.5px] font-medium text-ink">{p.name}</span>
              <span className="mt-1 flex items-center gap-3">
                <span className="block h-1.5 min-w-12 flex-1 overflow-hidden rounded-full bg-well">
                  <span className="block h-full rounded-full bg-kb" style={{ width: `${max > 0 ? ((Number(p.score) || 0) / max) * 100 : 0}%` }} />
                </span>
                <span className="num shrink-0 text-[12px] text-ink-3">{count(p.clients, "client")} · {count(p.works, "work")}</span>
              </span>
            </span>
          </button>
        </li>
      ))}
    </ol>
  );
}

function Isolated({ r, onOpen }: { r: any; onOpen: OpenFn }) {
  const iso: any[] = r.isolated || [];
  return (
    <>
      <div className="grid grid-cols-3 gap-4 border-b border-line pb-4">
        <Stat label="Main component" value={r.main_component_size} />
        <Stat label="Outside it" value={iso.length} />
        <Stat label="Components" value={r.n_components ?? "—"} />
      </div>
      {iso.length ? (
        <ul className="-mx-2 mt-2 flex flex-col gap-0.5">
          {iso.map((m) => (
            <li key={`${m.label}:${m.key}`}>
              <button type="button" onClick={() => onOpen(m.label, m.key)} className={ROW}>
                <span className="min-w-0 flex-1 truncate text-[13.5px] text-ink">{m.name || m.key}</span>
                <Badge>{m.label}</Badge>
              </button>
            </li>
          ))}
        </ul>
      ) : <p className="mt-3 text-[13px] text-ink-3">Every person and client is in one component.</p>}
      <p className="mt-3 text-[12.5px] leading-snug text-ink-3">
        Evidence-integrity check (algo.WCC over documents, works, clients, people, credentials): anything outside the main component has no evidence path, such as people with a CV but no work they led, or clients with no completed work on record.
      </p>
    </>
  );
}

function AvatarStack({ names }: { names: string[] }) {
  return (
    <span className="flex shrink-0 -space-x-1.5">
      {names.slice(0, 3).map((n) => (
        <span key={n} className="inline-flex rounded-full ring-2 ring-surface-solid"><Avatar name={n} size={24} /></span>
      ))}
    </span>
  );
}

function Communities({ rows }: { rows: any[] }) {
  const teams = rows.filter((c) => c.size > 1);
  const solo: string[] = rows.filter((c) => c.size <= 1).flatMap((c) => c.people || []);
  if (!rows.length) return <p className="text-[13px] text-ink-3">No communities found.</p>;
  return (
    <ul className="-mt-1 divide-y divide-line">
      {teams.map((c) => (
        <li key={c.communityId} className="flex items-center gap-3 py-3" title={`Community ${c.communityId}`}>
          <AvatarStack names={c.people} />
          <p className="min-w-0 flex-1 text-[13px] leading-snug text-ink-2">
            {c.people.slice(0, 8).join(", ")}{c.size > 8 ? ` and ${c.size - 8} more` : ""}
          </p>
          <span className="num shrink-0 text-[12.5px] text-ink-3">{count(c.size, "person", "people")}</span>
        </li>
      ))}
      {solo.length > 0 && (
        <li className="py-3 text-[13px] leading-snug text-ink-3">
          <span className="num">{solo.length}</span> outside any team: {solo.slice(0, 8).join(", ")}{solo.length > 8 ? ` and ${solo.length - 8} more` : ""}
        </li>
      )}
    </ul>
  );
}

function Audit({ rows, evidence, onOpen }: { rows: any[]; evidence: any[]; onOpen: OpenFn }) {
  const openDoc = useOpenDoc();
  if (!rows.length) return <p className="text-[13px] text-ink-3">No past bids on record.</p>;
  const flagged = rows.filter((b) => b.n_violations).length;
  return (
    <>
      <div className="grid grid-cols-2 gap-4 border-b border-line pb-4">
        <Stat label="Bids audited" value={rows.length} />
        <Stat label="With violations" value={flagged} tone={flagged ? "var(--bad)" : "var(--ok)"} />
      </div>
      <ul className="divide-y divide-line">
        {rows.map((b) => {
          const early: any[] = b.cited_before_completion || [];
          const uncert: string[] = b.proposed_before_certified || [];
          const ev = evidence.find((e) => e.doc_id && e.doc_id === b.doc);
          return (
            <li key={b.bid} className="py-3">
              <div className="flex items-center gap-2">
                <button type="button" onClick={() => onOpen("Bid", b.bid)}
                  className="num truncate text-[13.5px] font-semibold text-ink transition-colors duration-150 ease-out hover:text-kb-ink">
                  {b.bid}
                </button>
                <span className="shrink-0 text-[12.5px] text-ink-3">submitted <span className="num">{b.submitted}</span></span>
                <span className="ml-auto shrink-0"><Verdict v={b.n_violations ? "FAIL" : "PASS"} size="sm" /></span>
              </div>
              <p className={clsx("mt-1 text-[13px]", early.length ? "text-bad" : "text-ok")}>
                {count(early.length, "cited work")} not yet complete at submission
              </p>
              {early.length > 0 && (
                <ul className="mt-1 space-y-0.5 text-[12.5px] text-ink-3">
                  {early.slice(0, 3).map((w, i) => (
                    <li key={i} className="truncate">{w.title} · completed <span className="num">{w.completed}</span></li>
                  ))}
                  {early.length > 3 && <li>{early.length - 3} more</li>}
                </ul>
              )}
              {uncert.length > 0 && (
                <p className="mt-1 text-[13px] text-bad">{uncert.join(", ")} proposed before certification</p>
              )}
              {ev && (
                <button type="button" onClick={() => openDoc(ev)}
                  className="mt-1.5 inline-flex items-center gap-1.5 text-[12.5px] font-medium text-kb-ink hover:underline">
                  <FileText size={14} strokeWidth={1.75} />
                  <span className="num">{ev.doc_id}</span>{ev.page ? <span className="num text-ink-3">p.{ev.page}</span> : null}
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </>
  );
}

function Paths({ rows }: { rows: any[] }) {
  if (!rows.length) return <p className="text-[13px] text-ink-3">No path within 6 hops.</p>;
  return (
    <ol className="-mt-1 divide-y divide-line">
      {rows.map((p, i) => {
        const names = String(p.path || "").split(" → ");
        return (
          <li key={i} className="flex items-start gap-3 py-3">
            <span className="num w-4 shrink-0 pt-px text-right text-[12.5px] text-ink-3">{i + 1}</span>
            <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1.5 gap-y-1 text-[13.5px] leading-snug">
              {names.map((n, j) => (
                <Fragment key={j}>
                  {j > 0 && (
                    <span className="inline-flex items-center gap-1 text-ink-3">
                      {p.rels?.[j - 1] && <span className="num text-[11.5px]">{p.rels[j - 1]}</span>}
                      <ArrowRight size={14} strokeWidth={1.75} />
                    </span>
                  )}
                  <span className="font-medium text-ink">{n}</span>
                </Fragment>
              ))}
            </div>
            <span className="num shrink-0 pt-px text-[12.5px] text-ink-3">{count(p.hops, "hop")}</span>
          </li>
        );
      })}
    </ol>
  );
}
