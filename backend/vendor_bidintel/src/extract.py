"""Per-document-type extractors: unstructured text -> normalized records.

Each document type ships in more than one layout (BRIEFING.md s1): completion
certificates come as label/value tables *and* as two-paragraph prose with the
value mid-sentence. Every extractor therefore tries the tabular reading first
and falls back to a prose pattern, rather than assuming one template.

The join key is the *work title* ("Sewerage Network — Rajasthan Pkg-10"), folded
by norm_key. The certificate reference looks like a better key but is not: one
of the two company-record layouts prints only our own internal ref and carries
no client reference at all. The title is present in every view, is unique across
the 155 works, and survives the portfolio's small-caps rendering — which alters
letter case but not letters, so folding to lowercase recovers it exactly.
"""
from __future__ import annotations

import datetime as dt
import re

from money import best_money, parse_money

CERT_REF = re.compile(r"\bCC/\d+/\d+/\d+\b")
QUOTED = re.compile(r"[“\"]([^”\"]+)[”\"]")

_MONTHS = ("January February March April May June July August September "
           "October November December").split()


def norm_key(name: str) -> str:
    """Fold a work title to a join key: case, punctuation and dash style vary."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def parse_date(text: str) -> str | None:
    """Parse any date rendering used in the corpus to ISO yyyy-mm-dd."""
    if not text:
        return None
    text = text.strip()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return m.group(0)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)  # dd/mm/yyyy
    if m:
        d, mo, y = (int(g) for g in m.groups())
        return _iso(y, mo, d)
    m = re.search(r"\b([A-Z][a-z]{2,8})\s+(\d{1,2}),?\s+(\d{4})\b", text)  # May 19, 2019
    if m and (mo := _month(m.group(1))):
        return _iso(int(m.group(3)), mo, int(m.group(2)))
    m = re.search(r"\b(\d{1,2})\s+([A-Z][a-z]{2,8})\s+(\d{4})\b", text)  # 06 Feb 2011
    if m and (mo := _month(m.group(2))):
        return _iso(int(m.group(3)), mo, int(m.group(1)))
    return None


def _month(name: str) -> int | None:
    name = name.lower()
    for i, full in enumerate(_MONTHS, 1):
        if full.lower().startswith(name[:3]):
            return i
    return None


def _iso(y: int, m: int, d: int) -> str | None:
    try:
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return None


def days_between(a: str, b: str) -> int:
    """Absolute day count between two ISO dates."""
    return abs((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days)


def label_value(text: str, *labels: str) -> str | None:
    """Value of a label/value pair: `Label\\nValue` or `Label: Value`.

    The label must occupy the whole line or be followed by a separator. Allowing
    a bare prefix match makes the label "Work" fire on the line "Work Category"
    and return "Category" — which corrupts the record without erroring.

    Labels are tried in order, so callers list the most specific first.
    """
    lines = [ln.strip() for ln in text.replace("\f", "\n").split("\n")]
    for label in labels:
        alone = re.compile(rf"^{re.escape(label)}\s*[:·]?\s*$", re.IGNORECASE)
        inline = re.compile(rf"^{re.escape(label)}\s*[:·]\s*(\S.*)$", re.IGNORECASE)
        for i, line in enumerate(lines):
            if m := inline.match(line):
                return m.group(1).strip()
            if alone.match(line):
                for nxt in lines[i + 1:]:
                    if nxt:
                        return nxt
    return None


def first_line(text: str) -> str:
    for line in text.replace("\f", "\n").split("\n"):
        if line.strip():
            return line.strip()
    return ""


# --------------------------------------------------------------------------
# document types
# --------------------------------------------------------------------------

GRADES = ["Outstanding", "Excellent", "Very Good", "Good", "Satisfactory", "Fair", "Poor"]


def completion_certificate(text: str) -> dict:
    """Client's sign-off: value, dates, category, the client's written grading."""
    ref = re.search(r"(?<!NICL/)\bCC/\d+/\d+/\d+\b", text)
    work = label_value(text, "Name of Work", "Name of the Work", "Project Name", "Work")
    category = label_value(text, "Nature / Category", "Nature/Category", "Work Category", "Category")
    value_s = label_value(text, "Contract Value (Original)", "Contract Value",
                          "Executed Value", "Gross Executed Value", "Value of Work")
    completed = label_value(text, "Completion Date", "Date of Completion", "Completed On")
    pm = label_value(text, "Contractor's Project Manager", "Project Manager", "Project Lead")

    if not work:  # prose layout: work title quoted, category parenthesised after it
        if q := QUOTED.search(text):
            work = q.group(1).replace("\n", " ").strip()
            tail = text[q.end():q.end() + 80]
            if c := re.match(r"\s*\(([^)]+)\)", tail):
                category = category or c.group(1)
    if not value_s:
        # prose layout states the value mid-sentence, often twice:
        # "INR 19,32,99,999/- (Rupees 19.33 Crore Only)" — keep both, pick the exact one
        m = re.search(r"(?:gross executed value of|executed value of|value of|valued at)\s+(.{0,80})",
                      text, re.I | re.S)
        value_s = m.group(1) if m else None
    if not completed:
        m = re.search(r"completed[^.]{0,60}?on\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})", text)
        completed = m.group(1) if m else None
    if not pm:
        m = re.search(r"supervised[^.]{0,60}?by\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)", text)
        pm = m.group(1) if m else None

    return {
        "cert_ref": ref.group(0) if ref else None,
        "client": first_line(text),
        "work": work,
        "work_key": norm_key(work),
        "category": (category or "").strip().lower() or None,
        "value": best_money(value_s or ""),
        "completed": parse_date(completed or ""),
        "grading": _grading(text),
        "project_manager": pm,
    }


_GRADE_TRIGGER = re.compile(
    r"(?:graded|rated|assessed(?:\s+the\s+completed\s+work)?|"
    r"assessment\s+of\s+the\s+work\s+is|performance\s+is|quality[^.]{0,40}?\bis)"
    r"\s+(?:as\s+|at\s+)?", re.IGNORECASE)

_GRADES_RANKED = sorted(GRADES, key=len, reverse=True)  # "Very Good" before "Good"


def _grading(text: str) -> str | None:
    """The client's written assessment — prose only, no field anywhere (BRIEFING s1).

    Anchored on the trigger phrase, then matched longest-grade-first. A greedy
    two-word capture reads "graded Good. Workmanship..." as "Good Workmanship"
    and silently drops the grading.
    """
    for m in _GRADE_TRIGGER.finditer(text):
        tail = text[m.end():m.end() + 24].lstrip()
        for g in _GRADES_RANKED:
            if tail.lower().startswith(g.lower()):
                return g
    return None


def company_completion_certificate(text: str) -> dict:
    """Our own record of a work — the only source of the Project Lead.

    Ships in two layouts: a short label/value card, and a longer numbered form
    that prints only our internal ref (NICL/CC/...) and no client reference.
    """
    ref = label_value(text, "Client Certificate Ref")
    w = label_value(text, "Work", "Project Name")
    client = label_value(text, "Client")
    return {
        "cert_ref": m.group(0) if ref and (m := CERT_REF.search(ref)) else None,
        "work": w,
        "work_key": norm_key(w),
        "client": strip_client_suffix(client),
        "category": (label_value(text, "Category", "Work Category") or "").strip().lower() or None,
        "value": parse_money(label_value(text, "Executed Value", "Contract Value") or ""),
        "completed": parse_date(label_value(text, "Completion", "Completion Date") or ""),
        "project_lead": label_value(text, "Project Lead", "Project Manager"),
        "grading": _grading(text),
    }


def strip_client_suffix(client: str | None) -> str | None:
    """Drop the sector tag: "... Dept, Govt of Rajasthan (Government)"."""
    cleaned = re.sub(r"\s*\((government|private|psu|public)\)\s*$", "", client or "", flags=re.I)
    return cleaned.strip() or None


def reference_letter(text: str) -> dict:
    """Client testimonial. Only 132 of 155 works have one — the gap is queryable.

    Two layouts: a prose letter naming the work in quotes, and a tabular one
    with a Project Details block. The tabular form additionally states the
    contractor's role, which otherwise appears only in the portfolio.
    """
    work = label_value(text, "Project Name", "Name of Work")
    if not work and (q := QUOTED.search(text)):
        work = q.group(1)
    work = re.sub(r"\s+", " ", work).strip() if work else None
    role = label_value(text, "Contractor's Role", "Role")
    return {
        "client": first_line(text),
        "work": work,
        "work_key": norm_key(work),
        "role": "JV Partner" if role and "jv" in role.lower() else ("Prime" if role else None),
        "value": parse_money(label_value(text, "Contract Value") or ""),
    }


def personnel_certificate(text: str) -> dict:
    """PMP / Six Sigma credential: holder, type, credential id, issue date.

    Two layouts, as every type in this corpus has. The label/value one prints
    "Credential Type" and "Credential ID". The conferral one labels neither:
    the credential is a banner line directly above "This credential is conferred
    upon", the holder is the line below it, and the id sits under "Certificate
    No.". Reading only the labelled layout returned person, credential and
    credential_id all None for 20 of the 48 certificates — including the PMP two
    sample questions are built on — while still populating both dates, so the
    records looked present rather than empty.

    `credential_key` is the folded form, because the two layouts differ in case
    ("Six Sigma Black Belt" vs "SIX SIGMA BLACK BELT"). Filters compare on the
    key; `credential` keeps what the document actually says.
    """
    credential = label_value(text, "Credential Type") or _banner_credential(text)
    return {
        "person": (label_value(text, "This is to certify that")
                   or _conferred_on(text) or _holder(text)),
        "credential": credential,
        "credential_key": norm_key(credential) or None,
        "credential_id": label_value(text, "Credential ID", "Certificate No."),
        "employee_id": _first_field(label_value(text, "Employee ID")),
        "issued": parse_date(label_value(text, "Date of Issue", "Issued") or ""),
        "valid_through": parse_date(label_value(text, "Valid Through") or ""),
    }


def _holder(text: str) -> str | None:
    m = re.search(r"^(.+)\nCredential Holder\s*$", text, re.M)
    return m.group(1).strip() if m else None


_CONFERRED = "this credential is conferred upon"


def _conferred_on(text: str) -> str | None:
    m = re.search(rf"^{_CONFERRED}\s*\n(.+)$", text, re.M | re.I)
    return m.group(1).strip() if m else None


def _banner_credential(text: str) -> str | None:
    """The line immediately above the conferral sentence, anchored on that
    sentence rather than read at a fixed offset, so a taller masthead cannot
    shift it."""
    lines = [ln.strip() for ln in text.replace("\f", "\n").split("\n") if ln.strip()]
    for i, line in enumerate(lines):
        if i and line.lower().startswith(_CONFERRED):
            return lines[i - 1]
    return None


def _first_field(value: str | None) -> str | None:
    """"EMP-001 — National Infrastructure Corp. Ltd." -> "EMP-001".

    Inline label/value pairs sometimes run two fields onto one line separated by
    a bullet or dash, which PyMuPDF may render as U+FFFD when the glyph is
    unmapped."""
    if not value:
        return None
    return re.split(r"\s*[–—·•�|]\s*", value)[0].strip() or None


def portfolio_entries(text: str) -> list[dict]:
    """Split the 64-page portfolio's detail section into per-work entries.

    The detail section is the only place the Prime / JV Partner role appears.
    Work titles there are rendered in small caps and unreliable, so entries are
    keyed on the certificate reference printed in the Completed line.
    """
    entries = []
    for block in re.split(r"\n(?=\d{1,3}\.\s)", text.replace("\f", "\n")):
        head = re.match(r"\d{1,3}\.\s*(.+)", block)
        client = label_value(block, "Client")
        if not (head and client):
            continue
        ref = CERT_REF.search(block)
        role = "Prime"
        if m := re.search(r"\(([^)]*)\)\s*$", client.strip()):
            role = m.group(1).strip()
            client = client[: m.start()].strip()
        entries.append({
            "cert_ref": ref.group(0) if ref else None,
            "work_key": norm_key(head.group(1)),
            "client": strip_client_suffix(client),
            "role": "JV Partner" if "jv" in role.lower() else "Prime",
            "category": (label_value(block, "Category") or "").strip().lower() or None,
            "value": parse_money(label_value(block, "Executed Value") or ""),
            "completed": parse_date(label_value(block, "Completed") or ""),
        })
    return entries
