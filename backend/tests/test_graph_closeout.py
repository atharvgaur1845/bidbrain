"""Graph closeout (round 4): blast_radius dependency paths, fact_as_of time semantics, Tender status, projections.

Needs `bidbrain_base` loaded (`python -m bidbrain.ingest.load_graph`). Writes go to GRAPH.COPY scratch graphs only.
"""
from __future__ import annotations

import datetime as dt
import json
import time

import pytest

from bidbrain import config
from bidbrain.graph.client import Graph, day
from bidbrain.ingest.load_graph import ensure_projections
from bidbrain.tools.case import apply_flips, blast_radius, case_open
from bidbrain.tools.impact import TZ, _instant, fact_as_of

PKG116 = "sixlanehighwaywestbengalpkg116"
IST = TZ


def _copy(name: str) -> Graph:
    base = Graph()
    assert base.exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    for _ in range(60):          # FalkorDB forks for GRAPH.COPY and Redis allows one child at a time
        try:
            return base.copy_to(name)
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return base.copy_to(name)


def _ms(d: dt.datetime) -> int:
    return int(d.timestamp() * 1000)


# ------------------------------------------------------------------ blast_radius: past bids through led works


def _expected_bids(g: Graph, person: str) -> dict[str, set[str]]:
    rows = g.read("""MATCH (p:Person {key:$k})<-[:LED_BY]-(w:Work)<-[:CITES]-(b:Bid) RETURN b.key AS b, w.key AS w
                     UNION MATCH (p:Person {key:$k})<-[:PROPOSES]-(b:Bid) RETURN b.key AS b, null AS w""", {"k": person})
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["b"], set()).add(r["w"])
    return out


def test_person_history_reaches_every_bid_citing_their_works():
    g = Graph()
    exp = _expected_bids(g, "rahulmenon")
    assert len(exp) >= 2                              # a person whose history affects more than one bid
    r = blast_radius(g, "Person", "rahulmenon", rerun=False)["result"]
    got = {b["bid"]: b for b in r["past_bids_citing"]}
    assert set(got) == set(exp)                       # every dependent, nothing else
    unrelated = {b for b in g.read("MATCH (b:Bid) RETURN b.key AS b") for b in [b["b"]]} - set(exp)
    assert unrelated and not unrelated & set(got)
    for bid, works in exp.items():
        assert {p.split("<-[:CITES]-")[0].rsplit("Work:", 1)[1] for p in got[bid]["paths"]} == works
        for p in got[bid]["paths"]:
            assert p.startswith("Person:rahulmenon<-[:LED_BY]-Work:") and p.endswith(f"<-[:CITES]-Bid:{bid}")
    # structured paths: node ids and relationship types from the subject to the bid
    dep = next(d for d in r["dependents"] if d["kind"] == "bid")
    path = dep["paths"][0]
    assert path["nodes"][0] == "Person:rahulmenon" and path["nodes"][-1] == f"Bid:{dep['id']}"
    assert [h["type"] for h in path["rels"]] == ["LED_BY", "CITES"] and {h["dir"] for h in path["rels"]} == {"<-"}
    # tenders those bids were for, with their status
    tenders = {t["tender"] for t in r["tenders"]}
    assert tenders == {t for t in g.read("""MATCH (:Person {key:'rahulmenon'})<-[:LED_BY]-(:Work)<-[:CITES]-(:Bid)
                                            -[:FOR_TENDER]->(t:Tender) RETURN DISTINCT t.key AS t""")
                       for t in [t["t"]]}
    assert all(t["status"] == "closed" for t in r["tenders"])
    assert r["tenders_secured"] == []                 # SECURES is a bond's edge, not a person's
    # the credentials they hold depend on them too
    creds = {c["c"] for c in g.read("MATCH (:Person {key:'rahulmenon'})-[:HOLDS]->(c) RETURN c.key AS c")}
    assert {d["id"] for d in r["dependents"] if d["kind"] == "credential"} == creds


def test_credential_reaches_bids_through_its_holder():
    g = Graph()
    cred = g.scalar("MATCH (:Person {key:'rahulmenon'})-[:HOLDS]->(c) RETURN c.key ORDER BY c.key LIMIT 1")
    r = blast_radius(g, "Credential", cred, rerun=False)["result"]
    assert {b["bid"] for b in r["past_bids_citing"]} == set(_expected_bids(g, "rahulmenon"))
    assert all(b["path"].startswith(f"Credential:{cred}<-[:HOLDS]-Person:rahulmenon<-[:LED_BY]-")
               for b in r["past_bids_citing"])
    assert [d["id"] for d in r["dependents"] if d["kind"] == "person"] == ["rahulmenon"]


def test_bond_secures_its_tender():
    g = Graph()
    b = g.read("MATCH (b:Bond)-[:SECURES]->(t:Tender) RETURN b.key AS b, t.key AS t LIMIT 1")[0]
    r = blast_radius(g, "Bond", b["b"], rerun=False)["result"]
    assert [t["tender"] for t in r["tenders_secured"]] == [b["t"]]


# ------------------------------------------------------------------ blast_radius on live cases, then the ripple


@pytest.fixture(scope="module")
def cases():
    from bidbrain.agents.orchestrator import qualify_tender
    s = _copy("pytest_graph_closeout")
    ts = {t["id"]: t for t in json.load(open(config.DATA / "tenders" / "demo_tenders.json"))}
    ids = {}
    for t in ("T-NHX-2026-014", "T-PHED-WTP-2027-03", "T-JN-LB-2026-09"):
        qualify_tender(s, ts[t], prefer_llm=False)
        ids[t] = s.scalar("MATCH (c:Case)-[:FOR_TENDER]->(:Tender {key:$t}) RETURN c.id", {"t": t})
    yield s, ids
    s.delete()


def test_person_reaches_every_open_case_and_clause_relying_on_them(cases):
    s, ids = cases
    exp = {(r["c"], r["k"]) for r in s.read("""MATCH (:Person {key:'amitiyer'})<-[:SATISFIED_BY]-(k:Clause)
                                              <-[:HAS_CLAUSE]-(c:Case) RETURN c.id AS c, k.id AS k""")}
    assert {c for c, _ in exp} == {ids["T-NHX-2026-014"], ids["T-PHED-WTP-2027-03"]}
    r = blast_radius(s, "Person", "amitiyer", rerun=False)["result"]
    assert {(d["case_id"], d["clause_id"]) for d in r["cases_relying"]} == exp
    assert ids["T-JN-LB-2026-09"] not in {d["case_id"] for d in r["cases_relying"]}     # unrelated case excluded
    assert all(d["case"] for d in r["cases_relying"])
    assert {"T-NHX-2026-014", "T-PHED-WTP-2027-03"} <= {t["tender"] for t in r["tenders"]}
    live = [t for t in r["tenders"] if t["tender"] in ("T-NHX-2026-014", "T-PHED-WTP-2027-03")]
    assert all(t["status"] == "open" and "-[:FOR_TENDER]->Tender:" in t["path"] for t in live)
    assert {d["id"] for d in r["dependents"] if d["kind"] == "case"} == {c for c, _ in exp}


def test_person_reaches_similar_works_clauses_through_the_work_hop(cases):
    s, ids = cases
    row = s.read("""MATCH (p:Person)<-[:LED_BY]-(w:Work)<-[:SATISFIED_BY]-(k:Clause {kind:'similar_works'})
                    <-[:HAS_CLAUSE]-(c:Case) RETURN p.key AS p, w.key AS w, k.id AS k, c.id AS c LIMIT 1""")[0]
    r = blast_radius(s, "Person", row["p"])["result"]
    hit = next(d for d in r["cases_relying"] if d["clause_id"] == row["k"])
    assert hit["path"] == (f"Person:{row['p']}<-[:LED_BY]-Work:{row['w']}<-[:SATISFIED_BY]-Clause:{row['k']}"
                           f"<-[:HAS_CLAUSE]-Case:{row['c']}")
    assert r["clauses_rechecked"] >= 1 and not r["verdict_flips"]         # nothing changed: nothing flips
    cred = s.scalar("MATCH (:Person {key:$p})-[:HOLDS]->(c) RETURN c.key LIMIT 1", {"p": row["p"]})
    if cred:
        rc = blast_radius(s, "Credential", cred, rerun=False)["result"]
        assert row["k"] in {d["clause_id"] for d in rc["cases_relying"]}


def test_discovery_leads_to_re_evaluation(cases):
    s, ids = cases
    pmp = s.scalar("MATCH (:Person {key:'amitiyer'})-[:HOLDS]->(c:Credential {kind_key:'pmp'}) RETURN c.key")
    clauses = {r["k"] for r in s.read("MATCH (:Person {key:'amitiyer'})<-[:SATISFIED_BY]-(k:Clause) RETURN k.id AS k")}
    jn_findings = s.scalar("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->()-[:HAS_FINDING]->(f:Finding {kind:'ripple'})
                              RETURN count(f)""", {"c": ids["T-JN-LB-2026-09"]})
    s.run("MATCH (c:Credential {key:$k}) SET c.valid_through = '2020-01-01', c.valid_through_day = $d",
          {"k": pmp, "d": day("2020-01-01")})
    r = blast_radius(s, "Credential", pmp)["result"]
    touched = {f["clause_id"] for f in r["verdict_flips"] + r["evidence_changed"]}
    assert clauses <= touched                         # every clause the lapsed credential's holder satisfied
    changed = apply_flips(s, r["verdict_flips"], f"credential {pmp} lapsed", r["evidence_changed"])
    assert {c["case_id"] for c in changed} == {ids["T-NHX-2026-014"], ids["T-PHED-WTP-2027-03"]}
    assert all(c["memo"] == "rebuilt" for c in changed)
    assert s.scalar("MATCH (:Person {key:'amitiyer'})<-[:SATISFIED_BY]-(k:Clause) RETURN count(k)") == 0
    assert s.scalar("""MATCH (k:Clause)-[:HAS_FINDING]->(f:Finding {kind:'ripple'}) WHERE k.id IN $ks
                       RETURN count(DISTINCT k)""", {"ks": sorted(clauses)}) == len(clauses)
    assert s.scalar("""MATCH (:Case {id:$c})-[:HAS_CLAUSE]->()-[:HAS_FINDING]->(f:Finding {kind:'ripple'})
                       RETURN count(f)""", {"c": ids["T-JN-LB-2026-09"]}) == jn_findings   # unrelated case untouched


# ------------------------------------------------------------------ Tender status


def test_every_tender_has_a_status_from_the_snapshot():
    g = Graph()
    snap = g.scalar("MATCH (m:Meta) RETURN m.corpus_snapshot")
    rows = g.read("""MATCH (t:Tender) RETURN t.key AS k, t.status AS s, t.corpus_status AS cs, t.bid_date AS d,
                     t.status_basis AS why, t.status_as_of AS at, size([(b:Bid)-[:FOR_TENDER]->(t) | b]) AS bids""")
    assert len(rows) == 63 and {r["s"] for r in rows} == {"open", "closed"}
    for r in rows:
        assert r["s"] == r["cs"] and r["why"] and r["at"] == snap
        if r["bids"]:
            assert r["s"] == "closed"
        elif r["d"]:
            assert r["s"] == ("open" if r["d"] >= snap else "closed"), r
    idx = {(x["label"], p) for x in g.read("CALL db.indexes() YIELD label, properties RETURN label, properties")
           for p in x["properties"]}
    assert ("Tender", "status") in idx


def test_a_live_case_opens_its_tender_in_that_session_only():
    s = _copy("pytest_graph_closeout_tender")
    try:
        t = s.scalar("MATCH (t:Tender {status:'closed'}) RETURN t.key ORDER BY t.key LIMIT 1")
        case_open(s, "probe", tender_ref=t)
        row = s.read("MATCH (t:Tender {key:$t}) RETURN t.status AS s, t.corpus_status AS cs, t.status_basis AS b",
                     {"t": t})[0]
        assert row["s"] == "open" and row["cs"] == "closed" and row["b"].startswith("live case CASE-")
        assert Graph().scalar("MATCH (t:Tender {key:$t}) RETURN t.status", {"t": t}) == "closed"
    finally:
        s.delete()


# ------------------------------------------------------------------ OVERLAPS_CLIENTS follows session writes


def test_projections_rebuild_when_who_led_what_changes():
    assert ensure_projections(Graph()) is False       # the base graph is current: read-only
    s = _copy("pytest_graph_closeout_proj")
    try:
        assert ensure_projections(s) is False
        w = s.read("""MATCH (w:Work)-[l:LED_BY]->(p:Person), (w)-[:FOR_CLIENT]->(c:Client), (q:Person)
                      WHERE q <> p AND NOT (q)-[:WORKED_FOR]->(c)
                      RETURN w.key AS w, q.key AS q, c.key AS c LIMIT 1""")[0]
        s.run("MATCH (w:Work {key:$w})-[l:LED_BY]->() DELETE l", {"w": w["w"]})
        s.run("MATCH (w:Work {key:$w}), (q:Person {key:$q}) CREATE (w)-[:LED_BY]->(q)", w)
        assert ensure_projections(s) is True
        assert s.scalar("MATCH (:Person {key:$q})-[:WORKED_FOR]->(:Client {key:$c}) RETURN count(*)", w) == 1
        assert ensure_projections(s) is False
    finally:
        s.delete()


# ------------------------------------------------------------------ fact_as_of: input times


def test_date_only_means_end_of_day_in_kolkata():
    ms, d, how = _instant("2026-10-18")
    assert dt.datetime.fromtimestamp(ms / 1000, IST) == dt.datetime(2026, 10, 18, 23, 59, 59, 999000, IST)
    assert d == day("2026-10-18") and "end of day" in how
    # timestamps keep their instant and offset; a naive one is read in Asia/Kolkata
    assert _instant("2026-10-18T15:00:00+05:30")[0] == _instant("2026-10-18T09:30:00Z")[0]
    assert _instant("2026-10-18T15:00:00")[0] == _instant("2026-10-18T15:00:00+05:30")[0]
    # the valid-time day of a timestamp is its Kolkata date: 20:00 UTC on the 17th is the 18th in IST
    assert _instant("2026-10-17T20:00:00Z")[1] == day("2026-10-18")


@pytest.fixture()
def live():
    """A scratch graph with a live document (issued 2026-10-01) whose claim about PKG116's value was recorded
    at 15:00 IST on 2026-10-18, beside the corpus claims."""
    s = _copy("pytest_graph_closeout_time")
    rec = _ms(dt.datetime(2026, 10, 18, 15, 0, tzinfo=IST))
    s.run("""CREATE (d:Document {key:'DOC-T', doc_id:'DOC-T', live:true, issued:'2026-10-01', issued_day:$dd})
             CREATE (c:Claim {id:'CL-T', subject_key:$k, predicate:'value', value:1, value_str:'1',
                              source_kind:'client_corrigendum', authority:99, recorded_at:$r, status:'active'})
             CREATE (c)-[:FROM {page:1}]->(d)
             WITH c MATCH (w:Work {key:$k}) CREATE (c)-[:ABOUT]->(w)""",
          {"k": PKG116, "r": rec, "dd": day("2026-10-01")})
    yield s, rec
    s.delete()


def _v(g, **kw):
    return fact_as_of(g, PKG116, "value", **kw)["result"]


def test_a_change_during_the_requested_day_is_visible(live):
    s, rec = live
    assert _v(s, as_of="2026-10-18")["claim"]["claim"] == "CL-T"
    assert _v(s, as_of="2026-10-17")["claim"]["claim"] != "CL-T"
    assert "CL-T" in {c["claim"] for c in _v(s, as_of="2026-10-17")["learned_later"]}


def test_recorded_time_boundaries(live):
    s, rec = live
    at = dt.datetime.fromtimestamp(rec / 1000, dt.timezone.utc)
    assert _v(s, as_of=at.isoformat())["claim"]["claim"] == "CL-T"                           # inclusive
    assert _v(s, as_of=(at - dt.timedelta(milliseconds=1)).isoformat())["claim"]["claim"] != "CL-T"  # exclusive
    # the same instant written in Kolkata time, and with no offset (read in Kolkata)
    assert _v(s, as_of="2026-10-18T15:00:00+05:30")["claim"]["claim"] == "CL-T"
    assert _v(s, as_of="2026-10-18T14:59:59.999")["claim"]["claim"] != "CL-T"


def test_valid_time_compares_days_inclusively(live):
    s, _ = live
    assert _v(s, valid_at="2026-10-01", known_at="2026-10-19")["claim"]["claim"] == "CL-T"
    r = _v(s, valid_at="2026-09-30", known_at="2026-10-19")
    assert r["claim"]["claim"] != "CL-T" and "CL-T" in {c["claim"] for c in r["not_yet_issued"]}
    # a corpus fact before its printed completion date: none, and why
    early = _v(s, as_of="2020-02-10")
    assert early["value"] is None and early["why_none"].startswith("no recorded claim's fact had started")
    assert _v(s, as_of="2020-02-11")["value"] is not None


def test_valid_at_and_known_at_are_separate_axes(live):
    s, _ = live
    # valid on the 5th, as known on the 17th: the corrigendum was not yet recorded
    a = _v(s, valid_at="2026-10-05", known_at="2026-10-17")
    assert a["claim"]["claim"] != "CL-T" and a["valid_day"] == "2026-10-05"
    # valid on the 5th, as known on the 19th: it was
    b = _v(s, valid_at="2026-10-05", known_at="2026-10-19")
    assert b["claim"]["claim"] == "CL-T" and b["read_as"]["known_at"].startswith("date")
    # as_of sets both; an explicit axis overrides it
    assert _v(s, as_of="2026-10-19", known_at="2026-10-17")["claim"]["claim"] != "CL-T"
    with pytest.raises(ValueError):
        _v(s, valid_at="2026-10-05")


def test_superseded_fact_and_no_rewriting_of_prior_decisions(live):
    s, rec = live
    t = rec + 3_600_000                    # a decision an hour after the corrigendum was recorded
    old = s.read("""MATCH (c:Claim {subject_key:$k, predicate:'value'}) WHERE c.id <> 'CL-T' AND c.invalidated_at IS NULL
                    RETURN c.id AS id""", {"k": PKG116})
    before_decisions = s.read("MATCH (d:Decision) RETURN d.id AS id, d.at AS at ORDER BY id")
    before = _v(s, known_at="2026-10-18T15:30:00+05:30", valid_at="2026-10-18")
    s.run("""CREATE (d:Decision {id:'DEC-T', key:'DEC-T', subject_key:$k, predicate:'value', policy:'human', at:$t})
             WITH d MATCH (w:Claim {id:'CL-T'}) CREATE (d)-[:SELECTS]->(w), (d)-[:BASED_ON]->(w)
             WITH d MATCH (l:Claim) WHERE l.id IN $old
             CREATE (d)-[:BASED_ON]->(l) SET l.invalidated_at = $t, l.invalidated_by = 'DEC-T'""",
          {"k": PKG116, "t": t, "old": [o["id"] for o in old]})
    after = _v(s, as_of="2026-10-19")
    assert after["claim"]["claim"] == "CL-T" and after["decision_then"]["decision"] == "DEC-T"
    assert {c["claim"] for c in after["set_aside_by_then"]} == {o["id"] for o in old}   # superseded, not lost
    # the earlier knowledge time still answers what it answered before the decision existed
    again = _v(s, known_at="2026-10-18T15:30:00+05:30", valid_at="2026-10-18")
    assert again["claim"]["claim"] == before["claim"]["claim"]
    assert [d["decision"] for d in again["decisions_later"]] == ["DEC-T"]
    # reading history wrote nothing: the earlier decisions are exactly as they were
    assert s.read("MATCH (d:Decision) WHERE d.id <> 'DEC-T' RETURN d.id AS id, d.at AS at ORDER BY id") == before_decisions


def test_recorded_time_history_of_a_reactivated_claim(live):
    s, rec = live
    a, b = rec + 1000, rec + 2000
    s.run("MATCH (c:Claim {id:'CL-T'}) SET c.status_history = [$h]", {"h": f"rejected:{a}-{b}:DEC-X"})
    iso = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()  # noqa: E731
    alive = lambda ms: "CL-T" in {c["claim"] for c in _v(s, as_of=iso(ms))["alive_then"]}  # noqa: E731
    assert alive(a - 1) and not alive(a) and not alive(b - 1) and alive(b)
