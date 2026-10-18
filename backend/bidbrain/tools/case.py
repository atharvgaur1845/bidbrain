"""The case graph — shared state between agents — plus ripple and precedent tools.

A tender qualification is a small subgraph every agent reads from and writes to:

    (:Case)-[:HAS_CLAUSE]->(:Clause {kind, params, verdict})
    (:Clause)-[:SATISFIED_BY {credited}]->(Work | Person | FinancialStatement | Certification | Company)
    (:Clause)-[:HAS_FINDING]->(:Finding {kind: absence|conflict|expiry|integrity|ripple})-[:CITES]->(...)
    (:AgentRun {agent})-[:WORKED_ON]->(:Case), (:AgentRun)-[:WROTE]->(...), (:AgentRun)-[:HANDED_OFF_TO]->(:AgentRun)
    (:Case)-[:PRECEDENT]->(:Bid)

A handoff between agents is nothing more than a graph write the next agent
reads — so the trail of who concluded what, from which evidence, is queryable
after the fact, and a later change to any cited fact can find every case it
touches (`blast_radius`).
"""
from __future__ import annotations

import hashlib
import json
import time

from bidbrain.graph.client import Graph
from bidbrain.tools.registry import tool

LABEL_OF = {"Work": "Work", "Person": "Person", "FinancialStatement": "FinancialStatement",
            "Certification": "Certification", "Company": "Company", "Bond": "Bond", "Credential": "Credential",
            "Contract": "Contract"}


SAT_LABEL = {"similar_works": "Work", "key_personnel": "Person", "certification": "Certification"}


def satisfied_edges(kind: str, res: dict) -> list[tuple[str, str, dict]]:
    """The SATISFIED_BY edges a qualify_clause result implies — one definition for the
    EvidenceHunter and for the ripple, so a re-checked clause is rewired the same way."""
    if kind == "turnover":
        return [("FinancialStatement", f"FS-{y['fy']}", {"credited": y["revenue"]}) for y in res.get("years", [])]
    if kind == "years_in_business":
        return [("Company", "company", {"credited": 1})]
    label = SAT_LABEL.get(kind)
    if not label:
        return []
    return [(label, s["key"], {"credited": s.get("value") or s.get("experience_years") or 1})
            for s in res.get("satisfied_by") or [] if s.get("key")]


def _id(prefix, *parts):
    return prefix + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


@tool("case_open", "Open a new bid qualification case (writes a Case node).",
      {"title": {"type": "string"}, "tender_text": {"type": "string"}, "bid_date": {"type": "string"},
       "tender_ref": {"type": "string"}, "category": {"type": "string"}, "authority": {"type": "string"}},
      ["title"], writes=True)
def case_open(g: Graph, title: str, tender_text: str = "", bid_date: str | None = None,
              tender_ref: str | None = None, category: str | None = None, authority: str | None = None) -> dict:
    cid = _id("CASE-", title, time.time_ns())
    g.run("""CREATE (c:Case {id:$id, key:$id, title:$t, tender_text:$tx, bid_date:$bd, tender_ref:$tr,
                             category:$cat, authority:$au, status:'open', created_at:timestamp()})""",
          {"id": cid, "t": title, "tx": tender_text, "bd": bid_date, "tr": tender_ref, "cat": category,
           "au": authority})
    if tender_ref:
        g.run("MATCH (c:Case {id:$id}) MERGE (t:Tender {key:$r}) ON CREATE SET t.ref = $r "
              "MERGE (c)-[:FOR_TENDER]->(t) "
              # a tender with a live case in this session is open here, whatever the corpus said
              "SET t.status = 'open', t.status_basis = 'live case ' + $id", {"id": cid, "r": tender_ref})
    return {"result": {"case_id": cid}}


def agent_run(g: Graph, case_id: str, agent: str, prev_run: str | None = None, **props) -> str:
    rid = _id("RUN-", case_id, agent, time.time_ns())
    g.run("""MATCH (c:Case {id:$c}) CREATE (r:AgentRun {id:$id, key:$id, agent:$a, status:'running',
                 started_at:timestamp()}) SET r += $p CREATE (r)-[:WORKED_ON]->(c)""",
          {"c": case_id, "id": rid, "a": agent, "p": props})
    if prev_run:
        g.run("MATCH (a:AgentRun {id:$a}), (b:AgentRun {id:$b}) CREATE (a)-[:HANDED_OFF_TO]->(b)",
              {"a": prev_run, "b": rid})
    return rid


def agent_done(g: Graph, run_id: str, **props) -> None:
    g.run("MATCH (r:AgentRun {id:$id}) SET r.status = 'done', r.ended_at = timestamp(), r += $p",
          {"id": run_id, "p": props})


def add_clause(g: Graph, case_id: str, clause: dict, run_id: str | None = None) -> str:
    kid = _id("CL-", case_id, clause.get("id"), clause.get("text"))
    # `corrected`: where the rule parser overrode the LLM's reading of this clause, and why
    g.run("""MATCH (c:Case {id:$c}) CREATE (k:Clause {id:$id, key:$id, ref:$ref, text:$text, kind:$kind,
                 params:$params, verdict:'pending', corrected:$corr}) CREATE (c)-[:HAS_CLAUSE]->(k)""",
          {"c": case_id, "id": kid, "ref": clause.get("id"), "text": clause.get("text", ""),
           "kind": clause["kind"], "params": json.dumps(clause.get("params", {})), "corr": clause.get("corrected")})
    if run_id:
        g.run("MATCH (r:AgentRun {id:$r}), (k:Clause {id:$k}) CREATE (r)-[:WROTE]->(k)", {"r": run_id, "k": kid})
    return kid


def set_verdict(g: Graph, clause_id: str, verdict: str, basis: str, satisfied: list[tuple[str, str, dict]],
                run_id: str | None = None) -> None:
    g.run("MATCH (k:Clause {id:$id}) SET k.verdict = $v, k.basis = $b", {"id": clause_id, "v": verdict, "b": basis})
    g.run("MATCH (k:Clause {id:$id})-[s:SATISFIED_BY]->() DELETE s", {"id": clause_id})
    for label, key, props in satisfied:
        g.run(f"MATCH (k:Clause {{id:$id}}), (s:{label} {{key:$key}}) CREATE (k)-[r:SATISFIED_BY]->(s) SET r += $p",
              {"id": clause_id, "key": key, "p": props})
    if run_id:
        g.run("MATCH (r:AgentRun {id:$r}), (k:Clause {id:$k}) MERGE (r)-[:WROTE]->(k)", {"r": run_id, "k": clause_id})


def add_finding(g: Graph, clause_id: str, kind: str, text: str, severity: str = "medium",
                cites: list[tuple[str, str]] | None = None, run_id: str | None = None) -> str:
    fid = _id("FND-", clause_id, kind, text)
    g.run("""MATCH (k:Clause {id:$c}) MERGE (f:Finding {id:$id}) SET f.key = $id, f.kind = $kind, f.text = $text,
                 f.severity = $sev MERGE (k)-[:HAS_FINDING]->(f)""",
          {"c": clause_id, "id": fid, "kind": kind, "text": text, "sev": severity})
    for label, key in cites or []:
        prop = "id" if label in ("Claim", "ConflictCard") else ("doc_id" if label == "Document" else "key")
        g.run(f"MATCH (f:Finding {{id:$f}}), (x:{label} {{{prop}:$k}}) MERGE (f)-[:CITES]->(x)", {"f": fid, "k": key})
    if run_id:
        g.run("MATCH (r:AgentRun {id:$r}), (f:Finding {id:$f}) MERGE (r)-[:WROTE]->(f)", {"r": run_id, "f": fid})
    return fid


@tool("case_get", "Read a case: clauses, verdicts, what satisfies each, findings, and which agent wrote what.",
      {"case_id": {"type": "string"}}, ["case_id"])
def case_get(g: Graph, case_id: str) -> dict:
    q = """MATCH (c:Case {id:$id})
           OPTIONAL MATCH (c)-[:HAS_CLAUSE]->(k:Clause)
           OPTIONAL MATCH (k)-[s:SATISFIED_BY]->(x)
           WITH c, k, collect(CASE WHEN x IS NULL THEN NULL ELSE {label: labels(x)[0], key: x.key,
                name: coalesce(x.name, x.title, x.standard, x.key), credited: s.credited} END) AS sat
           OPTIONAL MATCH (k)-[:HAS_FINDING]->(f:Finding)
           WITH c, k, sat, collect(CASE WHEN f IS NULL THEN NULL ELSE {kind: f.kind, text: f.text, severity: f.severity,
                status: coalesce(f.status, 'open'), resolution: f.resolution} END) AS fnd
           RETURN c {.id, .title, .bid_date, .tender_ref, .status, .verdict, .memo} AS case,
                  collect(CASE WHEN k IS NULL THEN NULL ELSE {id: k.id, ref: k.ref, text: k.text, kind: k.kind,
                          params: k.params, verdict: k.verdict, basis: k.basis, corrected: k.corrected,
                          satisfied_by: sat, findings: fnd} END) AS clauses"""
    rows = g.read(q, {"id": case_id})
    if not rows:
        raise LookupError(case_id)
    runs = g.read("""MATCH (r:AgentRun)-[:WORKED_ON]->(:Case {id:$id})
                     OPTIONAL MATCH (r)-[:HANDED_OFF_TO]->(n:AgentRun)
                     RETURN r.agent AS agent, r.status AS status, r.started_at AS t, r.llm_calls AS llm_calls,
                            r.tool_calls AS tool_calls, r.ms AS ms, n.agent AS handed_to ORDER BY t""", {"id": case_id})
    return {"result": {**rows[0], "agent_runs": runs}, "cypher": [q]}


@tool("precedents",
      """Past bids similar to a tender (episodic memory): same category and/or same client, with the
      works they cited and people they proposed. Use to reuse what worked before.""",
      {"category": {"type": "string"}, "client": {"type": "string"}, "limit": {"type": "integer"}})
def precedents(g: Graph, category: str | None = None, client: str | None = None, limit: int = 3) -> dict:
    from bidbrain.tools.read import _resolve
    ck = _resolve(g, client, "client")[0] if client else None
    q = """MATCH (b:Bid) OPTIONAL MATCH (b)-[:TO_CLIENT]->(c:Client)
           WITH b, c, (CASE WHEN $cat IS NOT NULL AND b.category = $cat THEN 2 ELSE 0 END) +
                      (CASE WHEN $ck IS NOT NULL AND c.key = $ck THEN 1 ELSE 0 END) AS score
           WHERE score > 0 OR ($cat IS NULL AND $ck IS NULL)
           OPTIONAL MATCH (b)-[:CITES]->(w:Work) WHERE $cat IS NULL OR w.category = $cat
           RETURN b.key AS bid, b.category AS category, b.bid_value AS bid_value, b.submitted AS submitted,
                  c.name AS client, score, collect(w.title)[0..6] AS cited_same_category, b.doc_id AS doc
           ORDER BY score DESC, b.submitted DESC LIMIT $lim"""
    rows = g.read(q, {"cat": category, "ck": ck, "lim": int(limit)})
    return {"result": rows, "evidence": [{"doc_id": r["doc"], "page": 1, "what": f"past bid {r['bid']}"} for r in rows],
            "cypher": [q]}


@tool("audit_bid",
      """Integrity check of a past bid (or all bids): works cited as past performance that were not yet
      complete on the submission date, people proposed whose credential had lapsed, bid security
      expiring before the bid validity. Returns concrete violations with evidence.""",
      {"bid": {"type": "string"}})
def audit_bid(g: Graph, bid: str | None = None) -> dict:
    q = """MATCH (b:Bid) WHERE $bid IS NULL OR b.key = $bid
           OPTIONAL MATCH (b)-[:CITES]->(w:Work) WHERE w.completed > b.submitted
           WITH b, collect({title: w.title, completed: w.completed}) AS premature
           OPTIONAL MATCH (b)-[:PROPOSES]->(p:Person)-[:HOLDS]->(cr:Credential)
           WHERE cr.kind = 'PMP' AND cr.issued > b.submitted
           RETURN b.key AS bid, b.submitted AS submitted, b.doc_id AS doc,
                  [x IN premature WHERE x.title IS NOT NULL] AS cited_before_completion,
                  collect(DISTINCT p.name) AS proposed_before_certified"""
    rows = g.read(q, {"bid": bid})
    for r in rows:
        r["n_violations"] = len(r["cited_before_completion"]) + len(r["proposed_before_certified"])
    return {"result": rows, "evidence": [{"doc_id": r["doc"], "page": 10, "what": f"{r['bid']} annexure D"} for r in rows],
            "cypher": [q]}


# Every path from a changed subject to a decision that rests on it. Each prefix walks from the subject (s) to a
# node a bid or a clause can point at (the subject itself; a person's led works; a credential's holder and the
# holder's works); each suffix walks from there to the affected item (x). The path is returned with the item.
_REACH = {
    "Person": ["", "<-[:LED_BY]-(:Work)"],
    "Credential": ["", "<-[:HOLDS]-(:Person)", "<-[:HOLDS]-(:Person)<-[:LED_BY]-(:Work)"],
}
_SUFFIX = [
    ("bid", "<-[:CITES|PROPOSES|SECURED_BY]-(x:Bid)"),
    ("clause", "<-[:SATISFIED_BY]-(x:Clause)<-[:HAS_CLAUSE]-(:Case {status:'open'})"),
    ("case", "<-[:SATISFIED_BY]-(:Clause)<-[:HAS_CLAUSE]-(x:Case {status:'open'})"),
    ("tender", "<-[:CITES|PROPOSES|SECURED_BY]-(:Bid)-[:FOR_TENDER]->(x:Tender)"),
    ("tender", "<-[:SATISFIED_BY]-(:Clause)<-[:HAS_CLAUSE]-(:Case {status:'open'})-[:FOR_TENDER]->(x:Tender)"),
]
_EXTRA = {
    "Person": [("credential", "-[:HOLDS]->(x:Credential)")],
    "Credential": [("person", "<-[:HOLDS]-(x:Person)")],
    "Bond": [("tender", "-[:SECURES]->(x:Tender)")],
}


def dependency_patterns(label: str) -> list[tuple[str, str]]:
    """(kind, pattern after the subject node) for every dependency path blast_radius follows from `label`."""
    pats = [(kind, pre + suf) for pre in _REACH.get(label, [""]) for kind, suf in _SUFFIX]
    return pats + _EXTRA.get(label, [])


def dependents(g: Graph, label: str, key: str) -> tuple[list[dict], list[str]]:
    """Every item that depends on (label, key), each with the paths that explain why: node ids
    ('Label:key') and relationship types (with direction) from the changed subject to the item."""
    out: dict[tuple[str, str], dict] = {}
    cyphers = []
    for kind, pat in dependency_patterns(label):
        q = (f"MATCH p = (s:{label} {{key:$k}}){pat} "
             "RETURN coalesce(x.id, x.key) AS id, coalesce(x.title, x.name, x.key, x.id) AS name, "
             "[n IN nodes(p) | labels(n)[0] + ':' + coalesce(n.key, n.id)] AS nodes, "
             "[r IN relationships(p) | type(r)] AS rels, "
             "[r IN relationships(p) | labels(startNode(r))[0] + ':' + coalesce(startNode(r).key, startNode(r).id)] "
             "AS starts, x.submitted AS submitted, [(x)<-[:HAS_CLAUSE]-(c1:Case) | c1.id][0] AS case_id, "
             "[(x)<-[:HAS_CLAUSE]-(c2:Case) | c2.title][0] AS case_title, x.status AS status")
        cyphers.append(q)
        for r in g.read(q, {"k": key}):
            nodes, rels = r["nodes"], r["rels"]
            # direction: an edge points forward along the path when it starts at the earlier node
            hops = [{"type": t, "dir": "->" if st == nodes[i] else "<-"} for i, (t, st) in enumerate(zip(rels, r["starts"]))]
            text = nodes[0] + "".join((f"-[:{h['type']}]->" if h["dir"] == "->" else f"<-[:{h['type']}]-") + n
                                      for h, n in zip(hops, nodes[1:]))
            item = out.setdefault((kind, r["id"]), {"kind": kind, "id": r["id"], "name": r["name"], "paths": []})
            for f in ("submitted", "case_id", "case_title", "status"):
                if r[f] is not None:
                    item[f] = r[f]
            if text not in {p["text"] for p in item["paths"]}:
                item["paths"].append({"nodes": nodes, "rels": hops, "text": text})
    order = {"case": 0, "clause": 1, "bid": 2, "tender": 3, "credential": 4, "person": 5}
    return sorted(out.values(), key=lambda d: (order.get(d["kind"], 9), str(d["id"]))), cyphers


@tool("blast_radius",
      """What depends on a fact: given a subject (work / person / bond / financial statement / credential)
      find every open case and clause that relies on it, past bids that cited it (for a person: bids that
      proposed them or cited a work they led; for a credential: through its holder), tenders those bids or
      cases are for, and tenders it secures; each with the explaining path. Then re-run every affected clause
      and report verdict flips (PASS -> FAIL etc.).""",
      {"label": {"type": "string"}, "key": {"type": "string"}, "rerun": {"type": "boolean"}}, ["label", "key"])
def blast_radius(g: Graph, label: str, key: str, rerun: bool = True) -> dict:
    if label not in LABEL_OF:      # the label is interpolated into Cypher below
        raise ValueError(f"label must be one of {sorted(LABEL_OF)}")
    label = LABEL_OF[label]
    deps, dep_cypher = dependents(g, label, key)
    direct = [{"case_id": d["case_id"], "case": d.get("case_title"), "clause_id": d["id"],
               "path": d["paths"][0]["text"]} for d in deps if d["kind"] == "clause"]
    bids = [{"bid": d["id"], "submitted": d.get("submitted"), "path": d["paths"][0]["text"],
             "paths": [p["text"] for p in d["paths"]]} for d in deps if d["kind"] == "bid"]
    tenders = [{"tender": d["id"], "status": d.get("status"), "path": d["paths"][0]["text"]}
               for d in deps if d["kind"] == "tender"]
    secured = [t for t in tenders if label == "Bond" and "-[:SECURES]->" in t["path"]]
    relying = sorted({d["clause_id"] for d in direct})
    kinds = {"Work": ["similar_works"], "Person": ["key_personnel"], "Credential": ["key_personnel"],
             "FinancialStatement": ["turnover"], "Certification": ["certification"]}.get(label, [])
    # every open clause whose *candidate set* could include this subject is re-checked, not only the
    # ones it currently satisfies: a corrigendum can make a failing clause pass, too
    clauses = g.read("""MATCH (c:Case {status:'open'})-[:HAS_CLAUSE]->(k:Clause) WHERE k.kind IN $kinds
                           OR k.id IN $relying
                        OPTIONAL MATCH (k)-[:SATISFIED_BY]->(s)
                        RETURN c.id AS case_id, c.title AS case, c.bid_date AS bid_date, k.id AS clause_id,
                               k.ref AS ref, k.kind AS kind, k.params AS params, k.verdict AS verdict,
                               k.prefer_reference AS pref, collect(s.key) AS sat""",
                     {"kinds": kinds, "relying": relying})
    flips, rewired = [], []
    if rerun:
        for c in clauses:
            try:
                # same selection policy as the team: a clause the EvidenceHunter re-selected onto referenced
                # works is re-checked that way too, so a ripple does not point it back at a work with no letter
                from bidbrain.agents.orchestrator import select
                out = select(g, c["kind"], json.loads(c["params"] or "{}"), c["bid_date"], bool(c.get("pref")))[0]
            except Exception:           # a clause the engine cannot re-check keeps its verdict
                continue
            new = out["result"]["verdict"]
            sat = satisfied_edges(c["kind"], out["result"])
            entry = {"case": c["case"], "case_id": c["case_id"], "clause": c["ref"], "kind": c["kind"],
                     "from": c["verdict"], "to": new, "basis": out["result"]["basis"],
                     "clause_id": c["clause_id"], "satisfied": sat,
                     "detail": {f: out["result"].get(f) for f in _DETAIL if out["result"].get(f)}}
            if new != c["verdict"]:
                flips.append(entry)
            elif sorted(k for _, k, _ in sat) != sorted(c["sat"]):
                rewired.append(entry)       # same verdict, different evidence: still rewire SATISFIED_BY
    if label == "Work":
        # good news is a change too: a reference letter now on file for this work closes the absence
        # findings that named it (apply_flips resolves them and fulfils the letter requests)
        by_clause = {e["clause_id"]: e for e in flips + rewired}
        for r in reference_closures(g, key):
            if r["clause_id"] in by_clause:
                by_clause[r["clause_id"]].setdefault("reference_letters", []).append(r["letter"])
            else:
                rewired.append({"case": r["case"], "case_id": r["case_id"], "clause": r["clause"],
                                "kind": r["kind"], "from": r["verdict"], "to": r["verdict"], "basis": r["basis"],
                                "clause_id": r["clause_id"], "reference_letters": [r["letter"]]})
    nodes, edges = {f"{label}:{key}": {"id": f"{label}:{key}", "label": label, "name": key}}, {}
    for d in deps:
        for p in d["paths"]:
            for n in p["nodes"]:
                nodes.setdefault(n, {"id": n, "label": n.split(":", 1)[0], "name": n.split(":", 1)[1]})
            for i, h in enumerate(p["rels"]):
                a, b = p["nodes"][i], p["nodes"][i + 1]
                src, dst = (a, b) if h["dir"] == "->" else (b, a)
                edges[(src, dst, h["type"])] = {"source": src, "target": dst, "type": h["type"]}
    by_kind: dict[str, int] = {}
    for d in deps:
        by_kind[d["kind"]] = by_kind.get(d["kind"], 0) + 1
    return {"result": {"subject": f"{label}:{key}", "dependents": deps, "dependents_by_kind": by_kind,
                       "cases_relying": direct, "past_bids_citing": bids,
                       "tenders": tenders, "tenders_secured": secured, "clauses_rechecked": len(clauses), "verdict_flips": flips,
                       "evidence_changed": rewired},
            "subgraph": {"nodes": list(nodes.values()), "edges": list(edges.values())},
            "cypher": dep_cypher + [
                       "MATCH (c:Case {status:'open'})-[:HAS_CLAUSE]->(k:Clause) WHERE k.kind IN $kinds OR k.id IN $relying "
                       "RETURN c.id, c.bid_date, k.id, k.kind, k.params, k.verdict   // each is re-run with the selection policy"]}


_DETAIL = ("expiring_within_90d", "safe_alternates", "near_misses")


def reference_closures(g: Graph, work_key: str) -> list[dict]:
    """Open absence findings (in open cases) that name this work although a reference letter for it is now
    on file: one row per finding, with the newest letter."""
    rows = g.read("""MATCH (w:Work {key:$k})-[:HAS_REFERENCE]->(d:Document)
                     MATCH (c:Case {status:'open'})-[:HAS_CLAUSE]->(k:Clause)-[:HAS_FINDING]->(f:Finding {kind:'absence'})
                           -[:CITES]->(w)
                     WHERE coalesce(f.status, 'open') <> 'resolved'
                     WITH c, k, f, w, d ORDER BY d.issued DESC, d.doc_id DESC
                     WITH c, k, f, w, collect(d.doc_id)[0] AS doc
                     RETURN c.id AS case_id, c.title AS case, k.id AS clause_id, k.ref AS clause, k.kind AS kind,
                            k.verdict AS verdict, k.basis AS basis, f.id AS finding, w.key AS work, w.title AS title,
                            doc ORDER BY case_id, clause_id, finding""", {"k": work_key})
    return [{**r, "letter": {"doc_id": r["doc"], "work": r["work"], "title": r["title"], "finding": r["finding"]}}
            for r in rows]


# finding kinds that describe the clause's *current* verdict (a gap closes, a ripple note is
# history once the clause passes) and so do not hold a PASS clause back
_VERDICT_BOUND = ("gap", "ripple")


def overall_verdict(g: Graph, case_id: str) -> str:
    """The case recommendation — the one rule both the Assembler and the ripple use.

    NO-BID if any clause FAILs; BID WITH FIXES if any is AT_RISK or carries a live high-severity
    finding (on any clause, PASS included: a contested fact or a missing reference letter is a
    risk even where the clause passes); else BID. A 'conflict' finding stays live only while a
    claim it cites is still contested; a finding marked resolved (the letter arrived, a ripple moved
    the clause off what it named) is not live."""
    rows = g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause)
                     OPTIONAL MATCH (k)-[:HAS_FINDING]->(f:Finding {severity:'high'})
                     WHERE coalesce(f.status, 'open') <> 'resolved'
                     OPTIONAL MATCH (f)-[:CITES]->(cl:Claim)
                     WITH k, f, collect(cl.status) AS cited
                     RETURN k.verdict AS v, collect(CASE WHEN f IS NULL THEN NULL
                            ELSE {kind: f.kind, cited: cited} END) AS high""", {"c": case_id})
    verds = [r["v"] for r in rows if r["v"] != "DECLARE"]

    def live(f: dict, verdict: str) -> bool:
        if f["kind"] == "conflict" and f["cited"]:
            return "contested" in f["cited"]
        return not (verdict == "PASS" and f["kind"] in _VERDICT_BOUND)

    high = any(live(f, r["v"]) for r in rows for f in r["high"] if f)
    if "FAIL" in verds:
        return "NO-BID"
    if "NEEDS REVIEW" in verds:          # an unverified material requirement never yields a BID
        return "NEEDS REVIEW"
    return "BID WITH FIXES" if "AT_RISK" in verds or high else "BID"


def apply_flips(g: Graph, flips: list[dict], cause: str, evidence_changed: list[dict] | None = None,
                decision_id: str | None = None, cause_doc: str | None = None) -> list[dict]:
    """Write ripple results back: new verdicts *and* the evidence they now rest on (SATISFIED_BY is
    rebuilt), a 'ripple' finding naming the cause, and the case's recomputed overall recommendation.
    `evidence_changed` (blast_radius's list of same-verdict clauses whose satisfying set moved, or that a newly
    arrived reference letter strengthens) is rewired too.

    Then the loop is closed the way a fresh qualification would close it, deterministically (no LLM): every
    changed clause is re-audited (`reaudit`: absence / expiry / near-miss gap findings, the referenced-works
    hand-back), findings that no longer apply are resolved (citing the reference letter when that is why),
    their open follow-ups are superseded or fulfilled, and the action generator (`from_findings`) queues the
    letter / renewal requests the new findings call for. The memo of every changed case is re-assembled
    (template, marked as superseding the one written at qualification), so the bid pack matches the case. A
    case the Verifier could not verify stays NEEDS REVIEW. With `decision_id`, each ripple finding is linked
    (Finding)-[:CAUSED_BY]->(Decision); every case whose recommendation changed gets a `review_bid` action
    (company.actions). Returns per-case before/after, with `follow_ups` (actions queued) and `closed_actions`."""
    from bidbrain.agents.orchestrator import rebuild_memo
    from bidbrain.company import actions

    def caused(fid: str) -> None:
        if decision_id:
            g.run("MATCH (f:Finding {id:$f}), (d:Decision {id:$d}) MERGE (f)-[:CAUSED_BY]->(d)",
                  {"f": fid, "d": decision_id})
    for f in flips:
        _rewire(g, f)
        caused(add_finding(g, f["clause_id"], "ripple", f"verdict changed {f['from']} -> {f['to']} because {cause}",
                           severity="high" if f["to"] == "FAIL" else "medium"))
    for f in evidence_changed or []:
        if "satisfied" in f or "reference_letters" not in f:
            _rewire(g, f)
            caused(add_finding(g, f["clause_id"], "ripple", f"verdict {f['to']} unchanged, but the evidence it "
                                                            f"rests on changed because {cause}", severity="info"))
        for r in f.get("reference_letters") or []:
            caused(add_finding(g, f["clause_id"], "ripple",
                               f"a client reference letter for {r['title']} is now on file ({r['doc_id']}); the "
                               f"absence finding that named it is resolved", severity="info",
                               cites=[("Document", r["doc_id"])]))
    touched = flips + list(evidence_changed or [])
    audit = reaudit(g, sorted({f["clause_id"] for f in touched}), cause)
    changed = []
    for cid in sorted({f["case_id"] for f in touched}):
        row = g.read("MATCH (c:Case {id:$c}) RETURN c.verdict AS v, c.verified AS ok", {"c": cid})[0]
        before = row["v"]
        after = "NEEDS REVIEW" if row["ok"] is False else overall_verdict(g, cid)
        g.run("MATCH (c:Case {id:$c}) SET c.verdict = $v", {"c": cid, "v": after})
        rebuild_memo(g, cid, cause, before, after)
        changed.append({"case_id": cid, "from": before, "to": after, "memo": "rebuilt"})
        for f in flips:
            if f["case_id"] == cid:
                f["case_from"], f["case_to"] = before, after
    if decision_id:                     # the impact view reads before/after off the findings this decision caused
        for c in changed:
            g.run("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->()-[:HAS_FINDING]->(f:Finding {kind:'ripple'})
                     -[:CAUSED_BY]->(:Decision {id:$d}) SET f.case_from = $b, f.case_to = $a""",
                  {"c": c["case_id"], "d": decision_id, "b": c["from"], "a": c["to"]})
    for a in actions.from_ripple(g, changed, decision_id, cause, cause_doc):
        for c in changed:
            if c["case_id"] == g.scalar("MATCH (a:Action {id:$a}) RETURN a.case_id", {"a": a["id"]}):
                c["action"] = a["id"]
    # follow-ups: what the findings that no longer apply asked for is closed; what the new ones ask for is queued
    closed = actions.close_for_findings(g, audit["resolved_moot"], f"the finding it was raised for no longer "
                                                                   f"applies after {cause}")
    for doc, works in sorted(audit["letters"].items()):
        closed += actions.fulfil_reference(g, sorted(works), doc)
    case_of = {r["id"]: r["c"] for r in g.read("MATCH (a:Action) WHERE a.id IN $ids RETURN a.id AS id, "
                                               "a.case_id AS c", {"ids": closed})} if closed else {}
    for c in changed:
        c["follow_ups"] = [a["id"] for a in actions.from_findings(g, c["case_id"]) if a["created"]]
        c["closed_actions"] = [a for a in closed if case_of.get(a) == c["case_id"]]
        c["findings_opened"] = [x for x in audit["opened"] if x["case_id"] == c["case_id"]]
        c["findings_resolved"] = [x for x in audit["resolved"] if x["case_id"] == c["case_id"]]
    if closed and changed:              # a fulfilled request for a work cited in another case is still reported
        rest = [a for a in closed if case_of.get(a) not in {c["case_id"] for c in changed}]
        changed[0]["closed_actions"] += rest
    return changed


def _rewire(g: Graph, f: dict) -> None:
    if "satisfied" in f:
        set_verdict(g, f["clause_id"], f["to"], f["basis"], [tuple(x) for x in f["satisfied"]])
    else:                               # a flip computed by an older blast_radius: verdict only
        g.run("MATCH (k:Clause {id:$id}) SET k.verdict = $v, k.basis = $b",
              {"id": f["clause_id"], "v": f["to"], "b": f["basis"]})
    if "detail" in f:                   # what the auditor reads (near misses, lapsing credentials), as the hunter writes it
        g.run("MATCH (k:Clause {id:$id}) SET k.detail = $d", {"id": f["clause_id"],
                                                               "d": json.dumps(f["detail"], default=str)})


# ---- the AbsenceAuditor's checks, re-run deterministically on clauses a ripple changed -------------------

_AUDITED = ("absence", "expiry", "gap")


def _expected_findings(g: Graph, k: dict) -> list[dict]:
    """The findings the AbsenceAuditor (agents/orchestrator.py, step 4) writes for this clause as it stands now:
    same conditions, same wording, same citations. The referenced-works hand-back to the EvidenceHunter is
    applied first (select(prefer_reference=True), kept only at an equal verdict), as in a fresh run."""
    from bidbrain.agents.orchestrator import _cr, select
    from bidbrain.tools.proof import prove_absence
    params = json.loads(k["params"] or "{}")
    detail = json.loads(k["detail"] or "{}") if isinstance(k["detail"], str) else (k["detail"] or {})
    want = []
    if k["kind"] == "similar_works":
        sat = [r["key"] for r in g.read("MATCH (:Clause {id:$k})-[:SATISFIED_BY]->(w:Work) RETURN w.key AS key",
                                        {"k": k["id"]})]
        missing = g.read("MATCH (w:Work) WHERE w.key IN $k AND NOT (w)-[:HAS_REFERENCE]->() "
                         "RETURN w.key AS key, w.title AS title ORDER BY key", {"k": sat}) if sat else []
        if missing and not k["pref"] and k["verdict"] in ("PASS", "AT_RISK"):
            try:
                out, used_ref, _ = select(g, k["kind"], params, k["bid_date"], prefer_reference=True)
            except Exception:
                out, used_ref = None, False
            if used_ref and out["result"]["verdict"] == k["verdict"]:
                res = out["result"]
                set_verdict(g, k["id"], res["verdict"], res["basis"], satisfied_edges(k["kind"], res))
                g.run("MATCH (k:Clause {id:$k}) SET k.prefer_reference = true, k.detail = $d",
                      {"k": k["id"], "d": json.dumps({f: res.get(f) for f in _DETAIL if res.get(f)}, default=str)})
                add_finding(g, k["id"], "ripple", "re-selected referenced works for this clause (the works it "
                                                  f"relied on have no reference letter); still {res['verdict']}",
                            severity="info")
                return _expected_findings(g, {**k, "pref": True})
        if missing:
            clients = [r["client"] for r in g.read(
                """MATCH (w:Work)-[:FOR_CLIENT]->(c:Client) WHERE w.key IN $k
                   RETURN DISTINCT c.key AS client ORDER BY client""", {"k": [m["key"] for m in missing]})]
            checks = [prove_absence(g, "reference_letter", client=ck)["result"]["checks"] for ck in clients]
            if checks:
                txt = (f"No client reference letter on file for {', '.join(m['title'] for m in missing)} "
                       f"(proof across {len(checks)} client(s): {checks[0]['corpus_closure']}; boundary "
                       f"{', '.join(str(ch['boundary_attested_by_3_views']) for ch in checks)} attested). "
                       f"Request letters from the client before submission or cite alternates.")
                want.append({"kind": "absence", "text": txt, "cites": [("Work", m["key"]) for m in missing],
                             "severity": "high"})   # a credited work with no letter is always a fix, as in the auditor
        if k["verdict"] == "FAIL" and detail.get("near_misses"):
            nm = detail["near_misses"][:3]
            cats = " / ".join(params.get("categories") or []) or "similar-works"
            txt = ("No qualifying combination. Closest works: " +
                   "; ".join(f"{w['title']} ({_cr(w['value'])})" for w in nm) +
                   f". Consider a JV with a partner holding a larger {cats} credential.")
            want.append({"kind": "gap", "text": txt, "cites": [("Work", w["key"]) for w in nm], "severity": "high"})
    if k["kind"] == "key_personnel":
        credited = {r["name"] for r in g.read("MATCH (:Clause {id:$k})-[:SATISFIED_BY]->(p:Person) RETURN p.name AS name",
                                              {"k": k["id"]})}
        exp = [e for e in detail.get("expiring_within_90d") or [] if e["name"] in credited]
        if exp:
            alts = ", ".join(a["name"] for a in detail.get("safe_alternates", []))
            txt = (f"PMP of {', '.join(e['name'] + ' (to ' + e['valid_through'] + ')' for e in exp)} lapses "
                   f"within 90 days of the bid date. Renew now, or propose {alts or 'alternates'} (valid well beyond).")
            keys = [r["key"] for r in g.read("MATCH (p:Person) WHERE p.name IN $n RETURN p.key AS key",
                                             {"n": [e["name"] for e in exp]})]
            want.append({"kind": "expiry", "text": txt, "cites": [("Person", x) for x in keys],
                         "severity": "high" if k["verdict"] == "AT_RISK" else "low"})
    return want


def reaudit(g: Graph, clause_ids: list[str], cause: str) -> dict:
    """Re-run the AbsenceAuditor's checks on these clauses and reconcile their findings with the result.

    A finding still called for (same kind, same cited works or people) is kept as it is; a new one is written
    (raised_by 'ripple'); one no longer called for is resolved, citing the reference letter(s) when a letter is
    why (RESOLVED_BY), otherwise "no longer applies after <cause>". Returns {opened, resolved, resolved_moot
    (finding ids whose follow-ups should be superseded), letters ({doc_id: works} to fulfil)}."""
    out = {"opened": [], "resolved": [], "resolved_moot": [], "letters": {}}
    if not clause_ids:
        return out
    clauses = g.read("""MATCH (c:Case)-[:HAS_CLAUSE]->(k:Clause) WHERE k.id IN $ids AND k.kind IN $kinds
                        RETURN k.id AS id, k.ref AS ref, k.kind AS kind, k.text AS text, k.params AS params,
                               k.verdict AS verdict, k.detail AS detail, coalesce(k.prefer_reference, false) AS pref,
                               c.id AS case_id, c.bid_date AS bid_date ORDER BY id""",
                     {"ids": clause_ids, "kinds": ["similar_works", "key_personnel"]})
    for k in clauses:
        want = _expected_findings(g, k)
        have = g.read("""MATCH (:Clause {id:$k})-[:HAS_FINDING]->(f:Finding) WHERE f.kind IN $kinds
                           AND coalesce(f.status, 'open') <> 'resolved'
                         OPTIONAL MATCH (f)-[:CITES]->(x) WHERE x:Work OR x:Person
                         RETURN f.id AS id, f.kind AS kind, collect(x.key) AS cites""",
                      {"k": k["id"], "kinds": list(_AUDITED)})
        sig = lambda kind, keys: (kind, tuple(sorted(keys)))
        have_by = {sig(h["kind"], h["cites"]): h["id"] for h in have}
        kept = set()
        for w in want:
            s = sig(w["kind"], [key for _, key in w["cites"]])
            if s in have_by:
                kept.add(have_by[s])
                continue
            fid = add_finding(g, k["id"], w["kind"], w["text"], w["severity"], w["cites"])
            g.run("""MATCH (f:Finding {id:$f}) SET f.status = 'open', f.raised_by = 'ripple', f.cause = $c,
                         f.resolution = null""", {"f": fid, "c": cause})
            kept.add(fid)
            out["opened"].append({"case_id": k["case_id"], "clause": k["ref"], "finding": fid, "kind": w["kind"],
                                  "text": w["text"]})
        for h in have:
            if h["id"] in kept:
                continue
            letters = []
            if h["kind"] == "absence" and h["cites"]:
                letters = g.read("""MATCH (w:Work)-[:HAS_REFERENCE]->(d:Document) WHERE w.key IN $w
                                    WITH w, d ORDER BY d.issued DESC, d.doc_id DESC
                                    RETURN w.key AS work, collect(d.doc_id)[0] AS doc""", {"w": h["cites"]})
                if len(letters) < len(h["cites"]):
                    letters = []            # not every work it named has a letter: moot for another reason
            if letters:
                docs = sorted({r["doc"] for r in letters})
                why = f"resolved: client reference letter on file ({', '.join(docs)})"
                for r in letters:
                    out["letters"].setdefault(r["doc"], set()).add(r["work"])
                    g.run("MATCH (f:Finding {id:$f}), (d:Document {doc_id:$d}) MERGE (f)-[:RESOLVED_BY]->(d)",
                          {"f": h["id"], "d": r["doc"]})
            else:
                why = f"resolved: no longer applies after {cause}"
                out["resolved_moot"].append(h["id"])
            g.run("""MATCH (f:Finding {id:$f}) SET f.status = 'resolved', f.resolution = $why,
                         f.resolved_at = timestamp(), f.severity_before = f.severity, f.severity = 'info',
                         f.text = f.text + ' [' + $why + ']'""", {"f": h["id"], "why": why})
            out["resolved"].append({"case_id": k["case_id"], "clause": k["ref"], "finding": h["id"],
                                    "kind": h["kind"], "resolution": why})
    return out
