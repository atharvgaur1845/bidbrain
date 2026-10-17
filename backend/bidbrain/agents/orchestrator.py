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
* The **Verifier** does not replay the EvidenceHunter: it re-reads each clause's text with the rule parser
  and compares the thresholds with the params the team used (a disagreement that would change the verdict
  persists -> NEEDS REVIEW), recomputes every credited value from the winning claims rather than the
  materialized property, and re-derives the verdict. The Assembler rejects an LLM memo that cites a DOC or
  a ₹ amount it was not given (amounts are formatted as ₹X.XX Cr in Python before the model sees them).

Only two steps need language: reading the tender (ClauseParser) and writing the memo (Assembler). Both
degrade to deterministic fallbacks, so a demo can never be stopped by a rate limit.
"""
from __future__ import annotations

import json
import re
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
CLAIM_PRED = {"Work": "value", "FinancialStatement": "revenue_operations", "Person": "experience_years"}
MONEY_LABELS = {"Work", "FinancialStatement"}             # SATISFIED_BY.credited is rupees for these
# the params that decide a threshold (what a params/text disagreement can change)
THRESHOLD_KEYS = {"estimated_cost", "min_value", "count", "options", "lookback_years", "min_average",
                  "min_each_year", "years", "min_experience_years", "min_years", "credential"}
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
                              OPTIONAL MATCH (k)-[sb:SATISFIED_BY]->(s)
                              RETURN k.id AS id, k.ref AS ref, k.kind AS kind, k.text AS text, k.params AS params,
                                     k.verdict AS verdict, k.detail AS detail, k.prefer_reference AS pref,
                                     collect(CASE WHEN s:Work THEN s.key END) AS works,
                                     collect(s.key) AS sat,
                                     collect(CASE WHEN s IS NULL THEN NULL ELSE {label: labels(s)[0], key: s.key,
                                             credited: sb.credited} END) AS edges""", {"c": self.case_id, "ids": ids})
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
    def claim_check(self, s: dict) -> list[str]:
        """Recompute what each SATISFIED_BY edge credits from the winning claims (status 'active', highest
        authority, then most precise) rather than the materialized property, and re-check the clause's
        threshold on those values. Facts with no active claim (contested) are left to the AT_RISK flag."""
        by_label: dict[str, list[dict]] = {}
        for e in s.get("edges") or []:
            if e and e["label"] in CLAIM_PRED and e.get("credited") is not None:
                by_label.setdefault(e["label"], []).append(e)
        if not by_label:
            return []
        out, values = [], {}
        for label, edges in by_label.items():
            rows = self.g.read(f"""MATCH (x:{label}) WHERE x.key IN $keys
                                   OPTIONAL MATCH (c:Claim {{predicate: $p, status: 'active'}})-[:ABOUT]->(x)
                                   WITH x, c ORDER BY coalesce(c.authority, 0) DESC, coalesce(c.precision, 1) ASC
                                   WITH x, collect(c)[0] AS c
                                   RETURN x.key AS key, c.value AS value, c.precision AS prec, c.source_doc AS doc""",
                               {"keys": [e["key"] for e in edges], "p": CLAIM_PRED[label]})
            self.M.tool()
            claim = {r["key"]: r for r in rows}
            for e in edges:
                r = claim.get(e["key"]) or {}
                v = r.get("value")
                if not isinstance(v, (int, float)):
                    continue
                values[e["key"]] = v
                if abs(float(e["credited"]) - float(v)) > max(1.0, float(r.get("prec") or 1)):
                    out.append(f"{e['key']} credited {_fmt_amount(label, e['credited'])} but its winning claim "
                               f"({r.get('doc')}) says {_fmt_amount(label, v)}")
        passing = s["verdict"] in RANK
        p, vals = s["params"], list(values.values())
        if s["kind"] == "similar_works" and passing and vals:
            alts = clause_mod.alternatives(p) or set()
            if alts and not any(sum(v >= mv for v in vals) >= n for n, mv in alts):
                out.append("the credited works' claimed values meet none of the clause's alternatives")
        elif s["kind"] == "turnover" and vals:
            if p.get("min_each_year"):
                ok = all(v >= int(p["min_each_year"]) for v in vals)
            else:
                ok = sum(vals) / len(vals) >= int(p.get("min_average") or 0)
            if ok != passing:
                out.append(f"claimed revenues give {'a pass' if ok else 'a fail'}, the case says {s['verdict']}")
        elif s["kind"] == "key_personnel" and passing and p.get("min_experience_years"):
            short = [k for k, v in values.items() if v < int(p["min_experience_years"])]
            if short:
                out.append(f"claimed experience below {p['min_experience_years']} yrs for {', '.join(short)}")
        return out

    def verify(self, ids: list[str], immaterial: frozenset | set = frozenset()) -> tuple[list[dict], int]:
        """Three checks per clause, none of which is the EvidenceHunter's own reasoning replayed:
        (1) the clause's TEXT re-read by the rule parser must give the params the team used (skipped for a
        clause whose disagreement was already shown not to change the verdict); (2) every credited value is
        recomputed from the winning claims and the threshold re-checked on those; (3) the verdict and the
        satisfying set are re-derived from the graph."""
        mismatches, checked = [], 0
        for s in self.clauses(ids):
            if s["kind"] == "other":
                continue
            checked += 1
            reasons = [] if s["id"] in immaterial else \
                [f"text: {d}" for d in clause_mod.reading_diff(s["kind"], s["params"], s["text"] or "")]
            reasons += [f"claims: {d}" for d in self.claim_check(s)]
            try:
                out, _, n = select(self.g, s["kind"], s["params"], self.bid_date, bool(s["pref"]))
                self.M.tool(n)
                fresh = out["result"]
            except Exception as exc:
                fresh = {"verdict": "ERROR", "satisfied_by": []}
                reasons.append(f"re-derivation failed: {exc}")
            fresh_keys = sorted(x["key"] for x in fresh.get("satisfied_by") or [] if "key" in x)
            stored_keys = sorted(k for k in s["sat"] if k and not k.startswith("FS-") and k != "company")
            if not (fresh["verdict"] == s["verdict"] and (not fresh_keys or fresh_keys == stored_keys)):
                reasons.append(f"re-derived {fresh['verdict']} vs stored {s['verdict']}")
            if reasons:
                mismatches.append({"clause": s["ref"] or s["id"], "clause_id": s["id"], "stored": s["verdict"],
                                   "fresh": fresh["verdict"], "reasons": reasons})
        return mismatches, checked

    def text_verdict(self, k: dict) -> str | None:
        """The verdict the clause would get under its text's reading (thresholds from the rule parser, the
        rest of the clause as the team had it): does a params/text disagreement change the outcome?"""
        rules = [r for r in clause_mod.parse_rules(k["text"] or "") if r["kind"] == k["kind"]]
        if len(rules) != 1:
            return None
        params = {x: v for x, v in k["params"].items() if x not in THRESHOLD_KEYS}
        params.update({x: v for x, v in rules[0]["params"].items() if x in THRESHOLD_KEYS})
        try:
            return select(self.g, k["kind"], params, self.bid_date, bool(k["pref"]))[0]["result"]["verdict"]
        except Exception:
            return None

    # ── hand-back 2: Verifier -> EvidenceHunter -> Verifier ─────────────────
    def rederive(self, first: list[dict]) -> list[dict]:
        g, M = self.g, self.M
        reqs = self.requests("rederive")
        run = M.start("EvidenceHunter", requested_by="Verifier", reason="rederive")
        self.emit({"type": "handoff", "from": "Verifier", "to": "EvidenceHunter", "run": run,
                   "reason": "the Verifier's independent checks disagree with the case",
                   "clauses": [m["clause"] for m in first]})
        self.emit({"type": "agent_start", "agent": "EvidenceHunter", "run": run, "pass_no": M.passes["EvidenceHunter"]})
        ids = [r["clause_id"] for r in reqs]
        by_id = {m["clause_id"]: m for m in first}
        immaterial, notes = set(), {}
        for k in self.clauses(ids):
            res = self.hunt(k["id"], {"id": k["ref"], "kind": k["kind"], "params": k["params"]}, run, bool(k["pref"]))
            m = by_id.get(k["id"]) or {}
            if any(r.startswith("text:") for r in m.get("reasons", [])):
                now = (res or {}).get("verdict", k["verdict"])
                alt = self.text_verdict(k)
                M.tool()
                if alt == now:
                    immaterial.add(k["id"])
                    notes[k["id"]] = (f"its params and its text disagree, but both readings give {now}; "
                                      f"the params were kept")
                else:
                    notes[k["id"]] = (f"its params give {now} but its own text reads {alt or 'differently'}: "
                                      f"a person must confirm which reading applies")
                    m["fresh"] = alt or m.get("fresh")
        M.done(rederived=len(ids))
        self.emit({"type": "agent_done", "agent": "EvidenceHunter", "summary": f"re-derived {len(ids)} clause(s)"})
        rv = M.start("Verifier", uses_llm=False, requested_by="EvidenceHunter", reason="re-verify")
        self.emit({"type": "agent_start", "agent": "Verifier", "run": rv, "pass_no": M.passes["Verifier"]})
        left, _ = self.verify(ids, immaterial)
        for m in left:
            if m["clause_id"] in notes:
                m["fresh"] = by_id.get(m["clause_id"], {}).get("fresh", m["fresh"])
                m["note"] = notes[m["clause_id"]]
        bad = {m["clause_id"] for m in left}
        for r in reqs:
            note = notes.get(r["clause_id"])
            if r["clause_id"] in bad:
                self.close(r["finding"], rv, "persisted", "Still differs after one re-derivation"
                           + (f": {note}." if note else ": needs a person."))
            else:
                self.close(r["finding"], rv, "resolved", "Resolved: re-derived by the EvidenceHunter and re-verified"
                           + (f"; {note}." if note else "."), severity="info")
        M.done(mismatches=len(left))
        self.plan["handoffs"].append({"from": "Verifier", "to": "EvidenceHunter", "run": run,
                                      "why": "verification mismatch", "outcomes": [
                                          {"clause": m["clause"], "outcome": "persisted" if m["clause_id"] in bad
                                           else "resolved"} for m in first]})
        return left


def qualify_tender(g: Graph, tender: dict, emit=None, prefer_llm: bool = True) -> dict:
    emit = emit or (lambda e: None)
    t_start = time.perf_counter()
    case_id = C.case_open(g, title=tender.get("title", "Tender"), tender_text=tender.get("text", ""),
                          bid_date=tender.get("bid_date"), tender_ref=tender.get("id"),
                          category=tender.get("category"), authority=tender.get("authority"))["result"]["case_id"]
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
              "text": c["text"], "params": c["params"], **({"corrected": c["corrected"]} if c.get("corrected") else {})})
    n_corr = sum(bool(c.get("corrected")) for c in parsed)
    M.done(method=how, n_clauses=len(parsed), corrected=n_corr)
    emit({"type": "agent_done", "agent": "ClauseParser", "summary": f"{len(parsed)} clauses ({how})"
          + (f", {n_corr} corrected by the rule parser" if n_corr else "")})

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
                # only the people this bid actually credits (SATISFIED_BY) need renewing, not every lapsing candidate
                credited = {r["name"] for r in g.read(
                    "MATCH (:Clause {id:$k})-[:SATISFIED_BY]->(p:Person) RETURN p.name AS name", {"k": kid})}
                exp = [e for e in res.get("expiring_within_90d") or [] if e["name"] in credited]
                if exp:
                    alts = ", ".join(a["name"] for a in res.get("safe_alternates", []))
                    txt = (f"PMP of {', '.join(e['name'] + ' (to ' + e['valid_through'] + ')' for e in exp)} lapses "
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
                cats = " / ".join(h["params"].get("categories") or []) or "similar-works"
                txt = ("No qualifying combination. Closest works: " +
                       "; ".join(f"{w['title']} ({_cr(w['value'])})" for w in nm) +
                       f". Consider a JV with a partner holding a larger {cats} credential.")
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
                            f"Verifier: {'; '.join(m['reasons'])} (case says {m['stored']}); asked the "
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
    memo, how, check = None, "template", None
    if prefer_llm and (providers.available() or providers.REPLAY):
        try:
            # every rupee amount is written as "₹X.XX Cr" here, so the model copies figures instead of
            # converting them (its own conversions were 10x off)
            facts = _money_facts({"tender": {k: tender.get(k) for k in ("id", "title", "authority", "bid_date")},
                                  "overall": overall, "clauses": rec["clauses"], "evidence": evidence[:40],
                                  "verifier": {"ok": not mismatches, "mismatches": mismatches, "retried": first},
                                  "team_handoffs": T.plan["handoffs"], "precedents": prec.get("result", [])[:2]})
            full = json.dumps(facts, default=str, ensure_ascii=False)
            res = run_agent(g, "Assembler", ASSEMBLER_SYSTEM, full[:16000], ["who_knows", "key_people"], max_steps=4,
                            emit=emit)
            M.llm(res.usage["llm_calls"], res.usage["prompt"], res.usage["completion"])
            M.tool(res.usage["tool_calls"])
            if res.final and not res.error:
                # what the model saw: the record it was given and what its own tools returned
                seen = full[:16000] + json.dumps(res.evidence, default=str)
                problems = memo_problems(res.final, seen)
                check = {"ok": not problems, "problems": problems}
                if problems:
                    emit({"type": "memo_check", "agent": "Assembler", "ok": False, "problems": problems,
                          "summary": "LLM memo rejected; the template memo is used"})
                else:
                    memo, how = res.final, "llm"
        except Exception:
            memo = None
    if not memo:
        memo = _template_memo(tender, rec, overall, prec.get("result", []), evidence, mismatches,
                              T.plan["handoffs"])
    g.run("MATCH (c:Case {id:$c}) SET c.memo = $m, c.verdict = $v, c.status = 'open', c.memo_method = $h, "
          "c.memo_check = $k", {"c": case_id, "m": memo, "v": overall, "h": how,
                                "k": json.dumps(check) if check else None})
    M.done(method=how, **({"llm_memo_rejected": len(check["problems"])} if check and not check["ok"] else {}))
    emit({"type": "memo", "agent": "Assembler", "markdown": memo, "overall": overall, "method": how,
          **({"memo_check": check} if check else {})})
    # the Assembler's follow-ups become accountable actions (reference-letter requests, credential renewals)
    from bidbrain.company import actions as A
    acts = A.from_findings(g, case_id)
    if acts:
        emit({"type": "actions", "agent": "Assembler", "case_id": case_id, "actions": acts,
              "summary": f"{len(acts)} follow-up action(s) queued for approval"})
    final = C.case_get(g, case_id)["result"]
    final["actions"] = A.list_actions(g, case_id=case_id)
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


# ── money, and the memo's own check ─────────────────────────────────────────
_MONEY_PARAMS = ("estimated_cost", "min_value", "min_average", "min_each_year", "bid_value")
_BIG = re.compile(r"(?<![\w.,])\d{1,3}(?:,\d{3}){2,}(?![\d,])")       # 2,500,000,000: rupees, not a count
_DOC = re.compile(r"\bDOC-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


def _cr(v) -> str:
    return f"₹{float(v) / 1e7:,.2f} Cr"


def _fmt_amount(label: str, v) -> str:
    return _cr(v) if label in MONEY_LABELS else f"{v}"


def _fmt_text(t: str) -> str:
    """Raw rupee figures in a basis line ('>= 2,000,000,000') written as ₹ crore."""
    return _BIG.sub(lambda m: _cr(int(m.group(0).replace(",", ""))), t or "")


def _money_facts(x, key: str = ""):
    """The Assembler's record with every rupee amount formatted as '₹X.XX Cr' in Python."""
    if isinstance(x, dict):
        label = x.get("label") or x.get("kind")
        out = {}
        for k, v in x.items():
            if k == "params" and isinstance(v, str):
                try:
                    v = json.loads(v)
                except ValueError:
                    pass
            if k in _MONEY_PARAMS and isinstance(v, (int, float)) and v:
                out[k] = _cr(v)
            elif k == "credited" and isinstance(v, (int, float)) and label in MONEY_LABELS:
                out[k] = _cr(v)
            else:
                out[k] = _money_facts(v, k)
        return out
    if isinstance(x, list):
        return [_money_facts(v, key) for v in x]
    if isinstance(x, str):
        return _fmt_text(x)
    return x


def memo_problems(memo: str, facts: str) -> list[str]:
    """An LLM memo is kept only if every DOC id it cites and every ₹ amount it states is in what it was
    given (the case record, its evidence table and its own tool results); amounts within ₹0.01 Cr."""
    allowed_docs = set(_DOC.findall(facts))
    bad_docs = sorted(set(_DOC.findall(memo)) - allowed_docs)
    known = {round(v / 1e5) for v in clause_mod._amounts(facts)}
    bad_amounts = sorted({_cr(v) for v in clause_mod._amounts(memo)
                          if not ({round(v / 1e5) - 1, round(v / 1e5), round(v / 1e5) + 1} & known)})
    return ([f"cites {d}, which is not in the case evidence" for d in bad_docs]
            + [f"states {a}, which is not among the figures it was given" for a in bad_amounts])


def rebuild_memo(g: Graph, case_id: str, cause: str, before: str | None, after: str) -> str:
    """After a ripple changes verdicts, the memo is re-assembled from the case graph (template), so the bid
    pack a person downloads matches the case. The note says what changed and that it supersedes the memo
    written at qualification."""
    row = g.read("""MATCH (c:Case {id:$c}) RETURN c.title AS title, c.bid_date AS bid_date, c.tender_ref AS id,
                           c.category AS category, c.authority AS authority, c.plan AS plan""", {"c": case_id})[0]
    rec = C.case_get(g, case_id)["result"]
    prec = C.precedents(g, category=row["category"])["result"] if row["category"] else []
    plan = json.loads(row["plan"] or "{}")
    memo = _template_memo(row, rec, after, prec, _evidence(g, case_id), None, plan.get("handoffs"))
    head = (f"> **Updated after {cause}.** Recommendation {before or '—'} → **{after}**. This memo was "
            f"re-assembled from the case graph and supersedes the one written at qualification.")
    memo = head + "\n\n" + memo
    g.run("MATCH (c:Case {id:$c}) SET c.memo = $m, c.memo_method = 'template', c.memo_superseded_at = timestamp(), "
          "c.memo_superseded_by = $cause", {"c": case_id, "m": memo, "cause": cause})
    return memo


def _template_memo(tender: dict, rec: dict, overall: str, precedents: list, evidence: list | None = None,
                   mismatches: list | None = None, handoffs: list | None = None) -> str:
    lines = [f"## {tender.get('title')} — recommendation: **{overall}**", "",
             f"Authority: {tender.get('authority', '—')} · bid due {tender.get('bid_date', '—')}", "",
             "| Clause | Verdict | Basis |", "|---|---|---|"]
    order = lambda c: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", str(c.get("ref") or ""))]
    for c in sorted(rec["clauses"], key=order):
        basis = _fmt_text(c.get("basis") or "")
        if c.get("corrected"):
            basis += f" (parser correction: {_fmt_text(c['corrected'])})"
        lines.append(f"| {c['ref']} {c['kind']} | {c['verdict']} | {basis.replace('|', '/')} |")
    if mismatches:
        lines += ["", "> **Verifier:** independent checks still disagree for "
                  + "; ".join(f"{m['clause']} (case {m['stored']}: {m.get('note') or '; '.join(m.get('reasons') or [])})"
                              for m in mismatches)
                  + ". Recommendation withheld until a person reviews it."]
    if evidence:
        lines += ["", "### Evidence", "", "| Clause | Relies on | Credited | Document | Page |", "|---|---|---|---|---|"]
        for e in evidence:
            credited = (_fmt_amount(e["kind"], int(e["credited"])) if isinstance(e.get("credited"), (int, float))
                        else "—")
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
