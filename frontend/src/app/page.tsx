"use client";

import { ArrowRight, Copy, FileStack, GitBranch, MessageSquareText, Plug, ScrollText, ShieldQuestion } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import GraphCanvas, { GData } from "@/components/GraphCanvas";
import { Panel, Stat } from "@/components/ui";
import { API_BASE, api } from "@/lib/api";

const HIDE = new Set(["Chunk", "Meta", "TBLine", "LedgerAccount", "Measurement", "BOQItem", "Asset", "AnnualReport", "State", "Bank", "BusinessUnit", "FiscalYear"]);

const HEROES = [
  { href: "/qualify/", icon: ScrollText, title: "Tender in, bid memo out",
    text: "Six agents read an eligibility section, qualify each clause by graph traversal, and cite the certificate pages that prove it. A Verifier with no LLM re-derives every verdict." },
  { href: "/ask/", icon: ShieldQuestion, title: "Proof of absence",
    text: "\"Which works have no reference letter?\" Vector RAG answers from what it retrieved. The graph knows the boundary of the set and returns a proof." },
  { href: "/live/", icon: FileStack, title: "Live documents ripple",
    text: "A corrigendum arrives. It becomes a claim, the conflict is settled by policy or by you, and every open bid that relied on the fact is re-checked: BID → NO-BID." },
  { href: "/decisions/", icon: GitBranch, title: "Why did we decide that?",
    text: "Every settled fact is a Decision node with its policy, rationale, decider, and both documents. Your resolutions become rules the system reuses." },
];

export default function Home() {
  const [ov, setOv] = useState<any>(null);
  const [schema, setSchema] = useState<any[]>([]);
  const [origin, setOrigin] = useState("");
  useEffect(() => {
    api("/api/overview").then(setOv).catch(() => {});
    api("/api/schema").then((r) => setSchema(r.triples || [])).catch(() => {});
    setOrigin(API_BASE || window.location.origin);
  }, []);
  const schemaGraph: GData = useMemo(() => {
    const t = schema.filter((x) => !HIDE.has(x.src) && !HIDE.has(x.dst) && x.src !== x.dst);
    const labels = [...new Set(t.flatMap((x) => [x.src, x.dst]))];
    return { nodes: labels.map((l) => ({ id: `L:${l}`, label: l, name: l })),
             edges: t.map((x) => ({ source: `L:${x.src}`, target: `L:${x.dst}`, type: x.rel })) };
  }, [schema]);
  const mcpConfig = JSON.stringify({ mcpServers: { bidbrain: { type: "http", url: `${origin}/mcp/` } } }, null, 2);
  const n = (l: string) => ov?.labels?.find((x: any) => x.label === l)?.n ?? "–";
  const totalNodes = ov?.labels?.reduce((a: number, l: any) => a + l.n, 0);
  const totalEdges = ov?.relationships?.reduce((a: number, l: any) => a + l.n, 0);

  return (
    <div className="space-y-8">
      <section className="pt-2">
        <div className="eyebrow">Graph Hacks · Company Brain · built on FalkorDB</div>
        <h1 className="mt-2 max-w-4xl text-[38px] font-bold leading-[1.1] tracking-tight">
          The bid desk&apos;s company brain. <span className="text-accent">Every answer shows its path.</span>
        </h1>
        <p className="mt-3 max-w-3xl text-[15.5px] leading-relaxed text-ink-2">
          Infrastructure firms win public tenders by <em>proving</em> past performance. The proof is spread across completion
          certificates, reference letters, CVs, bank guarantees, ledgers and audited accounts, and those documents
          contradict each other over time. BidBrain puts all 687 documents of a construction company into one FalkorDB graph.
          Agents act on it: they qualify tenders, prove absences, settle conflicting documents and trace every decision back to its source.
        </p>
        <div className="mt-5 flex flex-wrap gap-2">
          <Link href="/qualify/" className="btn btn-primary">Qualify a tender <ArrowRight size={15} /></Link>
          <Link href="/ask/" className="btn"><MessageSquareText size={15} /> Ask a multi-hop question</Link>
          <Link href="/benchmark/" className="btn">See the benchmark</Link>
        </div>
      </section>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-6">
        <Stat label="Nodes" value={totalNodes?.toLocaleString() ?? "–"} sub="one graph per visitor" />
        <Stat label="Relationships" value={totalEdges?.toLocaleString() ?? "–"} />
        <Stat label="Completed works" value={n("Work")} sub={`${n("Client")} clients`} />
        <Stat label="Claims" value={n("Claim")} sub="every contestable fact, sourced" />
        <Stat label="Decisions" value={ov?.decisions ?? "–"} sub="settled conflicts, traceable" />
        <Stat label="Documents" value={n("Document")} sub={`${n("Chunk")} indexed pages`} />
      </section>

      <section className="grid gap-4 md:grid-cols-2">
        {HEROES.map(({ href, icon: Icon, title, text }) => (
          <Link key={href} href={href} className="panel group p-5 transition-colors hover:border-line-2">
            <div className="flex items-center gap-2.5">
              <span className="grid h-9 w-9 place-items-center rounded-lg bg-bg-2"><Icon size={18} className="text-accent" /></span>
              <h2 className="text-[17px] font-bold">{title}</h2>
              <ArrowRight size={16} className="ml-auto text-ink-3 transition-transform group-hover:translate-x-1" />
            </div>
            <p className="mt-2.5 text-[13.5px] leading-relaxed text-ink-2">{text}</p>
          </Link>
        ))}
      </section>

      <section className="grid gap-4 lg:grid-cols-5">
        <Panel title="The graph at a glance — labels and the relationships that join them" className="lg:col-span-3" pad={false}>
          <div className="p-2"><GraphCanvas data={schemaGraph} height={380} showAllLabels /></div>
        </Panel>
        <Panel title={<span className="flex items-center gap-1.5"><Plug size={13} /> Use it from any MCP client</span>} className="lg:col-span-2">
          <p className="text-[13px] leading-relaxed text-ink-2">The same graph tools the agents use are served over MCP (streamable HTTP), read-only against the company graph. Add this to Claude Desktop or any MCP host:</p>
          <pre className="num mt-3 overflow-x-auto rounded-lg border hairline bg-bg-2 p-3 text-[11.5px] text-ink-2">{mcpConfig}</pre>
          <button className="btn mt-2 !py-1.5 !text-[12px]" onClick={() => navigator.clipboard?.writeText(mcpConfig)}><Copy size={12} /> Copy</button>
          <div className="mt-3 text-[12px] text-ink-3">Tools include <span className="num">find_portfolio</span>, <span className="num">prove_absence</span>, <span className="num">qualify_clause</span>, <span className="num">explain_path</span>, <span className="num">key_people</span> and <span className="num">cypher_readonly</span>.</div>
        </Panel>
      </section>

      <Panel title="How the graph does the work">
        <div className="grid gap-4 text-[13px] leading-relaxed text-ink-2 md:grid-cols-4">
          <div><div className="mb-1 font-semibold text-ink">1 · Connected records</div>Person → credential → work → client → that client&apos;s whole portfolio → letters, bonds, invoices. Questions take 4–6 hops; each hop is one edge.</div>
          <div><div className="mb-1 font-semibold text-ink">2 · Claims, not overwrites</div>Every contestable value is a <span className="num">(:Claim)</span> tied to its document and page. Conflicts become <span className="num">CONTRADICTS</span> edges and are settled by <span className="num">(:Decision)</span> nodes.</div>
          <div><div className="mb-1 font-semibold text-ink">3 · Tools, not guesses</div>25 graph tools over MCP: traversals, exact integer arithmetic, absence proofs, CPWD eligibility rules, and FalkorDB algorithms (betweenness, WCC, SPpaths).</div>
          <div><div className="mb-1 font-semibold text-ink">4 · Shared, isolated memory</div>Agents hand off through a case subgraph. Each visitor works in a <span className="num">GRAPH.COPY</span> of the company graph, so nothing one person does leaks into another&apos;s.</div>
        </div>
      </Panel>
    </div>
  );
}
