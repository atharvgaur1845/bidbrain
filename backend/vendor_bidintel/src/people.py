"""Personnel facts: the `staff` and `business_units` blocks.

Two blocks, two very different kinds of number, and the distinction between them
is the whole point of this module:

* **`business_units`** (6 records, from the tender dossiers' Annexure A) is a
  **corpus-wide truth**. Its head-counts sum to 486 — the "Employees on record"
  figure in `dataset/README.md`, restated independently by all 40 compliance
  matrices ("486 personnel on rolls" / "486 engineers available"). Any question
  of the form *"how many employees / staff / engineers do we have"* is answered
  from `sum(headcount)`, never from `len(staff)`.

* **`staff`** (39 records, one per `cv`) is a **sample**: the key personnel —
  every one of them a Project Manager — whose CVs are annexed to a bid. 39 is
  8% of the workforce. A router that answers "how many employees" with
  `len(staff)` returns 39 against a gold of 486 and scores zero.

The rule that separates them: a question that needs a *per-person attribute*
(experience, qualification, wage group, joining date, a credential) can only be
about the 39, because those attributes exist for nobody else in the estate. A
question that needs only a *count of bodies* is about the 486. See the audit
output, which prints both numbers side by side and labels them.

`personnel` (48 credential certificates) is not owned here — `build_facts.py`
owns it — but the CV name is the join key into it, so this module audits the
join and reports it.
"""
from __future__ import annotations

import collections
import json
import pathlib
import re

import extract
import pdftext

REPO = pathlib.Path(__file__).resolve().parents[1]
OUT = REPO / "results" / "raw" / "people.json"

# Stated in dataset/README.md ("Employees on record", "Business units"). Used as
# an external oracle in the audit, never as a value written into a fact.
README = REPO / "dataset" / "README.md"


# --------------------------------------------------------------------------
# curriculum vitae -> staff
# --------------------------------------------------------------------------

# The header is a label/value block; `extract.label_value` is used rather than a
# positional read because it is the whole-line matcher the corpus already forced
# on us (decision_log.md s2). Labels are ordered most-specific-first per its
# contract, so "Total Experience" is tried before any bare "Experience".
_CV_LABELS = {
    "person": ("Name of Employee", "Full Name", "Name"),
    "employee_id": ("Employee ID", "Employee Code", "Emp ID"),
    "designation": ("Designation", "Position", "Role"),
    "business_unit": ("Business Unit / Division", "Business Unit", "Division", "Unit"),
    "experience_raw": ("Total Experience (years)", "Total Experience", "Experience"),
    "qualification": ("Highest Qualification", "Educational Qualification", "Qualification"),
    "joined_raw": ("Date of Joining (DOJ)", "Date of Joining", "Joined", "DOJ"),
    "wage_group": ("Wage Group", "Wage Band", "Pay Group", "Grade"),
}

_YEARS = re.compile(r"(\d{1,3})\s*(?:years?|yrs?)\b", re.IGNORECASE)


def parse_years(text: str | None) -> int | None:
    """"28 years" -> 28. Returns None rather than 0 for an unreadable value.

    A bare integer is accepted too, but a bare *year* is not: "2013" as a
    duration is a mis-parse, not 2013 years of service, so anything above a
    human career length is rejected loudly by returning None.
    """
    if not text:
        return None
    m = _YEARS.search(text)
    if not m:
        m = re.fullmatch(r"\s*(\d{1,3})\s*", text)
        if not m:
            return None
    n = int(m.group(1))
    return n if 0 <= n <= 70 else None


def as_of(corpus: dict[str, str]) -> str | None:
    """The corpus's own "today", read from the documents rather than the clock.

    Every CV footer prints "Verified: March 31, 2026" and every compliance matrix
    is dated the same day. Tenure ("how long has X been with us") must be measured
    against that date: using `date.today()` makes the same question return a
    different number every year, and the gold answers were computed once.

    Note this is *tenure*, a different quantity from `experience_years`, which is
    whole-career experience. A staff member with 28 years of experience who
    joined in 2013 has 12 years of tenure. Confusing the two is a whole class of
    wrong answer.
    """
    seen = collections.Counter()
    for text in pdftext.docs_of_type(corpus, "cv").values():
        if m := re.search(r"Verified\s*[:·]\s*(.+)", text):
            if iso := extract.parse_date(m.group(1)):
                seen[iso] += 1
    return seen.most_common(1)[0][0] if seen else None


def tenure_years(joined: str | None, reference: str | None) -> int | None:
    """Completed years between a joining date and the corpus reference date."""
    if not (joined and reference):
        return None
    j, r = (tuple(int(x) for x in d.split("-")) for d in (joined, reference))
    return r[0] - j[0] - ((r[1], r[2]) < (j[1], j[2]))


def cv_layout(text: str) -> str:
    """Which CV rendering this is. 'unknown' means no record can be trusted.

    Every extractor in this project is expected to survive 2+ layouts per type
    (BRIEFING.md s1). The CVs turn out to be the exception — all 39 ship one
    header layout — so this returns a name rather than silently assuming it, and
    `audit` reports the census. If a hidden-set CV arrives in a second layout it
    lands in 'unknown' and is flagged instead of producing a hollow record.
    """
    if all(extract.label_value(text, *_CV_LABELS[f]) for f in
           ("person", "employee_id", "designation", "business_unit")):
        return "label_header"
    if re.search(r"^\s*Name\s*[:·]", text, re.M) or "Curriculum" in text:
        return "prose_header"
    return "unknown"


def curriculum_vitae(text: str) -> dict:
    """One CV -> one staff record (schema: fact_schema.md, `staff`)."""
    raw = {k: extract.label_value(text, *labels) for k, labels in _CV_LABELS.items()}
    return {
        "person": (raw["person"] or "").strip() or None,
        "employee_id": raw["employee_id"],
        "designation": raw["designation"],
        "business_unit": raw["business_unit"],
        "experience_years": parse_years(raw["experience_raw"]),
        "qualification": raw["qualification"],
        "joined": extract.parse_date(raw["joined_raw"] or ""),
        "wage_group": raw["wage_group"],
    }


# --------------------------------------------------------------------------
# tender dossier Annexure A -> business_units
# --------------------------------------------------------------------------

# The column header. It is a line of its own in the column-per-line rendering
# ("Head-count") and part of the header row in a single-line rendering
# ("Unit   Scale   Head-count"), so this is a search, bounded to a short line so
# it cannot fire on prose that happens to mention head-count.
_HEADCOUNT_HEADER = re.compile(r"head[\s-]?counts?\b", re.IGNORECASE)
_INT = re.compile(r"^\d{1,6}$")
# A scale is a short word or two ("enterprise", "mega", "sme"). Deliberately not
# an enumeration of the values seen — a new scale word in the hidden corpus must
# still parse.
_SCALE = re.compile(r"^[A-Za-z][A-Za-z &/-]{1,24}$")
_FOOTER = re.compile(r"^(DOC-|Page \d|Annexure\b)", re.IGNORECASE)


def business_units(text: str) -> list[dict]:
    """The Annexure A "Business Units" table: Unit / Scale / Head-count.

    Two readings are tried, in the order the corpus makes them likely: the
    column-per-line rendering that the layout-preserving extractor produces from
    the shipped dossiers, and a single-line-per-row fallback for a dossier whose
    table columns survive on one line.
    """
    lines = [ln.strip() for ln in text.replace("\f", "\n").split("\n")]
    start = next((i + 1 for i, ln in enumerate(lines)
                  if len(ln) < 60 and _HEADCOUNT_HEADER.search(ln)), None)
    if start is None:
        return []

    rows, i = [], start
    while i + 2 < len(lines):
        unit, scale, head = lines[i], lines[i + 1], lines[i + 2]
        if not (unit and _SCALE.match(scale) and _INT.match(head)) or _FOOTER.match(unit):
            break
        rows.append({"unit": unit, "scale": scale.lower(), "headcount": int(head)})
        i += 3
    if rows:
        return rows

    inline = re.compile(r"^(?P<unit>\S.*?)\s{2,}(?P<scale>[A-Za-z][A-Za-z &/-]*?)\s{2,}(?P<hc>\d{1,6})$")
    for ln in lines[start:]:
        if _FOOTER.match(ln):
            break
        if m := inline.match(ln):
            rows.append({"unit": m.group("unit").strip(),
                         "scale": m.group("scale").strip().lower(),
                         "headcount": int(m.group("hc"))})
    return rows


# --------------------------------------------------------------------------
# name joins
# --------------------------------------------------------------------------

def norm_name(name: str | None) -> str:
    """Fold a person's name to a join key.

    The three views of a person (CV header, credential certificate, company
    completion certificate's Project Lead) agree character-for-character today,
    so this only folds case and whitespace. It exists so that the day one view
    renders "ASHA NAIR" or "Asha  Nair" the join does not silently lose a
    person — the same failure class as decision_log.md s2, where a wrong value
    cost more than a missing one.
    """
    return re.sub(r"\s+", " ", (name or "").strip().lower())


# --------------------------------------------------------------------------
# read-only cross-check oracles (NOT fact sources — other modules own these)
# --------------------------------------------------------------------------

# "Project Manager - 28 yrs - Diploma Civil - on rolls since [September 18, 2013]"
# The joining date wraps to the next line about half the time, and the page
# footer is glued to the front of the following name ("Page 37 of 48Priya
# Gupta"), so this is scanned line by line rather than as one multiline match.
_ANNEX_E = re.compile(
    r"^(?P<designation>[A-Z][A-Za-z ]+?)\s*[^\w\s]\s*(?P<years>\d{1,3})\s*yrs?\s*[^\w\s]\s*"
    r"(?P<qualification>.+?)\s*[^\w\s]\s*on rolls since\s*(?P<joined>.*)$")
_PAGE_GLUE = re.compile(r"^(?:DOC-\S+|Page\s+\d+\s+of\s+\d+)+", re.IGNORECASE)

_HEADLINE = re.compile(r"(\d{2,6})\s+(?:personnel on rolls|engineers available)", re.IGNORECASE)


def dossier_key_personnel(text: str) -> dict[str, dict]:
    """Annexure E restates each CV's headline fields. Independent oracle only.

    Annexure E is a *second rendering of the same 39 people*, so agreeing with it
    is evidence the CV parse is right; disagreeing is evidence one of them is
    wrong. It is never written into a fact — one fact, one owner.
    """
    lines = [ln.strip() for ln in text.replace("\f", "\n").split("\n")]
    out = {}
    for i, line in enumerate(lines):
        m = _ANNEX_E.match(line)
        if not m or i == 0:
            continue
        person = _PAGE_GLUE.sub("", lines[i - 1]).strip()
        if not person:
            continue
        joined = m.group("joined")
        if not extract.parse_date(joined):  # the date wrapped to the next line
            joined = f"{joined} {lines[i + 1] if i + 1 < len(lines) else ''}"
        out[norm_name(person)] = {
            "person": person,
            "designation": m.group("designation").strip(),
            "experience_years": int(m.group("years")),
            "qualification": m.group("qualification").strip(),
            "joined": extract.parse_date(joined),
        }
    return out


def compliance_headline_headcount(corpus: dict[str, str]) -> collections.Counter:
    """Every headline workforce number the 40 compliance matrices restate.

    Both compliance layouts carry it — "486 personnel on rolls" (layout A) and
    "486 engineers available" (layout B). Used to test the dossier head-count
    sum against a source with no shared parsing path.
    """
    seen = collections.Counter()
    for text in pdftext.docs_of_type(corpus, "compliance_matrix").values():
        for m in _HEADLINE.finditer(re.sub(r"\s+", " ", text)):
            seen[int(m.group(1))] += 1
    return seen


def readme_employee_count() -> int | None:
    """The "Employees on record" row of dataset/README.md — the external oracle."""
    if not README.exists():
        return None
    m = re.search(r"\|\s*Employees on record\s*\|\s*([\d,]+)\s*\|",
                  README.read_text(encoding="utf-8"))
    return int(m.group(1).replace(",", "")) if m else None


def credential_holders(corpus: dict[str, str]) -> dict[str, list[dict]]:
    """Credential certificates keyed by folded holder name — cross-check only.

    `extract.personnel_certificate` reads only the label/value layout and
    returns person=credential=credential_id=None for the 20 certificates in the
    "This credential is conferred upon" layout (audited: 28/48 populated). Until
    that is patched in `extract.py`, this second reading exists so the CV -> the
    credential join can be measured rather than assumed. Delete it once
    `personnel` is complete; the audit prints the gap between the two readings.
    """
    raw = []
    for doc_id, text in pdftext.docs_of_type(corpus, "personnel_certificate").items():
        person = extract.label_value(text, "This is to certify that")
        credential = extract.label_value(text, "Credential Type")
        credential_id = extract.label_value(text, "Credential ID", "Certificate No.")
        if not person:
            if m := re.search(r"This credential is conferred upon\s*\n(.+)", text):
                person = m.group(1).strip()
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            credential = credential or (lines[2] if len(lines) > 2 else None)
        if person:
            raw.append((doc_id, person.strip(), credential, credential_id))

    label = _credential_labels(c for _, _, c, _ in raw)
    out: dict[str, list[dict]] = collections.defaultdict(list)
    for doc_id, person, credential, credential_id in raw:
        out[norm_name(person)].append({
            "person": person,
            "credential": label.get(credential_key(credential)),
            "credential_id": credential_id,
            "source_doc": doc_id,
        })
    return dict(out)


def credential_key(name: str | None) -> str | None:
    """Fold a credential name for comparison: "SIX SIGMA BLACK BELT" == "Six Sigma
    Black Belt", and "PMP CERTIFICATION" == "PMP"."""
    if not name:
        return None
    cleaned = re.sub(r"\s+certification$", "", name.strip(), flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "", cleaned.lower()) or None


def _credential_labels(names) -> dict[str, str]:
    """Pick one display label per credential, from the renderings the corpus uses.

    The two certificate layouts shout differently — layout B prints "SIX SIGMA
    BLACK BELT", layout A prints "Six Sigma Black Belt". Rather than guess a
    casing rule (which turns "PMP" into "Pmp", or leaves "BELT" shouting), the
    mixed-case rendering the corpus itself supplies is preferred, and title case
    is only a fallback for a credential that appears in caps everywhere.
    """
    seen: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for name in names:
        if key := credential_key(name):
            seen[key][re.sub(r"\s+certification$", "", name.strip(), flags=re.IGNORECASE)] += 1
    labels = {}
    for key, counts in seen.items():
        mixed = [n for n in counts if not n.isupper()]
        if mixed:
            labels[key] = max(mixed, key=lambda n: counts[n])
        else:
            best = max(counts, key=lambda n: counts[n])
            labels[key] = best if len(best.split()) == 1 else best.title()
    return labels


# --------------------------------------------------------------------------
# module contract
# --------------------------------------------------------------------------

def build(corpus: dict[str, str]) -> dict[str, list[dict]]:
    """corpus is doc_id -> text. Returns the `staff` and `business_units` blocks."""
    staff = []
    for doc_id, text in pdftext.docs_of_type(corpus, "cv").items():
        record = curriculum_vitae(text)
        record["source_doc"] = doc_id
        staff.append(record)
    staff.sort(key=lambda r: (r["employee_id"] or "", r["source_doc"]))

    # All six dossiers print the same table; one record per unit, keyed on the
    # unit name, sourced from the first dossier in id order. Disagreement between
    # dossiers would be a real fact about the corpus, so audit() reports it
    # rather than this silently taking the first reading.
    units: dict[str, dict] = {}
    for doc_id, text in pdftext.docs_of_type(corpus, "tender_dossier").items():
        for row in business_units(text):
            units.setdefault(row["unit"], {**row, "source_doc": doc_id})

    return {"staff": staff, "business_units": list(units.values())}


def audit(blocks: dict[str, list[dict]]) -> None:
    """Coverage per block against dataset/README.md and the filesystem."""
    staff = blocks["staff"]
    units = blocks["business_units"]
    corpus = pdftext.load_corpus()
    n_cv = len(list((pdftext.DOCS / "cv").glob("*.pdf"))) or len(pdftext.docs_of_type(corpus, "cv"))
    n_dossier = len(list((pdftext.DOCS / "tender_dossier").glob("*.pdf")))

    def line(label, got, want):
        flag = "" if got == want else "   <-- INCOMPLETE"
        print(f"{label:44s} {got:>6}   (expected {want}){flag}")

    print("== people.py ==")
    line("staff records (one per cv pdf)", len(staff), n_cv)
    line("business units (dossier Annexure A)", len(units), 6)

    print("\nfield coverage — staff")
    for f in ("person", "employee_id", "designation", "business_unit",
              "experience_years", "qualification", "joined", "wage_group", "source_doc"):
        n = sum(1 for r in staff if r.get(f) not in (None, ""))
        flag = "" if n == len(staff) else "   <-- INCOMPLETE"
        print(f"  {f:18s} {n:3d}/{len(staff)}{flag}")
    print("field coverage — business_units")
    for f in ("unit", "scale", "headcount", "source_doc"):
        n = sum(1 for r in units if r.get(f) not in (None, ""))
        flag = "" if n == len(units) else "   <-- INCOMPLETE"
        print(f"  {f:18s} {n:3d}/{len(units)}{flag}")

    layouts = collections.Counter(cv_layout(t) for t in pdftext.docs_of_type(corpus, "cv").values())
    print(f"\ncv layouts: {dict(layouts)}"
          f"{'   <-- INCOMPLETE' if layouts.get('unknown') else ''}")

    # ---- the distinction this module exists to protect -------------------
    total = sum(u["headcount"] for u in units)
    readme = readme_employee_count()
    print("\n-- CORPUS-WIDE TRUTH vs SAMPLE SIZE ------------------------------")
    print(f"  employees on record (sum of business-unit head-counts) = {total}"
          f"   README says {readme}{'  OK' if total == readme else '  <-- MISMATCH'}")
    print(f"  compliance-matrix headline restatements: {dict(compliance_headline_headcount(corpus))}")
    print(f"  staff records (CVs on file)                            = {len(staff)}"
          f"   <- a SAMPLE ({len(staff) * 100 // (total or 1)}% of the workforce)")
    print("  'how many employees / engineers / personnel'  -> 486 (sum of head-counts)")
    print(f"  'how many key personnel / project managers with a CV' -> {len(staff)}")
    print("  any per-person attribute (experience, qualification, wage group,")
    print(f"  joining date, credential) is only knowable for the {len(staff)}.")

    print("\nhead-count by business unit")
    for u in sorted(units, key=lambda r: -r["headcount"]):
        n_cv_unit = sum(1 for s in staff if s["business_unit"] == u["unit"])
        print(f"  {u['unit']:38s} {u['headcount']:4d}  ({u['scale']}, {n_cv_unit} CVs on file)")

    # all six dossiers must print the same table
    per_dossier = {d: {r["unit"]: r["headcount"] for r in business_units(t)}
                   for d, t in pdftext.docs_of_type(corpus, "tender_dossier").items()}
    distinct = {tuple(sorted(v.items())) for v in per_dossier.values()}
    print(f"\ndossiers read: {len(per_dossier)}/{n_dossier}; "
          f"distinct business-unit tables: {len(distinct)}"
          f"{'  (all agree)' if len(distinct) == 1 else '   <-- DISAGREEMENT'}")

    # ---- distinct-value inventories --------------------------------------
    print("\ndistinct values")
    for f in ("designation", "business_unit", "qualification", "wage_group"):
        c = collections.Counter(r[f] for r in staff)
        note = "   <-- zero variance in this corpus" if len(c) == 1 else ""
        print(f"  {f} ({len(c)}){note}")
        for k, v in c.most_common():
            print(f"      {v:3d}  {k}")
    years = sorted(r["experience_years"] for r in staff if r["experience_years"] is not None)
    reference = as_of(corpus)
    tenures = sorted(t for r in staff if (t := tenure_years(r["joined"], reference)) is not None)
    for name, series in (("experience_years (whole career)", years),
                         (f"tenure years (joined -> {reference}, the corpus's own 'today')", tenures)):
        if not series:
            continue
        print(f"  {name}: n={len(series)} min={series[0]} max={series[-1]} "
              f"mean={sum(series) / len(series):.2f} "
              f"median={series[len(series) // 2]} total={sum(series)}")
        for t in (5, 10, 15, 20, 25, 30):
            print(f"      > {t:2d}: {sum(1 for y in series if y > t):2d}    "
                  f">= {t:2d}: {sum(1 for y in series if y >= t):2d}")

    # ---- Annexure E: an independent rendering of the same 39 people -------
    annex: dict[str, dict] = {}
    for text in pdftext.docs_of_type(corpus, "tender_dossier").values():
        annex.update(dossier_key_personnel(text))
    if annex:
        matched = [r for r in staff if norm_name(r["person"]) in annex]
        bad = [(r["person"], f, r[f], annex[norm_name(r["person"])][f])
               for r in matched for f in ("experience_years", "qualification", "joined")
               if r[f] != annex[norm_name(r["person"])][f]]
        print(f"\ncv vs dossier Annexure E (independent rendering): "
              f"{len(matched)}/{len(staff)} people matched, {len(bad)} field disagreements"
              f"{'' if not bad else '   <-- INCOMPLETE'}")
        for row in bad[:5]:
            print(f"    {row[0]}  {row[1]}: cv={row[2]!r} annexure={row[3]!r}")

    # ---- name joins the router depends on --------------------------------
    names = {norm_name(r["person"]) for r in staff}
    creds = credential_holders(corpus)
    n_certs = sum(len(v) for v in creds.values())
    print(f"\nname joins")
    print(f"  credential certificates read here: {n_certs} held by {len(creds)} people; "
          f"{len(set(creds) & names)}/{len(creds)} join to a staff record")
    by_cred = collections.Counter(c["credential"] for v in creds.values() for c in v)
    for k, v in by_cred.most_common():
        holders = len({p for p, cs in creds.items() if any(c["credential"] == k for c in cs)})
        note = "   <-- held by EVERY staff member: filtering on it is a no-op" \
            if holders == len(staff) else ""
        print(f"      {v:3d} certificates  {holders:3d} holders  {k}{note}")

    stale = sum(1 for text in pdftext.docs_of_type(corpus, "personnel_certificate").values()
                if extract.personnel_certificate(text)["person"])
    if stale < n_certs:
        print(f"      NOTE: extract.personnel_certificate populates person on only "
              f"{stale}/{n_certs} — the `personnel` block is short {n_certs - stale} "
              f"holders. See people.py:credential_holders.   <-- INCOMPLETE")

    facts = REPO / "results" / "raw" / "facts.json"
    if facts.exists():
        works = json.loads(facts.read_text(encoding="utf-8"))["works"]
        leads = {norm_name(w["project_lead"]) for w in works if w.get("project_lead")}
        print(f"  project leads in works: {len(leads)}; "
              f"{len(leads & names)}/{len(leads)} join to a staff record"
              f"{'' if leads <= names else '   <-- INCOMPLETE'}")
        led = {n for n in names if n in leads}
        print(f"  staff who lead no completed work: {len(names - led)} "
              f"({', '.join(sorted(r['person'] for r in staff if norm_name(r['person']) not in leads)) or '-'})")
        total = sum(w["value"] for w in works if w.get("value"))
        for key in sorted({credential_key(c["credential"]) for v in creds.values() for c in v}):
            holders = {p for p, cs in creds.items()
                       if any(credential_key(c["credential"]) == key for c in cs)}
            value = sum(w["value"] for w in works
                        if norm_name(w.get("project_lead")) in holders and w.get("value"))
            label = next(c["credential"] for v in creds.values() for c in v
                         if credential_key(c["credential"]) == key)
            note = "   <-- equals the WHOLE portfolio; the filter selects nothing" \
                if value == total else ""
            print(f"  project leads holding {label}: {len(holders & leads):2d}; "
                  f"value of works they led: {value:,}{note}")


if __name__ == "__main__":
    blocks = build(pdftext.load_corpus())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8")
    audit(blocks)
    print(f"\nwritten -> {OUT}")
