"""Planted-conflict events: the 'living knowledge' stream, with known labels.

The base corpus was generated with no deliberate conflicts (its gold answers
involve no judgement calls), so to exercise and *measure* the conflict engine we
generate the documents a real bid desk keeps receiving — corrigenda, re-issued
certificates, bank notices, HR corrections, JV partners' certificates — each
labelled with what a careful bid manager would conclude:

    expect.conflict  should a contradiction be detected at all?
    expect.route     auto (policy can settle it) | human (needs judgement) | none
    expect.winner    new | old  (which claim should the graph end up showing)

Hard negatives are included on purpose: rounding-only restatements, the same
value restated, and amendments to fields nothing depends on. A detector that
flags every changed string fails these.

Every event is applied as a Document + Claims through the same claim layer the
corpus uses, so the PDF path (render_pdf.py) and this JSON path converge.
"""
from __future__ import annotations

import datetime as dt
import json
import random

from bidbrain import config
from bidbrain.graph.client import Graph
from bidbrain.ingest import claims as C

GRADES = ["Satisfactory", "Good", "Very Good", "Excellent"]
JV_LEADS = ["Vardhaman Infraprojects Ltd.", "Konark Buildcon Pvt. Ltd.", "Sahyadri Engineering Ltd.",
            "Deccan Highways JV Lead", "Ganga Structures Ltd."]

TYPES = {
    "corrigendum_value": dict(conflict=True, route="auto", winner="new"),
    "revised_completion": dict(conflict=True, route="auto", winner="new"),
    "bond_invoked": dict(conflict=True, route="auto", winner="new"),
    "bond_extended": dict(conflict=True, route="auto", winner="new"),
    "hr_experience": dict(conflict=True, route="auto", winner="new"),
    "llm_grading": dict(conflict=True, route="auto", winner="old"),
    "jv_role_dispute": dict(conflict=True, route="human", winner=None),
    "letter_grading": dict(conflict=True, route="human", winner=None),
    # hard negatives
    "rounding_restatement": dict(conflict=False, route="none", winner=None),
    "consistent_restatement": dict(conflict=False, route="none", winner=None),
    "unrelated_field": dict(conflict=False, route="none", winner=None),
}


def _d(day_offset: int) -> str:
    return (dt.date(2026, 4, 1) + dt.timedelta(days=day_offset)).isoformat()


def build_events(g: Graph, per_type: int = 25, seed: int = 7) -> list[dict]:
    rnd = random.Random(seed)
    works = g.read("""MATCH (w:Work)-[:FOR_CLIENT]->(c:Client)
                      RETURN w.key AS key, w.title AS title, w.value AS value, w.completed AS completed,
                             w.role AS role, w.grading AS grading, c.name AS client ORDER BY w.key""")
    bonds = g.read("MATCH (b:Bond)-[:ISSUED_BY]->(k:Bank) RETURN b.key AS key, b.status AS status, "
                   "b.expiry AS expiry, b.issued AS issued, k.name AS bank ORDER BY b.key")
    people = g.read("MATCH (p:Person) RETURN p.key AS key, p.name AS name, p.experience_years AS exp ORDER BY p.key")
    grade_src = {r["k"]: r["kind"] for r in g.read(
        "MATCH (c:Claim {predicate:'grading'}) RETURN c.subject_key AS k, c.source_kind AS kind")}

    pools = {"value": rnd.sample(works, len(works)), "completed": rnd.sample(works, len(works)),
             "role": rnd.sample([w for w in works if w["role"] == "Prime"], k=sum(1 for w in works if w["role"] == "Prime")),
             "grading": rnd.sample([w for w in works if w["grading"]], k=sum(1 for w in works if w["grading"])),
             "misc": rnd.sample(works, len(works))}
    take = lambda pool, n: [pools[pool].pop() for _ in range(min(n, len(pools[pool])))]
    events: list[dict] = []
    n = 0

    def add(etype, doc_type, issuer, issued, subj_label, subj_key, pred, value, kind, narrative, title):
        nonlocal n
        n += 1
        doc_id = f"DOC-EV-{n:04d}"
        events.append({
            "event_id": f"EV-{n:04d}", "type": etype, "expect": TYPES[etype],
            "doc": {"doc_id": doc_id, "doc_type": doc_type, "issuer": issuer, "issued": issued, "title": title},
            "claims": [{"subject_label": subj_label, "subject_key": subj_key, "predicate": pred, "value": value,
                        "source_kind": kind, "issuer": issuer, "observed": issued, "page": 1}],
            "narrative": narrative})

    for w in take("value", per_type):
        new = int(w["value"] * rnd.choice([0.88, 0.91, 0.94, 1.06, 1.09, 1.12]) // 100000 * 100000)
        add("corrigendum_value", "corrigendum", w["client"], _d(rnd.randint(10, 180)), "Work", w["key"], "value",
            new, "client_corrigendum",
            f"{w['client']} issued a corrigendum revising the contract value of {w['title']} "
            f"from {w['value']:,} to {new:,}.", f"Corrigendum — {w['title']}")
    for w in take("completed", per_type):
        d0 = dt.date.fromisoformat(w["completed"])
        new = (d0 + dt.timedelta(days=rnd.choice([-120, -45, 30, 75, 160]))).isoformat()
        add("revised_completion", "revised_completion_certificate", w["client"], _d(rnd.randint(10, 180)), "Work",
            w["key"], "completed", new, "revised_certificate",
            f"{w['client']} re-issued the completion certificate for {w['title']} with completion date {new} "
            f"(was {w['completed']}).", f"Revised Completion Certificate — {w['title']}")
    with_status = [b for b in bonds if b["status"]]
    rnd.shuffle(with_status)
    for b in with_status[:per_type]:
        add("bond_invoked", "bank_notice", b["bank"], _d(rnd.randint(10, 180)), "Bond", b["key"], "status",
            "Invoked", "bank_notice", f"{b['bank']} notified invocation of performance bond {b['key']} (was {b['status']}).",
            f"Notice of Invocation — {b['key']}")
    others = [b for b in bonds if b not in with_status[:per_type]]
    rnd.shuffle(others)
    for b in others[:per_type]:
        new = (dt.date.fromisoformat(b["expiry"]) + dt.timedelta(days=rnd.choice([180, 365, 540]))).isoformat()
        add("bond_extended", "bank_notice", b["bank"], _d(rnd.randint(10, 180)), "Bond", b["key"], "expiry", new,
            "bank_notice", f"{b['bank']} extended the validity of performance bond {b['key']} to {new} "
                           f"(was {b['expiry']}).", f"Amendment to Bank Guarantee — {b['key']}")
    ppl = rnd.sample(people, len(people))
    for p in ppl[:per_type]:
        new = max(1, (p["exp"] or 10) - rnd.choice([2, 3, 4, 5, 6]))
        add("hr_experience", "hr_record", "company", _d(rnd.randint(10, 180)), "Person", p["key"],
            "experience_years", new, "company_record",
            f"HR verified {p['name']}'s experience as {new} years against service records (CV says {p['exp']}).",
            f"HR Service Verification — {p['name']}")
    graded = take("grading", per_type * 2)
    for w in graded[:per_type]:
        new = rnd.choice([x for x in GRADES if x != w["grading"]])
        add("llm_grading", "llm_extraction", "llm:qwen3-32b", _d(rnd.randint(10, 180)), "Work", w["key"], "grading",
            new, "llm_prose", f"An LLM reading the prose of {w['title']}'s documents proposed grading '{new}' "
                              f"(certificate says '{w['grading']}').", f"LLM extraction — {w['title']}")
    for w in graded[per_type:]:
        new = rnd.choice([x for x in GRADES if x != w["grading"]])
        add("letter_grading", "reference_letter", w["client"], _d(rnd.randint(10, 180)), "Work", w["key"], "grading",
            new, "reference_letter", f"A later letter from {w['client']} describes {w['title']} as '{new}' while the "
                                     f"{'certificate' if grade_src.get(w['key']) == 'client_certificate' else 'company record'}"
                                     f" says '{w['grading']}'.", f"Letter of Recommendation (supplementary) — {w['title']}")
    for w in take("role", per_type):
        lead = rnd.choice(JV_LEADS)
        add("jv_role_dispute", "partner_certificate", lead, _d(rnd.randint(10, 180)), "Work", w["key"], "role",
            "JV Partner", "partner_certificate",
            f"{lead}, as JV lead partner, certifies our participation in {w['title']} as 'JV Partner' "
            f"(our portfolio lists it as 'Prime').", f"JV Experience Certificate — {w['title']}")
    for w in take("value", per_type):
        jitter = rnd.choice([-40_000, -25_000, 15_000, 30_000, 45_000])
        add("rounding_restatement", "brochure", "company", _d(rnd.randint(10, 180)), "Work", w["key"], "value",
            w["value"] + jitter, "portfolio", f"Company brochure restates {w['title']} at {w['value'] + jitter:,} "
                                              f"(rounded to 0.01 Cr; certificate {w['value']:,}).",
            f"Company Brochure — {w['title']}")
    for w in take("value", per_type):
        add("consistent_restatement", "reference_letter", w["client"], _d(rnd.randint(10, 180)), "Work", w["key"],
            "value", w["value"], "reference_letter", f"A later letter from {w['client']} restates {w['title']} at "
                                                     f"the certified value {w['value']:,}.",
            f"Letter of Appreciation — {w['title']}")
    for w in take("misc", per_type):
        add("unrelated_field", "corrigendum", w["client"], _d(rnd.randint(10, 180)), "Work", w["key"],
            "defect_liability_days", rnd.choice([365, 540, 730]), "client_corrigendum",
            f"{w['client']} amended only the defect liability period of {w['title']}.",
            f"Corrigendum (DLP) — {w['title']}")
    return events


def apply_event(g: Graph, ev: dict, resolve: bool = True) -> dict:
    """Create the event's Document + Claims, then run the conflict engine on its subjects."""
    from bidbrain.conflicts import engine
    d = ev["doc"]
    g.run("""MERGE (doc:Document {doc_id:$id}) SET doc.key = $id, doc.doc_type = $t, doc.issuer = $iss,
               doc.issued = $issued, doc.title = $title, doc.pages = 1, doc.planted = $etype, doc.live = true""",
          {"id": d["doc_id"], "t": d["doc_type"], "iss": d["issuer"], "issued": d["issued"], "title": d["title"],
           "etype": ev["type"]})
    rows = []
    for c in ev["claims"]:
        if c["source_kind"] not in C.AUTHORITY:
            raise ValueError(f"unknown source kind {c['source_kind']}")
        rows.append(C.make(c["subject_label"], c["subject_key"], c["predicate"], c["value"],
                           source_doc=d["doc_id"], source_kind=c["source_kind"], page=c.get("page", 1),
                           issuer=c["issuer"], observed=c["observed"], planted=ev["type"],
                           extracted_by=c.get("extracted_by", "event"), basis=ev["event_id"]))
        if c.get("precision"):
            # the document's own rendering can be coarser than the source kind's default:
            # "INR 19.33 Cr" in a letter is exact only to 1 lakh
            rows[-1]["precision"] = max(rows[-1]["precision"] or 1, int(c["precision"]))
    for r in rows:
        g.run(f"""MATCH (s:{r['subject_label']} {{key:$k}}), (d:Document {{doc_id:$doc}})
                  MERGE (c:Claim {{id:$id}}) SET c += $r
                  MERGE (c)-[:ABOUT]->(s) MERGE (c)-[:FROM {{page:$page}}]->(d)""",
              {"k": r["subject_key"], "doc": d["doc_id"], "id": r["id"], "r": r, "page": r["page"]})
    if not resolve:
        return {"event_id": ev["event_id"], "claims": [r["id"] for r in rows]}
    out = engine.run(g, [c["subject_key"] for c in ev["claims"]])
    # a fact stated for the first time has no rival, so no conflict materializes it: adopt it
    # (kept apart from `decisions`, which are resolved contradictions — what the P/R eval scores)
    in_conflict = {c["id"] for x in out["decisions"] + out["cards"] for c in x["conflict"]["claims"]}
    out["first_claims"] = engine.adopt_uncontested(g, [r["id"] for r in rows], in_conflict)
    return {"event_id": ev["event_id"], "claims": [r["id"] for r in rows], **out}


def save(events: list[dict], path=None) -> None:
    path = path or (config.DATA / "conflicts" / "events.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(events, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    evs = build_events(Graph())
    save(evs)
    import collections
    print(len(evs), collections.Counter(e["type"] for e in evs))
