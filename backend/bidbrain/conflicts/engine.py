"""Conflict detection and resolution over the claim layer.

    detect   two active claims about the same (subject, predicate) disagree beyond
             the coarser source's precision  ->  (a)-[:CONTRADICTS]->(b)
    resolve  claims are grouped by the value they state (within precision); in order:
               0. a claim carrying a published company-record fact (opted-in sessions only) settles
                  the claims its source decision already weighed (policy company_record); newer
                  evidence is weighed against it on steps 1-4, and the record only breaks a tie
                  against equal-or-lower authority (see _propose_with_record)
               1. a learned PolicyRule whose two kinds really disagree here, with no
                  third source in play                (procedural memory)
               2. same issuer, later document         (recency: corrigenda, re-issues)
               3. the strongest claim of the leading value outranks the strongest
                  claim of any rival value by >= 20   (the ladder in ingest/claims.py)
               otherwise -> a resolve card for a human (one open card per fact: a new
               claim on a contested fact is folded into the card already open)
    record   every outcome is a (:Decision) with SELECTS / BASED_ON / DECIDED_BY; only
             claims that disagree with the winner lose (agreeing ones CORROBORATES it),
             other open cards on the fact are closed, and the subject's property is
             re-materialized from the winner.

A human resolution can be generalised ("apply to similar"): it becomes a
(:PolicyRule) that the next conflict of the same shape is resolved by, citing
the decision it was learned from. That is the system getting better over time.

Organisational memory. A session's rules die with the session. A bid manager can
*promote* one into the shared company graph (`bidbrain_org`, never written
implicitly): an (:OrgRule) carrying who promoted it, when, the source session (as
a hash, never the session id) and a copy of the decision it was LEARNED_FROM with
its rationale. Every session then consults company rules after its own rules and
before recency and authority (policy `org_rule`), and the Decision cites the
promoted rule. Retiring a company rule takes it out of play for everyone.

Governance. Company rules apply only in sessions that opted in (`use_company_memory`, off by
default), so one visitor's promoted rule never changes another visitor's scripted demo. Provenance
is the promoting session's tag, not a name the caller types (a display name is kept as an unverified
label). Only the promoting session retires a rule; any other session can *contest* it, which takes the
rule out of play in that session only and reopens the decisions it made there as resolve cards. A
promote, retire or contest re-checks the acting session: open cards of the rule's shape are re-run
through the engine, and decisions made by a rule that is now retired or contested are reopened.
A company rule whose promoting session is gone is retired automatically after ORG_ORPHAN_TTL.

Transaction time. A claim set aside by a decision gets `invalidated_at` / `invalidated_by`; when a later
decision, a reopened card or the company record puts it back in play, both are cleared and the set-aside
interval is appended to `status_history`.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time

from bidbrain.graph.client import Graph, day, db

AUTHORITY_GAP = 20

# predicate -> how to re-materialize the subject property from the winning claim
PROJECT = {
    "value": ["value"], "grading": ["grading"], "role": ["role"], "status": ["status"],
    "completed": ["completed", "completed_day", "completed_year"],
    "expiry": ["expiry", "expiry_day"], "valid_through": ["valid_through", "valid_through_day"],
    "issued": ["issued", "issued_day"], "experience_years": ["experience_years"],
    "amount": ["amount"], "awarded_value": ["awarded_value"], "billed_value": ["billed_value"],
    "valid_until": ["valid_until", "valid_until_day"],
}


def _id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def _differs(a: dict, b: dict) -> bool:
    va, vb = a["value"], b["value"]
    if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and not isinstance(va, bool):
        tol = max(a.get("precision") or 1, b.get("precision") or 1) / 2
        return abs(va - vb) > tol
    return str(va).strip().lower() != str(vb).strip().lower()


def _rounding_only(a: dict, b: dict) -> bool:
    va, vb = a["value"], b["value"]
    return (isinstance(va, (int, float)) and isinstance(vb, (int, float)) and va != vb
            and not _differs(a, b))


# claims out of play: set aside by a decision, or withdrawn with the company-record fact that brought them
OUT_OF_PLAY = ("superseded", "rejected", "withdrawn")

CLAIMS_FOR = """MATCH (c:Claim)-[:ABOUT]->(s) WHERE NOT c.status IN ['superseded', 'rejected', 'withdrawn']
  AND ($keys IS NULL OR c.subject_key IN $keys)
RETURN c.id AS id, c.subject_label AS subject_label, c.subject_key AS subject_key, c.predicate AS predicate,
       c.value AS value, c.value_str AS value_str, c.source_doc AS source_doc, c.source_kind AS source_kind,
       c.authority AS authority, c.precision AS precision, c.issuer AS issuer, c.observed_day AS observed_day,
       c.page AS page, c.status AS status, c.planted AS planted, c.company_record AS company_record,
       c.record_ruled AS record_ruled, c.record_published_at AS record_published_at, c.recorded_at AS recorded_at
ORDER BY c.subject_key, c.predicate"""

# Reactivating a claim that a decision had set aside: it is in play again, so `invalidated_at` is cleared (a
# point-in-time read must see it as alive now); the interval it spent set aside is kept in `status_history`
# as "<status>:<invalidated_at>-<reactivated_at>:<decision>" strings.
_REACTIVATE = """MATCH (c:Claim) WHERE c.id IN $ids AND c.invalidated_at IS NOT NULL
SET c.status_history = coalesce(c.status_history, []) +
        [coalesce(c.status, '?') + ':' + toString(c.invalidated_at) + '-' + toString(timestamp()) + ':' +
         coalesce(c.invalidated_by, '')],
    c.invalidated_at = NULL, c.invalidated_by = NULL"""


def reactivate(g: Graph, ids: list[str], status: str = "active") -> None:
    """Put claims back in play (status `active` or `contested`), clearing `invalidated_at` with history."""
    if not ids:
        return
    g.run(_REACTIVATE, {"ids": ids})
    g.run("MATCH (c:Claim) WHERE c.id IN $ids SET c.status = $st", {"ids": ids, "st": status})


def detect(g: Graph, subject_keys: list[str] | None = None) -> dict:
    """Find disagreements; write CONTRADICTS edges; return conflict groups + rounding notes."""
    rows = g.read(CLAIMS_FOR, {"keys": subject_keys})
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["subject_label"], r["subject_key"], r["predicate"]), []).append(r)
    conflicts, rounding = [], []
    now = int(time.time())
    for (label, key, pred), cl in groups.items():
        if len(cl) < 2:
            continue
        pairs = [(a, b) for i, a in enumerate(cl) for b in cl[i + 1:]]
        bad = [(a, b) for a, b in pairs if _differs(a, b)]
        rounding += [{"subject_key": key, "predicate": pred, "a": a["id"], "b": b["id"],
                      "values": [a["value"], b["value"]]} for a, b in pairs if _rounding_only(a, b)]
        if not bad:
            continue
        for a, b in bad:
            g.run("""MATCH (a:Claim {id:$a}), (b:Claim {id:$b})
                     WHERE NOT (a)-[:CONTRADICTS]-(b)
                     CREATE (a)-[:CONTRADICTS {rule:'value_mismatch', detected_at:$t}]->(b)""",
                  {"a": a["id"], "b": b["id"], "t": now})
        involved = {c["id"]: c for p in bad for c in p}
        conflicts.append({"id": _id("CF-", label, key, pred, *sorted(involved)),
                          "subject_label": label, "subject_key": key, "predicate": pred,
                          "claims": sorted(involved.values(), key=lambda c: -c["authority"])})
    return {"conflicts": conflicts, "rounding": rounding}


def _rules(g: Graph) -> list[dict]:
    """Active learned rules, oldest first (deterministic: the first matching rule wins)."""
    return g.read("MATCH (r:PolicyRule) WHERE r.retired IS NULL OR r.retired = false "
                  "RETURN r.id AS id, r.predicate AS predicate, r.prefer_kind AS prefer_kind, "
                  "r.over_kind AS over_kind, r.rationale AS rationale ORDER BY r.created_at, r.id")


def _rank_key(c: dict):
    """Strongest first: authority, then the later document, then id (deterministic)."""
    return (-(c.get("authority") or 0), -(c.get("observed_day") or 0), str(c.get("id")))


def _groups(cl: list[dict]) -> list[list[dict]]:
    """Partition claims into groups that state the same value (within precision), strongest first.
    Each group is represented by its strongest claim; a claim joins the first group whose
    representative it does not contradict."""
    groups: list[list[dict]] = []
    for c in sorted(cl, key=_rank_key):
        for grp in groups:
            if not _differs(grp[0], c):
                grp.append(c)
                break
        else:
            groups.append([c])
    return groups


def _issuers(grp: list[dict]) -> str:
    return ", ".join(sorted({c.get("issuer") or "?" for c in grp}))


def _rule_applies(r: dict, cl: list[dict], groups: list[list[dict]]) -> dict | None:
    """A learned rule settles a conflict only if the conflict really is between its two kinds:
    every claim of the preferred kind states one value, at least one claim of the other kind
    disagrees with it, and every disagreeing claim is either of that other kind or loses to the
    preferred claim on authority anyway. Returns the winning claim, else None."""
    pref = [c for c in cl if c["source_kind"] == r["prefer_kind"]]
    if not pref:
        return None
    home = next(grp for grp in groups if pref[0] in grp)
    if any(c not in home for c in pref):
        return None                     # the preferred kind contradicts itself
    win = min(pref, key=_rank_key)
    dissent = [c for c in cl if c not in home]
    if not any(c["source_kind"] == r["over_kind"] for c in dissent):
        return None                     # the two kinds agree: the rule has nothing to decide
    if any(c["source_kind"] != r["over_kind"] and c["authority"] > win["authority"] - AUTHORITY_GAP
           for c in dissent):
        return None                     # a third source the rule never ruled on is in play
    return win


def propose(conflict: dict, rules: list[dict], org_rules: list[dict] | None = None) -> dict:
    """Decide (without writing) how a conflict should be resolved.

    Order: this session's learned rules, then company rules (promoted to `bidbrain_org`), then
    recency (same issuer), then authority; otherwise a human decides. A claim carrying a published
    company-record fact takes part as one more claim (see `_propose_with_record`)."""
    recs = [c for c in conflict["claims"] if c.get("company_record")]
    if recs:
        return _propose_with_record(conflict, rules, org_rules, recs)
    return _ladder(conflict, rules, org_rules)


def _same_issuer(a: dict, b: dict) -> bool:
    return bool(a.get("issuer")) and (a.get("issuer") or "").lower() == (b.get("issuer") or "").lower()


def _propose_with_record(conflict: dict, rules: list[dict], org_rules: list[dict] | None, recs: list[dict]) -> dict:
    """A published company-record fact is one participant with its own authority, not a trump card.

    * Claims the published decision already weighed (`record_ruled`: its BASED_ON claims, which are corpus
      claims with the same ids in every workspace; and any older published value of the same fact) are not
      re-litigated: if they are the only disagreement, the record settles it (policy `company_record`).
    * Evidence the published decision never saw is weighed against the record claim on the ordinary ladder
      (rules, same-issuer recency, authority). A newer same-issuer amendment or a stronger source can win.
    * When the ladder cannot decide, the record breaks the tie, but only against claims of equal or lower
      authority that are not a later statement by the record's own issuer; otherwise a person decides."""
    cl = conflict["claims"]
    rec = max(recs, key=lambda c: (c.get("record_published_at") or 0, str(c["id"])))
    fid = rec["company_record"]
    listed = rec.get("record_ruled")

    def weighed(c: dict) -> bool:
        if c.get("company_record"):                 # an older published value of the same fact
            return True
        if listed is None:                          # a fact published before its basis was recorded:
            return not c.get("recorded_at")         # the corpus claims (recorded_at 0) were its basis
        return c["id"] in listed

    fresh = [c for c in cl if c is not rec and _differs(c, rec) and not weighed(c)]
    origin = f"company record {fid} (published from another workspace; source {rec['source_doc']})"
    if not fresh:
        return {"auto": True, "policy": "company_record", "winner": rec, "record_fact": fid,
                "rationale": f"{origin}: every claim here that disagrees with it was weighed by the decision it "
                             f"was published from"}
    sub = [rec] + [c for c in cl if c is not rec and not c.get("company_record") and not _differs(c, rec)] + fresh
    p = _ladder({**conflict, "claims": sub}, rules, org_rules)
    newer = ", ".join(f"{c['source_kind']} {c['source_doc']} ({c['authority']})" for c in fresh)
    if p["auto"]:
        if _differs(p["winner"], rec):
            return {**p, "rationale": f"newer evidence outweighs {origin}: {p['rationale']}"}
        return {**p, "policy": "company_record", "winner": rec, "record_fact": fid, "rule_id": None,
                "org_rule": None, "rationale": f"{origin} stands against newer evidence {newer}: {p['rationale']}"}
    stronger = [c for c in fresh if (c.get("authority") or 0) > (rec.get("authority") or 0)
                or (_same_issuer(c, rec) and (c.get("observed_day") or 0) > (rec.get("observed_day") or 0))]
    if not stronger:
        return {"auto": True, "policy": "company_record", "winner": rec, "record_fact": fid,
                "rationale": f"{origin} breaks the tie: the newer evidence ({newer}) has equal or lower authority "
                             f"({p['rationale']})"}
    return {**p, "suggested": rec,
            "rationale": f"newer evidence ({newer}) disagrees with {origin} and is not weaker: a person decides "
                         f"({p['rationale']})"}


def _ladder(conflict: dict, rules: list[dict], org_rules: list[dict] | None = None) -> dict:
    """The ordinary policy order: session rules, company rules, same-issuer recency, authority, else human."""
    cl = conflict["claims"]
    groups = _groups(cl)
    for r in rules:
        if r["predicate"] != conflict["predicate"]:
            continue
        win = _rule_applies(r, cl, groups)
        if win is not None:
            return {"auto": True, "policy": "learned_rule", "rule_id": r["id"], "winner": win,
                    "rationale": f"learned rule {r['id']}: prefer {r['prefer_kind']} over {r['over_kind']} "
                                 f"for '{conflict['predicate']}' ({r['rationale']})"}
    for r in org_rules or []:
        if r["predicate"] != conflict["predicate"]:
            continue
        win = _rule_applies(r, cl, groups)
        if win is not None:
            src = r.get("source_decision") or "?"
            return {"auto": True, "policy": "org_rule", "org_rule": r, "winner": win,
                    "rationale": f"company rule {r['id']}: prefer {r['prefer_kind']} over {r['over_kind']} "
                                 f"for '{conflict['predicate']}', promoted by {r.get('promoted_by') or '?'} "
                                 f"from decision {src} ({r['rationale']})"}
    # Recency: an issuer may amend its own statement — but only with a document
    # at least as formal as the one it amends (a corrigendum or re-issued
    # certificate, not a chatty letter), and only if no *other* issuer's claim
    # that disagrees with the amendment outranks it.
    top = min(cl, key=_rank_key)
    same = [c for c in cl if (c["issuer"] or "").lower() == (top["issuer"] or "").lower() and c.get("observed_day")]
    if len(same) >= 2:
        latest = max(same, key=lambda c: (c["observed_day"], c["authority"], str(c["id"])))
        earlier = [c for c in same if c is not latest and _differs(c, latest)]
        others = [c for c in cl if c not in same and _differs(c, latest)]
        if (earlier and latest["observed_day"] > max(c["observed_day"] for c in earlier)
                and latest["authority"] >= max(c["authority"] for c in earlier)
                and all(o["authority"] < latest["authority"] for o in others)):
            return {"auto": True, "policy": "recency_same_issuer", "winner": latest,
                    "rationale": f"{latest['issuer']} amended its own statement in {latest['source_doc']} "
                                 f"({latest['source_kind']}, at least as formal as what it replaces); "
                                 f"the later document supersedes the earlier"}
    # Authority: compare the strongest claim for each *value*, so a second source that agrees
    # with the strongest one corroborates it instead of narrowing the gap.
    best, rival = groups[0], groups[1] if len(groups) > 1 else []
    if not rival:                       # cannot happen for a detected conflict; be safe
        return {"auto": True, "policy": "authority", "winner": best[0],
                "rationale": "all sources agree within precision"}
    a, b = best[0], rival[0]
    if a["authority"] - b["authority"] >= AUTHORITY_GAP:
        corro = f", corroborated by {len(best) - 1} more" if len(best) > 1 else ""
        return {"auto": True, "policy": "authority", "winner": a,
                "rationale": f"{a['source_kind']} (authority {a['authority']}{corro}) outranks "
                             f"{b['source_kind']} ({b['authority']}) by >= {AUTHORITY_GAP}"}
    ia, ib = _issuers(best), _issuers(rival)
    who = (f"from different issuers ({ia} vs {ib})" if ia.lower() != ib.lower()
           else f"both from {ia}, with no formal amendment")
    return {"auto": False, "policy": "needs_human", "winner": None, "suggested": a,
            "rationale": f"sources are close in authority ({a['source_kind']} {a['authority']} vs "
                         f"{b['source_kind']} {b['authority']}) and {who}"}


def _materialize(g: Graph, label: str, key: str, predicate: str, value) -> None:
    props = PROJECT.get(predicate, [predicate])
    sets = {props[0]: value}
    if len(props) > 1 and isinstance(value, str):
        if props[1].endswith("_day"):
            sets[props[1]] = day(value)
        if len(props) > 2 and props[2] == "completed_year":
            sets[props[2]] = int(value[:4])
    g.run(f"MATCH (s:{label} {{key:$k}}) SET s += $p", {"k": key, "p": sets})
    if label == "Work" and predicate == "value":
        # WORKED_FOR / ENGAGED carry sum(w.value): rebuild the projections from the corrected value
        from bidbrain.ingest.load_graph import project
        project(g)


def _decision(g: Graph, did: str, conflict_id: str, subject_key: str, predicate: str, policy: str,
              rationale: str, value, winner_id: str, based_on: list[str], actor: str, actor_kind: str) -> None:
    g.run(f"""MERGE (a:{actor_kind} {{key:$actor}}) ON CREATE SET a.name = $actor
              CREATE (d:Decision {{id:$id, key:$id, conflict_id:$cid, subject_key:$sk, predicate:$pred,
                                   policy:$policy, rationale:$why, at:timestamp(), chosen_value:$val}})
              CREATE (d)-[:DECIDED_BY]->(a)
              WITH d MATCH (w:Claim {{id:$win}}) CREATE (d)-[:SELECTS]->(w)
              WITH d UNWIND $ids AS cid MATCH (c:Claim {{id:cid}}) CREATE (d)-[:BASED_ON]->(c)""",
          {"actor": actor, "id": did, "cid": conflict_id, "sk": subject_key, "pred": predicate,
           "policy": policy, "why": rationale, "val": str(value), "win": winner_id, "ids": based_on})
    # earlier decisions about the same fact are superseded by this one (one head per fact)
    g.run("""MATCH (old:Decision {subject_key:$sk, predicate:$pred}) WHERE old.id <> $d
             AND NOT ()-[:SUPERSEDES]->(old)
             WITH old MATCH (d:Decision {id:$d}) CREATE (d)-[:SUPERSEDES]->(old)""",
          {"sk": subject_key, "pred": predicate, "d": did})


def record(g: Graph, conflict: dict, winner_id: str, policy: str, rationale: str,
           actor: str = "policy-engine", actor_kind: str = "Agent", rule_id: str | None = None,
           keep_card: str | None = None, org_rule: dict | None = None, record_fact: str | None = None) -> dict:
    """Write the Decision, flip claim statuses, re-materialize the property.

    Only claims that *disagree* with the winner lose; claims stating the same value (within
    precision) stay active and are linked CORROBORATES to the winner. Any other open card on
    the same fact is closed as superseded by this decision."""
    did = _id("DEC-", conflict["id"], winner_id, policy, time.time_ns())
    loser_status = "superseded" if policy in ("recency_same_issuer",) else "rejected"
    win = next(c for c in conflict["claims"] if c["id"] == winner_id)
    _decision(g, did, conflict["id"], conflict["subject_key"], conflict["predicate"], policy, rationale,
              win["value"], winner_id, [c["id"] for c in conflict["claims"]], actor, actor_kind)
    if rule_id:
        g.run("MATCH (d:Decision {id:$d}), (r:PolicyRule {id:$r}) CREATE (d)-[:APPLIED_RULE]->(r)",
              {"d": did, "r": rule_id})
    if org_rule:
        # the company rule lives in another graph: cite it with a local reference node (not a
        # PolicyRule, so it never becomes a session rule) and on the decision itself
        g.run("""MERGE (o:OrgRuleRef {id:$id}) SET o.key = $id, o.graph = $graph, o.predicate = $p,
                     o.prefer_kind = $pk, o.over_kind = $ok, o.promoted_by = $by, o.promoted_by_label = $lab,
                     o.source_decision = $src, o.rationale = $why, o.status = 'active'
                 WITH o MATCH (d:Decision {id:$d}) SET d.org_rule = $id, d.org_rule_status = 'active'
                 CREATE (d)-[:APPLIED_ORG_RULE]->(o)""",
              {"id": org_rule["id"], "graph": ORG_GRAPH, "p": org_rule["predicate"],
               "pk": org_rule["prefer_kind"], "ok": org_rule["over_kind"], "by": org_rule.get("promoted_by"),
               "lab": org_rule.get("promoted_by_label"),
               "src": org_rule.get("source_decision"), "why": org_rule.get("rationale"), "d": did})
    if record_fact:
        # the published fact lives in the company graph: cite it through this workspace's reference node
        g.run("""MATCH (d:Decision {id:$d}) SET d.company_record = $f, d.company_record_status = 'active'
                 WITH d MATCH (r:CompanyRecordRef {id:$f}) MERGE (d)-[:APPLIED_RECORD]->(r)""",
              {"d": did, "f": record_fact})
    losers = [c["id"] for c in conflict["claims"] if c["id"] != winner_id and _differs(c, win)]
    agree = [c["id"] for c in conflict["claims"] if c["id"] != winner_id and not _differs(c, win)]
    # transaction time: a set-aside claim keeps its value and records when, and by which decision, it stopped counting
    g.run("""MATCH (c:Claim) WHERE c.id IN $ids SET c.status = $st, c.invalidated_at = timestamp(),
             c.invalidated_by = $d""", {"ids": losers, "st": loser_status, "d": did})
    reactivate(g, agree + [winner_id])           # back in play: invalidated_at cleared, history kept
    g.run("""MATCH (w:Claim {id:$w}) UNWIND $ids AS lid MATCH (l:Claim {id:lid})
             WHERE NOT (w)-[:PREVAILS_OVER]->(l) CREATE (w)-[:PREVAILS_OVER {decision:$d}]->(l)""",
          {"w": winner_id, "ids": losers, "d": did})
    g.run("""MATCH (w:Claim {id:$w}) UNWIND $ids AS aid MATCH (a:Claim {id:aid})
             WHERE NOT (a)-[:CORROBORATES]->(w) CREATE (a)-[:CORROBORATES {decision:$d}]->(w)""",
          {"w": winner_id, "ids": agree, "d": did})
    # every other open card on this fact is now stale: close it, and release any of its claims
    # this conflict did not cover (they disagree with nothing, or they would be in it)
    g.run("""MATCH (k:ConflictCard {subject_key:$sk, predicate:$p, status:'open'}) WHERE $keep IS NULL OR k.id <> $keep
             SET k.status = 'superseded', k.decision = $d, k.superseded_at = timestamp()
             WITH k MATCH (k)-[:ABOUT_CLAIM]->(c:Claim {status:'contested'}) WHERE NOT c.id IN $ids
             SET c.status = 'active'""",
          {"sk": conflict["subject_key"], "p": conflict["predicate"], "keep": keep_card, "d": did,
           "ids": [c["id"] for c in conflict["claims"]]})
    _materialize(g, conflict["subject_label"], conflict["subject_key"], conflict["predicate"], win["value"])
    g.run("MATCH (m:Meta {key:'meta'}) SET m.version = m.version + 1")
    return {"decision_id": did, "policy": policy, "winner": winner_id, "value": win["value"],
            "rationale": rationale, "corroborating": agree, "rejected": losers}


def open_card(g: Graph, conflict: dict, proposal: dict) -> dict:
    """Open a resolve card — or, if one is already open on this fact, fold the new claims into it
    (one fact never has two open cards that a human could resolve inconsistently)."""
    ids = [c["id"] for c in conflict["claims"]]
    reactivate(g, ids, "contested")              # a claim in an open card is in play, not set aside
    existing = g.read("""MATCH (k:ConflictCard {subject_key:$sk, predicate:$p, status:'open'})
                         RETURN k.id AS id ORDER BY k.opened_at, k.id""",
                      {"sk": conflict["subject_key"], "p": conflict["predicate"]})
    card_id = existing[0]["id"] if existing else conflict["id"]
    for extra in existing[1:]:          # legacy duplicates: fold them in too
        g.run("MATCH (k:ConflictCard {id:$id}) SET k.status = 'superseded', k.merged_into = $into",
              {"id": extra["id"], "into": card_id})
    g.run("""MERGE (k:ConflictCard {id:$id}) ON CREATE SET k.opened_at = timestamp()
             SET k.key = $id, k.subject_label = $sl, k.subject_key = $sk, k.predicate = $p, k.status = 'open',
                 k.suggested = $sug, k.rationale = $why, k.conflict_id = $cid, k.updated_at = timestamp()
             WITH k OPTIONAL MATCH (k)-[old:ABOUT_CLAIM]->(c:Claim) WHERE NOT c.id IN $ids DELETE old""",
          {"id": card_id, "sl": conflict["subject_label"], "sk": conflict["subject_key"],
           "p": conflict["predicate"], "sug": proposal["suggested"]["id"], "why": proposal["rationale"],
           "cid": conflict["id"], "ids": ids})
    g.run("MATCH (k:ConflictCard {id:$id}) UNWIND $ids AS cid MATCH (c:Claim {id:cid}) MERGE (k)-[:ABOUT_CLAIM]->(c)",
          {"id": card_id, "ids": ids})
    return {"card_id": card_id, "suggested": proposal["suggested"]["id"], "rationale": proposal["rationale"],
            "merged": bool(existing)}


def run(g: Graph, subject_keys: list[str] | None = None) -> dict:
    """detect -> propose -> record | open_card, for the given subjects (None = everything)."""
    found = detect(g, subject_keys)
    rules = _rules(g)
    orgs = session_org_rules(g) if found["conflicts"] else []
    decisions, cards = [], []
    for cf in found["conflicts"]:
        prop = propose(cf, rules, orgs)
        if prop["auto"]:
            d = record(g, cf, prop["winner"]["id"], prop["policy"], prop["rationale"], rule_id=prop.get("rule_id"),
                       org_rule=prop.get("org_rule"), record_fact=prop.get("record_fact"))
            decisions.append({**d, "conflict": _brief(cf)})
        else:
            cards.append({**open_card(g, cf, prop), "conflict": _brief(cf)})
    return {"decisions": decisions, "cards": cards, "rounding": found["rounding"]}


def adopt_uncontested(g: Graph, claim_ids: list[str], skip: set[str] = frozenset()) -> list[dict]:
    """A new claim on a fact nobody has stated before (no rival claim at all) becomes the fact:
    materialize it on the subject and record a 'first_claim' Decision, so the property is set and
    the caller can ripple it. Claims in `skip` (already part of a conflict) are left alone."""
    rows = g.read("""MATCH (n:Claim)-[:ABOUT]->(s) WHERE n.id IN $ids AND n.status = 'active'
                     OPTIONAL MATCH (o:Claim)-[:ABOUT]->(s)
                     WHERE o.predicate = n.predicate AND o.id <> n.id
                       AND NOT o.status IN ['superseded', 'rejected', 'withdrawn']
                     WITH n, count(o) AS rivals WHERE rivals = 0
                     RETURN n {.*} AS c""", {"ids": [i for i in claim_ids if i not in skip]})
    out = []
    for r in rows:
        c = r["c"]
        cid = _id("CF-", c["subject_label"], c["subject_key"], c["predicate"], c["id"])
        did = _id("DEC-", cid, c["id"], "first_claim", time.time_ns())
        why = f"first statement of '{c['predicate']}' for {c['subject_key']} ({c['source_kind']}, {c['source_doc']})"
        _decision(g, did, cid, c["subject_key"], c["predicate"], "first_claim", why, c["value"], c["id"],
                  [c["id"]], "policy-engine", "Agent")
        _materialize(g, c["subject_label"], c["subject_key"], c["predicate"], c["value"])
        out.append({"decision_id": did, "policy": "first_claim", "winner": c["id"], "value": c["value"],
                    "rationale": why, "conflict": _brief({"id": cid, "subject_label": c["subject_label"],
                                                          "subject_key": c["subject_key"],
                                                          "predicate": c["predicate"], "claims": [c]})})
    if out:
        g.run("MATCH (m:Meta {key:'meta'}) SET m.version = m.version + 1")
    return out


def resettle(g: Graph, subject_label: str, subject_key: str, predicate: str, why: str,
             policy: str = "record_withdrawn") -> dict:
    """Decide a fact again from the claims still in play (after a company-record fact was withdrawn): the
    engine settles a remaining disagreement as usual (decision or card); if nothing disagrees any more, the
    strongest remaining claim is selected by a `policy` decision, which re-materializes the subject and closes
    any open card on the fact. Returns {decisions, cards} in `run`'s format."""
    out = run(g, [subject_key])
    if any(x["conflict"]["predicate"] == predicate for x in out["decisions"] + out["cards"]):
        return {"decisions": out["decisions"], "cards": out["cards"]}
    rows = g.read("""MATCH (c:Claim)-[:ABOUT]->(s) WHERE c.subject_key = $k AND c.predicate = $p
                       AND c.status IN ['active', 'contested'] RETURN c {.*} AS c""",
                  {"k": subject_key, "p": predicate})
    if not rows:
        return {"decisions": out["decisions"], "cards": out["cards"]}
    claims = sorted((r["c"] for r in rows), key=_rank_key)
    cf = {"id": _id("CF-", subject_label, subject_key, predicate, *sorted(c["id"] for c in claims)),
          "subject_label": subject_label, "subject_key": subject_key, "predicate": predicate, "claims": claims}
    d = record(g, cf, claims[0]["id"], policy, why)
    return {"decisions": out["decisions"] + [{**d, "conflict": _brief(cf)}], "cards": out["cards"]}


def _brief(cf: dict) -> dict:
    return {"id": cf["id"], "subject_label": cf["subject_label"], "subject_key": cf["subject_key"],
            "predicate": cf["predicate"],
            "claims": [{k: c.get(k) for k in ("id", "value", "value_str", "source_doc", "source_kind",
                                               "authority", "issuer", "page", "planted")} for c in cf["claims"]]}


def resolve_card(g: Graph, card_id: str, winner_claim: str, rationale: str, user: str = "bid-manager",
                 apply_to_similar: bool = False) -> dict:
    """A human picks the winner on an open card; optionally learn a PolicyRule from it.

    Refuses a card that is no longer open (resolved, or superseded by a later decision or merge)
    and a card whose claims have since been settled — resolving it would resurrect a loser."""
    rows = g.read("""MATCH (k:ConflictCard {id:$id})-[:ABOUT_CLAIM]->(c:Claim)
                     RETURN k.subject_label AS sl, k.subject_key AS sk, k.predicate AS p, k.status AS status,
                            collect(c {.*}) AS claims""",
                  {"id": card_id})
    if not rows:
        raise LookupError(f"no open card {card_id}")
    r = rows[0]
    if r["status"] != "open":
        raise ValueError(f"card {card_id} is {r['status']}, not open")
    settled = [c["id"] for c in r["claims"] if c.get("status") in ("rejected", "superseded")]
    if settled:
        raise ValueError(f"card {card_id} is stale: claims {settled} were settled by a later decision")
    if winner_claim not in {c["id"] for c in r["claims"]}:
        raise ValueError(f"{winner_claim} is not one of card {card_id}'s claims")
    cf = {"id": card_id, "subject_label": r["sl"], "subject_key": r["sk"], "predicate": r["p"],
          "claims": sorted(r["claims"], key=_rank_key)}
    d = record(g, cf, winner_claim, "human", rationale, actor=user, actor_kind="User", keep_card=card_id)
    g.run("MATCH (k:ConflictCard {id:$id}) SET k.status = 'resolved', k.decision = $d", {"id": card_id, "d": d["decision_id"]})
    rules = []
    if apply_to_similar:
        win = next(c for c in cf["claims"] if c["id"] == winner_claim)
        # one rule per kind the human actually ruled against: it disagreed with the winner and is a
        # different kind (a rule preferring a kind over itself would match nearly anything)
        over = sorted({c["source_kind"] for c in cf["claims"]
                       if c["source_kind"] != win["source_kind"] and _differs(c, win)})
        for ok in over:
            rid = _id("RULE-", cf["predicate"], win["source_kind"], ok)
            # the human's latest word wins: retire the opposite preference if one was learned before
            g.run("""MATCH (x:PolicyRule {predicate:$p, prefer_kind:$ok, over_kind:$pk})
                     WHERE x.retired IS NULL OR x.retired = false
                     SET x.retired = true, x.retired_by = $id""",
                  {"p": cf["predicate"], "pk": win["source_kind"], "ok": ok, "id": rid})
            g.run("""MERGE (r:PolicyRule {id:$id}) SET r.key = $id, r.predicate = $p, r.prefer_kind = $pk,
                         r.over_kind = $ok, r.rationale = $why, r.created_by = $u, r.created_at = timestamp(),
                         r.retired = false
                     WITH r MATCH (d:Decision {id:$d}) MERGE (r)-[:LEARNED_FROM]->(d)""",
                  {"id": rid, "p": cf["predicate"], "pk": win["source_kind"], "ok": ok,
                   "why": rationale, "u": user, "d": d["decision_id"]})
            rules.append(rid)
    return {**d, "learned_rule": rules[0] if rules else None, "learned_rules": rules}


# ------------------------------------------------------------------ organisational (company) memory
# A separate graph that opted-in sessions read and only an explicit promote / retire / contest writes.
# Session rules stay private; a rule enters company memory only when a session promotes it.

ORG_GRAPH = os.getenv("ORG_GRAPH", "bidbrain_org")
ORG_MAX_ACTIVE = int(os.getenv("ORG_MAX_RULES", "50"))     # active company rules (a public sandbox)
ORG_MAX_KEPT = 200                                         # retired rules kept for the audit trail
ORG_MAX_CONTESTS = 100                                     # contests recorded per rule (one per session)
# a company rule whose promoting session no longer exists is "orphaned": nobody can retire it but an operator.
# It is retired automatically once it has been orphaned for ORG_ORPHAN_TTL seconds (default 24 h), so a
# burst of throwaway sessions cannot keep the ORG_MAX_ACTIVE slots full; operators also see `orphaned` in
# GET /api/memory/org and can retire any rule with the admin token.
ORG_ORPHAN_TTL = int(os.getenv("ORG_ORPHAN_TTL", str(24 * 3600)))
RULE_ID_RE = re.compile(r"^RULE-[0-9a-f]{10}$")
ORG_ID_RE = re.compile(r"^ORG-[0-9a-f]{10}$")
_ORG_LOCK = threading.RLock()


def _export_path_from_env() -> str | None:
    """Where company memory is mirrored as JSON (survives a lost FalkorDB data dir or a format change).
    ORG_EXPORT_PATH wins; else <BIDBRAIN_STATE_DIR>/org_memory.json; else no export (local dev, tests)."""
    if os.getenv("ORG_EXPORT_PATH"):
        return os.environ["ORG_EXPORT_PATH"]
    if os.getenv("BIDBRAIN_STATE_DIR"):
        return os.path.join(os.environ["BIDBRAIN_STATE_DIR"], "org_memory.json")
    return None


ORG_EXPORT_PATH = _export_path_from_env()


class OrgMemoryError(ValueError):
    """A promote / retire / contest the company graph refuses; `status` is the HTTP code to answer with."""

    def __init__(self, msg: str, status: int = 409):
        super().__init__(msg)
        self.status = status


def _org() -> Graph | None:
    return Graph(ORG_GRAPH) if ORG_GRAPH in db().list_graphs() else None


def _label(user) -> str | None:
    """A display name the caller typed: kept as an unverified label, never as provenance."""
    lab = re.sub(r"[^\w .@-]", "", str(user or ""))[:64].strip()
    return lab or None


def session_tag(session_graph: str) -> str:
    """Provenance names the source session by a hash: a session id is a capability (it opens that graph)."""
    return "s-" + hashlib.sha256(session_graph.encode()).hexdigest()[:12]


def _need_session(g: Graph | None) -> str:
    if g is None or not g.name.startswith("session_"):
        raise OrgMemoryError("company memory changes need a session (X-Session header)", 400)
    return session_tag(g.name)


def org_rules() -> list[dict]:
    """Active company rules, oldest first (the first matching rule wins, as with session rules)."""
    g = _org()
    if g is None:
        return []
    return g.read("""MATCH (r:OrgRule) WHERE r.retired = false
                     OPTIONAL MATCH (r)-[:LEARNED_FROM]->(d:SourceDecision)
                     RETURN r.id AS id, r.predicate AS predicate, r.prefer_kind AS prefer_kind,
                            r.over_kind AS over_kind, r.rationale AS rationale, r.promoted_by AS promoted_by,
                            r.promoted_by_label AS promoted_by_label, d.id AS source_decision
                     ORDER BY r.promoted_at, r.id""")


# ---- per-session opt-in

def uses_company_memory(g: Graph) -> bool:
    """Off by default: a session consults company memory only after it opts in."""
    return bool(g.scalar("MATCH (m:Meta {key:'meta'}) RETURN coalesce(m.use_company_memory, false)"))


def contested_here(g: Graph) -> set[str]:
    return {r["id"] for r in g.read("MATCH (o:OrgRuleRef) WHERE o.contested = true RETURN o.id AS id")}


def session_org_rules(g: Graph) -> list[dict]:
    """The company rules this graph applies: none outside sessions (building the base graph and the
    evals stay hermetic), none in a session that has not opted in, and never one it contested."""
    if not g.name.startswith("session_") or not uses_company_memory(g):
        return []
    skip = contested_here(g)
    return [r for r in org_rules() if r["id"] not in skip]


def set_company_memory(g: Graph, on: bool) -> dict:
    """Turn company memory on or off for this session. Turning it on re-checks open cards, so a card
    a company rule can settle is settled now (and cites the rule)."""
    g.run("MATCH (m:Meta {key:'meta'}) SET m.use_company_memory = $on", {"on": bool(on)})
    out = {"use_company_memory": bool(on), "decisions": [], "cards": []}
    if on:
        out.update(recheck_open_cards(g))
    return out


# ---- re-checking past state

def recheck_open_cards(g: Graph) -> dict:
    """Re-run the engine over the subjects of this session's open cards: a card that a rule now in play
    (a newly learned or promoted one) can settle is auto-resolved; the rest stay open (merged)."""
    keys = [r["k"] for r in g.read("MATCH (k:ConflictCard {status:'open'}) RETURN DISTINCT k.subject_key AS k")]
    if not keys:
        return {"decisions": [], "cards": []}
    out = run(g, keys)
    return {"decisions": out["decisions"], "cards": out["cards"]}


def reopen_decision(g: Graph, decision_id: str, reason: str) -> dict | None:
    """Turn a settled decision back into a resolve card: its claims become contested again, the
    claim-level PREVAILS_OVER / CORROBORATES edges it wrote are removed, and the Decision is kept (marked
    reopened, with the reason) for the audit trail. The subject keeps the old winner's value until a
    person resolves the card."""
    rows = g.read("""MATCH (d:Decision {id:$id})-[:BASED_ON]->(c:Claim)
                     OPTIONAL MATCH (d)-[:SELECTS]->(w:Claim)
                     RETURN d.conflict_id AS cid, d.subject_key AS sk, d.predicate AS p, w.id AS winner,
                            collect(DISTINCT c {.*}) AS claims""", {"id": decision_id})
    claims = [c for c in (rows[0]["claims"] if rows else []) if c.get("status") != "withdrawn"]
    if len(claims) < 2:
        return None
    r = rows[0]
    claims = sorted(claims, key=_rank_key)
    g.run("""MATCH (:Claim)-[e:PREVAILS_OVER]->(:Claim) WHERE e.decision = $d DELETE e""", {"d": decision_id})
    g.run("""MATCH (:Claim)-[e:CORROBORATES]->(:Claim) WHERE e.decision = $d DELETE e""", {"d": decision_id})
    cf = {"id": r["cid"] or _id("CF-", r["sk"], r["p"], decision_id), "subject_label": claims[0]["subject_label"],
          "subject_key": r["sk"], "predicate": r["p"], "claims": claims}
    win = next((c for c in claims if c["id"] == r["winner"]), claims[0])
    card = open_card(g, cf, {"suggested": win, "rationale": reason})
    g.run("""MATCH (d:Decision {id:$id}) SET d.reopened = true, d.review = $why, d.reopened_card = $card,
                 d.reopened_at = timestamp()
             WITH d MATCH (k:ConflictCard {id:$card}) SET k.reopened_from = $id""",
          {"id": decision_id, "why": reason, "card": card["card_id"]})
    g.run("MATCH (m:Meta {key:'meta'}) SET m.version = m.version + 1")
    return {**card, "decision_id": decision_id, "rationale": reason, "conflict": _brief(cf)}


_REVIEW = {"retired": "company rule {id} was retired: review this decision",
           "contested": "company rule {id} is contested in this session: decide this fact yourself",
           "unknown": "company rule {id} is no longer in company memory: review this decision"}


def review_org_rule(g: Graph, org_id: str, status: str, reopen: bool = True) -> dict:
    """Mark every decision this session made by `org_id` with the rule's current status, and reopen
    the ones that still stand (the head decision for their fact) as resolve cards."""
    why = _REVIEW[status].format(id=org_id)
    g.run("MATCH (o:OrgRuleRef {id:$id}) SET o.status = $s", {"id": org_id, "s": status})
    flagged = g.run("""MATCH (d:Decision)-[:APPLIED_ORG_RULE]->(:OrgRuleRef {id:$id})
                       SET d.org_rule_status = $s RETURN count(d) AS n""", {"id": org_id, "s": status})
    heads = g.read("""MATCH (d:Decision)-[:APPLIED_ORG_RULE]->(:OrgRuleRef {id:$id})
                      WHERE NOT ()-[:SUPERSEDES]->(d) AND coalesce(d.reopened, false) = false
                      RETURN d.id AS id ORDER BY d.at""", {"id": org_id})
    reopened = []
    for h in heads:
        if reopen:
            card = reopen_decision(g, h["id"], why)
            if card:
                reopened.append(card)
                continue
        g.run("MATCH (d:Decision {id:$id}) SET d.review = $why", {"id": h["id"], "why": why})
    return {"flagged": int(flagged[0]["n"]) if flagged else 0, "reopened": reopened}


def sync_org_status(g: Graph) -> dict[str, str]:
    """Bring this session's view of the company rules it applied up to date (a rule retired by its
    promoter elsewhere, or pruned): OrgRuleRef.status and Decision.org_rule_status become
    active | retired | contested | unknown, and a still-standing decision by a rule that is no longer
    active is flagged for review (Decision.review). Cheap when nothing changed."""
    refs = g.read("MATCH (o:OrgRuleRef) RETURN o.id AS id, o.status AS status, "
                  "coalesce(o.contested, false) AS contested")
    if not refs:
        return {}
    org = _org()
    live = {}
    if org is not None:
        live = {r["id"]: ("retired" if r["retired"] else "active") for r in org.read(
            "MATCH (o:OrgRule) WHERE o.id IN $ids RETURN o.id AS id, o.retired AS retired",
            {"ids": [r["id"] for r in refs]})}
    out = {}
    for r in refs:
        st = live.get(r["id"], "unknown")
        if st == "active" and r["contested"]:
            st = "contested"
        out[r["id"]] = st
        if st != r["status"]:
            g.run("""MATCH (o:OrgRuleRef {id:$id}) SET o.status = $s
                     WITH o MATCH (d:Decision)-[:APPLIED_ORG_RULE]->(o) SET d.org_rule_status = $s""",
                  {"id": r["id"], "s": st})
            if st in ("retired", "unknown"):
                g.run("""MATCH (d:Decision)-[:APPLIED_ORG_RULE]->(:OrgRuleRef {id:$id})
                         WHERE NOT ()-[:SUPERSEDES]->(d) AND d.review IS NULL SET d.review = $why""",
                      {"id": r["id"], "why": _REVIEW[st].format(id=r["id"])})
    return out


# ---- orphaned rules

def live_session_tags() -> set[str]:
    return {session_tag(n) for n in db().list_graphs() if n.startswith("session_")}


def expire_orphan_rules(now_ms: int | None = None) -> list[str]:
    """Stamp `orphaned_at` on active company rules whose promoting session is gone (clear it if the session is
    back), and retire those orphaned for longer than ORG_ORPHAN_TTL (retired_by 'expired'). Returns the ids
    retired. Run by the session reaper and before every promote."""
    with _ORG_LOCK:
        org = _org()
        if org is None:
            return []
        rows = org.read("""MATCH (o:OrgRule) WHERE o.retired = false
                           RETURN o.id AS id, o.source_session AS s, o.orphaned_at AS t""")
        if not rows:
            return []
        live = live_session_tags()
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        expired = []
        for r in rows:
            if r["s"] in live:
                if r["t"] is not None:
                    org.run("MATCH (o:OrgRule {id:$id}) SET o.orphaned_at = NULL", {"id": r["id"]})
            elif r["t"] is None:
                org.run("MATCH (o:OrgRule {id:$id}) SET o.orphaned_at = $now", {"id": r["id"], "now": now})
            elif now - r["t"] >= ORG_ORPHAN_TTL * 1000:
                org.run("""MATCH (o:OrgRule {id:$id}) SET o.retired = true, o.retired_at = $now,
                               o.retired_by = 'expired', o.retire_reason = 'its promoting session no longer exists'""",
                        {"id": r["id"], "now": now})
                expired.append(r["id"])
        if expired:
            _export_locked()
        return expired


# ---- listing, promote, retire, contest

def list_org(include_retired: bool = False, session_graph: str | None = None) -> list[dict]:
    """Company rules with full provenance, newest first. With a session, each rule says whether that
    session promoted it (`mine`: it may retire it) — the session tag itself is public provenance."""
    g = _org()
    if g is None:
        return []
    rows = g.read("""MATCH (r:OrgRule) WHERE $all OR r.retired = false
                     OPTIONAL MATCH (r)-[:PROMOTED_BY]->(u:User)
                     OPTIONAL MATCH (r)-[:LEARNED_FROM]->(d:SourceDecision)
                     OPTIONAL MATCH (c:Contest)-[:CONTESTS]->(r)
                     WITH r, u, d, count(c) AS contests
                     RETURN r.id AS id, r.predicate AS predicate, r.prefer_kind AS prefer, r.over_kind AS over,
                            r.rationale AS rationale, r.retired AS retired,
                            CASE WHEN r.retired THEN 'retired' ELSE 'active' END AS status,
                            r.promoted_at AS promoted_at, r.retired_at AS retired_at, r.retired_by AS retired_by,
                            r.retired_by_label AS retired_by_label,
                            r.source_rule AS source_rule, r.source_session AS source_session,
                            r.orphaned_at AS orphaned_at, r.retire_reason AS retire_reason,
                            coalesce(r.promoted_by, u.key) AS promoted_by, r.promoted_by_label AS promoted_by_label,
                            contests,
                            d {.id, .rationale, .policy, .decided_by, .subject_key, .subject, .predicate,
                               .chosen_value, .source_doc, .at} AS learned_from
                     ORDER BY promoted_at DESC, id""", {"all": include_retired})
    live = live_session_tags() if rows else set()
    for r in rows:              # orphaned: its promoting session is gone (auto-retired after ORG_ORPHAN_TTL)
        r["orphaned"] = not r.get("retired") and r["source_session"] not in live
    if session_graph:
        tag = session_tag(session_graph)
        for r in rows:
            r["mine"] = r["source_session"] == tag
    return rows


def promote(g: Graph, rule_id: str, user: str | None = None) -> dict:
    """Copy a session's learned PolicyRule into company memory, with provenance, then re-check this
    session's open cards. Explicit only. `promoted_by` is the session's tag; `user` is only a label."""
    tag = _need_session(g)
    if not isinstance(rule_id, str) or not RULE_ID_RE.match(rule_id):
        raise OrgMemoryError("rule_id must look like RULE-<10 hex>", 400)
    label = _label(user)
    rows = g.read("""MATCH (r:PolicyRule {id:$id})
                     OPTIONAL MATCH (r)-[:LEARNED_FROM]->(d:Decision)
                     OPTIONAL MATCH (d)-[:DECIDED_BY]->(a)
                     OPTIONAL MATCH (d)-[:SELECTS]->(w:Claim)
                     OPTIONAL MATCH (w)-[:ABOUT]->(s)
                     RETURN r {.*} AS rule, d {.*} AS decision, a.key AS decided_by, w.source_doc AS source_doc,
                            coalesce(s.title, s.name, s.key) AS subject LIMIT 1""", {"id": rule_id})
    if not rows:
        raise OrgMemoryError(f"no learned rule {rule_id} in this session", 404)
    r, d = rows[0]["rule"], rows[0]["decision"] or {}
    if r.get("retired"):
        raise OrgMemoryError(f"{rule_id} was retired in this session (a later decision overrode it)")
    try:
        expire_orphan_rules()           # free the slots of rules whose sessions are long gone
    except Exception:
        pass
    with _ORG_LOCK:
        org = Graph(ORG_GRAPH)
        if _org() is not None:
            same = org.read("""MATCH (o:OrgRule {predicate:$p, prefer_kind:$pk, over_kind:$ok}) WHERE o.retired = false
                               RETURN o.id AS id""",
                            {"p": r["predicate"], "pk": r["prefer_kind"], "ok": r["over_kind"]})
            if same:
                return {"org_rule": same[0]["id"], "already": True, "recheck": recheck_open_cards(g)}
            opposite = org.read("""MATCH (o:OrgRule {predicate:$p, prefer_kind:$ok, over_kind:$pk}) WHERE o.retired = false
                                   RETURN o.id AS id""",
                                {"p": r["predicate"], "pk": r["prefer_kind"], "ok": r["over_kind"]})
            if opposite:
                raise OrgMemoryError(f"company memory already prefers {r['over_kind']} over {r['prefer_kind']} "
                                     f"for '{r['predicate']}' ({opposite[0]['id']}); retire it first")
            n = org.scalar("MATCH (o:OrgRule) WHERE o.retired = false RETURN count(o)") or 0
            if n >= ORG_MAX_ACTIVE:
                raise OrgMemoryError(f"company memory is full ({ORG_MAX_ACTIVE} active rules); retire one first")
        oid = _id("ORG-", r["predicate"], r["prefer_kind"], r["over_kind"], tag, time.time_ns())
        org.run("""MERGE (u:User {key:$tag}) ON CREATE SET u.name = $tag, u.kind = 'session'
                   CREATE (o:OrgRule {id:$id, key:$id, predicate:$p, prefer_kind:$pk, over_kind:$ok, rationale:$why,
                                      retired:false, promoted_at:timestamp(), promoted_by:$tag,
                                      source_rule:$rid, source_session:$tag})
                   SET o.promoted_by_label = $lab
                   CREATE (o)-[:PROMOTED_BY {at:timestamp()}]->(u)
                   CREATE (o)-[:LEARNED_FROM]->(:SourceDecision {id:$did, key:$did, rationale:$drat, policy:$dpol,
                          decided_by:$dby, subject_key:$sk, subject:$subj, predicate:$p, chosen_value:$val,
                          source_doc:$doc, at:$dat, source_session:$tag})""",
                {"tag": tag, "lab": label, "id": oid, "p": r["predicate"], "pk": r["prefer_kind"],
                 "ok": r["over_kind"], "why": r.get("rationale") or "", "rid": rule_id,
                 "did": d.get("id"), "drat": d.get("rationale"), "dpol": d.get("policy"),
                 "dby": rows[0]["decided_by"], "sk": d.get("subject_key"), "subj": rows[0]["subject"],
                 "val": d.get("chosen_value"), "doc": rows[0]["source_doc"], "dat": d.get("at")})
        # bounded audit trail: drop the oldest retired rules beyond the cap
        org.run("""MATCH (o:OrgRule) WHERE o.retired = true WITH o ORDER BY o.retired_at DESC SKIP $keep
                   OPTIONAL MATCH (o)-[:LEARNED_FROM]->(d:SourceDecision)
                   OPTIONAL MATCH (c:Contest)-[:CONTESTS]->(o) DETACH DELETE o, d, c""",
                {"keep": ORG_MAX_KEPT})
        _export_locked()
    return {"org_rule": oid, "already": False, "recheck": recheck_open_cards(g)}


def _owner_check(o: dict, tag: str | None, admin: bool, org_id: str) -> None:
    if admin:
        return
    if tag is None:
        raise OrgMemoryError("retiring a company rule needs a session (X-Session header)", 400)
    if o.get("source_session") != tag:
        raise OrgMemoryError(f"only the session that promoted {org_id} can retire it; contest it instead "
                             f"(POST /api/memory/org/{org_id}/contest) to take it out of play in your session", 403)


def retire_org(org_id: str, g: Graph | None = None, user: str | None = None, admin: bool = False) -> dict:
    """Take a company rule out of play for every session (kept, marked retired, for the audit trail).
    Only the promoting session (or an operator with the admin token) may retire; the acting session's
    decisions made by the rule are then reopened as cards."""
    if not isinstance(org_id, str) or not ORG_ID_RE.match(org_id):
        raise OrgMemoryError("id must look like ORG-<10 hex>", 400)
    tag = session_tag(g.name) if g is not None and g.name.startswith("session_") else None
    with _ORG_LOCK:
        org = _org()
        rows = org.read("MATCH (o:OrgRule {id:$id}) RETURN o.retired AS retired, o.source_session AS source_session",
                        {"id": org_id}) if org else []
        if not rows:
            raise OrgMemoryError(f"no company rule {org_id}", 404)
        _owner_check(rows[0], tag, admin, org_id)
        already = bool(rows[0]["retired"])
        if not already:
            org.run("""MATCH (o:OrgRule {id:$id}) SET o.retired = true, o.retired_at = timestamp(),
                           o.retired_by = $by, o.retired_by_label = $lab""",
                    {"id": org_id, "by": tag if tag and not admin else "admin", "lab": _label(user)})
            _export_locked()
    review = review_org_rule(g, org_id, "retired") if tag else {"flagged": 0, "reopened": []}
    return {"org_rule": org_id, "retired": True, "already": already, **review}


def contest_org(g: Graph, org_id: str, user: str | None = None, reason: str | None = None) -> dict:
    """A session that disagrees with a company rule it did not promote: the rule stops applying in this
    session, the decisions it made here are reopened as resolve cards, and the contest is recorded on
    the rule in company memory (one per session) for its promoter to see."""
    tag = _need_session(g)
    if not isinstance(org_id, str) or not ORG_ID_RE.match(org_id):
        raise OrgMemoryError("id must look like ORG-<10 hex>", 400)
    with _ORG_LOCK:
        org = _org()
        rows = org.read("""MATCH (o:OrgRule {id:$id}) RETURN o {.*} AS o""", {"id": org_id}) if org else []
        if not rows:
            raise OrgMemoryError(f"no company rule {org_id}", 404)
        o = rows[0]["o"]
        if o.get("retired"):
            raise OrgMemoryError(f"{org_id} is already retired: it applies nowhere")
        if o.get("source_session") == tag:
            raise OrgMemoryError(f"this session promoted {org_id}: retire it instead")
        n = org.scalar("MATCH (c:Contest)-[:CONTESTS]->(:OrgRule {id:$id}) RETURN count(c)", {"id": org_id}) or 0
        if n < ORG_MAX_CONTESTS:
            org.run("""MATCH (o:OrgRule {id:$id})
                       MERGE (c:Contest {key:$key}) ON CREATE SET c.at = timestamp(), c.session = $tag
                       SET c.label = $lab, c.reason = $why
                       MERGE (c)-[:CONTESTS]->(o)""",
                    {"id": org_id, "key": f"{org_id}:{tag}", "tag": tag, "lab": _label(user),
                     "why": (str(reason or "")[:200]).strip() or None})
            _export_locked()
        contests = org.scalar("MATCH (c:Contest)-[:CONTESTS]->(:OrgRule {id:$id}) RETURN count(c)", {"id": org_id})
    g.run("""MERGE (r:OrgRuleRef {id:$id}) SET r.key = $id, r.graph = $graph, r.predicate = $p,
                 r.prefer_kind = $pk, r.over_kind = $ok, r.promoted_by = $by, r.rationale = $why,
                 r.contested = true, r.contested_at = timestamp()""",
          {"id": org_id, "graph": ORG_GRAPH, "p": o.get("predicate"), "pk": o.get("prefer_kind"),
           "ok": o.get("over_kind"), "by": o.get("promoted_by"), "why": o.get("rationale")})
    review = review_org_rule(g, org_id, "contested")
    return {"org_rule": org_id, "contested": True, "contests": int(contests or 0), **review}


# ---- JSON mirror of company memory (FalkorDB persistence is the primary store)

def _export_locked(path: str | None = None) -> str | None:
    path = path or ORG_EXPORT_PATH
    if not path:
        return None
    g = _org()
    rows = [] if g is None else g.read(
        """MATCH (o:OrgRule)
           OPTIONAL MATCH (o)-[:LEARNED_FROM]->(d:SourceDecision)
           OPTIONAL MATCH (o)-[:PROMOTED_BY]->(u:User)
           OPTIONAL MATCH (c:Contest)-[:CONTESTS]->(o)
           WITH o, d, u, collect(c {.*}) AS contests ORDER BY o.promoted_at, o.id
           RETURN o {.*} AS rule, d {.*} AS source, u {.*} AS user, contests""")
    facts = [] if g is None else [r["f"] for r in g.read("MATCH (f:OrgFact) RETURN f {.*} AS f ORDER BY f.published_at")]
    import json
    import pathlib
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps({"format": "bidbrain-org-memory/1", "graph": ORG_GRAPH,
                               "exported_at": int(time.time() * 1000), "rules": rows,
                               "facts": facts}, indent=1, default=str))
    os.replace(tmp, p)
    return str(p)


def export_org(path: str | None = None, only_if_present: bool = False) -> str | None:
    """Write company memory to JSON (atomic rename). With only_if_present, an absent company graph
    leaves the file alone (a failed import must never be followed by an empty export)."""
    with _ORG_LOCK:
        if only_if_present and _org() is None:
            return None
        return _export_locked(path)


def import_org(path: str | None = None) -> int:
    """Rebuild company memory from its JSON mirror, only when the company graph is absent (a FalkorDB
    data dir that survived the restart is newer and wins). Returns the number of rules restored."""
    import json
    import pathlib
    path = path or ORG_EXPORT_PATH
    if not path or not pathlib.Path(path).is_file():
        return 0
    clean = lambda m: {k: v for k, v in (m or {}).items() if v is not None and not k.startswith("_")}
    with _ORG_LOCK:
        if _org() is not None:
            return 0
        data = json.loads(pathlib.Path(path).read_text())
        org = Graph(ORG_GRAPH)
        n = 0
        for row in data.get("rules", []):
            rule = clean(row.get("rule"))
            if not ORG_ID_RE.match(str(rule.get("id", ""))):
                continue
            org.run("CREATE (o:OrgRule) SET o += $r", {"r": rule})
            if row.get("source"):
                org.run("MATCH (o:OrgRule {id:$id}) CREATE (o)-[:LEARNED_FROM]->(d:SourceDecision) SET d += $d",
                        {"id": rule["id"], "d": clean(row["source"])})
            user = clean(row.get("user"))
            if user.get("key"):
                org.run("""MATCH (o:OrgRule {id:$id}) MERGE (u:User {key:$k}) SET u += $u
                           CREATE (o)-[:PROMOTED_BY {at:o.promoted_at}]->(u)""",
                        {"id": rule["id"], "k": user["key"], "u": user})
            for c in row.get("contests") or []:
                c = clean(c)
                if c.get("key"):
                    org.run("MATCH (o:OrgRule {id:$id}) CREATE (c:Contest) SET c += $c CREATE (c)-[:CONTESTS]->(o)",
                            {"id": rule["id"], "c": c})
            n += 1
        for f in data.get("facts") or []:          # the company record (published facts) rides along
            f = clean(f)
            if str(f.get("id", "")).startswith("OF-"):
                org.run("MERGE (o:OrgFact {id:$id}) SET o += $f", {"id": f["id"], "f": f})
        return n


def apply_event(g: Graph, ev: dict, **kw) -> dict:
    """Convenience re-export of conflicts.events.apply_event (a document + its claims -> engine)."""
    from bidbrain.conflicts.events import apply_event as _apply
    return _apply(g, ev, **kw)
