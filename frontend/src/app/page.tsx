"use client";

import Link from "next/link";
import { Fragment, useEffect, useMemo, useState } from "react";
import { GData } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { Tabs } from "@/components/ui";
import { API_BASE, api } from "@/lib/api";
import { ConnectTab, DataTab, Overview, OverviewTab, PillLink, Triple, fmt } from "./_home/parts";

const HIDE = new Set(["Chunk", "Meta", "TBLine", "LedgerAccount", "Measurement", "BOQItem", "Asset", "AnnualReport", "State", "Bank", "BusinessUnit", "FiscalYear"]);

export default function Home() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [ovFailed, setOvFailed] = useState(false);
  const [schema, setSchema] = useState<Triple[]>([]);
  const [schemaFailed, setSchemaFailed] = useState(false);
  const [origin, setOrigin] = useState("");
  const [tab, setTab] = useState("overview");
  useEffect(() => {
    api("/api/overview").then(setOv).catch(() => setOvFailed(true));
    api("/api/schema").then((r) => setSchema(r.triples || [])).catch(() => setSchemaFailed(true));
    // eslint-disable-next-line react-hooks/set-state-in-effect -- window exists only after mount (static export)
    setOrigin(API_BASE || window.location.origin);
  }, []);
  const schemaGraph: GData = useMemo(() => {
    const t = schema.filter((x) => !HIDE.has(x.src) && !HIDE.has(x.dst) && x.src !== x.dst);
    const labels = [...new Set(t.flatMap((x) => [x.src, x.dst]))];
    return { nodes: labels.map((l) => ({ id: `L:${l}`, label: l, name: l })),
             edges: t.map((x) => ({ source: `L:${x.src}`, target: `L:${x.dst}`, type: x.rel })) };
  }, [schema]);
  const mcpConfig = JSON.stringify({ mcpServers: { bidbrain: { type: "http", url: `${origin}/mcp/` } } }, null, 2);
  const count = (l: string): number | undefined => ov?.labels?.find((x) => x.label === l)?.n;
  const totalNodes = ov?.labels?.reduce((a, l) => a + l.n, 0);
  const totalEdges = ov?.relationships?.reduce((a, l) => a + l.n, 0);

  const facts: [unknown, string][] = [
    [count("Document"), "documents"], [totalNodes, "nodes"], [totalEdges, "relationships"], [ov?.decisions, "decisions"],
  ];

  return (
    <div>
      <PageHeader
        title="BidBrain"
        subtitle={<>
          A company brain for a construction bid desk, built on FalkorDB. It qualifies tenders clause by clause, proves
          what is <em>missing</em>, and re-checks every open bid when a corrigendum arrives. Same model, same questions:{" "}
          <Link href="/benchmark/" className="text-kb-ink underline-offset-2 hover:underline">64% exact on 333 multi-hop
          questions, against 18% for text-to-Cypher and 8% for vector RAG</Link>.
        </>}
        meta={ovFailed && !ov ? <span>Graph facts unavailable</span> : facts.map(([v, label], i) => (
          <Fragment key={label}>
            {i > 0 && <span aria-hidden>·</span>}
            <span><span className="num text-ink-2">{fmt(v)}</span> {label}</span>
          </Fragment>
        ))}
        actions={<>
          <PillLink href="/benchmark/">Benchmark</PillLink>
          <PillLink href="/qualify/" variant="primary">Qualify a tender</PillLink>
        </>}
      />

      <Tabs value={tab} onChange={setTab}
        tabs={[{ id: "overview", label: "Overview" }, { id: "data", label: "Data", count: ov?.labels?.length }, { id: "connect", label: "Connect" }]} />

      <div className="pt-6" role="tabpanel">
        {tab === "overview" && <OverviewTab ov={ov} count={count} onData={() => setTab("data")} />}
        {tab === "data" && <DataTab ov={ov} failed={ovFailed} schemaFailed={schemaFailed} triples={schema} graph={schemaGraph} />}
        {tab === "connect" && <ConnectTab ov={ov} mcpConfig={mcpConfig} origin={origin} />}
      </div>
    </div>
  );
}
