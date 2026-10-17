"""Build the `bidbrain_base` graph from the reconstructed facts.

    python -m bidbrain.ingest.load_graph [--graph NAME] [--no-chunks]

Deterministic and idempotent: the graph is dropped and rebuilt (~seconds), so
the HF Space rebuilds it at boot instead of shipping an RDB file.

Every entity node carries a uniform `key` property, which is what lets the
claim layer, the case graph and the tools address any subject the same way.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import time

from bidbrain import config
from bidbrain.graph.client import Graph, day
from bidbrain.ingest import aliases, claims as claims_mod
from bidbrain.ingest.aliases import norm
from bidbrain.ingest.pages import page_index

WB_DOC = {  # workbook filename -> doc_id in document_index.csv
    "Receivables_Ageing.xlsx": "DOC-XL-AGEING",
    "Plant_and_Machinery_Register.xlsx": "DOC-XL-ASSETS",
    "Trial_Balance_by_Year.xlsx": "DOC-XL-TB",
}

INDEXES = [
    ("Work", "key"), ("Client", "key"), ("Person", "key"), ("Credential", "key"),
    ("Document", "doc_id"), ("Category", "key"), ("State", "key"), ("Tender", "key"),
    ("Requirement", "key"), ("Bond", "key"), ("Bank", "key"), ("Contract", "key"),
    ("BOQItem", "key"), ("Measurement", "key"), ("Invoice", "key"), ("FiscalYear", "key"),
    ("FinancialStatement", "key"), ("AnnualReport", "key"), ("TBLine", "key"),
    ("LedgerAccount", "key"), ("Asset", "key"), ("Certification", "key"),
    ("BusinessUnit", "key"), ("Claim", "id"), ("Claim", "subject_key"), ("Chunk", "id"),
    ("Case", "id"), ("Clause", "id"), ("Finding", "id"), ("Decision", "id"),
    ("Bid", "key"), ("PolicyRule", "id"), ("AgentRun", "id"), ("Conversation", "id"),
]


def category_of(text: str | None, cats: list[str]) -> str | None:
    t = (text or "").lower().replace(" works", "")
    for c in sorted(cats, key=len, reverse=True):
        if c in t:
            return c
    return None


def state_of_title(title: str) -> str | None:
    m = re.search(r"—\s*(.+?)\s+Pkg-\d+", title or "")
    return m.group(1).strip() if m else None


def req_kind(text: str) -> str:
    t = text.lower()
    rules = [("iso", "certification"), ("certification", "certification"),
             ("management (", "certification"), ("occupational", "certification"),
             ("turnover", "turnover"), ("similar-works", "experience_years"),
             ("comparable project", "similar_works"), ("pmp", "key_personnel"),
             ("key technical staff", "staff_count"), ("site engineers", "staff_count"),
             ("plant & machinery", "plant"), ("equipment list", "plant"),
             ("earnest money", "bid_security"), ("emd", "bid_security"),
             ("years in business", "years_in_business"), ("blacklisted", "declaration"),
             ("gst", "registration"), ("pan card", "registration"), ("gem", "registration"),
             ("labour license", "registration"), ("safety plan", "plan"), ("quality plan", "plan")]
    return next((k for needle, k in rules if needle in t), "other")


OVERLAP_MIN_SHARED = 2       # OVERLAPS_CLIENTS: at least this many clients in common ...
OVERLAP_MIN_JACCARD = 0.25   # ... and they are at least this share of the two people's combined client lists
LEADS_MIN_SHARED = 2         # SHARES_LEADS: two clients served by at least this many of the same project leads

PROJECTIONS = {
    # every pair of people with >= 2 clients in common. 70 pairs over 31 people: one connected block, so label
    # propagation over it returns a single community of 31 (kept for reference; `communities` does not use it)
    "SHARES_CLIENTS": """MATCH (a:Person)-[:WORKED_FOR]->(c:Client)<-[:WORKED_FOR]-(b:Person) WHERE a.key < b.key
        WITH a, b, count(c) AS n WHERE n >= 2
        CREATE (a)-[:SHARES_CLIENTS {n:n}]->(b), (b)-[:SHARES_CLIENTS {n:n}]->(a)""",
    # the people-community projection: >= 2 shared clients AND Jaccard >= 0.25 over the two client lists, so a lead
    # with 7 clients is not tied to everyone who once shared one or two of them
    "OVERLAPS_CLIENTS": f"""MATCH (a:Person)-[:WORKED_FOR]->(c:Client)<-[:WORKED_FOR]-(b:Person) WHERE a.key < b.key
        WITH a, b, count(c) AS shared
        MATCH (a)-[:WORKED_FOR]->(ca:Client) WITH a, b, shared, count(ca) AS na
        MATCH (b)-[:WORKED_FOR]->(cb:Client) WITH a, b, shared, na, count(cb) AS nb
        WITH a, b, shared, toFloat(shared) / (na + nb - shared) AS jaccard
        WHERE shared >= {OVERLAP_MIN_SHARED} AND jaccard >= {OVERLAP_MIN_JACCARD}
        CREATE (a)-[:OVERLAPS_CLIENTS {{shared:shared, jaccard:jaccard}}]->(b),
               (b)-[:OVERLAPS_CLIENTS {{shared:shared, jaccard:jaccard}}]->(a)""",
    # the client-community projection: two clients whose works were led by >= 2 of the same people
    "SHARES_LEADS": f"""MATCH (a:Client)<-[:WORKED_FOR]-(p:Person)-[:WORKED_FOR]->(b:Client) WHERE a.key < b.key
        WITH a, b, count(p) AS leads WHERE leads >= {LEADS_MIN_SHARED}
        CREATE (a)-[:SHARES_LEADS {{leads:leads}}]->(b), (b)-[:SHARES_LEADS {{leads:leads}}]->(a)""",
}


def project(g: Graph) -> None:
    """(Re)build the analysis projections; called at load and after any write
    that changes who-worked-for-whom."""
    g.run("MATCH ()-[r:WORKED_FOR|ENGAGED|SHARES_CLIENTS|OVERLAPS_CLIENTS|SHARES_LEADS]->() DELETE r")
    g.run("""MATCH (p:Person)<-[:LED_BY]-(w:Work)-[:FOR_CLIENT]->(c:Client)
             WITH p, c, count(w) AS n, sum(w.value) AS v
             CREATE (p)-[:WORKED_FOR {works:n, value:v}]->(c), (c)-[:ENGAGED {works:n, value:v}]->(p)""")
    for q in PROJECTIONS.values():
        g.run(q)


def unwind(g: Graph, cypher: str, rows: list[dict], batch: int = 400) -> None:
    for i in range(0, len(rows), batch):
        g.run(cypher, {"rows": rows[i:i + batch]})


def load(graph_name: str = config.BASE_GRAPH, with_chunks: bool = True, quiet: bool = False) -> Graph:
    t0 = time.time()
    log = (lambda *a: None) if quiet else print
    f = json.loads(config.FACTS_JSON.read_text(encoding="utf-8"))
    pages = page_index()
    g = Graph(graph_name, timeout_ms=120_000)
    g.delete()
    g = Graph(graph_name, timeout_ms=120_000)
    for label, prop in INDEXES:
        g.run(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")

    cats = sorted({w["category"] for w in f["works"]})
    # ---- documents -------------------------------------------------------
    docs = []
    with open(config.DATASET / "document_index.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            docs.append({"doc_id": r["doc_id"], "doc_type": r["doc_type"], "filename": r["filename"],
                         "size_bytes": int(r["size_bytes"]), "pages": pages.n_pages(r["doc_id"])})
    unwind(g, "UNWIND $rows AS r CREATE (:Document {key:r.doc_id, doc_id:r.doc_id, doc_type:r.doc_type, "
              "filename:r.filename, size_bytes:r.size_bytes, pages:r.pages})", docs)
    # The CIN printed on every financial statement encodes the incorporation
    # year (U45201WB2005PLC904417 -> 2005): evidence for "years in business".
    cin_m = re.search(r"CIN ([A-Z]\d{5}[A-Z]{2}(\d{4})[A-Z]{3}\d{6})", pages.raw.get("DOC-FS-2019", ""))
    g.run("""CREATE (c:Company {key:'company', name:$n, cin:$cin, incorporated_year:$y, cin_doc:'DOC-FS-2019'})
             WITH c MATCH (d:Document {doc_id:'DOC-FS-2019'})
             CREATE (c)-[:EVIDENCED_BY {kind:'cin', page:1}]->(d)""",
          {"n": config.COMPANY, "cin": cin_m.group(1) if cin_m else None,
           "y": int(cin_m.group(2)) if cin_m else None})

    # ---- categories, states, clients -------------------------------------
    unwind(g, "UNWIND $rows AS r CREATE (:Category {key:r, name:r})", cats)
    client_names = sorted({w["client"] for w in f["works"]} | {i["client"] for i in f["invoices"]}
                          | {c["client"] for c in f["contracts"]})
    states = sorted({s for s in [state_of_title(w["work"]) for w in f["works"]] if s}
                    | {s for s in map(aliases.state_of_client, client_names) if s}
                    | {a["location"] for a in f["assets"] if a.get("location")})
    unwind(g, "UNWIND $rows AS r CREATE (:State {key:r, name:r})", states)
    clients = []
    for c in client_names:
        al = aliases.client_aliases(c)
        clients.append({"key": norm(c), "name": c, "aliases": al, "aliases_text": " | ".join(al),
                        "govt_level": aliases.govt_level(c), "state": aliases.state_of_client(c)})
    unwind(g, """UNWIND $rows AS r CREATE (c:Client {key:r.key, name:r.name, aliases:r.aliases,
                 aliases_text:r.aliases_text, govt_level:r.govt_level, govt_level_source:'name-rule',
                 state:r.state})
                 WITH c, r MATCH (s:State {key:r.state}) CREATE (c)-[:IN_STATE]->(s)""", clients)

    # ---- people, credentials, business units ------------------------------
    unwind(g, "UNWIND $rows AS r CREATE (:BusinessUnit {key:r.unit, name:r.unit, scale:r.scale, "
              "headcount:r.headcount})", f["business_units"])
    staff = [{**s, "key": norm(s["person"]), "joined_day": day(s.get("joined"))} for s in f["staff"]]
    unwind(g, """UNWIND $rows AS r MATCH (co:Company)
        CREATE (p:Person {key:r.key, name:r.person, employee_id:r.employee_id, designation:r.designation,
                experience_years:r.experience_years, qualification:r.qualification, joined:r.joined,
                joined_day:r.joined_day, wage_group:r.wage_group})
        CREATE (co)-[:EMPLOYS]->(p)
        WITH p, r MATCH (d:Document {doc_id:r.source_doc}) CREATE (p)-[:EVIDENCED_BY {kind:'cv', page:1}]->(d)
        WITH p, r MATCH (b:BusinessUnit {key:r.business_unit}) CREATE (p)-[:MEMBER_OF]->(b)""", staff)
    creds = []
    for c in f["personnel"]:
        kind = "Six Sigma Black Belt" if c["credential"].lower().startswith("six sigma") else c["credential"]
        creds.append({"key": c["credential_id"], "kind": kind, "kind_key": c["credential_key"],
                      "issued": c["issued"], "issued_day": day(c["issued"]),
                      "valid_through": c["valid_through"], "valid_through_day": day(c["valid_through"]),
                      "person": norm(c["person"]), "doc": c["doc_id"]})
    unwind(g, """UNWIND $rows AS r MATCH (p:Person {key:r.person})
        CREATE (c:Credential {key:r.key, id:r.key, kind:r.kind, kind_key:r.kind_key, issued:r.issued,
                issued_day:r.issued_day, valid_through:r.valid_through, valid_through_day:r.valid_through_day})
        CREATE (p)-[:HOLDS]->(c)
        WITH c, r MATCH (d:Document {doc_id:r.doc}) CREATE (c)-[:EVIDENCED_BY {kind:'personnel_certificate', page:1}]->(d)""",
           creds)

    # ---- works ----------------------------------------------------------
    works = []
    for w in f["works"]:
        cc, ccc = w["docs"].get("completion_certificate"), w["docs"].get("company_record")
        views = [v for v, ok in (("cc", cc), ("ccc", ccc), ("pf", w.get("value_portfolio") is not None)) if ok]
        works.append({
            "key": w["work_key"], "title": w["work"], "cert_ref": w["cert_ref"], "value": w["value"],
            "value_portfolio": w.get("value_portfolio"), "completed": w["completed"],
            "completed_day": day(w["completed"]), "completed_year": int(w["completed"][:4]) if w["completed"] else None,
            "category": w["category"], "grading": w.get("grading"), "role": w.get("role"),
            "state": state_of_title(w["work"]), "has_reference": w["has_reference"], "views": views,
            "client": norm(w["client"]), "lead": norm(w["project_lead"]) if w.get("project_lead") else None,
            "cc": cc, "cc_page": pages.find_money(cc, w["value"]) or 1,
            "ccc": ccc, "ccc_page": pages.find(ccc, w["project_lead"]) or 1,
            "pf_page": pages.find("DOC-PPP-001", w["work"], w["cert_ref"]),
            "refs": [{"doc": d, "page": pages.find(d, w["work"]) or 1} for d in w["reference_letters"]],
        })
    unwind(g, """UNWIND $rows AS r
        MATCH (cl:Client {key:r.client}), (cat:Category {key:r.category})
        CREATE (w:Work {key:r.key, title:r.title, cert_ref:r.cert_ref, value:r.value,
                value_portfolio:r.value_portfolio, completed:r.completed, completed_day:r.completed_day,
                completed_year:r.completed_year, category:r.category, grading:r.grading, role:r.role,
                state:r.state, has_reference:r.has_reference, views:r.views})
        CREATE (w)-[:FOR_CLIENT]->(cl), (w)-[:IN_CATEGORY]->(cat)
        WITH w, r OPTIONAL MATCH (s:State {key:r.state})
        FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END | CREATE (w)-[:IN_STATE]->(s))
        WITH w, r OPTIONAL MATCH (p:Person {key:r.lead})
        FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END | CREATE (w)-[:LED_BY]->(p))
        WITH w, r MATCH (d1:Document {doc_id:r.cc}), (d2:Document {doc_id:r.ccc}), (d3:Document {doc_id:'DOC-PPP-001'})
        CREATE (w)-[:EVIDENCED_BY {kind:'completion_certificate', page:r.cc_page}]->(d1),
               (w)-[:EVIDENCED_BY {kind:'company_record', page:r.ccc_page}]->(d2)
        FOREACH (_ IN CASE WHEN r.pf_page IS NULL THEN [] ELSE [1] END |
               CREATE (w)-[:EVIDENCED_BY {kind:'portfolio', page:r.pf_page}]->(d3))
        WITH w, r UNWIND r.refs AS ref MATCH (d:Document {doc_id:ref.doc})
        CREATE (w)-[:HAS_REFERENCE {page:ref.page}]->(d)""", works)

    # ---- tenders, requirements, bonds, certifications ----------------------
    unwind(g, """UNWIND $rows AS r MATCH (co:Company)
        CREATE (c:Certification {key:r.cert_no, cert_no:r.cert_no, standard:r.standard, body:r.body,
                initial_date:r.initial_date, valid_until:r.valid_until, valid_until_day:r.vday})
        CREATE (co)-[:CERTIFIED]->(c)
        WITH c, r MATCH (d:Document {doc_id:r.source_doc}) CREATE (c)-[:EVIDENCED_BY {kind:'iso_certificate', page:1}]->(d)""",
           [{**c, "vday": day(c["valid_until"])} for c in f["certifications"]])
    tender_cat: dict[str, str | None] = {}
    for b in f["bonds"]:
        tender_cat[b["tender_ref"]] = category_of(b.get("work_description"), cats)
    for r in f["compliance"]:
        if "comparable project" in r["requirement"].lower():
            tender_cat[r["tender_ref"]] = category_of(r["requirement"], cats) or tender_cat.get(r["tender_ref"])
        tender_cat.setdefault(r["tender_ref"], None)
    unwind(g, """UNWIND $rows AS r CREATE (t:Tender {key:r.ref, ref:r.ref, category:r.cat})
        WITH t, r MATCH (c:Category {key:r.cat}) CREATE (t)-[:IN_CATEGORY]->(c)""",
           [{"ref": k, "cat": v} for k, v in sorted(tender_cat.items())])
    reqs = []
    for i, r in enumerate(f["compliance"]):
        m = re.search(r"\b(ORG-\d+)\b", r.get("evidence") or "")
        reqs.append({"key": f"{r['matrix_id']}#{i}", "matrix_id": r["matrix_id"], "tender": r["tender_ref"],
                     "text": r["requirement"], "kind": req_kind(r["requirement"]),
                     "status": "MET" if r["status"] in ("MET", "Complied") else r["status"],
                     "status_raw": r["status"], "threshold_value": r.get("threshold_value"),
                     "threshold_unit": r.get("threshold_unit"), "evidence": r.get("evidence"),
                     "doc": r["source_doc"], "page": pages.find(r["source_doc"], r["requirement"]) or 1,
                     "cert": m.group(1) if m else None})
    unwind(g, """UNWIND $rows AS r MATCH (t:Tender {key:r.tender}), (d:Document {doc_id:r.doc})
        CREATE (q:Requirement {key:r.key, matrix_id:r.matrix_id, text:r.text, kind:r.kind, status:r.status,
                status_raw:r.status_raw, threshold_value:r.threshold_value, threshold_unit:r.threshold_unit,
                evidence:r.evidence})
        CREATE (t)-[:REQUIRES]->(q), (q)-[:EVIDENCED_BY {kind:'compliance_matrix', page:r.page}]->(d)
        WITH q, r MATCH (c:Certification {key:r.cert}) CREATE (q)-[:CITES_CERT]->(c)""", reqs)
    # When was each compliance claim made? The full matrices print "Bid Submission Date" and "Bid Value"; the
    # short checklists print only their own signature date. `compliance_check` re-tests each MET row on this date.
    tdates = []
    for doc in sorted({r["source_doc"]: r["tender_ref"] for r in f["compliance"]}.items()):
        t = pages.raw.get(doc[0], "")
        sub = re.search(r"Bid Submission Date\s*\n?\s*(\d{4}-\d{2}-\d{2})", t)
        val = re.search(r"Bid Value:?\s*\n?\s*INR\s*([\d.]+)\s*Cr", t)
        signed = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|"
                           r"December) (\d{1,2}), (\d{4})", t)
        signed_iso = (dt.datetime.strptime(signed.group(0), "%B %d, %Y").date().isoformat() if signed else None)
        tdates.append({"tender": doc[1], "doc": doc[0], "bid_value": int(round(float(val.group(1)) * 1e7)) if val else None,
                       "bid_date": sub.group(1) if sub else signed_iso,
                       "basis": "bid_submission_date" if sub else ("matrix_signature_date" if signed else None)})
    unwind(g, """UNWIND $rows AS r MATCH (t:Tender {key:r.tender})
                 SET t.bid_date = r.bid_date, t.bid_date_basis = r.basis, t.bid_value = r.bid_value,
                     t.matrix_doc = r.doc""", tdates)
    banks = sorted({b["bank"] for b in f["bonds"] if b.get("bank")})
    unwind(g, "UNWIND $rows AS r CREATE (:Bank {key:r, name:r})", banks)
    bonds = [{**b, "key": b["bond_no"], "issued_day": day(b.get("issued")), "expiry_day": day(b.get("expiry")),
              "category": category_of(b.get("work_description"), cats),
              "page": pages.find_money(b["source_doc"], b.get("amount")) or 1} for b in f["bonds"]]
    unwind(g, """UNWIND $rows AS r MATCH (t:Tender {key:r.tender_ref}), (bk:Bank {key:r.bank}), (d:Document {doc_id:r.source_doc})
        CREATE (b:Bond {key:r.key, bond_no:r.bond_no, amount:r.amount, issued:r.issued, issued_day:r.issued_day,
                expiry:r.expiry, expiry_day:r.expiry_day, status:r.status, work_description:r.work_description,
                category:r.category})
        CREATE (b)-[:SECURES]->(t), (b)-[:ISSUED_BY]->(bk), (b)-[:EVIDENCED_BY {kind:'performance_bond', page:r.page}]->(d)
        WITH b, r MATCH (c:Category {key:r.category}) CREATE (b)-[:FOR_CATEGORY]->(c)""", bonds)

    # ---- contracts, BOQ, measurements, invoices ---------------------------------
    unwind(g, """UNWIND $rows AS r MATCH (cl:Client {key:r.client}), (d:Document {doc_id:r.source_doc})
        CREATE (c:Contract {key:'C' + toString(r.contract), contract:r.contract, awarded_value:r.awarded_value,
                billed_value:r.billed_value, period_from:r.period_from, period_to:r.period_to,
                period_to_day:r.pto, n_ra_bills:r.n_ra_bills})
        CREATE (c)-[:FOR_CLIENT]->(cl), (c)-[:EVIDENCED_BY {kind:'final_ra_bill', page:1}]->(d)""",
           [{**c, "client": norm(c["client"]), "pto": day(c.get("period_to"))} for c in f["contracts"]])
    unwind(g, """UNWIND $rows AS r MATCH (c:Contract {key:r.ck})
        CREATE (b:BOQItem {key:r.key, contract:r.contract, item_no:r.item_no, description:r.description,
                unit:r.unit, quantity:r.quantity, rate:r.rate, amount:r.amount})
        CREATE (c)-[:HAS_ITEM]->(b)""",
           [{**b, "ck": f"C{b['contract']}", "key": f"C{b['contract']}-{b['item_no']}"} for b in f["boq_items"]])
    unwind(g, """UNWIND $rows AS r MATCH (b:BOQItem {key:r.bk})
        CREATE (m:Measurement {key:r.key, contract:r.contract, ra_no:r.ra_no, measured_on:r.measured_on,
                measured_day:r.mday, item_no:r.item_no, qty:r.qty, amount:r.amount})
        CREATE (m)-[:MEASURES]->(b)""",
           [{**m, "bk": f"C{m['contract']}-{m['item_no']}", "mday": day(m.get("measured_on")),
             "key": f"C{m['contract']}-RA{m['ra_no']}-{m['item_no']}-{i}"} for i, m in enumerate(f["measurements"])])
    unwind(g, """UNWIND $rows AS r MATCH (cl:Client {key:r.client}), (d:Document {doc_id:'DOC-XL-AGEING'})
        CREATE (i:Invoice {key:r.invoice_no, invoice_no:r.invoice_no, invoice_date:r.invoice_date,
                invoice_day:r.iday, year:r.year, invoiced:r.invoiced, status:r.status, received:r.received,
                outstanding:r.outstanding})
        CREATE (i)-[:BILLED_TO]->(cl), (i)-[:EVIDENCED_BY {kind:'ageing_workbook'}]->(d)""",
           [{**i, "client": norm(i["client"]), "iday": day(i.get("invoice_date")),
             "year": int(i["invoice_date"][:4]) if i.get("invoice_date") else None} for i in f["invoices"]])

    # ---- finance -----------------------------------------------------------
    fys = sorted({x["fy"] for blk in ("financials", "annual_reports", "trial_balance", "ledger_accounts")
                  for x in f[blk]} | {claims_mod.prev_fy(x["fy"]) for x in f["financials"]})
    unwind(g, "UNWIND $rows AS r CREATE (:FiscalYear {key:r.fy, fy:r.fy, start_year:r.y})",
           [{"fy": fy, "y": int(fy[:4])} for fy in fys])
    fin_rows = []
    own = {fs["fy"] for fs in f["financials"]}
    for fs in f["financials"]:
        fin_rows.append({"key": f"FS-{fs['fy']}", "fy": fs["fy"], "doc": fs["source_doc"], "own": True,
                         "props": {k: fs.get(k) for k in claims_mod.FIN_FIELDS}})
    for fs in f["financials"]:            # years seen only through a comparative column
        p = claims_mod.prev_fy(fs["fy"])
        if p not in own:
            own.add(p)
            fin_rows.append({"key": f"FS-{p}", "fy": p, "doc": fs["source_doc"], "own": False,
                             "props": {k: fs.get(f"{k}_previous") for k in claims_mod.FIN_FIELDS}})
    for r in fin_rows:
        g.run("""MATCH (y:FiscalYear {key:$fy}), (d:Document {doc_id:$doc})
                 CREATE (s:FinancialStatement {key:$key, fy:$fy, from_comparative_only: NOT $own})
                 SET s += $props
                 CREATE (s)-[:FOR_FY]->(y), (s)-[:EVIDENCED_BY {kind:'financial_statement', page:1}]->(d)""",
              {"fy": r["fy"], "doc": r["doc"], "key": r["key"], "own": r["own"],
               "props": {k: v for k, v in r["props"].items() if v is not None}})
    unwind(g, """UNWIND $rows AS r MATCH (y:FiscalYear {key:r.fy}), (d:Document {doc_id:r.source_doc})
        CREATE (a:AnnualReport {key:'AR-' + r.fy, fy:r.fy, gross_billings:r.gross_billings,
                net_revenue:r.net_revenue, profit:r.profit})
        CREATE (a)-[:FOR_FY]->(y), (a)-[:EVIDENCED_BY {kind:'annual_report', page:1}]->(d)""", f["annual_reports"])
    unwind(g, """UNWIND $rows AS r MATCH (y:FiscalYear {key:r.fy})
        CREATE (t:TBLine {key:r.fy + '|' + r.account, fy:r.fy, account:r.account, debit:r.debit,
                credit:r.credit, balance:r.balance})
        CREATE (t)-[:FOR_FY]->(y)""", f["trial_balance"])
    unwind(g, """UNWIND $rows AS r MATCH (y:FiscalYear {key:r.fy}), (d:Document {doc_id:r.source_doc})
        CREATE (l:LedgerAccount {key:r.fy + '|' + r.account_code, fy:r.fy, account_code:r.account_code,
                account_name:r.account_name, debit_total:r.debit_total, credit_total:r.credit_total,
                closing_balance:r.closing_balance})
        CREATE (l)-[:FOR_FY]->(y), (l)-[:EVIDENCED_BY {kind:'general_ledger_book'}]->(d)""", f["ledger_accounts"])
    unwind(g, """UNWIND $rows AS r MATCH (co:Company)
        CREATE (a:Asset {key:'A' + toString(r.asset_id), asset_id:r.asset_id, asset_type:r.asset_type, make:r.make,
                acquired:r.acquired, cost:r.cost, condition:r.condition, location:r.location,
                ownership:r.ownership, safety_certified:r.safety_certified})
        CREATE (co)-[:OWNS]->(a)
        WITH a, r MATCH (s:State {key:r.location}) CREATE (a)-[:LOCATED_IN]->(s)""", f["assets"])

    # ---- provenance of workbook rows ----------------------------------------
    # Assets, trial-balance lines, BOQ items and measurements come from spreadsheets. Their facts carry a
    # source filename that was never linked: `isolated_entities(scope='corpus')` (algo.WCC) found 404 entities
    # (210 assets, 194 TB lines) with no path to any document, and 22 documents nothing cited (8 workbooks,
    # 6 RA bills, 8 bank statements). The workbooks are linked here. Bank statements are not modelled, and the 6 RA
    # bills are for contracts #70, #76 and #77, which have no final-bill record, so they stay uncited (reported).
    by_file = {**{r["filename"].rsplit("/", 1)[-1]: r["doc_id"] for r in docs}, **WB_DOC}
    prov = [("Asset", "asset_register", [{"key": f"A{a['asset_id']}", "src": a.get("source_doc")} for a in f["assets"]]),
            ("TBLine", "trial_balance", [{"key": f"{t['fy']}|{t['account']}", "src": t.get("source_doc")}
                                         for t in f["trial_balance"]]),
            ("BOQItem", "boq_workbook", [{"key": f"C{b['contract']}-{b['item_no']}", "src": b.get("source_doc")}
                                         for b in f["boq_items"]]),
            ("Measurement", "boq_workbook", [{"key": f"C{m['contract']}-RA{m['ra_no']}-{m['item_no']}-{i}",
                                              "src": m.get("source_doc")} for i, m in enumerate(f["measurements"])])]
    for label, kind, rows in prov:
        rows = [{"key": r["key"], "doc": by_file[r["src"]]} for r in rows if r["src"] in by_file]
        unwind(g, f"""UNWIND $rows AS r MATCH (n:{label} {{key:r.key}}), (d:Document {{doc_id:r.doc}})
                      CREATE (n)-[:EVIDENCED_BY {{kind:'{kind}'}}]->(d)""", rows)
    # a running-account bill prints "Contract #76" and "RA 2": it evidences that contract's RA-2 measurements
    # (when the contract is on record; none of this corpus's RA bills is)
    ra = []
    for d in docs:
        if d["doc_type"] == "ra_bill":
            t = pages.raw.get(d["doc_id"], "")
            m_c, m_r = re.search(r"Contract #(\d+)", t), re.search(r"Running Account Bill\s*[—-]\s*RA (\d+)", t)
            if m_c and m_r:
                ra.append({"doc": d["doc_id"], "contract": int(m_c.group(1)), "ra_no": int(m_r.group(1))})
    unwind(g, """UNWIND $rows AS r MATCH (d:Document {doc_id:r.doc}), (c:Contract {contract:r.contract})
                 CREATE (c)-[:EVIDENCED_BY {kind:'ra_bill', ra_no:r.ra_no, page:1}]->(d)
                 WITH r, d MATCH (m:Measurement {contract:r.contract, ra_no:r.ra_no})
                 CREATE (m)-[:EVIDENCED_BY {kind:'ra_bill', page:1}]->(d)""", ra)

    # ---- claims ---------------------------------------------------------------
    cl = claims_mod.from_facts(f, pages)
    by_label: dict[str, list[dict]] = {}
    for c in cl:
        by_label.setdefault(c["subject_label"], []).append(c)
    for label, rows in by_label.items():
        unwind(g, f"""UNWIND $rows AS r MATCH (s:{label} {{key:r.subject_key}})
            CREATE (c:Claim) SET c = r
            CREATE (c)-[:ABOUT]->(s)
            WITH c, r MATCH (d:Document {{doc_id:r.source_doc}}) CREATE (c)-[:FROM {{page:r.page}}]->(d)""", rows)

    # ---- past bids (episodic memory) -----------------------------------------
    from bidbrain.ingest.dossiers import load_bids
    load_bids(g, pages)

    # ---- analysis projections ---------------------------------------------
    # Domain edges are directed (Work->Person, Work->Client), so in a directed
    # shortest-path sense nothing ever passes *through* a person and their
    # betweenness is 0. These projections make the people<->client structure
    # explicit, in both directions, with counts as weights — what the
    # centrality / component / community tools run on.
    project(g)

    # ---- search indexes ---------------------------------------------------
    g.run("CALL db.idx.fulltext.createNodeIndex('Work', 'title')")
    g.run("CALL db.idx.fulltext.createNodeIndex('Client', 'name', 'aliases_text')")
    g.run("CALL db.idx.fulltext.createNodeIndex('Person', 'name')")
    if with_chunks:
        from bidbrain.ingest.chunks import load_chunks
        load_chunks(g, pages)
    g.run("MERGE (m:Meta {key:'meta'}) SET m.version = 1, m.built_at = timestamp()")
    # Resolve the corpus's own disagreements (financial restatements) so the
    # base graph starts with a decision trail rather than silent overwrites.
    from bidbrain.conflicts import engine
    res = engine.run(g)
    log(f"conflicts: {len(res['decisions'])} auto-resolved, {len(res['cards'])} open cards, "
        f"{len(res['rounding'])} rounding-only differences")
    stats = g.read("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n ORDER BY n DESC")
    log(f"built {graph_name} in {time.time() - t0:.1f}s:",
        ", ".join(f"{r['label']}={r['n']}" for r in stats))
    log("edges:", g.scalar("MATCH ()-[e]->() RETURN count(e)"))
    return g


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", default=config.BASE_GRAPH)
    ap.add_argument("--no-chunks", action="store_true")
    a = ap.parse_args()
    load(a.graph, with_chunks=not a.no_chunks)
