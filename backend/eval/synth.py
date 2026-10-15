"""Synthesized benchmark sets with gold computed independently of BidBrain's tools.

    python -m eval.synth            -> data/eval/abs_set.json, data/eval/tnd_set.json

ABS — absence questions ("which/how many have NO ..."), the pattern a retriever
cannot answer because it never sees the missing thing. Gold: direct Cypher here,
not `prove_absence` (so BidBrain is not graded against its own code).

TND — tender eligibility questions with a yes/no or count answer. Gold: a
separate, deliberately plain re-implementation of the CPWD 80/60/40 rule and the
turnover / personnel checks over direct Cypher — not `qualify_clause`.

Mentions use the informal forms people type ("PHED Odisha", "Gujarat PWD") in
a third of questions, generated from the alias lists.
"""
from __future__ import annotations

import datetime as dt
import json
import random

from bidbrain import config
from bidbrain.graph.client import Graph, day

R = random.Random(2026)


def _informal(name: str, aliases: list[str]) -> str:
    short = [a for a in aliases if a != name.lower() and len(a) > 4 and " " in a]
    return R.choice(short).upper() if short and R.random() < 0.33 else name


def build_abs(g: Graph) -> list[dict]:
    out = []
    clients = g.read("""MATCH (c:Client)<-[:FOR_CLIENT]-(w:Work)
                        RETURN c.key AS key, c.name AS name, c.aliases AS aliases,
                               count(w) AS n, sum(CASE WHEN w.has_reference THEN 0 ELSE 1 END) AS missing""")
    for c in clients:
        nm = _informal(c["name"], c["aliases"] or [])
        out.append({"qid": f"ABS-C-{c['key']}", "answer_type": "count", "gold": int(c["missing"]), "hops": 3,
                    "family": "absence_client", "absence": True,
                    "question": R.choice([f"How many of {nm}'s completed works have no client reference letter on file?",
                                          f"For {nm}, count the completed works that lack a reference letter.",
                                          f"Across everything we completed for {nm}, how many works have no client testimonial on record?"])})
    people = g.read("""MATCH (p:Person)<-[:LED_BY]-(w:Work)
                       RETURN p.key AS key, p.name AS name, count(w) AS n,
                              sum(CASE WHEN w.has_reference THEN 0 ELSE 1 END) AS missing""")
    for p in people:
        if p["n"] < 2:
            continue
        out.append({"qid": f"ABS-P-{p['key']}", "answer_type": "count", "gold": int(p["missing"]), "hops": 3,
                    "family": "absence_person", "absence": True,
                    "question": f"Of the works {p['name']} led as project lead, how many have no client reference letter?"})
    # person -> credential -> works for a client: 5 hops
    rows = g.read("""MATCH (p:Person)-[:HOLDS]->(cr:Credential), (p)<-[:LED_BY]-(w0:Work)-[:FOR_CLIENT]->(c:Client)
                     WITH p, cr, c, w0 ORDER BY w0.key
                     WITH p, cr, c, collect(w0)[0] AS anchor
                     MATCH (c)<-[:FOR_CLIENT]-(w:Work)
                     RETURN p.name AS person, cr.kind AS cred, anchor.title AS anchor, c.name AS client,
                            count(w) AS n, sum(CASE WHEN w.has_reference THEN 0 ELSE 1 END) AS missing""")
    R.shuffle(rows)
    for r in rows[:40]:
        out.append({"qid": f"ABS-H-{len(out)}", "answer_type": "count", "gold": int(r["missing"]), "hops": 5,
                    "family": "absence_multihop", "absence": True,
                    "question": f"Starting from {r['person']}'s {r['cred']} and their {r['anchor']} project, how many of "
                                f"that client's completed works have no reference letter on file?"})
    cats = g.read("""MATCH (w:Work)-[:FOR_CLIENT]->(c:Client) WITH w.category AS cat, c.govt_level AS lvl,
                            count(w) AS n, sum(CASE WHEN w.has_reference THEN 0 ELSE 1 END) AS missing
                     WHERE n >= 2 RETURN cat, lvl, n, missing""")
    lvl_word = {"central": "central government", "state": "state government", "psu": "public sector undertaking",
                "municipal": "municipal", "private": "private-sector"}
    for r in cats:
        out.append({"qid": f"ABS-K-{r['cat'].replace(' ', '_')}-{r['lvl']}", "answer_type": "count", "gold": int(r["missing"]), "hops": 4,
                    "family": "absence_category", "absence": True,
                    "question": f"How many {r['cat']} works we completed for {lvl_word[r['lvl']]} clients have no client "
                                f"reference letter on file?"})
    # credential absence at a future date
    for when in ("2027-06-30", "2027-12-31", "2028-06-30"):
        for c in R.sample(clients, 6):
            rows = g.read("""MATCH (c:Client {key:$c})<-[:FOR_CLIENT]-(:Work)-[:LED_BY]->(p:Person)
                             WITH DISTINCT p OPTIONAL MATCH (p)-[:HOLDS]->(cr:Credential {kind:'PMP'})
                             WHERE cr.issued_day <= $d
                             WITH p, max(cr.valid_through_day) AS v
                             RETURN count(p) AS n, sum(CASE WHEN v IS NULL OR v < $d THEN 1 ELSE 0 END) AS lacking""",
                          {"c": c["key"], "d": day(when)})
            out.append({"qid": f"ABS-V-{c['key']}-{when}", "answer_type": "count", "gold": int(rows[0]["lacking"]),
                        "hops": 4, "family": "absence_credential", "absence": True,
                        "question": f"Of the project leads who have delivered work for {c['name']}, how many will NOT hold a "
                                    f"valid PMP certification on {when}?"})
    return out


def _cpwd(values: list[int], est: int) -> bool:
    s = sorted(values, reverse=True)
    return (len(s) >= 1 and s[0] >= 0.8 * est) or (len(s) >= 2 and s[1] >= 0.6 * est) or \
           (len(s) >= 3 and s[2] >= 0.4 * est)


def build_tnd(g: Graph) -> list[dict]:
    out = []
    groups = [["roads highways", "expressways"], ["bridges flyovers", "large bridges"], ["water treatment", "water supply"],
              ["sewerage drainage"], ["buildings", "small buildings"], ["irrigation"], ["tunnels"], ["industrial epc"]]
    for cats in groups:
        for est_cr in (40, 80, 150, 250, 400):
            for years, bid in ((5, "2026-06-30"), (7, "2026-09-30")):
                since = (dt.date.fromisoformat(bid).replace(year=int(bid[:4]) - years)).isoformat()
                vals = [r["v"] for r in g.read("""MATCH (w:Work) WHERE w.category IN $cats
                                                    AND w.completed >= $since AND w.completed < $bid
                                                  RETURN w.value AS v""", {"cats": cats, "since": since, "bid": bid})]
                ok = _cpwd(vals, est_cr * 10_000_000)
                out.append({"qid": f"TND-S-{cats[0][:6]}-{est_cr}-{years}", "answer_type": "verdict",
                            "gold": "QUALIFIES" if ok else "DOES NOT QUALIFY", "hops": 4, "family": "tender_similar",
                            "question": (f"Tender clause (bid due {bid}): 'Similar works ({' / '.join(cats)}) completed in the "
                                         f"last {years} years: one work of at least 80% of the estimated cost of INR {est_cr} Cr, "
                                         f"or two works of 60%, or three works of 40%.' Do we qualify? Answer QUALIFIES or "
                                         f"DOES NOT QUALIFY.")})
    # CPWD reads "last 3 financial years" as the 3 FYs completed before the bid date. With the bid due
    # 2026-06-30 (inside FY 2026-27) those are FY 2023-24, 2024-25 and 2025-26.
    turnover_bid = "2026-06-30"
    fs = {r["y"]: r["rev"] for r in g.read("""MATCH (f:FinancialStatement)-[:FOR_FY]->(y:FiscalYear)
                                              WHERE f.from_comparative_only = false
                                              RETURN y.start_year AS y, f.revenue_operations AS rev""")}
    last3 = [fs[y] for y in (2023, 2024, 2025)]
    for cr in (75, 150, 200, 230, 240, 250, 260, 300):
        bar = cr * 10_000_000
        out.append({"qid": f"TND-T-avg-{cr}", "answer_type": "verdict", "hops": 2, "family": "tender_turnover",
                    "gold": "QUALIFIES" if sum(last3) / 3 >= bar else "DOES NOT QUALIFY",
                    "question": f"Tender clause (bid due {turnover_bid}): 'Average annual turnover of the last 3 financial "
                                f"years not less than INR {cr} Cr.' Do we qualify? Answer QUALIFIES or DOES NOT QUALIFY."})
        out.append({"qid": f"TND-T-each-{cr}", "answer_type": "verdict", "hops": 2, "family": "tender_turnover",
                    "gold": "QUALIFIES" if min(last3) >= bar else "DOES NOT QUALIFY",
                    "question": f"Tender clause (bid due {turnover_bid}): 'Annual turnover >= INR {cr} Cr in each of the "
                                f"last 3 financial years.' Do we qualify? Answer QUALIFIES or DOES NOT QUALIFY."})
    for cats in groups[:6]:
        for minx in (10, 15, 20, 25):
            for when in ("2026-06-30", "2027-09-30"):
                n = g.read("""MATCH (p:Person)-[:HOLDS]->(c:Credential {kind:'PMP'})
                              WHERE c.valid_through_day >= $d AND c.issued_day <= $d
                                AND p.experience_years >= $x AND (p)<-[:LED_BY]-(:Work {category: $c0})
                              RETURN count(DISTINCT p) AS n""", {"d": day(when), "x": minx, "c0": cats[0]})[0]["n"]
                out.append({"qid": f"TND-P-{cats[0][:6]}-{minx}-{when}", "answer_type": "count", "gold": int(n), "hops": 4,
                            "family": "tender_personnel",
                            "question": f"How many of our people could be proposed as Project Manager for a bid due {when} "
                                        f"that requires a PMP valid on that date, at least {minx} years' experience, and "
                                        f"having led at least one {cats[0]} project?"})
    return out


def _unique(rows: list[dict], name: str) -> list[dict]:
    """qids key results and resumes in eval.run, so two questions must never share one."""
    import collections
    dup = [k for k, v in collections.Counter(r["qid"] for r in rows).items() if v > 1]
    assert not dup, f"{name}: duplicate qids {dup}"
    return rows


if __name__ == "__main__":
    g = Graph()
    a, t = _unique(build_abs(g), "ABS"), _unique(build_tnd(g), "TND")
    (config.DATA / "eval" / "abs_set.json").write_text(json.dumps(a, indent=1))
    (config.DATA / "eval" / "tnd_set.json").write_text(json.dumps(t, indent=1))
    import collections
    print("ABS", len(a), collections.Counter(x["family"] for x in a))
    print("TND", len(t), collections.Counter(x["family"] for x in t),
          collections.Counter(x["gold"] for x in t if x["answer_type"] == "verdict"))
