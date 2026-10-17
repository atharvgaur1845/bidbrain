"""Deterministic workflow evaluation: does the company loop actually do what it claims?

    cd backend && .venv/bin/python -m eval.workflow            # run every scenario, write results
    .venv/bin/python -m eval.workflow --only temporal,isolation
    .venv/bin/python -m eval.workflow --report                 # print the table of the last run

The answer-accuracy benchmarks (S21 / Q333 / ABS / TND, conflict P/R) score *answers*. This scores the
*workflow*: graph state transitions, the reach of change impact, adversarial conflict routing, company
memory transfer, workspace isolation, bitemporal history and provenance. No LLM is called
(`providers.chat` raises). Every scenario drives the real code paths, mostly through the HTTP API
(FastAPI TestClient against bidbrain.api.main, as tests/test_loop_e2e.py does), and every expectation is
derived from the fixture definitions (data/conflicts/demo_events.json, data/tenders/demo_tenders.json) or
from independent Cypher / an independent re-run of qualify_clause, never from the function under test.

Dimensions
    task_completion   approve -> execute changes the graph (approved_recommendation, DocumentRequest,
                      OrgFact); the gate holds; execute is idempotent; ripples never double the queue
    impact_recall     for each demo event, the (case, clause) flips /api/impact/{doc} reports vs an
                      independent expectation: every open clause re-run with qualify_clause on a graph copy
                      before and after the event (not via blast_radius) -> precision / recall
    conflict          adversarial routing: rounding, irrelevant corrigenda, same-issuer recency (and a less
                      formal same-issuer letter that must NOT win by recency), close-authority human cards,
                      expiry at the bid date, absence PROVEN vs UNPROVEN when corpus closure breaks, aliases
    memory_transfer   promoted rule applies in another session only after opt-in; contest/retire reopen;
                      a published company fact applies in an opted-in workspace (policy company_record) and
                      never in a fresh one; publishing is never self-approved
    isolation         session B sees none of A's cases, actions, turns, cards, uploads; cross-session ids
                      404; MCP qualify_tender refuses non-session graphs; base and real org untouched
    temporal          fact_as_of before / after the corrigendum; superseded claims keep value + invalidated_at
    provenance        evidence rows point at existing documents and valid pages; every Decision has
                      SELECTS + BASED_ON claims FROM documents

Safety: only throwaway graphs (session_* copies made through the API, `wfeval_*` copies, a private
`wfeval_org_*` company graph swapped in for engine.ORG_GRAPH). Copies are made one at a time (FalkorDB
forks per GRAPH.COPY) and every graph created is deleted, also on failure. The run asserts that
`bidbrain_base` and the real `bidbrain_org` are byte-for-byte unchanged in node/edge counts.

Outputs: data/eval/workflow_summary.json, data/eval/results/workflow.jsonl (one row per case:
id, dimension, description, passed, expected, observed, ms).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import secrets
import shutil
import tempfile
import time
import traceback

from bidbrain import config
from bidbrain.graph.client import Graph, day, db

RESULTS = config.DATA / "eval" / "results" / "workflow.jsonl"
SUMMARY = config.DATA / "eval" / "workflow_summary.json"
DIMENSIONS = ["task_completion", "impact_recall", "conflict", "memory_transfer", "isolation", "temporal",
              "provenance"]

DEMO_EVENTS = json.loads((config.DATA / "conflicts" / "demo_events.json").read_text())
EV = {e["event_id"]: e for e in DEMO_EVENTS}
TENDERS = json.loads((config.DATA / "tenders" / "demo_tenders.json").read_text())
TENDER = {t["id"]: t for t in TENDERS}
PKG113 = "ringroaduttarpradeshpkg113"
RANK = {"PASS": 2, "AT_RISK": 1}        # orchestrator.select's ranking, restated (not imported)


# ---------------------------------------------------------------------------------------------- harness

class Harness:
    """Throwaway company graph + blocked LLM + a TestClient; tracks and deletes every graph it creates."""

    def __init__(self):
        self.rows: list[dict] = []
        self.made: set[str] = set()
        self.upload_sids: list[str] = []
        self._t = time.perf_counter()

    # -- lifecycle
    def __enter__(self):
        from fastapi.testclient import TestClient

        from bidbrain.agents import providers
        from bidbrain.api import main, sessions
        from bidbrain.conflicts import engine
        assert Graph().exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
        self.main, self.sessions, self.engine, self.providers = main, sessions, engine, providers
        self._saved = {(engine, "ORG_GRAPH"): engine.ORG_GRAPH, (engine, "ORG_EXPORT_PATH"): engine.ORG_EXPORT_PATH,
                       (providers, "chat"): providers.chat, (sessions, "MAX_SESSIONS"): sessions.MAX_SESSIONS,
                       (sessions, "SESSION_RATE"): sessions.SESSION_RATE}
        self.real_org = engine.ORG_GRAPH
        self.tmp = tempfile.mkdtemp(prefix="wfeval_")
        engine.ORG_EXPORT_PATH = f"{self.tmp}/org_memory.json"

        def no_llm(*a, **k):
            raise AssertionError("the workflow evaluation never calls an LLM")
        providers.chat = no_llm
        sessions.MAX_SESSIONS = 10_000          # never evict anyone else's live session
        sessions.SESSION_RATE = 10_000
        self.base_counts = _counts(config.BASE_GRAPH)
        self.org_counts = _counts(self.real_org)
        self.fresh_org()
        self.client = TestClient(main.app)
        return self

    def __exit__(self, *exc):
        self.cleanup()
        Graph(self.engine.ORG_GRAPH).delete()
        for (mod, attr), v in self._saved.items():
            setattr(mod, attr, v)
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    def fresh_org(self) -> str:
        """A new, empty private company graph (scenarios must not leak company memory into each other)."""
        if self.engine.ORG_GRAPH != self.real_org:
            Graph(self.engine.ORG_GRAPH).delete()
        self.engine.ORG_GRAPH = f"wfeval_org_{secrets.token_hex(4)}"
        assert self.engine.ORG_GRAPH != self.real_org
        return self.engine.ORG_GRAPH

    def cleanup(self) -> None:
        for name in sorted(self.made):
            try:
                Graph(name).delete()
            except Exception:
                pass
            self.sessions._last_seen.pop(name, None)
        self.made.clear()
        for sid in self.upload_sids:
            shutil.rmtree(self.sessions.upload_dir(sid), ignore_errors=True)
        self.upload_sids.clear()

    # -- graphs
    def session(self) -> dict:
        r = self.client.post("/api/session")
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        self.made.add(f"session_{sid}")
        return {"X-Session": sid}

    def g(self, h: dict) -> Graph:
        return Graph(f"session_{h['X-Session']}")

    def copy(self, src: str, prefix: str = "wfeval") -> Graph:
        """GRAPH.COPY with the same fork-slot wait/retry the product uses; sequential by construction."""
        name = f"{prefix}_{secrets.token_hex(4)}"
        self.made.add(name)
        for i in range(60):
            self.sessions._wait_for_no_fork()
            try:
                return Graph(src).copy_to(name)
            except Exception as exc:
                if "fork" not in str(exc).lower() or i == 59:
                    raise
                time.sleep(0.2 * min(i + 1, 10))
        raise RuntimeError("unreachable")

    # -- API helpers
    def stream(self, path: str, h: dict, body: dict | None = None) -> dict:
        r = self.client.post(path, json=body or {}, headers=h)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:300]}"
        evs = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        errs = [e for e in evs if e.get("type") == "error"]
        assert not errs, f"{path}: {errs}"
        return next(e["data"] for e in evs if e.get("type") == "result")

    def qualify(self, h: dict, tender_id: str) -> dict:
        return self.stream("/api/qualify", h, {"tender_id": tender_id, "use_llm": False})["case"]

    def fire(self, h: dict, event_id: str) -> dict:
        return self.stream(f"/api/events/{event_id}/fire", h)

    def act(self, h: dict, aid: str, verb: str, **body):
        return self.client.post(f"/api/actions/{aid}/{verb}", json=body, headers=h)

    def approve_publish(self, h: dict, aid: str):
        """Four eyes: the publishing workspace's approve files a request (403); a second workspace approves it;
        then the first approve succeeds. Returns (first status code, final response)."""
        first = self.act(h, aid, "approve").status_code
        other = self.session()
        for r in self.client.get("/api/company/record", headers=other).json()["requests"]:
            if not r["mine"] and r["status"] == "pending":
                self.client.post(f"/api/company/record/requests/{r['id']}/approve", json={}, headers=other)
        return first, self.act(h, aid, "approve")

    def opt_in(self, h: dict) -> dict:
        return self.client.post("/api/session/settings", json={"use_company_memory": True}, headers=h).json()

    # -- recording
    def mark(self) -> None:
        self._t = time.perf_counter()

    def check(self, cid: str, dim: str, desc: str, expected, observed, passed: bool | None = None) -> bool:
        ok = (expected == observed) if passed is None else bool(passed)
        ms = round((time.perf_counter() - self._t) * 1000, 1)
        self.rows.append({"id": cid, "dimension": dim, "description": desc, "passed": ok,
                          "expected": _jsonable(expected), "observed": _jsonable(observed), "ms": ms})
        self._t = time.perf_counter()
        return ok


def _jsonable(x):
    return json.loads(json.dumps(x, default=str))


def _counts(name: str) -> dict | None:
    if name not in db().list_graphs():
        return None
    g = Graph(name)
    return {"nodes": g.scalar("MATCH (n) RETURN count(n)"), "edges": g.scalar("MATCH ()-[r]->() RETURN count(r)")}


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _iso_ms(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------- independent clause re-checking

def recheck(g: Graph, kind: str, params: dict, bid_date: str, prefer_reference: bool) -> str:
    """A clause's verdict from qualify_clause alone (the deterministic engine), with the documented
    reference-preference policy restated here rather than imported."""
    from bidbrain.tools.proof import qualify_clause
    v = qualify_clause(g, kind, params, bid_date)["result"]["verdict"]
    if prefer_reference and kind == "similar_works" and not params.get("require_reference"):
        r = qualify_clause(g, kind, {**params, "require_reference": True}, bid_date)["result"]["verdict"]
        if RANK.get(r, 0) >= max(1, RANK.get(v, 0)):
            return r
    return v


OPEN_CLAUSES = """MATCH (c:Case {status:'open'})-[:HAS_CLAUSE]->(k:Clause)
                  WHERE k.params IS NOT NULL AND k.kind IS NOT NULL AND k.kind <> 'other'
                  RETURN c.id AS case_id, c.bid_date AS bid_date, k.ref AS ref, k.kind AS kind, k.params AS params,
                         coalesce(k.prefer_reference, false) AS pref, k.verdict AS stored"""


def clause_verdicts(g: Graph, clauses: list[dict]) -> dict[tuple[str, str], str]:
    out = {}
    for c in clauses:
        try:
            out[(c["case_id"], c["ref"])] = recheck(g, c["kind"], json.loads(c["params"]), c["bid_date"], c["pref"])
        except Exception as exc:
            out[(c["case_id"], c["ref"])] = f"error:{type(exc).__name__}"
    return out


def ripple_event(H: Harness, g: Graph, ev: dict) -> dict:
    """The API's fire path for an event that is not in the demo file: apply_event + the same _ripple_after."""
    from bidbrain.conflicts.events import apply_event
    out = apply_event(g, ev)
    out.setdefault("doc", ev["doc"])
    return {**out, **H.main._ripple_after(g, lambda e: None, out, ev["doc"]["title"])}


def flipped_pairs(imp: dict) -> set[tuple[str, str]]:
    return {(a["case_id"], a["clause"]) for a in imp["affected"] if (a.get("what") or "").startswith("verdict changed")}


def corrigendum(work: str, value: int, observed: str = "2026-06-01") -> dict:
    """A client corrigendum revising a work's value, issued by the issuer of the work's completion certificate."""
    b = Graph()
    iss = b.scalar("MATCH (c:Claim {subject_key:$k, predicate:'value', source_kind:'client_certificate'}) "
                   "RETURN c.issuer", {"k": work})
    title = b.scalar("MATCH (w:Work {key:$k}) RETURN w.title", {"k": work})
    return synth_event(f"CORR-{work[-6:]}", "corrigendum", iss, f"Corrigendum — {title}", "Work", work, "value",
                       value, "client_corrigendum", observed)


def fresh(ev: dict) -> dict:
    """A copy of an event under a new document id (as the API does per firing)."""
    ev = json.loads(json.dumps(ev))
    ev["doc"]["doc_id"] = f"{ev['doc']['doc_id']}-{secrets.token_hex(2)}"
    return ev


def synth_event(eid: str, doc_type: str, issuer: str, title: str, label: str, key: str, pred: str, value,
                kind: str, observed: str = "2026-06-01") -> dict:
    return {"event_id": eid, "type": "demo", "narrative": title,
            "doc": {"doc_id": f"DOC-WFEVAL-{eid}-{secrets.token_hex(2)}", "doc_type": doc_type, "issuer": issuer,
                    "issued": observed, "title": title},
            "claims": [{"subject_label": label, "subject_key": key, "predicate": pred, "value": value,
                        "source_kind": kind, "issuer": issuer, "observed": observed, "page": 1}]}


# ============================================================================================ scenarios

def s_task_completion(H: Harness) -> None:
    D = "task_completion"
    h = H.session()
    g = H.g(h)
    case = H.qualify(h, "T-NHX-2026-014")
    cid = case["id"]
    H.fire(h, "DEMO-0")
    out = H.fire(h, "DEMO-1")
    H.mark()
    doc = out["doc"]["doc_id"]
    # gold for the recommendation: re-run every clause of the case independently; any FAIL -> NO-BID
    cl = [c for c in g.read(OPEN_CLAUSES) if c["case_id"] == cid]
    verds = clause_verdicts(g, cl)
    gold_rec = "NO-BID" if "FAIL" in verds.values() else None
    review = g.read("""MATCH (a:Action {kind:'review_bid'})-[:FOR_CASE]->(:Case {id:$c})
                       OPTIONAL MATCH (a)-[:CITES]->(d:Document) RETURN a.id AS id, a.status AS status,
                       collect(d.doc_id) AS cites""", {"c": cid})
    H.check("tc.review_action_created", D, "the corrigendum that flips the open NHX bid queues exactly one "
            "review_bid action FOR_CASE that case, citing the corrigendum document",
            {"n": 1, "status": "pending", "cites": [doc]},
            {"n": len(review), "status": review[0]["status"] if review else None,
             "cites": review[0]["cites"] if review else None})
    H.check("tc.case_recommendation_flipped", D, "Case.verdict equals the recommendation implied by an "
            "independent re-run of every clause (a FAIL clause => NO-BID)",
            gold_rec, g.scalar("MATCH (c:Case {id:$c}) RETURN c.verdict", {"c": cid}))
    if not review:
        return
    aid = review[0]["id"]
    r = H.act(h, aid, "execute")
    H.check("tc.gate_blocks_unapproved", D, "executing a pending action is refused (409) and changes nothing",
            {"status": 409, "approved_recommendation": None},
            {"status": r.status_code,
             "approved_recommendation": g.scalar("MATCH (c:Case {id:$c}) RETURN c.approved_recommendation", {"c": cid})})
    H.act(h, aid, "approve", user="bid manager")
    done = H.act(h, aid, "execute").json()
    H.check("tc.approve_execute_sets_recommendation", D, "approve -> execute sets Case.approved_recommendation "
            "to the independently derived recommendation",
            {"status": "executed", "approved_recommendation": gold_rec},
            {"status": done.get("status"),
             "approved_recommendation": g.scalar("MATCH (c:Case {id:$c}) RETURN c.approved_recommendation", {"c": cid})})
    n_ev = g.scalar("MATCH (:Action {id:$a})-[:HAS_EVENT]->(e:ActionEvent) RETURN count(e)", {"a": aid})
    again = H.act(h, aid, "execute").json()
    H.check("tc.execute_idempotent", D, "a repeated execute returns the stored result and writes no ActionEvent",
            {"events": n_ev, "result": done.get("result")},
            {"events": g.scalar("MATCH (:Action {id:$a})-[:HAS_EVENT]->(e:ActionEvent) RETURN count(e)", {"a": aid}),
             "result": again.get("result")})
    trail = [r["to"] for r in g.read("""MATCH (:Action {id:$a})-[:HAS_EVENT]->(e:ActionEvent)
                                        RETURN e.to AS to ORDER BY e.at, id(e)""", {"a": aid})]
    H.check("tc.audit_trail", D, "the action's audit trail is exactly pending -> approved -> executed",
            ["pending", "approved", "executed"], trail)
    # duplicate ripple: re-run the API's ripple for the same document output; the queue must not grow
    n_act = g.scalar("MATCH (a:Action) RETURN count(a)")
    H.main._ripple_after(g, lambda e: None, out, "duplicate ripple")
    from bidbrain.company import actions
    dup = actions.from_ripple(g, [{"case_id": cid, "from": "BID", "to": "NO-BID"}],
                              next((d["decision_id"] for d in out["decisions"]), None), "dup", doc)
    H.check("tc.duplicate_ripple_no_duplicates", D, "re-running the ripple (and re-queuing the same flip) "
            "creates no new Action node", {"actions": n_act, "created": [False]},
            {"actions": g.scalar("MATCH (a:Action) RETURN count(a)"), "created": [a["created"] for a in dup]})
    n_rev = g.scalar("MATCH (a:Action {kind:'review_bid'}) RETURN count(a)")
    H.fire(h, "DEMO-1")
    H.mark()
    H.check("tc.refire_same_corrigendum_no_new_review", D, "firing the same corrigendum again (a new copy of "
            "the document, same value) queues no new review_bid: nothing flipped",
            n_rev, g.scalar("MATCH (a:Action {kind:'review_bid'}) RETURN count(a)"))
    # publish the decided fact: OrgFact in the (private) company graph with this decision as its source
    pub = next(a for a in H.client.get(f"/api/impact/{doc}", headers=h).json()["result"]["actions"]
               if a["kind"] == "publish_to_company_record")
    dec = g.scalar("MATCH (:Action {id:$a})-[:TRIGGERED_BY]->(d:Decision) RETURN d.id", {"a": pub["id"]})
    first, _ = H.approve_publish(h, pub["id"])
    H.check("tc.publish_not_self_approved", D, "the workspace that queued a publish cannot approve it alone (403); "
            "a second workspace approves its request", 403, first)
    res = H.act(h, pub["id"], "execute").json()
    of = (res.get("result") or {}).get("org_fact")
    org = Graph(H.engine.ORG_GRAPH)
    row = org.read("MATCH (f:OrgFact {id:$f}) RETURN f.source_decision AS d, f.value AS v, f.status AS s",
                   {"f": of}) if of else []
    H.check("tc.publish_creates_orgfact", D, "an executed publish action writes one active OrgFact whose "
            "source_decision is the triggering Decision and whose value is DEMO-1's",
            {"d": dec, "v": str(EV["DEMO-1"]["claims"][0]["value"]), "s": "active"}, row[0] if row else None)

    # renewal: PHED clause (ii) has people whose PMP lapses within 90 days of the bid date
    pcase = H.qualify(h, "T-PHED-WTP-2027-03")
    H.mark()
    bd = day(TENDER["T-PHED-WTP-2027-03"]["bid_date"])
    # gold: persons proposed on the personnel clause whose PMP is valid on the bid date but lapses within 90 days
    lapsing = {r["k"] for r in g.read("""
        MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause {kind:'key_personnel'})-[:SATISFIED_BY]->(p:Person)
        MATCH (p)-[:HOLDS]->(cr:Credential) WHERE toLower(cr.kind) CONTAINS 'pmp'
          AND cr.valid_through_day >= $bd AND cr.valid_through_day < $bd + 90
        RETURN DISTINCT p.key AS k""", {"c": pcase["id"], "bd": bd})}
    ren = g.read("""MATCH (a:Action {kind:'renew_credential', case_id:$c})-[:ASSIGNED_TO]->(p:Person)
                    RETURN a.id AS id, p.key AS person""", {"c": pcase["id"]})
    H.check("tc.renewal_actions_match_lapsing", D, "one renew_credential action per proposed person whose PMP "
            "lapses within 90 days of the bid date (independent Cypher over HOLDS/valid_through_day)",
            sorted(lapsing), sorted(r["person"] for r in ren), passed=bool(lapsing) and set(lapsing) == {r["person"] for r in ren})
    if ren:
        a = ren[0]
        H.act(h, a["id"], "approve")
        out_r = H.act(h, a["id"], "execute").json()
        req = g.read("""MATCH (:Action {id:$a})-[:CREATED]->(r:DocumentRequest)-[:FOR_PERSON]->(p:Person)
                        RETURN r.kind AS kind, r.status AS status, p.key AS person, r.id AS id""", {"a": a["id"]})
        H.check("tc.renewal_creates_request", D, "executing a renewal creates a DocumentRequest "
                "(credential_renewal, requested) FOR_PERSON the assignee",
                [{"kind": "credential_renewal", "status": "requested", "person": a["person"],
                  "id": (out_r.get("result") or {}).get("request_id")}], req)
    # reference letter: RBC clause 1/2 credit works with no reference letter
    rcase = H.qualify(h, "T-RBC-2026-31")
    H.mark()
    gold_works = {r["w"] for r in g.read("""
        MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(:Clause {kind:'similar_works'})-[:SATISFIED_BY]->(w:Work)
        WHERE NOT (w)-[:HAS_REFERENCE]->(:Document) RETURN DISTINCT w.key AS w""", {"c": rcase["id"]})}
    let = g.read("""MATCH (a:Action {kind:'request_reference_letter', case_id:$c}) RETURN a.id AS id, a.target AS w""",
                 {"c": rcase["id"]})
    H.check("tc.letter_actions_match_unreferenced", D, "one request_reference_letter per credited work with no "
            "HAS_REFERENCE edge (independent Cypher)", sorted(gold_works), sorted(r["w"] for r in let),
            passed=bool(gold_works) and gold_works == {r["w"] for r in let})
    if let:
        a = let[0]
        H.act(h, a["id"], "approve")
        H.act(h, a["id"], "execute")
        req = g.read("""MATCH (:Action {id:$a})-[:CREATED]->(r:DocumentRequest)-[:FOR_WORK]->(w:Work)
                        OPTIONAL MATCH (r)-[:ADDRESSED_TO]->(c:Client)
                        RETURN r.kind AS kind, w.key AS work, c.key AS client""", {"a": a["id"]})
        client = g.scalar("MATCH (:Work {key:$w})-[:FOR_CLIENT]->(c:Client) RETURN c.key", {"w": a["w"]})
        H.check("tc.letter_creates_request", D, "executing a letter request creates a DocumentRequest "
                "(reference_letter) FOR_WORK the work and ADDRESSED_TO its client",
                [{"kind": "reference_letter", "work": a["w"], "client": client}], req)
    # rejected actions never run: the remaining renewal (or a fresh one) is rejected, then approve/execute fail
    pend = g.read("MATCH (a:Action {status:'pending'}) WHERE a.kind <> 'publish_to_company_record' "
                  "RETURN a.id AS id, a.kind AS kind LIMIT 1")
    if pend:
        a = pend[0]["id"]
        n_req = g.scalar("MATCH (r:DocumentRequest) RETURN count(r)")
        H.act(h, a, "reject", note="not needed")
        codes = (H.act(h, a, "approve").status_code, H.act(h, a, "execute").status_code)
        H.check("tc.rejected_cannot_run", D, "a rejected action cannot be approved or executed (409, 409); it stays "
                "rejected and creates no DocumentRequest",
                {"codes": [409, 409], "status": "rejected", "requests": n_req},
                {"codes": list(codes), "status": g.scalar("MATCH (a:Action {id:$a}) RETURN a.status", {"a": a}),
                 "requests": g.scalar("MATCH (r:DocumentRequest) RETURN count(r)")})
    H.check("tc.unknown_action_and_verb", D, "an unknown action id is 404, an unknown verb 400",
            [404, 400], [H.act(h, "ACT-0000000000", "approve").status_code, H.act(h, aid, "launch").status_code])
    H.cleanup()


def s_impact_recall(H: Harness, summary: dict) -> None:
    """Per event: qualify all five sample tenders, then compare reported flips with an independent re-run."""
    D = "impact_recall"
    from bidbrain.conflicts.events import apply_event
    # (tag, events fired first, the event measured). Synthetic corrigenda are issued by the work's own client
    # (the issuer of its completion certificate), so the engine settles them by same-issuer recency.
    plan = [("DEMO-0", [], "DEMO-0"), ("DEMO-1", [], "DEMO-1"), ("DEMO-1+after-DEMO-0", ["DEMO-0"], "DEMO-1"),
            ("DEMO-2", [], "DEMO-2"), ("DEMO-3", [], "DEMO-3"), ("DEMO-4", [], "DEMO-4"),
            # T-RBC-2026-31 clause 1 (one large bridge >= INR 70 Cr): its credited work is cut to 65 Cr while an
            # alternate still qualifies (expect no flip, evidence rewired) ...
            ("RBC-cut-Pkg124", [], corrigendum("extradosedbridgerajasthanpkg124", 650_000_000)),
            # ... then, with the alternate cut first, the same cut leaves nothing (expect clause 1 to FAIL)
            ("RBC-cut-Pkg124+after-cut-Pkg127", [corrigendum("extradosedbridgemadhyapradeshpkg127", 600_000_000)],
             corrigendum("extradosedbridgerajasthanpkg124", 650_000_000)),
            # a corrigendum can also make a failing clause pass: JN-LB clause 1 needs one bridge >= 80% of 450 Cr
            ("JN-raise-Pkg127", [], corrigendum("extradosedbridgemadhyapradeshpkg127", 3_700_000_000))]
    tp = fp = fn = 0
    ctp = cfp = cfn = 0
    per, n_checked = [], 0
    for tag, pre, target in plan:
        H.fresh_org()
        h = H.session()
        g = H.g(h)
        for t in TENDERS:
            H.qualify(h, t["id"])
        for p in pre:
            H.fire(h, p) if isinstance(p, str) else ripple_event(H, g, fresh(p))
        ev = json.loads(json.dumps(EV[target])) if isinstance(target, str) else fresh(target)
        H.mark()
        clauses = g.read(OPEN_CLAUSES)
        before = clause_verdicts(g, clauses)
        pred = H.copy(g.name)
        try:
            apply_event(pred, json.loads(json.dumps(ev)))
            after = clause_verdicts(pred, clauses)
        finally:
            pred.delete()
            H.made.discard(pred.name)
        expected = {k for k in before if before[k] != after[k]}
        n_checked += len(before)
        out = H.fire(h, target) if isinstance(target, str) else ripple_event(H, g, ev)
        doc = out["doc"]["doc_id"]
        imp = H.client.get(f"/api/impact/{doc}", headers=h).json()["result"]
        observed = flipped_pairs(imp)
        stored = {(c["case_id"], c["ref"]): c["stored"] for c in g.read(OPEN_CLAUSES)}
        tp += len(expected & observed); fp += len(observed - expected); fn += len(expected - observed)
        ec, oc = {c for c, _ in expected}, {c for c, _ in observed}
        ctp += len(ec & oc); cfp += len(oc - ec); cfn += len(ec - oc)
        nm = {c["id"]: c["tender_ref"] for c in g.read("MATCH (c:Case) RETURN c.id AS id, c.tender_ref AS tender_ref")}
        fmt = lambda s: sorted(f"{nm.get(c, c)}#{r}" for c, r in s)
        per.append({"event": tag, "expected": fmt(expected), "observed": fmt(observed)})
        H.check(f"ir.{tag}.flips", D, f"{tag}: the (case, clause) flips /api/impact reports equal the independent "
                "before/after re-run of every open clause on a graph copy", fmt(expected), fmt(observed))
        mism = {k: (stored.get(k), after[k]) for k in after if stored.get(k) != after[k]}
        H.check(f"ir.{tag}.state", D, f"{tag}: after the ripple, every open clause's stored verdict equals its "
                "independent re-run on the post-event graph", {}, {f"{nm.get(c, c)}#{r}": v for (c, r), v in mism.items()})
        H.cleanup()
    P = tp / (tp + fp) if tp + fp else 1.0
    R = tp / (tp + fn) if tp + fn else 1.0
    summary["impact_recall"] = {"events": len(plan), "clauses_rechecked": n_checked, "pairs": {"tp": tp, "fp": fp, "fn": fn, "precision": round(P, 4), "recall": round(R, 4)},
                                "cases": {"tp": ctp, "fp": cfp, "fn": cfn,
                                          "precision": round(ctp / (ctp + cfp), 4) if ctp + cfp else 1.0,
                                          "recall": round(ctp / (ctp + cfn), 4) if ctp + cfn else 1.0},
                                "per_event": per}


def s_conflict(H: Harness) -> None:
    D = "conflict"
    from bidbrain.conflicts import engine
    from bidbrain.conflicts.events import apply_event
    from bidbrain.tools.proof import prove_absence
    from bidbrain.tools.read import resolve_entity
    base = Graph()
    cert = base.read("""MATCH (c:Claim {subject_key:$k, predicate:'value', source_kind:'client_certificate'})
                        RETURN c.value AS v, c.issuer AS iss""", {"k": PKG113})[0]

    # 1 rounding: a portfolio-style figure within the coarser source's precision is no conflict
    g = H.copy(config.BASE_GRAPH)
    tol = 100_000 // 2
    ev = synth_event("ROUND", "past_performance_portfolio", "company", "Portfolio extract", "Work", PKG113,
                     "value", int(cert["v"]) + tol - 10_000, "portfolio")
    H.mark()
    out = apply_event(g, ev)
    H.check("cf.rounding_not_a_conflict", D, "a portfolio value 0.4 lakh off the certificate (portfolio precision "
            "INR 1 lakh) opens no card and makes no decision, is reported as rounding, and the value is unchanged",
            {"decisions": 0, "cards": 0, "rounding": True, "value": cert["v"]},
            {"decisions": len(out["decisions"]), "cards": len(out["cards"]), "rounding": bool(out.get("rounding")),
             "value": g.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113})})
    ev = synth_event("ROUND2", "past_performance_portfolio", "company", "Portfolio extract", "Work", PKG113,
                     "value", int(cert["v"]) + tol + 10_000, "portfolio")
    out = apply_event(g, ev)
    d = out["decisions"][0] if out["decisions"] else {}
    H.check("cf.rounding_boundary_is_conflict", D, "just beyond half the precision (0.6 lakh) it is a conflict, "
            "settled by authority for the certificate (90 vs 60)",
            {"policy": "authority", "value": cert["v"]},
            {"policy": d.get("policy"), "value": g.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113})})
    # 2 same-issuer recency (DEMO-1 as fixture) and a less formal same-issuer letter that must not win by recency
    out = apply_event(g, json.loads(json.dumps(EV["DEMO-1"])))
    d = next((x for x in out["decisions"] if x["conflict"]["predicate"] == "value"), {})
    old = g.read("""MATCH (c:Claim {subject_key:$k, predicate:'value', source_kind:'client_certificate'})
                    RETURN c.status AS s""", {"k": PKG113})[0]["s"]
    H.check("cf.recency_same_issuer", D, "the issuer's own later corrigendum (95 >= 90) supersedes its certificate "
            "by policy recency_same_issuer (fixture: same issuer on both documents)",
            {"same_issuer": True, "policy": "recency_same_issuer", "winner_new": True, "old_status": "superseded"},
            {"same_issuer": cert["iss"] == EV["DEMO-1"]["claims"][0]["issuer"], "policy": d.get("policy"),
             "winner_new": d.get("winner") in out["claims"], "old_status": old})
    ev = synth_event("LETTER", "reference_letter", cert["iss"], "Letter — Ring Road Pkg-113", "Work", PKG113,
                     "value", 1_700_000_000, "reference_letter", observed="2026-07-01")
    out = apply_event(g, ev)
    pol = [x["policy"] for x in out["decisions"]] + ["needs_human"] * len(out["cards"])
    H.check("cf.informal_later_letter_not_recency", D, "a later but less formal letter (80) from the same issuer "
            "does not win by recency; the corrigendum's value stands", {"recency": False, "value": EV["DEMO-1"]["claims"][0]["value"]},
            {"recency": "recency_same_issuer" in pol,
             "value": g.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113})})
    # 3 close authority, different issuers: a human card, never auto
    out = apply_event(g, json.loads(json.dumps(EV["DEMO-3"])))
    k3 = EV["DEMO-3"]["claims"][0]["subject_key"]
    H.check("cf.close_authority_goes_to_human", D, "partner certificate (75) vs portfolio (60), different issuers: "
            "a resolve card, no decision, the role stays as it was",
            {"decisions": 0, "cards": 1, "role": base.scalar("MATCH (w:Work {key:$k}) RETURN w.role", {"k": k3})},
            {"decisions": len(out["decisions"]), "cards": len(out["cards"]),
             "role": g.scalar("MATCH (w:Work {key:$k}) RETURN w.role", {"k": k3})})
    # an irrelevant document on a contested fact's *other* predicate does not touch the open card
    n_cards = g.scalar("MATCH (k:ConflictCard {status:'open'}) RETURN count(k)")
    iss3 = base.scalar("MATCH (c:Claim {subject_key:$k, predicate:'completed', source_kind:'client_certificate'}) "
                       "RETURN c.issuer", {"k": k3})
    ev = synth_event("OTHERPRED", "corrigendum", iss3, "Corrigendum (completion date)", "Work", k3, "completed",
                     base.scalar("MATCH (w:Work {key:$k}) RETURN w.completed", {"k": k3}), "client_corrigendum")
    out = apply_event(g, ev)
    H.check("cf.same_value_restatement_no_card", D, "a corrigendum restating the work's existing completion date "
            "(other predicate than the open role card) opens no card on 'completed', makes no decision on it, and "
            "the role card stays the one open card on that work",
            {"completed_cards": 0, "completed_decisions": 0, "open_cards": n_cards},
            {"completed_cards": sum(1 for c in out["cards"] if c["conflict"]["predicate"] == "completed"),
             "completed_decisions": sum(1 for c in out["decisions"] if c["conflict"]["predicate"] == "completed"),
             "open_cards": g.scalar("MATCH (k:ConflictCard {status:'open'}) RETURN count(k)")})
    g.delete(); H.made.discard(g.name)

    # 4 irrelevant corrigenda in a live workspace: no clause of any open bid flips
    h = H.session()
    g = H.g(h)
    for t in TENDERS:
        H.qualify(h, t["id"])
    H.mark()
    used = {c for t in TENDERS for k in t.get("clauses") or [] for c in (k["params"].get("categories") or [])}
    w = g.read("""MATCH (w:Work)-[:FOR_CLIENT]->(c:Client) WHERE NOT w.category IN $used
                  RETURN w.key AS key, w.title AS title, w.value AS v, c.name AS client ORDER BY w.key LIMIT 1""",
               {"used": sorted(used)})[0]
    before = {(c["case_id"], c["ref"]): c["stored"] for c in g.read(OPEN_CLAUSES)}
    out = ripple_event(H, g, synth_event("IRR", "corrigendum", w["client"], f"Corrigendum — {w['title']}", "Work",
                                         w["key"], "value", int(w["v"] * 0.5), "client_corrigendum"))
    imp = H.client.get(f"/api/impact/{out['doc']['doc_id']}", headers=h).json()["result"]
    after = {(c["case_id"], c["ref"]): c["stored"] for c in g.read(OPEN_CLAUSES)}
    H.check("cf.irrelevant_subject_no_flip", D, f"a corrigendum halving a {w['title'].split(' — ')[0]} work "
            "(a category no sample tender asks for) is decided but flips no clause and queues no review",
            {"decided": True, "flips": [], "verdicts_unchanged": True, "review_actions": 0},
            {"decided": bool(imp["decisions"]), "flips": sorted(flipped_pairs(imp)), "verdicts_unchanged": before == after,
             "review_actions": sum(1 for a in imp["actions"] if a["kind"] == "review_bid")})
    out = ripple_event(H, g, synth_event("IRR2", "corrigendum", cert["iss"], "Corrigendum (grading) — Pkg-113",
                                         "Work", PKG113, "grading", "Good", "client_corrigendum"))
    imp = H.client.get(f"/api/impact/{out['doc']['doc_id']}", headers=h).json()["result"]
    H.check("cf.irrelevant_predicate_no_flip", D, "a corrigendum on Pkg-113's grading (no clause reads grading) "
            "flips nothing although Pkg-113 carries the NHX similar-works clause",
            {"flips": [], "verdicts_unchanged": True},
            {"flips": sorted(flipped_pairs(imp)),
             "verdicts_unchanged": after == {(c["case_id"], c["ref"]): c["stored"] for c in g.read(OPEN_CLAUSES)}})
    # 5 expired credential at the bid date: re-checking the personnel clause on a date past every PMP expiry
    k = TENDER["T-PHED-WTP-2027-03"]["clauses"][1]
    assert k["kind"] == "key_personnel"
    from bidbrain.tools.proof import qualify_clause
    last = g.scalar("MATCH (c:Credential) WHERE toLower(c.kind) CONTAINS 'pmp' RETURN max(c.valid_through)")
    late = (dt.date.fromisoformat(last) + dt.timedelta(days=1)).isoformat()
    res = qualify_clause(g, k["kind"], k["params"], late)["result"]
    H.check("cf.expired_credentials_fail", D, f"bid date {late}, one day after the last PMP expiry in the graph "
            "(independent max over Credential.valid_through): the personnel clause FAILs with nobody qualified",
            {"verdict": "FAIL", "n_qualified": 0}, {"verdict": res["verdict"], "n_qualified": res["n_qualified"]})
    pcase = g.read("MATCH (c:Case {tender_ref:'T-PHED-WTP-2027-03'}) RETURN c.id AS id")[0]["id"]
    fnd = g.read("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->(k:Clause {kind:'key_personnel'})-[:HAS_FINDING]->
                     (f:Finding {kind:'expiry'}) RETURN f.severity AS sev, k.verdict AS v""", {"c": pcase})
    H.check("cf.expiry_finding_at_bid_date", D, "on PHED's own bid date the strongest PMP holders lapse within 90 "
            "days: the clause is AT_RISK with a high-severity expiry finding",
            [{"sev": "high", "v": "AT_RISK"}], fnd)
    H.cleanup()

    # 6 absence: PROVEN on a closed corpus, UNPROVEN once closure or the orphan search breaks
    g = H.copy(config.BASE_GRAPH)
    cl = g.read("""MATCH (w:Work)-[:FOR_CLIENT]->(c:Client) WHERE NOT (w)-[:HAS_REFERENCE]->(:Document)
                   RETURN c.name AS client, count(w) AS n ORDER BY n DESC, client LIMIT 1""")[0]
    gold_abs = sorted(r["k"] for r in g.read("""MATCH (w:Work)-[:FOR_CLIENT]->(:Client {name:$c})
                                                WHERE NOT (w)-[:HAS_REFERENCE]->(:Document) RETURN w.key AS k""",
                                             {"c": cl["client"]}))
    H.mark()
    p = prove_absence(g, "reference_letter", client=cl["client"])["result"]
    H.check("cf.absence_proven", D, f"works of {cl['client']} with no reference letter: the proof's absent set "
            "equals independent Cypher and the verdict is PROVEN on the closed corpus",
            {"verdict": "PROVEN", "absent": gold_abs},
            {"verdict": p["verdict"], "absent": sorted(a["key"] for a in p["absent"])})
    g.run("CREATE (:Document {doc_id:'DOC-REF-WFEVAL-1', key:'DOC-REF-WFEVAL-1', doc_type:'reference_letter', pages:1})")
    p = prove_absence(g, "reference_letter", client=cl["client"])["result"]
    H.check("cf.absence_unproven_closure", D, "one reference letter linked to no work breaks corpus closure: "
            "the same absence is UNPROVEN", "UNPROVEN", p["verdict"])
    g.run("MATCH (d:Document {doc_id:'DOC-REF-WFEVAL-1'}) DELETE d")
    title = g.scalar("MATCH (w:Work {key:$k}) RETURN w.title", {"k": gold_abs[0]})
    from bidbrain.ingest.chunks import _ft
    txt = f"Reference letter. We certify that {title} was completed satisfactorily."
    # a misfiled scan: the letter's text is in the estate, but its Document is not typed reference_letter,
    # so corpus closure (which counts reference_letter documents) still holds; only the orphan search can see it
    g.run("""CREATE (d:Document {doc_id:'DOC-REF-WFEVAL-2', key:'DOC-REF-WFEVAL-2', doc_type:'scan', pages:1})
             CREATE (:Chunk {id:'CH-WFEVAL', key:'CH-WFEVAL', doc_id:'DOC-REF-WFEVAL-2', page:1, text:$t, ft:$ft})
                    -[:PART_OF]->(d)""", {"t": txt, "ft": _ft(txt)})
    p = prove_absence(g, "reference_letter", client=cl["client"])["result"]
    hit = next((a for a in p["absent"] if a["key"] == gold_abs[0]), {})
    H.check("cf.absence_unproven_orphan_text", D, "an unlinked letter's text naming a 'missing' work is found by "
            "the orphan search: UNPROVEN, and that work carries the hit",
            {"verdict": "UNPROVEN", "hit": ["DOC-REF-WFEVAL-2"]},
            {"verdict": p["verdict"], "hit": hit.get("orphan_search_hits")})
    # 7 aliases
    r = resolve_entity(g, "ISRO", "client")
    H.check("cf.alias_unknown_not_found", D, "'ISRO' is not a client of this company: not_found, no candidates",
            {"not_found": True, "result": []}, {"not_found": r.get("not_found", False), "result": r["result"]})
    gold = g.scalar("""MATCH (c:Client) WHERE toLower(c.name) CONTAINS 'public health' AND
                       toLower(c.name) CONTAINS 'odisha' RETURN c.key""")
    r = resolve_entity(g, "phed odisha", "client")
    sc = [x["score"] for x in r["result"]]
    H.check("cf.alias_informal_resolves", D, "'phed odisha' resolves to the Public Health Engineering client in "
            "Odisha (independent Cypher) as the top candidate, strictly ahead of any other, not flagged ambiguous",
            {"top": gold, "decisive": True, "ambiguous": False},
            {"top": r["result"][0]["key"] if r["result"] else None,
             "decisive": len(sc) == 1 or (len(sc) > 1 and sc[0] > sc[1]),
             "ambiguous": bool(r["result"] and r["result"][0].get("ambiguous"))})
    r = resolve_entity(g, "Pkg-9999", "work")
    H.check("cf.alias_unknown_package", D, "a package number no work carries is not_found (never a nearest guess)",
            {"not_found": True, "result": []}, {"not_found": r.get("not_found", False), "result": r["result"]})
    g.delete(); H.made.discard(g.name)


def s_memory_transfer(H: Harness) -> None:
    D = "memory_transfer"
    H.fresh_org()
    org = H.engine.ORG_GRAPH
    k3, k4 = EV["DEMO-3"]["claims"][0]["subject_key"], EV["DEMO-4"]["claims"][0]["subject_key"]
    hA = H.session()
    H.fire(hA, "DEMO-3")
    card = next(c for c in H.client.get("/api/conflicts", headers=hA).json()["cards"] if c["key"] == k3)
    partner = next(c for c in card["claims"] if c["source_kind"] == "partner_certificate")
    res = H.client.post(f"/api/conflicts/{card['id']}/resolve", headers=hA,
                        json={"winner_claim": partner["id"], "apply_to_similar": True, "user": "eval",
                              "rationale": "the JV lead certifies our role"}).json()
    rule = res["decision"]["learned_rule"]
    H.mark()
    H.check("mt.nothing_promoted_implicitly", D, "learning a rule in session A writes nothing to the company graph",
            False, org in db().list_graphs() and bool(Graph(org).scalar("MATCH (r:OrgRule) RETURN count(r)")))
    oid = H.client.post("/api/memory/promote", json={"rule_id": rule, "user": "eval"}, headers=hA).json()["org_rule"]
    hB = H.session()
    gB = H.g(hB)
    out = H.fire(hB, "DEMO-4")
    H.check("mt.no_opt_in_no_transfer", D, "session B (company memory off) fires DEMO-4: still a human card",
            {"cards": 1, "decisions": 0}, {"cards": len(out["cards"]), "decisions": len(out["decisions"])})
    on = H.client.post("/api/session/settings", json={"use_company_memory": True}, headers=hB).json()
    d = (on.get("decisions") or [{}])[0]
    ref = gB.scalar("MATCH (:Decision {id:$d})-[:APPLIED_ORG_RULE]->(o:OrgRuleRef) RETURN o.id",
                    {"d": d.get("decision_id")})
    H.check("mt.opt_in_settles_with_org_rule", D, "B opts in: the open DEMO-4 card settles by policy org_rule, "
            "citing the promoted rule (APPLIED_ORG_RULE edge), and the role becomes the partner's value",
            {"policy": "org_rule", "ref": oid, "role": EV["DEMO-4"]["claims"][0]["value"], "open_cards": 0},
            {"policy": d.get("policy"), "ref": ref, "role": gB.scalar("MATCH (w:Work {key:$k}) RETURN w.role", {"k": k4}),
             "open_cards": gB.scalar("MATCH (k:ConflictCard {status:'open'}) RETURN count(k)")})
    r = H.client.post(f"/api/memory/org/{oid}/contest", json={"reason": "eval"}, headers=hB)
    card = (H.client.get("/api/conflicts", headers=hB).json()["cards"] or [{}])[0]
    H.check("mt.contest_reopens", D, "B contests the company rule: its org_rule decision is reopened as a card",
            {"status": 200, "reopened_from": d.get("decision_id")},
            {"status": r.status_code, "reopened_from": card.get("reopened_from")})
    hC = H.session()
    H.client.post("/api/session/settings", json={"use_company_memory": True}, headers=hC)
    dC = (H.fire(hC, "DEMO-4")["decisions"] or [{}])[0]
    r = H.client.post(f"/api/memory/org/{oid}/retire", json={"user": "eval"}, headers=hA)
    trace = H.client.get(f"/api/decisions/{dC.get('decision_id')}/trace", headers=hC).json()
    hD = H.session()
    H.client.post("/api/session/settings", json={"use_company_memory": True}, headers=hD)
    outD = H.fire(hD, "DEMO-4")
    H.check("mt.retire_stops_transfer", D, "A retires the rule: C's decision is flagged retired for review, and a "
            "new opted-in session D gets a human card again",
            {"retired": True, "c_status": "retired", "d_cards": 1, "d_decisions": 0},
            {"retired": r.json().get("retired"), "c_status": trace.get("org_rule_status"),
             "d_cards": len(outD["cards"]), "d_decisions": len(outD["decisions"])})
    H.cleanup()

    # company record: a published fact applies in a new workspace, citing the OrgFact
    H.fresh_org()
    hA = H.session()
    gA = H.g(hA)
    H.fire(hA, "DEMO-0")
    out = H.fire(hA, "DEMO-1")
    pub = next(a for a in H.client.get(f"/api/impact/{out['doc']['doc_id']}", headers=hA).json()["result"]["actions"]
               if a["kind"] == "publish_to_company_record")
    H.approve_publish(hA, pub["id"])
    hPre = H.session()                     # a workspace that exists (and opted in) before execution
    H.opt_in(hPre)
    H.mark()
    gPre = H.g(hPre)
    base_v = Graph().scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113})
    H.check("mt.unpublished_fact_stays_private", D, "approved but not executed: a new workspace still has the "
            "corpus value (independent read of the base graph)", base_v,
            gPre.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113}))
    of = H.act(hA, pub["id"], "execute").json()["result"]["org_fact"]
    hF = H.session()                       # company memory off (the default): the record never applies
    gF = H.g(hF)
    H.client.get("/api/cases", headers=hF)
    H.check("mt.company_fact_needs_opt_in", D, "a fresh workspace (company memory off) keeps the corpus value and "
            "applies nothing, so it reproduces the scripted demo", {"value": base_v, "applied": 0},
            {"value": gF.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113}),
             "applied": gF.scalar("MATCH (r:CompanyRecordRef) RETURN count(r)")})
    hN = H.session()
    gN = H.g(hN)
    H.opt_in(hN)
    dec = gN.read("""MATCH (d:Decision {subject_key:$k, predicate:'value', policy:'company_record'})-[:SELECTS]->(c:Claim)
                     RETURN d.rationale AS why, c.company_record AS rec""", {"k": PKG113})
    H.check("mt.company_fact_applies_in_new_session", D, "a new workspace starts from DEMO-1's value, decided by "
            "policy company_record, the winning claim and rationale citing the OrgFact",
            {"value": EV["DEMO-1"]["claims"][0]["value"], "decisions": 1, "cites": True, "claim_rec": of},
            {"value": gN.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113}), "decisions": len(dec),
             "cites": bool(dec) and of in dec[0]["why"], "claim_rec": dec[0]["rec"] if dec else None})
    H.client.get("/api/cases", headers=hPre)
    H.check("mt.existing_session_catches_up", D, "the workspace opened before publication catches up on its next "
            "request (company record applied once, policy company_record)",
            {"value": EV["DEMO-1"]["claims"][0]["value"], "applied": 1},
            {"value": gPre.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": PKG113}),
             "applied": gPre.scalar("MATCH (r:CompanyRecordRef {id:$f}) RETURN count(r)", {"f": of})})
    H.client.get("/api/cases", headers=hN)
    H.check("mt.company_fact_applied_once", D, "repeated requests do not re-apply the fact (one company_record "
            "decision, one claim)", {"decisions": 1, "claims": 1},
            {"decisions": gN.scalar("MATCH (d:Decision {subject_key:$k, policy:'company_record'}) RETURN count(d)", {"k": PKG113}),
             "claims": gN.scalar("MATCH (c:Claim {company_record:$f}) RETURN count(c)", {"f": of})})
    H.cleanup()


def s_isolation(H: Harness) -> None:
    D = "isolation"
    from bidbrain.agents import qa
    H.fresh_org()
    hA = H.session()
    gA = H.g(hA)
    case = H.qualify(hA, "T-PHED-WTP-2027-03")
    H.fire(hA, "DEMO-3")
    saved = qa.ask

    def fake_ask(g, question, emit=None, history=None, max_steps=10):
        return {"explanation": "Priya Patel led Ring Road — Uttar Pradesh Pkg-113.", "answer": "Priya Patel",
                "evidence": [{"doc_id": "DOC-CC-113", "page": 1, "what": "t"}], "steps": [], "usage": {},
                "subgraph": {"nodes": [{"id": "Person:priyapatel", "label": "Person", "name": "Priya Patel"}],
                             "edges": []}}
    qa.ask = fake_ask
    try:
        H.stream("/api/ask", hA, {"question": "Who led Pkg-113?"})
    finally:
        qa.ask = saved
    pdf = H.main.PDF_DIR / "DOC-DEMO-1.pdf"
    H.upload_sids.append(hA["X-Session"])
    r = H.client.post("/api/ingest", headers=hA, files={"file": ("corrigendum.pdf", pdf.read_bytes(), "application/pdf")})
    up = next((json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ") and '"document"' in l), {})
    up_doc = (up.get("doc") or {}).get("doc_id")
    aid = gA.scalar("MATCH (a:Action) RETURN a.id LIMIT 1")
    H.mark()
    a_state = {"cases": gA.scalar("MATCH (c:Case) RETURN count(c)"), "actions": gA.scalar("MATCH (a:Action) RETURN count(a)"),
               "turns": gA.scalar("MATCH (t:Turn) RETURN count(t)"),
               "cards": gA.scalar("MATCH (k:ConflictCard {status:'open'}) RETURN count(k)"),
               "upload": gA.scalar("MATCH (d:Document {doc_id:$d}) RETURN count(d)", {"d": up_doc})}
    H.check("is.a_has_private_state", D, "precondition: A holds a case, actions, turns, an open card and an upload",
            True, all(a_state.values()), passed=all(a_state.values()))
    hB = H.session()
    gB = H.g(hB)
    H.check("is.b_api_sees_nothing", D, "B's API lists none of A's cases, actions, turns or cards",
            {"cases": [], "actions": [], "turns": [], "cards": []},
            {"cases": H.client.get("/api/cases", headers=hB).json(),
             "actions": H.client.get("/api/actions", headers=hB).json()["actions"],
             "turns": H.client.get("/api/memory", headers=hB).json()["turns"],
             "cards": H.client.get("/api/conflicts", headers=hB).json()["cards"]})
    H.check("is.b_graph_has_none", D, "B's graph (independent Cypher) has no Case, Action, Turn, open card, or A's "
            "uploaded Document/Chunks", {"cases": 0, "actions": 0, "turns": 0, "cards": 0, "upload": 0, "chunks": 0},
            {"cases": gB.scalar("MATCH (c:Case) RETURN count(c)"), "actions": gB.scalar("MATCH (a:Action) RETURN count(a)"),
             "turns": gB.scalar("MATCH (t:Turn) RETURN count(t)"),
             "cards": gB.scalar("MATCH (k:ConflictCard {status:'open'}) RETURN count(k)"),
             "upload": gB.scalar("MATCH (d:Document {doc_id:$d}) RETURN count(d)", {"d": up_doc}),
             "chunks": gB.scalar("MATCH (c:Chunk {doc_id:$d}) RETURN count(c)", {"d": up_doc})})
    st = gA.scalar("MATCH (a:Action {id:$a}) RETURN a.status", {"a": aid})
    codes = [H.client.get(f"/api/actions/{aid}", headers=hB).status_code, H.act(hB, aid, "approve").status_code,
             H.act(hB, aid, "execute").status_code, H.client.get(f"/api/cases/{case['id']}", headers=hB).status_code,
             H.client.get(f"/api/impact/{up_doc}", headers=hB).status_code]
    H.check("is.cross_session_ids_404", D, "A's action (get/approve/execute), case and uploaded document are 404 "
            "from B, and A's action is untouched", {"codes": [404] * 5, "a_status": st},
            {"codes": codes, "a_status": gA.scalar("MATCH (a:Action {id:$a}) RETURN a.status", {"a": aid})})
    H.check("is.upload_text_private", D, "A's upload has text in A, none from B", {"A": True, "B": []},
            {"A": bool(H.client.get(f"/api/docs/{up_doc}/text", headers=hA).json()["chunks"]),
             "B": H.client.get(f"/api/docs/{up_doc}/text", headers=hB).json()["chunks"]})
    H.check("is.upload_folders_distinct", D, "uploads live in per-session folders named by hash, never the session id",
            {"distinct": True, "sid_in_path": False},
            {"distinct": H.sessions.upload_dir(hA["X-Session"]) != H.sessions.upload_dir(hB["X-Session"]),
             "sid_in_path": hA["X-Session"] in str(H.sessions.upload_dir(hA["X-Session"]))})
    # MCP: never writes outside a session graph
    from mcp.server.mcpserver.exceptions import ToolError

    from bidbrain.mcp import server
    base_before = _counts(config.BASE_GRAPH)
    errs = []
    for sid in ("bidbrain_base", "../base", "abc"):
        try:
            server.qualify_tender(sid, tender_id="T-NHX-2026-014", use_llm=False)
            errs.append("ran")
        except ToolError:
            errs.append("refused")
    saved_gf = H.sessions.graph_for
    for target in (config.BASE_GRAPH, "wfeval_not_a_session"):
        H.sessions.graph_for = lambda sid, _t=target: Graph(_t)
        try:
            server.qualify_tender("abcdef123456", tender_id="T-NHX-2026-014", use_llm=False)
            errs.append("ran")
        except ToolError as exc:
            errs.append("refused" if "outside a session" in str(exc) else f"other:{exc}")
        finally:
            H.sessions.graph_for = saved_gf
    H.check("is.mcp_refuses_non_session", D, "MCP qualify_tender refuses invalid ids and any graph that is not a "
            "session_* graph (base, other), leaving the base graph unchanged",
            {"calls": ["refused"] * 5, "base": base_before},
            {"calls": errs, "base": _counts(config.BASE_GRAPH)}, )
    res = server.qualify_tender("new", tender_id="T-RBC-2026-31", use_llm=False)
    H.made.add(res["graph"])
    H.check("is.mcp_writes_its_session_only", D, "MCP 'new' writes the case to a fresh session graph only "
            "(A and B case counts unchanged)", {"graph_prefix": True, "case_there": 1, "A": a_state["cases"], "B": 0},
            {"graph_prefix": res["graph"].startswith("session_"),
             "case_there": Graph(res["graph"]).scalar("MATCH (c:Case {id:$c}) RETURN count(c)", {"c": res["case_id"]}),
             "A": gA.scalar("MATCH (c:Case) RETURN count(c)"), "B": gB.scalar("MATCH (c:Case) RETURN count(c)")})
    H.cleanup()


def s_temporal(H: Harness) -> None:
    D = "temporal"
    H.fresh_org()
    h = H.session()
    g = H.g(h)
    H.fire(h, "DEMO-0")
    H.fire(h, "DEMO-1")
    H.mark()
    base = Graph()
    gold_old = base.scalar("""MATCH (c:Claim {subject_key:$k, predicate:'value', source_kind:'client_certificate'})
                              RETURN c.value""", {"k": PKG113})
    new_v = EV["DEMO-1"]["claims"][0]["value"]
    rec = g.read("""MATCH (c:Claim {subject_key:$k, predicate:'value', source_kind:'client_corrigendum'})
                    RETURN c.recorded_at AS t, c.id AS id""", {"k": PKG113})[0]
    tool = lambda as_of: H.client.post("/api/tool/fact_as_of", headers=h,
                                       json={"key": PKG113, "predicate": "value", "as_of": as_of}).json()["result"]
    # valid time: the certificate counts from the date its document prints (base-graph Cypher), not before
    cert_day = base.scalar("""MATCH (c:Claim {subject_key:$k, predicate:'value', source_kind:'client_certificate'})
                              -[:FROM]->(d:Document) RETURN d.issued""", {"k": PKG113})
    early, before_doc = tool(cert_day), tool("2000-01-01")
    H.check("tm.before_corrigendum_cert_value", D, "as of the certificate's own date the company believed the "
            "certificate's value (base-graph Cypher), with the corrigendum listed as learned later; as of 2000-01-01, "
            "before the certificate existed, nothing, with the certificate listed as not yet issued",
            {"value": gold_old, "learned_later": True, "before_doc": None, "not_yet_issued": True},
            {"value": _num(early["value"]), "learned_later": any(r["claim"] == rec["id"] for r in early["learned_later"]),
             "before_doc": before_doc["value"],
             "not_yet_issued": any(r["source_kind"] == "client_certificate" and r["valid_from"] == cert_day
                                   for r in before_doc.get("not_yet_issued", []))})
    just_before, just_after = tool(_iso_ms(rec["t"] - 5)), tool(_iso_ms(rec["t"] + 5))
    H.check("tm.instant_before_vs_after", D, "5 ms before the corrigendum was recorded: the certificate value; "
            "5 ms after: the corrigendum value", [gold_old, new_v],
            [_num(just_before["value"]), _num(just_after["value"])])
    now = tool((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat())
    H.check("tm.now_corrigendum", D, "as of tomorrow the believed value is the corrigendum's, from its document",
            {"value": new_v, "claim": rec["id"]}, {"value": _num(now["value"]), "claim": (now["claim"] or {}).get("claim")})
    old = g.read("""MATCH (c:Claim {subject_key:$k, predicate:'value'}) WHERE c.source_kind IN
                    ['client_certificate', 'portfolio'] RETURN c.source_kind AS kind, c.value AS v, c.status AS s,
                    c.invalidated_at AS t, c.invalidated_by AS by ORDER BY kind""", {"k": PKG113})
    dec = g.scalar("MATCH (d:Decision {subject_key:$k, predicate:'value'})-[:SELECTS]->(:Claim {id:$c}) RETURN d.id",
                   {"k": PKG113, "c": rec["id"]})
    H.check("tm.superseded_keep_value", D, "the set-aside certificate and portfolio claims keep their values, are "
            "not active, and carry invalidated_at and invalidated_by = the corrigendum's decision",
            [{"kind": k, "v": gold_old, "inactive": True, "has_t": True, "by": dec} for k in ("client_certificate", "portfolio")],
            [{"kind": r["kind"], "v": r["v"], "inactive": r["s"] != "active", "has_t": r["t"] is not None, "by": r["by"]}
             for r in old])
    H.cleanup()


def s_provenance(H: Harness) -> None:
    D = "provenance"
    from bidbrain.agents.orchestrator import _evidence
    from bidbrain.tools.proof import qualify_clause
    H.fresh_org()
    h = H.session()
    g = H.g(h)
    pages = None
    for t in TENDERS:
        case = H.qualify(h, t["id"])
        H.mark()
        if pages is None:
            pages = {r["d"]: r["p"] for r in g.read("MATCH (d:Document) RETURN d.doc_id AS d, d.pages AS p")}
        rows = _evidence(g, case["id"])
        for k in t.get("clauses") or []:            # each clause tool's own evidence rows, too
            try:
                ev = qualify_clause(g, k["kind"], k["params"], t["bid_date"]).get("evidence") or []
            except Exception:
                ev = []
            rows += [{"clause": k["id"], "kind": "tool", "item": e.get("what"), "doc": e.get("doc_id"),
                      "page": e.get("page")} for e in ev]
        bad = [r for r in rows if r["doc"] not in pages
               or (r["page"] is not None and pages[r["doc"]] is not None and r["page"] > pages[r["doc"]])
               or (r["page"] is not None and r["page"] < 1)]
        H.check(f"pv.evidence.{t['id']}", D, f"{t['id']}: every evidence row (bid-pack table + clause tools) names "
                "an existing Document and a page within its page count", {"rows": len(rows), "bad": []},
                {"rows": len(rows), "bad": bad[:6]}, passed=bool(rows) and not bad)
    H.fire(h, "DEMO-0")
    H.fire(h, "DEMO-1")
    H.fire(h, "DEMO-2")
    H.fire(h, "DEMO-3")
    card = H.client.get("/api/conflicts", headers=h).json()["cards"][0]
    H.client.post(f"/api/conflicts/{card['id']}/resolve", headers=h,
                  json={"winner_claim": card["claims"][0]["id"], "rationale": "eval"})
    H.mark()
    decs = g.read("""MATCH (d:Decision)
                     OPTIONAL MATCH (d)-[:SELECTS]->(w:Claim)
                     OPTIONAL MATCH (d)-[:BASED_ON]->(b:Claim)
                     OPTIONAL MATCH (b)-[:FROM]->(doc:Document)
                     WITH d, count(DISTINCT w) AS sel, count(DISTINCT b) AS based,
                          count(DISTINCT CASE WHEN doc IS NULL THEN NULL ELSE b END) AS sourced
                     RETURN d.id AS id, d.policy AS policy, sel, based, sourced""")
    bad = [d for d in decs if d["sel"] != 1 or d["based"] < 1 or d["sourced"] != d["based"]]
    H.check("pv.decisions_sourced", D, "every Decision SELECTS exactly one claim, is BASED_ON at least one, and "
            "every BASED_ON claim comes FROM a Document", {"n": len(decs), "bad": []}, {"n": len(decs), "bad": bad},
            passed=len(decs) >= 4 and not bad)
    sel_in = g.scalar("""MATCH (d:Decision)-[:SELECTS]->(w:Claim) WHERE NOT (d)-[:BASED_ON]->(w) RETURN count(d)""")
    H.check("pv.winner_among_basis", D, "the selected claim is always one of the claims the decision was based on",
            0, sel_in)
    docs = g.read("MATCH (d:Document) WHERE d.live = true RETURN d.doc_id AS d")
    missing = []
    for d in docs:
        imp = H.client.get(f"/api/impact/{d['d']}", headers=h).json()
        missing += [e for e in imp["evidence"] if e["doc_id"] != d["d"] or e["page"] is None]
    acts = g.scalar("""MATCH (a:Action) WHERE a.kind IN ['review_bid','publish_to_company_record']
                       AND NOT (a)-[:CITES]->(:Document) RETURN count(a)""")
    H.check("pv.impact_and_actions_cite_documents", D, "change_impact evidence rows cite their document with a "
            "page, and every review/publish action CITES a Document", {"bad_rows": [], "uncited_actions": 0},
            {"bad_rows": missing[:5], "uncited_actions": acts}, passed=bool(docs) and not missing and acts == 0)
    H.cleanup()


SCENARIOS = {"task_completion": s_task_completion, "impact_recall": s_impact_recall, "conflict": s_conflict,
             "memory_transfer": s_memory_transfer, "isolation": s_isolation, "temporal": s_temporal,
             "provenance": s_provenance}


# ================================================================================================ main

def run(only: list[str] | None = None, write: bool = True) -> dict:
    t0 = time.time()
    extra: dict = {}
    with Harness() as H:
        for name in only or DIMENSIONS:
            fn = SCENARIOS[name]
            H.mark()
            try:
                fn(H, extra) if name == "impact_recall" else fn(H)
            except Exception as exc:
                H.check(f"{name}.crashed", name, "the scenario ran to completion", "no exception",
                        f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}", passed=False)
            finally:
                H.cleanup()
        H.fresh_org()
        Graph(H.engine.ORG_GRAPH).delete()
        base_ok = _counts(config.BASE_GRAPH) == H.base_counts
        org_ok = _counts(H.real_org) == H.org_counts
        H.check("is.base_and_org_untouched", "isolation", "after the whole run bidbrain_base and the real "
                "bidbrain_org have the same node and edge counts as before",
                {"base": H.base_counts, "org": H.org_counts},
                {"base": _counts(config.BASE_GRAPH), "org": _counts(H.real_org)}, passed=base_ok and org_ok)
        rows = H.rows
    left = [n for n in db().list_graphs() if n.startswith("wfeval_")]
    by_dim = {}
    for r in rows:
        d = by_dim.setdefault(r["dimension"], {"n": 0, "passed": 0})
        d["n"] += 1
        d["passed"] += r["passed"]
    for d in by_dim.values():
        d["rate"] = round(d["passed"] / d["n"], 4)
    cf = {}
    for p, key in (("conflict_pr_json.json", "json"), ("conflict_pr_pdf.json", "pdf")):
        f = config.DATA / "eval" / p
        if f.exists():
            s = json.loads(f.read_text()).get("summary", {})
            cf[key] = {k: s.get(k) for k in ("n_events", "n_pdfs", "detection", "routing_accuracy", "winner_accuracy")}
    summary = {"summary": {"n_cases": len(rows), "passed": sum(r["passed"] for r in rows),
                           "pass_rate": round(sum(r["passed"] for r in rows) / max(1, len(rows)), 4),
                           "by_dimension": by_dim, "impact": extra.get("impact_recall"),
                           "failed": [r["id"] for r in rows if not r["passed"]],
                           "conflict_pr_cited": cf, "leftover_eval_graphs": left,
                           "seconds": round(time.time() - t0, 1), "llm_calls": 0,
                           "run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}}
    if write:
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        RESULTS.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        SUMMARY.write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    summary["rows"] = rows
    return summary


def report(summary: dict | None = None, rows: list[dict] | None = None) -> str:
    summary = summary or json.loads(SUMMARY.read_text())
    s = summary["summary"]
    rows = rows if rows is not None else [json.loads(l) for l in RESULTS.read_text().splitlines() if l.strip()]
    out = [f"{'dimension':<18}{'cases':>6}{'passed':>8}{'rate':>8}", "-" * 40]
    for d in DIMENSIONS:
        if d in s["by_dimension"]:
            x = s["by_dimension"][d]
            out.append(f"{d:<18}{x['n']:>6}{x['passed']:>8}{x['rate'] * 100:>7.1f}%")
    out += ["-" * 40, f"{'total':<18}{s['n_cases']:>6}{s['passed']:>8}{s['pass_rate'] * 100:>7.1f}%"]
    if s.get("impact"):
        p, c = s["impact"]["pairs"], s["impact"]["cases"]
        out.append(f"impact (case,clause) P={p['precision']:.3f} R={p['recall']:.3f} (tp {p['tp']} fp {p['fp']} "
                   f"fn {p['fn']}); case-level P={c['precision']:.3f} R={c['recall']:.3f}")
    for k, v in (s.get("conflict_pr_cited") or {}).items():
        det = v.get("detection") or {}
        out.append(f"conflict P/R ({k}, cited from eval.conflict_pr): n={v.get('n_events') or v.get('n_pdfs')} P={det.get('precision')} "
                   f"R={det.get('recall')} routing={v.get('routing_accuracy')} winner={v.get('winner_accuracy')}")
    fails = [r for r in rows if not r["passed"]]
    if fails:
        out.append("failed:")
        out += [f"  {r['id']}: expected {json.dumps(r['expected'])[:160]} | observed {json.dumps(r['observed'])[:160]}"
                for r in fails]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", action="store_true", help="print the table of the last run and exit")
    ap.add_argument("--only", help="comma-separated dimensions: " + ",".join(DIMENSIONS))
    a = ap.parse_args()
    if a.report:
        print(report())
        return
    only = [x.strip() for x in a.only.split(",")] if a.only else None
    s = run(only, write=only is None)
    print(report(s, s["rows"]))


if __name__ == "__main__":
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    main()
