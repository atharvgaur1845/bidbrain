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


# ---- the date a document carries -----------------------------------------------------------------
# Used for valid time: a claim cannot be believed as of a day before the document that states it existed.
# Only a date the document prints about itself counts (its issue / signature / submission date). Dates
# inside the content (a completion date, an audit schedule, an agreement "dated ...") do not, and a
# document that prints none stays undated rather than being given a guessed date.
_D = (r"(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}|\d{1,2} (?:[A-Z][a-z]{2,8}) \d{4}|"
      r"(?:[A-Z][a-z]{2,8}) \d{1,2}, \d{4})")
_DOC_DATE = [rf"Issue Date:\s*{_D}", rf"Date of Issue\s*{_D}", rf"Issued:?\s*{_D}", rf"Submitted:\s*{_D}",
             rf"Dated?:\s*{_D}"]


def _iso_of(s: str) -> str | None:
    import datetime as _dt
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return _dt.datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def doc_date(text: str | None) -> str | None:
    """ISO date the document prints as its own date, or None. Labelled dates first ('Issue Date:',
    'Date of Issue', 'Issued:', 'Submitted:', 'Date:'), then a line that is only a date in the letterhead
    (the first seven lines, where letters and bills put it)."""
    if not text:
        return None
    for pat in _DOC_DATE:
        m = re.search(pat, text)
        if m and (iso := _iso_of(m.group(1))):
            return iso
    for line in text.split("\n")[:7]:
        if re.fullmatch(_D, line.strip()) and (iso := _iso_of(line)):
            return iso
    return None
