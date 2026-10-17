"""Audit the company's own past compliance claims against the graph.

Every compliance matrix the company filed marks each tender requirement MET. `compliance_check` re-tests each
MET row the engine can evaluate (turnover, similar works, key personnel, ISO certificates, years in business,
plus track-record, staff and plant figures) on the date the claim was made, with the same `qualify_clause`
rules the bid team and the Verifier use, and reports where the graph agrees and where it does not.
"""
from __future__ import annotations

import json
import re

from bidbrain import config
from bidbrain.graph.client import Graph, day
from bidbrain.tools.proof import qualify_clause
from bidbrain.tools.registry import tool

REQS = """
MATCH (t:Tender)-[:REQUIRES]->(q:Requirement)-[e:EVIDENCED_BY {kind:'compliance_matrix'}]->(d:Document)
WHERE ($tender IS NULL OR t.key = $tender) AND ($kind IS NULL OR q.kind = $kind)
OPTIONAL MATCH (q)-[:CITES_CERT]->(cert:Certification)
RETURN t.key AS tender, t.category AS category, t.bid_date AS bid_date, t.bid_date_basis AS date_basis,
       t.bid_value AS bid_value, q.key AS key, q.text AS text, q.kind AS kind, q.status AS status,
       q.status_raw AS claimed, q.threshold_value AS threshold, q.evidence AS claimed_evidence,
       d.doc_id AS doc, e.page AS page, cert.key AS cited_cert, cert.standard AS cited_standard
ORDER BY tender, doc, page"""

TRACK_RECORD = """
MATCH (w:Work)-[e:EVIDENCED_BY {kind:'completion_certificate'}]->(d:Document)
WHERE w.category = $cat AND w.completed_day <= $bd
RETURN w.key AS key, w.title AS title, w.completed AS completed, w.completed_day AS cday, d.doc_id AS doc,
       e.page AS page
ORDER BY w.completed_day LIMIT 1"""

STAFF = """
MATCH (p:Person)-[e:EVIDENCED_BY {kind:'cv'}]->(:Document) WHERE p.joined_day <= $bd
WITH count(p) AS with_cv
OPTIONAL MATCH (b:BusinessUnit)
RETURN with_cv, sum(b.headcount) AS headcount"""

PLANT = """
MATCH (a:Asset)-[:EVIDENCED_BY]->(d:Document)
WHERE a.acquired <= $year AND ($owned IS NULL OR a.ownership = 'owned')
RETURN count(a) AS n, collect(DISTINCT d.doc_id)[0] AS doc"""

KINDS = ["turnover", "similar_works", "key_personnel", "certification", "years_in_business", "experience_years",
         "staff_count", "plant", "bid_security", "registration", "declaration", "plan"]
OUTCOME = {"PASS": "agree", "FAIL": "disagree", "AT_RISK": "unconfirmed"}


def _rule(r: dict) -> tuple[str | None, dict | str]:
    """Requirement row -> (engine kind, params), or (None, why it cannot be checked)."""
    text, thr, kind = r["text"] or "", r["threshold"], r["kind"]
    low = text.lower()
    if kind == "turnover" and thr:
        m = re.search(r"last (\d+) ?fy", low)
        key = "min_each_year" if "each" in low else "min_average"
        return "turnover", {key: int(thr), "years": int(m.group(1)) if m else 3}
    if kind == "years_in_business" and thr:
        return "years_in_business", {"min_years": int(thr)}
    if kind == "key_personnel" and "pmp" in low:
        return "key_personnel", {"credential": "PMP", "count": 1}
    if kind == "certification":
        m = re.search(r"iso\s*(\d{4,5})", low)
        return ("certification", {"standard": f"ISO {m.group(1)}"}) if m else (None, "not an ISO standard the graph records")
    if kind == "similar_works":
        if not r["category"]:
            return None, "tender category unknown"
        if not r["bid_value"]:
            return None, "no value threshold stated and no bid value on the matrix"
        # CPWD rule against the bid value printed on the matrix (the tender's estimated cost is not in the corpus)
        return "similar_works", {"categories": [r["category"]], "estimated_cost": int(r["bid_value"])}
    if kind == "experience_years" and thr and r["category"]:
        return "track_record", {"years": int(thr), "category": r["category"]}
    if kind == "staff_count" and thr:
        return "staff", {"min": int(thr)}
    if kind == "plant":
        m = re.search(r"(\d+)\s*(owned assets|items)", (r["claimed_evidence"] or "").lower())
        if m:
            return "plant", {"stated": int(m.group(1)), "owned": "owned" in m.group(2)}
        return None, "no figure stated"
    return None, {"bid_security": "the EMD instrument is not in the corpus (bonds on file are performance bonds)",
                  "registration": "registrations (GST, PAN, labour licence) are not modelled",
                  "declaration": "a self-declaration; nothing in the graph to test it against",
                  "plan": "a plan document submitted with the bid; nothing to test",
                  }.get(kind, "no threshold the engine can evaluate")


def _custom(g: Graph, kind: str, p: dict, bid_date: str) -> dict:
    bd = day(bid_date)
    if kind == "track_record":
        rows = g.read(TRACK_RECORD, {"cat": p["category"], "bd": bd})
        if not rows:
            return {"result": {"verdict": "FAIL", "basis": f"no completed {p['category']} work by {bid_date}"},
                    "evidence": [], "cypher": [TRACK_RECORD]}
        first = rows[0]
        yrs = round((bd - first["cday"]) / 365.25, 1)
        return {"result": {"verdict": "PASS" if yrs >= p["years"] else "FAIL",
                           "basis": f"first completed {p['category']} work {first['completed']} ({first['title']}): "
                                    f"{yrs} years before {bid_date}, need {p['years']}"},
                "evidence": [{"doc_id": first["doc"], "page": first["page"],
                              "what": f"{first['title']} completed {first['completed']}"}],
                "cypher": [TRACK_RECORD]}
    if kind == "staff":
        r = g.read(STAFF, {"bd": bd})[0]
        return {"result": {"verdict": "PASS" if r["with_cv"] >= p["min"] else "FAIL",
                           "basis": f"{r['with_cv']} staff with a CV on file had joined by {bid_date} (need {p['min']}); "
                                    f"business-unit headcount today {r['headcount']}"},
                "evidence": [], "cypher": [STAFF]}
    if kind == "plant":
        year = int(bid_date[:4])
        r = g.read(PLANT, {"year": year, "owned": True if p["owned"] else None})[0]
        what = "owned assets" if p["owned"] else "assets"
        return {"result": {"verdict": "PASS" if r["n"] >= p["stated"] else "FAIL",
                           "basis": f"matrix states {p['stated']} {what}; the asset register has {r['n']} {what} "
                                    f"acquired by {year}"},
                "evidence": [{"doc_id": r["doc"], "page": None, "what": f"asset register: {r['n']} {what} by {year}"}]
                if r["doc"] else [], "cypher": [PLANT]}
    raise ValueError(kind)


@tool("compliance_check",
      """Audit the company's own past compliance claims. Every tender compliance matrix in the corpus
      marks its requirements MET; this re-tests each MET row against the graph ON THE DATE THE CLAIM WAS
      MADE (the bid submission date printed on the matrix, else the matrix's signature date), using the
      same qualify_clause rules as bid qualification: turnover (average / each year), similar works (CPWD
      rule against the bid value), PMP key personnel, ISO certificates, years in business, plus
      similar-works track record (years since the first completed work in the category), CV'd staff
      count and the stated asset figure. outcome: agree (graph confirms) | disagree (graph says it was
      not met then) | unconfirmed (e.g. an audited year missing) | not_checkable (EMD, GST, declarations).
      likely_generator_artifacts counts the systematic disagreements that most likely come from the synthetic corpus (certificates dated after the matrices that cite them, one PMP
      issue date for everyone, the whole asset register as the plant figure).
      Optional tender (ref), kind (requirement kind), only_disagreements.""",
      {"tender": {"type": "string"}, "kind": {"type": "string", "enum": KINDS},
       "only_disagreements": {"type": "boolean"}})
def compliance_check(g: Graph, tender: str | None = None, kind: str | None = None,
                     only_disagreements: bool = False) -> dict:
    tk = tender.strip().upper() if tender else None
    rows = g.read(REQS, {"tender": tk, "kind": kind})
    if tk and not rows:
        raise LookupError(f"no compliance matrix for tender {tk}")
    memo: dict[tuple, dict] = {}
    cypher = [REQS]
    out_rows, ev = [], []
    for r in rows:
        bid_date = r["bid_date"] or config.AS_OF
        base = {"tender": r["tender"], "requirement": r["text"], "kind": r["kind"], "claimed": r["claimed"],
                "claimed_evidence": r["claimed_evidence"], "bid_date": bid_date,
                "date_basis": r["date_basis"] or "corpus date (none on the matrix)", "doc": r["doc"], "page": r["page"]}
        if r["status"] != "MET":
            out_rows.append({**base, "outcome": "not_claimed", "why": f"matrix status {r['claimed']}"})
            continue
        ek, params = _rule(r)
        if ek is None:
            out_rows.append({**base, "outcome": "not_checkable", "why": params})
            continue
        mk = (ek, json.dumps(params, sort_keys=True), bid_date)
        if mk not in memo:
            memo[mk] = (qualify_clause(g, ek, params, bid_date) if ek in
                        ("turnover", "similar_works", "key_personnel", "certification", "years_in_business")
                        else _custom(g, ek, params, bid_date))
            for q in memo[mk].get("cypher", []):
                if q not in cypher:
                    cypher.append(q)
        res = memo[mk]["result"]
        row = {**base, "check": ek, "params": params, "engine_verdict": res["verdict"],
               "outcome": OUTCOME[res["verdict"]], "why": res.get("basis")}
        if ek == "turnover" and res["verdict"] == "FAIL" and not any(y.get("revenue") for y in res.get("years", [])):
            # no audited statement for any year in the window: the graph cannot say the bar was missed
            row["outcome"] = "unconfirmed"
            row["why"] = (f"no audited statement in the graph for FY {', '.join(res.get('missing_years', []))}; "
                          f"the matrix cites '{r['claimed_evidence']}'")
        if r["cited_cert"] and ek == "certification" and r["cited_standard"] and \
                params["standard"].lower() not in r["cited_standard"].lower():
            row["outcome"], row["why"] = "disagree", f"matrix cites {r['cited_cert']} ({r['cited_standard']})"
        out_rows.append(row)
        if row["outcome"] != "agree":
            ev.append({"doc_id": r["doc"], "page": r["page"],
                       "what": f"{r['tender']}: claimed {r['claimed']} for '{r['text']}' on {bid_date}"})
            ev += [e for e in memo[mk].get("evidence", [])[:2] if e.get("doc_id")]
    met = [x for x in out_rows if x["outcome"] != "not_claimed"]
    count = lambda xs, o: sum(1 for x in xs if x["outcome"] == o)
    checked = [x for x in met if x["outcome"] in ("agree", "disagree", "unconfirmed")]
    by_kind: dict[str, dict] = {}
    for x in met:
        k = by_kind.setdefault(x["kind"], {"agree": 0, "disagree": 0, "unconfirmed": 0, "not_checkable": 0})
        k[x["outcome"]] += 1
    tenders: dict[str, dict] = {}
    for x in met:
        t = tenders.setdefault(x["tender"], {"tender": x["tender"], "bid_date": x["bid_date"],
                                             "date_basis": x["date_basis"], "agree": 0, "disagree": 0,
                                             "unconfirmed": 0, "not_checkable": 0})
        t[x["outcome"]] += 1
    summary = {"requirements": len(out_rows), "claimed_met": len(met), "checked": len(checked),
               "agree": count(met, "agree"), "disagree": count(met, "disagree"),
               "unconfirmed": count(met, "unconfirmed"), "not_checkable": count(met, "not_checkable"),
               "agreement_rate": round(count(met, "agree") / len(checked), 3) if checked else None}
    slim = lambda x: {k: v for k, v in x.items() if k not in ("claimed_evidence", "params")}
    disagreements = [slim(x) for x in met if x["outcome"] == "disagree"]
    result = {"summary": summary, "by_kind": by_kind,
              "disagreements": disagreements if tk else disagreements[:40],
              "unconfirmed": [slim(x) for x in met if x["outcome"] == "unconfirmed"][: None if tk else 15]}
    if tk:
        result["requirements"] = [slim(x) for x in out_rows
                                  if not only_disagreements or x["outcome"] == "disagree"]
    else:
        result["tenders"] = sorted(tenders.values(), key=lambda t: (-t["disagree"], t["tender"]))
        if len(disagreements) > 40:
            result["note"] = f"{len(disagreements)} disagreements; first 40 shown, pass `tender` or `kind` to narrow"
    if only_disagreements:
        result.pop("unconfirmed", None)
    caveats = _generator_artifacts(g, [x for x in met if x["outcome"] == "disagree"])
    if caveats:
        result["likely_generator_artifacts"] = caveats
    return {"result": result, "evidence": ev[:60], "cypher": cypher}


ARTIFACT_FACTS = """
MATCH (a:Asset) WITH sum(CASE WHEN a.ownership = 'owned' THEN 1 ELSE 0 END) AS owned, count(a) AS assets
OPTIONAL MATCH (c:Certification)
WITH owned, assets, collect({standard: c.standard, initial: c.initial_date}) AS certs
OPTIONAL MATCH (cr:Credential {kind:'PMP'})
RETURN owned, assets, certs, min(cr.issued) AS first_pmp, max(cr.issued) AS last_pmp"""


def _generator_artifacts(g: Graph, disagree: list[dict]) -> list[dict]:
    """Three disagreement patterns are systematic and most likely come from the synthetic corpus generator, not
    from false claims: matrices dated before the certificate they cite was first issued, matrices dated before
    any PMP on file was issued, and a plant figure equal to the whole asset register (owned + leased, including
    plant bought after the bid). They are counted here so nobody reads them as fraud."""
    f = g.read(ARTIFACT_FACTS)[0]
    owned, assets = int(f["owned"]), int(f["assets"])
    first = {c["standard"].split(":")[0]: c["initial"] for c in f["certs"] if c["standard"]}
    out = []

    def span(xs):
        d = sorted(x["bid_date"] for x in xs)
        return f"{d[0]}..{d[-1]}"
    iso = [x for x in disagree if x["kind"] == "certification" and x["bid_date"]
           and (x.get("params") or {}).get("standard") in first and x["bid_date"] < first[x["params"]["standard"]]]
    if iso:
        out.append({"kind": "certification", "n": len(iso),
                    "pattern": f"matrices dated {span(iso)} claim ISO certificates whose initial_date on file is "
                               f"{min(first[x['params']['standard']] for x in iso)} or later",
                    "reading": "the certificates' issue dates postdate the matrices that cite them; most likely the "
                               "generator dated the certificates independently of the matrices"})
    pmp = [x for x in disagree if x["kind"] == "key_personnel" and f["first_pmp"] and x["bid_date"] < f["first_pmp"]]
    if pmp:
        same = " (all on the same day)" if f["first_pmp"] == f["last_pmp"] else ""
        out.append({"kind": "key_personnel", "n": len(pmp),
                    "pattern": f"matrices dated {span(pmp)}; every PMP on file was issued from {f['first_pmp']}{same}",
                    "reading": "a single issue date for every credential of a kind is a generator default, not evidence "
                               "that the PMP claims were false"})
    plant = [x for x in disagree if x["kind"] == "plant" and (x.get("params") or {}).get("stated") == assets]
    if plant:
        out.append({"kind": "plant", "n": len(plant),
                    "pattern": f"every matrix states {assets}, the size of the whole asset register "
                               f"({owned} owned + {assets - owned} leased, including plant acquired after the bid)",
                    "reading": "the figure is the register's total at generation time, not a claim made on the bid date"})
    return out