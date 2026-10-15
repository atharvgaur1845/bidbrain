"""Page-level provenance.

facts.json says which *document* a value came from but not which page. The text
cache keeps pages separated by form feeds, so a value's page is the first page
whose (whitespace-folded) text contains one of its renderings. Money is searched
in every way the corpus writes it (BRIEFING.md s2): `INR 33.38 Cr`,
`3,338.00 Lakh`, `33,38,00,000`, `333800000`.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache

from bidbrain import config


def _fold(s: str) -> str:
    return re.sub(r"\s+", " ", s).lower()


def indian_grouping(n: int) -> str:
    s = str(abs(int(n)))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def money_renderings(rupees: int | None) -> list[str]:
    if rupees is None:
        return []
    cr = (Decimal(rupees) / Decimal(10_000_000)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    lakh = (Decimal(rupees) / Decimal(100_000)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    sign = "-" if rupees < 0 else ""
    out = [f"{cr} Cr", f"{lakh:,} Lakh", f"{lakh} Lakh", f"{cr} Crore",
           sign + indian_grouping(rupees), str(rupees)]
    if rupees % 100_000 == 0:          # statements print whole lakhs: "19,911"
        whole = abs(rupees) // 100_000
        out += [f"{sign}{whole:,}", f"{sign}{indian_grouping(whole)}"]
    return out


_MON = ["January", "February", "March", "April", "May", "June", "July", "August",
        "September", "October", "November", "December"]


def date_renderings(iso_date: str | None) -> list[str]:
    """Every way the corpus writes a date (certificates mix at least five)."""
    if not iso_date:
        return []
    y, m, d = int(iso_date[:4]), int(iso_date[5:7]), int(iso_date[8:10])
    mon, mo3 = _MON[m - 1], _MON[m - 1][:3]
    suf = "th" if 11 <= d <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
    return [iso_date, f"{mon} {d}, {y}", f"{d} {mon} {y}", f"{d:02d} {mon} {y}",
            f"{d} {mo3} {y}", f"{d:02d} {mo3} {y}", f"{mo3} {d}, {y}", f"{d}{suf} {mon} {y}",
            f"{d:02d}/{m:02d}/{y}", f"{d:02d}-{m:02d}-{y}", f"{d:02d}.{m:02d}.{y}",
            f"{d:02d}-{mo3}-{y}", f"{d}-{mo3}-{y}"]


class PageIndex:
    def __init__(self, corpus: dict[str, str]):
        self.raw = corpus
        self.pages = {d: [_fold(p) for p in t.split("\f")] for d, t in corpus.items()}

    def n_pages(self, doc_id: str) -> int:
        return len(self.pages.get(doc_id, [])) or 1

    def find(self, doc_id: str | None, *needles: str | None) -> int | None:
        """1-based page of the first needle (in priority order) found in doc."""
        pages = self.pages.get(doc_id or "")
        if not pages:
            return None
        for needle in needles:
            if not needle:
                continue
            n = _fold(str(needle))
            for i, p in enumerate(pages):
                if n in p:
                    return i + 1
        return None

    def find_money(self, doc_id: str | None, rupees: int | None, *fallback: str) -> int | None:
        return self.find(doc_id, *money_renderings(rupees), *fallback)

    def find_date(self, doc_id: str | None, iso_date: str | None, *fallback: str) -> int | None:
        return self.find(doc_id, *date_renderings(iso_date), *fallback)


@lru_cache(maxsize=1)
def page_index() -> PageIndex:
    return PageIndex(json.loads(config.TEXT_CACHE.read_text(encoding="utf-8")))
