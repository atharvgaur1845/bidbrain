"""Accountable actions: what the bid desk must do because the graph changed.

A recommendation is not the end of the loop. When a corrigendum flips an open bid, or an auditor finds a
credited work with no reference letter, or a decision changes a company fact, an Action node records the
follow-up with its trigger, its evidence, its owner and its lifecycle:

    (:Action {id, kind, status, title, detail, owner, idem, created_at, updated_at, result})
        -[:FOR_CASE]->(:Case)              the open bid it concerns (if any)
        -[:TRIGGERED_BY]->(:Decision|:Finding)  why it exists
        -[:CITES]->(:Document)             the evidence behind it
        -[:ASSIGNED_TO]->(:Person)         the accountable person, when the graph knows one
        -[:HAS_EVENT]->(:ActionEvent {at, from, to, by, note})   the audit trail

    pending --approve--> approved --execute--> executing --> executed --(letter arrives)--> fulfilled
       |                    |                     |
       +------reject--------+--> rejected         +--> superseded   (review_bid whose case moved on)
       +--(the finding it was raised for no longer applies / the letter arrived)--> superseded | fulfilled

Rules:
* Creation is idempotent: the same (kind, case, trigger, target) is MERGEd on `idem`, so a retried ripple
  never doubles the queue.
* Every transition is a compare-and-set on the current status; a stale click gets a 409, a repeated one
  (or a concurrent identical one) gets the same answer.
* Execute claims the action first (CAS approved -> executing), then runs the executor, then CAS
  executing -> executed. Reject is allowed only from pending/approved, so it can never interleave with a
  running executor, and every executor write is conditioned on `status = 'executing'` and MERGEd on keys
  derived from the action, so a retry never duplicates a side effect and nothing is written under a
  rejected action. A failed executor hands the action back (executing -> approved); one left `executing`
  by a crashed process can be re-claimed after EXECUTING_LEASE_MS.
* Identity: workspace-local actions (review_bid, letters, renewals) stay session-scoped. Approving or executing
  a publish_to_company_record needs a signed-in principal (api/auth.py) and a second, different approver
  (company/record.py). Every ActionEvent records the verified `principal` (null when anonymous) next to `by`.
  An optional `version` makes a transition apply only to the version the caller saw (409 otherwise).
* Only deterministic code here executes an action, and an action is `executed` only after its executor
  returned. Nothing is reported as sent that was not: the reference-letter request is an internal
  DocumentRequest node, because there is no email integration.
"""
from __future__ import annotations

import hashlib
import json
import time

from bidbrain.graph.client import Graph

TRANSITIONS = {"approve": ("pending", "approved"), "reject": (("pending", "approved"), "rejected"),
               "execute": ("approved", "executed")}
# statuses after which nothing more happens to an action (execute on them returns the stored outcome)
DONE = ("executed", "superseded", "fulfilled")
EXECUTING_LEASE_MS = 120_000        # an `executing` claim older than this was left by a dead process
EXECUTE_WAIT_S = 10.0               # a concurrent execute waits this long for the running one to finish

KINDS = {
    "review_bid": "Re-approve an open bid whose recommendation changed",
    "request_reference_letter": "Ask the client for a reference letter the bid relies on",
    "renew_credential": "Confirm a key person's credential will be valid on the bid date",
    "publish_to_company_record": "Publish a decided fact to the company record so every new workspace uses it",
}


class ActionError(ValueError):
    def __init__(self, msg: str, status: int = 409):
        super().__init__(msg)
        self.status = status


class Superseded(Exception):
    """Raised by an executor that finds the action no longer describes the graph: nothing was done."""

    def __init__(self, reason: str, **extra):
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


def _idem(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def ensure(g: Graph, kind: str, *, title: str, detail: str, case_id: str | None = None,
           trigger: tuple[str, str] | None = None, target: str = "", cites: list[str] | None = None,
           owner_key: str | None = None, owner: str = "bid-manager", payload: dict | None = None) -> dict:
    """Create the action unless an identical one exists; returns {id, created, status}."""
    if kind not in KINDS:
        raise ActionError(f"unknown action kind {kind!r}; use one of {sorted(KINDS)}", 400)
    idem = _idem(kind, case_id, trigger and trigger[1], target)
    aid = "ACT-" + idem[:10]
    row = g.run("""MERGE (a:Action {idem:$idem})
                    ON CREATE SET a.id = $id, a.key = $id, a.kind = $kind, a.status = 'pending', a.title = $title,
                                  a.detail = $detail, a.owner = $owner, a.case_id = $case, a.target = $target,
                                  a.payload = $payload, a.created_at = timestamp(), a.updated_at = timestamp(),
                                  a._new = true, a.version = 1
                    WITH a, coalesce(a._new, false) AS created SET a._new = null
                    RETURN a.id AS id, a.status AS status, created""",
                 {"idem": idem, "id": aid, "kind": kind, "title": title, "detail": detail, "owner": owner,
                  "case": case_id, "target": target, "payload": json.dumps(payload or {})})[0]
    if row["created"]:
        if case_id:
            g.run("MATCH (a:Action {id:$a}), (c:Case {id:$c}) MERGE (a)-[:FOR_CASE]->(c)", {"a": aid, "c": case_id})
        if trigger:
            label = {"Decision": "Decision", "Finding": "Finding"}[trigger[0]]
            g.run(f"MATCH (a:Action {{id:$a}}), (t:{label} {{id:$t}}) MERGE (a)-[:TRIGGERED_BY]->(t)",
                  {"a": aid, "t": trigger[1]})
        for doc in cites or []:
            g.run("MATCH (a:Action {id:$a}), (d:Document {doc_id:$d}) MERGE (a)-[:CITES]->(d)", {"a": aid, "d": doc})
        if owner_key:
            g.run("MATCH (a:Action {id:$a}), (p:Person {key:$p}) MERGE (a)-[:ASSIGNED_TO]->(p)",
                  {"a": aid, "p": owner_key})
        _event(g, aid, None, "pending", "system", f"created: {KINDS[kind]}")
    return {"id": aid, "created": row["created"], "status": row["status"]}


def _event(g: Graph, aid: str, frm: str | None, to: str, by: str, note: str) -> None:
    from bidbrain.api import auth
    p = auth.current.get()
    g.run("""MATCH (a:Action {id:$a})
             CREATE (a)-[:HAS_EVENT]->(:ActionEvent {at:timestamp(), from:$f, to:$t, by:$by, note:$n,
                                                     principal:$p, target:$a})""",
          {"a": aid, "f": frm, "t": to, "by": by, "n": note, "p": p.name if p else None})


# ---- generation: the graph decides which follow-ups exist ------------------------------------------

def from_ripple(g: Graph, changed: list[dict], decision_id: str | None, cause: str,
                cause_doc: str | None) -> list[dict]:
    """One review_bid per open case whose recommendation changed; returns the actions touched."""
    out = []
    for c in changed:
        if not c.get("from") or c["from"] == c["to"]:
            continue
        title = g.scalar("MATCH (x:Case {id:$c}) RETURN x.title", {"c": c["case_id"]}) or c["case_id"]
        out.append(ensure(g, "review_bid", case_id=c["case_id"], target=f"{c['from']}->{c['to']}",
                          trigger=("Decision", decision_id) if decision_id else None,
                          cites=[cause_doc] if cause_doc else [],
                          title=f"Re-approve {title}: {c['from']} → {c['to']}",
                          detail=f"The recommendation changed because {cause}. Re-verify every clause against "
                                 f"the current graph and approve the new recommendation before submission."))
        # an older review of this case that asked to approve a different recommendation is now stale
        for old in g.read("""MATCH (a:Action {kind:'review_bid', case_id:$c}) WHERE a.status IN ['pending', 'approved']
                               AND a.id <> $new AND NOT a.target ENDS WITH $to
                             RETURN a.id AS id, a.target AS target""",
                          {"c": c["case_id"], "new": out[-1]["id"], "to": f"->{c['to']}"}):
            supersede(g, old["id"], "system",
                      f"the case's recommendation is now {c['to']} (because {cause}), not the "
                      f"{old['target'].split('->')[-1]} this review was raised to approve; {out[-1]['id']} replaces it")
    return out


def from_findings(g: Graph, case_id: str) -> list[dict]:
    """A reference-letter request for every credited work an absence finding flagged, owned by its lead."""
    rows = g.read("""MATCH (c:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause)-[:HAS_FINDING]->(f:Finding {kind:'absence'})
                     WHERE coalesce(f.status, 'open') <> 'resolved'
                     MATCH (f)-[:CITES]->(w:Work) WHERE coalesce(w.has_reference, false) = false
                     OPTIONAL MATCH (w)-[:FOR_CLIENT]->(cl:Client)
                     OPTIONAL MATCH (w)-[:LED_BY]->(p:Person)
                     RETURN DISTINCT f.id AS finding, k.ref AS clause, w.key AS work, w.title AS title,
                            cl.name AS client, p.key AS lead, p.name AS lead_name""", {"c": case_id})
    out = []
    for r in rows:
        out.append(ensure(g, "request_reference_letter", case_id=case_id, target=r["work"],
                          trigger=("Finding", r["finding"]), owner_key=r["lead"], owner=r["lead_name"] or "bid-manager",
                          title=f"Request a reference letter from {r['client'] or 'the client'} for {r['title']}",
                          detail=f"Clause {r['clause']} credits {r['title']}, but no reference letter for it exists "
                                 f"in the corpus (proved by prove_absence). Ask the client for one.",
                          payload={"work": r["work"], "client": r["client"]}))
    # an expiry finding cites every proposed person but names only those whose credential lapses around the
    # bid date: one renewal task per named person
    rows = g.read("""MATCH (c:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause)-[:HAS_FINDING]->(f:Finding {kind:'expiry'})
                     WHERE coalesce(f.status, 'open') <> 'resolved'
                     MATCH (f)-[:CITES]->(p:Person) WHERE f.text CONTAINS p.name
                     RETURN DISTINCT f.id AS finding, k.ref AS clause, f.text AS text, p.key AS person, p.name AS name""",
                  {"c": case_id})
    for r in rows:
        out.append(ensure(g, "renew_credential", case_id=case_id, target=r["person"],
                          trigger=("Finding", r["finding"]), owner_key=r["person"], owner=r["name"],
                          title=f"Confirm {r['name']}'s credential renewal before the bid date",
                          detail=f"Clause {r['clause']}: {r['text']}",
                          payload={"person": r["person"], "kind": "credential_renewal"}))
    return out


def for_decision(g: Graph, decision_id: str) -> dict | None:
    """A decided fact that changed a company record (not a planted eval claim) can be published."""
    d = g.read("""MATCH (d:Decision {id:$d})-[:SELECTS]->(w:Claim)
                  OPTIONAL MATCH (w)-[:FROM]->(doc:Document)
                  RETURN d.subject_key AS sk, d.predicate AS p, d.chosen_value AS v, d.policy AS policy,
                         doc.doc_id AS doc""", {"d": decision_id})
    if not d:
        return None
    d = d[0]
    return ensure(g, "publish_to_company_record", trigger=("Decision", decision_id), target=decision_id,
                  cites=[d["doc"]] if d["doc"] else [],
                  title=f"Publish {d['sk']} {d['p']} = {d['v']} to the company record",
                  detail=f"Decided by {d['policy']}. Once approved and executed, every new workspace starts from "
                         f"this value and cites this decision.",
                  payload={"decision_id": decision_id})


# ---- closure: the graph moved on, so the follow-up is no longer needed ------------------------------

def _close(g: Graph, aid: str, frm: tuple[str, ...], to: str, by: str, note: str, result: dict) -> bool:
    """CAS one action from any of `frm` to a terminal `to`, with an audit event. False if it had moved."""
    rows = g.run("""MATCH (a:Action {id:$a}) WHERE a.status IN $frm
                     WITH a, a.status AS before
                     SET a.status = $to, a.updated_at = timestamp(), a.closed_at = timestamp(), a.closed_by = $by,
                         a.closure = $r, a.result = coalesce(a.result, $r)
                     RETURN before""",
                 {"a": aid, "frm": list(frm), "to": to, "by": by, "r": json.dumps(result, default=str)})
    if rows:
        _event(g, aid, rows[0]["before"], to, by, note)
    return bool(rows)


def supersede(g: Graph, aid: str, by: str, reason: str) -> bool:
    """A pending/approved action that no longer describes the graph is closed without running."""
    return _close(g, aid, ("pending", "approved"), "superseded", by, f"superseded: {reason}",
                  {"superseded": True, "reason": reason, "summary": f"superseded, nothing executed: {reason}"})


def close_for_findings(g: Graph, finding_ids: list[str], reason: str) -> list[str]:
    """Supersede the open follow-ups raised for findings that no longer apply (a ripple moved the clause
    off the work or person they named). Returns the actions closed."""
    rows = g.read("""MATCH (a:Action)-[:TRIGGERED_BY]->(f:Finding) WHERE f.id IN $f
                       AND a.status IN ['pending', 'approved'] RETURN DISTINCT a.id AS id""", {"f": finding_ids})
    return [r["id"] for r in rows if supersede(g, r["id"], "system", reason)]


def fulfil_reference(g: Graph, work_keys: list[str], doc_id: str, by: str = "system") -> list[str]:
    """A reference letter for these works is now on file (`doc_id`): every request_reference_letter for them
    is fulfilled (pending/approved: no request needs sending any more; executed: the request was answered)
    and the internal DocumentRequest it created is marked fulfilled by that document. Returns the actions."""
    acts = g.read("""MATCH (a:Action {kind:'request_reference_letter'})
                     WHERE a.target IN $w AND a.status IN ['pending', 'approved', 'executed']
                     RETURN a.id AS id, a.target AS work, a.status AS status ORDER BY id""", {"w": work_keys})
    g.run("""MATCH (r:DocumentRequest {kind:'reference_letter'})-[:FOR_WORK]->(w:Work) WHERE w.key IN $w
               AND r.status = 'requested'
             MATCH (d:Document {doc_id:$d})
             SET r.status = 'fulfilled', r.fulfilled_at = timestamp(), r.fulfilled_by = $d
             MERGE (r)-[:FULFILLED_BY]->(d)""", {"w": work_keys, "d": doc_id})
    out = []
    for a in acts:
        what = ("no request needs sending" if a["status"] != "executed" else "the request was answered")
        note = f"fulfilled: reference letter {doc_id} for {a['work']} is now on file; {what}"
        if _close(g, a["id"], (a["status"],), "fulfilled", by, note,
                  {"fulfilled_by": doc_id, "work": a["work"], "was": a["status"], "summary": note}):
            g.run("MATCH (a:Action {id:$a}), (d:Document {doc_id:$d}) MERGE (a)-[:FULFILLED_BY]->(d)",
                  {"a": a["id"], "d": doc_id})
            out.append(a["id"])
    return out


# ---- lifecycle -----------------------------------------------------------------------------------

def transition(g: Graph, aid: str, verb: str, by: str, note: str = "", version: int | None = None) -> dict:
    if verb not in ("approve", "reject"):
        raise ActionError(f"unknown transition {verb!r}", 400)
    frm, to = TRANSITIONS[verb]
    frm = frm if isinstance(frm, tuple) else (frm,)
    cur = g.scalar("MATCH (a:Action {id:$a}) RETURN a.status", {"a": aid})
    if cur is None:
        raise ActionError(f"no action {aid}", 404)
    if cur == to:
        return get(g, aid)                      # a repeated click: same answer, no second event
    if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
        raise ActionError("version must be an integer", 400)
    if verb == "approve":                       # four eyes on publish_to_company_record (see company/record.py)
        from bidbrain.company import record
        record.check_publish_approval(g, aid, by)
    from bidbrain.api import auth
    p = auth.current.get()
    rows = g.run("""MATCH (a:Action {id:$a}) WHERE a.status IN $frm AND ($v IS NULL OR coalesce(a.version, 1) = $v)
                     WITH a, a.status AS before
                     SET a.status = $to, a.updated_at = timestamp(), a.decided_by = $by,
                         a.decided_principal = $p, a.version = coalesce(a.version, 1) + 1 RETURN before""",
                  {"a": aid, "frm": list(frm), "to": to, "by": by, "v": version, "p": p.name if p else None})
    if not rows:
        now = g.scalar("MATCH (a:Action {id:$a}) RETURN a.status", {"a": aid})
        if now == to:
            return get(g, aid)                  # a concurrent identical click won the CAS: same answer
        if version is not None and now in frm:
            raise ActionError(f"{aid} changed since version {version}; reload it and decide again")
        raise ActionError(f"{aid} is {now}; cannot {verb} it"
                          + (" while it is being executed" if now == "executing" else ""))
    _event(g, aid, rows[0]["before"], to, by, note or verb)
    return get(g, aid)


def execute(g: Graph, aid: str, by: str, wait_s: float = EXECUTE_WAIT_S) -> dict:
    """approved -> executing (CAS: exactly one caller wins) -> executor -> executing -> executed.

    A repeat on a finished action returns its stored outcome; a call that finds another one running waits
    for it (up to `wait_s`) and then returns the same outcome. An executor that finds the action stale
    (Superseded) closes it as `superseded` without doing anything; one that fails hands it back to
    `approved`, so it can be retried (every executor is idempotent)."""
    deadline = time.monotonic() + wait_s
    if g.scalar("MATCH (a:Action {id:$a}) RETURN a.kind", {"a": aid}) == "publish_to_company_record":
        from bidbrain.api import auth
        from bidbrain.company import record
        if not record.approving_admin.get():
            try:
                auth.require("executing a company-record publication")
            except auth.AuthError as exc:
                raise ActionError(str(exc), exc.status)
    while True:
        a = get(g, aid)
        st = a["status"]
        if st in DONE:
            return a                            # idempotent: the stored outcome, nothing re-run
        if st == "executing":
            if g.run("""MATCH (a:Action {id:$a}) WHERE a.status = 'executing'
                          AND coalesce(a.executing_since, 0) < timestamp() - $lease
                        SET a.executing_since = timestamp(), a.executing_by = $by RETURN a.id AS id""",
                     {"a": aid, "lease": EXECUTING_LEASE_MS, "by": by}):
                break                           # the claim was left by a dead process: take it over
            if time.monotonic() >= deadline:
                raise ActionError(f"{aid} is being executed by another request; retry shortly")
            time.sleep(0.05)
            continue
        if st != "approved":
            raise ActionError(f"{aid} is {st}; only an approved action can be executed")
        if g.run("""MATCH (a:Action {id:$a}) WHERE a.status = 'approved'
                    SET a.status = 'executing', a.executing_since = timestamp(), a.executing_by = $by,
                        a.updated_at = timestamp() RETURN a.id AS id""", {"a": aid, "by": by}):
            break                               # this call owns the execution
        # lost the CAS (a concurrent execute or reject moved it): look again
    a = get(g, aid)
    try:
        result = EXECUTORS[a["kind"]](g, a, by)
    except Superseded as exc:
        result = {"superseded": True, "reason": exc.reason, **exc.extra,
                  "summary": f"superseded, nothing executed: {exc.reason}"}
        if g.run("""MATCH (a:Action {id:$a}) WHERE a.status = 'executing'
                    SET a.status = 'superseded', a.updated_at = timestamp(), a.closed_at = timestamp(),
                        a.closed_by = $by, a.result = $r, a.closure = $r, a.executing_since = null
                    RETURN a.id AS id""", {"a": aid, "by": by, "r": json.dumps(result, default=str)}):
            _event(g, aid, "approved", "superseded", by, result["summary"])
        return get(g, aid)
    except Exception as exc:
        if g.run("""MATCH (a:Action {id:$a}) WHERE a.status = 'executing'
                    SET a.status = 'approved', a.updated_at = timestamp(), a.executing_since = null
                    RETURN a.id AS id""", {"a": aid}):
            _event(g, aid, "approved", "approved", by, f"execution failed, nothing recorded as done: {exc}")
        raise
    if g.run("""MATCH (a:Action {id:$a}) WHERE a.status = 'executing'
                SET a.status = 'executed', a.updated_at = timestamp(), a.executed_at = timestamp(),
                    a.executed_by = $by, a.result = $r, a.executing_since = null RETURN a.id AS id""",
             {"a": aid, "by": by, "r": json.dumps(result, default=str)}):
        _event(g, aid, "approved", "executed", by, result.get("summary", "executed"))
    return get(g, aid)


def _exec_review_bid(g: Graph, a: dict, by: str) -> dict:
    """Independent re-verification of every clause of the case against the graph as it is now."""
    from bidbrain.tools.case import overall_verdict
    from bidbrain.tools.proof import qualify_clause
    case = g.read("MATCH (c:Case {id:$c}) RETURN c.bid_date AS bd, c.verdict AS v", {"c": a["case_id"]})
    if not case:
        raise ActionError(f"case {a['case_id']} no longer exists", 410)
    # the action asks to approve one specific change (from -> to); if the case has moved on since, approving
    # "whatever the verdict is now" would sign off something nobody reviewed
    frm, _, to = (a.get("target") or "").partition("->")
    if to and case[0]["v"] != to:
        newer = g.scalar("""MATCH (n:Action {kind:'review_bid', case_id:$c}) WHERE n.id <> $a
                              AND n.status IN ['pending', 'approved', 'executing', 'executed']
                              AND n.target ENDS WITH $now
                            RETURN n.id ORDER BY n.created_at DESC LIMIT 1""",
                         {"c": a["case_id"], "a": a["id"], "now": f"->{case[0]['v']}"})
        raise Superseded(f"this review was raised to approve {frm} -> {to}, but the case's recommendation is "
                         f"now {case[0]['v']}" + (f"; {newer} covers the current change" if newer else
                                                  "; re-run the review for the current recommendation"),
                         case_verdict=case[0]["v"], expected=to, replaced_by=newer)
    clauses = g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause)
                        RETURN k.id AS id, k.ref AS ref, k.kind AS kind, k.params AS params, k.verdict AS verdict""",
                     {"c": a["case_id"]})
    checked, mismatches = 0, []
    for k in clauses:
        if not k["params"] or k["kind"] in (None, "other"):
            continue
        try:
            now = qualify_clause(g, k["kind"], json.loads(k["params"]), case[0]["bd"])["result"]["verdict"]
        except Exception:
            continue
        checked += 1
        if now != k["verdict"]:
            mismatches.append({"clause": k["ref"], "stored": k["verdict"], "recomputed": now})
    rec = overall_verdict(g, a["case_id"])
    if mismatches:
        raise ActionError(f"re-verification disagrees with the stored verdicts on {len(mismatches)} clause(s): "
                          f"{mismatches}; re-run the tender team before approving", 409)
    g.run("""MATCH (a:Action {id:$a}) WHERE a.status = 'executing'
             MATCH (c:Case {id:$c}) SET c.approved_recommendation = $r, c.reviewed_at = timestamp(),
             c.reviewed_by = $by, c.reviewed_action = $a""", {"a": a["id"], "c": a["case_id"], "r": rec, "by": by})
    return {"clauses_checked": checked, "recommendation": rec,
            "summary": f"re-verified {checked} clause(s) against the graph; recommendation {rec} approved"}


def _exec_renewal(g: Graph, a: dict, by: str) -> dict:
    p = json.loads(a.get("payload") or "{}")
    rid = "REQ-" + _idem("renewal", p.get("person"), a.get("case_id"))[:10]
    # one statement, conditioned on this action holding the execution claim: nothing is written for an
    # action that is not executing, and a retry MERGEs onto the same nodes and edges
    g.run("""MATCH (a:Action {id:$a}) WHERE a.status = 'executing'
             MERGE (r:DocumentRequest {id:$id}) ON CREATE SET r.key = $id, r.kind = 'credential_renewal',
                 r.status = 'requested', r.requested_at = timestamp(), r.requested_by = $by, r.channel = 'internal',
                 r.action_id = $a
             MERGE (a)-[:CREATED]->(r)
             WITH r MATCH (p:Person {key:$p}) MERGE (r)-[:FOR_PERSON]->(p)""",
          {"id": rid, "by": by, "p": p.get("person"), "a": a["id"]})
    return {"request_id": rid, "channel": "internal",
            "summary": f"renewal proof requested from the employee as {rid} (internal queue; nothing was emailed)"}


def _exec_request_letter(g: Graph, a: dict, by: str) -> dict:
    p = json.loads(a.get("payload") or "{}")
    rid = "REQ-" + _idem("letter", p.get("work"))[:10]
    # one request per work (two bids relying on the same letter share it), written only while this action
    # holds the execution claim; MERGE makes a retry a no-op
    g.run("""MATCH (a:Action {id:$a}) WHERE a.status = 'executing'
             MERGE (r:DocumentRequest {id:$id}) ON CREATE SET r.key = $id, r.kind = 'reference_letter',
                 r.status = 'requested', r.requested_at = timestamp(), r.requested_by = $by, r.channel = 'internal',
                 r.action_id = $a
             MERGE (a)-[:CREATED]->(r)
             WITH r MATCH (w:Work {key:$w}) MERGE (r)-[:FOR_WORK]->(w)
             WITH r, w OPTIONAL MATCH (w)-[:FOR_CLIENT]->(c:Client) FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END |
                 MERGE (r)-[:ADDRESSED_TO]->(c))""",
          {"id": rid, "by": by, "w": p.get("work"), "a": a["id"]})
    return {"request_id": rid, "channel": "internal",
            "summary": f"request {rid} recorded in the internal queue (no email integration: nothing was sent)"}


def _exec_publish(g: Graph, a: dict, by: str) -> dict:
    from bidbrain.company import record
    p = json.loads(a.get("payload") or "{}")
    return record.publish(g, p["decision_id"], by)


EXECUTORS = {"review_bid": _exec_review_bid, "request_reference_letter": _exec_request_letter,
             "renew_credential": _exec_renewal,
             "publish_to_company_record": _exec_publish}


# ---- reads ---------------------------------------------------------------------------------------

def get(g: Graph, aid: str) -> dict:
    rows = g.read("""MATCH (a:Action {id:$a})
                     OPTIONAL MATCH (a)-[:TRIGGERED_BY]->(t)
                     OPTIONAL MATCH (a)-[:ASSIGNED_TO]->(p:Person)
                     OPTIONAL MATCH (a)-[:CITES]->(d:Document)
                     RETURN properties(a) AS a, labels(t)[0] AS tl, t.id AS tid, p.key AS pk, p.name AS pn,
                            collect(DISTINCT d.doc_id) AS docs""", {"a": aid})
    if not rows:
        raise ActionError(f"no action {aid}", 404)
    r = rows[0]
    ev = g.read("""MATCH (:Action {id:$a})-[:HAS_EVENT]->(e:ActionEvent)
                   RETURN e.at AS at, e.from AS from, e.to AS to, e.by AS by, e.principal AS principal, e.note AS note
                   ORDER BY e.at""",
                {"a": aid})
    out = {k: v for k, v in r["a"].items() if k not in ("idem", "key")}
    out.update(trigger={"label": r["tl"], "id": r["tid"]} if r["tid"] else None,
               assignee={"key": r["pk"], "name": r["pn"]} if r["pk"] else None, cites=r["docs"], events=ev)
    for k in ("result", "closure"):
        if out.get(k):
            out[k] = json.loads(out[k])
    return out


def list_actions(g: Graph, status: str | None = None, case_id: str | None = None) -> list[dict]:
    rows = g.read("""MATCH (a:Action) WHERE ($s IS NULL OR a.status = $s) AND ($c IS NULL OR a.case_id = $c)
                     OPTIONAL MATCH (a)-[:ASSIGNED_TO]->(p:Person)
                     RETURN a.id AS id, a.kind AS kind, a.status AS status, a.title AS title, a.owner AS owner,
                            p.name AS assignee, a.case_id AS case_id, a.created_at AS created_at,
                            a.updated_at AS updated_at
                     ORDER BY CASE a.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 WHEN 'executing' THEN 1
                              ELSE 2 END, a.created_at DESC""",
                  {"s": status, "c": case_id})
    return rows
