"""Live ingestion: a new PDF arrives -> claims -> conflicts -> decisions -> ripple.

    ingest_pdf(g, path) -> {doc, kind, subject, claims, decisions, cards, ...}

Text comes from the vendored layout-preserving extractor; known templates are
parsed by the vendored parsers (a re-issued completion certificate goes through
the unmodified `extract.completion_certificate`), new document kinds by small
label/value parsers built on the same helpers. The subject is resolved from the
document's own identifiers — certificate reference first, then title / package
number — so a corrigendum for Pkg-151 can never land on Pkg-142.
"""
from __future__ import annotations

import pathlib
import re
import sys

from bidbrain import config
from bidbrain.conflicts.events import apply_event
from bidbrain.graph.client import Graph

sys.path.insert(0, str(config.VENDOR / "src"))
import extract as X  # noqa: E402  (vendored)
import pdftext  # noqa: E402  (vendored)
from money import best_money  # noqa: E402  (vendored)

GRADE_RE = re.compile(r"\b(Outstanding|Excellent|Very Good|Good|Satisfactory|Fair|Poor)\b")


def _first_date(text: str) -> str | None:
    for lab in ("Date",):
        v = X.label_value(text, lab)
        if v and (d := X.parse_date(v)):
            return d
    for m in re.finditer(r"Date:\s*([^\n]+)", text):
        if d := X.parse_date(m.group(1)):
            return d
    for line in text.splitlines()[:8]:
        if d := X.parse_date(line):
            return d
    return None


def _resolve_work(g: Graph, title: str | None, cert_ref: str | None) -> tuple[str | None, str]:
    if cert_ref:
        k = g.scalar("MATCH (w:Work {cert_ref:$r}) RETURN w.key", {"r": cert_ref})
        if k:
            return k, f"certificate reference {cert_ref}"
    if title:
        k = g.scalar("MATCH (w:Work {key:$k}) RETURN w.key", {"k": X.norm_key(title)})
        if k:
            return k, "exact title"
        from bidbrain.tools.read import _resolve
        try:
            k, how = _resolve(g, title, "work")
            return k, f"title ({(how or {}).get('via', 'exact')})"
        except LookupError:
            return None, "unresolved"
    return None, "no identifier"


def classify(text: str) -> str:
    head = text[:600].upper()
    if "COMPLETION CERTIFICATE (REVISED)" in head:
        return "revised_certificate"
    if "CORRIGENDUM" in head:
        return "corrigendum"
    if "NOTICE OF INVOCATION" in head:
        return "bank_invocation"
    if "AMENDMENT TO BANK GUARANTEE" in head:
        return "bank_amendment"
    if "SERVICE VERIFICATION RECORD" in head:
        return "hr_record"
    if "EXPERIENCE CERTIFICATE (JOINT VENTURE)" in head:
        return "partner_certificate"
    if "LETTER OF RECOMMENDATION" in head or "LETTER OF APPRECIATION" in head:
        return "client_letter"
    if "COMPANY PROFILE" in head:
        return "brochure"
    return "unknown"


def parse(g: Graph, text: str, kind: str) -> dict:
    """-> {subject_label, subject_key, resolved_via, issuer, observed, claims:[(pred, value, source_kind)]}"""
    issuer = X.first_line(text)
    observed = _first_date(text)
    out = {"issuer": issuer, "observed": observed, "claims": []}
    if kind == "revised_certificate":
        r = X.completion_certificate(text)                      # vendored, unmodified
        key, how = _resolve_work(g, r["work"], r["cert_ref"])
        out.update(subject_label="Work", subject_key=key, resolved_via=how)
        out["claims"] = [("completed", r["completed"], "revised_certificate"),
                         ("value", r["value"], "revised_certificate")]
    elif kind == "corrigendum":
        title = X.label_value(text, "Name of Work")
        ref = X.label_value(text, "Certificate Reference")
        key, how = _resolve_work(g, title, ref)
        out.update(subject_label="Work", subject_key=key, resolved_via=how)
        field = (X.label_value(text, "Field Amended") or "").lower()
        amended = X.label_value(text, "Amended Entry") or ""
        if "value" in field:
            out["claims"] = [("value", best_money(amended), "client_corrigendum")]
        elif "completion" in field:
            out["claims"] = [("completed", X.parse_date(amended), "client_corrigendum")]
        elif "defect" in field:
            m = re.search(r"\d+", amended)
            out["claims"] = [("defect_liability_days", int(m.group()) if m else None, "client_corrigendum")]
    elif kind in ("bank_invocation", "bank_amendment"):
        bond = X.label_value(text, "Bond Reference")
        key = g.scalar("MATCH (b:Bond {key:$k}) RETURN b.key", {"k": (bond or "").strip()})
        out.update(subject_label="Bond", subject_key=key, resolved_via="bond reference")
        if kind == "bank_invocation":
            out["claims"] = [("status", "Invoked", "bank_notice")]
        else:
            out["claims"] = [("expiry", X.parse_date(X.label_value(text, "Revised Expiry") or ""), "bank_notice")]
    elif kind == "hr_record":
        name = X.label_value(text, "Employee")
        from bidbrain.ingest.aliases import norm
        key = g.scalar("MATCH (p:Person {key:$k}) RETURN p.key", {"k": norm(name or "")})
        out.update(subject_label="Person", subject_key=key, resolved_via="employee name", issuer="company")
        v = X.label_value(text, "Verified Experience (years)")
        out["claims"] = [("experience_years", int(re.search(r"\d+", v).group()) if v else None, "company_record")]
    elif kind == "partner_certificate":
        title = X.label_value(text, "Name of Work")
        key, how = _resolve_work(g, title, None)
        out.update(subject_label="Work", subject_key=key, resolved_via=how)
        role = X.label_value(text, "Role of M/s National Infrastructure Corp. Ltd.") or ""
        out["claims"] = [("role", "JV Partner" if "jv" in role.lower() else "Prime", "partner_certificate")]
    elif kind == "client_letter":
        q = X.QUOTED.search(text)
        title = re.sub(r"\s+", " ", q.group(1)).strip() if q else None
        key, how = _resolve_work(g, title, None)
        out.update(subject_label="Work", subject_key=key, resolved_via=how)
        # fold whitespace: a wrapped "Very\nGood" must still read as one grade
        body = " ".join((text[q.end():] if q else text).split())
        if m := re.search(r"assess its overall quality as\s+(Outstanding|Excellent|Very Good|Good|Satisfactory|Fair|Poor)", body):
            out["claims"].append(("grading", m.group(1), "reference_letter"))
        if m := re.search(r"valued at\s+(.{0,40})", body, re.S):
            out["claims"].append(("value", best_money(m.group(1)), "reference_letter"))
    elif kind == "brochure":
        title = X.label_value(text, "Name of Work")
        key, how = _resolve_work(g, title, None)
        out.update(subject_label="Work", subject_key=key, resolved_via=how, issuer="company")
        out["claims"] = [("value", best_money(X.label_value(text, "Contract Value") or ""), "portfolio")]
    out["claims"] = [c for c in out["claims"] if c[1] is not None]
    return out


def ingest_pdf(g: Graph, path: str | pathlib.Path, doc_id: str | None = None) -> dict:
    path = pathlib.Path(path)
    text = pdftext.extract_pdf(path)                              # vendored, unmodified
    kind = classify(text)
    doc_id = doc_id or path.stem
    if kind == "unknown":
        return {"doc_id": doc_id, "kind": kind, "error": "unrecognised document type", "text": text[:500]}
    p = parse(g, text, kind)
    if not p.get("subject_key"):
        return {"doc_id": doc_id, "kind": kind, "error": f"could not resolve subject ({p.get('resolved_via')})",
                "parsed": p}
    ev = {"event_id": doc_id, "type": f"pdf:{kind}",
          "doc": {"doc_id": doc_id, "doc_type": kind, "issuer": p["issuer"], "issued": p["observed"] or config.AS_OF,
                  "title": text.splitlines()[2] if len(text.splitlines()) > 2 else kind},
          "claims": [{"subject_label": p["subject_label"], "subject_key": p["subject_key"], "predicate": pred,
                      "value": val, "source_kind": sk, "issuer": p["issuer"], "observed": p["observed"] or config.AS_OF,
                      "page": 1, "extracted_by": "pdf"} for pred, val, sk in p["claims"]]}
    out = apply_event(g, ev)
    _add_chunk(g, doc_id, text)
    return {"doc_id": doc_id, "kind": kind, "subject": {"label": p["subject_label"], "key": p["subject_key"],
                                                        "resolved_via": p["resolved_via"]},
            "issuer": p["issuer"], "observed": p["observed"],
            "extracted": [{"predicate": a, "value": b, "source_kind": c} for a, b, c in p["claims"]], **out}


def _add_chunk(g: Graph, doc_id: str, text: str) -> None:
    from bidbrain.ingest.chunks import embed
    vec = embed([text[:1400]])[0].tolist()
    g.run("""MATCH (d:Document {doc_id:$d}) MERGE (c:Chunk {id:$id}) SET c.key = $id, c.doc_id = $d, c.page = 1,
               c.text = $t, c.ft = $ft, c.embedding = vecf32($v) MERGE (c)-[:PART_OF]->(d)""",
          {"d": doc_id, "id": f"{doc_id}#p1#0", "t": text[:1400], "ft": " ".join(text[:1400].split()), "v": vec})
