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
EST_RE = re.compile(r"estimated\s+(?:cost|value)(?:\s+of\s+the\s+(?:work|project|contract))?[\s(:,=-]*"
                    r"(?:\((?:put\s+to\s+tender|approx\w*\.?)\)[\s:,=-]*)?"
                    r"(?:(?:of|is|i\.e\.?|being|namely|approx\w*\.?|about)\s+)?", re.I)
# 'X% of the estimated cost' with no amount after it: the estimate is stated elsewhere in the tender
PCT_OF_EST = re.compile(r"\s*of\s+(?:the\s+)?(?:estimated|tender(?:ed)?|contract|project)\s+(?:cost|value|amount)\b",
                        re.I)
# 'a cumulative value of INR 500 Cr', 'total road works of INR 120 Cr', 'aggregate value of ... not less than'
# ('total' only of works in the plural: 'one work of total value INR 50 Cr' is a single-work value)
CUMULATIVE_RE = re.compile(r"\b(?:(?:cumulative|aggregate)\s+(?:[\w&/-]+\s+){0,3}?"
                           r"(?:value|cost|amount|worth|works?|contracts?)"
                           r"|total\s+(?:(?:value|cost|amount|worth)\s+of\s+(?:all\s+|such\s+|the\s+)?)?"
                           r"(?:[\w&/-]+\s+){0,2}?(?:works|contracts|projects)"
                           # 'of cumulative INR 500 Cr', 'cumulatively worth', 'in aggregate', 'summing to'
                           r"|cumulative(?:ly)?|aggregat(?:e|ed|ing)|in\s+(?:the\s+)?aggregate|summing\s+(?:up\s+)?to"
                           r"|sum\s+(?:total\s+)?of)\b", re.I)
# 'works of total value not less than ...': aggregate when the works are plural and no single work is named
TOTAL_VALUE_RE = re.compile(r"\btotal\s+(?:value|cost|amount|worth)\b", re.I)
SINGLE_WORK_RE = re.compile(rf"\b(?:one|a|single|1|each)\s+(?:[\w&/()-]+\s+){{0,4}}?(?:work|contract|project)\b(?!s)",
                            re.I)
# financial requirements the engine has no kind (and the graph no checked data path) for: a clause naming one
# is NEEDS REVIEW, never DECLARE and never folded into another kind. (FinancialStatement holds paid_up_capital
# and reserves, so net worth is derivable, but no engine check reads it; a person must confirm it.)
UNSUPPORTED = (("net_worth", r"\bnet[\s-]*worth\b"), ("bid_capacity", r"\bbid(?:ding)?\s+capacity\b"),
               ("solvency", r"\bsolven(?:cy|t)\b"), ("liquid_assets", r"\bliquid(?:ity|\s+assets?|\s+funds?)\b"),
               ("working_capital", r"\bworking\s+capital\b"),
               ("credit_facility", r"\b(?:credit\s+(?:facilit\w+|limits?|lines?)|line\s+of\s+credit|bank\s+credit)\b"),
               ("profit", r"\b(?:net\s+)?profit(?:s|ab\w*)?\b|\blos(?:s|ses)\b"),
               ("ratio", r"\b(?:current|debt[\s-]+equity|quick)\s+ratio\b"),
               ("multiple", r"\b(?:twice|thrice|double|triple|two[\s-]fold|(?:\d+(?:\.\d+)?|one|two|three|four|five)"
                            r"(?:\s*(?:times|x)\s+(?:the\s+)?(?:estimated|annual|tender|contract|project|value|cost)|\s+times\b))"))
UNSUPPORTED_RE = [(k, re.compile(r, re.I)) for k, r in UNSUPPORTED]
# a deposit or fee to enclose with the bid (EMD, bid security, tender fee): its amount is not a qualification
DEPOSIT_RE = re.compile(r"\b(?:emd|e\.m\.d\.?|earnest\s+money|bid\s+(?:security|guarantee|bond)|"
                        r"tender\s+(?:fee|cost|document\s+(?:fee|cost)|processing\s+fee)|cost\s+of\s+(?:the\s+)?"
                        r"(?:tender|bid)\s+document|document\s+fee|processing\s+fee|security\s+deposit|"
                        r"performance\s+(?:security|guarantee|bank\s+guarantee))\b", re.I)
# tender metadata printed above the criteria ('Name of Work: ...', 'Tender No. ...', 'Estimated Cost: Rs 120 Cr')
CONTEXT_RE = re.compile(r"^(?:\(?(?:[ivxl]+|[a-z]|\d+(?:\.\d+)*)[.)]\s*)?(?:the\s+)?(?:name\s+of\s+(?:the\s+)?(?:work|project)|"
                        r"(?:nit|tender|bid|rfp|reference|ref)\.?\s*(?:no|number|id|ref)\b|estimated\s+(?:cost|value)|"
                        r"(?:period|time)\s+of\s+completion|completion\s+(?:period|time)|location|client|employer|"
                        r"authority|department|(?:last|closing|due)\s+date|date\s+of|bid\s+(?:due|validity))\b", re.I)
LABEL_LINE_RE = re.compile(r"^[A-Z][\w .,()/&'-]{1,48}:\s*\S")


def unsupported_term(text: str) -> str | None:
    """The financial requirement a text names that no engine kind checks ('net_worth', 'bid_capacity', ...)."""
    for k, rx in UNSUPPORTED_RE:
        if rx.search(text or ""):
            return k
    return None


def _deposit_bound(s: str, at: int) -> bool:
    """The amount / percentage starting at `at` belongs to a deposit or fee named just before it."""
    head = s[max(0, at - 70):at]
    m = list(DEPOSIT_RE.finditer(head))
    return bool(m) and not re.search(r"[.;]\s", head[m[-1].end():])


def material(text: str) -> str | None:
    """Why a text is a qualification requirement rather than a declaration / document to enclose: a money
    amount or percentage (other than the tender's estimate or a deposit / fee), an aggregate value, or a
    financial term the engine has no kind for. None for a genuine declaration (affidavit, undertaking, no
    blacklisting, a bid security to enclose)."""
    s = text or ""
    if (u := unsupported_term(s)):
        return u
    est_at = _estimate(s)[1]
    for a, _, _ in _money_spans(s):
        if a != est_at and not _deposit_bound(s, a):
            return "financial"
    for m in PCT_RE.finditer(s):
        if not _deposit_bound(s, m.start()):
            return "financial"
    if CUMULATIVE_RE.search(s) or TOTAL_VALUE_RE.search(s):
        return "financial"
    return None
# a line that only states the tender's estimated cost ('Estimated cost Rs 2000 Cr'): context, not a requirement
EST_LINE_RE = re.compile(r"^(?:\(?(?:[ivxl]+|[a-z]|\d+(?:\.\d+)*)[.)]\s*)?(?:the\s+)?estimated\s+(?:cost|value)\b", re.I)


def _estimate(s: str) -> tuple[int | None, int | None]:
    """The estimated cost a percentage refers to ('...of the estimated cost of INR 250 Cr'), and where it starts."""
    for m in EST_RE.finditer(s):
        for a, _, v in _money_spans(s):
            if m.end() <= a <= m.end() + 12:
                return v, a
    return None, None


def tender_estimate(text: str) -> int | None:
    """The tender's estimated cost wherever it is stated (a header line 'Estimated cost Rs 2000 Cr', or
    another clause '... 80% of the estimated cost of INR 250 Cr'): what 'X% of the estimated cost' means in a
    clause that does not repeat the amount."""
    return _estimate(text or "")[0]


def _pct_of(s: str, est: int | None = None) -> list[int | None]:
    """'X% of [the estimated cost of] INR Y Cr' -> X% of Y, for each such phrase; 'X% of the estimated cost'
    with no amount after it -> X% of `est` (the tender's estimate), or None when the tender states none."""
    out, spans = [], _money_spans(s)
    for m in PCT_RE.finditer(s):
        after = s[m.end():m.end() + 70]
        if not re.match(r"\s*of\b", after, re.I):
            continue
        nxt = next(((a, v) for a, _, v in spans if a >= m.end()), None)
        # the amount after 'X% of' is its base only when nothing else is said in between ('40% of the estimated
        # cost or two works of INR 30 Cr': 30 Cr belongs to the other alternative, not to the 40%)
        if nxt and nxt[0] - m.end() <= 70 and not PCT_RE.search(s[m.end():nxt[0]]) \
                and not re.search(r"\b(?:or|and|each|works?|contracts?|projects?)\b|[,;]", s[m.end():nxt[0]], re.I):
            out.append(int(round(float(m.group(1)) / 100 * nxt[1])))
        elif PCT_OF_EST.match(after):
            out.append(int(round(float(m.group(1)) / 100 * est)) if est else None)
    return out


def _threshold(s: str, est: int | None = None) -> int | None:
    """The amount a clause requires: 'X% of INR Y Cr' is X% of Y (or X% of the tender's estimate), otherwise
    the first amount written. None when a percentage cannot be resolved or there are several of them (the
    first amount would then be a guess)."""
    pct = _pct_of(s, est)
    if not pct:
        return None if PCT_RE.search(s) and not _amounts(s) else _money(s)
    return pct[0] if len(pct) == 1 else None


def _cumulative(s: str, est: int | None = None) -> tuple[int | None, tuple[int, int] | None, bool]:
    """An aggregate-value requirement ('and a cumulative value of INR 500 Cr'): (rupees, the span holding the
    phrase and its amount, whether the phrase was seen at all). A phrase with no readable amount comes back as
    (None, None, True): the clause must not silently lose it."""
    m = CUMULATIVE_RE.search(s)
    t = TOTAL_VALUE_RE.search(s)
    if t and (not m or t.start() < m.start()) and re.search(r"\b(?:works|contracts|projects)\b", s[:t.start()], re.I) \
            and not SINGLE_WORK_RE.search(s[:t.start()]):
        m = t
    if not m:
        return None, None, False
    window = s[m.start():m.end() + 100]
    stop = re.search(r"[.;](?:\s|$)", window[m.end() - m.start():])
    if stop:
        window = window[:m.end() - m.start() + stop.start()]
    pm = PCT_RE.search(window)
    am = next(iter(_money_spans(window)), None)
    if pm and (not am or pm.start() < am[0]):
        v = _pct_of(window[pm.start():], est)
        if v and v[0]:
            end = m.start() + pm.end()
            if am and am[0] >= pm.end() and am[0] - pm.end() <= 70:
                end = m.start() + am[1]
            return v[0], (m.start(), end), True
        return None, None, True
    if am:
        return am[2], (m.start(), m.start() + am[1]), True
    return None, None, True


def _blank(s: str, span: tuple[int, int] | None) -> str:
    return s if not span else s[:span[0]] + " " * (span[1] - span[0]) + s[span[1]:]


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


def similar_options(raw: str, context_est: int | None = None) -> tuple[list[tuple[int, int]], int | None] | None:
    """Either/or alternatives of a similar-works clause, in the tender's order, as (count, min_value):
    'one work of at least INR 50 Cr, or two works each of at least INR 40 Cr' -> [(1, 50 Cr), (2, 40 Cr)].
    Percentages are resolved against the estimated cost (the clause's own, else `context_est`, the tender's).
    Only when every pair of consecutive amounts is separated by an 'or' (so 'X and a total of Y' is not read
    as alternatives). Returns (options, estimate)."""
    est, est_at = _estimate(raw)
    est = est or context_est
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
    """A clause whose threshold the rules cannot read must not pass vacuously, and is not a declaration
    either: it is kept for a person to confirm (kind 'other' with `unparsed` = the kind it is: the team
    marks it NEEDS REVIEW, which blocks a BID), carrying what was not understood."""
    return {"id": cid, "text": raw, "kind": "other", "params": {"unparsed": kind, "reason": f"could not read {what}"}}


KNOWN_KINDS = ("turnover", "similar_works", "key_personnel", "certification", "years_in_business")


def known_kind(text: str, context_est: int | None = None) -> str | None:
    """The eligibility kind the rule parser recognises in a text (whether or not it could read the
    threshold), or what makes it a requirement ('net_worth', 'financial', ...: see `material`), or None for a
    genuine declaration / document to enclose. A text with a non-None answer is never DECLARE."""
    for r in parse_rules(text or "", context_est):
        if r["kind"] in KNOWN_KINDS:
            return r["kind"]
        if r["params"].get("unparsed"):
            return r["params"]["unparsed"]
    return None


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
            if LABEL_LINE_RE.match(line) or CONTEXT_RE.match(line) or EST_LINE_RE.match(line):
                # 'Estimated Cost: Rs. 120 Crore' / 'Name of Work: ...' is a line of its own: never joined
                # onto the requirement below it (its amount would be read as that requirement's threshold)
                if buf:
                    out.append(buf)
                buf = ""
                if CONTEXT_RE.match(line) and not _REQ_WORDS.search(line):
                    continue                    # tender metadata: context for the clauses, not one
            buf = f"{buf} {line}".strip()
            if re.search(r"[.;:]$", buf):
                out.append(buf)
                buf = ""
        if buf:
            out.append(buf)
    return out


_REQ_WORDS = re.compile(r"turnover|similar|experience|shall|must|should|required|minimum|at least|not less|"
                        r"net[\s-]*worth|bid(?:ding)?\s+capacity|solven", re.I)


def topic(seg: str) -> str | None:
    """Which requirement a text is about, by keyword (None: no requirement keyword of its own, so it
    continues the text before it): used only to decide where one clause ends and the next begins."""
    low = (seg or "").lower()
    if unsupported_term(seg):
        return "unsupported"
    if "turnover" in low:
        return "turnover"
    if re.search(r"\bpmp\b|six\s*sigma|project manager|key personnel", low):
        return "personnel"
    if re.search(r"iso\s*\d+|cpwd|nabl", low):
        return "certification"
    if re.search(r"\byears?\b", low) and re.search(r"business|existence|incorporat", low):
        return "business"
    if re.search(r"similar|experience|\bworks?\b|\bprojects?\b|\bcontracts?\b", low):
        return "works"
    if DEPOSIT_RE.search(seg or "") or re.search(r"blacklist|debar|affidavit|undertaking|declar", low):
        return "declaration"
    return None


def split_requirements(raw: str) -> list[str]:
    """One chunk -> the requirements it states. A ';' or a sentence end starts a new requirement when what
    follows is about a different topic ('three similar works ... 5 years; average annual turnover of Rs 60
    Cr'); a continuation ('; or (b) two works of 60%', 'Client reference letters shall be enclosed') stays."""
    parts = [p for p in re.split(r"(?<=;)\s*|(?<=\.)\s+(?=[A-Z(])", raw) if p.strip()]
    out: list[str] = []
    cur_topic = None
    for p in parts:
        t = topic(p)
        joins = re.match(r"\W*(?:\(?[a-z0-9]{1,4}[.)]\s*)?(?:or|and|nor|either|but|provided)\b", p, re.I)
        if out and (joins or t is None or t == cur_topic or cur_topic is None):
            out[-1] = f"{out[-1]} {p}"
            cur_topic = cur_topic or t
        else:
            out.append(p)
            cur_topic = t
    return [o.strip().rstrip(";").strip() for o in out]


def _turnover_sentence(raw: str) -> str:
    sents = re.split(r"(?<=[.;])\s+(?=[A-Z(])", raw)
    return " ".join(x for x in sents if "turnover" in x.lower()) or raw


def parse_rules(text: str, context_est: int | None = None) -> list[dict]:
    """Split on numbered/lettered items and classify each line. 'X% of the estimated cost' in a clause that
    does not state the amount is resolved against `context_est`, or the estimate stated anywhere in `text`."""
    clauses = []
    est = context_est or tender_estimate(text)
    segs = []
    for i, chunk in enumerate(_chunks(text)):
        ref = re.match(r"\(?([ivxl]+|[a-z]|\d+(?:\.\d+)*)[.)]?\s", chunk, re.I)
        base = ref.group(1) if ref else f"C{i}"
        parts = split_requirements(chunk)
        segs += [(base if len(parts) == 1 else f"{base}{'abcdefghij'[j]}", part) for j, part in enumerate(parts)]
    for cid, raw in segs:
        if len(raw) < 12:
            continue
        low = raw.lower()
        if EST_LINE_RE.match(raw) and len(raw) < 140 and not re.search(
                r"turnover|similar|experience|shall|must|should|required|minimum|at least|not less", low):
            continue                        # 'Estimated cost Rs 2000 Cr': context for the clauses, not one
        if (u := unsupported_term(raw)):
            clauses.append(_unparsed(cid, raw, u, f"it: no engine check reads a {u.replace('_', ' ')} requirement"))
            continue
        money = _money(raw)
        if "turnover" in low:
            ts = _turnover_sentence(raw)
            money = _threshold(ts, est) or _threshold(raw, est)
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
        elif "similar" in low or ("experience" in low and ("work" in low or "project" in low)) or (
                re.search(r"\b(?:execut\w*|complet\w*|carried\s+out|undertaken|constructed)\b", low)
                and re.search(r"\b(?:works?|projects?|contracts?)\b", low)
                and (_amounts(raw) or PCT_RE.search(raw) or CUMULATIVE_RE.search(raw))):
            p = {"categories": _cats(raw), "lookback_years": _num(LAST_YEARS, raw, 7) or 7}
            # an aggregate value ('and a cumulative value of INR 500 Cr') is its own requirement, read first
            # and blanked out so it is not mistaken for a single-work threshold or an alternative
            total, span, seen = _cumulative(raw, est)
            if seen and not total:
                clauses.append(_unparsed(cid, raw, "similar_works", "the cumulative / aggregate value"))
                continue
            main = _blank(raw, span)
            amts = _amounts(main)
            opts = similar_options(main, est)
            pcts = {round(float(x)) for x in PCT_RE.findall(main)}
            clause_est = _estimate(main)[0] or est
            if opts and len(opts[0]) >= 2:
                # either/or alternatives: any one is enough (never two must-pass clauses)
                cp = cpwd_estimate(*opts)
                if cp:
                    p["estimated_cost"] = cp
                else:
                    p["options"] = [{"count": c, "min_value": v} for c, v in opts[0]]
            elif {80, 60, 40} <= pcts and clause_est:
                # the CPWD 1 x 80% / 2 x 60% / 3 x 40% structure, written without 'or' between the options
                p["estimated_cost"] = clause_est
            elif len(amts) == 3 and cpwd_estimate([(1, amts[0]), (2, amts[1]), (3, amts[2])]):
                p["estimated_cost"] = cpwd_estimate([(1, amts[0]), (2, amts[1]), (3, amts[2])])
            elif amts or PCT_RE.search(main):
                # one threshold: 'one road work ... not less than 40% of the estimated cost of INR 80 Cr' is
                # one work of 32 Cr (40% of 80), not the CPWD rule on an 80 Cr estimate
                mv = _threshold(main, est)
                if not mv:
                    clauses.append(_unparsed(cid, raw, "similar_works", "the work value"))
                    continue
                p.update(min_value=mv, count=_num(rf"\b{NUM}{GLOSS}\s+(?:[\w-]+\s+){{0,3}}works?\b", main, 1) or 1)
            if total:
                p["min_total"] = total
            if not (p.get("estimated_cost") or p.get("min_value") or p.get("options") or p.get("min_total")):
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
        elif (why := material(raw)):
            # an amount, a percentage or an aggregate the rules cannot map onto an engine kind
            clauses.append(_unparsed(cid, raw, why, "it: it states an amount / financial term no engine check "
                                                    "maps onto"))
        else:
            clauses.append({"id": cid, "text": raw, "kind": "other", "params": {}})
    return clauses


# a clause of these kinds without one of these params would pass for anyone
THRESHOLDS = {"turnover": ("min_average", "min_each_year"),
              "similar_works": ("estimated_cost", "min_value", "options", "min_total"),
              "years_in_business": ("min_years",)}


_SYNONYMS = {"min_experience": "min_experience_years", "experience_years": "min_experience_years",
             "min_years_experience": "min_experience_years", "category": "categories", "lookback": "lookback_years",
             "years_lookback": "lookback_years", "period_years": "lookback_years", "min_turnover": "min_average",
             "average_turnover": "min_average", "num_people": "count", "certificate": "standard",
             "estimated_value": "estimated_cost", "cost": "estimated_cost", "value": "min_value",
             "alternatives": "options", "cumulative_value": "min_total", "aggregate_value": "min_total",
             "total_value": "min_total", "min_cumulative": "min_total", "min_cumulative_value": "min_total",
             "min_aggregate": "min_total", "min_aggregate_value": "min_total", "min_total_value": "min_total",
             "cumulative": "min_total", "aggregate": "min_total", "total": "min_total"}
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


def text_reading(kind: str, text: str, context_est: int | None = None) -> dict | None:
    """What the rule parser reads from a clause's own text for the params that decide its verdict (only
    what the text states, not the parser's defaults), or None when it cannot read that kind from the text.
    The Verifier compares this with the params the team actually used (a second opinion on structure:
    counts, either/or; the amounts are checked independently by `amount_check`)."""
    rules = [r for r in parse_rules(text or "", context_est) if r["kind"] == kind]
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
        if rp.get("min_total"):
            out["min_total"] = rp["min_total"]
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


def reading_diff(kind: str, params: dict, text: str, context_est: int | None = None) -> list[str]:
    """Differences between a clause's params and what its own text says (empty when they agree, or when the
    rule parser cannot read the text). Money within 0.5%, counts and years exactly."""
    want = text_reading(kind, text, context_est)
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
    if kind == "similar_works" and (want.get("min_total") or params.get("min_total")):
        a, b = int(params.get("min_total") or 0), int(want.get("min_total") or 0)
        if not (a and b and _near(a, b)):
            out.append(f"min_total {_crs(a) if a else 'none'} vs text {_crs(b) if b else 'none'}")
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


# ── the Verifier's independent reading: a bag of amounts, not the parser's structure ──────────────────────
# Deliberately simpler than parse_rules and sharing none of its code: every money amount and every
# percentage in the clause text, flat. Each threshold the team used must be one of those amounts or a stated
# percentage of one (or of the tender's estimate), and each amount and percentage in the text must be used by
# some threshold. A parser that misreads the structure (a dropped cumulative value, a 40% read as the CPWD
# rule, an LLM that is 10x off) fails this check whatever its reasoning was.
_V_TOKEN = re.compile(r"(₹|\bINR\b|\bRs\b\.?)?\s*(\d+(?:,\d+)*(?:\.\d+)?)\s*"
                      r"(%|per\s*cent\b|percent\b|crores?\b|cr\b|lakhs?\b|lacs?\b|million\b|mn\b|billion\b|bn\b)?",
                      re.I)
_V_SCALE = {"cr": 1e7, "cro": 1e7, "lak": 1e5, "lac": 1e5, "mil": 1e6, "mn": 1e6, "bil": 1e9, "bn": 1e9}
_V_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve "
                                       "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
_V_EST = re.compile(r"estimated\s+(?:cost|value)[^\S\n]*(?:\([^)\n]{0,30}\))?[^\S\n]*[:=-]?[^\S\n]*"
                    r"(?:(?:of|is|about|approx\.?|being)[^\S\n]+)?((?:₹|INR|Rs\.?)[^\S\n]*\d[\d,.]*(?:[^\S\n]*(?:crores?|cr|lakhs?))?"
                    r"|\d[\d,.]*[^\S\n]*(?:crores?|cr|lakhs?)\b)", re.I)


def text_amounts(text: str) -> tuple[list[int], list[float]]:
    """(money amounts in rupees, percentages) written in a text: a number is money only with a currency
    sign or a crore/lakh/million unit, a percentage only with % / per cent; counts and years are neither."""
    money, pcts = [], []
    for m in _V_TOKEN.finditer(text or ""):
        cur, num, unit = m.group(1), m.group(2), (m.group(3) or "").lower()
        v = float(num.replace(",", ""))
        if unit.startswith(("%", "per")):
            pcts.append(v)
        elif unit:
            money.append(int(round(v * _V_SCALE.get(unit[:3], _V_SCALE.get(unit, 1)))))
        elif cur:
            money.append(int(round(v)))
    return money, pcts


def stated_estimate(tender_text: str) -> int | None:
    """The tender's estimated cost as its text states it ('Estimated cost Rs 2000 Cr'), for the Verifier."""
    m = _V_EST.search(tender_text or "")
    if not m:
        return None
    money, _ = text_amounts(m.group(1))
    return money[0] if money else None


def _near(a: float, b: float) -> bool:
    return abs(a - b) <= max(1.0, 0.005 * max(abs(a), abs(b)))


def _thresholds(kind: str, params: dict) -> list[tuple[str, int]]:
    """The money thresholds a clause's params apply, as (what, rupees)."""
    out = []
    if kind == "similar_works":
        if params.get("estimated_cost"):
            e = int(params["estimated_cost"])
            out += [(f"{n} work(s) >= {int(f * 100)}% of the estimate", int(round(f * e)))
                    for n, f in ((1, 0.8), (2, 0.6), (3, 0.4))]
        for i, o in enumerate(params.get("options") or []):
            if isinstance(o, dict) and o.get("min_value"):
                out.append((f"option {'abcdefgh'[i]}", int(o["min_value"])))
        if params.get("min_value"):
            out.append(("min_value", int(params["min_value"])))
        if params.get("min_total"):
            out.append(("min_total", int(params["min_total"])))
    elif kind == "turnover":
        out += [(k, int(params[k])) for k in ("min_average", "min_each_year") if params.get(k)]
    return out


def _counts_in(text: str) -> set[int]:
    out = {int(x) for x in re.findall(r"(?<![\d.,])(\d{1,2})(?![\d.,%])", text or "")}
    low = (text or "").lower()
    out |= {v for w, v in _V_WORDS.items() if re.search(rf"\b{w}\b", low)}
    return out


def amount_check(kind: str, params: dict, text: str, context_est: int | None = None) -> dict:
    """The Verifier's independent check of a clause's params against its own text (see above).
    status 'verified' (every threshold is an amount or a stated percentage of one, and every amount and
    percentage in the text is used), 'mismatch' (with the problems), or 'unverified' (the text gives nothing
    to check the params against: a percentage with no base, or no amount at all). `candidates` is what the
    text allows a threshold to be."""
    params = params or {}
    money, pcts = text_amounts(text)
    bases = money + ([int(context_est)] if context_est and not any(_near(context_est, a) for a in money) else [])
    cands = [(f"{_crs(a)}", a) for a in money]
    cands += [(f"{p:g}% of {_crs(b)}", int(round(p / 100 * b))) for p in pcts for b in bases]
    out = {"status": "verified", "problems": [], "candidates": cands, "amounts": money, "percentages": pcts}
    problems = out["problems"]

    def unverified(why):
        out["status"] = "unverified"
        problems.append(why)
        return out

    terms = term_problems(kind, params, text)
    if any("no engine check reads" in t for t in terms):
        return unverified("; ".join(t for t in terms if "no engine check reads" in t))
    problems += terms
    if kind in ("similar_works", "turnover"):
        th = _thresholds(kind, params)
        if not th:
            return unverified("the params carry no money threshold to check")
        if pcts and not bases:
            return unverified(f"the text states {', '.join(f'{p:g}%' for p in pcts)} of an amount it does not give "
                              f"(no estimated cost in the clause or the tender)")
        if not cands:
            return unverified("the text states no amount the threshold could be checked against")
        for what, v in th:
            if not any(_near(v, c) for _, c in cands):
                problems.append(f"{what} {_crs(v)} is not an amount in the text or a stated percentage of one "
                                f"(the text allows {', '.join(sorted({n for n, _ in cands}))})")
        est = int(params.get("estimated_cost") or 0)
        for a in money:
            used = any(_near(a, v) for _, v in th) or (est and _near(a, est)) or \
                any(_near(p / 100 * a, v) for p in pcts for _, v in th)
            if not used:
                problems.append(f"the text states {_crs(a)}, which no threshold in the params accounts for")
        for p in pcts:
            if not any(_near(p / 100 * b, v) for b in bases for _, v in th):
                problems.append(f"the text's {p:g}% is not applied by any threshold in the params")
        counts = _counts_in(text)
        need = [int(o.get("count") or 1) for o in params.get("options") or [] if isinstance(o, dict)]
        if params.get("min_value"):
            need.append(int(params.get("count") or 1))
        for n in need:
            if n > 1 and n not in counts:
                problems.append(f"a count of {n} works is not in the text")
    if kind in ("key_personnel", "certification", "years_in_business"):
        problems += [f"the text states {_crs(a)}, which a {kind} clause does not check" for a in money]
        problems += [f"the text states {p:g}%, which a {kind} clause does not check" for p in pcts]
    if kind in ("similar_works", "turnover"):
        pass
    elif kind == "key_personnel":
        cred = str(params.get("credential") or "")
        low = (text or "").lower()
        if cred and not (cred.lower() in low or (cred.upper() == "PMP" and "project management professional" in low)):
            problems.append(f"credential {cred} is not named in the text")
        yrs = params.get("min_experience_years")
        stated = {int(x) for x in re.findall(r"(\d{1,2})\s*\+?\s*(?:\(\w+\)\s*)?(?:full\s+|complete\s+)?years?",
                                              text or "", re.I)}
        stated |= {v for w, v in _V_WORDS.items() if re.search(rf"\b{w}\b[^.\d]{{0,12}}years?", low)}
        if yrs is not None and int(yrs) not in stated:
            problems.append(f"min_experience_years {yrs} is not stated in the text ({sorted(stated) or 'none'})")
        if yrs is None and stated:
            problems.append(f"the text states {sorted(stated)} years, which the params do not use")
        if int(params.get("count") or 1) > 1 and int(params["count"]) not in _counts_in(text):
            problems.append(f"a count of {params['count']} people is not in the text")
    elif kind == "certification":
        std = str(params.get("standard") or "")
        key = (re.findall(r"\d{3,5}", std) or [std])[0].lower()
        if not key or key not in (text or "").lower():
            problems.append(f"standard {std or 'none'} is not named in the text")
    elif kind == "years_in_business":
        n = params.get("min_years")
        if n is None:
            return unverified("the params carry no minimum years")
        if int(n) not in _counts_in(text):
            problems.append(f"min_years {n} is not stated in the text")
    else:
        return unverified(f"no independent check for kind {kind}")
    if kind in ("similar_works", "turnover"):
        m = re.search(r"(?:last|past|preceding)\s+(\w+)(?:\s*\(\w+\))?\s+(?:[\w-]+\s+){0,3}?years", text or "", re.I)
        if m:
            w = m.group(1).lower()
            n = int(w) if w.isdigit() else _V_WORDS.get(w)
            key, default = ("lookback_years", 7) if kind == "similar_works" else ("years", 3)
            if n is not None and int(params.get(key) or default) != n:
                problems.append(f"{key} {params.get(key, default)} but the text says the last {n} years")
    if problems:
        out["status"] = "mismatch"
    return out


# financial terms, read independently of the parser: (what, pattern). 'aggregate' needs a min_total on a
# similar-works clause; every other term has no engine check, so the clause stating it must be NEEDS REVIEW.
_V_TERMS = [("aggregate", re.compile(r"\bcumulative(?:ly)?\b|\baggregat\w*|\bsum(?:ming)?\s+(?:total\s+)?(?:of|to)\b|"
                                     r"\bworks\b[^.;]{0,60}\btotal\s+(?:value|cost|amount|worth)\b", re.I)),
            ("net worth", re.compile(r"\bnet[\s-]*worth\b", re.I)),
            ("bid capacity", re.compile(r"\bbid(?:ding)?\s+capacity\b", re.I)),
            ("solvency", re.compile(r"\bsolven(?:cy|t)\b", re.I)),
            ("liquid assets", re.compile(r"\bliquid(?:ity|\s+assets?|\s+funds?)\b", re.I)),
            ("working capital", re.compile(r"\bworking\s+capital\b", re.I)),
            ("credit facility", re.compile(r"\bcredit\s+(?:facilit|limit|line)|\bbank\s+credit\b", re.I)),
            ("profit", re.compile(r"\bprofit|\blosses\b", re.I)),
            ("ratio", re.compile(r"\bratio\b", re.I)),
            ("a multiple of an amount", re.compile(r"\b(?:twice|thrice|double|triple)\b|\b(?:\d+(?:\.\d+)?|one\s+and\s+a\s+half|two|three|four|"
                                                  r"five)\s*(?:times|x)\s", re.I))]
_V_DEPOSIT = re.compile(r"\bemd\b|earnest\s+money|bid\s+(?:security|guarantee|bond)|tender\s+(?:fee|cost|document)|"
                        r"document\s+(?:fee|cost)|processing\s+fee|security\s+deposit|performance\s+(?:security|"
                        r"guarantee|bank)", re.I)


def _v_material(text: str) -> tuple[list[int], list[float], list[str]]:
    """(money amounts, percentages, financial terms) a text states as requirements: amounts that are the
    tender's estimated cost or a deposit / fee named just before them are context, not requirements."""
    t = text or ""
    est = {m.start(1) for m in _V_EST.finditer(t)}
    money, pcts = [], []
    for m in _V_TOKEN.finditer(t):
        cur, unit = m.group(1), (m.group(3) or "").lower()
        if not (cur or unit):
            continue
        head = t[max(0, m.start() - 70):m.start()]
        dep = list(_V_DEPOSIT.finditer(head))
        if (dep and not re.search(r"[.;]\s", head[dep[-1].end():])) or any(abs(m.start() - e) <= 1 for e in est):
            continue
        v = float(m.group(2).replace(",", ""))
        if unit.startswith(("%", "per")):
            pcts.append(v)
        else:
            money.append(int(round(v * _V_SCALE.get(unit[:3], _V_SCALE.get(unit, 1)))) if unit else int(round(v)))
    terms = [w for w, rx in _V_TERMS if rx.search(t)]
    return money, pcts, terms


def declaration_problems(text: str) -> list[str]:
    """Why a clause filed as a declaration (DECLARE) is not one: it states a requirement amount, a percentage
    or a financial term. Empty for an affidavit / undertaking / no-blacklisting / deposit to enclose."""
    money, pcts, terms = _v_material(text)
    return ([f"it states {_crs(a)}" for a in money] + [f"it states {p:g}%" for p in pcts]
            + [f"it names {w}" for w in terms])


def term_problems(kind: str, params: dict, text: str) -> list[str]:
    """Financial terms in a clause's text that its kind / params do not represent."""
    _, _, terms = _v_material(text)
    out = [f"the text names {w}, which no engine check reads" for w in terms if w != "aggregate"]
    if kind == "similar_works":
        if "aggregate" in terms and not (params or {}).get("min_total"):
            out.append("the text states an aggregate / cumulative value, but the params carry no min_total")
        if (params or {}).get("min_total") and "aggregate" not in terms:
            out.append("the params carry a min_total, but the text states no aggregate / cumulative value")
    elif "aggregate" in terms:
        out.append(f"the text states an aggregate / cumulative value, which a {kind} clause does not check")
    return out


def _v_segments(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"\n+|;|(?<=[.])\s+(?=[A-Z(])", text or "") if x and x.strip()]


def _v_words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (s or "").lower())}


def coverage_problems(tender_text: str, clauses: list[dict]) -> list[dict]:
    """Requirement text the clause list does not carry at all: every segment of the tender that states a
    requirement amount, a percentage or a financial term must overlap some emitted clause's text that also
    states it. A clause the parser (or the LLM) dropped comes back as {'text', 'problems'}."""
    out = []
    for seg in _v_segments(tender_text):
        money, pcts, terms = _v_material(seg)
        if not (money or pcts or terms):
            continue
        words = _v_words(seg)
        cover = [c for c in clauses if seg.lower() in (c.get("text") or "").lower()
                 or (words and len(words & _v_words(c.get("text"))) >= 0.6 * len(words))]
        cm, cp, ct = [], [], []
        for c in cover:
            m2, p2, t2 = _v_material(c.get("text") or "")
            cm, cp, ct = cm + m2, cp + p2, ct + t2
        miss = ([f"{_crs(a)}" for a in money if not any(_near(a, b) for b in cm)]
                + [f"{p:g}%" for p in pcts if not any(abs(p - q) < 1e-6 for q in cp)]
                + [w for w in terms if w not in ct])
        if miss:
            out.append({"text": seg, "problems": miss})
    return out


def _crs(v: float) -> str:
    return f"₹{v / 1e7:,.2f} Cr"


def _fix_amounts(c: dict, rule: dict | None, context_est: int | None) -> dict:
    """An LLM reading whose amounts the text does not account for (40% of 80 Cr read as 3.2 Cr, 'INR 30 Cr'
    read as 3 Cr, a cumulative value dropped): the rule parser's reading is used when it passes the same
    independent check; otherwise each money threshold that is not a candidate but is a power of ten away from
    exactly one is replaced by it. Every correction is noted on the clause."""
    if c["kind"] not in ("similar_works", "turnover"):
        return c
    chk = amount_check(c["kind"], c["params"], c["text"], context_est)
    if chk["status"] != "mismatch":
        return c
    params, notes = dict(c["params"]), []
    rp = (rule or {}).get("params") or {}
    if rule and amount_check(c["kind"], rp, c["text"], context_est)["status"] == "verified":
        money_keys = ("estimated_cost", "min_value", "count", "options", "min_total", "min_average", "min_each_year")
        for k in money_keys:
            params.pop(k, None)
        params.update({k: v for k, v in rp.items() if k in money_keys})
        if c["kind"] == "turnover" and rp.get("years"):
            params["years"] = rp["years"]
        if c["kind"] == "similar_works" and rp.get("lookback_years"):
            params["lookback_years"] = rp["lookback_years"]
        before = ", ".join(f"{w} {_crs(v)}" for w, v in _thresholds(c["kind"], c["params"])) or "none"
        after = ", ".join(f"{w} {_crs(v)}" for w, v in _thresholds(c["kind"], params))
        notes.append(f"amounts: LLM read {before}; the text's amounts give {after} ({'; '.join(chk['problems'])})")
    else:
        cands = [v for _, v in chk["candidates"]]
        for k in ("min_value", "min_total", "min_average", "min_each_year", "estimated_cost"):
            v = params.get(k)
            if not v or any(_near(int(v), x) for x in cands):
                continue
            hits = {x for x in cands for f in (10, 100, 0.1, 0.01) if _near(int(v) * f, x)}
            if len(hits) == 1:
                params[k] = hits.pop()
                notes.append(f"{k}: LLM {_crs(int(v))} -> {_crs(params[k])} (a power of ten off an amount in the text)")
    if not notes:
        return c
    return {**c, "params": params, "corrected": "; ".join(filter(None, [c.get("corrected"), *notes]))}


def _checked(cl: dict, context_est: int | None = None) -> list[dict]:
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
        if (u := unsupported_term(c["text"])):
            # 'net worth', 'bid capacity', 'twice the estimated cost': whatever kind the LLM chose, no engine
            # check represents it, so its params would be a guess
            out.append({**_unparsed(c["id"], c["text"], u, f"it: no engine check reads a {u.replace('_', ' ')} "
                                                           f"requirement"),
                        "corrected": f"the LLM read it as {c['kind']}; it names {u.replace('_', ' ')}"})
            continue
        rules = [r for r in parse_rules(c["text"], context_est) if r["kind"] == c["kind"]]
        if len(rules) == 1:
            c = _reconcile(c, rules[0])
        c = _fix_amounts(c, rules[0] if len(rules) == 1 else None, context_est)
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
                          if k not in ("min_value", "count", "estimated_cost", "options", "min_total")}
                params.update({k: rule["params"][k] for k in ("estimated_cost", "options", "min_total")
                               if k in rule["params"]})
                out[-1] = {**prev, "text": joined, "params": params,
                           "corrected": f"either/or: the LLM split one requirement into {prev['id']} and {c['id']}; "
                                        f"merged into one clause with {len(ra)} alternatives (any one is enough)"}
                continue
        out.append(c)
    return out


def parse_llm(text: str) -> list[dict]:
    est = tender_estimate(text)
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
        text = c.get("text") or ""
        parts = split_requirements(text)
        if len(parts) > 1:
            # one LLM clause holding several requirements ('three similar works ...; average annual turnover of
            # Rs 60 Cr'): its params can represent only one, so each requirement is read on its own by the rules
            cid = str(c.get("id", len(cls) + 1))
            for j, r in enumerate(parse_rules(";\n".join(parts), est)):
                cls.append({**r, "id": f"{cid}{'abcdefghij'[j]}",
                            "corrected": f"the LLM merged {len(parts)} requirements into one {c['kind']} clause; "
                                         f"each is read on its own by the rule parser"})
            continue
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
        if cl["kind"] == "other" and not cl["params"].get("unparsed"):
            # a requirement the LLM filed as a declaration ('average turnover ... 30% of the estimated cost')
            # must not become DECLARE: the rule parser's reading of it, or NEEDS REVIEW when it cannot read it
            kk = known_kind(cl["text"], est)
            if kk:
                rules = [r for r in parse_rules(cl["text"], est) if r["kind"] == kk]
                note = f"the LLM filed a {kk.replace('_', ' ')} requirement as a declaration"
                cl = ({**rules[0], "id": cl["id"], "text": cl["text"], "corrected": note + "; rule parser's reading used"}
                      if len(rules) == 1 else {**_unparsed(cl["id"], cl["text"], kk, "its threshold"),
                                               "corrected": note})
        need = THRESHOLDS.get(cl["kind"])
        if need and not any(cl["params"].get(k) for k in need):      # no threshold: must not pass vacuously
            rules = [r for r in parse_rules(cl["text"], est) if r["kind"] == cl["kind"]]
            cl = {**rules[0], "id": cl["id"], "text": cl["text"]} if rules \
                else _unparsed(cl["id"], cl["text"], cl["kind"], " / ".join(need))
        out.extend(_checked(cl, est) if cl["kind"] != "other" else [cl])
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
