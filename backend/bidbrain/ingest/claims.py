"""The claim layer: every contestable scalar, with who said it and where.

An entity property (`Work.value`) is only a *materialized view* of the claim
that currently wins. The claims themselves are nodes:

    (:Claim {predicate, value, source_doc, page, issuer, authority, ...})
        -[:ABOUT]->(subject)   -[:FROM]->(:Document)

so when a corrigendum arrives the old value is not overwritten — a new claim
is added, the two are linked SUPERSEDES / CONTRADICTS, and a Decision records
which one the property now shows and why.
"""
from __future__ import annotations

import hashlib
import json

from bidbrain import config
from bidbrain.graph.client import day

# Authority ladder — how much a source is trusted on a fact it states.
# Ordered by who is accountable for the number: the client who certified the
# work outranks our own records, which outrank marketing (the portfolio), which
# outranks what a person writes about themselves (a CV).
AUTHORITY = {
    "client_corrigendum": 95,      # client formally amends its own certificate
    "bank_instrument": 95,         # the bank's guarantee document
    "financial_statement": 95,     # audited statement, for its own year
    "revised_certificate": 92,     # client re-issues the certificate
    "client_certificate": 90,      # completion certificate issued by the client
    "certification_body": 90,      # PMP / Six Sigma / ISO issuer
    "reference_letter": 80,        # client's letter (prose)
    "final_bill": 75,              # final RA bill acknowledged by client
    "partner_certificate": 75,     # JV lead partner's experience certificate
    "bank_notice": 95,             # bank's notice amending / invoking a guarantee
    "company_record": 70,          # our completion record / HR record
    "portfolio": 60,               # past-performance portfolio (rounded to 0.01 Cr)
    "comparative_column": 50,      # previous-year column of next year's statement
    "cv": 40,                      # self-declared
    "llm_prose": 30,               # value read out of prose by an LLM
    "name_rule": 20,               # derived from a naming convention
}

# Precision a source writes money at. Two claims whose difference is within the
# coarser source's precision are *consistent*, not contradictory: the portfolio
# prints INR 19.33 Cr for a certificate's 193,299,999.
MONEY_PRECISION = {"portfolio": 100_000, "comparative_column": 100_000}


def claim_id(subject_key: str, predicate: str, source_doc: str, basis: str = "") -> str:
    h = hashlib.sha1(f"{subject_key}|{predicate}|{source_doc}|{basis}".encode()).hexdigest()
    return "CL-" + h[:10]


def make(subject_label: str, subject_key: str, predicate: str, value, *,
         source_doc: str, source_kind: str, page: int | None = None,
         issuer: str | None = None, valid_from: str | None = None,
         valid_to: str | None = None, observed: str | None = None,
         extracted_by: str = "extractor", basis: str = "", planted: str | None = None) -> dict:
    return {
        "id": claim_id(subject_key, predicate, source_doc, basis),
        "subject_label": subject_label,
        "subject_key": subject_key,
        "predicate": predicate,
        "value": value,
        "value_str": f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value),
        "source_doc": source_doc,
        "source_kind": source_kind,
        "authority": AUTHORITY[source_kind],
        "precision": MONEY_PRECISION.get(source_kind, 1),
        "issuer": issuer,
        "page": page,
        "valid_from_day": day(valid_from),
        "valid_to_day": day(valid_to),
        "observed_day": day(observed),
        "extracted_by": extracted_by,
        "status": "active",
        "planted": planted,          # label for the P/R eval; None for real corpus claims
    }


FIN_FIELDS = ["revenue_operations", "contract_revenue", "other_operating_revenue",
              "cost_of_materials", "subcontracting_labour", "employee_benefit_expense",
              "depreciation", "other_expenses", "total_expenses", "pbt", "tax", "pat",
              "paid_up_capital", "reserves", "long_term_borrowings", "trade_payables",
              "other_current_liabilities", "ppe", "intangible_assets", "inventories",
              "trade_receivables", "cash", "order_book"]


def prev_fy(fy: str) -> str:
    a = int(fy[:4]) - 1
    return f"{a}-{str(a + 1)[2:]}"


def from_facts(f: dict, pages) -> list[dict]:
    """All claims the base corpus makes. `pages` is a PageIndex."""
    out: list[dict] = []
    for w in f["works"]:
        k = w["work_key"]
        cc = w["docs"].get("completion_certificate")
        ccc = w["docs"].get("company_record")
        if cc:
            p = pages.find_money(cc, w["value"])
            out.append(make("Work", k, "value", w["value"], source_doc=cc, source_kind="client_certificate",
                            page=p, issuer=w["client"], observed="2026-03-31"))
            out.append(make("Work", k, "completed", w["completed"], source_doc=cc,
                            source_kind="client_certificate", page=pages.find_date(cc, w["completed"]),
                            issuer=w["client"], observed="2026-03-31"))
            if w.get("grading"):
                # The grading is read from the client's certificate when it says
                # so, otherwise from our own record restating the client's
                # assessment ("assessed the completed work as Excellent") — and
                # the claim is attributed to whichever document actually holds it.
                pg = pages.find(cc, w["grading"])
                src, kind = (cc, "client_certificate") if pg else (ccc, "company_record")
                # issuer = whoever issued the *document*: our own record quoting
                # the client's assessment is still a company statement.
                out.append(make("Work", k, "grading", w["grading"], source_doc=src, source_kind=kind,
                                page=pg or pages.find(ccc, w["grading"]),
                                issuer=w["client"] if src == cc else "company", observed="2026-03-31"))
        if ccc:
            out.append(make("Work", k, "client", w["client"], source_doc=ccc, source_kind="company_record",
                            page=pages.find(ccc, w["client"]), issuer="company"))
            if w.get("project_lead"):
                out.append(make("Work", k, "project_lead", w["project_lead"], source_doc=ccc,
                                source_kind="company_record", page=pages.find(ccc, w["project_lead"]),
                                issuer="company"))
        if w.get("value_portfolio") is not None:
            out.append(make("Work", k, "value", w["value_portfolio"], source_doc="DOC-PPP-001",
                            source_kind="portfolio", page=pages.find("DOC-PPP-001", w["work"], w["cert_ref"]),
                            issuer="company", basis="portfolio"))
        if w.get("role"):
            out.append(make("Work", k, "role", w["role"], source_doc="DOC-PPP-001", source_kind="portfolio",
                            page=pages.find("DOC-PPP-001", w["work"], w["cert_ref"]), issuer="company",
                            basis="portfolio-role"))
    for c in f["personnel"]:
        out.append(make("Credential", c["credential_id"], "valid_through", c["valid_through"],
                        source_doc=c["doc_id"], source_kind="certification_body", page=1,
                        issuer=c["credential"]))
        out.append(make("Credential", c["credential_id"], "issued", c["issued"],
                        source_doc=c["doc_id"], source_kind="certification_body", page=1,
                        issuer=c["credential"]))
    for s in f["staff"]:
        from bidbrain.ingest.aliases import norm
        if s.get("experience_years") is not None:
            out.append(make("Person", norm(s["person"]), "experience_years", s["experience_years"],
                            source_doc=s["source_doc"], source_kind="cv",
                            page=pages.find(s["source_doc"], "experience"), issuer=s["person"]))
    for b in f["bonds"]:
        for pred in ("amount", "expiry", "status"):
            if b.get(pred) is None:
                continue
            pg = (pages.find_money(b["source_doc"], b["amount"]) if pred == "amount"
                  else pages.find_date(b["source_doc"], b["expiry"]) if pred == "expiry"
                  else pages.find(b["source_doc"], b[pred]))
            out.append(make("Bond", b["bond_no"], pred, b[pred], source_doc=b["source_doc"],
                            source_kind="bank_instrument", page=pg, issuer=b["bank"], observed=b.get("issued")))
    for c in f["contracts"]:
        for pred in ("awarded_value", "billed_value"):
            if c.get(pred) is not None:
                out.append(make("Contract", f"C{c['contract']}", pred, c[pred], source_doc=c["source_doc"],
                                source_kind="final_bill", page=pages.find_money(c["source_doc"], c[pred]),
                                issuer=c["client"]))
    for fs in f["financials"]:
        fy = fs["fy"]
        signed = f"{int(fy[:4]) + 1}-09-30"   # statements are signed ~6 months after FY end (approximation)
        for fld in FIN_FIELDS:
            if fs.get(fld) is not None:
                out.append(make("FinancialStatement", f"FS-{fy}", fld, fs[fld], source_doc=fs["source_doc"],
                                source_kind="financial_statement",
                                page=pages.find_money(fs["source_doc"], fs[fld]), issuer="auditor", observed=signed))
            pv = fs.get(f"{fld}_previous")
            if pv is not None:
                out.append(make("FinancialStatement", f"FS-{prev_fy(fy)}", fld, pv, source_doc=fs["source_doc"],
                                source_kind="comparative_column",
                                page=pages.find_money(fs["source_doc"], pv), issuer="auditor",
                                basis="comparative", observed=signed))
    # LLM-read gradings (bidbrain/ingest/llm_grading.py), only those whose quote was verified in the document
    llm_path = config.DATA / "llm_gradings.json"
    if llm_path.exists():
        for r in json.loads(llm_path.read_text()):
            if r.get("grade") and r.get("verified_quote") and r.get("doc"):
                out.append(make("Work", r["work_key"], "grading", r["grade"], source_doc=r["doc"],
                                source_kind="llm_prose", page=r.get("page") or 1, issuer="llm:qwen3-32b",
                                extracted_by="llm:qwen3-32b", basis="llm-grading"))
    for c in f["certifications"]:
        out.append(make("Certification", c["cert_no"], "valid_until", c["valid_until"],
                        source_doc=c["source_doc"], source_kind="certification_body", page=1, issuer=c["body"]))
    return out
