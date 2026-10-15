"""Regressions for the conflict engine (review of Oct 16). Each runs on a GRAPH.COPY.

    cd backend && pytest -q tests/test_conflict_regressions.py
"""
from __future__ import annotations

import json

import pytest

from bidbrain import config
from bidbrain.conflicts import engine
from bidbrain.conflicts.events import apply_event
from bidbrain.graph.client import Graph


@pytest.fixture()
def scratch():
    g = Graph()
    assert g.exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    s = g.copy_to("pytest_conflict_regressions")
    yield s
    s.delete()


def _demo() -> dict:
    return {e["event_id"]: e for e in json.load(open(config.DATA / "conflicts" / "demo_events.json"))}


def _event(eid: str, key: str, pred: str, value, kind: str, issuer: str, observed: str, label: str = "Work") -> dict:
    return {"event_id": eid, "type": "test",
            "doc": {"doc_id": f"DOC-{eid}", "doc_type": kind, "issuer": issuer, "issued": observed, "title": eid},
            "claims": [{"subject_label": label, "subject_key": key, "predicate": pred, "value": value,
                        "source_kind": kind, "issuer": issuer, "observed": observed, "page": 1}]}


def _status(g: Graph, claim_id: str) -> str:
    return g.scalar("MATCH (c:Claim {id:$i}) RETURN c.status", {"i": claim_id})


def _works(g: Graph, n: int) -> list[dict]:
    return g.read("""MATCH (w:Work)-[:FOR_CLIENT]->(c:Client) WHERE w.role = 'Prime'
                     RETURN w.key AS key, w.value AS value, c.name AS client ORDER BY w.key LIMIT $n""", {"n": n})


def test_learned_rule_does_not_override_a_higher_authority_source(scratch):
    """Finding 1: a rule 'prefer reference_letter over client_certificate' must not fire on a work
    where the letter *agrees* with the certificate and the real dispute is with a corrigendum."""
    a, b = _works(scratch, 2)
    # work A: a client letter with a different value -> close authority (80 vs 90) -> a human decides
    ev = _event("T1-A", a["key"], "value", a["value"] + 30_000_000, "reference_letter", a["client"], "2026-05-01")
    out = apply_event(scratch, ev)
    assert out["cards"] and not out["decisions"]
    res = engine.resolve_card(scratch, out["cards"][0]["card_id"], out["claims"][0], "client letter is newer",
                              apply_to_similar=True)
    assert res["learned_rule"]
    # work B: a consistent restatement letter (no conflict), then the client's formal corrigendum
    ok = apply_event(scratch, _event("T1-B1", b["key"], "value", b["value"], "reference_letter", b["client"],
                                     "2026-05-02"))
    assert not ok["decisions"] and not ok["cards"]
    corr = apply_event(scratch, _event("T1-B2", b["key"], "value", 51_300_000, "client_corrigendum", b["client"],
                                       "2026-05-20"))
    assert len(corr["decisions"]) == 1
    d = corr["decisions"][0]
    assert d["policy"] != "learned_rule" and d["winner"] == corr["claims"][0]
    assert scratch.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": b["key"]}) == 51_300_000


def test_learned_rule_still_settles_the_next_conflict_of_its_shape(scratch):
    demo = _demo()
    first = apply_event(scratch, demo["DEMO-3"])
    res = engine.resolve_card(scratch, first["cards"][0]["card_id"], first["claims"][0], "JV lead",
                              apply_to_similar=True)
    # learned from the losing kind that disagreed (portfolio), never 'partner_certificate over itself'
    rules = scratch.read("MATCH (r:PolicyRule) RETURN r.prefer_kind AS p, r.over_kind AS o")
    assert rules == [{"p": "partner_certificate", "o": "portfolio"}] and res["learned_rule"]
    second = apply_event(scratch, demo["DEMO-4"])
    assert second["decisions"] and second["decisions"][0]["policy"] == "learned_rule"


def test_one_open_card_per_fact_and_stale_cards_cannot_be_resolved(scratch):
    """Finding 2: a second claim on a contested fact folds into the open card; any decision on
    the fact closes every other open card, and a closed card refuses to be resolved."""
    demo = _demo()
    first = apply_event(scratch, demo["DEMO-3"])
    key = demo["DEMO-3"]["claims"][0]["subject_key"]
    card = first["cards"][0]["card_id"]
    konark = apply_event(scratch, _event("T2-K", key, "role", "JV Partner", "partner_certificate",
                                         "Konark Buildcon Pvt. Ltd.", "2026-05-09"))
    assert konark["cards"] and konark["cards"][0]["card_id"] == card
    assert scratch.scalar("MATCH (k:ConflictCard {subject_key:$k, predicate:'role', status:'open'}) RETURN count(k)",
                          {"k": key}) == 1
    # now the client itself formally amends the role: decided by authority, the human card is stale
    client = scratch.scalar("MATCH (:Work {key:$k})-[:FOR_CLIENT]->(c) RETURN c.name", {"k": key})
    corr = apply_event(scratch, _event("T2-C", key, "role", "JV Partner", "client_corrigendum", client, "2026-05-20"))
    assert corr["decisions"] and not corr["cards"]
    assert scratch.scalar("MATCH (k:ConflictCard {id:$id}) RETURN k.status", {"id": card}) == "superseded"
    with pytest.raises(ValueError):
        engine.resolve_card(scratch, card, first["claims"][0], "too late")
    active = scratch.read("MATCH (c:Claim {subject_key:$k, predicate:'role'}) WHERE c.status IN ['active','contested'] "
                          "RETURN DISTINCT c.value AS v", {"k": key})
    assert active == [{"v": "JV Partner"}]
    assert scratch.scalar("MATCH (w:Work {key:$k}) RETURN w.role", {"k": key}) == "JV Partner"


def test_claims_agreeing_with_the_winner_are_not_rejected(scratch):
    """Finding 7: resolving for Konark 'JV Partner' keeps Vardhaman's identical claim active."""
    demo = _demo()
    first = apply_event(scratch, demo["DEMO-3"])
    key = demo["DEMO-3"]["claims"][0]["subject_key"]
    konark = apply_event(scratch, _event("T7-K", key, "role", "JV Partner", "partner_certificate",
                                         "Konark Buildcon Pvt. Ltd.", "2026-05-09"))
    res = engine.resolve_card(scratch, konark["cards"][0]["card_id"], konark["claims"][0], "Konark is the JV lead")
    assert _status(scratch, first["claims"][0]) == "active"
    assert first["claims"][0] in res["corroborating"]
    assert scratch.scalar("MATCH (a:Claim {id:$a})-[:CORROBORATES]->(:Claim {id:$w}) RETURN count(*)",
                          {"a": first["claims"][0], "w": konark["claims"][0]}) == 1
    portfolio = scratch.scalar("MATCH (c:Claim {subject_key:$k, predicate:'role', source_kind:'portfolio'}) "
                               "RETURN c.status", {"k": key})
    assert portfolio == "rejected"


def test_corroborating_source_does_not_block_authority(scratch):
    """Finding 6: a consistent client letter must not turn an authority win into a human card."""
    (w,) = _works(scratch, 1)
    apply_event(scratch, _event("T6-L", w["key"], "value", w["value"], "reference_letter", w["client"], "2026-05-01"))
    out = apply_event(scratch, _event("T6-P", w["key"], "value", w["value"] + 50_000_000, "portfolio", "company",
                                      "2026-05-03"))
    assert out["decisions"] and out["decisions"][0]["policy"] == "authority" and not out["cards"]
    assert scratch.scalar("MATCH (w:Work {key:$k}) RETURN w.value", {"k": w["key"]}) == w["value"]


def test_first_claim_on_a_fact_is_materialized(scratch):
    """Finding 8: an invocation notice for a bond with no status claim sets Bond.status."""
    bond = scratch.read("MATCH (b:Bond)-[:ISSUED_BY]->(k:Bank) WHERE NOT (:Claim {predicate:'status'})-[:ABOUT]->(b) "
                        "RETURN b.key AS key, k.name AS bank LIMIT 1")[0]
    out = apply_event(scratch, _event("T8", bond["key"], "status", "Invoked", "bank_notice", bond["bank"],
                                      "2026-05-15", label="Bond"))
    assert not out["decisions"] and not out["cards"]
    assert out["first_claims"] and out["first_claims"][0]["policy"] == "first_claim"
    assert scratch.scalar("MATCH (b:Bond {key:$k}) RETURN b.status", {"k": bond["key"]}) == "Invoked"


def test_rounded_pdf_money_is_not_a_contradiction():
    """Finding 3: '19.33 Cr' carries 1-lakh precision."""
    from bidbrain.conflicts.ingest_doc import _money
    assert _money("INR 19.33 Cr") == (193_300_000, 100_000)
    assert _money("Rs. 1,933.00 Lakh") == (193_300_000, 1_000)
    assert _money("INR 19,32,99,999/-") == (193_299_999, 1)
