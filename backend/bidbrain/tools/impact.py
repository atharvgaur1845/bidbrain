"""Change impact and point-in-time facts: what a document changed, and what we believed when.

`change_impact(doc_id)` reconstructs the whole loop from the graph alone (no text search):

    (Document)<-[:FROM]-(Claim)<-[:BASED_ON]-(Decision)-[:SELECTS]->(winner)-[:ABOUT]->(subject)
    (Decision)<-[:CAUSED_BY]-(Finding {kind:'ripple'})<-[:HAS_FINDING]-(Clause)<-[:HAS_CLAUSE]-(Case)
    (Decision)<-[:TRIGGERED_BY]-(Action)

`fact_as_of(key, predicate, as_of)` answers "what did the company believe on date T" on two axes:

    valid time        the claim's document existed by T: its printed issue date (Document.issued_day, parsed at
                      ingest), else the claim's observed_day, else a stated bound for undated documents
    transaction time  the graph had recorded the claim by T (recorded_at; corpus claims are the starting record,
                      recorded_at 0, because the base graph is rebuilt at every boot) and no decision had set it
                      aside by T (invalidated_at); decisions count from Decision.at (corpus decisions: 0)

so a superseded value is never lost, and a value from accounts that did not yet exist is never reported.
"""
from __future__ import annotations

import datetime as dt
import re

from bidbrain.graph.client import Graph, day, iso
from bidbrain.tools.registry import tool


@tool("change_impact",
      """Everything one document changed, from the graph: the claims it carried, the decisions they triggered
      (policy, before -> after), the open bids whose clauses flipped because of each decision, and the follow-up
      actions created. Param doc_id (e.g. 'DOC-DEMO-1-20827').""",
      {"doc_id": {"type": "string"}}, ["doc_id"])
def change_impact(g: Graph, doc_id: str) -> dict:
    doc = g.read("""MATCH (d:Document {doc_id:$d}) RETURN d.doc_id AS doc_id, d.title AS title, d.doc_type AS doc_type,
                           d.issuer AS issuer, d.issued AS issued,
                           d.issued_basis AS issued_basis""",
                 {"d": doc_id})
    if not doc:
        raise ValueError(f"no document {doc_id} in this workspace")
    q = """MATCH (d:Document {doc_id:$d})<-[:FROM]-(c:Claim)-[:ABOUT]->(s)
           OPTIONAL MATCH (dec:Decision)-[:BASED_ON]->(c)
           OPTIONAL MATCH (dec)-[:SELECTS]->(win:Claim)
           OPTIONAL MATCH (dec)-[:BASED_ON]->(other:Claim) WHERE other.id <> win.id
           RETURN c.id AS claim, c.predicate AS predicate, c.value_str AS value, c.page AS page, c.status AS status,
                  labels(s)[0] AS subject_label, s.key AS subject_key, coalesce(s.title, s.name, s.key) AS subject,
                  dec.id AS decision, dec.policy AS policy, dec.rationale AS rationale, dec.at AS decided_at,
                  win.value_str AS chosen, collect(DISTINCT other.value_str) AS replaced,
                  coalesce(c.recorded_at, 0) AS recorded_at, c.invalidated_at AS invalidated_at,
                  c.observed_day AS observed_day, d.issued_day AS doc_issued_day, d.issued AS doc_issued,
                  d.issued_basis AS doc_issued_basis"""
    rows = g.read(q, {"d": doc_id})
    snap = _corpus_snapshot(g)
    for r in rows:   # both time axes per claim: when the graph recorded it, and from when its document is valid
        vf, basis = _valid_from(r, snap)
        r["valid_from"], r["valid_from_basis"] = iso(vf), basis
        r["recorded"] = "corpus (starting record)" if r["recorded_at"] == 0 else _iso_ms(r["recorded_at"])
        for k in ("observed_day", "doc_issued_day", "doc_issued", "doc_issued_basis"):
            r.pop(k, None)
    decisions = sorted({r["decision"] for r in rows if r["decision"]})
    flips = g.read("""MATCH (dec:Decision)<-[:CAUSED_BY]-(f:Finding {kind:'ripple'})<-[:HAS_FINDING]-(k:Clause)
                             <-[:HAS_CLAUSE]-(c:Case)
                      WHERE dec.id IN $ds
                      RETURN dec.id AS decision, c.id AS case_id, c.title AS case, k.ref AS clause, k.kind AS kind,
                             f.text AS what, f.severity AS severity, k.verdict AS verdict_now, c.verdict AS case_now,
                             f.case_from AS case_before, f.case_to AS case_after,
                             c.approved_recommendation AS approved""", {"ds": decisions})
    acts = g.read("""MATCH (a:Action)-[:TRIGGERED_BY]->(dec:Decision) WHERE dec.id IN $ds
                     RETURN a.id AS id, a.kind AS kind, a.status AS status, a.title AS title, a.case_id AS case_id,
                            dec.id AS decision""", {"ds": decisions})
    cypher = [q]
    return {"result": {"document": doc[0], "claims": rows, "decisions": decisions, "affected": flips,
                       "actions": acts, "valid_basis": VALID_BASIS,
                       "summary": f"{len(rows)} claim(s), {len(decisions)} decision(s), "
                                  f"{len({f['case_id'] for f in flips})} open bid(s) affected, {len(acts)} action(s)"},
            "evidence": [{"doc_id": doc_id, "page": r["page"], "what": f"{r['subject']} {r['predicate']} = {r['value']}"}
                         for r in rows],
            "cypher": cypher}


DAY_MS = 86_400_000

VALID_BASIS = (
    "Two time axes. Valid time: a claim counts as of T only if the document stating it existed by T's day "
    "(valid_from = the date the document prints as its own issue date; else the date the claim was observed in "
    "that document at ingest; else, for an undated document recorded live, the day it was recorded; else, for an "
    "undated corpus document, the corpus snapshot date, flagged as such). Transaction time: the graph must have "
    "recorded the claim by T (recorded_at; 0 = part of the corpus, the company's starting record), and a claim "
    "set aside by a decision (invalidated_at) stops counting from that instant. Decisions count from their own "
    "time (Decision.at; the corpus's own decisions are part of the starting record, at 0): the latest decision "
    "on the fact made by T picks the winner among the claims that count; otherwise the highest authority wins.")


def _ms(as_of: str) -> int:
    d = dt.datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def _iso_ms(ms: int | None) -> str | None:
    if ms is None:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="milliseconds")


def _corpus_snapshot(g: Graph) -> int | None:
    try:
        return g.scalar("MATCH (m:Meta {key:'meta'}) RETURN m.corpus_snapshot_day")
    except Exception:  # noqa: BLE001 - a graph without Meta simply has no snapshot date
        return None


def _valid_from(r: dict, snapshot_day: int | None) -> tuple[int | None, str]:
    """(day the stating document is known to have existed, basis) for one claim row."""
    if r.get("doc_issued_day") is not None:
        return r["doc_issued_day"], f"document date ({r.get('doc_issued_basis') or 'issue date'})"
    if r.get("doc_issued"):
        try:
            return day(r["doc_issued"]), "document date (issue date of the live document)"
        except ValueError:
            pass
    if r.get("observed_day") is not None:
        return r["observed_day"], "claim observed_day (the date the claim was read with from its document)"
    if (r.get("recorded_at") or 0) > 0:
        return r["recorded_at"] // DAY_MS, "undated document: counted from the day it was recorded"
    if snapshot_day is not None:
        return snapshot_day, "undated corpus document: counted from the corpus snapshot date, not its own date"
    return None, "undated: no document date in the data"


_CLAIMS_Q = """MATCH (c:Claim {subject_key:$k, predicate:$p})
           OPTIONAL MATCH (c)-[:FROM]->(d:Document)
           RETURN c.id AS claim, c.value_str AS value, c.source_kind AS source_kind, c.authority AS authority,
                  d.doc_id AS doc, c.page AS page, coalesce(c.recorded_at, 0) AS recorded_at,
                  c.invalidated_at AS invalidated_at, c.invalidated_by AS invalidated_by, c.status AS status_now,
                  c.status_history AS status_history, c.observed_day AS observed_day, d.issued_day AS doc_issued_day, d.issued AS doc_issued,
                  d.issued_basis AS doc_issued_basis
           ORDER BY recorded_at, claim"""
_DECISIONS_Q = """MATCH (c:Claim {subject_key:$k, predicate:$p})<-[:BASED_ON|SELECTS]-(dec:Decision)
           WITH DISTINCT dec OPTIONAL MATCH (dec)-[:SELECTS]->(w:Claim)
           RETURN dec.id AS decision, coalesce(dec.at, 0) AS at, dec.policy AS policy, w.id AS selects,
                  dec.reopened_at AS reopened_at
           ORDER BY at, decision"""


_HIST = re.compile(r"^([^:]*):(-?\d+)-(-?\d+):(.*)$")


def _set_aside_spans(r: dict) -> list[tuple[int, int | None]]:
    """[from, until) intervals in which a decision had set the claim aside: the current one (invalidated_at, open)
    and any earlier ones a reactivation closed (status_history '<status>:<from>-<until>:<decision>')."""
    spans = []
    for h in r.get("status_history") or []:
        m = _HIST.match(str(h))
        if m:
            spans.append((int(m.group(2)), int(m.group(3))))
    if r.get("invalidated_at") is not None:
        spans.append((r["invalidated_at"], None))
    return spans


def _aside_at(r: dict, t: int) -> bool:
    return any(a <= t and (b is None or t < b) for a, b in _set_aside_spans(r))


@tool("fact_as_of",
      """What the company believed about one fact at a point in time, on both time axes: a claim counts only if
      its document existed by then (valid time) and the graph had recorded it and not set it aside by then
      (transaction time); the latest decision made by then picks among those. Also returns the claims recorded
      later, the claims whose document is dated after T, and the ones set aside since. Params key (entity key),
      predicate (e.g. 'value'), as_of (ISO date or datetime).""",
      {"key": {"type": "string"}, "predicate": {"type": "string"}, "as_of": {"type": "string"}},
      ["key", "predicate", "as_of"])
def fact_as_of(g: Graph, key: str, predicate: str, as_of: str) -> dict:
    t = _ms(as_of)
    t_day = t // DAY_MS
    rows = g.read(_CLAIMS_Q, {"k": key, "p": predicate})
    if not rows:
        raise ValueError(f"no claims about {key}.{predicate}")
    snap = _corpus_snapshot(g)
    for r in rows:
        vf, basis = _valid_from(r, snap)
        r["valid_from_day"], r["valid_from"], r["valid_from_basis"] = vf, iso(vf), basis
        r["recorded"] = "corpus (starting record)" if r["recorded_at"] == 0 else _iso_ms(r["recorded_at"])
        for k in ("observed_day", "doc_issued_day", "doc_issued", "doc_issued_basis"):
            r.pop(k, None)
    decs = g.read(_DECISIONS_Q, {"k": key, "p": predicate})
    made = [d for d in decs if d["at"] <= t and (d["reopened_at"] is None or d["reopened_at"] > t)]
    head = made[-1] if made else None
    known = [r for r in rows if r["recorded_at"] <= t]
    issued = [r for r in known if r["valid_from_day"] is not None and r["valid_from_day"] <= t_day]
    live = [r for r in issued if not _aside_at(r, t)]
    chosen_by = head["selects"] if head else None
    best = sorted(live, key=lambda r: (r["claim"] != chosen_by, -(r["authority"] or 0),
                                       -(r["valid_from_day"] or 0), r["claim"]))
    win = best[0] if best else None
    how = (None if not win else f"selected by decision {head['decision']} ({head['policy']})"
           if chosen_by == win["claim"] else "highest authority among the claims that count")
    undated = sorted({r["source_kind"] for r in rows if not r["valid_from_basis"].startswith(("document", "claim"))})
    return {"result": {"as_of": as_of, "as_of_day": iso(t_day), "value": win["value"] if win else None,
                       "claim": win, "chosen_by": how, "decision_then": head, "valid_basis": VALID_BASIS,
                       "alive_then": live,
                       "learned_later": [r for r in rows if r["recorded_at"] > t],
                       "not_yet_issued": [r for r in known if r["valid_from_day"] is None or r["valid_from_day"] > t_day],
                       "set_aside_since": [r for r in live if any(a > t for a, _ in _set_aside_spans(r))],
                       "set_aside_by_then": [r for r in issued if _aside_at(r, t)],
                       "decisions_later": [d for d in decs if d["at"] > t],
                       "undated_kinds": undated,
                       "note": (f"no document date in the data for {', '.join(undated)}: those claims' valid_from is "
                                "a stated bound (see valid_from_basis), not the document's own date") if undated else None},
            "evidence": [{"doc_id": r["doc"], "page": r["page"], "what": f"{predicate} = {r['value']}"} for r in live],
            "cypher": [_CLAIMS_Q, _DECISIONS_Q]}
