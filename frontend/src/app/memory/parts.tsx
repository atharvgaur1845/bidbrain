"use client";

import clsx from "clsx";
import { Building2, FileText, GitBranch, ListChecks, MessageSquare, Scale } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Avatar, Badge, Button, Card, Empty, Markdown, Spinner, Verdict, useOpenDoc } from "@/components/ui";
import { api } from "@/lib/api";

// Table cells line up with the card's 20px gutter. `.kt` is unlayered CSS, so the overrides need `!`.
// Cards that hold a table clip it (`overflow-hidden`) so the row hover tint follows the rounded corners.
const EDGE_CELLS = "[&_td:first-child]:pl-5! [&_th:first-child]:pl-5! [&_td:last-child]:pr-5! [&_th:last-child]:pr-5!";
const LINK = "font-medium text-kb-ink hover:underline";

export function when(ms?: number | null) {
  if (!ms) return "";
  const s = (Date.now() - Number(ms)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)} d ago`;
  return new Date(Number(ms)).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

function PillLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link href={href}
      className={clsx(
        "inline-flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-lg border border-transparent bg-surface-solid px-3",
        "text-[12.5px] font-medium whitespace-nowrap text-ink shadow-[var(--shadow-border)]",
        "transition-[scale,box-shadow] duration-150 ease-out hover:shadow-[var(--shadow-border-hover)] motion-safe:active:scale-[0.96]")}>
      {children}
    </Link>
  );
}

function Toolbar({ children, action }: { children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="mb-3 flex min-h-8 items-center gap-3 text-[13px] text-ink-3">
      <div className="min-w-0">{children}</div>
      {action && <div className="ml-auto">{action}</div>}
    </div>
  );
}

function Loading({ label }: { label: string }) {
  return <div className="px-5 py-8"><Spinner label={label} /></div>;
}

// ======================================================================= conversation

export type Turn = { i: number; role: string; text: string; at?: number; mentions?: string[]; recalled?: number[] };
export type Case = { id: string; title?: string; verdict?: string; bid_date?: string; status?: string; verified?: boolean | null; created_at?: number };
export type Decision = {
  id: string; policy?: string; rationale?: string; predicate?: string; key?: string; subject?: string;
  chosen?: string | number | null; at?: number; actor?: string; actor_kind?: string; source_doc?: string;
  org_rule?: string | null; org_rule_status?: string | null; review?: string | null; reopened?: boolean;
};
export type Rule = { id: string; predicate: string; prefer: string; over: string; rationale?: string; by?: string; learned_from?: string; applied: number };

/** Groups the flat turn list into exchanges: a user question plus the replies that follow it. */
function exchanges(turns: Turn[]) {
  const out: { q?: Turn; a: Turn[] }[] = [];
  for (const t of turns) {
    if (t.role === "user") out.push({ q: t, a: [] });
    else if (out.length) out[out.length - 1].a.push(t);
    else out.push({ a: [t] });
  }
  return out;
}

function TurnHead({ who, t }: { who: React.ReactNode; t: Turn }) {
  return (
    <div className="flex items-baseline gap-2 text-[13px]">
      {who}
      {t.at ? <span className="text-ink-3">{when(t.at)}</span> : null}
      <span className="num ml-auto text-[12px] text-ink-3">turn {t.i}</span>
    </div>
  );
}

function Mentions({ items }: { items?: string[] }) {
  if (!items?.length) return null;
  return (
    <p className="mt-3 text-[12.5px] leading-relaxed text-ink-3">
      Mentions <span className="text-ink-2">{items.join(" · ")}</span>
    </p>
  );
}

function Recalled({ items }: { items?: number[] }) {
  if (!items?.length) return null;
  const sorted = [...items].sort((a, b) => a - b);
  return (
    <p className="mt-1 text-[12.5px] leading-relaxed text-ink-3">
      Recalled {sorted.length === 1 ? "turn" : "turns"} <span className="num text-ink-2">{sorted.join(", ")}</span>
      {" "}(older turns that mention the same entities)
    </p>
  );
}

function Reply({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  if (!text.trim()) return <p className="mt-1 text-[13px] text-ink-3">No explanation recorded.</p>;
  const long = text.length > 420 || text.split("\n").length > 7;
  return (
    <div className="mt-1">
      <div className={clsx("[&_.md>:first-child]:mt-0 [&_.md>:last-child]:mb-0",
        long && !open && "max-h-[8.5rem] overflow-hidden [mask-image:linear-gradient(to_bottom,black_55%,transparent)]")}>
        <Markdown>{text}</Markdown>
      </div>
      {long && (
        <Button type="button" size="sm" className="mt-2" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "Show less" : "Show more"}
        </Button>
      )}
    </div>
  );
}

export function ConversationTab({ turns, ready }: { turns: Turn[]; ready: boolean }) {
  const groups = exchanges(turns);
  return (
    <>
      <Toolbar action={<PillLink href="/ask/">Ask a follow-up</PillLink>}>
        Follow-ups read the last 6 turns, plus up to 3 older turns that mention a client, person or work the question names.
        <span className="num ml-2 hidden text-[12px] xl:inline">(:Turn)-[:MENTIONS]-&gt;(entity)&lt;-[:MENTIONS]-(:Turn)</span>
      </Toolbar>
      <Card pad={false}>
        {!ready ? <Loading label="Loading conversation" /> : groups.length === 0 ? (
          <Empty icon={<MessageSquare size={20} strokeWidth={1.75} />} title="No conversation yet"
            text={<>Questions you ask on <Link href="/ask/" className={LINK}>Ask</Link> are stored here as turns.</>} />
        ) : (
          <ol className="divide-y divide-line">
            {groups.map((g, k) => (
              <li key={g.q?.i ?? `a${g.a[0]?.i ?? k}`} className="flex gap-3 px-5 py-4">
                <div className="w-7 shrink-0">{g.q && <Avatar name="You" size={28} />}</div>
                <div className="min-w-0 flex-1">
                  {g.q && (
                    <>
                      <TurnHead t={g.q} who={<span className="font-semibold text-ink">You</span>} />
                      <p className="mt-1 text-[14px] leading-relaxed whitespace-pre-line text-ink">{g.q.text}</p>
                      <Mentions items={g.q.mentions} />
                      <Recalled items={g.q.recalled} />
                    </>
                  )}
                  {g.a.map((a) => (
                    <div key={a.i} className={clsx("well px-4 py-3", g.q && "mt-3")}>
                      <TurnHead t={a} who={<span className="text-[14px] font-extrabold tracking-[-0.04em] text-kb-ink">bidbrain</span>} />
                      <Reply text={a.text || ""} />
                      <Mentions items={a.mentions} />
                    </div>
                  ))}
                </div>
              </li>
            ))}
          </ol>
        )}
      </Card>
    </>
  );
}

// ======================================================================= cases

export function CasesTab({ cases, ready }: { cases: Case[]; ready: boolean }) {
  return (
    <>
      <Toolbar action={<PillLink href="/qualify/">Qualify a tender</PillLink>}>
        Agents hand work to each other through each case. Newest first.
      </Toolbar>
      <Card pad={false} className="overflow-hidden">
        {!ready ? <Loading label="Loading cases" /> : cases.length === 0 ? (
          <Empty icon={<GitBranch size={20} strokeWidth={1.75} />} title="No cases yet"
            text={<><Link href="/qualify/" className={LINK}>Qualify a tender</Link> to open a case in your graph.</>} />
        ) : (
          <div className="scroll-thin contain-scroll overflow-x-auto pb-1">
            <table className={clsx("kt", EDGE_CELLS)}>
              <thead>
                <tr><th>Case</th><th>Bid date</th><th>Opened</th><th>Checked</th><th className="text-right!">Verdict</th></tr>
              </thead>
              <tbody>
                {cases.map((c) => (
                  <tr key={c.id}>
                    <td className="min-w-[16rem]">
                      <div className="font-medium text-ink">{c.title || "Untitled tender"}</div>
                      <div className="num text-[12px] text-ink-3">{c.id}</div>
                    </td>
                    <td className="num whitespace-nowrap text-[12.5px] text-ink-2">{c.bid_date || "—"}</td>
                    <td className="whitespace-nowrap text-ink-3">{when(c.created_at) || "—"}</td>
                    <td className="whitespace-nowrap">
                      {c.verified === true ? <span className="text-ok">Verified</span>
                        : c.verified === false ? <span className="text-warn">Mismatches</span>
                        : <span className="text-ink-3">—</span>}
                    </td>
                    <td className="text-right!"><Verdict v={c.verdict} size="sm" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

// ======================================================================= decisions and rules

export function DecisionsTab({ mine, rules, total, openCards, ready }:
  { mine: Decision[]; rules: Rule[]; total: number; openCards: number; ready: boolean }) {
  const openDoc = useOpenDoc();
  const others = total - mine.length;
  return (
    <div className="flex flex-col gap-6">
      <Card title="Your decisions"
        action={<Link href="/decisions/" className={clsx("text-[13px]", LINK)}>All decisions{others > 0 ? ` (${others} by agents)` : ""}</Link>}
        pad={false} className="overflow-hidden">
        {!ready ? <Loading label="Loading decisions" /> : mine.length === 0 ? (
          <Empty icon={<Scale size={20} strokeWidth={1.75} />} title="No decisions yet"
            text={<>Pick the winning claim on a conflict in <Link href="/live/" className={LINK}>Live documents</Link>.
              {openCards > 0 && <> <span className="num">{openCards}</span> {openCards === 1 ? "conflict is" : "conflicts are"} open.</>}</>} />
        ) : (
          <div className="scroll-thin contain-scroll overflow-x-auto pb-1">
            <table className={clsx("kt", EDGE_CELLS)}>
              <thead>
                <tr><th>Subject</th><th>Field</th><th>Chosen value</th><th>Why</th><th>Source</th><th>When</th></tr>
              </thead>
              <tbody>
                {mine.map((d) => (
                  <tr key={d.id}>
                    <td className="min-w-[12rem]">
                      <div className="font-medium text-ink">{d.subject || d.key || "—"}</div>
                      {d.key && d.key !== d.subject && <div className="num text-[12px] text-ink-3">{d.key}</div>}
                    </td>
                    <td className="num text-[12.5px] whitespace-nowrap text-ink-2">{d.predicate || "—"}</td>
                    <td className="num font-semibold text-ink">{d.chosen ?? "—"}</td>
                    <td className="max-w-[18rem]">
                      <div className="truncate text-ink-2" title={d.rationale}>{d.rationale || "—"}</div>
                      {d.review && <div className="truncate text-[12px] text-warn" title={d.review}>{d.review}</div>}
                    </td>
                    <td>
                      {d.source_doc ? (
                        <Button type="button" variant="ghost" size="sm" className="num -ml-3"
                          onClick={() => openDoc({ doc_id: d.source_doc ?? "", what: [d.predicate, d.subject].filter(Boolean).join(" · ") })}>
                          <FileText size={16} strokeWidth={1.75} className="text-kb-ink" />
                          {d.source_doc}
                        </Button>
                      ) : <span className="text-ink-3">—</span>}
                    </td>
                    <td className="whitespace-nowrap text-ink-3">{when(d.at) || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Learned rules" pad={false} className="overflow-hidden"
        action={<span className="num hidden text-[12px] text-ink-3 sm:inline">(:PolicyRule)-[:LEARNED_FROM]-&gt;(:Decision)</span>}>
        {!ready ? <Loading label="Loading rules" /> : rules.length === 0 ? (
          <Empty icon={<ListChecks size={20} strokeWidth={1.75} />} title="No learned rules"
            text={<>Resolve a conflict with “Choose and learn rule” on <Link href="/live/" className={LINK}>Live documents</Link>. The next conflict of that shape resolves itself.</>} />
        ) : (
          <div className="scroll-thin contain-scroll overflow-x-auto pb-1">
            <table className={clsx("kt", EDGE_CELLS)}>
              <thead>
                <tr><th>Field</th><th>Rule</th><th>Learned from</th><th className="text-right!">Applied</th></tr>
              </thead>
              <tbody>
                {rules.map((r) => (
                  <tr key={r.id}>
                    <td className="num text-[12.5px] whitespace-nowrap text-ink-2">{r.predicate}</td>
                    <td className="min-w-[16rem]">
                      <div className="text-ink-2">Prefer <span className="font-semibold text-ink">{r.prefer}</span> over {r.over}</div>
                      {r.rationale && <div className="max-w-[28rem] truncate text-[12.5px] text-ink-3" title={r.rationale}>{r.rationale}</div>}
                    </td>
                    <td className="num text-[12px] whitespace-nowrap text-ink-3">
                      {r.learned_from || "—"}{r.by ? <span className="block font-sans">by {r.by}</span> : null}
                    </td>
                    <td className="num text-right! font-semibold text-ink">{r.applied}×</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

// ======================================================================= company memory

export type OrgRule = {
  id: string; predicate: string; prefer: string; over: string; rationale?: string; retired?: boolean;
  status?: string; promoted_by?: string; promoted_by_label?: string | null; promoted_at?: number;
  source_rule?: string; source_session?: string; mine?: boolean; contested_here?: boolean; contests?: number;
  learned_from?: { id?: string; rationale?: string; decided_by?: string; subject?: string; subject_key?: string;
                   chosen_value?: string; source_doc?: string; at?: number } | null;
};

/** Per-session switch: consult company memory here (off by default, so nobody else's rule changes your demo). */
export function CompanyMemoryToggle({ onChange }: { onChange?: (on: boolean, out?: any) => void }) {
  const [on, setOn] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => { api("/api/session/settings").then((s) => setOn(!!s.use_company_memory)).catch(() => {}); }, []);
  async function toggle() {
    if (on === null) return;
    setBusy(true); setErr("");
    try {
      const out = await api("/api/session/settings", { method: "POST", body: JSON.stringify({ use_company_memory: !on }) });
      setOn(!!out.use_company_memory);
      onChange?.(!!out.use_company_memory, out);
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally { setBusy(false); }
  }
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px]">
      <label className="inline-flex cursor-pointer items-center gap-2">
        <input type="checkbox" role="switch" aria-checked={!!on} checked={!!on} disabled={on === null || busy} onChange={toggle} />
        <span className="font-medium text-ink">Use company memory in this session</span>
      </label>
      <span className="text-ink-3">
        {on === null ? "…" : on ? "On: company rules settle matching conflicts here, after your own rules."
          : "Off: only this session's own rules apply."}
      </span>
      {err && <span className="text-bad">{err}</span>}
    </div>
  );
}

/** Rules promoted out of a session into the shared company graph, with where each one came from.
 *  Only the promoting session may retire a rule; any other session may contest it (it stops applying there
 *  and the decisions it made there reopen as cards). */
export function CompanyMemory({ rules, cap, graph, ready, onRetire, onContest, toggle }:
  { rules: OrgRule[]; cap?: number; graph?: string; ready: boolean; onRetire: (id: string) => Promise<void>;
    onContest?: (id: string) => Promise<void>; toggle?: React.ReactNode }) {
  const openDoc = useOpenDoc();
  const [busy, setBusy] = useState<string | null>(null);
  return (
    <Card title="Company memory" pad={false} className="overflow-hidden"
      action={<span className="num text-[12px] text-ink-3">
        {ready ? `${rules.length}${cap ? ` / ${cap}` : ""} rules` : ""}
        <span className="ml-2 hidden sm:inline">{graph || "bidbrain_org"}</span>
      </span>}>
      <p className="px-5 pt-1 pb-3 text-[13px] leading-snug text-ink-3">
        Shared by every session. A rule gets here only when someone presses “Promote to company memory” on{" "}
        <Link href="/live/" className={LINK}>Live documents</Link>. Each session checks its own learned rules first,
        then these (if company memory is on for it), then recency and authority.
      </p>
      {toggle && <div className="px-5 pb-3">{toggle}</div>}
      {!ready ? <Loading label="Loading company memory" /> : rules.length === 0 ? (
        <Empty icon={<Building2 size={20} strokeWidth={1.75} />} title="No company rules"
          text="Learn a rule on a conflict, then promote it. The next session that meets a conflict of that shape resolves it by this rule and cites it." />
      ) : (
        <div className="scroll-thin contain-scroll overflow-x-auto pb-1">
          <table className={clsx("kt", EDGE_CELLS)}>
            <thead>
              <tr><th>Field</th><th>Rule</th><th>Learned from</th><th>Promoted</th><th className="text-right!"><span className="sr-only">Retire or contest</span></th></tr>
            </thead>
            <tbody>
              {rules.map((r) => {
                const d = r.learned_from || {};
                return (
                  <tr key={r.id}>
                    <td className="num text-[12.5px] whitespace-nowrap text-ink-2">{r.predicate}</td>
                    <td className="min-w-[14rem]">
                      <div className="text-ink-2">Prefer <span className="font-semibold text-ink">{r.prefer}</span> over {r.over}</div>
                      <div className="num text-[12px] text-ink-3">{r.id}</div>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {r.mine && <Badge tone="blue">Yours</Badge>}
                        {r.contested_here && <Badge tone="warn">Contested here</Badge>}
                        {!!r.contests && <span className="num text-[11.5px] text-ink-3">{r.contests} contest{r.contests === 1 ? "" : "s"}</span>}
                      </div>
                    </td>
                    <td className="min-w-[16rem] max-w-[26rem]">
                      {d.id ? (
                        <>
                          <div className="text-[12.5px] text-ink-2">
                            <span className="num">{d.id}</span>
                            {d.decided_by ? <> by <span className="font-medium text-ink">{d.decided_by}</span></> : null}
                            {d.subject ? <> on {d.subject}</> : null}
                            {d.chosen_value ? <> → <span className="num font-semibold text-ink">{d.chosen_value}</span></> : null}
                          </div>
                          {d.rationale && <div className="truncate text-[12.5px] text-ink-3" title={d.rationale}>“{d.rationale}”</div>}
                          {d.source_doc && (
                            <button className="num text-[12px] text-kb-ink hover:underline"
                              onClick={() => openDoc({ doc_id: d.source_doc ?? "", what: `${r.predicate} · ${d.subject ?? ""}` })}>
                              {d.source_doc}
                            </button>
                          )}
                        </>
                      ) : <span className="text-ink-3">—</span>}
                    </td>
                    <td className="text-[12.5px] whitespace-nowrap text-ink-3">
                      <div title="Provenance is the promoting session's tag; the name was typed by the promoter and is not verified">
                        by <span className="num font-medium text-ink-2">{r.promoted_by || "—"}</span>
                        {r.promoted_by_label ? <span> ({r.promoted_by_label}, unverified)</span> : null}
                      </div>
                      <div>{when(r.promoted_at)}</div>
                      <div className="num text-[11.5px]" title="Source session (hashed) and the session rule promoted">
                        {r.source_session} · {r.source_rule}
                      </div>
                    </td>
                    <td className="text-right!">
                      {r.mine ? (
                        <Button size="sm" variant="ghost" disabled={busy !== null}
                          title="Take this rule out of company memory for every session"
                          onClick={async () => { setBusy(r.id); try { await onRetire(r.id); } finally { setBusy(null); } }}>
                          {busy === r.id ? "Retiring…" : "Retire"}
                        </Button>
                      ) : onContest ? (
                        <Button size="sm" variant="ghost" disabled={busy !== null || r.contested_here}
                          title="Stop applying this rule in your session and reopen the decisions it made here as cards"
                          onClick={async () => { setBusy(r.id); try { await onContest(r.id); } finally { setBusy(null); } }}>
                          {busy === r.id ? "Contesting…" : r.contested_here ? "Contested" : "Contest"}
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
