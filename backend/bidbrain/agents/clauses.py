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


def _money_spans(s: str) -> list[tuple[int, int, int]]:
    """(start, end, rupees) for every money amount, the same reading as `_amounts`."""
    out = []
    for m in MONEY_RE.finditer(s):
        if not (m.group("cur") or m.group("unit")):
            continue
        v = float(m.group("num").replace(",", ""))
        unit = (m.group("unit") or "").lower().rstrip(".")
        unit = {"crores": "crore", "lakhs": "lakh", "lacs": "lac"}.get(unit, unit)
        out.append((m.start("num") if not m.group("cur") else m.start(), m.end(), int(round(v * UNIT.get(unit, 1)))))
    return out


PCT_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:%|per\s*cent\b|percent\b)", re.I)
EST_RE = re.compile(r"estimated\s+(?:cost|value)(?:\s+of\s+the\s+(?:work|project|contract))?[\s(:,-]*"
                    r"(?:(?:of|is|i\.e\.?|being|namely)\s+)?", re.I)


def _estimate(s: str) -> tuple[int | None, int | None]:
    """The estimated cost a percentage refers to ('...of the estimated cost of INR 250 Cr'), and where it starts."""
    for m in EST_RE.finditer(s):
        for a, _, v in _money_spans(s):
            if m.end() <= a <= m.end() + 12:
                return v, a
    return None, None


def _pct_of(s: str) -> list[int]:
    """'X% of [the estimated cost of] INR Y Cr' -> X% of Y, for each such phrase."""
    out, spans = [], _money_spans(s)
    for m in PCT_RE.finditer(s):
        after = s[m.end():m.end() + 70]
        if not re.match(r"\s*of\b", after, re.I):
            continue
        nxt = next(((a, v) for a, _, v in spans if a >= m.end()), None)
        if nxt and nxt[0] - m.end() <= 70 and not PCT_RE.search(s[m.end():nxt[0]]):
            out.append(int(round(float(m.group(1)) / 100 * nxt[1])))
    return out


def _threshold(s: str) -> int | None:
    """The amount a clause requires: 'X% of INR Y Cr' is X% of Y, otherwise the first amount written."""
    pct = _pct_of(s)
    return pct[0] if len(pct) == 1 else _money(s)


def unambiguous_amount(s: str) -> bool:
    """One amount and no percentage in the clause: the rule parser's reading of the amount cannot be wrong
    about *which* number is meant, so it may correct an LLM reading of the same text."""
    return len(_amounts(s)) == 1 and not PCT_RE.search(s)


def _opt_count(seg: str) -> int:
    """The number of works one alternative asks for: 'two building works each ...' / '(b) two of ...'."""
    ms = list(re.finditer(rf"(?<![\d.]){NUM}{GLOSS}\s+(?:[\w&/-]+\s+){{0,4}}?works?\b", seg, re.I))
    if not ms:          # 'or two of INR 54 Cr': the last count word that is not an item marker, years or money
        ms = list(re.finditer(rf"(?<![\d.]){NUM}(?![.)]|\s*(?:years?|%|per|cr\b|crores?|lakhs?))", seg, re.I))
    v = _wordnum(ms[-1].group(1)) if ms else None
    return v or 1


def similar_options(raw: str) -> tuple[list[tuple[int, int]], int | None] | None:
    """Either/or alternatives of a similar-works clause, in the tender's order, as (count, min_value):
    'one work of at least INR 50 Cr, or two works each of at least INR 40 Cr' -> [(1, 50 Cr), (2, 40 Cr)].
    Percentages are resolved against the estimated cost. Only when every pair of consecutive amounts is
    separated by an 'or' (so 'X and a total of Y' is not read as alternatives). Returns (options, estimate)."""
    est, est_at = _estimate(raw)
    toks = [(a, b, v) for a, b, v in _money_spans(raw) if a != est_at]
    for m in PCT_RE.finditer(raw):
        if not est:
            return None             # a percentage of an estimate the clause does not state
        toks.append((m.start(), m.end(), int(round(float(m.group(1)) / 100 * est))))
    toks.sort()
    if len(toks) < 2:
        return None
    starts = [0]
    for (_, prev_end, _), (a, _, _) in zip(toks, toks[1:]):
        ors = list(re.finditer(r"\bor\b", raw[prev_end:a], re.I))
        if not ors:
            return None
        starts.append(prev_end + ors[-1].end())
    return [(_opt_count(raw[st:a]), v) for st, (a, _, v) in zip(starts, toks)], est


def cpwd_estimate(opts: list[tuple[int, int]], est: int | None = None) -> int | None:
    """1 work >= 80% / 2 >= 60% / 3 >= 40% of one estimate: the CPWD rule, stored as estimated_cost."""
    by = sorted(opts)
    if [c for c, _ in by] != [1, 2, 3]:
        return None
    implied = [v / f for (_, v), f in zip(by, (0.8, 0.6, 0.4))]
    if max(implied) - min(implied) > 0.005 * max(implied):
        return None
    if est and abs(est - implied[0]) <= 0.005 * est:
        return est
    return int(round(implied[0]))


def _cats(s: str) -> list[str]:
    low = s.lower().replace("&", "and")
    out = [c for c in CATS if c in low]
    for w, cs in CAT_WORDS.items():          # "roads, highways or expressways", "sewerage and drainage"
        if w in low:
            # a size-qualified category only when the size is written: "bridge" is not "large bridges",
            # "buildings" is not "small buildings" (those are different credentials in the corpus); and a
            # bridge that is only ever written as large / major is not also "bridges flyovers"
            out += [c for c in cs if c not in out and not (c == "large bridges" and "large" not in low
                                                            and "major" not in low)
                    and not (c == "small buildings" and "small" not in low)
                    and not (w == "bridge" and c == "bridges flyovers"
                             and not re.search(r"(?<!large )(?<!major )\bbridges?\b", low))]
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
            money = _threshold(ts) or _threshold(raw)
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
            opts = similar_options(raw)
            if opts and len(opts[0]) >= 2:
                # either/or alternatives: any one is enough (never two must-pass clauses)
                est = cpwd_estimate(*opts)
                if est:
                    p["estimated_cost"] = est
                else:
                    p["options"] = [{"count": c, "min_value": v} for c, v in opts[0]]
            elif re.search(r"\b(?:80|60|40)\s*%", raw):
                est = re.search(r"estimated\s+cost", raw, re.I)
                after = _amounts(raw[est.end():est.end() + 60]) if est else []
                p["estimated_cost"] = after[0] if after else money
            elif len(amts) == 3 and amts[0] > amts[1] > amts[2] > 0:
                p["estimated_cost"] = int(round(amts[0] / 0.8))      # CPWD 80/60/40 written as amounts
            elif money:
                p.update(min_value=_threshold(raw),
                         count=_num(rf"\b{NUM}{GLOSS}\s+(?:\w+\s+){{0,3}}works?\b", raw, 1) or 1)
            if not (p.get("estimated_cost") or p.get("min_value") or p.get("options")):
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
THRESHOLDS = {"turnover": ("min_average", "min_each_year"), "similar_works": ("estimated_cost", "min_value", "options"),
              "years_in_business": ("min_years",)}


_SYNONYMS = {"min_experience": "min_experience_years", "experience_years": "min_experience_years",
             "min_years_experience": "min_experience_years", "category": "categories", "lookback": "lookback_years",
             "years_lookback": "lookback_years", "period_years": "lookback_years", "min_turnover": "min_average",
             "average_turnover": "min_average", "num_people": "count", "certificate": "standard",
             "estimated_value": "estimated_cost", "cost": "estimated_cost", "value": "min_value",
             "alternatives": "options"}
REFERENCE_WORDS = re.compile(r"referen|letter|testimonial", re.I)


def _llm_options(raw) -> list[tuple[int, int]]:
    out = []
    for o in raw if isinstance(raw, list) else []:
        try:
            if isinstance(o, dict) and int(o.get("min_value") or 0) > 0:
                out.append((max(1, int(o.get("count") or 1)), int(o["min_value"])))
        except (TypeError, ValueError):
            continue
    return out


def _sanitize(kind: str, params: dict, text: str = "") -> dict:
    """Coerce an LLM's clause params onto the engine's schema. Models invent shapes ('min_experience', a bare
    'category'); qualify_clause rejects unknown keys, so they are mapped here, and whatever cannot be mapped is
    dropped rather than crashing the run. Never widens the clause: either/or alternatives stay alternatives
    (`options`, any one passing), named categories stay as named, and `require_reference` is kept only when
    the clause text asks for a reference / letter / testimonial."""
    from bidbrain.tools.proof import PARAMS
    allowed = PARAMS.get(kind)
    if not allowed or not isinstance(params, dict):
        return params if isinstance(params, dict) else {}
    p = {_SYNONYMS.get(k, k): v for k, v in params.items()}
    if kind == "similar_works" and "options" in p:
        opts = _llm_options(p.pop("options"))
        if p.get("estimated_cost"):
            pass                                            # an explicit estimate is the CPWD rule already
        elif len(opts) >= 2:
            est = cpwd_estimate(opts)                         # 1 x 80% / 2 x 60% / 3 x 40% of the estimate
            p.pop("min_value", None), p.pop("count", None)
            if est:
                p["estimated_cost"] = est
            else:
                p["options"] = [{"count": c, "min_value": v} for c, v in opts]
        elif opts and not p.get("min_value"):
            p["count"], p["min_value"] = opts[0]
    if isinstance(p.get("categories"), str):
        p["categories"] = [p["categories"]]
    if p.get("categories"):
        # a category the corpus already names is kept as written ('large bridges' stays 'large bridges', not
        # also 'bridges flyovers'); only a free word ('roads', 'highway') is mapped onto the corpus's categories
        cats = []
        for w in map(str, p["categories"]):
            norm = re.sub(r"\s+", " ", w.lower().replace("&", " ").replace(",", " ").replace(" and ", " ")).strip()
            hits = [norm] if norm in CATS else _cats(norm)
            cats += [c for c in hits if c not in cats]
        p["categories"] = cats or None
    if p.get("require_reference") and not REFERENCE_WORDS.search(text or ""):
        p.pop("require_reference")                          # the model added a filter the tender does not ask for
    if isinstance(p.get("standard"), list):
        p["standard"] = p["standard"][0] if p["standard"] else None
    return {k: v for k, v in p.items() if k in allowed and v not in (None, "", [])}


_MONEY_KEYS = ("estimated_cost", "min_value", "min_average", "min_each_year")
_YEAR_KEYS = ("min_experience_years", "min_years")


def alternatives(params: dict) -> set[tuple[int, int]] | None:
    """A similar-works threshold as the set of (count, min_value) alternatives it accepts, whichever way it
    is written (CPWD estimate, min_value + count, options)."""
    if params.get("estimated_cost"):
        e = int(params["estimated_cost"])
        return {(1, int(0.8 * e)), (2, int(0.6 * e)), (3, int(0.4 * e))}
    if params.get("options"):
        return {(int(o.get("count") or 1), int(o["min_value"])) for o in params["options"]}
    if params.get("min_value"):
        return {(int(params.get("count") or 1), int(params["min_value"]))}
    return None


def _same_alts(a: set, b: set) -> bool:
    if len(a) != len(b):
        return False
    return all(any(ca == cb and abs(va - vb) <= max(1, 0.005 * max(va, vb)) for cb, vb in b) for ca, va in a)


def _subset_alts(a: set, b: set) -> bool:
    return all(any(ca == cb and abs(va - vb) <= max(1, 0.005 * max(va, vb)) for cb, vb in b) for ca, va in a)


def _reconcile(c: dict, rule: dict) -> dict:
    """Where the rule parser may correct an LLM reading of the same clause text. Only when its own reading
    is unambiguous: a money threshold when the text holds exactly one amount and no percentage; a years
    threshold when the text states one number of years; either/or alternatives when the LLM kept only some
    of the alternatives the text separates with 'or'. Every correction is noted on the clause."""
    text, notes, params = c["text"], [], dict(c["params"])
    if c["kind"] == "similar_works":
        ra, la = alternatives(rule["params"]), alternatives(params)
        if ra and la and len(ra) >= 2 and rule["params"].get("options") and not _same_alts(ra, la) \
                and _subset_alts(la, ra):
            for k in ("estimated_cost", "min_value", "count"):
                params.pop(k, None)
            params["options"] = rule["params"]["options"]
            notes.append(f"either/or: LLM kept {len(la)} of {len(ra)} alternatives -> rules read all {len(ra)}")
    if unambiguous_amount(text):
        for k in _MONEY_KEYS:
            rv, lv = rule["params"].get(k), params.get(k)
            if rv and lv and int(rv) != int(lv):
                params[k] = rv
                notes.append(f"{k}: LLM {int(lv):,} -> rules {int(rv):,} (the clause states one amount)")
    if len(re.findall(N_YEARS, text, re.I)) == 1:
        for k in _YEAR_KEYS:
            rv, lv = rule["params"].get(k), params.get(k)
            if rv and lv and int(rv) != int(lv):
                params[k] = rv
                notes.append(f"{k}: LLM {lv} -> rules {rv}")
    if not notes:
        return c
    return {**c, "params": params, "corrected": "; ".join(filter(None, [c.get("corrected"), *notes]))}


def text_reading(kind: str, text: str) -> dict | None:
    """What the rule parser reads from a clause's own text for the params that decide its verdict (only
    what the text states, not the parser's defaults), or None when it cannot read that kind from the text.
    The Verifier compares this with the params the team actually used."""
    rules = [r for r in parse_rules(text or "") if r["kind"] == kind]
    if not rules:
        return None
    if kind == "certification":
        return {"standards": sorted({r["params"]["standard"] for r in rules})}
    if len(rules) > 1:
        return None                                       # two readings of one text: nothing definite to compare
    rp, out = rules[0]["params"], {}
    stated_window = re.search(LAST_YEARS, text, re.I)
    if kind == "similar_works":
        out["alternatives"] = alternatives(rp)
        if stated_window:
            out["lookback_years"] = rp.get("lookback_years")
    elif kind == "turnover":
        out.update({k: rp[k] for k in ("min_average", "min_each_year") if rp.get(k)})
        if stated_window:
            out["years"] = rp.get("years")
    elif kind == "key_personnel":
        out["credential"] = rp.get("credential")
        if rp.get("min_experience_years") is not None:
            out["min_experience_years"] = rp["min_experience_years"]
    elif kind == "years_in_business":
        out["min_years"] = rp.get("min_years")
    return {k: v for k, v in out.items() if v not in (None, [], set())}


def reading_diff(kind: str, params: dict, text: str) -> list[str]:
    """Differences between a clause's params and what its own text says (empty when they agree, or when the
    rule parser cannot read the text). Money within 0.5%, counts and years exactly."""
    want = text_reading(kind, text)
    if not want:
        return []
    out = []
    if kind == "certification":
        if params.get("standard") and params["standard"] not in want["standards"]:
            out.append(f"standard {params['standard']} vs text {', '.join(want['standards'])}")
        return out
    if "alternatives" in want:
        have = alternatives(params) or set()
        if not _same_alts(have, want["alternatives"]):
            fmt = lambda alts: " or ".join(f"{c} x >= {v / 1e7:.2f} Cr" for c, v in sorted(alts))
            out.append(f"threshold {fmt(have) or 'none'} vs text {fmt(want['alternatives'])}")
    for k in ("min_average", "min_each_year"):
        if k in want and not (params.get(k) and abs(int(params[k]) - want[k]) <= 0.005 * want[k]):
            got = {kk: params.get(kk) for kk in ("min_average", "min_each_year") if params.get(kk)}
            out.append(f"{k} {want[k] / 1e7:.2f} Cr in the text; params {got or 'none'}")
    for k, default in (("lookback_years", 7), ("years", 3), ("min_years", None), ("min_experience_years", None)):
        if k in want and int(params.get(k) or default or 0) != int(want[k]):
            out.append(f"{k} {params.get(k, default)} vs text {want[k]}")
    if "credential" in want and params.get("credential") and \
            params["credential"].lower() not in want["credential"].lower():
        out.append(f"credential {params['credential']} vs text {want['credential']}")
    return out


def _checked(cl: dict) -> list[dict]:
    """An LLM clause the engine can actually evaluate, or the rule parser's reading of the same text, or an
    explicit 'unparsed' clause. A dry run against the graph catches what schema checks miss."""
    from bidbrain.graph.client import Graph
    from bidbrain.tools.proof import qualify_clause
    cands = [cl]
    if cl["kind"] == "certification" and isinstance(cl.get("_standards"), list):     # one clause per standard
        cands = [{**cl, "id": f"{cl['id']}{'abcdefgh'[i]}", "params": {"standard": s}}
                 for i, s in enumerate(cl["_standards"])]
    out = []
    for c in cands:
        c.pop("_standards", None)
        if c["kind"] in ("other",):
            out.append(c)
            continue
        rules = [r for r in parse_rules(c["text"]) if r["kind"] == c["kind"]]
        if len(rules) == 1:
            c = _reconcile(c, rules[0])
        try:
            qualify_clause(Graph(), c["kind"], c["params"])
            out.append(c)
        except Exception as exc:
            out.append({**rules[0], "id": c["id"], "text": c["text"],
                        "corrected": f"engine could not run the LLM reading ({exc}); rule parser's reading used"}
                       if rules else _unparsed(c["id"], c["text"], c["kind"], f"engine could not read it ({exc})"))
    return out


def _merge_split_alternatives(cls: list[dict]) -> list[dict]:
    """An LLM that splits one either/or requirement into two similar-works clauses makes both must-pass.
    Consecutive similar-works clauses whose joined text the rule parser reads as alternatives covering both
    (the second starting with 'or', or the first saying 'either', or the same text twice) become one."""
    out: list[dict] = []
    for c in cls:
        prev = out[-1] if out else None
        if prev and prev["kind"] == c["kind"] == "similar_works":
            same = prev["text"].strip() == c["text"].strip()
            joined = prev["text"] if same else f"{prev['text']} {c['text']}"
            linked = same or re.match(r"\W*(?:\(?[a-z0-9]{1,3}[.)]\s*)?or\b", c["text"], re.I) \
                or re.search(r"\beither\b", prev["text"], re.I)
            rule = next((r for r in parse_rules(joined) if r["kind"] == "similar_works"), None)
            if rule is None and (opts := similar_options(joined)) and len(opts[0]) >= 2:
                # a fragment pair without the words 'similar works': read the alternatives directly
                est = cpwd_estimate(*opts)
                rule = {"params": {"estimated_cost": est} if est else
                        {"options": [{"count": c, "min_value": v} for c, v in opts[0]]}}
            ra = alternatives(rule["params"]) if rule else None
            la = (alternatives(prev["params"]) or set()) | (alternatives(c["params"]) or set())
            if linked and ra and len(ra) >= 2 and la and _subset_alts(la, ra):
                params = {k: v for k, v in prev["params"].items()
                          if k not in ("min_value", "count", "estimated_cost", "options")}
                params.update({k: rule["params"][k] for k in ("estimated_cost", "options") if k in rule["params"]})
                out[-1] = {**prev, "text": joined, "params": params,
                           "corrected": f"either/or: the LLM split one requirement into {prev['id']} and {c['id']}; "
                                        f"merged into one clause with {len(ra)} alternatives (any one is enough)"}
                continue
        out.append(c)
    return out


def parse_llm(text: str) -> list[dict]:
    rep = providers.chat([{"role": "system", "content": CLAUSE_SYSTEM},
                          {"role": "user", "content": text}], max_tokens=2000)
    body = rep.content.strip()
    m = re.search(r"\{.*\}", body, re.S)
    data = json.loads(m.group(0) if m else body)
    cls = []
    for c in data.get("clauses", []):
        if c.get("kind") not in {"similar_works", "turnover", "key_personnel", "certification",
                                 "years_in_business", "other"}:
            c["kind"] = "other"
        raw = c.get("params") or {}
        cl = {"id": str(c.get("id", len(cls) + 1)), "text": c.get("text", ""), "kind": c["kind"],
              "params": _sanitize(c["kind"], raw, c.get("text", ""))}
        if c["kind"] == "certification" and isinstance(raw.get("standard"), list) and len(raw["standard"]) > 1:
            cl["_standards"] = raw["standard"]
        if c["kind"] == "turnover" and re.search(r"\beach\b", cl["text"], re.I) and cl["params"].get("min_average") \
                and not cl["params"].get("min_each_year"):       # "≥ X in each of the last 3 years" is not an average
            cl["params"]["min_each_year"] = cl["params"].pop("min_average")
        cls.append(cl)
    out = []
    for cl in _merge_split_alternatives(cls):
        need = THRESHOLDS.get(cl["kind"])
        if need and not any(cl["params"].get(k) for k in need):      # no threshold: must not pass vacuously
            rules = [r for r in parse_rules(cl["text"]) if r["kind"] == cl["kind"]]
            cl = {**rules[0], "id": cl["id"], "text": cl["text"]} if rules \
                else _unparsed(cl["id"], cl["text"], cl["kind"], " / ".join(need))
        out.extend(_checked(cl))
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
