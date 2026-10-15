"""Conflict detection and resolution over the claim layer.

    detect   two active claims about the same (subject, predicate) disagree beyond
             the coarser source's precision  ->  (a)-[:CONTRADICTS]->(b)
    resolve  in order:
               1. a learned PolicyRule matches        (procedural memory)
               2. same issuer, later document         (recency: corrigenda, re-issues)
               3. authority gap >= 20                 (the ladder in ingest/claims.py)
               otherwise -> an open resolve card for a human
    record   every outcome is a (:Decision) with SELECTS / BASED_ON / DECIDED_BY,
             and the subject's property is re-materialized from the winner.

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
    return g.read("MATCH (r:PolicyRule) RETURN r.id AS id, r.predicate AS predicate, "
                  "r.prefer_kind AS prefer_kind, r.over_kind AS over_kind, r.rationale AS rationale")


def propose(conflict: dict, rules: list[dict]) -> dict:
    """Decide (without writing) how a conflict should be resolved."""
    cl = conflict["claims"]
    kinds = {c["source_kind"] for c in cl}
    for r in rules:
        if r["predicate"] == conflict["predicate"] and r["prefer_kind"] in kinds and r["over_kind"] in kinds:
            win = next(c for c in cl if c["source_kind"] == r["prefer_kind"])
            return {"auto": True, "policy": "learned_rule", "rule_id": r["id"], "winner": win,
                    "rationale": f"learned rule {r['id']}: prefer {r['prefer_kind']} over {r['over_kind']} "
                                 f"for '{conflict['predicate']}' ({r['rationale']})"}
    # Recency: an issuer may amend its own statement — but only with a document
    # at least as formal as the one it amends (a corrigendum or re-issued
    # certificate, not a chatty letter), and only if no *other* issuer's claim
    # outranks the amendment.
    top = max(cl, key=lambda c: c["authority"])
    same = [c for c in cl if (c["issuer"] or "").lower() == (top["issuer"] or "").lower() and c.get("observed_day")]
    if len(same) >= 2:
        latest = max(same, key=lambda c: c["observed_day"])
        earlier = [c for c in same if c is not latest]
        others = [c for c in cl if c not in same]
        if (latest["observed_day"] > max(c["observed_day"] for c in earlier)
                and latest["authority"] >= max(c["authority"] for c in earlier)
                and all(o["authority"] < latest["authority"] for o in others)):
            return {"auto": True, "policy": "recency_same_issuer", "winner": latest,
                    "rationale": f"{latest['issuer']} amended its own statement in {latest['source_doc']} "
                                 f"({latest['source_kind']}, at least as formal as what it replaces); "
                                 f"the later document supersedes the earlier"}
    ranked = sorted(cl, key=lambda c: -c["authority"])
    if ranked[0]["authority"] - ranked[1]["authority"] >= AUTHORITY_GAP:
        win = ranked[0]
        return {"auto": True, "policy": "authority", "winner": win,
                "rationale": f"{win['source_kind']} (authority {win['authority']}) outranks "
                             f"{ranked[1]['source_kind']} ({ranked[1]['authority']}) by >= {AUTHORITY_GAP}"}
    return {"auto": False, "policy": "needs_human", "winner": None, "suggested": ranked[0],
            "rationale": f"sources are close in authority ({ranked[0]['source_kind']} {ranked[0]['authority']} vs "
                         f"{ranked[1]['source_kind']} {ranked[1]['authority']}) and from different issuers"}


def _materialize(g: Graph, label: str, key: str, predicate: str, value) -> None:
    props = PROJECT.get(predicate, [predicate])
    sets = {props[0]: value}
    if len(props) > 1 and isinstance(value, str):
        if props[1].endswith("_day"):
            sets[props[1]] = day(value)
        if len(props) > 2 and props[2] == "completed_year":
            sets[props[2]] = int(value[:4])
    g.run(f"MATCH (s:{label} {{key:$k}}) SET s += $p", {"k": key, "p": sets})


def record(g: Graph, conflict: dict, winner_id: str, policy: str, rationale: str,
           actor: str = "policy-engine", actor_kind: str = "Agent", rule_id: str | None = None) -> dict:
    """Write the Decision, flip claim statuses, re-materialize the property."""
    did = _id("DEC-", conflict["id"], winner_id, policy, time.time_ns())
    loser_status = "superseded" if policy in ("recency_same_issuer",) else "rejected"
    win = next(c for c in conflict["claims"] if c["id"] == winner_id)
    g.run(f"""MERGE (a:{actor_kind} {{key:$actor}}) ON CREATE SET a.name = $actor
              CREATE (d:Decision {{id:$id, key:$id, conflict_id:$cid, subject_key:$sk, predicate:$pred,
                                   policy:$policy, rationale:$why, at:timestamp(), chosen_value:$val}})
              CREATE (d)-[:DECIDED_BY]->(a)
              WITH d MATCH (w:Claim {{id:$win}}) CREATE (d)-[:SELECTS]->(w)
              WITH d UNWIND $ids AS cid MATCH (c:Claim {{id:cid}}) CREATE (d)-[:BASED_ON]->(c)""",
          {"actor": actor, "id": did, "cid": conflict["id"], "sk": conflict["subject_key"],
           "pred": conflict["predicate"], "policy": policy, "why": rationale, "val": str(win["value"]),
           "win": winner_id, "ids": [c["id"] for c in conflict["claims"]]})
    if rule_id:
        g.run("MATCH (d:Decision {id:$d}), (r:PolicyRule {id:$r}) CREATE (d)-[:APPLIED_RULE]->(r)",
              {"d": did, "r": rule_id})
    # earlier decisions about the same fact are superseded by this one
    g.run("""MATCH (old:Decision {subject_key:$sk, predicate:$pred}) WHERE old.id <> $d
             AND NOT ()-[:SUPERSEDES]->(old)
             WITH old MATCH (d:Decision {id:$d}) CREATE (d)-[:SUPERSEDES]->(old)""",
          {"sk": conflict["subject_key"], "pred": conflict["predicate"], "d": did})
    losers = [c["id"] for c in conflict["claims"] if c["id"] != winner_id]
    g.run("MATCH (c:Claim) WHERE c.id IN $ids SET c.status = $st", {"ids": losers, "st": loser_status})
    g.run("MATCH (c:Claim {id:$w}) SET c.status = 'active'", {"w": winner_id})
    for l in losers:
        g.run("MATCH (w:Claim {id:$w}), (l:Claim {id:$l}) WHERE NOT (w)-[:SUPERSEDES]->(l) "
              "CREATE (w)-[:SUPERSEDES {decision:$d}]->(l)", {"w": winner_id, "l": l, "d": did})
    _materialize(g, conflict["subject_label"], conflict["subject_key"], conflict["predicate"], win["value"])
    g.run("MATCH (m:Meta {key:'meta'}) SET m.version = m.version + 1")
    return {"decision_id": did, "policy": policy, "winner": winner_id, "value": win["value"],
            "rationale": rationale}


def open_card(g: Graph, conflict: dict, proposal: dict) -> dict:
    ids = [c["id"] for c in conflict["claims"]]
    g.run("MATCH (c:Claim) WHERE c.id IN $ids SET c.status = 'contested'", {"ids": ids})
    g.run("""MERGE (k:ConflictCard {id:$id}) SET k.key = $id, k.subject_label = $sl, k.subject_key = $sk,
                 k.predicate = $p, k.status = 'open', k.suggested = $sug, k.rationale = $why, k.opened_at = timestamp()
             WITH k UNWIND $ids AS cid MATCH (c:Claim {id:cid}) MERGE (k)-[:ABOUT_CLAIM]->(c)""",
          {"id": conflict["id"], "sl": conflict["subject_label"], "sk": conflict["subject_key"],
           "p": conflict["predicate"], "sug": proposal["suggested"]["id"], "why": proposal["rationale"], "ids": ids})
    return {"card_id": conflict["id"], "suggested": proposal["suggested"]["id"], "rationale": proposal["rationale"]}


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


def _brief(cf: dict) -> dict:
    return {"id": cf["id"], "subject_label": cf["subject_label"], "subject_key": cf["subject_key"],
            "predicate": cf["predicate"],
            "claims": [{k: c.get(k) for k in ("id", "value", "value_str", "source_doc", "source_kind",
                                               "authority", "issuer", "page", "planted")} for c in cf["claims"]]}


def resolve_card(g: Graph, card_id: str, winner_claim: str, rationale: str, user: str = "bid-manager",
                 apply_to_similar: bool = False) -> dict:
    """A human picks the winner on an open card; optionally learn a PolicyRule from it."""
    rows = g.read("""MATCH (k:ConflictCard {id:$id})-[:ABOUT_CLAIM]->(c:Claim)
                     RETURN k.subject_label AS sl, k.subject_key AS sk, k.predicate AS p, collect(c {.*}) AS claims""",
                  {"id": card_id})
    if not rows:
        raise LookupError(f"no open card {card_id}")
    r = rows[0]
    cf = {"id": card_id, "subject_label": r["sl"], "subject_key": r["sk"], "predicate": r["p"],
          "claims": r["claims"]}
    d = record(g, cf, winner_claim, "human", rationale, actor=user, actor_kind="User")
    g.run("MATCH (k:ConflictCard {id:$id}) SET k.status = 'resolved', k.decision = $d", {"id": card_id, "d": d["decision_id"]})
    rule = None
    if apply_to_similar:
        win = next(c for c in cf["claims"] if c["id"] == winner_claim)
        lose = next(c for c in cf["claims"] if c["id"] != winner_claim)
        rid = _id("RULE-", cf["predicate"], win["source_kind"], lose["source_kind"])
        g.run("""MERGE (r:PolicyRule {id:$id}) SET r.key = $id, r.predicate = $p, r.prefer_kind = $pk,
                     r.over_kind = $ok, r.rationale = $why, r.created_by = $u, r.created_at = timestamp()
                 WITH r MATCH (d:Decision {id:$d}) MERGE (r)-[:LEARNED_FROM]->(d)""",
              {"id": rid, "p": cf["predicate"], "pk": win["source_kind"], "ok": lose["source_kind"],
               "why": rationale, "u": user, "d": d["decision_id"]})
        rule = rid
    return {**d, "learned_rule": rule}
