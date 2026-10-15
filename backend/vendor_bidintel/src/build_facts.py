"""Reconstruct the works fact table from the document estate.

No mapping from documents to entities is supplied (README, "What you are
deliberately NOT given"), so the table is rebuilt by joining three independent
views of each work on the client's certificate reference:

    completion_certificate          value, dates, category, grading, client
    company_completion_certificate  project lead, client, cert ref
    past_performance_portfolio      Prime / JV role

Reference letters carry no certificate reference and are matched on the folded
work title instead. A work with no matching letter is a genuine absence, which
is itself an answer (BRIEFING.md s5) — so unmatched letters are reported rather
than silently dropped.

Values are taken from the client's own certificate wherever present: the
README's derivations read each work's value "out of that project's own
certificate", and the portfolio rounds to two decimals of a crore.
"""
from __future__ import annotations

import collections
import json
import pathlib

import extract
import financials
import instruments
import pdftext
import people
import workbooks

REPO = pathlib.Path(__file__).resolve().parents[1]
FACTS = REPO / "results" / "raw" / "facts.json"

# Each module owns its blocks and is the only writer of them, so a block can be
# dropped by deleting one entry here (decision_log.md s7). Order is irrelevant:
# the blocks are namespaced and no module reads another's output.
SOURCES = (workbooks, people, financials, instruments)


def build(corpus: dict[str, str]) -> dict:
    # not `people` — that name is the module owning the staff/business_unit blocks
    certs, records, letters, certs_by_person = {}, {}, [], {}

    for doc_id, text in corpus.items():
        kind = pdftext.doc_type(doc_id)
        if kind == "completion_certificate":
            r = extract.completion_certificate(text)
            r["doc_id"] = doc_id
            certs[r["work_key"]] = r
        elif kind == "company_completion_certificate":
            r = extract.company_completion_certificate(text)
            r["doc_id"] = doc_id
            records[r["work_key"]] = r
        elif kind == "reference_letter":
            r = extract.reference_letter(text)
            r["doc_id"] = doc_id
            letters.append(r)
        elif kind == "personnel_certificate":
            r = extract.personnel_certificate(text)
            r["doc_id"] = doc_id
            certs_by_person[doc_id] = r

    portfolio = {}
    for doc_id, text in corpus.items():
        if pdftext.doc_type(doc_id) == "past_performance_portfolio":
            for e in extract.portfolio_entries(text):
                portfolio[e["work_key"]] = e

    letters_by_key = collections.defaultdict(list)
    for letter in letters:
        letters_by_key[letter["work_key"]].append(letter["doc_id"])

    works = []
    for key in sorted(set(certs) | set(records) | set(portfolio)):
        cert = certs.get(key, {})
        rec = records.get(key, {})
        pf = portfolio.get(key, {})
        works.append({
            "cert_ref": cert.get("cert_ref") or rec.get("cert_ref") or pf.get("cert_ref"),
            "work": cert.get("work") or rec.get("work"),
            "work_key": key,
            # client naming is consistent across the three views; prefer our own record
            "client": rec.get("client") or cert.get("client") or pf.get("client"),
            "category": rec.get("category") or cert.get("category") or pf.get("category"),
            # the client's own certificate is authoritative: the portfolio rounds
            # to two decimals of a crore, the certificate does not
            "value": cert.get("value") or rec.get("value") or pf.get("value"),
            "value_portfolio": pf.get("value"),
            "completed": cert.get("completed") or rec.get("completed") or pf.get("completed"),
            "grading": cert.get("grading") or rec.get("grading"),
            "role": pf.get("role"),
            "project_lead": rec.get("project_lead") or cert.get("project_manager"),
            "reference_letters": letters_by_key.get(key, []),
            "has_reference": bool(letters_by_key.get(key)),
            "docs": {k: v for k, v in
                     (("completion_certificate", cert.get("doc_id")),
                      ("company_record", rec.get("doc_id"))) if v},
        })

    matched = {d for k in (w["work_key"] for w in works) for d in letters_by_key.get(k, [])}
    facts = {
        "works": works,
        "personnel": list(certs_by_person.values()),
        "unmatched_reference_letters": [l["doc_id"] for l in letters if l["doc_id"] not in matched],
    }

    # The other fact sources. A module that raises must not take the works table
    # down with it: works answer the 13 known shapes, and a missing block costs
    # only the shapes that need it.
    for module in SOURCES:
        name = module.__name__
        try:
            blocks = module.build(corpus)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed silently
            print(f"  !! {name}.build failed: {type(exc).__name__}: {exc}")
            continue
        for key, records in blocks.items():
            if key in facts:
                raise KeyError(f"{name} would overwrite the '{key}' block")
            facts[key] = records
    return facts


def audit(facts: dict) -> None:
    works, people = facts["works"], facts["personnel"]
    print(f"works reconstructed: {len(works)}   (expected 155)")
    print(f"personnel certificates: {len(people)}   (expected 48)")
    print(f"works with a reference letter: {sum(w['has_reference'] for w in works)}   (expected 132)")
    print(f"unmatched reference letters: {len(facts['unmatched_reference_letters'])}   (expected 0)")

    print("\nfield coverage")
    fields = ["work", "client", "category", "value", "completed", "grading", "role", "project_lead"]
    for f in fields:
        n = sum(1 for w in works if w.get(f) not in (None, ""))
        flag = "" if n == len(works) else "   <-- INCOMPLETE"
        print(f"  {f:16s} {n:3d}/{len(works)}{flag}")

    disagree = [w for w in works if w["value_portfolio"] and w["value"]
                and abs(w["value"] - w["value_portfolio"]) > 0]
    print(f"\ncertificate vs portfolio value: {len(disagree)} of {len(works)} differ")
    for w in disagree[:5]:
        d = w["value"] - w["value_portfolio"]
        print(f"  {w['cert_ref']}  cert={w['value']:,}  portfolio={w['value_portfolio']:,}  diff={d:+,}")

    print(f"\ndistinct clients: {len({w['client'] for w in works if w['client']})}   (expected 62)")
    grades = collections.Counter(w["grading"] for w in works)
    print(f"gradings: {dict(grades)}")

    # Each module audits the blocks it owns; the merged dict is a superset, and
    # a module only ever reads its own keys out of it.
    for module in SOURCES:
        print(f"\n--- {module.__name__}")
        try:
            module.audit(facts)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! audit failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    facts = build(pdftext.load_corpus())
    FACTS.parent.mkdir(parents=True, exist_ok=True)
    FACTS.write_text(json.dumps(facts, ensure_ascii=False, indent=1), encoding="utf-8")
    audit(facts)
    print(f"\nwritten -> {FACTS}")
