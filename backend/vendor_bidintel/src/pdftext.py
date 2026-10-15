"""Layout-preserving text extraction with an on-disk cache.

BRIEFING.md s6 warns that default extractors silently return a fraction of a
table-heavy page. PyMuPDF's "blocks" mode sorted by position keeps label/value
pairs adjacent, which is what every downstream parser here relies on.

Extraction of the full corpus takes ~1 minute, so results are cached to
results/raw/text_cache.json and reused until --refresh is passed.
"""
from __future__ import annotations

import json
import pathlib

import pymupdf

REPO = pathlib.Path(__file__).resolve().parents[1]
DOCS = REPO / "dataset" / "documents"
CACHE = REPO / "results" / "raw" / "text_cache.json"


def extract_pdf(path: pathlib.Path) -> str:
    """Return the document's text, pages joined by form feed."""
    with pymupdf.open(path) as doc:
        pages = []
        for page in doc:
            blocks = page.get_text("blocks")  # (x0, y0, x1, y1, text, ...)
            blocks.sort(key=lambda b: (round(b[1], 1), round(b[0], 1)))
            pages.append("\n".join(b[4].strip() for b in blocks if b[4].strip()))
    return "\f".join(pages)


def load_corpus(refresh: bool = False) -> dict[str, str]:
    """Map doc_id -> full text for every PDF in the corpus."""
    if CACHE.exists() and not refresh:
        return json.loads(CACHE.read_text(encoding="utf-8"))

    corpus = {}
    for path in sorted(DOCS.rglob("*.pdf")):
        corpus[path.stem] = extract_pdf(path)

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
    return corpus


def doc_type(doc_id: str) -> str:
    """Infer document type from its id prefix (DOC-CC-001 -> completion_certificate)."""
    return _PREFIX.get(doc_id.split("-")[1], "unknown")


# Keys are the *actual* id segments used by the bundle's filenames. The first
# draft of this map guessed six of them (PB / GL / BS / RA / FRA / ISO) and left
# 93 documents typed "unknown" — silently, because nothing consumed those types
# yet. Asserted against the corpus by test_doc_type_covers_every_document.
_PREFIX = {
    "CC": "completion_certificate",
    "CCC": "company_completion_certificate",
    "REF": "reference_letter",
    "BOND": "performance_bond",
    "PCERT": "personnel_certificate",
    "CV": "cv",
    "CM": "compliance_matrix",
    "GLB": "general_ledger_book",
    "BANK": "bank_statement",
    "FS": "financial_statement",
    "RABILL": "ra_bill",
    "FINBILL": "final_ra_bill",
    "DOSSIER": "tender_dossier",
    "CERT": "iso_certificate",
    "AR": "annual_report",
    "PPP": "past_performance_portfolio",
}


def docs_of_type(corpus: dict[str, str], kind: str) -> dict[str, str]:
    """The subset of the corpus of one document type, in id order."""
    return {k: v for k, v in sorted(corpus.items()) if doc_type(k) == kind}


if __name__ == "__main__":
    import sys

    corpus = load_corpus(refresh="--refresh" in sys.argv)
    print(f"cached {len(corpus)} documents -> {CACHE}")
    chars = sum(len(v) for v in corpus.values())
    print(f"{chars:,} characters of extractable text")
