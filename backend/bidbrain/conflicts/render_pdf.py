"""Render planted events as corpus-style PDFs (the 'drop a PDF' demo + PDF eval).

The layout mimics the corpus: issuer on the first line, an authority line, a
title, Ref/Date, then label/value rows drawn as separate text blocks on the same
baseline — which PyMuPDF's block extraction returns as "Label\\nValue", exactly
what the vendored `extract.label_value` parses. A revised completion certificate
uses the *same template as the originals*, so it goes through the unmodified
`extract.completion_certificate`.

Titles are deliberately written in varied forms ("Pkg 151", "(Package 151)") so
the PDF path also tests entity resolution, not just extraction.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import random

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from bidbrain import config
from bidbrain.ingest.pages import money_renderings

OUT = config.DATA / "conflicts" / "pdf"
W, H = A4


def _vary_title(title: str, rnd: random.Random) -> str:
    import re
    m = re.match(r"(.+?) — (.+?) Pkg-(\d+)$", title)
    if not m:
        return title
    name, state, n = m.groups()
    return rnd.choice([title, f"{name} — {state} Pkg {n}", f"{name}, {state} (Package {n})",
                       f"{name} - {state} Pkg-{n}"])


def _money(v: int, rnd: random.Random) -> str:
    cr, lakh, grouped = money_renderings(v)[0], money_renderings(v)[1], money_renderings(v)[4]
    return rnd.choice([f"INR {cr}", f"Rs. {lakh}", f"INR {grouped}/-"])


class Doc:
    def __init__(self, path: pathlib.Path):
        self.c = canvas.Canvas(str(path), pagesize=A4)
        self.y = H - 60

    def line(self, text: str, size: int = 10, bold: bool = False, x: int = 56, gap: int = 15):
        self.c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        self.c.drawString(x, self.y, text)
        self.y -= gap

    def row(self, label: str, value: str):
        self.c.setFont("Helvetica", 10)
        self.c.drawString(56, self.y, label)
        self.c.drawString(300, self.y, value)
        self.y -= 17

    def para(self, text: str, width: int = 95):
        import textwrap
        for ln in textwrap.wrap(text, width):
            self.line(ln, gap=13)
        self.y -= 6

    def footer(self, doc_id: str):
        self.c.setFont("Helvetica", 8)
        self.c.drawString(56, 40, doc_id)
        self.c.drawString(480, 40, "Page 1 of 1")
        self.c.save()


def render(ev: dict, g, rnd: random.Random | None = None) -> pathlib.Path:
    rnd = rnd or random.Random(hash(ev["event_id"]) & 0xFFFF)
    OUT.mkdir(parents=True, exist_ok=True)
    d, c = ev["doc"], ev["claims"][0]
    path = OUT / f"{d['doc_id']}.pdf"
    doc = Doc(path)
    issued = dt.date.fromisoformat(d["issued"])
    if c["subject_label"] == "Work":
        w = g.read("""MATCH (w:Work {key:$k})-[:FOR_CLIENT]->(cl:Client) OPTIONAL MATCH (w)-[:LED_BY]->(p:Person)
                      RETURN w.title AS title, w.cert_ref AS ref, w.value AS value, w.completed AS completed,
                             w.category AS category, w.grading AS grading, cl.name AS client, p.name AS lead""",
                   {"k": c["subject_key"]})[0]
        title = _vary_title(w["title"], rnd)
    t = ev["type"]
    if t == "revised_completion":
        doc.line(w["client"], 12, True)
        doc.line(f"Government of India / State Authority · {(w['title'].split('— ')[-1]).split(' Pkg')[0]} · IN")
        doc.line("Work Completion Certificate (Revised)", 13, True)
        doc.line(f"Ref: {w['ref']}")
        doc.line(f"Date: {d['issued']}")
        doc.line("1. Particulars of the Work", 11, True)
        doc.row("Name of Work", w["title"])
        doc.row("Nature / Category", w["category"].title())
        doc.row("Contract Value (Original)", _money(w["value"], rnd))
        doc.row("Completion Date", c["value"])
        doc.row("Contractor's Project Manager", w["lead"] or "")
        doc.para(f"This certificate supersedes the completion certificate issued earlier under the same "
                 f"reference. The date of completion has been revised on verification of the measurement "
                 f"book and the final inspection record.")
    elif t in ("corrigendum_value", "unrelated_field"):
        doc.line(w["client"], 12, True)
        doc.line("Office of the Executive Engineer")
        doc.line("CORRIGENDUM", 13, True)
        doc.line(f"Ref: {w['ref']}/CORR-{rnd.randint(1, 9)}")
        doc.line(f"Date: {issued.strftime('%d %b %Y')}")
        doc.row("Name of Work", title)
        doc.row("Certificate Reference", w["ref"])
        if t == "corrigendum_value":
            doc.row("Field Amended", "Contract Value (Original)")
            doc.row("Original Entry", _money(w["value"], rnd))
            doc.row("Amended Entry", _money(c["value"], rnd))
        else:
            doc.row("Field Amended", "Defect Liability Period")
            doc.row("Original Entry", "N/A days")
            doc.row("Amended Entry", f"{c['value']} days")
        doc.para("All other particulars of the completion certificate remain unchanged. This corrigendum "
                 "forms part of the certificate and shall be read with it.")
    elif t in ("bond_invoked", "bond_extended"):
        b = g.read("MATCH (b:Bond {key:$k})-[:SECURES]->(t:Tender) RETURN b.amount AS amount, b.expiry AS expiry, "
                   "t.key AS tender", {"k": c["subject_key"]})[0]
        doc.line(d["issuer"], 12, True)
        doc.line("Bank Guarantee Department")
        doc.line("NOTICE OF INVOCATION OF BANK GUARANTEE" if t == "bond_invoked"
                 else "AMENDMENT TO BANK GUARANTEE", 13, True)
        doc.line(f"Date: {issued.strftime('%d %B %Y')}")
        doc.row("Bond Reference", c["subject_key"])
        doc.row("Tender", b["tender"])
        doc.row("Guarantee Amount", _money(b["amount"], rnd))
        if t == "bond_invoked":
            doc.row("Status", "Invoked")
            doc.para("The beneficiary has invoked the above guarantee. The guaranteed amount stands claimed "
                     "and the instrument is no longer available as security.")
        else:
            doc.row("Original Expiry", b["expiry"])
            doc.row("Revised Expiry", c["value"])
            doc.para("All other terms of the guarantee remain unaltered.")
    elif t == "hr_experience":
        p = g.read("MATCH (p:Person {key:$k}) RETURN p.name AS name, p.employee_id AS eid, p.experience_years AS x",
                   {"k": c["subject_key"]})[0]
        doc.line("National Infrastructure Corp. Ltd.", 12, True)
        doc.line("Human Resources — Service Records")
        doc.line("SERVICE VERIFICATION RECORD", 13, True)
        doc.line(f"Date: {d['issued']}")
        doc.row("Employee", p["name"])
        doc.row("Employee ID", p["eid"] or "")
        doc.row("Experience stated in CV (years)", str(p["x"]))
        doc.row("Verified Experience (years)", str(c["value"]))
        doc.para("Verified against appointment letters, relieving letters and provident fund records.")
    elif t == "jv_role_dispute":
        doc.line(d["issuer"], 12, True)
        doc.line("Lead Partner, Joint Venture")
        doc.line("EXPERIENCE CERTIFICATE (JOINT VENTURE)", 13, True)
        doc.line(f"Date: {issued.strftime('%B %d, %Y')}")
        doc.row("Name of Work", title)
        doc.row("Client", w["client"])
        doc.row("Role of M/s National Infrastructure Corp. Ltd.", "JV Partner")
        doc.row("Share in JV (%)", str(rnd.choice([26, 30, 40, 49])))
        doc.para("Certified that the above firm participated in the execution of the work as a member of the "
                 "joint venture led by the undersigned.")
    elif t in ("letter_grading", "consistent_restatement"):
        doc.line(w["client"], 12, True)
        doc.line("Letter of Recommendation (Supplementary)" if t == "letter_grading" else "Letter of Appreciation",
                 13, True)
        doc.line(f"Our ref: S-{rnd.randint(100, 999)}")
        doc.line(d["issued"])
        if t == "letter_grading":
            doc.para(f"This office engaged M/s National Infrastructure Corp. Ltd. for the work “{title}”. "
                     f"On review of the completed work we assess its overall quality as {c['value']}.")
        else:
            doc.para(f"This office engaged M/s National Infrastructure Corp. Ltd. for the work “{title}” "
                     f"valued at {_money(c['value'], rnd)}, and records its appreciation.")
    elif t == "rounding_restatement":
        doc.line("National Infrastructure Corp. Ltd.", 12, True)
        doc.line("Company Profile — Selected Works", 13, True)
        doc.line(f"Date: {d['issued']}")
        doc.row("Name of Work", title)
        doc.row("Contract Value", f"INR {money_renderings(c['value'])[0]}")
        doc.row("Client", w["client"])
    elif t == "llm_grading":
        raise ValueError("llm_grading events are not documents")
    doc.footer(d["doc_id"])
    return path
