"use client";

import { FileStack, ScrollText } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { GData } from "@/components/GraphCanvas";
import { PageHeader } from "@/components/Shell";
import { ButtonLink, Tabs } from "@/components/ui";
import { API_BASE, api } from "@/lib/api";
import { ControlRoom } from "./_home/control";
import { ConnectTab, DataTab, Overview, Triple } from "./_home/parts";

const HIDE = new Set(["Chunk", "Meta", "TBLine", "LedgerAccount", "Measurement", "BOQItem", "Asset", "AnnualReport", "State", "Bank", "BusinessUnit", "FiscalYear"]);

export default function Home() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [ovFailed, setOvFailed] = useState(false);
  const [schema, setSchema] = useState<Triple[]>([]);
  const [schemaFailed, setSchemaFailed] = useState(false);
  const [origin, setOrigin] = useState("");
  const [tab, setTab] = useState("overview");

  const loadOverview = useCallback(() => {
    setOvFailed(false);
    api("/api/overview").then(setOv).catch(() => setOvFailed(true));
  }, []);
  useEffect(() => {
    loadOverview();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- window exists only after mount (static export)
    setOrigin(API_BASE || window.location.origin);
  }, [loadOverview]);
  useEffect(() => {
    if (tab !== "schema" || schema.length) return;
    api("/api/schema").then((r) => setSchema(r.triples || [])).catch(() => setSchemaFailed(true));
  }, [tab, schema.length]);

  const schemaGraph: GData = useMemo(() => {
    const t = schema.filter((x) => !HIDE.has(x.src) && !HIDE.has(x.dst) && x.src !== x.dst);
    const labels = [...new Set(t.flatMap((x) => [x.src, x.dst]))];
    return { nodes: labels.map((l) => ({ id: `L:${l}`, label: l, name: l })),
             edges: t.map((x) => ({ source: `L:${x.src}`, target: `L:${x.dst}`, type: x.rel })) };
  }, [schema]);
  const mcpConfig = JSON.stringify({ mcpServers: { bidbrain: { type: "http", url: `${origin}/mcp/` } } }, null, 2);

  return (
    <div>
      <PageHeader
        title="Control room"
        subtitle="What needs attention in your graph, why, and what to do next."
        meta={ov?.session ? <span>Session <span className="mono text-[11.5px] text-ink-2">{ov.session}</span></span> : null}
        actions={<>
          <ButtonLink href="/live/"><FileStack size={14} strokeWidth={1.75} aria-hidden /> Send a document</ButtonLink>
          <ButtonLink href="/qualify/" variant="primary"><ScrollText size={14} strokeWidth={1.75} aria-hidden /> Qualify a tender</ButtonLink>
        </>}
      />

      <Tabs value={tab} onChange={setTab} className="mb-4"
        tabs={[{ id: "overview", label: "Overview" }, { id: "schema", label: "Schema", count: ov?.labels?.length }, { id: "connect", label: "Connect (MCP)" }]} />

      <div role="tabpanel" aria-label={tab}>
        {tab === "overview" && <ControlRoom ov={ov} ovFailed={ovFailed} onSchema={() => setTab("schema")} onRetryOverview={loadOverview} />}
        {tab === "schema" && <DataTab ov={ov} failed={ovFailed} schemaFailed={schemaFailed} triples={schema} graph={schemaGraph} />}
        {tab === "connect" && <ConnectTab ov={ov} mcpConfig={mcpConfig} origin={origin} />}
      </div>
    </div>
  );
}
