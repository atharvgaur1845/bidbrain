"""Conflict detection and resolution over the claim layer.

    detect   two active claims about the same (subject, predicate) disagree beyond
             the coarser source's precision  ->  (a)-[:CONTRADICTS]->(b)
    resolve  claims are grouped by the value they state (within precision); in order:
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
"""
from __future__ import annotations

import hashlib
import time

from bidbrain.graph.client import Graph, day

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


CLAIMS_FOR = """MATCH (c:Claim)-[:ABOUT]->(s) WHERE c.status <> 'superseded' AND c.status <> 'rejected'
  AND ($keys IS NULL OR c.subject_key IN $keys)
RETURN c.id AS id, c.subject_label AS subject_label, c.subject_key AS subject_key, c.predicate AS predicate,
       c.value AS value, c.value_str AS value_str, c.source_doc AS source_doc, c.source_kind AS source_kind,
       c.authority AS authority, c.precision AS precision, c.issuer AS issuer, c.observed_day AS observed_day,
       c.page AS page, c.status AS status, c.planted AS planted
ORDER BY c.subject_key, c.predicate"""


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


def propose(conflict: dict, rules: list[dict]) -> dict:
    """Decide (without writing) how a conflict should be resolved."""
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
           keep_card: str | None = None) -> dict:
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
    losers = [c["id"] for c in conflict["claims"] if c["id"] != winner_id and _differs(c, win)]
    agree = [c["id"] for c in conflict["claims"] if c["id"] != winner_id and not _differs(c, win)]
    g.run("MATCH (c:Claim) WHERE c.id IN $ids SET c.status = $st", {"ids": losers, "st": loser_status})
    g.run("MATCH (c:Claim) WHERE c.id IN $ids SET c.status = 'active'", {"ids": agree + [winner_id]})
    g.run("""MATCH (w:Claim {id:$w}) UNWIND $ids AS lid MATCH (l:Claim {id:lid})
             WHERE NOT (w)-[:SUPERSEDES]->(l) CREATE (w)-[:SUPERSEDES {decision:$d}]->(l)""",
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
    g.run("MATCH (c:Claim) WHERE c.id IN $ids SET c.status = 'contested'", {"ids": ids})
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
    decisions, cards = [], []
    for cf in found["conflicts"]:
        prop = propose(cf, rules)
        if prop["auto"]:
            d = record(g, cf, prop["winner"]["id"], prop["policy"], prop["rationale"], rule_id=prop.get("rule_id"))
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
                       AND o.status <> 'superseded' AND o.status <> 'rejected'
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
