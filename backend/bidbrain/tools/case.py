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
       "tender_ref": {"type": "string"}, "category": {"type": "string"}}, ["title"], writes=True)
def case_open(g: Graph, title: str, tender_text: str = "", bid_date: str | None = None,
              tender_ref: str | None = None, category: str | None = None) -> dict:
    cid = _id("CASE-", title, time.time_ns())
    g.run("""CREATE (c:Case {id:$id, key:$id, title:$t, tender_text:$tx, bid_date:$bd, tender_ref:$tr,
                             category:$cat, status:'open', created_at:timestamp()})""",
          {"id": cid, "t": title, "tx": tender_text, "bd": bid_date, "tr": tender_ref, "cat": category})
    if tender_ref:
        g.run("MATCH (c:Case {id:$id}) MERGE (t:Tender {key:$r}) ON CREATE SET t.ref = $r "
              "MERGE (c)-[:FOR_TENDER]->(t)", {"id": cid, "r": tender_ref})
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
    g.run("""MATCH (c:Case {id:$c}) CREATE (k:Clause {id:$id, key:$id, ref:$ref, text:$text, kind:$kind,
                 params:$params, verdict:'pending'}) CREATE (c)-[:HAS_CLAUSE]->(k)""",
          {"c": case_id, "id": kid, "ref": clause.get("id"), "text": clause.get("text", ""),
           "kind": clause["kind"], "params": json.dumps(clause.get("params", {}))})
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
           WITH c, k, sat, collect(CASE WHEN f IS NULL THEN NULL ELSE {kind: f.kind, text: f.text, severity: f.severity} END) AS fnd
           RETURN c {.id, .title, .bid_date, .tender_ref, .status, .verdict, .memo} AS case,
                  collect(CASE WHEN k IS NULL THEN NULL ELSE {id: k.id, ref: k.ref, text: k.text, kind: k.kind,
                          params: k.params, verdict: k.verdict, basis: k.basis, satisfied_by: sat, findings: fnd} END) AS clauses"""
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


@tool("blast_radius",
      """What depends on a fact: given a subject (work / person / bond / financial statement / credential)
      find open cases whose clauses rely on it, past bids that cited it, tenders it secures — then
      re-run every affected clause and report verdict flips (PASS -> FAIL etc.).""",
      {"label": {"type": "string"}, "key": {"type": "string"}, "rerun": {"type": "boolean"}}, ["label", "key"])
def blast_radius(g: Graph, label: str, key: str, rerun: bool = True) -> dict:
    from bidbrain.tools.proof import qualify_clause
    if label not in LABEL_OF:      # the label is interpolated into Cypher below
        raise ValueError(f"label must be one of {sorted(LABEL_OF)}")
    label = LABEL_OF[label]
    direct = g.read(f"""MATCH (s:{label} {{key:$k}})<-[:SATISFIED_BY]-(k:Clause)<-[:HAS_CLAUSE]-(c:Case)
                        RETURN DISTINCT c.id AS case_id, c.title AS case, k.id AS clause_id""", {"k": key})
    bids = g.read(f"""MATCH (s:{label} {{key:$k}})<-[:CITES|PROPOSES|SECURED_BY]-(b:Bid)
                      RETURN b.key AS bid, b.submitted AS submitted""", {"k": key})
    tenders = g.read(f"MATCH (s:{label} {{key:$k}})-[:SECURES]->(t:Tender) RETURN t.key AS tender", {"k": key}) \
        if label == "Bond" else []
    kinds = {"Work": ["similar_works"], "Person": ["key_personnel"], "Credential": ["key_personnel"],
             "FinancialStatement": ["turnover"], "Certification": ["certification"]}.get(label, [])
    # every open clause whose *candidate set* could include this subject is re-checked, not only the
    # ones it currently satisfies: a corrigendum can make a failing clause pass, too
    clauses = g.read("""MATCH (c:Case {status:'open'})-[:HAS_CLAUSE]->(k:Clause) WHERE k.kind IN $kinds
                        OPTIONAL MATCH (k)-[:SATISFIED_BY]->(s)
                        RETURN c.id AS case_id, c.title AS case, c.bid_date AS bid_date, k.id AS clause_id,
                               k.ref AS ref, k.kind AS kind, k.params AS params, k.verdict AS verdict,
                               k.prefer_reference AS pref, collect(s.key) AS sat""",
                     {"kinds": kinds})
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
                     "clause_id": c["clause_id"], "satisfied": sat}
            if new != c["verdict"]:
                flips.append(entry)
            elif sorted(k for _, k, _ in sat) != sorted(c["sat"]):
                rewired.append(entry)       # same verdict, different evidence: still rewire SATISFIED_BY
    sg = {"nodes": [{"id": f"{label}:{key}", "label": label, "name": key}]
          + [{"id": f"Case:{d['case_id']}", "label": "Case", "name": d["case"]} for d in direct]
          + [{"id": f"Bid:{b['bid']}", "label": "Bid", "name": b["bid"]} for b in bids],
          "edges": [{"source": f"Case:{d['case_id']}", "target": f"{label}:{key}", "type": "RELIES_ON"} for d in direct]
          + [{"source": f"Bid:{b['bid']}", "target": f"{label}:{key}", "type": "CITES"} for b in bids]}
    return {"result": {"subject": f"{label}:{key}", "cases_relying": direct, "past_bids_citing": bids,
                       "tenders_secured": tenders, "clauses_rechecked": len(clauses), "verdict_flips": flips,
                       "evidence_changed": rewired},
            "subgraph": sg,
            "cypher": [f"MATCH (s:{label} {{key:$k}})<-[:SATISFIED_BY]-(k:Clause)<-[:HAS_CLAUSE]-(c:Case) "
                       "RETURN DISTINCT c.id, c.title, k.id",
                       f"MATCH (s:{label} {{key:$k}})<-[:CITES|PROPOSES|SECURED_BY]-(b:Bid) RETURN b.key, b.submitted",
                       f"MATCH (s:{label} {{key:$k}})-[:SECURES]->(t:Tender) RETURN t.key",
                       "MATCH (c:Case {status:'open'})-[:HAS_CLAUSE]->(k:Clause) WHERE k.kind IN $kinds "
                       "RETURN c.id, c.bid_date, k.id, k.kind, k.params, k.verdict   // each is re-run with qualify_clause"]}


# finding kinds that describe the clause's *current* verdict (a gap closes, a ripple note is
# history once the clause passes) and so do not hold a PASS clause back
_VERDICT_BOUND = ("gap", "ripple")


def overall_verdict(g: Graph, case_id: str) -> str:
    """The case recommendation — the one rule both the Assembler and the ripple use.

    NO-BID if any clause FAILs; BID WITH FIXES if any is AT_RISK or carries a live high-severity
    finding (on any clause, PASS included: a contested fact or a missing reference letter is a
    risk even where the clause passes); else BID. A 'conflict' finding stays live only while a
    claim it cites is still contested."""
    rows = g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause)
                     OPTIONAL MATCH (k)-[:HAS_FINDING]->(f:Finding {severity:'high'})
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
    return "NO-BID" if "FAIL" in verds else ("BID WITH FIXES" if "AT_RISK" in verds or high else "BID")


def apply_flips(g: Graph, flips: list[dict], cause: str, evidence_changed: list[dict] | None = None) -> list[dict]:
    """Write ripple results back: new verdicts *and* the evidence they now rest on (SATISFIED_BY is
    rebuilt), a 'ripple' finding naming the cause, and the case's recomputed overall recommendation.
    `evidence_changed` (blast_radius's list of same-verdict clauses whose satisfying set moved) is
    rewired too. Returns per-case before/after."""
    for f in flips:
        _rewire(g, f)
        add_finding(g, f["clause_id"], "ripple", f"verdict changed {f['from']} -> {f['to']} because {cause}",
                    severity="high" if f["to"] == "FAIL" else "medium")
    for f in evidence_changed or []:
        _rewire(g, f)
        add_finding(g, f["clause_id"], "ripple", f"verdict {f['to']} unchanged, but the evidence it rests on "
                                                 f"changed because {cause}", severity="info")
    changed = []
    for cid in sorted({f["case_id"] for f in flips + list(evidence_changed or [])}):
        before = g.scalar("MATCH (c:Case {id:$c}) RETURN c.verdict", {"c": cid})
        after = overall_verdict(g, cid)
        g.run("MATCH (c:Case {id:$c}) SET c.verdict = $v", {"c": cid, "v": after})
        changed.append({"case_id": cid, "from": before, "to": after})
        for f in flips:
            if f["case_id"] == cid:
                f["case_from"], f["case_to"] = before, after
    return changed


def _rewire(g: Graph, f: dict) -> None:
    if "satisfied" in f:
        set_verdict(g, f["clause_id"], f["to"], f["basis"], [tuple(x) for x in f["satisfied"]])
    else:                               # a flip computed by an older blast_radius: verdict only
        g.run("MATCH (k:Clause {id:$id}) SET k.verdict = $v, k.basis = $b",
              {"id": f["clause_id"], "v": f["to"], "b": f["basis"]})
