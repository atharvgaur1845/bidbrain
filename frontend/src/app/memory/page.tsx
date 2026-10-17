"use client";

import { History as HistoryIcon } from "lucide-react";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/Shell";
import { Card, Stat, Tabs } from "@/components/ui";
import { api, getSession } from "@/lib/api";
import { CasesTab, CompanyMemory, ConversationTab, DecisionsTab, type Case, type Decision, type OrgRule, type Rule, type Turn } from "./parts";

type Conflicts = { cards?: unknown[]; decisions: Decision[]; rules: Rule[] };
type Overview = {
  version?: number | null; open_cards?: number;
  labels?: { label: string; n: number }[]; relationships?: { type: string; n: number }[];
};

const TABS = [
  { id: "conversation", label: "Conversation" },
  { id: "cases", label: "Cases" },
  { id: "decisions", label: "Decisions and rules" },
];

const fmt = (n?: number | null) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("en-IN"));
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** "2 bid, 1 no-bid" from the case list, most common verdict first. */
function verdictMix(cases: Case[]) {
  const counts = new Map<string, number>();
  for (const c of cases) {
    const v = (c.verdict || "pending").replace("_", " ").toLowerCase();
    counts.set(v, (counts.get(v) || 0) + 1);
  }
  return [...counts].sort((a, b) => b[1] - a[1]).slice(0, 2).map(([v, n]) => `${n} ${v}`).join(", ");
}

export default function MemoryPage() {
  const [mem, setMem] = useState<{ turns?: Turn[] }>({ turns: [] });
  const [conf, setConf] = useState<Conflicts>({ decisions: [], rules: [] });
  const [cases, setCases] = useState<Case[]>([]);
  const [ov, setOv] = useState<Overview | null>(null);
  const [sid, setSid] = useState("");
  const [loaded, setLoaded] = useState({ mem: false, conf: false, cases: false });
  const [tab, setTab] = useState("conversation");
  const [org, setOrg] = useState<{ rules: OrgRule[]; cap?: number; graph?: string }>({ rules: [] });
  const [orgReady, setOrgReady] = useState(false);
  const [orgError, setOrgError] = useState("");

  const loadOrg = () => api("/api/memory/org").then(setOrg).catch(() => {}).finally(() => setOrgReady(true));
  async function retire(id: string) {
    setOrgError("");
    try {
      await api(`/api/memory/org/${id}/retire`, { method: "POST", body: "{}" });
    } catch (e: any) {
      setOrgError(`Retire ${id}: ${e?.message || e}`);
    }
    await loadOrg();
  }

  useEffect(() => {
    const done = (k: keyof typeof loaded) => () => setLoaded((l) => ({ ...l, [k]: true }));
    getSession().then(setSid);
    api("/api/memory").then(setMem).catch(() => {}).finally(done("mem"));
    api("/api/conflicts").then(setConf).catch(() => {}).finally(done("conf"));
    api("/api/cases").then(setCases).catch(() => {}).finally(done("cases"));
    api("/api/overview").then(setOv).catch(() => {});
    loadOrg();
  }, []);

  const turns: Turn[] = mem.turns || [];
  const mine = conf.decisions.filter((d) => d.actor_kind === "User");
  const rules: Rule[] = conf.rules || [];
  const asked = turns.filter((t) => t.role === "user").length;
  const nodes = ov?.labels?.reduce((a, l) => a + l.n, 0);
  const edges = ov?.relationships?.reduce((a, r) => a + r.n, 0);
  const openCards = conf.cards?.length ?? ov?.open_cards ?? 0;

  return (
    <div>
      <PageHeader icon={<HistoryIcon size={19} strokeWidth={1.75} />} tone="lavender" title="Memory"
        subtitle="Your session is a private GRAPH.COPY of the company graph in FalkorDB. It keeps your conversation, cases, decisions and learned rules, and persists on this browser. Rules you promote go to company memory, a separate graph every session reads."
        meta={ov ? (
          <>
            <span>Version <span className="num text-ink-2">{fmt(ov.version)}</span></span>
            <span aria-hidden>·</span>
            <span><span className="num text-ink-2">{fmt(nodes)}</span> nodes</span>
            <span aria-hidden>·</span>
            <span><span className="num text-ink-2">{fmt(edges)}</span> relationships</span>
          </>
        ) : undefined} />

      <Card className="mb-6">
        <div className="grid grid-cols-2 gap-x-6 gap-y-5 pt-3 md:grid-cols-4 [&>*]:md:border-l [&>*]:md:border-line [&>*]:md:pl-6 [&>*:first-child]:md:border-l-0 [&>*:first-child]:md:pl-0">
          <Stat label="Your graph" value={<span className="text-[18px]" title={sid ? `session_${sid}` : undefined}>{sid ? `session_${sid}` : "…"}</span>}
            sub="Private to this browser" />
          <Stat label="Conversation turns" value={loaded.mem ? turns.length : "…"}
            sub={loaded.mem ? (turns.length ? `${plural(asked, "question")}, ${plural(turns.length - asked, "answer")}` : "Nothing asked yet") : undefined} />
          <Stat label="Cases" value={loaded.cases ? cases.length : "…"}
            sub={loaded.cases ? (cases.length ? verdictMix(cases) : "No tenders qualified yet") : undefined} />
          <Stat label="Your decisions / learned rules" value={loaded.conf ? `${mine.length} / ${rules.length}` : "…"}
            sub={loaded.conf ? (openCards ? `${plural(openCards, "conflict")} open` : "No open conflicts") : undefined} />
        </div>
      </Card>

      <div className="mb-6">
        <CompanyMemory rules={org.rules || []} cap={org.cap} graph={org.graph} ready={orgReady} onRetire={retire} />
        {orgError && <p className="mt-2 text-[13px] text-bad">{orgError}</p>}
      </div>

      <Tabs tabs={TABS} value={tab} onChange={setTab} />
      <div className="pt-6" role="tabpanel">
        {tab === "conversation" && <ConversationTab turns={turns} ready={loaded.mem} />}
        {tab === "cases" && <CasesTab cases={cases} ready={loaded.cases} />}
        {tab === "decisions" && (
          <DecisionsTab mine={mine} rules={rules} total={conf.decisions.length} openCards={openCards} ready={loaded.conf} />
        )}
      </div>
    </div>
  );
}
