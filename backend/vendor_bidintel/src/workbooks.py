"""Ingest the nine .xlsx workbooks into five fact blocks.

`BRIEFING.md` s4 flags these files as carrying "structured detail that does not
appear in the PDFs" and warns that they hold live formulas. Both warnings are
real and both are handled here:

* **Every formula cell in the estate is uncached.** All 37 of them are
  `=SUM(...)` in a trailing `TOTAL` row, and `openpyxl.load_workbook(
  data_only=True)` returns `None` for each, because no copy of Excel ever
  calculated the file. Loading a second time with `data_only=False` recovers
  the formula text, which is evaluated here rather than dropped.
* Those `TOTAL` rows are **not** records. A sum over them would double every
  aggregate. They are excluded from the blocks and used instead as the file's
  own statement of how many data rows it contains: `=SUM(E2:E211)` says the
  register holds 210 assets, so a reader that returns any other number has
  dropped or invented rows. That check is the point of `_expected_rows()` --
  it is an oracle taken from the file, not a constant tuned to pass.

Field names and types follow `docs/fact_schema.md`, which is frozen.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import date, datetime
from decimal import Decimal

import openpyxl

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import extract  # noqa: E402
import money  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKBOOKS = REPO / "dataset" / "documents" / "workbooks"
OUT = REPO / "results" / "raw" / "workbooks.json"

BLOCKS = ("assets", "invoices", "trial_balance", "boq_items", "measurements")

# Sheets that are prose, not data. Excluded by name, and separately by the fact
# that they carry no header row we recognise.
NOTES_SHEET = "Notes"

# A trailing summary row: first cell literally says TOTAL. Detected rather than
# assumed at a fixed row index, because the sheets have different lengths.
_TOTAL_ROW = re.compile(r"^\s*totals?\s*$", re.IGNORECASE)

_SUM = re.compile(r"^=SUM\((.+)\)$", re.IGNORECASE)
_RANGE = re.compile(r"^\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)$")

# Counters filled in by build(), printed by audit(). Module-level because the
# audit contract takes only the blocks.
STATS: dict[str, int] = {}


# --------------------------------------------------------------------------
# cell coercion
# --------------------------------------------------------------------------

def _text(cell) -> str | None:
    """Trim a text cell. ' Tunnel Boring Machine' really is stored with a
    leading space in the plant register; not stripping it silently splits one
    asset type into two."""
    if cell is None:
        return None
    s = str(cell).strip()
    return s or None


def _money(cell) -> int | None:
    """Exact integer rupees. Workbook money cells are already whole rupees as
    ints; a string cell goes through `money.parse_money`, and a float goes
    through `Decimal(str(...))` so nothing is rounded by binary floating point."""
    if cell is None:
        return None
    if isinstance(cell, bool):
        raise ValueError(f"boolean in a money cell: {cell!r}")
    if isinstance(cell, int):
        return cell
    if isinstance(cell, float):
        d = Decimal(str(cell))
        if d != d.to_integral_value():
            raise ValueError(f"non-integral rupee cell: {cell!r}")
        return int(d)
    parsed = money.parse_money(str(cell))
    if parsed is None:
        raise ValueError(f"unparseable money cell: {cell!r}")
    return parsed


def _number(cell) -> float | None:
    """BOQ quantities are genuinely fractional and are not money."""
    if cell is None:
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    return float(str(cell).replace(",", ""))


def _int(cell) -> int | None:
    if cell is None:
        return None
    if isinstance(cell, int):
        return cell
    if isinstance(cell, float) and cell.is_integer():
        return int(cell)
    m = re.search(r"-?\d+", str(cell))
    return int(m.group(0)) if m else None


def _date(cell) -> str | None:
    """ISO yyyy-mm-dd. Measurement dates arrive as real datetimes; receivables
    dates arrive as strings and go through the corpus-wide date parser."""
    if cell is None:
        return None
    if isinstance(cell, datetime):
        return cell.date().isoformat()
    if isinstance(cell, date):
        return cell.isoformat()
    return extract.parse_date(str(cell))


_TRUE = {"true", "yes", "y", "1"}
_FALSE = {"false", "no", "n", "0"}


def _bool(cell) -> bool | None:
    """`Safety Certified` may be a real bool, "TRUE"/"Yes", or 1."""
    if cell is None:
        return None
    if isinstance(cell, bool):
        return cell
    if isinstance(cell, (int, float)):
        return bool(cell)
    s = str(cell).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"unrecognised boolean cell: {cell!r}")


# --------------------------------------------------------------------------
# sheet reading
# --------------------------------------------------------------------------

def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _evaluate_sum(formula: str, values) -> int | float:
    """Evaluate an uncached `=SUM(...)` against the value grid.

    Only SUM appears in these nine files (asserted by the caller, which raises
    on any other function), so this deliberately implements nothing else: a
    silent partial evaluator would be worse than a loud failure.
    """
    m = _SUM.match(formula.strip())
    if not m:
        raise ValueError(f"unsupported formula, only =SUM() is handled: {formula!r}")
    total = 0
    for term in m.group(1).split(","):
        term = term.strip()
        rng = _RANGE.match(term)
        if rng:
            c0, r0, c1, r1 = _col_index(rng.group(1)), int(rng.group(2)), \
                _col_index(rng.group(3)), int(rng.group(4))
            for r in range(min(r0, r1), max(r0, r1) + 1):
                for c in range(min(c0, c1), max(c0, c1) + 1):
                    v = values.cell(r, c).value
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        total += v
        else:
            single = re.match(r"^\$?([A-Z]+)\$?(\d+)$", term)
            if not single:
                raise ValueError(f"unsupported SUM operand: {term!r}")
            v = values.cell(int(single.group(2)), _col_index(single.group(1))).value
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total += v
    return total


def _expected_rows(formulas) -> int | None:
    """How many data rows the sheet's own TOTAL formula says it has.

    `=SUM(E2:E211)` means rows 2..211 are data, so the sheet holds 210 records.
    Where a sheet carries several such formulas they must agree.
    """
    spans = set()
    for row in formulas.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                m = _SUM.match(cell.value.strip())
                if not m:
                    continue
                for term in m.group(1).split(","):
                    rng = _RANGE.match(term.strip())
                    if rng:
                        spans.add(abs(int(rng.group(4)) - int(rng.group(2))) + 1)
    if not spans:
        return None
    if len(spans) > 1:
        raise ValueError(f"TOTAL formulas disagree on the data range: {sorted(spans)}")
    return spans.pop()


def _read_sheet(path: pathlib.Path, sheet: str) -> tuple[list[str], list[list], dict]:
    """Return (headers, data rows, per-sheet stats).

    Every row between the header and the end of the sheet dimension is either
    returned or counted in the stats, so nothing can be dropped without showing
    up in the audit.
    """
    values = openpyxl.load_workbook(path, data_only=True)[sheet]
    formulas = openpyxl.load_workbook(path, data_only=False)[sheet]

    ncols = values.max_column
    headers = [_text(values.cell(1, c).value) for c in range(1, ncols + 1)]

    rows, totals, blanks = [], [], 0
    for r in range(2, values.max_row + 1):
        raw = [values.cell(r, c).value for c in range(1, ncols + 1)]
        if all(v is None for v in raw):
            blanks += 1
            continue
        if isinstance(raw[0], str) and _TOTAL_ROW.match(raw[0]):
            totals.append(r)
            continue
        rows.append(raw)

    # Formula reconciliation. Every formula in these files sits in a TOTAL row,
    # so a computed value never enters a record -- but it is computed anyway,
    # both to prove the TOTAL rows are pure subtotals and to give the audit an
    # independent figure to compare the record sum against.
    computed, uncached = {}, 0
    for row in formulas.iter_rows():
        for cell in row:
            if not (isinstance(cell.value, str) and cell.value.startswith("=")):
                continue
            if values[cell.coordinate].value is None:
                uncached += 1
                computed[cell.coordinate] = _evaluate_sum(cell.value, values)
            else:
                computed[cell.coordinate] = values[cell.coordinate].value

    stats = {
        "sheet_rows": values.max_row - 1,     # everything below the header
        "data_rows": len(rows),
        "total_rows": len(totals),
        "blank_rows": blanks,
        "formula_cells": len(computed),
        "uncached_formula_cells": uncached,
        "expected_rows": _expected_rows(formulas),
        "computed": computed,
    }
    if stats["data_rows"] + stats["total_rows"] + stats["blank_rows"] != stats["sheet_rows"]:
        raise AssertionError(f"{path.name}/{sheet}: rows read do not account for the sheet")
    if stats["expected_rows"] not in (None, stats["data_rows"]):
        raise AssertionError(
            f"{path.name}/{sheet}: TOTAL formula spans {stats['expected_rows']} rows "
            f"but {stats['data_rows']} data rows were read")
    return headers, rows, stats


def _record_stats(key: str, path: pathlib.Path, sheet: str, stats: dict) -> None:
    for field in ("sheet_rows", "data_rows", "total_rows", "blank_rows",
                  "formula_cells", "uncached_formula_cells"):
        STATS[f"{key}.{field}"] = STATS.get(f"{key}.{field}", 0) + stats[field]
    STATS.setdefault("sheets", 0)
    STATS["sheets"] += 1


# --------------------------------------------------------------------------
# per-workbook readers
# --------------------------------------------------------------------------

def _read_assets() -> list[dict]:
    path = WORKBOOKS / "Plant_and_Machinery_Register.xlsx"
    _, rows, stats = _read_sheet(path, "Plant Register")
    _record_stats("assets", path, "Plant Register", stats)
    STATS["assets.total_cost_formula"] = stats["computed"].get("E212")
    return [{
        "asset_id": _int(r[0]),
        "asset_type": _text(r[1]),
        "make": _text(r[2]),
        "acquired": _int(r[3]),
        "cost": _money(r[4]),
        "condition": _text(r[5]),
        "location": _text(r[6]),
        "ownership": _text(r[7]),
        "safety_certified": _bool(r[8]),
        "source_doc": path.name,
    } for r in rows]


def _read_invoices() -> list[dict]:
    path = WORKBOOKS / "Receivables_Ageing.xlsx"
    _, rows, stats = _read_sheet(path, "AR Ageing")
    _record_stats("invoices", path, "AR Ageing", stats)
    return [{
        "invoice_no": _text(r[0]),
        "client": _text(r[1]),
        "invoice_date": _date(r[2]),
        "invoiced": _money(r[3]),
        "status": _text(r[4]),
        "received": _money(r[5]),
        "outstanding": _money(r[6]),
        "source_doc": path.name,
    } for r in rows]


def _fy_from_sheet(name: str) -> str:
    """'TB 2019-20' -> '2019-20', per the frozen fiscal-year convention."""
    m = re.search(r"(\d{4})\s*[-‐-―/]\s*(\d{2}|\d{4})", name)
    if not m:
        raise ValueError(f"no fiscal year in sheet name {name!r}")
    return f"{m.group(1)}-{m.group(2)[-2:]}"


def _read_trial_balance() -> list[dict]:
    path = WORKBOOKS / "Trial_Balance_by_Year.xlsx"
    book = openpyxl.load_workbook(path, data_only=True)
    out = []
    for sheet in book.sheetnames:
        if sheet == NOTES_SHEET:
            continue
        _, rows, stats = _read_sheet(path, sheet)
        _record_stats("trial_balance", path, sheet, stats)
        fy = _fy_from_sheet(sheet)
        out += [{
            "fy": fy,
            "account": _text(r[0]),
            "debit": _money(r[1]),
            "credit": _money(r[2]),
            "balance": _money(r[3]),
            "source_doc": path.name,
        } for r in rows]
    return out


def _contract_workbooks() -> list[tuple[int, pathlib.Path]]:
    """Contract number comes from the filename, never from a hardcoded list."""
    found = []
    for path in sorted(WORKBOOKS.glob("BOQ_and_Measurements_Contract_*.xlsx")):
        m = re.search(r"Contract_(\d+)$", path.stem)
        if not m:
            raise ValueError(f"no contract number in filename {path.name!r}")
        found.append((int(m.group(1)), path))
    return found


def _read_boq_and_measurements() -> tuple[list[dict], list[dict]]:
    boq, meas = [], []
    for contract, path in _contract_workbooks():
        _, rows, stats = _read_sheet(path, "BOQ")
        _record_stats("boq_items", path, "BOQ", stats)
        boq += [{
            "contract": contract,
            "item_no": _int(r[0]),
            "description": _text(r[1]),
            "unit": _text(r[2]),
            "quantity": _number(r[3]),
            "rate": _money(r[4]),
            "amount": _money(r[5]),
            "source_doc": path.name,
        } for r in rows]

        _, rows, stats = _read_sheet(path, "Measurements")
        _record_stats("measurements", path, "Measurements", stats)
        meas += [{
            "contract": contract,
            "ra_no": _int(r[0]),
            "measured_on": _date(r[1]),
            "item_no": _int(r[2]),
            "description": _text(r[3]),
            "qty": _number(r[4]),
            "amount": _money(r[5]),
            "source_doc": path.name,
        } for r in rows]
    return boq, meas


# --------------------------------------------------------------------------
# module contract
# --------------------------------------------------------------------------

def build(corpus: dict[str, str] | None = None) -> dict[str, list[dict]]:
    """`corpus` is ignored: these facts come from the .xlsx files, not the PDFs.

    The parameter is kept so `build_facts.py` can call every module the same way.
    """
    STATS.clear()
    boq, meas = _read_boq_and_measurements()
    return {
        "assets": _read_assets(),
        "invoices": _read_invoices(),
        "trial_balance": _read_trial_balance(),
        "boq_items": boq,
        "measurements": meas,
    }


def audit(blocks: dict[str, list[dict]]) -> None:
    """Coverage per block against the workbooks' own TOTAL formulas."""
    print("workbooks.py -- five blocks from "
          f"{len(list(WORKBOOKS.glob('*.xlsx')))} .xlsx files, {STATS.get('sheets', 0)} data sheets")

    for key in BLOCKS:
        records = blocks.get(key, [])
        read = STATS.get(f"{key}.data_rows", 0)
        sheet_rows = STATS.get(f"{key}.sheet_rows", 0)
        totals = STATS.get(f"{key}.total_rows", 0)
        flag = "" if len(records) == read and read + totals == sheet_rows else "   <-- INCOMPLETE"
        print(f"  {key:<14} {len(records):>4} records   "
              f"(sheet rows below header {sheet_rows}, minus {totals} TOTAL row(s)){flag}")

        if not records:
            continue
        fields = list(records[0])
        widths = max(len(f) for f in fields)
        for field in fields:
            filled = sum(1 for r in records if r.get(field) is not None)
            short = "   <-- INCOMPLETE" if filled < len(records) else ""
            print(f"      {field:<{widths}} {filled:>4}/{len(records)}{short}")

    uncached = sum(v for k, v in STATS.items() if k.endswith(".uncached_formula_cells"))
    cells = sum(v for k, v in STATS.items() if k.endswith(".formula_cells"))
    print(f"  formula cells {cells}, of which {uncached} had no cached value "
          f"and were evaluated here")

    # The TOTAL rows are excluded from the blocks, so their (computed) value is
    # free as an independent check on the records that were kept.
    cost = STATS.get("assets.total_cost_formula")
    if cost is not None:
        summed = sum(r["cost"] for r in blocks.get("assets", []))
        ok = "OK" if cost == summed else "<-- MISMATCH"
        print(f"  plant register TOTAL formula {money.format_money(int(cost))} "
              f"vs sum of asset records {money.format_money(summed)}  {ok}")

    for fy in sorted({r["fy"] for r in blocks.get("trial_balance", [])}):
        rows = [r for r in blocks["trial_balance"] if r["fy"] == fy]
        d, c, b = (sum(r[k] for r in rows) for k in ("debit", "credit", "balance"))
        ok = "OK" if d == c and b == 0 else "<-- DOES NOT BALANCE"
        print(f"  trial balance {fy}: {len(rows):>3} accounts, "
              f"debits {money.format_money(d)} = credits {money.format_money(c)}, "
              f"balance {b}  {ok}")


def main() -> None:
    blocks = build({})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8")
    audit(blocks)
    print(f"  wrote {OUT}")


if __name__ == "__main__":
    main()
