"""The tender qualification team: a planner, six agents, one shared case graph.

    ClauseParser -> Planner -> EvidenceHunter -> AbsenceAuditor -> ConflictSentinel -> Verifier -> Assembler
                                     ^   ^              |                                    |
                                     |   +-- re-select referenced works (absence finding) --+|
                                     +------ re-derive a clause once (verifier mismatch) ----+

Each agent is an AgentRun node; it reads what the others wrote to the Case subgraph and writes its own
conclusions there (HANDED_OFF_TO links the runs, in the order they actually ran).

* The **Planner** (no LLM) reads the clause kinds and decides which agents run on which clauses: the
  AbsenceAuditor only audits clauses that cite works or people, the ConflictSentinel runs only if a fact
  a verdict relies on has claim history (checked once the evidence is chosen). The plan is stored on the
  Case (`c.plan`), every agent reads its assignment from there, and the plan is streamed as a `plan` event.
* **Hand-backs.** The AbsenceAuditor's finding that the works a similar-works clause relies on have no
  reference letter is a request on the Finding node; when referenced alternates exist the EvidenceHunter
  runs again, re-selects preferring referenced works (kept only if the clause passes as well with them)
  and marks the finding resolved. A Verifier mismatch is likewise a request: the EvidenceHunter re-derives
  that clause once and the Verifier re-checks; NEEDS REVIEW only if the mismatch persists.

Only two steps need language: reading the tender (ClauseParser) and writing the memo (Assembler). Both
degrade to deterministic fallbacks, so a demo can never be stopped by a rate limit.
"""
from __future__ import annotations

import json
import time
from collections import Counter

from bidbrain import config
from bidbrain.agents import clauses as clause_mod
from bidbrain.agents import providers
from bidbrain.agents.loop import run_agent
from bidbrain.agents.prompts import ASSEMBLER_SYSTEM
from bidbrain.graph.client import Graph
from bidbrain.tools import call
from bidbrain.tools import case as C
from bidbrain.tools.proof import _day_before, _years_before, prove_absence, qualify_clause
from bidbrain.tools.read import find_portfolio

# which facts each clause kind actually depends on (ConflictSentinel only reports these)
RELEVANT = {"similar_works": ["value", "completed", "role", "client", "grading"],
            "turnover": ["revenue_operations"], "key_personnel": ["experience_years", "valid_through"],
            "certification": ["valid_until"], "years_in_business": [], "other": []}
RANK = {"PASS": 2, "AT_RISK": 1}                          # verdicts that pass, best first
AUDITABLE = {"similar_works", "key_personnel"}            # clauses that cite works or people
PIPELINE = ["EvidenceHunter", "AbsenceAuditor", "ConflictSentinel", "Verifier", "Assembler"]
CONTINGENCIES = [
    "AbsenceAuditor -> EvidenceHunter: works a similar-works clause relies on have no reference letter and "
    "referenced alternates exist: re-select preferring referenced works, kept only if the clause passes as well",
    "Verifier -> EvidenceHunter: a re-derived verdict disagrees with the case: re-derive that clause once and "
    "re-verify; NEEDS REVIEW only if the mismatch persists",
]
_HISTORY = """MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause)-[:SATISFIED_BY]->(s) WHERE k.id IN $ids
              MATCH (s)-[:HOLDS*0..1]->(x)<-[:ABOUT]-(cl:Claim)
              WHERE cl.status IN ['contested','rejected','superseded'] AND cl.predicate IN $preds[k.kind]"""


class _Meter:
    """Per-agent cost: wall ms, LLM calls, tool calls. Written onto the AgentRun node when the agent
    finishes (so the case graph records what each agent did and what it cost) and returned in `agents`.
    Runs are chained by HANDED_OFF_TO in the order they ran, so a hand-back shows as an edge back to an
    agent that already ran (its second AgentRun carries `pass_no`)."""

    def __init__(self, g: Graph, case_id: str):
        self.g, self.case_id, self.runs, self.prev = g, case_id, [], None
        self.passes: Counter = Counter()

    def start(self, agent: str, **props) -> str:
        self.passes[agent] += 1
        if self.passes[agent] > 1:
            props["pass_no"] = self.passes[agent]
        rid = C.agent_run(self.g, self.case_id, agent, prev_run=self.prev, **props)
        self.runs.append({"agent": agent, "run": rid, "t0": time.perf_counter(), "llm_calls": 0, "tool_calls": 0,
                          "prompt_tokens": 0, "completion_tokens": 0})
        self.prev = rid
        return rid

    def tool(self, n: int = 1) -> None:
        self.runs[-1]["tool_calls"] += n

    def llm(self, n: int = 1, prompt: int = 0, completion: int = 0) -> None:
        r = self.runs[-1]
        r["llm_calls"] += n
        r["prompt_tokens"] += prompt
        r["completion_tokens"] += completion

    def done(self, **props) -> None:
        r = self.runs[-1]
        r["ms"] = round((time.perf_counter() - r.pop("t0")) * 1000, 1)
        C.agent_done(self.g, r["run"], ms=r["ms"], llm_calls=r["llm_calls"], tool_calls=r["tool_calls"],
                     prompt_tokens=r["prompt_tokens"], completion_tokens=r["completion_tokens"], **props)


def select(g: Graph, kind: str, params: dict, bid_date: str | None, prefer_reference: bool = False):
    """The EvidenceHunter's selection policy, and the one the Verifier re-derives with.

    By default the engine's own pick. With `prefer_reference` (similar works only) the clause is re-run
    over referenced works, and that selection is kept only if it passes at least as well as the default
    (never trading PASS for AT_RISK or FAIL). Returns (qualify_clause output, used referenced?, tool calls);
    a referenced selection that was tried and not kept leaves its verdict in out['referenced_verdict']."""
    out = qualify_clause(g, kind, params, bid_date)
    if not prefer_reference or kind != "similar_works" or params.get("require_reference"):
        return out, False, 1
    ref = qualify_clause(g, kind, {**params, "require_reference": True}, bid_date)
    if RANK.get(ref["result"]["verdict"], 0) >= max(1, RANK.get(out["result"]["verdict"], 0)):
        return ref, True, 2
    out["referenced_verdict"] = ref["result"]["verdict"]
    return out, False, 2


def make_plan(clause_ids: list[tuple[str, dict]]) -> dict:
    """The Planner: which agents run, on which clauses, from the clause kinds alone (no LLM)."""
    provable = [(k, c) for k, c in clause_ids if c["kind"] != "other"]
    groups = {"EvidenceHunter": clause_ids,
              "AbsenceAuditor": [(k, c) for k, c in clause_ids if c["kind"] in AUDITABLE],
              "ConflictSentinel": [(k, c) for k, c in provable if RELEVANT.get(c["kind"])],
              "Verifier": provable}
    skip = {}
    if not groups["AbsenceAuditor"]:
        skip["AbsenceAuditor"] = "no clause cites works or people"
    if not groups["ConflictSentinel"]:
        skip["ConflictSentinel"] = "no clause relies on a fact that can carry claim history"
    return {"agents": [a for a in PIPELINE if a not in skip], "skip": skip,
            "kinds": dict(Counter(c["kind"] for _, c in clause_ids)),
            "routes": {a: [k for k, _ in xs] for a, xs in groups.items()},
            "route_refs": {a: [c["id"] for _, c in xs] for a, xs in groups.items()},
            "conditions": {"ConflictSentinel": "runs only if a fact a verdict relies on has claim history "
                                               "(checked once the evidence is chosen)"}
            if "ConflictSentinel" not in skip else {},
            "contingencies": CONTINGENCIES, "checkpoints": [], "handoffs": []}


class _Team:
    def __init__(self, g: Graph, tender: dict, case_id: str, emit, M: _Meter):
        self.g, self.tender, self.case_id, self.emit, self.M = g, tender, case_id, emit, M
        self.bid_date = tender.get("bid_date")
        self.plan: dict = {}

    # ── plan, kept on the Case node ─────────────────────────────────────────
    def save_plan(self, stage: str, **extra) -> None:
        blob = json.dumps(self.plan, default=str)
        self.g.run("MATCH (c:Case {id:$c}) SET c.plan = $p", {"c": self.case_id, "p": blob})
        p = json.loads(blob)        # a snapshot: later stages must not rewrite an event already emitted
        self.emit({"type": "plan", "agent": "Planner", "stage": stage, "agents": p["agents"], "skip": p["skip"],
                   "routes": p["route_refs"], "conditions": p["conditions"], "kinds": p["kinds"],
                   "contingencies": p["contingencies"], **extra})

    def assigned(self, agent: str) -> list[str]:
        """An agent's clauses, read from the plan on the Case node (the plan is the graph's, not Python's)."""
        p = self.g.read("MATCH (c:Case {id:$c}) RETURN c.plan AS p", {"c": self.case_id})[0]["p"]
        return json.loads(p or "{}").get("routes", {}).get(agent, [])

    def skip(self, agent: str, why: str) -> None:
        self.plan["skip"][agent] = why
        self.plan["agents"] = [a for a in self.plan["agents"] if a != agent]
        self.emit({"type": "agent_done", "agent": agent, "summary": f"skipped by the Planner: {why}",
                   "skipped": True})

    def clauses(self, ids: list[str]) -> list[dict]:
        """Clauses as the case graph holds them now (kind may have been downgraded, verdict re-derived)."""
        rows = self.g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause) WHERE k.id IN $ids
                              OPTIONAL MATCH (k)-[:SATISFIED_BY]->(s)
                              RETURN k.id AS id, k.ref AS ref, k.kind AS kind, k.text AS text, k.params AS params,
                                     k.verdict AS verdict, k.detail AS detail, k.prefer_reference AS pref,
                                     collect(CASE WHEN s:Work THEN s.key END) AS works,
                                     collect(s.key) AS sat""", {"c": self.case_id, "ids": ids})
        order = {k: i for i, k in enumerate(ids)}
        for r in rows:
            r["params"] = json.loads(r["params"] or "{}")
            r["detail"] = json.loads(r["detail"] or "{}")
            r["works"] = [w for w in r["works"] if w]
        return sorted(rows, key=lambda r: order[r["id"]])

    def requests(self, request: str) -> list[dict]:
        """Open hand-back requests one agent left on a Finding for another."""
        return self.g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause)-[:HAS_FINDING]->(f:Finding)
                              WHERE f.request = $r AND f.status = 'open'
                              RETURN k.id AS clause_id, f.id AS finding ORDER BY k.ref""",
                           {"c": self.case_id, "r": request})

    def request(self, finding: str, request: str, run: str, **props) -> None:
        self.g.run("MATCH (f:Finding {id:$f}) SET f.status = 'open', f.request = $q, f.requested_by = $r, f += $p",
                   {"f": finding, "q": request, "r": run, "p": props})

    def close(self, finding: str, run: str, status: str, note: str, severity: str | None = None) -> None:
        self.g.run("""MATCH (f:Finding {id:$f}), (r:AgentRun {id:$r})
                      SET f.status = $s, f.text = f.text + ' ' + $n, f.closed_by = $r,
                          f.severity = coalesce($sev, f.severity)
                      MERGE (r)-[:RESOLVED]->(f)""",
                   {"f": finding, "r": run, "s": status, "n": note, "sev": severity})

    # ── EvidenceHunter: one clause ──────────────────────────────────────────
    def hunt(self, kid: str, c: dict, run: str, prefer_reference: bool = False, selected=None) -> dict | None:
        """Check one clause and write the verdict, SATISFIED_BY and detail. `selected` is a (output,
        used referenced?) pair the caller already computed with `select`."""
        g = self.g
        if c["kind"] == "other":
            C.set_verdict(g, kid, "DECLARE", "self-declaration / document to enclose — not provable from records",
                          [], run)
            self.emit({"type": "verdict", "agent": "EvidenceHunter", "clause_id": kid, "ref": c["id"],
                       "verdict": "DECLARE", "basis": "declaration — include signed undertaking"})
            return None
        try:
            if selected:
                (out, used_ref), n = selected, 0
            else:
                out, used_ref, n = select(g, c["kind"], c["params"], self.bid_date, prefer_reference)
        except Exception as exc:        # one unreadable clause must not abort the whole case
            msg = f"could not check this clause automatically ({exc}); verify by hand"
            C.set_verdict(g, kid, "DECLARE", msg, [], run)
            g.run("MATCH (k:Clause {id:$k}) SET k.kind = 'other'", {"k": kid})
            self.emit({"type": "verdict", "agent": "EvidenceHunter", "clause_id": kid, "ref": c["id"],
                       "kind": c["kind"], "verdict": "DECLARE", "basis": msg})
            return None
        self.M.tool(n)
        res = out["result"]
        C.set_verdict(g, kid, res["verdict"], res["basis"], C.satisfied_edges(c["kind"], res), run)
        # what later agents need beyond verdict + SATISFIED_BY is written to the clause too: the handoff is the graph
        g.run("MATCH (k:Clause {id:$k}) SET k.detail = $d, k.prefer_reference = $p", {"k": kid, "p": used_ref, "d": json.dumps(
            {f: res.get(f) for f in ("expiring_within_90d", "safe_alternates", "near_misses") if res.get(f)}, default=str)})
        self.emit({"type": "verdict", "agent": "EvidenceHunter", "clause_id": kid, "ref": c["id"], "kind": c["kind"],
                   "verdict": res["verdict"], "basis": res["basis"], "subgraph": out.get("subgraph"),
                   "evidence": out.get("evidence", [])[:8], "cypher": out.get("cypher", [])[:1],
                   **({"selection": "referenced works preferred"} if used_ref else {})})
        res["_referenced"] = used_ref
        return res

    # ── AbsenceAuditor helpers ──────────────────────────────────────────────
    def referenced_alternates(self, params: dict, exclude: list[str]) -> list[str]:
        """Referenced works in the clause's own scope (categories, window, government level, JV rule)
        that the current selection does not use: the precondition for handing the clause back."""
        bid_date = self.bid_date or config.AS_OF
        since = _years_before(bid_date, int(params.get("lookback_years", 7)))
        pf = find_portfolio(self.g, categories=params.get("categories"), completed_after=_day_before(since),
                            completed_before=bid_date, govt_levels=params.get("govt_levels"),
                            role=None if params.get("include_jv", True) else "Prime", has_reference=True)
        return [w["key"] for w in pf["result"]["works"] if w["key"] not in exclude]

    # ── hand-back 1: AbsenceAuditor -> EvidenceHunter ───────────────────────
    def reselect_referenced(self) -> None:
        reqs = self.requests("reselect_referenced")
        if not reqs:
            return
        g, M = self.g, self.M
        refs = {c["id"]: c["ref"] for c in self.clauses([r["clause_id"] for r in reqs])}
        run = M.start("EvidenceHunter", requested_by="AbsenceAuditor", reason="reselect_referenced")
        self.emit({"type": "handoff", "from": "AbsenceAuditor", "to": "EvidenceHunter", "run": run,
                   "reason": "satisfying works have no reference letter; referenced alternates exist",
                   "clauses": [refs.get(r["clause_id"]) for r in reqs]})
        self.emit({"type": "agent_start", "agent": "EvidenceHunter", "run": run, "pass_no": M.passes["EvidenceHunter"]})
        outcomes = []
        for rq in reqs:
            k = self.clauses([rq["clause_id"]])[0]
            before = k["verdict"]
            out, used_ref, n = select(g, k["kind"], k["params"], self.bid_date, prefer_reference=True)
            M.tool(n)
            if used_ref:
                res = self.hunt(k["id"], {"id": k["ref"], "kind": k["kind"], "params": k["params"]}, run,
                                selected=(out, used_ref))
                titles = ", ".join(w.get("title", w.get("key", "?")) for w in res.get("satisfied_by") or [])
                note = f"Resolved by the EvidenceHunter: re-selected referenced works ({titles}); {res['verdict']}."
                self.close(rq["finding"], run, "resolved", note, severity="info")
                self.emit({"type": "finding", "agent": "EvidenceHunter", "clause_id": k["id"], "kind": "resolved",
                           "severity": "info", "text": f"Absence resolved: the clause now relies on referenced "
                                                       f"works ({titles}) and still {res['verdict']}."})
                outcomes.append({"clause": k["ref"], "outcome": "reselected", "verdict": res["verdict"]})
            else:
                note = (f"The EvidenceHunter tried referenced works only: {out.get('referenced_verdict')} "
                        f"(vs {before}); kept the original selection.")
                self.close(rq["finding"], run, "kept", note)
                self.emit({"type": "finding", "agent": "EvidenceHunter", "clause_id": k["id"], "kind": "kept",
                           "severity": "info", "text": note})
                outcomes.append({"clause": k["ref"], "outcome": "kept", "verdict": before})
        n_re = sum(o["outcome"] == "reselected" for o in outcomes)
        M.done(reselected=n_re, requests=len(reqs))
        self.plan["handoffs"].append({"from": "AbsenceAuditor", "to": "EvidenceHunter", "run": run,
                                      "why": "no reference letter", "outcomes": outcomes})
        self.emit({"type": "agent_done", "agent": "EvidenceHunter",
                   "summary": f"re-selected {n_re} of {len(reqs)} clause(s) with referenced works"})

    # ── Verifier ────────────────────────────────────────────────────────────
    def verify(self, ids: list[str]) -> tuple[list[dict], int]:
        mismatches, checked = [], 0
        for s in self.clauses(ids):
            if s["kind"] == "other":
                continue
            checked += 1
            out, _, n = select(self.g, s["kind"], s["params"], self.bid_date, bool(s["pref"]))
            self.M.tool(n)
            fresh = out["result"]
            fresh_keys = sorted(x["key"] for x in fresh.get("satisfied_by") or [] if "key" in x)
            stored_keys = sorted(k for k in s["sat"] if k and not k.startswith("FS-") and k != "company")
            if not (fresh["verdict"] == s["verdict"] and (not fresh_keys or fresh_keys == stored_keys)):
                mismatches.append({"clause": s["ref"] or s["id"], "clause_id": s["id"], "stored": s["verdict"],
                                   "fresh": fresh["verdict"]})
        return mismatches, checked

    # ── hand-back 2: Verifier -> EvidenceHunter -> Verifier ─────────────────
    def rederive(self, first: list[dict]) -> list[dict]:
        g, M = self.g, self.M
        reqs = self.requests("rederive")
        run = M.start("EvidenceHunter", requested_by="Verifier", reason="rederive")
        self.emit({"type": "handoff", "from": "Verifier", "to": "EvidenceHunter", "run": run,
                   "reason": "re-derived verdict disagrees with the case", "clauses": [m["clause"] for m in first]})
        self.emit({"type": "agent_start", "agent": "EvidenceHunter", "run": run, "pass_no": M.passes["EvidenceHunter"]})
        ids = [r["clause_id"] for r in reqs]
        for k in self.clauses(ids):
            self.hunt(k["id"], {"id": k["ref"], "kind": k["kind"], "params": k["params"]}, run, bool(k["pref"]))
        M.done(rederived=len(ids))
        self.emit({"type": "agent_done", "agent": "EvidenceHunter", "summary": f"re-derived {len(ids)} clause(s)"})
        rv = M.start("Verifier", uses_llm=False, requested_by="EvidenceHunter", reason="re-verify")
        self.emit({"type": "agent_start", "agent": "Verifier", "run": rv, "pass_no": M.passes["Verifier"]})
        left, _ = self.verify(ids)
        bad = {m["clause_id"] for m in left}
        for r in reqs:
            if r["clause_id"] in bad:
                self.close(r["finding"], rv, "persisted", "Still differs after one re-derivation: needs a person.")
            else:
                self.close(r["finding"], rv, "resolved",
                           "Resolved: re-derived by the EvidenceHunter and re-verified.", severity="info")
        M.done(mismatches=len(left))
        self.plan["handoffs"].append({"from": "Verifier", "to": "EvidenceHunter", "run": run,
                                      "why": "verdict mismatch", "outcomes": [
                                          {"clause": m["clause"], "outcome": "persisted" if m["clause_id"] in bad
                                           else "resolved"} for m in first]})
        return left


def qualify_tender(g: Graph, tender: dict, emit=None, prefer_llm: bool = True) -> dict:
    emit = emit or (lambda e: None)
    t_start = time.perf_counter()
    case_id = C.case_open(g, title=tender.get("title", "Tender"), tender_text=tender.get("text", ""),
                          bid_date=tender.get("bid_date"), tender_ref=tender.get("id"),
                          category=tender.get("category"))["result"]["case_id"]
    emit({"type": "case_open", "case_id": case_id, "title": tender.get("title")})
    M = _Meter(g, case_id)
    T = _Team(g, tender, case_id, emit, M)

    # 1 ── ClauseParser ──────────────────────────────────────────────────────
    r1 = M.start("ClauseParser")
    emit({"type": "agent_start", "agent": "ClauseParser", "run": r1})
    if tender.get("clauses"):
        # a sample tender whose text was not edited keeps its vetted clauses; the LLM parses new or edited text
        parsed, how = tender["clauses"], "pre-parsed"
    else:
        parsed, how = clause_mod.parse(tender.get("text", ""), prefer_llm=prefer_llm)
        if how == "llm":
            M.llm()
    clause_ids = []
    for c in parsed:
        kid = C.add_clause(g, case_id, c, r1)
        clause_ids.append((kid, c))
        emit({"type": "clause", "agent": "ClauseParser", "clause_id": kid, "ref": c["id"], "kind": c["kind"],
              "text": c["text"], "params": c["params"]})
    M.done(method=how, n_clauses=len(parsed))
    emit({"type": "agent_done", "agent": "ClauseParser", "summary": f"{len(parsed)} clauses ({how})"})

    # 2 ── Planner (no LLM): who runs, on which clauses ──────────────────────
    rp = M.start("Planner", uses_llm=False)
    emit({"type": "agent_start", "agent": "Planner", "run": rp})
    T.plan = make_plan(clause_ids)
    T.save_plan("initial")
    M.done(agents=",".join(T.plan["agents"]), skipped=",".join(T.plan["skip"]))
    emit({"type": "agent_done", "agent": "Planner", "summary": " → ".join(T.plan["agents"])
          + (f" (skip {', '.join(T.plan['skip'])})" if T.plan["skip"] else "")})

    # 3 ── EvidenceHunter ────────────────────────────────────────────────────
    r2 = M.start("EvidenceHunter")
    emit({"type": "agent_start", "agent": "EvidenceHunter", "run": r2})
    mine = set(T.assigned("EvidenceHunter"))
    for kid, c in clause_ids:
        if kid in mine:
            T.hunt(kid, c, r2)
    M.done(n_clauses=len(mine))
    emit({"type": "agent_done", "agent": "EvidenceHunter"})

    # 4 ── AbsenceAuditor (only clauses that cite works or people) ───────────
    n_find = 0
    if "AbsenceAuditor" in T.plan["skip"]:
        T.skip("AbsenceAuditor", T.plan["skip"]["AbsenceAuditor"])
    else:
        r3 = M.start("AbsenceAuditor")
        emit({"type": "agent_start", "agent": "AbsenceAuditor", "run": r3})
        # read the EvidenceHunter's work back from the case subgraph (not from memory)
        for h in T.clauses(T.assigned("AbsenceAuditor")):
            kid, kind = h["id"], h["kind"]
            res = {"verdict": h["verdict"], **h["detail"], "satisfied_by": [{"key": k} for k in h["works"]]}
            if kind == "similar_works" and res["satisfied_by"]:
                keys = [w["key"] for w in res["satisfied_by"]]
                missing = g.read("MATCH (w:Work) WHERE w.key IN $k AND NOT (w)-[:HAS_REFERENCE]->() "
                                 "RETURN w.key AS key, w.title AS title", {"k": keys})
                if missing:
                    # one absence proof per client of the missing works, so the proof covers every work named
                    clients = [r["client"] for r in g.read(
                        """MATCH (w:Work)-[:FOR_CLIENT]->(c:Client) WHERE w.key IN $k
                           RETURN DISTINCT c.key AS client ORDER BY client""", {"k": [m["key"] for m in missing]})]
                    proofs = [prove_absence(g, "reference_letter", client=ck) for ck in clients]
                    M.tool(len(proofs))
                    proof = proofs[0]
                    checks = [p["result"]["checks"] for p in proofs]
                    txt = (f"No client reference letter on file for {', '.join(m['title'] for m in missing)} "
                           f"(proof across {len(proofs)} client(s): {checks[0]['corpus_closure']}; boundary "
                           f"{', '.join(str(ch['boundary_attested_by_3_views']) for ch in checks)} attested). "
                           f"Request letters from the client before submission or cite alternates.")
                    alts = T.referenced_alternates(h["params"], keys)
                    M.tool()
                    if alts:
                        txt += (f" {len(alts)} referenced work(s) in the clause's scope: handed back to the "
                                f"EvidenceHunter to re-select.")
                    fid = C.add_finding(g, kid, "absence", txt, "high" if "reference" in (h["text"] or "").lower()
                                        else "medium", [("Work", m["key"]) for m in missing], r3)
                    if alts:
                        T.request(fid, "reselect_referenced", r3, alternates=len(alts))
                    n_find += 1
                    emit({"type": "finding", "agent": "AbsenceAuditor", "clause_id": kid, "kind": "absence",
                          "text": txt, "subgraph": proof.get("subgraph")})
            if kind == "key_personnel":
                exp = res.get("expiring_within_90d") or []
                if exp:
                    alts = ", ".join(a["name"] for a in res.get("safe_alternates", []))
                    txt = (f"PMP of {', '.join(e['name'] + ' (to ' + e['valid_through'] + ')' for e in exp[:4])} lapses "
                           f"within 90 days of the bid date. Renew now, or propose {alts or 'alternates'} (valid well beyond).")
                    names = [e["name"] for e in exp]
                    keys = [r["key"] for r in g.read("MATCH (p:Person) WHERE p.name IN $n RETURN p.key AS key",
                                                     {"n": names})]
                    C.add_finding(g, kid, "expiry", txt, "high" if res.get("verdict") == "AT_RISK" else "low",
                                  [("Person", k) for k in keys], r3)
                    n_find += 1
                    emit({"type": "finding", "agent": "AbsenceAuditor", "clause_id": kid, "kind": "expiry", "text": txt})
            if kind == "similar_works" and res.get("verdict") == "FAIL" and res.get("near_misses"):
                nm = res["near_misses"][:3]
                txt = ("No qualifying combination. Closest works: " +
                       "; ".join(f"{w['title']} ({w['value'] / 1e7:.2f} Cr)" for w in nm) +
                       ". Consider a JV with a partner holding a larger bridge credential.")
                C.add_finding(g, kid, "gap", txt, "high", [("Work", w["key"]) for w in nm], r3)
                n_find += 1
                emit({"type": "finding", "agent": "AbsenceAuditor", "clause_id": kid, "kind": "gap", "text": txt})
        M.done(findings=n_find)
        emit({"type": "agent_done", "agent": "AbsenceAuditor", "summary": f"{n_find} findings"})
        T.reselect_referenced()      # the EvidenceHunter acts on the auditor's requests, read from the graph

    # 5 ── Planner checkpoint, then ConflictSentinel ─────────────────────────
    n_hist = 0
    if "ConflictSentinel" not in T.plan["skip"]:
        ids = T.assigned("ConflictSentinel")
        n_hist = g.scalar(_HISTORY + " RETURN count(DISTINCT x.key + '|' + cl.predicate)",
                          {"c": case_id, "ids": ids, "preds": RELEVANT}) or 0
        decision = "run" if n_hist else "skip"
        T.plan["checkpoints"].append({"before": "ConflictSentinel", "facts_with_history": n_hist,
                                      "decision": decision})
        if not n_hist:
            T.skip("ConflictSentinel", "no fact the verdicts rely on has claim history")
        T.save_plan("checkpoint", decision={"ConflictSentinel": decision, "facts_with_history": n_hist})
    else:
        T.skip("ConflictSentinel", T.plan["skip"]["ConflictSentinel"])
    if "ConflictSentinel" not in T.plan["skip"]:
        r4 = M.start("ConflictSentinel")
        emit({"type": "agent_start", "agent": "ConflictSentinel", "run": r4})
        # a claim about the satisfying node itself, or about a credential that person HOLDS (PMP validity);
        # its resolution is the *head* decision for that fact (the one nothing supersedes), not whichever
        # past decision happened to cite the claim
        rows = g.read(_HISTORY + """
                         OPTIONAL MATCH (d:Decision {subject_key: x.key, predicate: cl.predicate})
                         WHERE NOT ()-[:SUPERSEDES]->(d)
                         WITH k, s, x, cl, d ORDER BY d.at DESC
                         WITH k, s, x, cl, collect(d) AS ds
                         RETURN k.id AS clause_id, labels(s)[0] AS label, x.key AS key,
                                coalesce(s.title, s.name, s.key) AS name, cl.predicate AS predicate,
                                cl.value_str AS value, cl.source_doc AS doc, cl.status AS status, cl.id AS claim,
                                [y IN ds[0..1] | {policy: y.policy, why: y.rationale, chosen: y.chosen_value}] AS decisions
                         ORDER BY clause_id, key, predicate, claim""",
                      {"c": case_id, "ids": T.assigned("ConflictSentinel"), "preds": RELEVANT})
        seen = set()
        for r in rows:
            if (r["clause_id"], r["key"], r["predicate"]) in seen:
                continue
            seen.add((r["clause_id"], r["key"], r["predicate"]))
            if r["status"] == "contested":
                txt = (f"{r['name']}: '{r['predicate']}' is contested ({r['value']} in {r['doc']}) — "
                       f"open resolve card; verdict relies on an unresolved fact.")
                sev = "high"
            else:
                d = r["decisions"][0] if r["decisions"] else {}
                txt = (f"{r['name']}: '{r['predicate']}' had a competing value {r['value']} ({r['doc']}) "
                       f"resolved by {d.get('policy', 'policy')} → {d.get('chosen', '?')}. {d.get('why', '')}")
                sev = "info"
            C.add_finding(g, r["clause_id"], "conflict", txt, sev, [("Claim", r["claim"])], r4)
            emit({"type": "finding", "agent": "ConflictSentinel", "clause_id": r["clause_id"], "kind": "conflict",
                  "text": txt, "severity": sev})
        M.done(findings=len(seen))
        emit({"type": "agent_done", "agent": "ConflictSentinel", "summary": f"{len(seen)} facts with history"})

    # 6 ── Verifier (no LLM), with one re-derivation on a mismatch ───────────
    r5 = M.start("Verifier", uses_llm=False)
    emit({"type": "agent_start", "agent": "Verifier", "run": r5})
    first, checked = T.verify(T.assigned("Verifier"))
    for m in first:
        fid = C.add_finding(g, m["clause_id"], "verification_failed",
                            f"Verifier re-derived {m['fresh']} but the case says {m['stored']}; asked the "
                            f"EvidenceHunter to re-derive it once.", "high", [], r5)
        T.request(fid, "rederive", r5)
    M.done(mismatches=len(first))
    mismatches = T.rederive(first) if first else []
    g.run("MATCH (c:Case {id:$c}) SET c.verified = $v", {"c": case_id, "v": not mismatches})
    emit({"type": "verified", "agent": "Verifier", "ok": not mismatches, "mismatches": mismatches,
          "checked": checked, "retried": first})
    T.save_plan("final", handoffs=T.plan["handoffs"])

    # 7 ── Assembler ─────────────────────────────────────────────────────────
    r6 = M.start("Assembler")
    emit({"type": "agent_start", "agent": "Assembler", "run": r6})
    rec = C.case_get(g, case_id)["result"]
    prec = call(g, "precedents", {"category": tender.get("category")})
    M.tool()
    for p in prec.get("result", [])[:2]:
        g.run("MATCH (c:Case {id:$c}), (b:Bid {key:$b}) MERGE (c)-[:PRECEDENT]->(b)", {"c": case_id, "b": p["bid"]})
    overall = C.overall_verdict(g, case_id)      # the same rule the ripple recomputes with
    if mismatches:
        # the Verifier's re-derivation still disagrees after one retry: never recommend on that record
        overall = "NEEDS REVIEW"
    evidence = _evidence(g, case_id)
    memo, how = None, "template"
    if prefer_llm and (providers.available() or providers.REPLAY):
        try:
            ctx = json.dumps({"tender": {k: tender.get(k) for k in ("id", "title", "authority", "bid_date")},
                              "overall": overall, "clauses": rec["clauses"], "evidence": evidence[:40],
                              "verifier": {"ok": not mismatches, "mismatches": mismatches, "retried": first},
                              "team_handoffs": T.plan["handoffs"],
                              "precedents": prec.get("result", [])[:2]}, default=str)[:16000]
            res = run_agent(g, "Assembler", ASSEMBLER_SYSTEM, ctx, ["who_knows", "key_people"], max_steps=4, emit=emit)
            M.llm(res.usage["llm_calls"], res.usage["prompt"], res.usage["completion"])
            M.tool(res.usage["tool_calls"])
            if res.final and not res.error:
                memo, how = res.final, "llm"
        except Exception:
            memo = None
    if not memo:
        memo = _template_memo(tender, rec, overall, prec.get("result", []), evidence, mismatches,
                              T.plan["handoffs"])
    g.run("MATCH (c:Case {id:$c}) SET c.memo = $m, c.verdict = $v, c.status = 'open'",
          {"c": case_id, "m": memo, "v": overall})
    M.done(method=how)
    emit({"type": "memo", "agent": "Assembler", "markdown": memo, "overall": overall, "method": how})
    final = C.case_get(g, case_id)["result"]
    final["ms"] = round((time.perf_counter() - t_start) * 1000, 1)
    final["agents"] = M.runs
    final["plan"] = T.plan
    final["usage"] = {k: sum(r[k] for r in M.runs) for k in ("llm_calls", "tool_calls", "prompt_tokens",
                                                            "completion_tokens")}
    emit({"type": "done", "case_id": case_id, "overall": overall, "ms": final["ms"]})
    return final


def _evidence(g: Graph, case_id: str) -> list[dict]:
    """What each clause relies on, with the document and page that prove it (the bid pack's evidence table)."""
    return g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause)-[sb:SATISFIED_BY]->(s)
                     OPTIONAL MATCH (s)-[e:EVIDENCED_BY]->(d:Document)
                     WITH k, sb, s, e, d ORDER BY CASE e.kind WHEN 'completion_certificate' THEN 0 ELSE 1 END
                     WITH k, sb, s, collect({doc: d.doc_id, page: e.page})[0] AS ev
                     RETURN k.ref AS clause, labels(s)[0] AS kind, coalesce(s.title, s.name, s.fy, s.key) AS item,
                            sb.credited AS credited, ev.doc AS doc, ev.page AS page
                     ORDER BY clause, credited DESC""", {"c": case_id})


def _template_memo(tender: dict, rec: dict, overall: str, precedents: list, evidence: list | None = None,
                   mismatches: list | None = None, handoffs: list | None = None) -> str:
    lines = [f"## {tender.get('title')} — recommendation: **{overall}**", "",
             f"Authority: {tender.get('authority', '—')} · bid due {tender.get('bid_date', '—')}", "",
             "| Clause | Verdict | Basis |", "|---|---|---|"]
    for c in rec["clauses"]:
        lines.append(f"| {c['ref']} {c['kind']} | {c['verdict']} | {(c.get('basis') or '').replace('|', '/')} |")
    if mismatches:
        lines += ["", "> **Verifier:** the re-derived verdict differs for "
                  + ", ".join(f"{m['clause']} ({m['stored']} → {m['fresh']})" for m in mismatches)
                  + ". Recommendation withheld until a person reviews it."]
    if evidence:
        lines += ["", "### Evidence", "", "| Clause | Relies on | Credited | Document | Page |", "|---|---|---|---|---|"]
        for e in evidence:
            credited = f"{int(e['credited']):,}" if isinstance(e.get("credited"), (int, float)) else "—"
            lines.append(f"| {e['clause']} | {e['kind']}: {str(e['item']).replace('|', '/')} | {credited} | "
                         f"{e.get('doc') or '—'} | {e.get('page') or '—'} |")
    risks = [(c["ref"], f) for c in rec["clauses"] for f in c["findings"] if f and f.get("severity") in ("high", "medium")]
    if risks:
        lines += ["", "### Gaps & risks"] + [f"- **{ref}** ({f['kind']}): {f['text']}" for ref, f in risks]
    if handoffs:
        lines += ["", "### Team hand-backs"] + [
            f"- {h['from']} → {h['to']} ({h['why']}): "
            + "; ".join(f"{o['clause']} {o['outcome']}" for o in h["outcomes"]) for h in handoffs]
    if precedents:
        lines += ["", "### Precedents"] + [f"- {p['bid']} ({p['category']}, {p['client']}, submitted {p['submitted']})"
                                           for p in precedents[:2]]
    return "\n".join(lines)
