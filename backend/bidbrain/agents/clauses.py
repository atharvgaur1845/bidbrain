"""Tender text -> typed clauses. LLM first (Qwen3, JSON), deterministic fallback.

The fallback is not a toy: it covers the phrasing of the corpus's own 491
compliance rows and standard CPWD/NHAI eligibility wording, so the pipeline
still completes — and the demo still works — when no LLM is reachable.
"""
from __future__ import annotations

import json
import re

from bidbrain.agents import providers
from bidbrain.agents.prompts import CLAUSE_SYSTEM

CATS = ["roads maintenance", "roads highways", "expressways", "large bridges", "bridges flyovers", "tunnels",
        "water treatment", "water supply", "sewerage drainage", "irrigation", "small buildings", "buildings",
        "industrial epc"]
CAT_WORDS = {"road": ["roads highways"], "highway": ["roads highways"], "expressway": ["expressways"],
             "bridge": ["bridges flyovers", "large bridges"], "flyover": ["bridges flyovers"],
             "tunnel": ["tunnels"], "water treatment": ["water treatment"], "water supply": ["water supply"],
             "sewer": ["sewerage drainage"], "drainage": ["sewerage drainage"], "irrigation": ["irrigation"],
             "building": ["buildings", "small buildings"], "epc": ["industrial epc"]}
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
WORDNUM = {w: i for i, w in enumerate(_ONES)} | {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
# a count/years token: '7', 'seven', 'twenty-five'; an optional gloss '(seven)' / '(7)' may follow
NUM = r"(\d+|(?:twenty|thirty|forty|fifty)(?:[\s-](?:one|two|three|four|five|six|seven|eight|nine)\b)?|" \
      + "|".join(sorted(WORDNUM, key=len, reverse=True)) + r")\b"
GLOSS = r"(?:\s*\(\s*\w+\s*\))?"
LAST_YEARS = (rf"(?:last|past|preceding)\s+{NUM}{GLOSS}\s+(?:(?:consecutive|completed|preceding)\s+)*"
              r"(?:(?:financial|fiscal|calendar)\s+)?years")
N_YEARS = rf"\b{NUM}{GLOSS}\s*\+?\s*(?:(?:full|complete|completed|calendar)\s+)?years?\b"
UNIT = {"cr": 10_000_000, "crore": 10_000_000, "lakh": 100_000, "lac": 100_000, "million": 1_000_000,
        "mn": 1_000_000, "billion": 1_000_000_000, "bn": 1_000_000_000}
MONEY_RE = re.compile(r"(?:(?P<cur>INR|Rs\.?|₹)\s*)?(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
                      r"(?P<unit>crores?\b|cr\b\.?|lakhs?\b|lacs?\b|million\b|mn\b|billion\b|bn\b)?", re.I)
# an item marker at the start of a line: 2.1  2.1.  3)  (iv)  iv.  (a)  a)  A.
ITEM = r"(?:\(?(?:[ivxl]+|[a-z]|\d+(?:\.\d+)*)[.)]|\d+(?:\.\d+)*)"
HEADER_RE = re.compile(r"^(?:section|eligibility|qualifying|qualification|pre-?qual\w*)\b[^\n:]*(?::\s*|$)", re.I)


def _wordnum(tok: str) -> int | None:
    tok = tok.lower().replace("-", " ")
    if tok.isdigit():
        return int(tok)
    parts = tok.split()
    if all(p in WORDNUM for p in parts):
        return sum(WORDNUM[p] for p in parts)
    return None


def _amounts(s: str) -> list[int]:
    """Every money amount in order: needs a currency prefix or a crore/lakh/million unit."""
    out = []
    for m in MONEY_RE.finditer(s):
        if not (m.group("cur") or m.group("unit")):
            continue
        v = float(m.group("num").replace(",", ""))
        unit = (m.group("unit") or "").lower().rstrip(".")
        unit = {"crores": "crore", "lakhs": "lakh", "lacs": "lac"}.get(unit, unit)
        out.append(int(round(v * UNIT.get(unit, 1))))
    return out


def _money(s: str) -> int | None:
    a = _amounts(s)
    return a[0] if a else None


def _cats(s: str) -> list[str]:
    low = s.lower().replace("&", "and")
    out = [c for c in CATS if c in low]
    for w, cs in CAT_WORDS.items():          # "roads, highways or expressways", "sewerage and drainage"
        if w in low:
            # a size-qualified category only when the size is written: "bridge" is not "large bridges",
            # "buildings" is not "small buildings" (those are different credentials in the corpus)
            out += [c for c in cs if c not in out and not (c == "large bridges" and "large" not in low
                                                            and "major" not in low)
                    and not (c == "small buildings" and "small" not in low)]
    return out


def _num(pattern: str, s: str, default=None):
    m = re.search(pattern, s, re.I)
    if not m:
        return default
    v = _wordnum(m.group(1))
    return default if v is None else v


def _unparsed(cid: str, raw: str, kind: str, what: str) -> dict:
    """A clause whose threshold the rules cannot read must not pass vacuously: it becomes a clause for a
    human to confirm (kind 'other' -> DECLARE), carrying what was not understood."""
    return {"id": cid, "text": raw, "kind": "other", "params": {"unparsed": kind, "reason": f"could not read {what}"}}


def _chunks(text: str) -> list[str]:
    """Numbered/lettered items; un-numbered prose is split into sentences-per-line, minus headers."""
    parts = re.split(rf"\n\s*(?={ITEM}\s)", "\n" + text, flags=re.I)
    out = []
    for p in (p.strip() for p in parts):
        if not p:
            continue
        if re.match(rf"{ITEM}\s", p, re.I):
            out.append(p)
            continue
        # preamble / un-numbered paragraph: drop header lines, keep each requirement line (re-joining
        # hard-wrapped lines that do not end a sentence)
        buf = ""
        for line in (l.strip() for l in p.splitlines()):
            if not line:
                continue
            h = HEADER_RE.match(line)
            if h and not buf:
                line = line[h.end():].strip()
                if not line:
                    continue
            buf = f"{buf} {line}".strip()
            if re.search(r"[.;:]$", buf):
                out.append(buf)
                buf = ""
        if buf:
            out.append(buf)
    return out


def _turnover_sentence(raw: str) -> str:
    sents = re.split(r"(?<=[.;])\s+(?=[A-Z(])", raw)
    return " ".join(x for x in sents if "turnover" in x.lower()) or raw


def parse_rules(text: str) -> list[dict]:
    """Split on numbered/lettered items and classify each line."""
    clauses = []
    for i, raw in enumerate(_chunks(text)):
        if len(raw) < 12:
            continue
        low = raw.lower()
        ref = re.match(r"\(?([ivxl]+|[a-z]|\d+(?:\.\d+)*)[.)]?\s", raw, re.I)
        cid = ref.group(1) if ref else f"C{i}"
        money = _money(raw)
        if "turnover" in low:
            ts = _turnover_sentence(raw)
            money = _money(ts) or money
            if not money:
                clauses.append(_unparsed(cid, raw, "turnover", "the turnover amount"))
                continue
            yrs = _num(LAST_YEARS, ts, 3) or 3
            each = re.search(r"\b(?:each|every)\b", ts, re.I)
            clauses.append({"id": cid, "text": raw, "kind": "turnover",
                            "params": {"min_each_year" if each else "min_average": money, "years": yrs}})
        elif re.search(r"\bpmp\b|six\s*sigma|project manager|key personnel", low):
            cred = "PMP" if re.search(r"\bpmp\b|project management professional", low) else (
                "Six Sigma" if re.search(r"six\s*sigma", low) else None)
            if not cred:
                clauses.append(_unparsed(cid, raw, "key_personnel", "a PMP / Six Sigma credential"))
                continue
            minx = _num(N_YEARS, raw, None)
            if minx is None and re.search(r"\byears?\b", low):
                clauses.append(_unparsed(cid, raw, "key_personnel", "the minimum years of experience"))
                continue
            p = {"credential": cred, "min_experience_years": minx,
                 "count": _num(rf"\b{NUM}{GLOSS}\s+(?:\w+\s+)?(?:project|quality|site)?\s*managers\b", raw, 1) or 1}
            if cats := _cats(raw.split("experience")[-1] if "experience" in low else raw):
                p["categories"] = cats
            clauses.append({"id": cid, "text": raw, "kind": "key_personnel", "params": p})
        elif "similar" in low or ("experience" in low and ("work" in low or "project" in low)):
            p = {"categories": _cats(raw), "lookback_years": _num(LAST_YEARS, raw, 7) or 7}
            amts = _amounts(raw)
            if re.search(r"\b(?:80|60|40)\s*%", raw):
                est = re.search(r"estimated\s+cost", raw, re.I)
                after = _amounts(raw[est.end():est.end() + 60]) if est else []
                p["estimated_cost"] = after[0] if after else money
            elif len(amts) == 3 and amts[0] > amts[1] > amts[2] > 0:
                p["estimated_cost"] = int(round(amts[0] / 0.8))      # CPWD 80/60/40 written as amounts
            elif money:
                p.update(min_value=money,
                         count=_num(rf"\b{NUM}{GLOSS}\s+(?:\w+\s+){{0,3}}works?\b", raw, 1) or 1)
            if not (p.get("estimated_cost") or p.get("min_value")):
                clauses.append(_unparsed(cid, raw, "similar_works", "a work value or estimated cost"))
                continue
            levels = [lv for lv, words in {"central": ["central"], "state": ["state"], "psu": ["psu"],
                                           "municipal": ["municipal", "urban local bod"]}.items()
                      if any(w in low for w in words)]
            if levels:
                p["govt_levels"] = levels
            if "reference letter" in low:
                p["require_reference"] = False   # documents to enclose, checked by the AbsenceAuditor
            clauses.append({"id": cid, "text": raw, "kind": "similar_works", "params": p})
        elif re.search(r"iso\s*\d+|cpwd|nabl", low):
            for std in re.findall(r"(ISO\s*\d+|CPWD|NABL)", raw, re.I):
                clauses.append({"id": f"{cid}-{std.replace(' ', '')}", "text": raw, "kind": "certification",
                                "params": {"standard": std.upper().replace("ISO", "ISO ").replace("  ", " ").strip()}})
        elif re.search(r"\byears?\b", low) and ("business" in low or "existence" in low or "incorporat" in low):
            n = _num(N_YEARS, raw, None)
            if n is None:
                clauses.append(_unparsed(cid, raw, "years_in_business", "the minimum years"))
            else:
                clauses.append({"id": cid, "text": raw, "kind": "years_in_business", "params": {"min_years": n}})
        else:
            clauses.append({"id": cid, "text": raw, "kind": "other", "params": {}})
    return clauses


# a clause of these kinds without one of these params would pass for anyone
THRESHOLDS = {"turnover": ("min_average", "min_each_year"), "similar_works": ("estimated_cost", "min_value"),
              "years_in_business": ("min_years",)}


def parse_llm(text: str) -> list[dict]:
    rep = providers.chat([{"role": "system", "content": CLAUSE_SYSTEM},
                          {"role": "user", "content": text}], max_tokens=2000)
    body = rep.content.strip()
    m = re.search(r"\{.*\}", body, re.S)
    data = json.loads(m.group(0) if m else body)
    out = []
    for c in data.get("clauses", []):
        if c.get("kind") not in {"similar_works", "turnover", "key_personnel", "certification",
                                 "years_in_business", "other"}:
            c["kind"] = "other"
        cl = {"id": str(c.get("id", len(out) + 1)), "text": c.get("text", ""), "kind": c["kind"],
              "params": c.get("params") or {}}
        need = THRESHOLDS.get(cl["kind"])
        if need and not any(cl["params"].get(k) for k in need):      # no threshold: must not pass vacuously
            cl = _unparsed(cl["id"], cl["text"], cl["kind"], " / ".join(need))
        out.append(cl)
    return out


def parse(text: str, prefer_llm: bool = True) -> tuple[list[dict], str]:
    if prefer_llm:
        try:
            cl = parse_llm(text)
            if cl:
                return cl, "llm"
        except Exception:
            pass
    return parse_rules(text), "rules"
