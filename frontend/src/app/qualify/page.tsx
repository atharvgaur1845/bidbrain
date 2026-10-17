"use client";

import { Download } from "lucide-react";
import Link from "next/link";

import { useEffect, useState } from "react";
import GraphCanvas, { GData, mergeGraph } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { Button, Card, Empty, EvidenceList, Markdown, Notice, Spinner, StatusBadge, Tabs, Verdict } from "@/components/ui";
import { ActionRow, kindLabel } from "@/lib/actions";
import { api, stream } from "@/lib/api";
import { AgentPipeline, AgentStatus, Clause, Handoff, Plan, TenderList, VerdictTable, VerifierBanner } from "./parts";

type Tab = "verdicts" | "evidence" | "memo";

/** The bid pack: the memo (verdict per clause, evidence with document pages, gaps, risks) as a Markdown file. */
function downloadMemo(markdown: string, id: string) {
  const url = URL.createObjectURL(new Blob([markdown], { type: "text/markdown;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: `bid-pack-${id}.md` });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function QualifyPage() {
  const [tenders, setTenders] = useState<any[]>([]);
  const [pick, setPick] = useState<any>(null);
  const [text, setText] = useState("");
  const [status, setStatus] = useState<Record<string, AgentStatus>>({});
  const [summaries, setSummaries] = useState<Record<string, string>>({});
  const [clauses, setClauses] = useState<Clause[]>([]);
  const [graph, setGraph] = useState<GData>({ nodes: [], edges: [] });
  const [focus, setFocus] = useState<string | null>(null);
  const [memo, setMemo] = useState<{ markdown: string; overall: string; method: string; memo_check?: { ok: boolean; problems: string[] } } | null>(null);
  const [verified, setVerified] = useState<any>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [handoffs, setHandoffs] = useState<Handoff[]>([]);
  const [running, setRunning] = useState(false);
  const [ms, setMs] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("verdicts");
  const [caseId, setCaseId] = useState<string | null>(null);
  const [caseInfo, setCaseInfo] = useState<{ title?: string; verdict?: string; loaded?: boolean } | null>(null);
  const [actions, setActions] = useState<ActionRow[]>([]);

  const loadActions = (id: string) =>
    api<{ actions: ActionRow[] }>(`/api/actions?case_id=${encodeURIComponent(id)}`).then((r) => setActions(r.actions || [])).catch(() => {});

  /** Open a stored case (deep link ?case=<id>): clauses, findings, agent runs and memo from GET /api/cases/{id}. */
  async function openCase(id: string, list: any[]) {
    setErr(null);
    try {
      const d = await api(`/api/cases/${encodeURIComponent(id)}`);
      const t = list.find((x) => x.id === d.case.tender_ref);
      if (t) { setPick(t); setText(t.text || ""); }
      setCaseId(id);
      setCaseInfo({ title: d.case.title, verdict: d.case.verdict, loaded: true });
      setClauses((d.clauses || []).map((c: any) => ({ clause_id: c.id, ref: c.ref, kind: c.kind, text: c.text, params: c.params,
        verdict: c.verdict, basis: c.basis, corrected: c.corrected || undefined, evidence: c.evidence || [],
        findings: (c.findings || []).map((f: any) => ({ kind: f.kind, text: f.text, severity: f.severity, agent: f.agent || "" })) })));
      setStatus(Object.fromEntries((d.agent_runs || []).map((r: any) => [r.agent, r.status === "skipped" ? "skipped" : "done"])));
      setSummaries(Object.fromEntries((d.agent_runs || []).filter((r: any) => r.summary).map((r: any) => [r.agent, r.summary])));
      if (d.case.memo) setMemo({ markdown: d.case.memo, overall: d.case.verdict, method: "stored" });
      loadActions(id);
    } catch (e: any) {
      setErr(`Case ${id}: ${e?.message || e}`);
    }
  }

  useEffect(() => {
    api("/api/tenders").then((t) => {
      setTenders(t);
      const q = new URLSearchParams(window.location.search);
      const want = t.find((x: any) => x.id === q.get("tender")) || t[0];
      setPick(want); setText(want?.text || "");
      const c = q.get("case");
      if (c) openCase(c, t);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on mount
  }, []);

  async function run() {
    setRunning(true); setClauses([]); setGraph({ nodes: [], edges: [] }); setMemo(null); setVerified(null); setErr(null);
    setStatus({}); setSummaries({}); setFocus(null); setMs(null); setPlan(null); setHandoffs([]); setActions([]); setCaseInfo(null);
    const body = pick && text === pick.text ? { tender_id: pick.id } : { tender_id: pick?.id, text };
    await stream("/api/qualify", body, (ev) => {
      if (ev.type === "agent_start") setStatus((s) => ({ ...s, [ev.agent]: "running" }));
      if (ev.type === "plan") setPlan(ev as unknown as Plan);
      if (ev.type === "handoff") setHandoffs((hs) => [...hs, ev as unknown as Handoff]);
      if (ev.type === "agent_done") {
        setStatus((s) => ({ ...s, [ev.agent]: ev.skipped ? "skipped" : "done" }));
        if (ev.summary) setSummaries((s) => ({ ...s, [ev.agent]: ev.summary }));
      }
      if (ev.type === "clause") setClauses((cs) => [...cs, { clause_id: ev.clause_id, ref: ev.ref, kind: ev.kind, text: ev.text, params: ev.params, corrected: ev.corrected, findings: [] }]);
      if (ev.type === "verdict") {
        setClauses((cs) => cs.map((c) => (c.clause_id === ev.clause_id ? { ...c, verdict: ev.verdict, basis: ev.basis, evidence: ev.evidence, subgraph: ev.subgraph } : c)));
        if (ev.subgraph) setGraph((g) => mergeGraph(g, ev.subgraph));
      }
      if (ev.type === "finding") {
        setClauses((cs) => cs.map((c) => (c.clause_id === ev.clause_id ? { ...c, findings: [...c.findings, { kind: ev.kind, text: ev.text, severity: ev.severity, agent: ev.agent }] } : c)));
        if (ev.subgraph) setGraph((g) => mergeGraph(g, ev.subgraph));
      }
      if (ev.type === "verified") { setVerified(ev); setStatus((s) => ({ ...s, Verifier: "done" })); setSummaries((s) => ({ ...s, Verifier: ev.ok ? `${ev.checked} verdicts checked, all agree` : `${ev.mismatches.length} mismatches: needs review` })); }
      if (ev.type === "memo") { setMemo({ markdown: ev.markdown, overall: ev.overall, method: ev.method, memo_check: ev.memo_check }); setStatus((s) => ({ ...s, Assembler: "done" })); setSummaries((s) => ({ ...s, Assembler: `memo (${ev.method})` })); }
      if (ev.type === "case_open") { setCaseId(ev.case_id); setCaseInfo({ title: ev.title }); }
      if (ev.type === "done") setMs(ev.ms);
      if (ev.type === "result" && ev.data?.case?.id) {
        setActions(ev.data.actions || []);
        loadActions(ev.data.case.id);
        try { const u = new URL(window.location.href); u.searchParams.set("case", ev.data.case.id); u.searchParams.delete("tender"); window.history.replaceState(null, "", u); } catch {}
      }
      if (ev.type === "error") setErr(ev.message);
    });
    setRunning(false);
  }

  const focused = clauses.find((c) => c.clause_id === focus);
  const hl = new Set((focused?.subgraph?.nodes || []).map((n) => n.id));

  const evidence = focused ? focused.evidence || [] : clauses.flatMap((c) => c.evidence || []);
  const docCount = new Set(clauses.flatMap((c) => c.evidence || []).filter((e) => e?.doc_id).map((e) => `${e.doc_id}|${e.page}`)).size;
  const tally = (v: string) => clauses.filter((c) => c.verdict === v).length;
  const edited = !!pick && text !== pick.text;

  const meta = clauses.length > 0 ? (
    <>
      <span><span className="num text-ink-2">{clauses.length}</span> clauses</span>
      <span><span className="num text-ok">{tally("PASS")}</span> pass</span>
      <span><span className="num text-bad">{tally("FAIL")}</span> fail</span>
      <span><span className="num text-warn">{tally("AT_RISK")}</span> at risk</span>
    </>
  ) : pick ? (
    <>
      <span className="num">{pick.id}</span>
      <span>{pick.authority}</span>
    </>
  ) : null;

  return (
    <div>
      <PageHeader
        title="Qualify a tender"
        subtitle="A planner routes six agents over one case graph. They read each other's findings there and hand work back when one finds a fix. Every verdict links back to its evidence."
        meta={meta}
        actions={memo ? <Verdict v={memo.overall} size="lg" /> : null}
      />

      <div className="grid items-start gap-6 xl:grid-cols-12">
        {/* ---------------------------------------------------------------- left: input + pipeline */}
        <div className="flex flex-col gap-5 xl:col-span-4">
          <Card title="Tender">
            <TenderList tenders={tenders} pickedId={pick?.id} onPick={(t) => {
              setPick(t); setText(t.text);
              try { const u = new URL(window.location.href); u.searchParams.set("tender", t.id); u.searchParams.delete("case"); window.history.replaceState(null, "", u); } catch {}
            }} />

            <div className="mt-5">
              <div className="mb-2 flex items-baseline gap-2">
                <label htmlFor="eligibility" className="text-[13px] font-medium text-ink-2">Eligibility text</label>
                {edited && <span className="ml-auto text-[12px] text-ink-3">Edited, runs as custom text</span>}
              </div>
              <textarea id="eligibility" spellCheck={false}
                className="field num scroll-thin contain-scroll h-44 resize-y text-[12.5px]! leading-relaxed"
                value={text} onChange={(e) => setText(e.target.value)} />
            </div>

            <Button variant={running ? "secondary" : "primary"} static={running} aria-disabled={running || undefined}
              className="mt-4 w-full aria-disabled:cursor-progress" disabled={!running && !text.trim()}
              onClick={running ? undefined : () => { setTab("verdicts"); run(); }}>
              {running ? <Spinner label="Agents running" /> : "Run agents"}
            </Button>
          </Card>

          <Card title="Agent pipeline" action={ms !== null ? <span className="num text-[12.5px] text-ink-3">{ms.toFixed(0)} ms</span> : null}>
            <AgentPipeline status={status} summaries={summaries} plan={plan} handoffs={handoffs} verified={verified} />
          </Card>

          {caseId && (
            <Card title="Actions for this case" pad={false}
              subtitle={<span className="mono">{caseId}</span>}
              action={<Link href="/actions/" className="text-[12.5px] font-medium text-kb-ink hover:underline">Queue</Link>}>
              {actions.length ? (
                <ul className="divide-y divide-line">
                  {actions.map((a) => (
                    <li key={a.id}>
                      <Link href={`/actions/?id=${encodeURIComponent(a.id)}`} className="flex items-start gap-2 px-4 py-2 hover:bg-surface-2">
                        <span className="min-w-0 flex-1">
                          <span className="block text-[12px] text-ink-3">{kindLabel(a.kind)}</span>
                          <span className="block text-[13px] leading-snug text-ink">{a.title}</span>
                        </span>
                        <StatusBadge status={a.status} size="sm" />
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : <p className="px-4 py-3 text-[12.5px] text-ink-3">No follow-up actions queued for this case.</p>}
            </Card>
          )}
        </div>

        {/* ---------------------------------------------------------------- right: results */}
        <div className="flex min-w-0 flex-col gap-5 xl:col-span-8">
          <Tabs value={tab} onChange={(id) => setTab(id as Tab)}
            tabs={[
              { id: "verdicts", label: "Verdicts", count: clauses.length || undefined },
              { id: "evidence", label: "Evidence", count: docCount || undefined },
              { id: "memo", label: "Memo" },
            ]} />

          {caseInfo?.loaded && !running && (
            <Notice tone="info" title={<>Stored case <span className="mono">{caseId}</span></>}
              action={caseInfo.verdict ? <Verdict v={caseInfo.verdict} size="sm" /> : null}>
              {caseInfo.title}: clauses, findings and memo as they stand in the graph now. Run the agents to re-qualify.
            </Notice>
          )}
          {err && <Notice tone="bad" title="Error">{err}</Notice>}

          {tab === "verdicts" && (
            <Card pad={false} className="overflow-hidden">
              {clauses.length > 0 && (
                <div className="flex items-center gap-3 px-5 pb-1 pt-3.5 text-[12.5px] text-ink-3">
                  {focused
                    ? <>Clause <span className="num text-ink-2">{focused.ref}</span> selected
                        <Button variant="ghost" size="sm" className="ml-auto -my-1" onClick={() => setTab("evidence")}>View evidence</Button></>
                    : "Select a clause to trace its evidence."}
                </div>
              )}
              <VerdictTable clauses={clauses} focus={focus} running={running}
                onFocus={(id) => setFocus(id === focus ? null : id)} />
            </Card>
          )}

          {tab === "evidence" && (
            <div className="grid gap-5 lg:grid-cols-5">
              <Card pad={false} className="overflow-hidden lg:col-span-3"
                title={focused ? <>Clause <span className="num">{focused.ref}</span></> : "Evidence graph"}
                action={focused ? <Button variant="ghost" size="sm" onClick={() => setFocus(null)}>Show all</Button> : null}>
                <GraphCanvas data={graph} highlight={hl} height={420} emptyHint="Works, people and statements the agents rely on." />
              </Card>
              <Card title="Documents" className="lg:col-span-2">
                <div className="scroll-thin contain-scroll max-h-[420px] overflow-y-auto">
                  <EvidenceList items={evidence} />
                </div>
              </Card>
            </div>
          )}

          {tab === "memo" && (
            memo || verified ? (
              <Card title="Bid memo"
                action={memo ? <>
                  <span className="hidden text-[12.5px] text-ink-3 sm:inline">{memo.method === "llm" ? "Written by the Assembler" : "Assembled from the verified record"}</span>
                  <Button size="sm" onClick={() => downloadMemo(memo.markdown, pick?.id || "tender")}>
                    <Download size={14} strokeWidth={1.75} /> Download bid pack
                  </Button>
                </> : null}>
                <div className="flex flex-col gap-4">
                  {verified && <VerifierBanner verified={verified} />}
                  {memo?.memo_check && !memo.memo_check.ok && (
                    <div role="status" className="rounded-md border border-warn-line bg-warn-soft px-3 py-2 text-[13px] leading-snug text-ink-2">
                      <span className="font-semibold text-ink">LLM memo rejected. </span>
                      It {memo.memo_check.problems.slice(0, 3).join("; ")}. This memo is assembled from the verified record instead.
                    </div>
                  )}
                  {memo
                    ? <Markdown>{memo.markdown}</Markdown>
                    : running ? <Spinner label="Assembler is writing the memo" /> : null}
                </div>
              </Card>
            ) : (
              <Card>
                {running
                  ? <div className="grid place-items-center py-12"><Spinner label="The memo is written after the Verifier finishes" /></div>
                  : <Empty title="No memo yet" text="Run the agents. The Assembler writes the memo once the Verifier has re-derived every verdict." />}
              </Card>
            )
          )}
        </div>
      </div>
    </div>
  );
}
