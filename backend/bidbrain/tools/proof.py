"""Proofs and eligibility: the tools that turn the graph into bid decisions.

`prove_absence` answers the question a retrieval system cannot: "which of this
client's works have NO reference letter?" A retriever reasons over what it
fetched, never sees the missing thing, and reports zero. The graph knows the
*boundary of the set*, so absence becomes a checkable proof object.

`qualify_clause` encodes Indian public-works eligibility rules (CPWD / NHAI
style) as traversals + exact arithmetic, and is the deterministic core the
Verifier re-runs without any LLM.
"""
from __future__ import annotations

import datetime as dt
import re

from bidbrain import config
from bidbrain.graph.client import Graph, day, iso
from bidbrain.tools.read import _edge, _node, _resolve, find_portfolio
from bidbrain.tools.registry import tool


def _ft_query(title: str) -> str:
    """Fulltext query for a work title: every alphanumeric token, AND-ed."""
    toks = [t for t in re.findall(r"[A-Za-z0-9]+", title) if t.lower() not in {"pkg", "the", "of", "and"}]
    return " ".join(toks)


# --------------------------------------------------------------------------
# proof of absence


@tool("prove_absence",
      """Prove which items in a scope LACK a required piece of evidence, with a proof object
      (universe, present, absent, and checks that the absence is real rather than an
      extraction miss). evidence='reference_letter' over works (scope by client / person /
      via_work / category); evidence='valid_credential' over people who led works in scope
      (credential_kind, valid_on date); evidence='performance_bond' over tenders (category).""",
      {"evidence": {"type": "string", "enum": ["reference_letter", "valid_credential", "performance_bond"]},
       "client": {"type": "string"}, "person": {"type": "string"}, "via_work": {"type": "string"},
       "category": {"type": "string"}, "credential_kind": {"type": "string"},
       "valid_on": {"type": "string"}},
      ["evidence"])
def prove_absence(g: Graph, evidence: str, client: str | None = None, person: str | None = None,
                  via_work: str | None = None, category: str | None = None,
                  credential_kind: str | None = None, valid_on: str | None = None) -> dict:
    given = {k for k, v in {"client": client, "person": person, "via_work": via_work, "category": category,
                            "credential_kind": credential_kind, "valid_on": valid_on}.items() if v}
    allowed = {"reference_letter": {"client", "person", "via_work", "category"},
               "valid_credential": {"client", "via_work", "category", "credential_kind", "valid_on"},
               "performance_bond": {"category"}}.get(evidence)
    if allowed is not None and given - allowed:
        raise ValueError(f"evidence='{evidence}' cannot be scoped by {sorted(given - allowed)}; "
                         f"supported: {sorted(allowed)}")
    if evidence == "reference_letter":
        return _absence_reference(g, client, person, via_work, category)
    if evidence == "valid_credential":
        if via_work and not client:     # 'the client behind that work', as find_portfolio does
            wk = _resolve(g, via_work, "work")[0]
            client = g.scalar("MATCH (:Work {key:$k})-[:FOR_CLIENT]->(c:Client) RETURN c.key", {"k": wk})
        return _absence_credential(g, client, category, credential_kind or "PMP", valid_on or config.AS_OF)
    if evidence == "performance_bond":
        return _absence_bond(g, category)
    raise ValueError(f"unknown evidence {evidence}")


def _absence_reference(g, client, person, via_work, category) -> dict:
    pf = find_portfolio(g, client=client, person=person, via_work=via_work,
                        categories=[category] if category else None)
    universe = pf["result"]["works"]
    keys = [w["key"] for w in universe]
    q_part = """MATCH (w:Work) WHERE w.key IN $keys
                OPTIONAL MATCH (w)-[h:HAS_REFERENCE]->(r:Document)
                RETURN w.key AS key, w.title AS title, w.views AS views,
                       collect({doc: r.doc_id, page: h.page}) AS refs"""
    rows = g.read(q_part, {"keys": keys})
    present, absent = [], []
    for r in rows:
        refs = [x for x in r["refs"] if x.get("doc")]
        (present if refs else absent).append({"key": r["key"], "title": r["title"], "letters": refs,
                                               "views": r["views"]})
    # check 1: every work in the boundary is attested by all three independent views
    triple = sum(1 for r in rows if len(r["views"] or []) == 3)
    # check 2: corpus closure — every reference letter in the estate is linked to some work,
    # so a work with no link cannot be explained by a letter we failed to match
    q_closure = """MATCH (d:Document {doc_type:'reference_letter'})
                   OPTIONAL MATCH (w:Work)-[:HAS_REFERENCE]->(d)
                   RETURN count(d) AS letters, sum(CASE WHEN w IS NULL THEN 1 ELSE 0 END) AS unlinked"""
    closure = g.read(q_closure)[0]
    # check 3: orphan search — fulltext for each missing title over reference-letter text
    q_orphan = """CALL db.idx.fulltext.queryNodes('Chunk', $q) YIELD node
                  WITH node WHERE node.doc_id STARTS WITH 'DOC-REF-'
                  RETURN collect(DISTINCT node.doc_id) AS hits"""
    for a in absent:
        hits = g.read(q_orphan, {"q": _ft_query(a["title"])})[0]["hits"]
        a["orphan_search_hits"] = hits
    proven = (closure["unlinked"] == 0 and triple == len(rows)
              and all(not a["orphan_search_hits"] for a in absent))
    ev = [e for e in pf["evidence"]]
    for p_ in present:
        for l in p_["letters"]:
            ev.append({"doc_id": l["doc"], "page": l["page"], "what": f"reference letter for {p_['title']}"})
    sg = pf["subgraph"]
    for a in absent:
        sg["nodes"].append(_node("Missing", f"ref:{a['key']}", "no reference letter", missing=True))
        sg["edges"].append(_edge(f"Work:{a['key']}", f"Missing:ref:{a['key']}", "HAS_REFERENCE?"))
    return {"result": {
        "claim": f"{len(absent)} of {len(rows)} works in scope have no reference letter on file",
        "verdict": "PROVEN" if proven else "UNPROVEN",
        "universe": len(rows), "present": len(present), "absent_count": len(absent),
        "absent": [{"key": a["key"], "title": a["title"], "orphan_search_hits": a["orphan_search_hits"]} for a in absent],
        "checks": {"boundary_attested_by_3_views": f"{triple}/{len(rows)}",
                   "corpus_closure": f"{int(closure['letters'] - closure['unlinked'])}/{int(closure['letters'])} letters linked",
                   "orphan_search": "no unlinked letter mentions a missing work" if all(
                       not a["orphan_search_hits"] for a in absent) else "possible unlinked letter found"},
        "scope": pf.get("scope")},
        "evidence": ev, "subgraph": sg, "cypher": [*pf["cypher"], q_part, q_closure, q_orphan]}


def _absence_credential(g, client, category, kind, valid_on) -> dict:
    ck = _resolve(g, client, "client")[0] if client else None
    cat = _resolve(g, category, "category")[0] if category else None
    q = """MATCH (p:Person)<-[:LED_BY]-(w:Work)-[:FOR_CLIENT]->(c:Client)
           WHERE ($c IS NULL OR c.key = $c) AND ($cat IS NULL OR w.category = $cat)
           WITH DISTINCT p
           OPTIONAL MATCH (p)-[:HOLDS]->(cr:Credential) WHERE toLower(cr.kind) CONTAINS toLower($kind)
           RETURN p.key AS key, p.name AS name,
                  collect({id: cr.key, valid_through: cr.valid_through, vday: cr.valid_through_day,
                           iday: cr.issued_day}) AS creds"""
    rows = g.read(q, {"c": ck, "cat": cat, "kind": kind})
    vd = day(valid_on)
    present, absent = [], []
    for r in rows:
        valid = [c for c in r["creds"] if c.get("id") and (c["vday"] or 0) >= vd
                 and (c.get("iday") is None or c["iday"] <= vd)]
        (present if valid else absent).append({"key": r["key"], "name": r["name"],
                                               "credentials": [c for c in r["creds"] if c.get("id")]})
    # Closed-world checks: a missing HOLDS edge or a missing date would turn "no record" into a false absence.
    q_check = """MATCH (cr:Credential) WHERE toLower(cr.kind) CONTAINS toLower($kind)
                 RETURN count(cr) AS total,
                        sum(CASE WHEN size([(h)-[:HOLDS]->(cr) | h]) = 0 THEN 1 ELSE 0 END) AS orphan,
                        sum(CASE WHEN cr.valid_through_day IS NULL THEN 1 ELSE 0 END) AS undated"""
    ck_row = g.read(q_check, {"kind": kind})[0]
    total, orphan, undated = (int(ck_row[k] or 0) for k in ("total", "orphan", "undated"))
    proven = orphan == 0 and undated == 0
    return {"result": {"claim": f"{len(absent)} of {len(rows)} project leads in scope hold no {kind} valid on {valid_on}",
                       "verdict": "PROVEN" if proven else "UNPROVEN", "universe": len(rows), "present": len(present),
                       "absent_count": len(absent), "absent": absent,
                       "checks": {"credential_closure": f"{total - orphan}/{total} {kind} credentials linked to a holder",
                                  "dated": f"{total - undated}/{total} carry a valid-through date"}},
            "cypher": [q, q_check]}


def _absence_bond(g, category) -> dict:
    cat = _resolve(g, category, "category")[0] if category else None
    q = """MATCH (t:Tender) WHERE $cat IS NULL OR t.category = $cat
           OPTIONAL MATCH (b:Bond)-[:SECURES]->(t)
           RETURN t.key AS tender, t.category AS category, collect(b.key) AS bonds"""
    rows = g.read(q, {"cat": cat})
    absent = [r for r in rows if not r["bonds"]]
    # Closed-world check: a Bond with no SECURES edge (or one dangling in scope) could be the "missing" bond.
    q_check = """MATCH (b:Bond) WHERE $cat IS NULL OR b.category = $cat
                 RETURN count(b) AS total,
                        sum(CASE WHEN size([(b)-[:SECURES]->(t:Tender) | t]) = 0 THEN 1 ELSE 0 END) AS orphan"""
    ck_row = g.read(q_check, {"cat": cat})[0]
    total, orphan = int(ck_row["total"] or 0), int(ck_row["orphan"] or 0)
    return {"result": {"claim": f"{len(absent)} of {len(rows)} tenders have no performance bond on file",
                       "verdict": "PROVEN" if orphan == 0 else "UNPROVEN", "universe": len(rows),
                       "absent_count": len(absent), "absent": absent,
                       "checks": {"bond_closure": f"{total - orphan}/{total} bonds in scope secure a tender"}},
            "cypher": [q, q_check]}


# --------------------------------------------------------------------------
# tender eligibility


def _day_before(iso: str) -> str:
    return (dt.date.fromisoformat(iso) - dt.timedelta(days=1)).isoformat()


def _years_before(as_of: str, years: int) -> str:
    d = dt.date.fromisoformat(as_of)
    try:
        return d.replace(year=d.year - years).isoformat()
    except ValueError:  # 29 Feb
        return d.replace(year=d.year - years, day=28).isoformat()


PARAMS = {"similar_works": {"categories", "estimated_cost", "min_value", "count", "options", "min_total",
                           "lookback_years", "govt_levels", "include_jv", "require_reference"},
          "turnover": {"years", "min_average", "min_each_year"},
          "key_personnel": {"credential", "count", "min_experience_years", "categories"},
          "certification": {"standard"},
          "years_in_business": {"min_years"}}
NEEDS = {"similar_works": ({"estimated_cost", "min_value", "options", "min_total"},
                          "estimated_cost, min_value, options or min_total"),
         "turnover": ({"min_average", "min_each_year"}, "min_average or min_each_year"),
         # without these the check passes for anyone (any certificate CONTAINS '', 0 years, a defaulted PMP)
         "key_personnel": ({"credential"}, "credential"),
         "certification": ({"standard"}, "standard"),
         "years_in_business": ({"min_years"}, "min_years")}


def _check_options(params: dict) -> list[dict]:
    """Either/or alternatives of a similar-works clause ("one work >= 50 Cr OR two works >= 40 Cr each"):
    a list of {count, min_value}, any one of which is enough. They replace min_value/count/estimated_cost
    (a clause with both would leave it unclear which reading applies, so that is an error, not a guess)."""
    opts = params.get("options")
    if not isinstance(opts, list) or not opts:
        raise ValueError("options must be a non-empty list of {count, min_value}")
    clash = sorted({"min_value", "count", "estimated_cost"} & set(params))
    if clash:
        raise ValueError(f"options cannot be combined with {clash}: each alternative carries its own count and min_value")
    out = []
    for o in opts:
        if not isinstance(o, dict) or set(o) - {"count", "min_value"} or not o.get("min_value"):
            raise ValueError(f"each option is {{count, min_value}}; got {o!r}")
        cnt, mv = int(o.get("count") or 1), int(o["min_value"])
        if cnt < 1 or mv <= 0:
            raise ValueError(f"option needs count >= 1 and min_value > 0; got {o!r}")
        out.append({"count": cnt, "min_value": mv})
    return out


@tool("qualify_clause",
      """Check one tender eligibility clause against the company's graph and return a verdict
      (PASS / FAIL / AT_RISK) with the works, people, statements or certificates that satisfy it,
      the gaps, and evidence pages. Clause kinds and params:
      similar_works {categories, ONE OF: estimated_cost (CPWD rule: 1 work >= 80% OR 2 >= 60% OR 3 >= 40%)
        | min_value + count | options [{count, min_value}, ...] (either/or alternatives, e.g. "one work
        >= 50 Cr or two works >= 40 Cr each": any one option passing is enough; the result names which),
        min_total (aggregate value: the works in scope must ALSO sum to at least this, e.g. "one work >= 30 Cr
        and a cumulative value of INR 500 Cr"; alone it is the whole requirement),
        lookback_years (default 7), govt_levels, include_jv (default true), require_reference (default false)};
      turnover {min_average (rupees) over last `years` FY (default 3) | min_each_year};
      key_personnel {credential (e.g. PMP), min_experience_years, count (default 1),
        categories (must have led such work)};
      certification {standard (e.g. 'ISO 9001')};
      years_in_business {min_years}.
      `bid_date` (ISO, default the corpus date) is when validity is judged.""",
      {"kind": {"type": "string", "enum": ["similar_works", "turnover", "key_personnel",
                                           "certification", "years_in_business"]},
       "params": {"type": "object"}, "bid_date": {"type": "string"}},
      ["kind", "params"])
def qualify_clause(g: Graph, kind: str, params: dict, bid_date: str | None = None) -> dict:
    # params is a free-form object in the tool schema, so a model can invent keys ("thresholds",
    # "min_experience"); an ignored key silently widens the check, so unknown keys are an error
    params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    unknown = sorted(set(params) - PARAMS.get(kind, set()))
    if unknown:
        raise ValueError(f"{kind} params take {sorted(PARAMS[kind])}; unknown: {unknown}")
    if kind in NEEDS and not (NEEDS[kind][0] & set(params)):
        raise ValueError(f"{kind} needs {NEEDS[kind][1]}")
    for k in ("min_years", "min_average", "min_each_year", "min_value", "estimated_cost"):
        if k in params:
            try:
                if int(params[k]) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a positive number; got {params[k]!r}") from None
    if kind == "similar_works" and "options" in params:
        params["options"] = _check_options(params)
    if kind == "similar_works" and "min_total" in params:
        try:
            if int(params["min_total"]) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"min_total must be a positive amount in rupees; got {params['min_total']!r}") from None
    bid_date = bid_date or config.AS_OF
    fn = {"similar_works": _q_similar, "turnover": _q_turnover, "key_personnel": _q_personnel,
          "certification": _q_cert, "years_in_business": _q_years}[kind]
    out = fn(g, dict(params or {}), bid_date)
    out["result"]["kind"] = kind
    out["result"]["bid_date"] = bid_date
    return out


def _q_similar(g, p, bid_date) -> dict:
    since = _years_before(bid_date, int(p.get("lookback_years", 7)))
    # CPWD: works completed in the N years ending on the bid date. find_portfolio's bounds are strict,
    # so the lower bound is the day before `since` (inclusive) and the bid date itself caps the window.
    pf = find_portfolio(g, categories=p.get("categories"), completed_after=_day_before(since),
                        completed_before=bid_date,
                        govt_levels=p.get("govt_levels"),
                        role=None if p.get("include_jv", True) else "Prime",
                        has_reference=True if p.get("require_reference") else None)
    works = pf["result"]["works"]
    # contested values: a work whose value has an open CONTRADICTS on it is credited but flagged
    contested = {r["key"] for r in g.read(
        """MATCH (c:Claim {predicate:'value', status:'contested'})-[:ABOUT]->(w:Work) RETURN DISTINCT w.key AS key""")}
    est = p.get("estimated_cost")
    extra: dict = {}
    if est:
        est = int(est)
        options = [(1, 0.8), (2, 0.6), (3, 0.4)]
        met = None
        for n, frac in options:
            qual = [w for w in works if w["value"] >= frac * est]
            if len(qual) >= n:
                met = {"option": f"{n} work(s) >= {int(frac * 100)}% of estimated cost "
                                 f"(>= {int(frac * est):,})", "works": qual[:n]}
                break
        near = sorted(works, key=lambda w: -w["value"])[:5]
        sat = met["works"] if met else []
        basis = met["option"] if met else (
            f"no option met: need 1 >= {int(0.8 * est):,} or 2 >= {int(0.6 * est):,} or 3 >= {int(0.4 * est):,}")
    elif p.get("options"):
        # either/or: the first alternative (in the tender's order) that the works meet is the one relied on
        checked, met = [], None
        for i, o in enumerate(p["options"]):
            qual = [w for w in works if w["value"] >= o["min_value"]]
            label = f"({'abcdefgh'[i]}) {o['count']} work(s) >= {o['min_value']:,}"
            checked.append({"option": label, "count": o["count"], "min_value": o["min_value"], "found": len(qual),
                            "passed": len(qual) >= o["count"]})
            if met is None and len(qual) >= o["count"]:
                met = {"option": label, "index": i, "works": qual[:o["count"]]}
        sat = met["works"] if met else []
        near = sorted(works, key=lambda w: -w["value"])[:3]     # the closest works to any alternative
        basis = ("either/or: " + "; ".join(f"{c['option']}: found {c['found']}" for c in checked)
                 + (f" -> option {met['option'][:3]} met" if met else " -> no alternative met"))
        extra = {"options_checked": checked, "option_met": met["option"] if met else None}
    elif p.get("min_value"):
        mv, cnt = int(p.get("min_value", 0)), int(p.get("count", 1))
        qual = [w for w in works if w["value"] >= mv]
        sat, met = qual[:cnt], len(qual) >= cnt
        near = sorted([w for w in works if w["value"] < mv], key=lambda w: -w["value"])[:3]
        basis = f"need {cnt} work(s) >= {mv:,}; found {len(qual)}"
    else:                                   # an aggregate-value requirement only
        sat, met, near, basis = [], True, [], ""
    if p.get("min_total"):
        # the aggregate value of the works in scope, on top of any single-work threshold: credited are the
        # works already relied on plus the largest others until the total is reached
        need_total = int(p["min_total"])
        total = sum(w["value"] for w in works)
        total_ok = total >= need_total
        chosen, run = list(sat), sum(w["value"] for w in sat)
        for w in sorted(works, key=lambda w: -w["value"]):
            if run >= need_total:
                break
            if all(w["key"] != c["key"] for c in chosen):
                chosen.append(w)
                run += w["value"]
        part = (f"cumulative value of {len(works)} work(s) in scope = {total:,} vs required {need_total:,}"
                + ("" if total_ok else f" (short by {need_total - total:,})"))
        basis = f"{basis}; {part}" if basis else part
        extra = {**extra, "cumulative_value": total, "min_total": need_total, "cumulative_met": total_ok}
        if met and total_ok:
            sat = chosen
        elif met and not total_ok:
            near = sorted(works, key=lambda w: -w["value"])[:5]
        met = bool(met) and total_ok
    passed = bool(met)
    risky = [w["key"] for w in sat if w["key"] in contested]
    verdict = "PASS" if passed and not risky else ("AT_RISK" if passed else "FAIL")
    return {"result": {"verdict": verdict, "basis": basis, "since": since,
                       "satisfied_by": sat, "near_misses": [] if passed else near,
                       "candidates": len(works), "contested_values": risky,
                       "gaps": [] if passed else [basis], **extra},
            "evidence": [e for e in pf["evidence"] if any(w["title"] in e["what"] for w in sat)] or pf["evidence"][:6],
            "subgraph": pf["subgraph"], "cypher": pf["cypher"]}


def _q_turnover(g, p, bid_date) -> dict:
    n = int(p.get("years", 3))
    q = """MATCH (f:FinancialStatement)-[:FOR_FY]->(y:FiscalYear)
           WHERE f.from_comparative_only = false AND y.start_year IN $years
           OPTIONAL MATCH (c:Claim {predicate:'revenue_operations'})-[:ABOUT]->(f)
           WITH f, y, collect({value: c.value, kind: c.source_kind, doc: c.source_doc, page: c.page,
                               status: c.status}) AS claims
           RETURN f.key AS key, f.fy AS fy, y.start_year AS start_year, f.revenue_operations AS revenue, claims
           ORDER BY y.start_year DESC"""
    # the FY in progress on the bid date never counts: the last N *completed* FYs (Apr-Mar) before it
    current = int(bid_date[:4]) if bid_date[5:7] > "03" else int(bid_date[:4]) - 1
    years = [current - i for i in range(1, n + 1)]
    rows = g.read(q, {"years": years})
    have = {r["start_year"] for r in rows if r["revenue"] is not None}
    missing = [f"{y}-{str(y + 1)[2:]}" for y in years if y not in have]
    revs = [r["revenue"] for r in rows if r["revenue"] is not None]
    avg = int(sum(revs) / len(revs)) if revs else 0
    contested = [r["fy"] for r in rows if any(c.get("status") == "contested" for c in r["claims"])]
    restated = [{"fy": r["fy"], "kept": r["revenue"],
                 "restated_as": sorted({c["value"] for c in r["claims"] if c.get("status") == "rejected"})}
                for r in rows if any(c.get("status") == "rejected" for c in r["claims"])]
    if p.get("min_each_year"):
        bar = int(p["min_each_year"])
        failing = [r["fy"] for r in rows if (r["revenue"] or 0) < bar]
        passed, basis = bool(revs) and not failing, f"each of last {n} FY >= {bar:,}; failing: {failing or 'none'}"
    else:
        bar = int(p.get("min_average", 0))
        passed, basis = bool(revs) and avg >= bar, f"average of last {n} FY = {avg:,} vs required {bar:,}"
    if missing:
        basis += f"; no audited statement for FY {', '.join(missing)} (averaged over {len(revs)} of {n} years)"
    # a missing year cannot be proven either way: a pass on the years we have is only AT_RISK
    verdict = "PASS" if passed and not contested and not missing else ("AT_RISK" if passed else "FAIL")
    ev = [{"doc_id": c["doc"], "page": c["page"], "what": f"FY{r['fy']} revenue {c['value']:,} ({c['kind']})"}
          for r in rows for c in r["claims"] if c.get("doc")]
    return {"result": {"verdict": verdict, "basis": basis, "average": avg,
                       "years": [{"fy": r["fy"], "revenue": r["revenue"],
                                  "competing_values": sorted({c["value"] for c in r["claims"] if c.get("value") is not None})}
                                 for r in rows],
                       "contested_years": contested, "resolved_restatements": restated,
                       "missing_years": missing,
                       "gaps": ([] if passed else [basis]) + [f"no audited statement for FY {m}" for m in missing]},
            "evidence": ev, "cypher": [q]}


def _q_personnel(g, p, bid_date) -> dict:
    cred, need = p.get("credential", "PMP"), int(p.get("count", 1))
    q = """MATCH (pp:Person)-[:HOLDS]->(c:Credential)-[e:EVIDENCED_BY]->(d:Document)
           WHERE toLower(c.kind) CONTAINS toLower($cred)
             AND ($minx IS NULL OR pp.experience_years >= $minx)
           OPTIONAL MATCH (pp)<-[:LED_BY]-(w:Work) WHERE $cats IS NULL OR w.category IN $cats
           RETURN pp.key AS key, pp.name AS name, pp.designation AS designation,
                  pp.experience_years AS experience_years, c.key AS credential_id,
                  c.valid_through AS valid_through, c.valid_through_day AS vday, c.issued_day AS iday,
                  d.doc_id AS doc, e.page AS page, count(w) AS similar_led
           ORDER BY similar_led DESC, experience_years DESC"""
    cats = [_resolve(g, c, "category")[0] for c in p["categories"]] if p.get("categories") else None
    rows = g.read(q, {"cred": cred, "minx": p.get("min_experience_years"), "cats": cats})
    bd = day(bid_date)
    if cats:
        rows = [r for r in rows if r["similar_led"] > 0]
    held = [r for r in rows if r["iday"] is None or r["iday"] <= bd]          # not yet issued => not held
    valid = [r for r in held if (r["vday"] or 0) >= bd]
    lapsing = [r for r in held if (r["vday"] or 0) < bd]
    soon = [r for r in valid if (r["vday"] or 0) < bd + 90]
    passed = len(valid) >= need
    # AT_RISK when the strongest candidates (ranked by similar work led, then experience) lapse within
    # 90 days of the bid date — the bid would be filed on people who are about to stop qualifying.
    top = valid[:need]
    soon_keys = {r["key"] for r in soon}
    alternates = [r for r in valid if r["key"] not in soon_keys][:need]
    verdict = ("AT_RISK" if any(r["key"] in soon_keys for r in top) else "PASS") if passed else "FAIL"
    basis = (f"{len(valid)} people hold a {cred} valid on {bid_date}"
             + (f" with >= {p['min_experience_years']} yrs" if p.get("min_experience_years") else "")
             + (f" and led {', '.join(cats)} work" if cats else "") + f"; need {need}")
    ev = [{"doc_id": r["doc"], "page": r["page"], "what": f"{r['name']} {cred} valid to {r['valid_through']}"}
          for r in valid[:8]]
    sg = {"nodes": [_node("Person", r["key"], r["name"]) for r in valid[:8]]
          + [_node("Credential", r["credential_id"], f"{cred} {r['credential_id']}") for r in valid[:8]],
          "edges": [_edge(f"Person:{r['key']}", f"Credential:{r['credential_id']}", "HOLDS") for r in valid[:8]]}
    return {"result": {"verdict": verdict, "basis": basis, "n_qualified": len(valid), "satisfied_by": valid[:max(need, 5)],
                       "lapsed_before_bid": [{"name": r["name"], "valid_through": r["valid_through"]} for r in lapsing],
                       "expiring_within_90d": [{"name": r["name"], "valid_through": r["valid_through"]} for r in soon],
                       "safe_alternates": [{"name": r["name"], "valid_through": r["valid_through"],
                                            "similar_led": r["similar_led"]} for r in alternates],
                       "gaps": [] if passed else [basis]},
            "evidence": ev, "subgraph": sg, "cypher": [q]}


def _q_cert(g, p, bid_date) -> dict:
    q = """MATCH (c:Certification)-[e:EVIDENCED_BY]->(d:Document) WHERE toLower(c.standard) CONTAINS toLower($s)
           RETURN c.key AS key, c.standard AS standard, c.body AS body, c.valid_until AS valid_until,
                  c.valid_until_day AS vday, c.initial_date AS initial, d.doc_id AS doc"""
    rows = g.read(q, {"s": p.get("standard", "")})
    ok = [r for r in rows if (r["vday"] or 0) >= day(bid_date) and (r["initial"] or "") <= bid_date]
    return {"result": {"verdict": "PASS" if ok else "FAIL",
                       "basis": f"{p.get('standard')} valid on {bid_date}: {bool(ok)}",
                       "satisfied_by": ok, "gaps": [] if ok else [f"no valid {p.get('standard')}"]},
            "evidence": [{"doc_id": r["doc"], "page": 1, "what": f"{r['standard']} valid to {r['valid_until']}"} for r in ok],
            "cypher": [q]}


def _q_years(g, p, bid_date) -> dict:
    r = g.read("MATCH (c:Company) RETURN c.incorporated_year AS y, c.cin AS cin, c.cin_doc AS doc")[0]
    yrs = int(bid_date[:4]) - int(r["y"] or 0)
    need = int(p.get("min_years", 0))
    return {"result": {"verdict": "PASS" if yrs >= need else "FAIL",
                       "basis": f"incorporated {r['y']} (from CIN {r['cin']}): {yrs} years vs required {need}",
                       "gaps": [] if yrs >= need else [f"{yrs} < {need} years"]},
            "evidence": [{"doc_id": r["doc"], "page": 1, "what": f"CIN {r['cin']} encodes incorporation year {r['y']}"}]}
