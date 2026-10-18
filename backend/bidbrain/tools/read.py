"""Read tools: multi-hop traversals the agents call instead of writing Cypher.

Design rules (each one exists because the alternative failed somewhere):
* Arithmetic never happens in the LLM. `aggregate` takes *work keys*, re-reads
  the values from the graph, and computes in exact integers — so a model cannot
  misadd, and cannot smuggle in a number it hallucinated.
* Entity arguments accept a key *or* a name; anything not exact goes through
  `resolve_entity` and the tool reports `resolved_as`, so a silent mis-resolve
  is visible in the trace.
* Every result carries `evidence` (doc + page) and a `subgraph` for the UI.
"""
from __future__ import annotations

import re
import statistics

from bidbrain import config
from bidbrain.graph.client import Graph, day, iso
from bidbrain.ingest import aliases
from bidbrain.tools.registry import tool

# --------------------------------------------------------------------------
# helpers


def _node(label: str, key: str, name: str | None = None, **extra) -> dict:
    return {"id": f"{label}:{key}", "label": label, "name": name or key, **extra}


def _edge(a: str, b: str, t: str) -> dict:
    return {"source": a, "target": b, "type": t}


_CANDS: dict[tuple[str, str], dict] = {}


def _candidates(g: Graph, kind: str) -> dict[str, list[str]]:
    ck = (g.name, kind)
    if ck in _CANDS:
        return _CANDS[ck]
    if kind == "client":
        rows = g.read("MATCH (c:Client) RETURN c.key AS k, c.name AS n, c.aliases AS a")
        out = {r["k"]: [r["n"], *(r["a"] or [])] for r in rows}
    elif kind == "person":
        rows = g.read("MATCH (p:Person) RETURN p.key AS k, p.name AS n")
        out = {r["k"]: [r["n"]] for r in rows}
    elif kind == "work":
        rows = g.read("MATCH (w:Work) RETURN w.key AS k, w.title AS n, w.cert_ref AS c")
        out = {r["k"]: [r["n"], r["c"] or ""] for r in rows}
    elif kind == "category":
        rows = g.read("MATCH (c:Category) RETURN c.key AS k")
        out = {r["k"]: [r["k"]] for r in rows}
    elif kind == "tender":
        rows = g.read("MATCH (t:Tender) RETURN t.key AS k")
        out = {r["k"]: [r["k"]] for r in rows}
    else:
        raise ValueError(f"unknown kind {kind}")
    _CANDS[ck] = out
    return out


def _resolve(g: Graph, text: str | None, kind: str) -> tuple[str | None, dict | None]:
    """Exact key/name first, then Pkg-number (works), then fuzzy (non-ambiguous)."""
    if not text:
        return None, None
    cands = _candidates(g, kind)
    if text in cands:
        return text, None
    low = text.lower().strip()
    for k, forms in cands.items():
        if low == forms[0].lower():
            return k, None
    if kind == "work":
        m = re.search(r"pkg[\s\-]*0*(\d+)", low)
        if m:
            hits = [k for k, f in cands.items() if re.search(rf"pkg-0*{m.group(1)}$", f[0].lower())]
            if len(hits) == 1:
                return hits[0], {"via": "package number", "matched": cands[hits[0]][0]}
            if not hits:    # never let "Pkg-999" fuzzy-match Pkg-99
                raise LookupError(f"no work with package number {m.group(1)}")
    ranked = aliases.resolve(text, cands, limit=3)
    if ranked and ranked[0]["score"] >= RESOLVE_MIN and not ranked[0].get("ambiguous") \
            and _strict_score(text, cands[ranked[0]["key"]]) >= RESOLVE_MIN:
        return ranked[0]["key"], {"via": "fuzzy", "matched": ranked[0]["name"], "score": ranked[0]["score"]}
    if not any(_strict_score(text, cands[r["key"]]) >= RESOLVE_MIN for r in ranked):
        raise LookupError(f"no {kind} named like '{text}' in the graph; nearest (not matches): "
                          + ", ".join(f"{r['name']} ({r['score']})" for r in ranked))
    raise LookupError(f"could not resolve {kind} '{text}' unambiguously; candidates: "
                      + ", ".join(f"{r['name']} ({r['score']})" for r in ranked))


_WORK_RET = """w.key AS key, w.title AS title, w.value AS value, w.completed AS completed,
    w.completed_year AS year, w.category AS category, w.grading AS grading, w.role AS role,
    w.has_reference AS has_reference, c.name AS client, c.key AS client_key,
    p.name AS lead, p.key AS lead_key, d.doc_id AS doc, e.page AS page"""


def _works_payload(rows: list[dict], scope_nodes: list[dict], scope_edges: list[dict]) -> dict:
    nodes, edges, ev = list(scope_nodes), list(scope_edges), []
    seen = {n["id"] for n in nodes}
    for r in rows:
        wid = f"Work:{r['key']}"
        nodes.append(_node("Work", r["key"], r["title"], value=r["value"], category=r["category"]))
        cid = f"Client:{r['client_key']}"
        if cid not in seen:
            nodes.append(_node("Client", r["client_key"], r["client"]))
            seen.add(cid)
        edges.append(_edge(wid, cid, "FOR_CLIENT"))
        if r.get("lead_key"):
            pid = f"Person:{r['lead_key']}"
            if pid not in seen:
                nodes.append(_node("Person", r["lead_key"], r["lead"]))
                seen.add(pid)
            edges.append(_edge(wid, pid, "LED_BY"))
        if r.get("doc"):
            nodes.append(_node("Document", r["doc"], r["doc"], page=r.get("page")))
            edges.append(_edge(wid, f"Document:{r['doc']}", "EVIDENCED_BY"))
            ev.append({"doc_id": r["doc"], "page": r.get("page"),
                       "what": f"{r['title']}: value {r['value']:,}, completed {r['completed']}"})
    return {"nodes": nodes, "edges": edges}, ev


# --------------------------------------------------------------------------
# entity resolution


# Below this score a mention is "not in the graph" (the same bar `_resolve` uses before it will act on a name).
RESOLVE_MIN = 70.0


def _strict_score(text: str, forms: list[str]) -> float:
    """aliases.resolve's score without its partial match against a form shorter than 4 characters: a
    two-letter alias ('ai') is contained in almost any query ('NHAI'), which is not a match."""
    from rapidfuzz import fuzz
    q, nq = (text or "").lower().strip(), aliases.norm((text or "").lower().strip())
    best = 0.0
    for f in forms:
        fl = (f or "").lower()
        if not fl:
            continue
        if nq and nq == aliases.norm(fl):
            return 100.0
        best = max(best, fuzz.token_set_ratio(q, fl) * 0.97,
                   fuzz.partial_ratio(q, fl) * 0.9 if len(q) >= 4 and len(fl) >= 4 else 0)
    return round(best, 1)


@tool("resolve_entity",
      """Resolve a free-text mention to graph entities (client, person, work, category, tender).
      Handles informal forms like 'phed odisha', 'gujarat pw', 'Pkg-59'. Returns ranked
      candidates; if `ambiguous` is true, narrow with more context instead of guessing. When nothing
      scores at least 70 the entity is not in the graph: `result` is empty, `not_found` is true and
      `nearest` lists the closest names (which are NOT matches).""",
      {"text": {"type": "string"},
       "kind": {"type": "string", "enum": ["client", "person", "work", "category", "tender"]}},
      ["text", "kind"])
def resolve_entity(g: Graph, text: str, kind: str) -> dict:
    cands = _candidates(g, kind)
    if kind == "work" and (m := re.search(r"pkg[\s\-]*0*(\d+)", text.lower())):
        hits = [k for k, f in cands.items() if re.search(rf"pkg-0*{m.group(1)}$", f[0].lower())]
        if len(hits) == 1:
            return {"result": [{"key": hits[0], "name": cands[hits[0]][0], "score": 100.0}]}
        if not hits:
            return {"result": [], "not_found": True, "nearest": [],
                    "note": f"no work with package number {m.group(1)}"}
    ranked = aliases.resolve(text, cands, limit=5)
    kept = [r for r in ranked if _strict_score(text, cands[r["key"]]) >= RESOLVE_MIN]
    if not kept:
        near = sorted(({"key": k, "name": f[0], "score": _strict_score(text, f)} for k, f in cands.items()),
                      key=lambda r: -r["score"])[:3]
        return {"result": [], "not_found": True, "nearest": near,
                "note": f"no {kind} named like '{text}' in the graph (best score below {RESOLVE_MIN:.0f})"}
    if len(kept) == 1:
        kept[0].pop("ambiguous", None)
    elif kept[0].get("ambiguous"):
        return {"result": kept, "ambiguous": True,
                "note": f"'{text}' matches {len(kept)} {kind}s about equally: ask which one, or narrow the name; "
                        "do not answer for one of them, and do not conclude that none exists"}
    return {"result": kept}


_DOC_TYPES: dict[str, list[str]] = {}


def doc_types(g: Graph) -> list[str]:
    if g.name not in _DOC_TYPES:
        _DOC_TYPES[g.name] = sorted(r["t"] for r in g.read("MATCH (d:Document) WHERE d.doc_type IS NOT NULL "
                                                           "RETURN DISTINCT d.doc_type AS t"))
    return _DOC_TYPES[g.name]


def doc_type_error(g: Graph, doc_type: str | None) -> str | None:
    """Why `doc_type` is not a document type in this graph, or None. A filter on an unknown type matches no
    document, which a search tool would otherwise report as an empty, confident result."""
    if not doc_type:
        return None
    valid = doc_types(g)
    return None if doc_type in valid else f"unknown doc_type {doc_type!r}; use one of {valid}"


@tool("fact_history",
      """Why we believe a fact: every competing claim about it (value, source document + page, issuer,
      authority, status) and the decisions that settled it (policy, rationale, who decided, any learned rule).
      Use for 'why is X's value Y', 'what changed', 'which document do we trust'. kind: work|person|bond|
      credential|financial_statement; predicate optional (value, completed, grading, role, valid_through, ...).""",
      {"key": {"type": "string", "description": "entity key or name, e.g. 'Pkg-113'"},
       "kind": {"type": "string", "enum": ["work", "person", "bond", "credential", "financial_statement"]},
       "predicate": {"type": "string"}},
      ["key", "kind"])
def fact_history(g: Graph, key: str, kind: str, predicate: str | None = None) -> dict:
    label = {"work": "Work", "person": "Person", "bond": "Bond", "credential": "Credential",
             "financial_statement": "FinancialStatement"}[kind]
    if kind in ("work", "person"):
        key, res = _resolve(g, key, kind)
    else:
        res = None
    q = """MATCH (c:Claim)-[:ABOUT]->(s {key:$k}) WHERE ($p IS NULL OR c.predicate = $p)
           OPTIONAL MATCH (c)-[f:FROM]->(d:Document)
           RETURN c.id AS claim, c.predicate AS predicate, c.value_str AS value, c.source_kind AS source,
                  c.issuer AS issuer, c.authority AS authority, c.status AS status, d.doc_id AS doc, f.page AS page
           ORDER BY predicate, authority DESC"""
    claims = g.read(q, {"k": key, "p": predicate})
    dq = """MATCH (dec:Decision {subject_key:$k}) WHERE ($p IS NULL OR dec.predicate = $p)
            OPTIONAL MATCH (dec)-[:DECIDED_BY]->(a) OPTIONAL MATCH (dec)-[:SELECTS]->(w:Claim)
            OPTIONAL MATCH (dec)-[:APPLIED_RULE]->(r:PolicyRule)
            RETURN dec.id AS decision, dec.predicate AS predicate, dec.policy AS policy, dec.rationale AS rationale,
                   dec.chosen_value AS chosen, labels(a)[0] AS decided_by_kind, a.key AS decided_by,
                   w.source_doc AS winning_doc, r.id AS learned_rule, dec.at AS at
            ORDER BY at DESC"""
    decisions = g.read(dq, {"k": key, "p": predicate})
    ev = [{"doc_id": c["doc"], "page": c["page"], "what": f"{c['predicate']} = {c['value']} ({c['source']}, {c['status']})"}
          for c in claims if c.get("doc")]
    sg = {"nodes": [_node(label, key, key)] + [_node("Decision", d["decision"], f"{d['policy']}") for d in decisions],
          "edges": [_edge(f"Decision:{d['decision']}", f"{label}:{key}", "ABOUT") for d in decisions]}
    return {"result": {"subject": f"{label}:{key}", "claims": claims, "decisions": decisions,
                       "note": None if decisions else "no competing claims: the value comes from a single source"},
            "resolved_as": res, "evidence": ev, "subgraph": sg, "cypher": [q, dq]}


@tool("get_entity",
      """Fetch one entity's properties and its direct relationships (1 hop), e.g. a work's
      client, lead, documents; a person's credentials and led works; a client's works.""",
      {"key": {"type": "string", "description": "entity key or name"},
       "kind": {"type": "string", "enum": ["client", "person", "work", "tender", "bond",
                                            "credential", "contract", "document"]}},
      ["key", "kind"])
def get_entity(g: Graph, key: str, kind: str) -> dict:
    label = {"client": "Client", "person": "Person", "work": "Work", "tender": "Tender",
             "bond": "Bond", "credential": "Credential", "contract": "Contract",
             "document": "Document"}[kind]
    res = None
    if kind in ("client", "person", "work", "tender"):
        key, res = _resolve(g, key, kind)
    q = (f"MATCH (n:{label} {{key:$k}}) OPTIONAL MATCH (n)-[r]-(m) WHERE NOT m:Claim AND NOT m:Chunk "
         "RETURN n AS node, collect({rel:type(r), out: startNode(r) = n, label: labels(m)[0], "
         "key: m.key, name: coalesce(m.name, m.title, m.doc_id, m.key)})[0..60] AS links")
    rows = g.read(q, {"k": key})
    if not rows:
        raise LookupError(f"no {label} with key {key}")
    node = {k: v for k, v in rows[0]["node"].items() if k not in ("embedding", "_id", "_labels")}
    links = rows[0]["links"]
    for l in links:                 # a Bid is a past tender submission, never an open bid
        if l.get("label") == "Bid":
            l["kind"] = "past_bid"
    sg = {"nodes": [_node(label, key, node.get("name") or node.get("title"))]
          + [_node(l["label"], l["key"], l["name"]) for l in links if l.get("key")],
          "edges": [_edge(f"{label}:{key}", f"{l['label']}:{l['key']}", l["rel"]) if l["out"]
                    else _edge(f"{l['label']}:{l['key']}", f"{label}:{key}", l["rel"])
                    for l in links if l.get("key")]}
    return {"result": {"entity": node, "links": links}, "resolved_as": res, "subgraph": sg, "cypher": [q]}


@tool("list_cases",
      """This workspace's bid qualification cases: the open bids the tender team is working on (not the
      past Bid submissions, which are history). For each case: case_id, title, tender, bid date, status,
      the team's current recommendation (BID / BID WITH FIXES / NO-BID / NEEDS REVIEW), the recommendation a
      person approved (and whether the current one differs from it), clause verdict counts and the open
      (unresolved) findings. Optional status filter (e.g. 'open'). Read one case in full with case_get.""",
      {"status": {"type": "string"}})
def list_cases(g: Graph, status: str | None = None) -> dict:
    q = """MATCH (c:Case) WHERE $s IS NULL OR c.status = $s
           OPTIONAL MATCH (c)-[:HAS_CLAUSE]->(k:Clause)
           WITH c, collect(k.verdict) AS verdicts
           RETURN c.id AS case_id, c.title AS title, c.tender_ref AS tender, c.bid_date AS bid_date,
                  c.status AS status, c.verdict AS recommendation, c.approved_recommendation AS approved,
                  verdicts, c.created_at AS created_at
           ORDER BY created_at DESC"""
    rows = g.read(q, {"s": status})
    fq = """MATCH (c:Case)-[:HAS_CLAUSE]->(k:Clause)-[:HAS_FINDING]->(f:Finding)
            WHERE c.id IN $ids AND coalesce(f.status, 'open') <> 'resolved'
            RETURN c.id AS case_id, k.ref AS clause, f.kind AS kind, f.severity AS severity, f.text AS text
            ORDER BY clause"""
    found = g.read(fq, {"ids": [r["case_id"] for r in rows]}) if rows else []
    cases = []
    for r in rows:
        counts: dict[str, int] = {}
        for v in r.pop("verdicts") or []:
            counts[v] = counts.get(v, 0) + 1
        r.pop("created_at", None)
        cases.append({**r, "kind": "case", "clause_verdicts": counts,
                      "changed_since_approval": bool(r["approved"]) and r["approved"] != r["recommendation"],
                      "open_findings": [{k: (f[k][:240] if k == "text" and f[k] else f[k])
                                         for k in ("clause", "kind", "severity", "text")}
                                        for f in found if f["case_id"] == r["case_id"]]})
    # tender status ('open' / 'closed', with its basis and as-of): "open tenders" are answered from this
    tq = """MATCH (t:Tender) RETURN t.key AS tender, coalesce(t.title, t.name, t.key) AS title, t.status AS status,
                   t.corpus_status AS corpus_status, t.status_basis AS status_basis, t.status_as_of AS status_as_of
            ORDER BY tender"""
    tenders = g.read(tq)
    for c in cases:
        t = next((t for t in tenders if t["tender"] and t["tender"] == c.get("tender")), None)
        if t:
            c["tender_status"] = {k: t[k] for k in ("status", "corpus_status", "status_basis", "status_as_of")}
    sg = {"nodes": [_node("Case", c["case_id"], c["title"]) for c in cases], "edges": []}
    return {"result": {"n": len(cases), "cases": cases,
                       "open_tenders": [t for t in tenders if t["status"] == "open"],
                       "tender_status_counts": {s: sum(1 for t in tenders if t["status"] == s)
                                                for s in sorted({str(t["status"]) for t in tenders})},
                       "note": None if cases else "no qualification cases in this workspace yet (run a tender "
                                                  "on the Qualify page); past Bid submissions are not cases"},
            "subgraph": sg, "cypher": [q, fq, tq]}


# --------------------------------------------------------------------------
# portfolio traversal


_PF_WHERE = """($client IS NULL OR c.key = $client)
  AND ($person IS NULL OR p.key = $person)
  AND ($cats IS NULL OR w.category IN $cats)
  AND ($xcats IS NULL OR NOT w.category IN $xcats)
  AND ($min IS NULL OR w.value >= $min) AND ($max IS NULL OR w.value <= $max)
  AND ($after IS NULL OR w.completed_day > $after)
  AND ($before IS NULL OR w.completed_day < $before)
  AND ($years IS NULL OR w.completed_year IN $years)
  AND ($role IS NULL OR w.role = $role)
  AND ($has_ref IS NULL OR w.has_reference = $has_ref)
  AND ($levels IS NULL OR c.govt_level IN $levels)"""

FIND_PORTFOLIO = f"""
MATCH (w:Work)-[:FOR_CLIENT]->(c:Client), (w)-[:LED_BY]->(p:Person),
      (w)-[e:EVIDENCED_BY {{kind:'completion_certificate'}}]->(d:Document)
WHERE {_PF_WHERE}
RETURN {_WORK_RET}
ORDER BY w.value DESC"""

# via_work in one query: anchor work -> its client -> every work for that client -> its certificate. The second
# MATCH is OPTIONAL so a filter that leaves nothing still returns the anchor and client (one row, w null).
FIND_PORTFOLIO_VIA = f"""
MATCH (anchor:Work {{key:$via}})-[:FOR_CLIENT]->(c:Client)
OPTIONAL MATCH (w:Work)-[:FOR_CLIENT]->(c), (w)-[:LED_BY]->(p:Person),
      (w)-[e:EVIDENCED_BY {{kind:'completion_certificate'}}]->(d:Document)
WHERE {_PF_WHERE}
RETURN anchor.key AS anchor_key, anchor.title AS anchor_title, {_WORK_RET}
ORDER BY w.value DESC"""


@tool("find_portfolio",
      """Find completed works. Scope by `client`, by `person` (works they led), or by `via_work`
      (= the whole portfolio of the client who commissioned that work — the usual multi-hop
      'starting from X's project, what about that client' pattern). Optional filters: categories,
      exclude_categories, min_value/max_value (rupees, inclusive), completed_after/completed_before
      (ISO date, strict), years, role ('Prime'|'JV Partner'), has_reference, govt_levels
      (central|state|psu|municipal|private). Returns works with value, date, category and the
      completion-certificate page that proves each one.""",
      {"client": {"type": "string"}, "person": {"type": "string"}, "via_work": {"type": "string"},
       "categories": {"type": "array", "items": {"type": "string"}},
       "exclude_categories": {"type": "array", "items": {"type": "string"}},
       "min_value": {"type": "integer"}, "max_value": {"type": "integer"},
       "completed_after": {"type": "string"}, "completed_before": {"type": "string"},
       "years": {"type": "array", "items": {"type": "integer"}},
       "role": {"type": "string", "enum": ["Prime", "JV Partner"]}, "has_reference": {"type": "boolean"},
       "govt_levels": {"type": "array", "items": {"type": "string",
                                                   "enum": ["central", "state", "psu", "municipal", "private"]}}})
def find_portfolio(g: Graph, client: str | None = None, person: str | None = None,
                   via_work: str | None = None, categories: list | None = None,
                   exclude_categories: list | None = None, min_value: int | None = None,
                   max_value: int | None = None, completed_after: str | None = None,
                   completed_before: str | None = None, years: list | None = None,
                   role: str | None = None, has_reference: bool | None = None,
                   govt_levels: list | None = None) -> dict:
    resolved, scope_nodes, scope_edges, cy = {}, [], [], []
    wk = None
    if via_work:
        wk, r = _resolve(g, via_work, "work")
        resolved["via_work"] = r or wk
        client = None                      # the query reaches the client through the anchor work
    elif client:
        client, r = _resolve(g, client, "client")
        resolved["client"] = r or client
    if person:
        person, r = _resolve(g, person, "person")
        resolved["person"] = r or person
    norm_cats = lambda xs: [_resolve(g, c, "category")[0] for c in xs] if xs else None
    params = {"client": client, "person": person, "cats": norm_cats(categories),
              "xcats": norm_cats(exclude_categories), "min": min_value, "max": max_value,
              "after": day(completed_after), "before": day(completed_before),
              "years": [int(y) for y in years] if years else None, "role": role,
              "has_ref": has_reference, "levels": govt_levels}
    if wk:
        rows = g.read(FIND_PORTFOLIO_VIA, {**params, "via": wk})
        if not rows:
            raise LookupError(f"work {wk} has no client")
        client = params["client"] = rows[0]["client_key"]
        scope_nodes.append(_node("Work", wk, rows[0]["anchor_title"], role="anchor"))
        scope_edges.append(_edge(f"Work:{wk}", f"Client:{client}", "FOR_CLIENT"))
        rows = [r for r in rows if r["key"] is not None]
        cy.append(FIND_PORTFOLIO_VIA)
    else:
        rows = g.read(FIND_PORTFOLIO, params)
        cy.append(FIND_PORTFOLIO)
    sg, ev = _works_payload(rows, scope_nodes, scope_edges)
    return {"result": {"n": len(rows), "total_value": sum(r["value"] or 0 for r in rows),
                       "works": [{k: r[k] for k in ("key", "title", "value", "completed", "category",
                                                     "grading", "role", "has_reference", "client", "lead")}
                                 for r in rows]},
            "scope": {k: v for k, v in params.items() if v is not None},
            "resolved_as": resolved, "evidence": ev, "subgraph": sg, "cypher": cy}


# --------------------------------------------------------------------------
# exact arithmetic over keys


@tool("aggregate",
      """Exact arithmetic over a set of works, by key (values are re-read from the graph — never
      pass numbers). ops: sum, count, average (truncated to int), median, mean_median_gap
      (mean − median; signed=true keeps the sign), max, min, rank_gap (largest − second largest),
      top_two_sum, distinct_categories, distinct_clients, distinct_leads, category_difference
      (needs category_a, category_b; absolute difference of their sums), year_difference (needs
      year_a, year_b; |sum(year_b) − sum(year_a)|), gap_to_threshold (needs threshold;
      max(threshold − sum, 0)), referenced_share (% of works with a reference letter, 2dp;
      absent=true gives the % without), absence_count (works with no reference letter).""",
      {"work_keys": {"type": "array", "items": {"type": "string"}},
       "op": {"type": "string"}, "signed": {"type": "boolean"},
       "category_a": {"type": "string"}, "category_b": {"type": "string"},
       "year_a": {"type": "integer"}, "year_b": {"type": "integer"},
       "threshold": {"type": "integer"}, "absent": {"type": "boolean"}},
      ["work_keys", "op"])
def aggregate(g: Graph, work_keys: list, op: str, signed: bool = False,
              category_a: str | None = None, category_b: str | None = None,
              year_a: int | None = None, year_b: int | None = None,
              threshold: int | None = None, absent: bool = False) -> dict:
    q = """MATCH (w:Work)-[:FOR_CLIENT]->(c:Client), (w)-[:LED_BY]->(p:Person) WHERE w.key IN $keys
           RETURN w.key AS key, w.value AS value, w.category AS category, w.completed_year AS year,
                  w.has_reference AS has_reference, c.key AS client, p.key AS lead"""
    rows = g.read(q, {"keys": list(work_keys)})
    missing = sorted(set(work_keys) - {r["key"] for r in rows})
    vals = [r["value"] for r in rows if r["value"] is not None]
    detail: dict = {}
    if op == "sum":
        v = sum(vals)
    elif op == "count":
        v = len(rows)
    elif op == "average":
        v = int(sum(vals) / len(vals)) if vals else 0
    elif op == "median":
        v = int(round(statistics.median(vals))) if vals else 0
    elif op == "mean_median_gap":
        gap = statistics.fmean(vals) - statistics.median(vals) if vals else 0
        detail = {"mean": statistics.fmean(vals) if vals else 0, "median": statistics.median(vals) if vals else 0}
        v = int(round(gap if signed else abs(gap)))
    elif op == "max":
        v = max(vals) if vals else 0
    elif op == "min":
        v = min(vals) if vals else 0
    elif op == "rank_gap":
        s = sorted(vals, reverse=True)
        v = s[0] - s[1] if len(s) > 1 else 0
        detail = {"largest": s[0] if s else None, "second": s[1] if len(s) > 1 else None}
    elif op == "top_two_sum":
        v = sum(sorted(vals, reverse=True)[:2])
    elif op == "distinct_categories":
        v = len({r["category"] for r in rows if r["category"]})
        detail = {"categories": sorted({r["category"] for r in rows if r["category"]})}
    elif op == "distinct_clients":
        v = len({r["client"] for r in rows})
    elif op == "distinct_leads":
        v = len({r["lead"] for r in rows})
    elif op == "category_difference":
        a, b = _resolve(g, category_a, "category")[0], _resolve(g, category_b, "category")[0]
        sa = sum(r["value"] or 0 for r in rows if r["category"] == a)
        sb = sum(r["value"] or 0 for r in rows if r["category"] == b)
        detail, v = {a: sa, b: sb}, abs(sa - sb)
    elif op == "year_difference":
        sa = sum(r["value"] or 0 for r in rows if r["year"] == int(year_a))
        sb = sum(r["value"] or 0 for r in rows if r["year"] == int(year_b))
        detail, v = {str(year_a): sa, str(year_b): sb}, abs(sb - sa)
    elif op == "gap_to_threshold":
        detail = {"current": sum(vals), "threshold": threshold}
        v = max(int(threshold) - sum(vals), 0)
    elif op == "referenced_share":
        n = sum(1 for r in rows if r["has_reference"])
        if absent:
            n = len(rows) - n
        v = round(100 * n / len(rows), 2) if rows else 0
    elif op == "absence_count":
        v = sum(1 for r in rows if not r["has_reference"])
    else:
        raise ValueError(f"unknown op {op}")
    return {"result": {"op": op, "value": v, "n": len(rows), **detail},
            "missing_keys": missing, "cypher": [q]}


# --------------------------------------------------------------------------
# people & credentials


@tool("credential_timeline",
      """A person's credentials (PMP, Six Sigma Black Belt): id, issued and valid-through dates,
      with the certificate page. Use it to anchor 'after X got certified' questions and to check
      a user's recollection of a date against the record.""",
      {"person": {"type": "string"}, "kind": {"type": "string"}}, ["person"])
def credential_timeline(g: Graph, person: str, kind: str | None = None) -> dict:
    pk, res = _resolve(g, person, "person")
    q = """MATCH (p:Person {key:$p})-[:HOLDS]->(c:Credential)-[e:EVIDENCED_BY]->(d:Document)
           WHERE $kind IS NULL OR toLower(c.kind) CONTAINS toLower($kind)
           RETURN p.name AS person, c.key AS id, c.kind AS kind, c.issued AS issued,
                  c.valid_through AS valid_through, d.doc_id AS doc, e.page AS page
           ORDER BY c.issued"""
    rows = g.read(q, {"p": pk, "kind": kind})
    asof = day(config.AS_OF)
    for r in rows:
        r["valid_as_of"] = (day(r["valid_through"]) or 0) >= asof
    ev = [{"doc_id": r["doc"], "page": r["page"], "what": f"{r['kind']} {r['id']} issued {r['issued']}"}
          for r in rows]
    sg = {"nodes": [_node("Person", pk, rows[0]["person"] if rows else pk)]
          + [_node("Credential", r["id"], f"{r['kind']} {r['id']}") for r in rows],
          "edges": [_edge(f"Person:{pk}", f"Credential:{r['id']}", "HOLDS") for r in rows]}
    return {"result": rows, "resolved_as": res, "evidence": ev, "subgraph": sg, "cypher": [q]}


@tool("date_span",
      """Days between two dates, read from the graph: from a credential's issue date
      (`credential_id`, or `person` + optional `credential_kind`) or an explicit `from_date`,
      to a work's completion date (`work`) or an explicit `to_date`. Returns days (to − from).""",
      {"credential_id": {"type": "string"}, "person": {"type": "string"},
       "credential_kind": {"type": "string"}, "from_date": {"type": "string"},
       "work": {"type": "string"}, "to_date": {"type": "string"}})
def date_span(g: Graph, credential_id: str | None = None, person: str | None = None,
              credential_kind: str | None = None, from_date: str | None = None,
              work: str | None = None, to_date: str | None = None) -> dict:
    ev, basis = [], {}
    if work and not (credential_id or person or from_date):
        # "Meera's PMP on the Pkg-29 job": the person is whoever LED that work.
        wk, _ = _resolve(g, work, "work")
        person = g.scalar("MATCH (:Work {key:$k})-[:LED_BY]->(p:Person) RETURN p.key", {"k": wk})
        basis["person_via"] = "work LED_BY"
    if credential_id or person:
        if credential_id:
            rows = g.read("MATCH (c:Credential {key:$k})-[e:EVIDENCED_BY]->(d) "
                          "RETURN c.issued AS issued, c.kind AS kind, d.doc_id AS doc, e.page AS page",
                          {"k": credential_id})
        else:
            rows = credential_timeline(g, person, credential_kind)["result"]
            rows = [{"issued": r["issued"], "kind": r["kind"], "doc": r["doc"], "page": r["page"]} for r in rows]
        if not rows:
            raise LookupError("credential not found")
        if len(rows) > 1:
            raise LookupError(f"{len(rows)} credentials match; pass credential_id or credential_kind")
        from_date = rows[0]["issued"]
        ev.append({"doc_id": rows[0]["doc"], "page": rows[0]["page"], "what": f"{rows[0]['kind']} issued {from_date}"})
        basis["from"] = f"credential issued {from_date}"
    if work:
        wk, _ = _resolve(g, work, "work")
        r = g.read("MATCH (w:Work {key:$k})-[e:EVIDENCED_BY {kind:'completion_certificate'}]->(d) "
                   "RETURN w.completed AS c, w.title AS t, d.doc_id AS doc, e.page AS page", {"k": wk})[0]
        to_date = r["c"]
        ev.append({"doc_id": r["doc"], "page": r["page"], "what": f"{r['t']} completed {to_date}"})
        basis["to"] = f"{r['t']} completed {to_date}"
    if not (from_date and to_date):
        raise ValueError("need both a from and a to date")
    return {"result": {"days": day(to_date) - day(from_date), "from": from_date, "to": to_date, **basis},
            "evidence": ev}


# --------------------------------------------------------------------------
# money: receivables, collection, awarded vs billed


@tool("receivables",
      """Invoice (receivables ageing) totals for a client: measure = outstanding | invoiced |
      received; optional status (paid | part_paid | due) and year. Invoices are keyed by
      client, not by work — to ask about a project, resolve its client first.""",
      {"client": {"type": "string"}, "measure": {"type": "string", "enum": ["outstanding", "invoiced", "received"]},
       "status": {"type": "string"}, "year": {"type": "integer"}}, ["measure"])
def receivables(g: Graph, measure: str, client: str | None = None, status: str | None = None,
                year: int | None = None) -> dict:
    if measure not in ("outstanding", "invoiced", "received"):   # interpolated into the Cypher below
        raise ValueError("measure must be outstanding, invoiced or received")
    if status:
        s = status.strip().lower().replace("-", "_").replace(" ", "_")
        status = {"partial": "part_paid", "partially_paid": "part_paid", "overdue": "due", "unpaid": "due",
                  "outstanding": "due", "open": "due"}.get(s, s)
        if status not in ("paid", "part_paid", "due"):
            raise ValueError("status must be paid, part_paid or due")
    res = None
    if client:
        client, res = _resolve(g, client, "client")
    q = f"""MATCH (i:Invoice)-[:BILLED_TO]->(c:Client)
            WHERE ($c IS NULL OR c.key = $c) AND ($s IS NULL OR i.status = $s) AND ($y IS NULL OR i.year = $y)
            RETURN count(i) AS n, sum(i.{measure}) AS total, c.name AS client"""
    rows = g.read(q, {"c": client, "s": status, "y": year})
    total = sum(r["total"] or 0 for r in rows)
    return {"result": {"measure": measure, "total": int(total), "n_invoices": sum(r["n"] for r in rows),
                       "client": rows[0]["client"] if len(rows) == 1 else None},
            "resolved_as": res, "evidence": [{"doc_id": "DOC-XL-AGEING", "page": None,
                                              "what": f"Receivables ageing workbook, {sum(r['n'] for r in rows)} rows"}],
            "cypher": [q]}


@tool("collection_rate",
      """% of invoiced amount actually received from a client (2dp). Accepts `client`, or `work`
      (hops work → its client first, since invoices carry no work).""",
      {"client": {"type": "string"}, "work": {"type": "string"}})
def collection_rate(g: Graph, client: str | None = None, work: str | None = None) -> dict:
    hop = None
    if work and not client:
        wk, _ = _resolve(g, work, "work")
        client = g.scalar("MATCH (:Work {key:$k})-[:FOR_CLIENT]->(c) RETURN c.key", {"k": wk})
        hop = wk
    ck, res = _resolve(g, client, "client")
    q = """MATCH (c:Client {key:$c}) OPTIONAL MATCH (i:Invoice)-[:BILLED_TO]->(c)
           RETURN c.name AS client, count(i) AS n, sum(i.invoiced) AS invoiced, sum(i.received) AS received"""
    r = g.read(q, {"c": ck})[0]          # a client with no invoices is a real answer (n = 0), not an error
    rate = round(100 * r["received"] / r["invoiced"], 2) if r["invoiced"] else None
    sg = {"nodes": [_node("Client", ck, r["client"])] + ([_node("Work", hop, hop)] if hop else []),
          "edges": [_edge(f"Work:{hop}", f"Client:{ck}", "FOR_CLIENT")] if hop else []}
    return {"result": {"client": r["client"], "rate_pct": rate, "invoiced": r["invoiced"],
                       "received": r["received"], "n_invoices": r["n"], "via_work": hop},
            "resolved_as": res, "subgraph": sg,
            "evidence": [{"doc_id": "DOC-XL-AGEING", "page": None, "what": f"{r['n']} invoices to {r['client']}"}],
            "cypher": [q]}


@tool("awarded_vs_billed",
      """Gap between what a client awarded us (sum of completed work values from certificates)
      and what we invoiced them (receivables ledger). Accepts `client` or `work` (hops to client).""",
      {"client": {"type": "string"}, "work": {"type": "string"}})
def awarded_vs_billed(g: Graph, client: str | None = None, work: str | None = None) -> dict:
    if work and not client:
        wk, _ = _resolve(g, work, "work")
        client = g.scalar("MATCH (:Work {key:$k})-[:FOR_CLIENT]->(c) RETURN c.key", {"k": wk})
    ck, res = _resolve(g, client, "client")
    q = """MATCH (c:Client {key:$c})
           OPTIONAL MATCH (w:Work)-[:FOR_CLIENT]->(c) WITH c, sum(w.value) AS awarded, count(w) AS nw
           OPTIONAL MATCH (i:Invoice)-[:BILLED_TO]->(c)
           RETURN c.name AS client, awarded, nw, sum(i.invoiced) AS billed, count(i) AS ni"""
    r = g.read(q, {"c": ck})[0]
    aw, bl = int(r["awarded"] or 0), int(r["billed"] or 0)
    return {"result": {"client": r["client"], "awarded": aw, "billed": bl, "gap": abs(aw - bl),
                       "signed_gap": aw - bl, "n_works": r["nw"], "n_invoices": r["ni"]},
            "resolved_as": res, "cypher": [q]}
