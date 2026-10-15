"""Parse Indian monetary renderings into exact integer rupees.

The corpus renders one value four ways (BRIEFING.md s2):
    INR 33.38 Cr | 3,338.00 Lakh | 33,38,00,000 | 333800000

Decimal scaling goes through int(round(Decimal * scale)) rather than float
arithmetic, because 19.33 Cr as a float lands on 193299999.99999997.
"""
from __future__ import annotations

import re
from decimal import Decimal

CRORE = Decimal(10_000_000)
LAKH = Decimal(100_000)

# "INR 33.38 Cr", "Rs. 3,338.00 Lakh", "₹ 33,38,00,000", "200.00 Cr"
_MONEY = re.compile(
    r"""(?:INR|Rs\.?|₹)?\s*
        (?P<num>\d[\d,]*(?:\.\d+)?)\s*
        (?P<unit>Cr(?:ore)?s?|Lakhs?|Lacs?|L)?\b""",
    re.IGNORECASE | re.VERBOSE,
)

_UNIT_SCALE = {"cr": CRORE, "crore": CRORE, "crores": CRORE, "crs": CRORE,
               "lakh": LAKH, "lakhs": LAKH, "lac": LAKH, "lacs": LAKH, "l": LAKH}


def _parse_match(m: re.Match) -> tuple[int, int] | None:
    """Return (rupees, granularity). Granularity is the smallest rupee step the
    rendering can express: 1 for `19,32,99,999`, 100000 for `19.33 Cr`."""
    raw = m.group("num").replace(",", "")
    try:
        value = Decimal(raw)
    except Exception:
        return None
    unit = (m.group("unit") or "").lower().rstrip(".")
    scale = _UNIT_SCALE.get(unit, Decimal(1))
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    granularity = max(int(scale) // (10 ** decimals), 1)
    return int((value * scale).to_integral_value(rounding="ROUND_HALF_UP")), granularity


def parse_money(text: str) -> int | None:
    """Return the first monetary amount in `text` as whole rupees, else None."""
    m = _MONEY.search(text or "")
    parsed = _parse_match(m) if m else None
    return parsed[0] if parsed else None


def best_money(text: str) -> int | None:
    """Return the most precisely rendered amount in `text`.

    A certificate may state the same value twice — "INR 19,32,99,999/- (Rupees
    19.33 Crore Only)". Both are correct, but only the grouped integer is exact,
    so prefer the rendering with the finest granularity.
    """
    best = None
    for m in _MONEY.finditer(text or ""):
        parsed = _parse_match(m)
        if parsed and (best is None or parsed[1] < best[1]):
            best = parsed
    return best[0] if best else None


def format_money(rupees: int) -> str:
    """Render rupees in Indian digit grouping, for reports and audit output."""
    s = str(abs(int(rupees)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(?<=\d)(?=(\d\d)+$)", ",", head)
        s = f"{head},{tail}"
    return ("-" if rupees < 0 else "") + s
