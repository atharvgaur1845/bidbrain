"""Bank guarantees, certifications, tender compliance, contract headers, ledgers.

105 PDFs that no other module reads: 60 `performance_bond`, 40 `compliance_matrix`,
5 `iso_certificate`, plus the 6 `final_ra_bill` headers and the 8 `general_ledger_book`
volumes. Field names and types follow `docs/fact_schema.md`, which is frozen.

Three things about this corpus drove the shape of the code:

* **The compliance matrices are read from PDF geometry, not from the text cache.**
  Every other type here is label/value prose and parses fine from
  `pdftext.load_corpus()`. The matrices are four-column tables whose cells wrap, and
  `get_text("blocks")` interleaves the wrapped fragments by vertical position:

      1 / Quality management certification (ISO / Complied / Certificate ORG-1001 / 9001:2015)

  The requirement's second line lands *after* the evidence, so no reading of the
  flattened text can tell a requirement continuation from an evidence continuation.
  That is not a cosmetic loss -- the tender gate values live in exactly those
  continuations ("Minimum average turnover requirement" / "(Rs. 150 Cr)"). Word
  x-positions recover the columns exactly, so this module opens those 40 files
  itself. Precedent: `workbooks.py` likewise ignores `corpus` for the .xlsx files.

* **Column boundaries are read off each document's own header row**, never
  hardcoded: the two layouts put Status at x=292 and x=257 respectively, and a
  constant would silently mis-assign one of them.

* **`ledger_accounts` is a cross-check, not a source** (`decision_log.md` s7). Only
  the per-account TOTALS line is extracted -- never a posting line. The 194 rows of
  `Trial_Balance_by_Year.xlsx` are reproduced exactly by these totals, and
  `audit()` says so every run, which is the point: it makes the reconciliation
  that `workbooks.py` did once reproducible from a fact block.

`bank_statement` is deliberately not ingested -- see `SKIPPED_BANK_STATEMENTS`.
"""
from __future__ import annotations

import collections
import json
import pathlib
import re
import sys

import pymupdf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import extract  # noqa: E402
import money  # noqa: E402
import pdftext  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
DOCS = REPO / "dataset" / "documents"
OUT = REPO / "results" / "raw" / "instruments.json"
WORKBOOKS_JSON = REPO / "results" / "raw" / "workbooks.json"

BLOCKS = ("contracts", "bonds", "certifications", "compliance", "ledger_accounts")

# The corpus's own "as at" date: every bond, matrix and final bill is signed
# "31 March 2026", and the last full ledger volume closes on it. Liveness
# questions are answered against a caller-supplied date; this is the default.
REFERENCE_DATE = "2026-03-31"

# The bundle separates inline fields with three different glyphs -- U+2014 in the
# bonds, U+2013 in the final bills, U+00B7 in the contract header line -- and
# PyMuPDF yields U+FFFD where a glyph is unmapped. All four are accepted. A plain
# ASCII hyphen is deliberately *not*: the field to the left of the separator is a
# client name, and a hyphenated client would be truncated at its own hyphen.
DASH = "[·‒-―�]"

# A monetary gate must carry a currency token. Without that guard the bare-number
# branch of money._MONEY reads "ISO 9001:2015" as Rs 9,001.
_MONEY_GATE = re.compile(
    r"(?:INR|Rs\.?|₹)\s*\d[\d,]*(?:\.\d+)?\s*(?:Cr(?:ore)?s?|Lakhs?|Lacs?)?",
    re.IGNORECASE)
_YEARS_GATE = re.compile(r"(\d+)\s*(?:yrs?|years?)\b", re.IGNORECASE)
_COUNT_GATE = re.compile(r"(?:minimum\s+(\d+)|(\d+)\s*minimum)", re.IGNORECASE)

# Counters filled by build(), printed by audit(). Module level because the audit
# contract takes only the blocks.
STATS: dict[str, object] = {}

SKIPPED_BANK_STATEMENTS = """\
The 8 `bank_statement` PDFs are not ingested. Each is a projection of general
ledger account 1010 BANK: same dates, same narrations, same amounts, same row
count. The one figure they carry that the ledger does not is the *cumulative*
closing balance -- the ledger's balance column restarts at zero each fiscal year
-- and that is the running sum of the ledgers' yearly 1010 net, exactly, for all
8 years (see `cumulative_bank_balance` and its test). A second parser over the
same numbers would add extraction risk and no fact."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _clean(text: str | None) -> str | None:
    """Collapse the whitespace a wrapped PDF line leaves behind."""
    if text is None:
        return None
    s = re.sub(r"\s+", " ", str(text)).strip()
    return s or None


def _int(text: str | None) -> int | None:
    m = re.search(r"-?\d[\d,]*", text or "")
    return int(m.group(0).replace(",", "")) if m else None


# An amount must carry a currency token to count as money. Without that guard,
# "Approved Variations  INR 48.88 Cr (3 amendment(s))" parses as Rs 3 -- the bare
# integer is the finer rendering, so best_money prefers it.
_STATED_AMOUNT = re.compile(
    r"(?:INR|Rs\.?|₹|Rupees)\s*\d[\d,]*(?:\.\d+)?\s*(?:Cr(?:ore)?s?|Lakhs?|Lacs?)?",
    re.IGNORECASE)


def _money_in(text: str | None) -> int | None:
    """Exact integer rupees from a fragment that may state the value twice.

    Currency-tagged renderings only; the finest-grained one wins, so
    "INR 19,32,99,999/- (Rupees 19.33 Crore Only)" keeps the exact integer.
    """
    tagged = " ".join(m.group(0) for m in _STATED_AMOUNT.finditer(text or ""))
    return money.best_money(tagged) if tagged else None


def _fy(text: str) -> str | None:
    """'FISCAL YEAR 2019-20' / 'FY 2019-20' -> '2019-20' per the frozen convention."""
    m = re.search(r"(\d{4})\s*[‒-―�/-]\s*(\d{2}|\d{4})", text or "")
    return f"{m.group(1)}-{m.group(2)[-2:]}" if m else None


_PATHS: dict[str, pathlib.Path] = {}


def _pdf_path(doc_id: str) -> pathlib.Path:
    """Locate a document on disk. Indexed once; the bundle nests by type."""
    if not _PATHS:
        for p in DOCS.rglob("*.pdf"):
            _PATHS[p.stem] = p
    if doc_id not in _PATHS:
        raise FileNotFoundError(f"no PDF on disk for {doc_id}")
    return _PATHS[doc_id]


# --------------------------------------------------------------------------
# bonds
# --------------------------------------------------------------------------
#
# Two layouts, split by which one names the instrument:
#
#   A (28 docs) "PERFORMANCE BANK GUARANTEE" -- 3 pages, numbered terms, a Status
#       cell on page 2, and the amount stated twice (offer clause + Liability
#       clause). Case varies between documents; the split tests structure, not case.
#   B (32 docs) "BG No: BG/6" -- a one-page letter with no status and no
#       liability restatement.

_BOND_A = re.compile(r"^\s*Bond No:", re.IGNORECASE | re.MULTILINE)
_BOND_B = re.compile(r"^\s*BG No:\s*(\S+)\s*\n\s*Date:\s*([^\n]+)", re.IGNORECASE | re.MULTILINE)


def bond_layout(text: str) -> str:
    return "A" if _BOND_A.search(text) else ("B" if _BOND_B.search(text) else "unknown")


def performance_bond(text: str) -> dict:
    """One bank guarantee. `amount` is what the document states, including zero.

    32 of the 60 print "Rs. 0 (Rupees 0 Only)". That is a stated figure, not a
    failed parse, so it is kept as 0 rather than folded to None -- the schema's
    "never 0" rule is about absent values, and absence here would be a different
    claim from the one the document makes. `audit()` reports the split so the
    zeros can never be mistaken for a parser fault.
    """
    layout = bond_layout(text)
    tender = re.search(r"\bRFP-\d+", text)
    if layout == "A":
        bond_no = _clean(extract.label_value(text, "Bond No", "Bond Reference"))
        issued = extract.parse_date(extract.label_value(text, "Issue Date") or "")
        expiry = re.search(r"until\s+(.{0,30}?)\s*\(the", text, re.S)
        work = re.search(r"for the work of\s+(.{0,120}?),\s*and\b", text, re.S)
        # Stated twice; best_money over both keeps the finer rendering.
        offer = re.search(r"not\s+exceeding\s+(.{0,50}?)\s*\(Rupees", text, re.S)
        liability = re.search(r"shall not exceed\s+(.{0,50}?)\s*\(India", text, re.S)
        amount = _money_in(" ".join(m.group(1) for m in (offer, liability) if m))
        status = _clean(extract.label_value(text, "Status"))
    else:
        head = _BOND_B.search(text)
        bond_no = _clean(head.group(1)) if head else None
        issued = extract.parse_date(head.group(2)) if head else None
        expiry = re.search(r"in force up to and including\s+(.{0,30}?),?\s*after which", text, re.S)
        work = re.search(rf"Subject:\s*Performance Bond\s*{DASH}\s*(.{{0,120}}?)\s*\(Tender", text, re.S)
        offer = re.search(r"not\s+exceeding\s+(.{0,50}?)\s*\(Rupees", text, re.S)
        amount = _money_in(offer.group(1)) if offer else None
        status = None

    return {
        "bond_no": bond_no,
        "bank": _clean(extract.first_line(text)),
        "amount": amount,
        "issued": issued,
        "expiry": extract.parse_date(expiry.group(1)) if expiry else None,
        "status": status,
        "tender_ref": tender.group(0) if tender else None,
        "work_description": _clean(work.group(1)) if work else None,
    }


def is_live(bond: dict, as_of: str = REFERENCE_DATE) -> bool | None:
    """Is this guarantee still on foot on `as_of`? None when undecidable.

    `status` is printed by only one of the two layouts, so expiry is the field
    that generalises: a guarantee "remains valid ... until <date>" and stands
    discharged after it. Where a status *is* printed it wins, because a released
    guarantee is off the books whatever its expiry says.
    """
    if bond.get("status"):
        return bond["status"].strip().lower() not in _CLOSED_STATUSES
    if not bond.get("expiry"):
        return None
    return bond["expiry"] >= as_of


# Enumerated from the corpus, not assumed: audit() raises if a new value appears.
_CLOSED_STATUSES = {"released", "expired", "cancelled", "closed", "discharged"}
_OPEN_STATUSES = {"active", "live", "invoked"}


# --------------------------------------------------------------------------
# certifications
# --------------------------------------------------------------------------

def iso_certificate(text: str) -> dict:
    """A certificate of registration. Two of the five are not ISO at all --
    ORG-1004 is a CPWD Class I registration and ORG-1005 a NABL accreditation --
    so `standard` is read from the document rather than assumed from the type
    name. It is the line the conformity sentence points at."""
    standard = re.search(r"requirements of\s*\n\s*([^\n]+)", text, re.IGNORECASE)
    return {
        "cert_no": _clean(extract.label_value(text, "Certificate No")),
        "standard": _clean(standard.group(1)) if standard else None,
        "body": _clean(extract.label_value(text, "Certification Body")),
        "initial_date": extract.parse_date(
            extract.label_value(text, "Initial Certification Date") or ""),
        "valid_until": extract.parse_date(extract.label_value(text, "Valid Until") or ""),
    }


# --------------------------------------------------------------------------
# compliance matrices  (geometry -- see the module docstring)
# --------------------------------------------------------------------------

_HEADER = re.compile(r"^(?:Sr\.|#)\s+Requirement\s+Status\s+", re.IGNORECASE)
_COLUMN_WORDS = {"sr.", "#", "requirement", "status", "evidence", "supporting", "remarks"}
_FOOTER = re.compile(r"^DOC-[A-Z]+-\d+(\s+Page\s+\d+\s+of\s+\d+)?$", re.IGNORECASE)
_ROW_NO = re.compile(r"^\d{1,2}$")

_COL_TOL = 3.0   # pt; a cell's words all start within this of the column's x
_N_COLS = 4


def _visual_lines(path: pathlib.Path):
    """Words grouped into visual lines, each sorted left to right.

    Rows are bucketed on y/3 rather than exact y: a line's words share a baseline
    to well under a point, and consecutive table lines are ~6pt apart, so the
    bucket cannot merge two rows or split one.
    """
    with pymupdf.open(path) as doc:
        for page in doc:
            lines: dict[int, list] = {}
            for word in page.get_text("words"):
                lines.setdefault(round(word[1] / 3), []).append(word)
            for y in sorted(lines):
                yield sorted(lines[y], key=lambda w: w[0])


def compliance_rows(path: pathlib.Path) -> list[dict]:
    """Every numbered requirement row of every table in one matrix.

    A table opens at its own header row, which also fixes the four column
    x-positions. It closes at the first line that is not table content: one
    starting left of the Sr. column (a section heading or a narrative paragraph),
    or one whose leftmost word aligns with no column (the signature block, set
    flush right *inside* the evidence column's x range -- so x-range membership
    alone would swallow it).
    """
    cols: list[float] | None = None
    current: dict | None = None
    rows: list[dict] = []

    for words in _visual_lines(path):
        line = " ".join(w[4] for w in words)
        if _FOOTER.match(line):
            continue                       # page furniture, and rows cross pages
        if _HEADER.match(line):
            cols = []
            for w in words:
                if w[4].lower() in _COLUMN_WORDS and (not cols or w[0] > cols[-1] + 5):
                    cols.append(w[0])
            cols, current = cols[:_N_COLS], None
            continue
        if cols is None or len(cols) < _N_COLS:
            continue
        if words[0][0] < cols[0] - 2:
            cols, current = None, None     # left of the table: the table ended
            continue

        cells = [""] * _N_COLS
        for w in words:
            i = max(j for j, x in enumerate(cols) if w[0] >= x - _COL_TOL)
            cells[i] = f"{cells[i]} {w[4]}".strip()

        if _ROW_NO.match(cells[0]):
            current = {"row": int(cells[0]), "requirement": cells[1],
                       "status": cells[2], "evidence": cells[3]}
            rows.append(current)
            continue
        if current is None:
            continue
        if not any(abs(words[0][0] - x) <= _COL_TOL for x in cols):
            cols, current = None, None     # not aligned to a column: not a cell
            continue
        if cells[0]:                       # a continuation indented into the Sr. gutter
            cells[1] = f"{cells[0]} {cells[1]}".strip()
        for key, i in (("requirement", 1), ("status", 2), ("evidence", 3)):
            if cells[i]:
                current[key] = f"{current[key]} {cells[i]}".strip()
    return rows


def requirement_gate(requirement: str) -> tuple[int | None, str | None]:
    """The tender threshold a requirement states, if it states one.

    Three forms occur, and they are matched by shape rather than by text:
    a currency amount ("(Rs. 150 Cr)", ">= INR 150 Cr") -> exact rupees; a span
    of years ("(5 yrs minimum)", "Minimum 5 years in business") -> years; a bare
    minimum count ("Key technical staff (10 minimum)", "Minimum 10 site
    engineers") -> count. Money is tried first because a money gate may also
    contain a digit that the count pattern would otherwise claim.
    """
    if m := _MONEY_GATE.search(requirement or ""):
        return money.parse_money(m.group(0)), "INR"
    if m := _YEARS_GATE.search(requirement or ""):
        return int(m.group(1)), "years"
    if m := _COUNT_GATE.search(requirement or ""):
        return int(m.group(1) or m.group(2)), "count"
    return None, None


def compliance_matrix(doc_id: str, text: str) -> list[dict]:
    """One record per requirement line. Both layouts print the tender reference;
    only the checklist layout prints a matrix id, so the other 19 carry None
    rather than an id invented from the filename."""
    tender = re.search(r"\bRFP-\d+", text)
    matrix_id = re.search(r"^\s*(CM/\d+)\s*$", text, re.MULTILINE)
    out = []
    for row in compliance_rows(_pdf_path(doc_id)):
        threshold, unit = requirement_gate(row["requirement"])
        out.append({
            "matrix_id": matrix_id.group(1) if matrix_id else None,
            "tender_ref": tender.group(0) if tender else None,
            "requirement": _clean(row["requirement"]),
            "status": _clean(row["status"]),
            "threshold_value": threshold,
            "threshold_unit": unit,
            "evidence": _clean(row["evidence"]),
            "_row": row["row"],
        })
    return out


# --------------------------------------------------------------------------
# contracts (final RA bill headers)
# --------------------------------------------------------------------------
#
# Only the header. The BOQ summary and the bill-wise abstract on the same pages
# are `workbooks.py`'s `boq_items` / `measurements`, extracted structurally from
# the six BOQ_and_Measurements_Contract_*.xlsx files (decision_log.md s7).

_CONTRACT_HEAD = re.compile(
    rf"Contract\s*#(\d+)\s*{DASH}\s*(.+?)\s*{DASH}\s*(\d+)\s+RA bills", re.IGNORECASE)


def final_ra_bill(text: str) -> dict:
    """Contract-level facts from the Part I abstract.

    `approved_variations` / `revised_value` are additive to the frozen field list:
    one of the six bills carries an amendment block worth INR 48.88 Cr, and the
    revised value it produces is stated nowhere else in the estate. They are None
    on the other five, which `audit()` reports as layout-conditional rather than
    as a gap.
    """
    head = _CONTRACT_HEAD.search(text)
    period = extract.label_value(text, "Period") or ""
    parts = re.split(rf"\s*{DASH}\s*", period, maxsplit=1)
    return {
        "contract": int(head.group(1)) if head else None,
        "client": _clean(head.group(2)) if head else None,
        "awarded_value": _money_in(extract.label_value(text, "Awarded Value") or ""),
        "billed_value": _money_in(extract.label_value(text, "Total Value of Work Billed") or ""),
        "period_from": extract.parse_date(parts[0]) if parts else None,
        "period_to": extract.parse_date(parts[1]) if len(parts) > 1 else None,
        "n_ra_bills": int(head.group(3)) if head else None,
        "approved_variations": _money_in(extract.label_value(text, "Approved Variations") or ""),
        "revised_value": _money_in(extract.label_value(text, "Revised Value") or ""),
    }


def ra_bill_contracts(corpus: dict[str, str]) -> list[dict]:
    """Contract number and client named by the 6 interim `ra_bill` PDFs.

    Returned for the audit, and deliberately *not* merged into `contracts`.
    Contracts 70, 76 and 77 appear nowhere else in the estate -- not in the
    workbooks, not in the final bills -- so the mapping is a real finding. But an
    interim bill states no awarded value, no period and no RA count, so admitting
    three all-None records would take the block from 6 to 9 and silently change
    the answer to "how many contracts" and "average awarded value". The frozen
    schema says this block is one record per final RA bill; extending it is the
    schema owner's call, not this module's.
    """
    seen = {}
    for doc_id, text in sorted(pdftext.docs_of_type(corpus, "ra_bill").items()):
        m = re.search(rf"Contract\s*#(\d+)\s*{DASH}\s*([^\n]+)", text)
        if m:
            seen.setdefault(int(m.group(1)), {
                "contract": int(m.group(1)), "client": _clean(m.group(2)),
                "source_doc": doc_id})
    return [seen[k] for k in sorted(seen)]


# --------------------------------------------------------------------------
# ledger accounts (cross-check only)
# --------------------------------------------------------------------------
#
# Posting lines are NOT extracted (decision_log.md s7): Receivables_Ageing.xlsx and
# Trial_Balance_by_Year.xlsx already hold that data structurally. Only the
# per-account TOTALS line is read, so the trial balance can be re-verified from a
# fact block on every run.

_ACCOUNT = re.compile(
    rf"ACCOUNT\s+(\d+)\s*{DASH}\s*(.+?)\s*\((\w+)\)\s*\n"      # code, name, (TYPE)
    r"(?:(?!ACCOUNT\s+\d).)*?"                                  # posting lines: dropped
    r"TOTALS\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s*(DR|CR)",
    re.S)


def general_ledger_book(text: str) -> list[dict]:
    """One record per (fiscal year, account) TOTALS line.

    `closing_balance` is signed Dr-positive / Cr-negative, which is the sign
    convention `Trial_Balance_by_Year.xlsx` uses, so the two are comparable
    without a translation step.
    """
    fy = _fy(re.search(r"FISCAL YEAR[^\n]*", text).group(0)) if "FISCAL YEAR" in text else None
    out = []
    for code, name, _type, debit, credit, balance, side in _ACCOUNT.findall(text):
        out.append({
            "fy": fy,
            "account_code": code,
            "account_name": _clean(name),
            "debit_total": _int(debit),
            "credit_total": _int(credit),
            "closing_balance": _int(balance) * (1 if side == "DR" else -1),
        })
    return out


def cumulative_bank_balance(ledger: list[dict]) -> dict[str, int]:
    """Cash at bank at the close of each fiscal year, fy -> rupees.

    The ledger's balance column restarts at zero every volume, so the closing
    balance a bank statement prints is the running sum of the yearly net on
    account 1010. Provided because that running sum is the only figure the
    skipped `bank_statement` PDFs carry (see SKIPPED_BANK_STATEMENTS), and it is
    reproduced to the rupee for all 8 years.
    """
    running, out = 0, {}
    for row in sorted((r for r in ledger if r["account_code"] == "1010"), key=lambda r: r["fy"]):
        running += row["closing_balance"]
        out[row["fy"]] = running
    return out


# --------------------------------------------------------------------------
# module contract
# --------------------------------------------------------------------------

def build(corpus: dict[str, str]) -> dict[str, list[dict]]:
    STATS.clear()
    layouts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    bonds = []
    for doc_id, text in pdftext.docs_of_type(corpus, "performance_bond").items():
        layouts["bonds"][bond_layout(text)] += 1
        bonds.append({**performance_bond(text), "source_doc": doc_id})

    certifications = []
    for doc_id, text in pdftext.docs_of_type(corpus, "iso_certificate").items():
        layouts["certifications"]["certificate of registration"] += 1
        certifications.append({**iso_certificate(text), "source_doc": doc_id})

    compliance, row_runs = [], {}
    for doc_id, text in pdftext.docs_of_type(corpus, "compliance_matrix").items():
        rows = compliance_matrix(doc_id, text)
        layouts["compliance"][
            "bid compliance checklist" if "Bid Compliance Checklist" in text
            else "compliance cum eligibility matrix"] += 1
        row_runs[doc_id] = [r.pop("_row") for r in rows]
        compliance += [{**r, "source_doc": doc_id} for r in rows]

    contracts = []
    for doc_id, text in pdftext.docs_of_type(corpus, "final_ra_bill").items():
        record = final_ra_bill(text)
        layouts["contracts"][
            "with amendments" if record["revised_value"] else "plain abstract"] += 1
        contracts.append({**record, "source_doc": doc_id})

    ledger_accounts = []
    for doc_id, text in pdftext.docs_of_type(corpus, "general_ledger_book").items():
        rows = general_ledger_book(text)
        layouts["ledger_accounts"]["account totals"] += 1
        ledger_accounts += [{**r, "source_doc": doc_id} for r in rows]

    STATS["layouts"] = {k: dict(v) for k, v in layouts.items()}
    STATS["row_runs"] = row_runs
    STATS["ra_bill_contracts"] = ra_bill_contracts(corpus)
    return {
        "contracts": contracts,
        "bonds": bonds,
        "certifications": certifications,
        "compliance": compliance,
        "ledger_accounts": ledger_accounts,
    }


def _on_disk(kind: str) -> int:
    """How many documents of a type the bundle actually contains -- an external
    oracle, so a reader that drops a file cannot hide it by agreeing with itself."""
    return sum(1 for p in DOCS.rglob("*.pdf") if pdftext.doc_type(p.stem) == kind)


def _expected() -> dict[str, int | None]:
    return {
        "contracts": _on_disk("final_ra_bill"),
        "bonds": _on_disk("performance_bond"),
        "certifications": _on_disk("iso_certificate"),
        # No filesystem count exists for table rows; the matrices number their own
        # rows 1..N, so the sum of those runs is the documents' own statement of
        # how many rows they hold. Checked for contiguity separately below.
        "compliance": sum(len(v) for v in STATS.get("row_runs", {}).values()) or None,
        "ledger_accounts": None,
    }


# Fields a layout carries only sometimes. Reported as conditional rather than as a
# gap, with the count that does carry them -- silence either way would be wrong.
_CONDITIONAL = {
    ("bonds", "status"): "printed by the 3-page guarantee layout only",
    ("compliance", "matrix_id"): "printed by the checklist layout only",
    ("compliance", "threshold_value"): "only requirements that state a gate",
    ("compliance", "threshold_unit"): "only requirements that state a gate",
    ("contracts", "approved_variations"): "only bills carrying an amendment block",
    ("contracts", "revised_value"): "only bills carrying an amendment block",
}


def audit(blocks: dict[str, list[dict]]) -> None:
    print(f"instruments.py -- five blocks from {sum(_on_disk(k) for k in ('performance_bond', 'compliance_matrix', 'iso_certificate', 'final_ra_bill', 'general_ledger_book'))} PDFs")
    expected = _expected()

    for key in BLOCKS:
        records = blocks.get(key, [])
        want = expected.get(key)
        flag = "   <-- INCOMPLETE" if want is not None and len(records) != want else ""
        against = f" of {want} expected" if want is not None else ""
        layout = STATS.get("layouts", {}).get(key, {})
        print(f"  {key:<16} {len(records):>4} records{against}   "
              f"layouts: {', '.join(f'{k} x{v}' for k, v in sorted(layout.items()))}{flag}")

        if not records:
            print("      no records   <-- INCOMPLETE")
            continue
        width = max(len(f) for f in records[0])
        for field in records[0]:
            filled = sum(1 for r in records if r.get(field) is not None)
            note = _CONDITIONAL.get((key, field))
            if filled == len(records):
                short = ""
            elif note:
                short = f"   (conditional: {note})"
            else:
                short = "   <-- INCOMPLETE"
            print(f"      {field:<{width}} {filled:>4}/{len(records)}{short}")

    _audit_compliance(blocks)
    _audit_bonds(blocks)
    _audit_ledger(blocks)
    _audit_ra_bills()


def _audit_compliance(blocks: dict[str, list[dict]]) -> None:
    runs = STATS.get("row_runs", {})
    broken = [d for d, r in runs.items() if r != list(range(1, len(r) + 1))]
    print(f"  compliance rows: {len(runs)} matrices, row numbers contiguous 1..N in "
          f"{len(runs) - len(broken)} of {len(runs)}"
          + ("   <-- INCOMPLETE " + ", ".join(broken) if broken else ""))
    subtotals = [r for r in blocks["compliance"]
                 if re.match(r"^\s*(total|sub-?total)\b", r["requirement"] or "", re.I)]
    print(f"  compliance rows that are subtotals rather than requirements: {len(subtotals)}"
          + ("   <-- INCOMPLETE" if subtotals else ""))
    gates = collections.Counter(
        (r["threshold_unit"], r["threshold_value"]) for r in blocks["compliance"]
        if r["threshold_value"] is not None)
    print(f"  tender gate values across {len(blocks['compliance'])} requirement rows:")
    for (unit, value), n in sorted(gates.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        shown = money.format_money(value) if unit == "INR" else value
        print(f"      {unit:<6} {shown:>15}   x{n}")


def _audit_bonds(blocks: dict[str, list[dict]]) -> None:
    bonds = blocks["bonds"]
    statuses = collections.Counter(b["status"] for b in bonds)
    unknown = [s for s in statuses if s and s.strip().lower() not in _CLOSED_STATUSES | _OPEN_STATUSES]
    print(f"  bond status: " + ", ".join(f"{k or 'not printed'} x{v}" for k, v in statuses.most_common())
          + ("   <-- INCOMPLETE unclassified: " + ", ".join(unknown) if unknown else ""))
    stated = [b for b in bonds if b["amount"]]
    print(f"  bond amounts: {len(stated)} of {len(bonds)} state a non-zero figure; "
          f"total {money.format_money(sum(b['amount'] or 0 for b in bonds))}. "
          f"The one-page letter layout always prints zero.")
    live = collections.Counter(is_live(b) for b in bonds)
    exposure = sum(b["amount"] or 0 for b in bonds if is_live(b))
    print(f"  live as of {REFERENCE_DATE}: {live[True]} live, {live[False]} closed, "
          f"{live[None]} undecidable; live exposure {money.format_money(exposure)}")

    refs = {b["tender_ref"] for b in bonds if b["tender_ref"]}
    cm_refs = {r["tender_ref"] for r in blocks["compliance"] if r["tender_ref"]}
    joined = len(cm_refs & refs)
    print(f"  tender_ref join: {len(refs)} distinct bond refs, {len(cm_refs)} matrix refs, "
          f"{joined} of {len(cm_refs)} matrices reach a bond"
          + ("   <-- INCOMPLETE" if joined != len(cm_refs) else ""))


def _audit_ledger(blocks: dict[str, list[dict]]) -> None:
    ledger = blocks["ledger_accounts"]
    print(f"  ledger: {len(ledger)} (fy, account) totals over "
          f"{len({r['source_doc'] for r in ledger})} volumes, "
          f"{len({r['fy'] for r in ledger})} fiscal years "
          f"({min(r['fy'] for r in ledger)} .. {max(r['fy'] for r in ledger)})")
    if not WORKBOOKS_JSON.exists():
        print(f"  trial-balance reconciliation skipped: {WORKBOOKS_JSON.name} not built")
        return
    trial = json.load(WORKBOOKS_JSON.open(encoding="utf-8"))["trial_balance"]
    index = {(r["fy"], _key(r["account"])): r for r in trial}
    agree = [r for r in ledger if (r["fy"], _key(r["account_name"])) in index
             and _totals(index[(r["fy"], _key(r["account_name"]))]) == _totals(r, ledger=True)]
    unmatched = [r for r in ledger if (r["fy"], _key(r["account_name"])) not in index]
    print(f"  trial balance vs ledger: {len(agree)} of {len(trial)} workbook rows reproduced "
          f"exactly (debit, credit, balance)"
          + ("" if len(agree) == len(trial) else "   <-- INCOMPLETE"))
    if unmatched:
        years = sorted({r["fy"] for r in unmatched})
        print(f"      {len(unmatched)} ledger accounts have no workbook row, all in "
              f"{', '.join(years)} -- the workbook does not cover that year")
    print("  cash at bank, cumulative (the figure the skipped bank statements print):")
    for fy, balance in cumulative_bank_balance(ledger).items():
        print(f"      {fy}  {money.format_money(balance):>17}")


def _audit_ra_bills() -> None:
    extra = STATS.get("ra_bill_contracts", [])
    print(f"  ra_bill headers name {len(extra)} further contracts, held out of `contracts` "
          f"(see ra_bill_contracts): "
          + "; ".join(f"#{r['contract']} {r['client']}" for r in extra))


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _totals(row: dict, ledger: bool = False) -> tuple:
    if ledger:
        return row["debit_total"], row["credit_total"], row["closing_balance"]
    return row["debit"], row["credit"], row["balance"]


def main() -> None:
    blocks = build(pdftext.load_corpus())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8")
    audit(blocks)
    print(f"  wrote {OUT}")
    print("  " + SKIPPED_BANK_STATEMENTS.replace("\n", "\n  "))


if __name__ == "__main__":
    main()
