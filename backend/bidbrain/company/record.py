"""The company record: decided facts that opted-in workspaces start from.

A workspace (session graph) is a private GRAPH.COPY: what one estimator ingests and decides stays in their copy.
When a decided fact should become company truth (a corrigendum the bid manager accepted), a
`publish_to_company_record` action, once approved by a second party and executed, copies exactly that fact into
the shared `bidbrain_org` graph:

    (:OrgFact {id, subject_label, subject_key, predicate, value, claim (json), document (json), ruled (json),
               source_decision, policy, rationale, source_session (hash), published_by, approved_by,
               published_at, status, demo})

Nothing else crosses: no conversation turns, no cases, no other claims.

Governance
* **Opt-in.** Facts apply only in workspaces that turned on `use_company_memory` (the same per-session switch as
  company rules, off by default). A fresh workspace never applies a fact, so it always reproduces the scripted
  demo (DEMO-0 / DEMO-1 -> BID -> NO-BID), whatever other visitors published. Facts whose winning claim came
  from a planted demo event are allowed but flagged `demo: true`; like every fact they apply only where the
  visitor opted in. Planted evaluation claims are never published.
* **Four eyes.** The session that queued a publish cannot approve it alone. Its approve attempt files a
  (:PublishRequest) in the company graph; a *different* session approves it there
  (POST /api/company/record/requests/{id}/approve), or an operator approves the action with ORG_ADMIN_TOKEN.
  The OrgFact records both `published_by` (the executing session) and `approved_by` (the other party).
* **Not a trump card.** An applied fact is a claim like any other in the conflict engine
  (`engine._propose_with_record`): it settles only what its source decision already weighed; newer or stronger
  evidence can beat it or open a card. Any session can *contest* a fact it applied: the decisions it made there
  reopen as resolve cards and it stops applying there.
* **Retire propagates.** Only the publisher (or an operator) retires a fact. Every workspace that applied it
  notices on its next request (`sync_record_status`): the claim the fact brought is withdrawn, the claims its
  decision set aside come back, the engine decides the fact again without it, and the change ripples to cases.

Applying (`apply`) writes the published claim and its source document, stamped `recorded_at = published_at`,
lets the engine settle it (`company_record`, citing the OrgFact through an APPLIED_RECORD edge to a local
(:CompanyRecordRef)), then runs the same ripple as a live document (blast_radius -> apply_flips -> actions).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import re
import threading

from bidbrain.graph.client import Graph, db

CLAIM_KEYS = ("id", "subject_label", "subject_key", "predicate", "value", "value_str", "source_doc", "source_kind",
              "authority", "precision", "issuer", "page", "valid_from_day", "valid_to_day", "observed_day",
              "extracted_by")
DOC_KEYS = ("doc_id", "doc_type", "title", "pages", "filename", "issuer", "live")
LABELS = ("Work", "Person", "Client", "Credential", "FinancialStatement", "Bond", "Certification")
FACT_ID_RE = re.compile(r"^OF-[0-9a-f]{10}$")
REQ_ID_RE = re.compile(r"^PR-[0-9a-f]{10}$")
MAX_PENDING_REQUESTS = 100            # pending publication requests kept in the company graph (oldest dropped)
MAX_DECIDED_REQUESTS = 200
MAX_CONTESTS = 100                    # contests recorded per fact (one per session)

# set (and reset) by the API around an action approval made with a valid ORG_ADMIN_TOKEN
approving_admin: contextvars.ContextVar[bool] = contextvars.ContextVar("approving_admin", default=False)
_catch_up_locks: dict[str, threading.Lock] = {}


def _org_name() -> str:
    from bidbrain.conflicts.engine import ORG_GRAPH
    return ORG_GRAPH


def _org(create: bool = False) -> Graph | None:
    name = _org_name()
    if name in db().list_graphs():
        return Graph(name)
    if not create:
        return None
    g = Graph(name)
    for label, prop in (("OrgFact", "id"), ("OrgFact", "subject_key"), ("PublishRequest", "id")):
        try:                            # first creation of the company graph: index its lookups
            g.run(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception:
            pass                        # already indexed
    return g


def _err(msg: str, status: int):
    from bidbrain.conflicts.engine import OrgMemoryError
    return OrgMemoryError(msg, status)


def _tag(g: Graph) -> str:
    from bidbrain.conflicts.engine import session_tag
    return session_tag(g.name)


def _lock():
    from bidbrain.conflicts.engine import _ORG_LOCK
    return _ORG_LOCK


# ------------------------------------------------------------------ publication requests (four eyes)

def _req_id(tag: str, decision_id: str) -> str:
    return "PR-" + hashlib.sha1(f"{tag}|{decision_id}".encode()).hexdigest()[:10]


def _request(rid: str) -> dict | None:
    org = _org()
    rows = org.read("MATCH (r:PublishRequest {id:$id}) RETURN r {.*} AS r", {"id": rid}) if org else []
    return rows[0]["r"] if rows else None


def _snapshot(g: Graph, decision_id: str) -> dict:
    rows = g.read("""MATCH (d:Decision {id:$d}) OPTIONAL MATCH (d)-[:SELECTS]->(c:Claim)
                     OPTIONAL MATCH (c)-[:ABOUT]->(s)
                     RETURN d.subject_key AS subject_key, d.predicate AS predicate, d.chosen_value AS value,
                            d.policy AS policy, d.rationale AS rationale, c.source_doc AS source_doc,
                            c.source_kind AS source_kind, c.planted AS planted,
                            coalesce(s.title, s.name, s.key) AS subject""", {"d": decision_id})
    if not rows:
        raise ValueError(f"no decision {decision_id} in this workspace")
    return rows[0]


def request_publication(g: Graph, action_id: str, decision_id: str) -> dict:
    """File (idempotently) a request for another party to approve publishing this decision."""
    snap = _snapshot(g, decision_id)
    tag, rid = _tag(g), _req_id(_tag(g), decision_id)
    with _lock():
        org = _org(create=True)
        org.run("""MERGE (r:PublishRequest {id:$id})
                   ON CREATE SET r.key = $id, r.status = 'pending', r.requested_at = timestamp(),
                                 r.source_session = $tag, r.decision_id = $d, r.action_id = $a,
                                 r.subject_key = $sk, r.subject = $subj, r.predicate = $p, r.value = $v,
                                 r.policy = $pol, r.rationale = $why, r.source_doc = $doc, r.demo = $demo""",
                {"id": rid, "tag": tag, "d": decision_id, "a": action_id, "sk": snap["subject_key"],
                 "subj": snap["subject"], "p": snap["predicate"], "v": snap["value"], "pol": snap["policy"],
                 "why": snap["rationale"], "doc": snap["source_doc"], "demo": snap["planted"] == "demo"})
        org.run("""MATCH (r:PublishRequest {status:'pending'}) WITH r ORDER BY r.requested_at DESC SKIP $keep
                   DELETE r""", {"keep": MAX_PENDING_REQUESTS})
        org.run("""MATCH (r:PublishRequest) WHERE r.status <> 'pending'
                   WITH r ORDER BY coalesce(r.decided_at, r.requested_at) DESC SKIP $keep DELETE r""",
                {"keep": MAX_DECIDED_REQUESTS})
    return _request(rid) or {"id": rid, "status": "pending"}


def check_publish_approval(g: Graph, action_id: str, by: str) -> None:
    """Called by actions.transition before an approve. A publish_to_company_record action is approved only
    with a valid admin token (`approving_admin`) or once a different session approved its PublishRequest.
    Otherwise the request is filed and the approve is refused (403); the action stays pending."""
    from bidbrain.company.actions import ActionError
    rows = g.read("MATCH (a:Action {id:$a}) RETURN a.kind AS kind, a.payload AS payload", {"a": action_id})
    if not rows or rows[0]["kind"] != "publish_to_company_record" or approving_admin.get():
        return
    decision_id = json.loads(rows[0]["payload"] or "{}").get("decision_id")
    tag = _tag(g)
    req = _request(_req_id(tag, decision_id)) if decision_id else None
    if req and req.get("status") == "approved" and req.get("approved_by") not in (None, tag):
        return
    if req and req.get("status") == "rejected":
        raise ActionError(f"publication request {req['id']} was rejected by {req.get('decided_by')}"
                          f"{': ' + req['note'] if req.get('note') else ''}; reject this action", 403)
    req = request_publication(g, action_id, decision_id)
    raise ActionError(f"publishing to the company record needs a second approver: request {req['id']} is now "
                      f"listed in the company record (Memory page) for another workspace to approve, or an operator "
                      f"approves this action with the admin token", 403)


def decide_request(rid: str, verb: str, g: Graph | None, admin: bool = False, user: str | None = None,
                   note: str | None = None) -> dict:
    """approve | reject a publication request, from a session other than the one that filed it (or as admin)."""
    from bidbrain.conflicts.engine import _label
    if not isinstance(rid, str) or not REQ_ID_RE.match(rid):
        raise _err("id must look like PR-<10 hex>", 400)
    if verb not in ("approve", "reject"):
        raise _err(f"unknown verb {verb!r}", 400)
    tag = _tag(g) if g is not None and g.name.startswith("session_") else None
    if not admin and tag is None:
        raise _err("approving a publication needs a session (X-Session header) or the admin token", 400)
    to = "approved" if verb == "approve" else "rejected"
    with _lock():
        req = _request(rid)
        if req is None:
            raise _err(f"no publication request {rid}", 404)
        if req["status"] == to:
            return {**req, "already": True}
        if req["status"] != "pending":
            raise _err(f"{rid} is {req['status']}; it cannot be {to}", 409)
        if not admin and req.get("source_session") == tag:
            raise _err("the workspace that asked to publish this fact cannot approve or reject its own request; "
                       "another workspace (or an operator with the admin token) must", 403)
        who = "admin" if admin else tag
        _org().run("""MATCH (r:PublishRequest {id:$id}) SET r.status = $to, r.decided_by = $by,
                          r.decided_by_label = $lab, r.decided_at = timestamp(), r.note = $note
                      SET r.approved_by = CASE WHEN $to = 'approved' THEN $by ELSE NULL END""",
                   {"id": rid, "to": to, "by": who, "lab": _label(user), "note": (str(note or "")[:200]).strip() or None})
    return {**(_request(rid) or {}), "already": False}


def list_requests(session_graph: str | None = None, include_decided: bool = False) -> list[dict]:
    org = _org()
    if org is None:
        return []
    rows = [r["r"] for r in org.read("""MATCH (r:PublishRequest) WHERE $all OR r.status = 'pending'
                                        RETURN r {.*} AS r ORDER BY r.requested_at DESC LIMIT 200""",
                                     {"all": include_decided})]
    if session_graph:
        from bidbrain.conflicts.engine import session_tag
        tag = session_tag(session_graph)
        for r in rows:
            r["mine"] = r.get("source_session") == tag
    for r in rows:
        r.pop("key", None)
    return rows


# ------------------------------------------------------------------ publish / retire / contest

def _approver(g: Graph, decision_id: str) -> tuple[str, str | None]:
    """Who approved this publication: the other session that approved its request, or 'admin'."""
    tag = _tag(g)
    req = _request(_req_id(tag, decision_id))
    if req and req.get("status") == "approved" and req.get("approved_by") not in (None, tag):
        return req["approved_by"], req["id"]
    by = g.scalar("""MATCH (a:Action {kind:'publish_to_company_record', target:$d})
                     WHERE a.status IN ['approved', 'executing'] AND a.decided_by STARTS WITH 'admin'
                     RETURN a.decided_by LIMIT 1""", {"d": decision_id})
    if by and str(by).startswith("admin"):
        return "admin", None
    raise ValueError("publishing needs a second approver (another workspace, or an operator with the admin token)")


def publish(g: Graph, decision_id: str, by: str) -> dict:
    """Copy one decided fact into the company record. Idempotent per decision."""
    rows = g.read("""MATCH (d:Decision {id:$d})-[:SELECTS]->(c:Claim)
                     OPTIONAL MATCH (c)-[:FROM]->(doc:Document)
                     OPTIONAL MATCH (d)-[:BASED_ON]->(b:Claim)
                     RETURN properties(d) AS d, properties(c) AS c, properties(doc) AS doc,
                            collect(DISTINCT b.id) AS ruled""", {"d": decision_id})
    if not rows:
        raise ValueError(f"no decision {decision_id} in this workspace")
    d, c, doc = rows[0]["d"], rows[0]["c"], rows[0]["doc"] or {}
    if c.get("planted") not in (None, "demo"):        # demo fixtures are flagged; eval plants never leave
        raise ValueError("planted evaluation claims are never published to the company record")
    approver, rid = _approver(g, decision_id)
    fid = "OF-" + hashlib.sha1(f"{c['subject_key']}|{c['predicate']}|{c['id']}".encode()).hexdigest()[:10]
    ruled = sorted(x for x in rows[0]["ruled"] if x and x != c["id"])
    with _lock():
        org = _org(create=True)
        org.run("""MERGE (f:OrgFact {id:$id})
                   ON CREATE SET f.key = $id, f.subject_label = $sl, f.subject_key = $sk, f.predicate = $p, f.value = $v,
                                 f.claim = $claim, f.document = $doc, f.ruled = $ruled, f.source_decision = $d,
                                 f.policy = $policy, f.rationale = $why, f.source_session = $sess,
                                 f.published_by = $by, f.approved_by = $appr, f.request = $rid,
                                 f.published_at = timestamp(), f.status = 'active', f.demo = $demo""",
                {"id": fid, "sl": c["subject_label"], "sk": c["subject_key"], "p": c["predicate"], "v": str(c["value"]),
                 "claim": json.dumps({k: c.get(k) for k in CLAIM_KEYS}, default=str),
                 "doc": json.dumps({k: doc.get(k) for k in DOC_KEYS}, default=str), "ruled": json.dumps(ruled),
                 "d": decision_id, "policy": d.get("policy"), "why": d.get("rationale"),
                 "sess": _tag(g), "by": by, "appr": approver, "rid": rid, "demo": c.get("planted") == "demo"})
        # an older published value of the same fact is superseded, never deleted (the record keeps its history)
        org.run("""MATCH (old:OrgFact {subject_key:$sk, predicate:$p, status:'active'}) WHERE old.id <> $id
                   SET old.status = 'superseded', old.superseded_by = $id, old.superseded_at = timestamp()""",
                {"sk": c["subject_key"], "p": c["predicate"], "id": fid})
        if rid:
            org.run("MATCH (r:PublishRequest {id:$r}) SET r.status = 'published', r.org_fact = $f", {"r": rid, "f": fid})
    g.run("MATCH (d:Decision {id:$d}) SET d.published_as = $f", {"d": decision_id, "f": fid})
    _mirror()
    _note_applied(g, fid, own=True)    # the publishing workspace already holds this fact (its own decision)
    return {"org_fact": fid, "subject_key": c["subject_key"], "predicate": c["predicate"], "value": c["value"],
            "approved_by": approver,
            "summary": f"published {c['subject_key']} {c['predicate']} = {c['value']} to the company record as {fid} "
                       f"(approved by {approver})"}


def retire(fid: str, session_graph: str | None, admin: bool = False, reason: str = "") -> dict:
    """Withdraw a published fact. Only the publishing workspace (or an operator) may; the fact is kept with status
    'retired' (history is never deleted). Every workspace that applied it withdraws it on its next request
    (`sync_record_status`): the decisions it made there are re-decided without it, and that ripples."""
    from bidbrain.conflicts.engine import session_tag
    org = _org()
    rows = org.read("MATCH (f:OrgFact {id:$id}) RETURN f.source_session AS s, f.status AS st", {"id": fid}) if org else []
    if not rows:
        raise LookupError(f"no published fact {fid}")
    if not admin and (session_graph is None or session_tag(session_graph) != rows[0]["s"]):
        raise PermissionError("only the workspace that published this fact can retire it; contest it instead "
                              f"(POST /api/company/record/{fid}/contest) to stop it applying in your workspace")
    if rows[0]["st"] == "retired":
        return {"retired": fid, "already": True}
    org.run("""MATCH (f:OrgFact {id:$id}) SET f.status = 'retired', f.retired_at = timestamp(), f.retire_reason = $r,
                   f.retired_by = $by""",
            {"id": fid, "r": (reason or "")[:300], "by": "admin" if admin else rows[0]["s"]})
    _mirror()
    return {"retired": fid, "already": False}


def contest(g: Graph, fid: str, reason: str | None = None, user: str | None = None) -> dict:
    """A workspace that applied a published fact disagrees with it: the fact stops counting as the company record
    here (its claim stays, as an ordinary claim with its own authority), the decisions it made here reopen as
    resolve cards, and the contest is recorded on the fact (one per session) for its publisher to see."""
    from bidbrain.conflicts import engine
    from bidbrain.conflicts.engine import _label
    if g is None or not g.name.startswith("session_"):
        raise _err("contesting a published fact needs a session (X-Session header)", 400)
    if not isinstance(fid, str) or not FACT_ID_RE.match(fid):
        raise _err("id must look like OF-<10 hex>", 400)
    tag = _tag(g)
    org = _org()
    rows = org.read("MATCH (f:OrgFact {id:$id}) RETURN f {.*} AS f", {"id": fid}) if org else []
    if not rows:
        raise _err(f"no published fact {fid}", 404)
    f = rows[0]["f"]
    ref = g.read("MATCH (r:CompanyRecordRef {id:$id}) RETURN r {.*} AS r", {"id": fid})
    ref = ref[0]["r"] if ref else None
    if f.get("source_session") == tag or (ref or {}).get("own"):
        raise _err(f"this workspace published {fid}: retire it instead", 409)
    if f.get("status") != "active":
        raise _err(f"{fid} is {f.get('status')}: it no longer applies anywhere new", 409)
    if ref is None:
        raise _err(f"{fid} was never applied in this workspace (company memory is off here)", 409)
    if ref.get("contested"):
        return {"org_fact": fid, "contested": True, "already": True, "contests": f.get("contests") or 0,
                "reopened": [], "flagged": 0}
    why = f"company record {fid} is contested in this workspace: decide this fact yourself"
    with _lock():
        n = org.scalar("MATCH (c:Contest)-[:CONTESTS]->(:OrgFact {id:$id}) RETURN count(c)", {"id": fid}) or 0
        if n < MAX_CONTESTS:
            org.run("""MATCH (f:OrgFact {id:$id})
                       MERGE (c:Contest {key:$key}) ON CREATE SET c.at = timestamp(), c.session = $tag
                       SET c.label = $lab, c.reason = $why
                       MERGE (c)-[:CONTESTS]->(f)""",
                    {"id": fid, "key": f"{fid}:{tag}", "tag": tag, "lab": _label(user),
                     "why": (str(reason or "")[:200]).strip() or None})
        contests = org.scalar("MATCH (c:Contest)-[:CONTESTS]->(:OrgFact {id:$id}) RETURN count(c)", {"id": fid}) or 0
        org.run("MATCH (f:OrgFact {id:$id}) SET f.contests = $n", {"id": fid, "n": contests})
    _mirror()
    g.run("""MATCH (r:CompanyRecordRef {id:$id}) SET r.contested = true, r.contested_at = timestamp(),
                 r.status = 'contested'""", {"id": fid})
    # the claim keeps its value and authority but no longer carries the record's tie-break here
    g.run("""MATCH (c:Claim {company_record:$id}) SET c.company_record_contested = $id, c.company_record = NULL""",
          {"id": fid})
    flagged = g.run("""MATCH (d:Decision)-[:APPLIED_RECORD]->(:CompanyRecordRef {id:$id})
                       SET d.company_record_status = 'contested' RETURN count(d) AS n""", {"id": fid})
    reopened = []
    for h in _heads(g, fid):
        card = engine.reopen_decision(g, h, why)
        if card:
            reopened.append(card)
        else:
            g.run("MATCH (d:Decision {id:$id}) SET d.review = $why", {"id": h, "why": why})
    return {"org_fact": fid, "contested": True, "already": False, "contests": int(contests),
            "flagged": int(flagged[0]["n"]) if flagged else 0, "reopened": reopened}


def _heads(g: Graph, fid: str, subject_key: str | None = None, predicate: str | None = None) -> list[str]:
    """Standing decisions this workspace made by the published fact (not superseded, not reopened)."""
    return [r["id"] for r in g.read("""MATCH (d:Decision)-[:APPLIED_RECORD]->(:CompanyRecordRef {id:$f})
                                        WHERE ($k IS NULL OR d.subject_key = $k) AND ($p IS NULL OR d.predicate = $p)
                                          AND NOT ()-[:SUPERSEDES]->(d) AND coalesce(d.reopened, false) = false
                                        RETURN d.id AS id ORDER BY d.at""",
                                     {"f": fid, "k": subject_key, "p": predicate})]


def _mirror() -> None:
    """Keep the JSON mirror of company memory (rules and facts) current; persistence must not depend on RDB alone."""
    try:
        from bidbrain.conflicts import engine
        engine.export_org(only_if_present=True)
    except Exception:
        pass


def facts(status: str | None = "active") -> list[dict]:
    org = _org()
    if org is None:
        return []
    return org.read("""MATCH (f:OrgFact) WHERE $s IS NULL OR f.status = $s
                       RETURN f.id AS id, f.subject_label AS subject_label, f.subject_key AS subject_key,
                              f.predicate AS predicate, f.value AS value, f.claim AS claim, f.document AS document,
                              f.ruled AS ruled, f.source_decision AS source_decision, f.policy AS policy,
                              f.rationale AS rationale, f.source_session AS source_session,
                              f.published_by AS published_by, f.approved_by AS approved_by, f.request AS request,
                              f.published_at AS published_at, f.status AS status, coalesce(f.demo, false) AS demo,
                              coalesce(f.contests, 0) AS contests, f.superseded_by AS superseded_by,
                              f.retired_at AS retired_at, f.retire_reason AS retire_reason
                       ORDER BY f.published_at""", {"s": status})


def facts_for(g: Graph | None) -> list[dict]:
    """Every published fact, and (with a workspace) what it is here: `here` = own (published here) | active
    (applied, deciding) | contested | retired / unknown (withdrawn here) | superseded | not_applied."""
    out = facts(None)
    if g is None:
        return out
    tag = _tag(g)
    refs = {r["id"]: r for r in g.read("""MATCH (r:CompanyRecordRef)
                                          RETURN r.id AS id, coalesce(r.own, false) AS own,
                                                 coalesce(r.status, 'active') AS status""")}
    for f in out:
        r = refs.get(f["id"])
        f["mine"] = f.get("source_session") == tag
        f["applied_here"] = r is not None
        f["here"] = "own" if (r and r["own"]) or f["mine"] else (r["status"] if r else "not_applied")
        f["contested_here"] = f["here"] == "contested"
    return out


def _note_applied(g: Graph, fid: str, own: bool = False) -> None:
    g.run("""MERGE (r:CompanyRecordRef {id:$id}) ON CREATE SET r.key = $id, r.applied_at = timestamp(),
                 r.status = 'active', r.graph = $graph
             SET r.own = coalesce(r.own, false) OR $own""", {"id": fid, "own": own, "graph": _org_name()})


# ------------------------------------------------------------------ applying, withdrawing, catching up

def apply(g: Graph) -> dict:
    """Bring an opted-in workspace up to date with the company record: write each unseen active fact's claim and
    source document, then let the conflict engine settle it. Returns {applied, decisions, cards, first_claims}
    (the caller ripples them: see `catch_up`)."""
    out: dict = {"applied": [], "decisions": [], "cards": [], "first_claims": []}
    if not g.name.startswith("session_"):
        return out                      # the base graph and the eval graphs stay hermetic
    from bidbrain.conflicts import engine
    if not engine.uses_company_memory(g):
        return out                      # off by default: a fresh workspace reproduces the scripted demo exactly
    seen = {r["id"] for r in g.read("MATCH (r:CompanyRecordRef) RETURN r.id AS id")}
    for f in [f for f in facts() if f["id"] not in seen]:
        c, doc = json.loads(f["claim"]), json.loads(f["document"])
        label = c.get("subject_label")
        if label not in LABELS or not g.read(f"MATCH (s:{label} {{key:$k}}) RETURN s.key", {"k": c["subject_key"]}):
            _note_applied(g, f["id"])
            continue
        if doc.get("doc_id"):
            g.run("""MERGE (d:Document {doc_id:$id}) ON CREATE SET d.key = $id, d.doc_type = $t, d.title = $ti,
                         d.pages = $pg, d.filename = $fn, d.issuer = $iss, d.live = true, d.from_company_record = $f""",
                  {"id": doc["doc_id"], "t": doc.get("doc_type"), "ti": doc.get("title"), "pg": doc.get("pages"),
                   "fn": doc.get("filename"), "iss": doc.get("issuer"), "f": f["id"]})
        _note_applied(g, f["id"])
        ruled = json.loads(f["ruled"]) if f.get("ruled") else None
        rec = {"f": f["id"], "ruled": ruled, "pub": f["published_at"]}
        existing = g.read("MATCH (x:Claim {id:$id}) RETURN x.status AS status", {"id": c["id"]})
        if existing:
            # the published claim is already here (a corpus claim the source decision picked): mark it
            g.run("""MATCH (x:Claim {id:$id}) SET x.company_record = $f, x.record_preexisting = true,
                         x.record_ruled = $ruled, x.record_published_at = $pub""", {"id": c["id"], **rec})
            if existing[0]["status"] in engine.OUT_OF_PLAY:
                engine.reactivate(g, [c["id"]])
        else:
            # transaction time: the company learned this fact when it was published, not when this workspace
            # happened to apply it
            g.run(f"""MATCH (s:{label} {{key:$sk}})
                      CREATE (x:Claim {{id:$id}}) SET x += $props, x.status = 'active', x.company_record = $f,
                             x.record_ruled = $ruled, x.record_published_at = $pub, x.recorded_at = $pub
                      MERGE (x)-[:ABOUT]->(s)
                      WITH x MATCH (d:Document {{doc_id:$doc}}) MERGE (x)-[fr:FROM]->(d) SET fr.page = x.page""",
                  {"sk": c["subject_key"], "id": c["id"], "doc": c.get("source_doc"), **rec,
                   "props": {k: v for k, v in c.items() if v is not None and k != "id"}})
        res = engine.run(g, [c["subject_key"]])
        out["decisions"] += res["decisions"]
        out["cards"] += res["cards"]
        if not existing:
            in_conflict = {x["id"] for d in res["decisions"] + res["cards"] for x in d["conflict"]["claims"]}
            out["first_claims"] += engine.adopt_uncontested(g, [c["id"]], in_conflict)
        out["applied"].append({"org_fact": f["id"], "subject_key": c["subject_key"], "predicate": c["predicate"],
                               "value": c.get("value")})
    return out


_WHY = {"retired": "company record {id} was retired by its publisher: this fact was decided again without it",
        "unknown": "company record {id} is no longer in the company record: this fact was decided again without it"}


def withdraw(g: Graph, fid: str, why: str) -> dict:
    """Take a published fact out of this workspace: its claim loses the record marker (and, if the record brought
    it, is withdrawn: status 'withdrawn', out of every open card); a decision this workspace made by the fact is
    flagged for review and the claims it set aside come back; the engine then decides the fact again without it.
    A decision this workspace made itself (a person, or newer evidence) that selected the claim stands."""
    from bidbrain.conflicts import engine
    out: dict = {"withdrawn": [], "decisions": [], "cards": []}
    rows = g.read("""MATCH (c:Claim)-[:ABOUT]->(s) WHERE c.company_record = $f OR c.company_record_contested = $f
                     RETURN c {.*} AS c, labels(s)[0] AS label""", {"f": fid})
    for row in rows:
        c = row["c"]
        heads = _heads(g, fid, c["subject_key"], c["predicate"])
        own = g.scalar("""MATCH (d:Decision)-[:SELECTS]->(:Claim {id:$c}) WHERE NOT ()-[:SUPERSEDES]->(d)
                            AND coalesce(d.reopened, false) = false AND d.company_record IS NULL RETURN count(d)""",
                       {"c": c["id"]}) or 0
        in_card = g.scalar("MATCH (k:ConflictCard {status:'open'})-[:ABOUT_CLAIM]->(:Claim {id:$c}) RETURN count(k)",
                           {"c": c["id"]}) or 0
        g.run("""MATCH (c:Claim {id:$c}) SET c.company_record = NULL, c.company_record_contested = NULL,
                     c.company_record_withdrawn = $f""", {"c": c["id"], "f": fid})
        if own and not heads:
            continue
        if heads:
            g.run("MATCH (d:Decision) WHERE d.id IN $ids SET d.review = $why, d.company_record_status = 'retired'",
                  {"ids": heads, "why": why})
            engine.reactivate(g, [r["id"] for r in g.read(
                "MATCH (l:Claim) WHERE l.invalidated_by IN $ids AND l.id <> $c RETURN l.id AS id",
                {"ids": heads, "c": c["id"]})])
        if not c.get("record_preexisting"):
            g.run("""MATCH (c:Claim {id:$c}) SET c.status = 'withdrawn', c.invalidated_at = timestamp(),
                         c.invalidated_by = $by
                     WITH c OPTIONAL MATCH (k:ConflictCard {status:'open'})-[e:ABOUT_CLAIM]->(c) DELETE e""",
                  {"c": c["id"], "by": f"company-record:{fid}"})
        out["withdrawn"].append(c["id"])
        if heads or in_card:
            res = engine.resettle(g, row["label"], c["subject_key"], c["predicate"], why)
            out["decisions"] += res["decisions"]
            out["cards"] += res["cards"]
    if out["decisions"] or out["cards"] or out["withdrawn"]:
        g.run("MATCH (m:Meta {key:'meta'}) SET m.version = m.version + 1")
    return out


def sync_record_status(g: Graph) -> dict:
    """Bring this workspace's view of the facts it applied up to date (`company_record_status`): each
    CompanyRecordRef, and every Decision made by it, becomes active | contested | superseded | retired | unknown.
    A fact that is now retired (or gone from the company record) is withdrawn here (`withdraw`). The publishing
    workspace's own facts are skipped (its decision is its own). Cheap when nothing changed."""
    out: dict = {"changed": {}, "withdrawn": [], "decisions": [], "cards": []}
    if not g.name.startswith("session_"):
        return out
    refs = g.read("""MATCH (r:CompanyRecordRef) WHERE coalesce(r.own, false) = false
                     RETURN r.id AS id, coalesce(r.status, 'active') AS status, coalesce(r.contested, false) AS contested""")
    if not refs:
        return out
    org = _org()
    live = {}
    if org is not None:
        live = {r["id"]: r["status"] for r in org.read("MATCH (f:OrgFact) WHERE f.id IN $ids RETURN f.id AS id, "
                                                        "f.status AS status", {"ids": [r["id"] for r in refs]})}
    for r in refs:
        st = live.get(r["id"], "unknown")
        if st == "active" and r["contested"]:
            st = "contested"
        if st == r["status"]:
            continue
        out["changed"][r["id"]] = st
        g.run("MATCH (x:CompanyRecordRef {id:$id}) SET x.status = $s", {"id": r["id"], "s": st})
        g.run("MATCH (d:Decision)-[:APPLIED_RECORD]->(:CompanyRecordRef {id:$id}) SET d.company_record_status = $s",
              {"id": r["id"], "s": st})
        if st in _WHY and r["status"] not in _WHY:
            res = withdraw(g, r["id"], _WHY[st].format(id=r["id"]))
            for k in ("withdrawn", "decisions", "cards"):
                out[k] += res[k]
    return out


def ripple(g: Graph, out: dict, cause: str, emit=None, publish: bool = True) -> dict:
    """The live-document ripple: blast_radius from every subject a decision, card or first claim touched ->
    apply_flips (cases, verdicts, review actions); with `publish`, a publish action per decided fact."""
    from bidbrain.company import actions
    from bidbrain.tools.case import apply_flips, blast_radius
    emit = emit or (lambda ev: None)
    subjects = {(c["conflict"]["subject_label"], c["conflict"]["subject_key"])
                for c in out.get("decisions", []) + out.get("cards", [])}
    # a first-time fact (no rival claim) is written to its subject too, so it can flip clauses as well
    subjects |= {(c["conflict"]["subject_label"], c["conflict"]["subject_key"]) for c in out.get("first_claims", [])}
    dec_of = {d["conflict"]["subject_key"]: d.get("decision_id") for d in out.get("decisions", [])}
    cause_doc = (out.get("doc") or {}).get("doc_id")
    ripples, acts = [], []
    for label, key in sorted(subjects):
        br = blast_radius(g, label, key)
        changed = apply_flips(g, br["result"]["verdict_flips"], cause, br["result"].get("evidence_changed"),
                              decision_id=dec_of.get(key), cause_doc=cause_doc)
        br["result"]["cases_changed"] = changed
        for c in changed:               # the review action, plus any letter / renewal follow-ups it raised
            acts += ([c["action"]] if c.get("action") else []) + list(c.get("follow_ups") or [])
        ripples.append(br["result"])
        emit({"type": "ripple", "subject": f"{label}:{key}", "result": br["result"], "subgraph": br["subgraph"]})
    # every decided fact from a live document can be published to the company record (behind approval)
    if publish:
        for d in out.get("decisions", []):
            if d.get("decision_id"):
                a = actions.for_decision(g, d["decision_id"])
                if a:
                    acts.append(a["id"])
    if acts:
        emit({"type": "actions", "actions": acts, "summary": f"{len(acts)} follow-up action(s) queued for approval"})
    return {"ripples": ripples, "actions": acts}


def catch_up(g: Graph) -> dict:
    """Run on every request of a workspace (sessions.graph_for) and when it opts in: withdraw facts retired
    since, apply facts published since (only if opted in), and ripple whatever that changed to cases and
    actions. One catch-up per workspace at a time."""
    empty = {"applied": [], "withdrawn": [], "changed": {}, "decisions": [], "cards": [], "ripples": [], "actions": []}
    if not g.name.startswith("session_"):
        return empty
    lock = _catch_up_locks.setdefault(g.name, threading.Lock())
    with lock:
        s = sync_record_status(g)
        a = apply(g)
        out = {"applied": a["applied"], "withdrawn": s["withdrawn"], "changed": s["changed"],
               "decisions": s["decisions"] + a["decisions"], "cards": s["cards"] + a["cards"],
               "first_claims": a["first_claims"]}
        if out["decisions"] or out["cards"] or out["first_claims"]:
            ids = sorted({x["org_fact"] for x in a["applied"]} | set(s["changed"]))
            out.update(ripple(g, out, f"company record {', '.join(ids)}", publish=False))
        else:
            out.update(ripples=[], actions=[])
    return out
