"""The company record: decided facts that every new workspace starts from.

A workspace (session graph) is a private GRAPH.COPY: what one estimator ingests and decides stays in their copy.
When a decided fact should become company truth (a corrigendum the bid manager accepted), a
`publish_to_company_record` action, once approved and executed, copies exactly that fact into the shared
`bidbrain_org` graph:

    (:OrgFact {id, subject_label, subject_key, predicate, value, claim (json), document (json),
               source_decision, policy, rationale, source_session (hash), approved_by, published_at, status})

Nothing else crosses: no conversation turns, no cases, no other claims. Every workspace applies the active
facts it has not seen yet (`apply`), through the conflict engine: the published claim and its source document
are written into the workspace, the engine detects the disagreement with the corpus claim, and settles it with
policy `company_record`, citing the OrgFact and the decision it came from. Point-in-time history is kept: the
superseded corpus claim stays (status 'superseded', with `invalidated_at`), and a newer OrgFact for the same fact
supersedes the older one rather than overwriting it.
"""
from __future__ import annotations

import hashlib
import json

from bidbrain.graph.client import Graph, db

CLAIM_KEYS = ("id", "subject_label", "subject_key", "predicate", "value", "value_str", "source_doc", "source_kind",
              "authority", "precision", "issuer", "page", "valid_from_day", "valid_to_day", "observed_day",
              "extracted_by")
DOC_KEYS = ("doc_id", "doc_type", "title", "pages", "filename", "issuer", "live")


def _org_name() -> str:
    from bidbrain.conflicts.engine import ORG_GRAPH
    return ORG_GRAPH


def _org(create: bool = False) -> Graph | None:
    name = _org_name()
    if create or name in db().list_graphs():
        return Graph(name)
    return None


def publish(g: Graph, decision_id: str, by: str) -> dict:
    """Copy one decided fact into the company record. Idempotent per decision."""
    from bidbrain.conflicts.engine import session_tag
    rows = g.read("""MATCH (d:Decision {id:$d})-[:SELECTS]->(c:Claim)
                     OPTIONAL MATCH (c)-[:FROM]->(doc:Document)
                     RETURN properties(d) AS d, properties(c) AS c, properties(doc) AS doc""", {"d": decision_id})
    if not rows:
        raise ValueError(f"no decision {decision_id} in this workspace")
    d, c, doc = rows[0]["d"], rows[0]["c"], rows[0]["doc"] or {}
    if c.get("planted") not in (None, "demo"):        # demo fixtures are documented; eval plants never leave
        raise ValueError("planted evaluation claims are never published to the company record")
    fid = "OF-" + hashlib.sha1(f"{c['subject_key']}|{c['predicate']}|{c['id']}".encode()).hexdigest()[:10]
    org = _org(create=True)
    org.run("""MERGE (f:OrgFact {id:$id})
               ON CREATE SET f.key = $id, f.subject_label = $sl, f.subject_key = $sk, f.predicate = $p, f.value = $v,
                             f.claim = $claim, f.document = $doc, f.source_decision = $d, f.policy = $policy,
                             f.rationale = $why, f.source_session = $sess, f.approved_by = $by,
                             f.published_at = timestamp(), f.status = 'active'""",
            {"id": fid, "sl": c["subject_label"], "sk": c["subject_key"], "p": c["predicate"], "v": str(c["value"]),
             "claim": json.dumps({k: c.get(k) for k in CLAIM_KEYS}, default=str),
             "doc": json.dumps({k: doc.get(k) for k in DOC_KEYS}, default=str),
             "d": decision_id, "policy": d.get("policy"), "why": d.get("rationale"),
             "sess": session_tag(g.name), "by": by})
    # an older published value of the same fact is superseded, never deleted (the record keeps its history)
    org.run("""MATCH (old:OrgFact {subject_key:$sk, predicate:$p, status:'active'}) WHERE old.id <> $id
               SET old.status = 'superseded', old.superseded_by = $id, old.superseded_at = timestamp()""",
            {"sk": c["subject_key"], "p": c["predicate"], "id": fid})
    g.run("MATCH (d:Decision {id:$d}) SET d.published_as = $f", {"d": decision_id, "f": fid})
    _note_applied(g, fid)             # the publishing workspace already holds this fact
    return {"org_fact": fid, "subject_key": c["subject_key"], "predicate": c["predicate"], "value": c["value"],
            "summary": f"published {c['subject_key']} {c['predicate']} = {c['value']} to the company record as {fid}"}


def retire(fid: str, session_graph: str | None, admin: bool = False, reason: str = "") -> dict:
    """Withdraw a published fact. Only the publishing workspace (or an operator) may; the fact is kept with status
    'retired' (history is never deleted), and workspaces stop applying it. Workspaces that already applied it keep
    their decision, which now cites a retired record (`company_record_status`)."""
    from bidbrain.conflicts.engine import session_tag
    org = _org()
    rows = org.read("MATCH (f:OrgFact {id:$id}) RETURN f.source_session AS s, f.status AS st", {"id": fid}) if org else []
    if not rows:
        raise LookupError(f"no published fact {fid}")
    if not admin and (session_graph is None or session_tag(session_graph) != rows[0]["s"]):
        raise PermissionError("only the workspace that published this fact can retire it")
    if rows[0]["st"] == "retired":
        return {"retired": fid, "already": True}
    org.run("""MATCH (f:OrgFact {id:$id}) SET f.status = 'retired', f.retired_at = timestamp(), f.retire_reason = $r""",
            {"id": fid, "r": reason[:300]})
    return {"retired": fid, "already": False}


def facts(status: str | None = "active") -> list[dict]:
    org = _org()
    if org is None:
        return []
    return org.read("""MATCH (f:OrgFact) WHERE $s IS NULL OR f.status = $s
                       RETURN f.id AS id, f.subject_label AS subject_label, f.subject_key AS subject_key,
                              f.predicate AS predicate, f.value AS value, f.claim AS claim, f.document AS document,
                              f.source_decision AS source_decision, f.policy AS policy, f.rationale AS rationale,
                              f.source_session AS source_session, f.approved_by AS approved_by,
                              f.published_at AS published_at, f.status AS status
                       ORDER BY f.published_at""", {"s": status})


def _note_applied(g: Graph, fid: str) -> None:
    g.run("MERGE (r:CompanyRecordRef {id:$id}) SET r.key = $id, r.applied_at = timestamp()", {"id": fid})


def apply(g: Graph) -> list[dict]:
    """Bring a workspace up to date with the company record: write each unseen active fact's claim and source
    document, then let the conflict engine settle it (policy `company_record`). Returns the decisions made."""
    if not g.name.startswith("session_"):
        return []                       # the base graph and the eval graphs stay hermetic
    pending = [f for f in facts() if not g.read("MATCH (r:CompanyRecordRef {id:$id}) RETURN r.id", {"id": f["id"]})]
    if not pending:
        return []
    from bidbrain.conflicts import engine
    out = []
    for f in pending:
        c, doc = json.loads(f["claim"]), json.loads(f["document"])
        if doc.get("doc_id"):
            g.run("""MERGE (d:Document {doc_id:$id}) ON CREATE SET d.key = $id, d.doc_type = $t, d.title = $ti,
                         d.pages = $pg, d.filename = $fn, d.live = true, d.from_company_record = $f""",
                  {"id": doc["doc_id"], "t": doc.get("doc_type"), "ti": doc.get("title"), "pg": doc.get("pages"),
                   "fn": doc.get("filename"), "f": f["id"]})
        label = c["subject_label"]
        if label not in ("Work", "Person", "Client", "Credential", "FinancialStatement", "Bond", "Certification"):
            continue
        g.run(f"""MATCH (s:{label} {{key:$sk}})
                  MERGE (c:Claim {{id:$id}})
                  ON CREATE SET c += $props, c.status = 'active', c.company_record = $f, c.recorded_at = timestamp()
                  MERGE (c)-[:ABOUT]->(s)
                  WITH c MATCH (d:Document {{doc_id:$doc}}) MERGE (c)-[:FROM]->(d)""",
              {"sk": c["subject_key"], "id": c["id"], "f": f["id"], "doc": c.get("source_doc"),
               "props": {k: v for k, v in c.items() if v is not None and k != "id"}})
        _note_applied(g, f["id"])
        res = engine.run(g, [c["subject_key"]])
        out.append({"org_fact": f["id"], "subject_key": c["subject_key"], "predicate": c["predicate"],
                    "engine": {k: res.get(k) for k in ("decided", "cards") if k in res}})
    return out
