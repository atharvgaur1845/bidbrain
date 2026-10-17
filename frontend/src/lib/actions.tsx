"use client";

// Accountable actions: shared types, labels and the Approve / Reject / Execute control used by /impact and /actions.

import { Check, Play, X } from "lucide-react";
import { useState } from "react";
import { Button, Notice } from "@/components/ui";
import { api } from "@/lib/api";

export type ActionEvent = { at: number; from: string | null; to: string; by: string; note?: string };
export type ActionRow = { id: string; kind: string; status: string; title: string; owner?: string | null; assignee?: string | null;
  case_id?: string | null; created_at?: number; updated_at?: number; decision?: string };
export type ActionFull = ActionRow & { detail?: string; target?: string; trigger?: { label: string; id: string } | null;
  cites?: string[]; events?: ActionEvent[]; result?: Record<string, any> | null; decided_by?: string; executed_by?: string;
  executed_at?: number };

export const KIND_LABEL: Record<string, string> = {
  review_bid: "Re-approve bid",
  request_reference_letter: "Request reference letter",
  renew_credential: "Renew credential",
  publish_to_company_record: "Publish to company record",
};
export const kindLabel = (k?: string) => KIND_LABEL[k || ""] || (k || "").replace(/_/g, " ");

/** What execute does, shown in the confirmation step. */
export const EXECUTE_EFFECT: Record<string, string> = {
  review_bid: "Re-verifies every clause of the case against the current graph and approves the new recommendation. It stops if any verdict disagrees.",
  request_reference_letter: "Records a reference-letter request against the work.",
  renew_credential: "Records a renewal request for the credential.",
  publish_to_company_record: "Writes this fact to the shared company record. Every new workspace starts from it.",
};

export function ago(ms?: number | null) {
  if (!ms) return "—";
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 0 || s > 86400 * 30) return new Date(ms).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
export const stamp = (ms?: number | null) =>
  ms ? new Date(ms).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";

/**
 * Approve / Reject (optional note) / Execute (with a confirmation step) for one action.
 * Calls the real endpoints; a 409 (stale click, re-verification disagreement) is shown as returned by the server.
 */
export function ActionControls({ action, onChange, size = "sm", withNote = false }:
  { action: Pick<ActionRow, "id" | "status" | "kind" | "title">; onChange: (a: ActionFull) => void; size?: "xs" | "sm"; withNote?: boolean }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [note, setNote] = useState("");
  const st = action.status;

  async function act(verb: "approve" | "reject" | "execute") {
    setBusy(verb); setErr(null);
    try {
      const a = await api<ActionFull>(`/api/actions/${encodeURIComponent(action.id)}/${verb}`,
        { method: "POST", body: JSON.stringify(note.trim() ? { note: note.trim() } : {}) });
      setConfirm(false); setNote("");
      onChange(a);
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(null);
    }
  }

  if (st === "executed" || st === "rejected" || st === "superseded" || st === "fulfilled") return null;
  if (st === "executing") return <span className="text-[12.5px] text-ink-3">Running…</span>;
  return (
    <div className="flex flex-col gap-2">
      {withNote && st === "pending" && !confirm && (
        <input className="field h-8! text-[12.5px]!" placeholder="Note (optional), saved in the audit trail" value={note}
          aria-label="Note for the audit trail" onChange={(e) => setNote(e.target.value)} maxLength={300} />
      )}
      {confirm ? (
        <div className="rounded-md border border-line-strong bg-surface-2 p-3">
          <div className="text-[13px] font-medium text-ink">Execute “{action.title}”?</div>
          <p className="mt-0.5 text-[12.5px] leading-snug text-ink-2">{EXECUTE_EFFECT[action.kind] || "Runs the deterministic executor for this action."}</p>
          <div className="mt-2.5 flex flex-wrap gap-2">
            <Button variant="primary" size={size} loading={busy === "execute"} onClick={() => act("execute")}>
              <Play size={13} strokeWidth={1.75} aria-hidden /> Confirm execute
            </Button>
            <Button variant="ghost" size={size} disabled={!!busy} onClick={() => setConfirm(false)}>Cancel</Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-1.5">
          {st === "pending" && (
            <Button variant="primary" size={size} loading={busy === "approve"} disabled={!!busy} onClick={() => act("approve")}>
              <Check size={13} strokeWidth={2} aria-hidden /> Approve
            </Button>
          )}
          {st === "approved" && (
            <Button variant="primary" size={size} disabled={!!busy} onClick={() => setConfirm(true)}>
              <Play size={13} strokeWidth={1.75} aria-hidden /> Execute
            </Button>
          )}
          <Button variant="secondary" size={size} loading={busy === "reject"} disabled={!!busy} onClick={() => act("reject")}>
            <X size={13} strokeWidth={2} aria-hidden /> Reject
          </Button>
          {st === "pending" && <span className="text-[12px] text-ink-3">Execute is enabled after approval</span>}
        </div>
      )}
      {err && <Notice tone="bad" title={/disagree/i.test(err) ? "Re-verification disagrees: not executed" : "Not done"}>{err}</Notice>}
    </div>
  );
}

/** The executor's result, as a compact definition block. */
export function ActionResult({ result }: { result?: Record<string, any> | null }) {
  if (!result) return null;
  const { summary, ...rest } = result;
  return (
    <div className="rounded-md border border-ok-line bg-ok-soft px-3 py-2 text-[12.5px] leading-snug">
      <div className="font-medium text-ok">Executed</div>
      {summary && <div className="text-ink-2">{summary}</div>}
      {Object.keys(rest).length > 0 && (
        <dl className="mt-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 text-[12px]">
          {Object.entries(rest).map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-ink-3">{k.replace(/_/g, " ")}</dt>
              <dd className="mono truncate text-ink-2">{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
