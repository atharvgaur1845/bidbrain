"use client";

import { ScrollText as ScrollTextIcon, Download } from "lucide-react";

import { useEffect, useState } from "react";
import GraphCanvas, { GData, mergeGraph } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { Button, Card, Empty, EvidenceList, Markdown, Spinner, Tabs, Verdict } from "@/components/ui";
import { api, stream } from "@/lib/api";
import { AgentPipeline, AgentStatus, Clause, TenderList, VerdictTable, VerifierBanner } from "./parts";

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
  const [memo, setMemo] = useState<{ markdown: string; overall: string; method: string } | null>(null);
  const [verified, setVerified] = useState<any>(null);
  const [running, setRunning] = useState(false);
  const [ms, setMs] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("verdicts");

  useEffect(() => { api("/api/tenders").then((t) => { setTenders(t); setPick(t[0]); setText(t[0]?.text || ""); }); }, []);

  async function run() {
    setRunning(true); setClauses([]); setGraph({ nodes: [], edges: [] }); setMemo(null); setVerified(null); setErr(null);
    setStatus({}); setSummaries({}); setFocus(null); setMs(null);
    const body = pick && text === pick.text ? { tender_id: pick.id } : { tender_id: pick?.id, text };
    await stream("/api/qualify", body, (ev) => {
      if (ev.type === "agent_start") setStatus((s) => ({ ...s, [ev.agent]: "running" }));
      if (ev.type === "agent_done") {
        setStatus((s) => ({ ...s, [ev.agent]: "done" }));
        if (ev.summary) setSummaries((s) => ({ ...s, [ev.agent]: ev.summary }));
      }
      if (ev.type === "clause") setClauses((cs) => [...cs, { clause_id: ev.clause_id, ref: ev.ref, kind: ev.kind, text: ev.text, params: ev.params, findings: [] }]);
      if (ev.type === "verdict") {
        setClauses((cs) => cs.map((c) => (c.clause_id === ev.clause_id ? { ...c, verdict: ev.verdict, basis: ev.basis, evidence: ev.evidence, subgraph: ev.subgraph } : c)));
        if (ev.subgraph) setGraph((g) => mergeGraph(g, ev.subgraph));
      }
      if (ev.type === "finding") {
        setClauses((cs) => cs.map((c) => (c.clause_id === ev.clause_id ? { ...c, findings: [...c.findings, { kind: ev.kind, text: ev.text, severity: ev.severity, agent: ev.agent }] } : c)));
        if (ev.subgraph) setGraph((g) => mergeGraph(g, ev.subgraph));
      }
      if (ev.type === "verified") { setVerified(ev); setStatus((s) => ({ ...s, Verifier: "done" })); setSummaries((s) => ({ ...s, Verifier: ev.ok ? `${ev.checked} verdicts re-derived, all agree` : `${ev.mismatches.length} mismatches!` })); }
      if (ev.type === "memo") { setMemo({ markdown: ev.markdown, overall: ev.overall, method: ev.method }); setStatus((s) => ({ ...s, Assembler: "done" })); setSummaries((s) => ({ ...s, Assembler: `memo (${ev.method})` })); }
      if (ev.type === "done") setMs(ev.ms);
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
      <PageHeader icon={<ScrollTextIcon size={19} strokeWidth={1.75} />} tone="teal"
        title="Qualify a tender"
        subtitle="Six agents work on one case graph, each reading what the previous one wrote. Every verdict links back to its evidence."
        meta={meta}
        actions={memo ? <Verdict v={memo.overall} size="lg" /> : null}
      />

      <div className="grid items-start gap-6 xl:grid-cols-12">
        {/* ---------------------------------------------------------------- left: input + pipeline */}
        <div className="flex flex-col gap-5 xl:col-span-4">
          <Card title="Tender">
            <TenderList tenders={tenders} pickedId={pick?.id} onPick={(t) => { setPick(t); setText(t.text); }} />

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
            <AgentPipeline status={status} summaries={summaries} />
          </Card>
        </div>

        {/* ---------------------------------------------------------------- right: results */}
        <div className="flex min-w-0 flex-col gap-5 xl:col-span-8">
          <Tabs value={tab} onChange={(id) => setTab(id as Tab)}
            tabs={[
              { id: "verdicts", label: "Verdicts", count: clauses.length || undefined },
              { id: "evidence", label: "Evidence", count: docCount || undefined },
              { id: "memo", label: "Memo" },
            ]} />

          {err && <div role="alert" className="rounded-[10px] bg-bad-soft px-4 py-3 text-[13.5px] text-bad">{err}</div>}

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
