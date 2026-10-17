"""Single-agent Q&A over the graph (the /ask page and the benchmark's 'BidBrain' system)."""
from __future__ import annotations

import re

from bidbrain.agents.loop import RunResult, run_agent
from bidbrain.agents.prompts import QA_SYSTEM, QA_TOOLS
from bidbrain.graph.client import Graph

ANSWER_RE = re.compile(r"ANSWER:\s*(.+)", re.I)
MARKDOWN_RE = re.compile(r"[*`]+|^\s*(?:#+|>)\s*", re.M)

_NUM = r"[-\u2212]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)"
# words a model puts in front of a bare number ("approximately 12", "Rs. 1,234", "a total of 3")
_LEAD = re.compile(r"(?:(?:approximately|approx\.?|about|around|roughly|nearly|exactly|(?:a\s+)?total(?:\s+of)?|"
                   r"~|\u2248|=)\s*)*(?:(?:\u20b9|INR|Rs\.?|rupees)\s*)?", re.I)
_SCALE = re.compile(r"\s*(crores?|cr\b\.?|lakhs?|lacs?)", re.I)
_LAKH_TAIL = re.compile(rf"\s*(?:and\s+|\+\s*)?({_NUM})\s*(?:lakhs?|lacs?)\b", re.I)
# A number glued to more of a token is not a bare number: "12th", "2024-25", "4/5", "2-lane", "3.5.1".
_GLUED = re.compile(r"[A-Za-z0-9]|[-/.:][A-Za-z0-9]")
_UNIT_GLUE = re.compile(r"%|/-|(?:rs|inr|cr|days?|yrs?|years?|works?|nos?)\b", re.I)
_QUOTES = " \t\"'`\u201c\u201d\u2018\u2019_"

VERDICT_YES, VERDICT_NO = "QUALIFIES", "DOES NOT QUALIFY"
_Q_WORD = re.compile(r"qualif|eligib", re.I)
_Q_NO = re.compile(r"(?:\bnot|n't|\bcannot|\bnever)\s+(?:yet\s+|currently\s+|be\s+)*(?:qualif|eligib)|"
                   r"\bdisqualif|\bunqualif|\bineligib|\bnon[\s-]?qualif", re.I)
_G_NO = re.compile(r"\bfail(?:s|ed)?\b|\bno[\s-]?go\b|(?:\bnot|n't|\bcannot)\s+(?:meet|satisf|pass)|\bnon[\s-]?complian", re.I)
_G_YES = re.compile(r"\bpass(?:es|ed)?\b|\bmeets?\b|\bsatisf(?:y|ies|ied)\b|\bcomplian", re.I)


def normalize_verdict(text) -> str | None:
    """Map a free-text eligibility answer to QUALIFIES / DOES NOT QUALIFY (None if it states neither).

    The clause that mentions qualifying decides, and a negated form is tested first because
    'DOES NOT QUALIFY' contains 'qualif'. The head (before any gloss) is read before the whole line,
    so 'QUALIFIES (does not fail any criterion)' stays QUALIFIES. Without the word, pass/fail and
    yes/no decide."""
    s = str(text or "").strip().strip(_QUOTES)
    head = re.split(r"\s*[(\[;:,.!]|\s+[-\u2013\u2014]\s+", s, maxsplit=1)[0].strip(_QUOTES)
    for c in (head, s):
        if _Q_WORD.search(c):
            return VERDICT_NO if _Q_NO.search(c) else VERDICT_YES
    for c in (head, s):
        if _G_NO.search(c):
            return VERDICT_NO
        if _G_YES.search(c):
            return VERDICT_YES
    h = head.lower()
    if h in ("yes", "y", "true"):
        return VERDICT_YES
    if h in ("no", "n", "false"):
        return VERDICT_NO
    return None


def _number(head: str):
    """The answer as a number when `head` IS a number, optionally with currency, scale and unit words
    ('3 works', '1,234 rupees', '45.50 percent', 'INR 5,00,00,000/-', '1.5 crore 20 lakh'); else None."""
    m = _LEAD.match(head)
    pos = m.end() if m else 0
    n = re.compile(_NUM).match(head, pos)
    if not n:
        return None
    tok = n.group(0)
    rest = head[n.end():]
    sm = _SCALE.match(rest)
    scale = 1
    if sm:
        scale = 10**7 if sm.group(1).lower().startswith(("crore", "cr")) else 10**5
        rest = rest[sm.end():]
    elif rest[:1] and _GLUED.match(rest) and not _UNIT_GLUE.match(rest):
        return None
    try:
        f = float(tok.replace(",", "").replace("\u2212", "-"))
    except ValueError:
        return None
    if scale > 1:
        total = f * scale
        if scale == 10**7 and (lk := _LAKH_TAIL.match(rest)):
            total += float(lk.group(1).replace(",", "")) * 10**5
        return round(total)
    return int(f) if f.is_integer() and "." not in tok else f


def parse_answer(text: str):
    """The value after the last `ANSWER:` line: a number when it is one, else the cleaned text.

    Shared by BidBrain and every baseline, so formatting quirks cost no system more than another:
    markdown emphasis (`**ANSWER:** 12`), a trailing gloss (`2,942,400,000 (\u20b9294.24 Cr)`), a value
    written in crore or lakh (`\u20b9294.24 Cr`, `1.5 crore 20 lakh`), and a unit noun after the number
    (`3 works`, `1,234 rupees`, `45.50 percent`, `INR 5,00,00,000/-`) all reduce to the number. Text keeps
    its full wording (a title such as `Road Widening - Maharashtra Pkg-21` stays whole) minus surrounding
    quotes, trailing punctuation and a bracketed gloss; an eligibility verdict becomes its canonical label.
    """
    text = MARKDOWN_RE.sub("", text or "")
    m = None
    for m in ANSWER_RE.finditer(text):
        pass
    raw = (m.group(1) if m else text.strip().splitlines()[-1] if text.strip() else "").strip().strip("_").strip()
    body = raw.strip(_QUOTES)
    head = re.split(r"\s*[(\[]|\s+[-\u2013\u2014]\s+|;", body, maxsplit=1)[0].strip(_QUOTES).rstrip(".,:!")
    num = _number(head)
    if num is not None:
        return num, raw
    if _Q_WORD.search(body) and (v := normalize_verdict(body)):
        return v, raw
    clean = re.split(r"\s*[(\[]|;", body, maxsplit=1)[0].strip(_QUOTES).rstrip(".,:!").strip(_QUOTES)
    return clean, raw


def ask(g: Graph, question: str, emit=None, history: list[dict] | None = None, max_steps: int = 10) -> dict:
    # ground=True: tool-free answers, names / amounts absent from the tool results and unnamed missing items are
    # caught (re-asked once, then flagged in `note`); see loop.py "answer checks"
    res: RunResult = run_agent(g, "qa", QA_SYSTEM, question, QA_TOOLS, max_steps=max_steps,
                               emit=emit, history=history, ground=True)
    value, raw = parse_answer(res.final)
    return {"answer": value, "answer_text": raw, "explanation": res.final, "steps": res.steps,
            "evidence": res.evidence, "subgraph": res.subgraph, "usage": res.usage, "ms": res.ms,
            "providers": res.providers, "switches": res.switches, "error": res.error,
            "note": getattr(res, "note", None), "checks": getattr(res, "checks", {})}
