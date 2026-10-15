"use client";

import { Brain, Database, GitBranch, MessageSquare } from "lucide-react";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/Shell";
import { Panel, Stat, Verdict } from "@/components/ui";
import { api, getSession } from "@/lib/api";

export default function MemoryPage() {
  const [mem, setMem] = useState<any>({ turns: [] });
  const [conf, setConf] = useState<any>({ decisions: [], rules: [] });
  const [cases, setCases] = useState<any[]>([]);
  const [ov, setOv] = useState<any>(null);
  const [sid, setSid] = useState("");
  useEffect(() => {
    getSession().then(setSid);
    api("/api/memory").then(setMem); api("/api/conflicts").then(setConf); api("/api/cases").then(setCases); api("/api/overview").then(setOv);
  }, []);
  const mine = conf.decisions.filter((d: any) => d.actor_kind === "User");
  return (
    <div>
      <PageHeader eyebrow="Agent memory & coordination" title="What BidBrain remembers — in your own graph">
        Your session is a separate FalkorDB graph, created with GRAPH.COPY from the company graph, and it persists between
        visits on this browser. Conversations, cases, the agents' handoffs, your decisions and the rules learned from them all live there as nodes and edges.
      </PageHeader>
      <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Your graph" value={<span className="text-[15px]">session_{sid}</span>} sub="isolated tenant · GRAPH.COPY" />
        <Stat label="Conversation turns" value={mem.turns.length} sub="(:Conversation)-[:HAS_TURN]->(:Turn)" />
        <Stat label="Cases" value={cases.length} sub="multi-agent shared state" />
        <Stat label="Your decisions / rules" value={`${mine.length} / ${conf.rules.length}`} sub="episodic → procedural" />
      </div>
      <div className="grid gap-5 lg:grid-cols-3">
        <Panel title={<span className="flex items-center gap-1.5"><MessageSquare size={13} /> Episodic: conversation</span>}>
          <div className="scroll-thin max-h-[60vh] space-y-2 overflow-y-auto">
            {mem.turns.length === 0 && <div className="text-[12.5px] text-ink-3">Ask something on the Ask page. Follow-up questions use these turns as context.</div>}
            {mem.turns.map((t: any) => (
              <div key={t.i} className={`rounded-lg px-3 py-2 text-[12.5px] ${t.role === "user" ? "bg-panel-2" : "border hairline"}`}>
                <div className="eyebrow">{t.role}</div>
                <div className="line-clamp-4 text-ink-2">{t.text}</div>
                {t.mentions?.length > 0 && <div className="mt-1 flex flex-wrap gap-1">{t.mentions.map((m: string) => <span key={m} className="chip !py-0 !text-[10.5px]">{m}</span>)}</div>}
              </div>
            ))}
          </div>
        </Panel>
        <Panel title={<span className="flex items-center gap-1.5"><GitBranch size={13} /> Shared state: cases</span>}>
          {cases.length === 0 && <div className="text-[12.5px] text-ink-3">Qualify a tender: six agents will hand work to each other through a Case subgraph.</div>}
          {cases.map((c) => (
            <div key={c.id} className="flex items-center gap-2 border-b hairline py-2 text-[12.5px]">
              <span className="min-w-0 flex-1 truncate">{c.title}</span><Verdict v={c.verdict} size="sm" />
            </div>
          ))}
        </Panel>
        <Panel title={<span className="flex items-center gap-1.5"><Brain size={13} /> Procedural: learned rules</span>}>
          {conf.rules.length === 0 && <div className="text-[12.5px] text-ink-3">None yet. Rules are learned from your conflict resolutions.</div>}
          {conf.rules.map((r: any) => <div key={r.id} className="py-1 text-[12.5px]"><b>{r.prefer}</b> over {r.over} for <span className="num">{r.predicate}</span> · applied {r.applied}×</div>)}
          <div className="mt-3 border-t hairline pt-3 text-[12px] text-ink-3"><Database size={12} className="mr-1 inline" />
            Graph version <span className="num">{ov?.version}</span>, bumped on every settled fact. Semantic memory is the company graph itself: {ov?.labels?.reduce((a: number, l: any) => a + l.n, 0)?.toLocaleString()} nodes.</div>
        </Panel>
      </div>
    </div>
  );
}
