"""The thin tool-calling loop every BidBrain agent runs on.

No framework: messages in, tool calls out, tool results back, until the model
answers or the step budget runs out. Only the tools offered to the agent can run:
a call to any other registered tool gets an error tool message instead. When the
budget runs out (or an endpoint rejects a mid-run request) the model gets one last
tool-free turn to answer from the results it already has, so gathered evidence is
never thrown away. Each step is emitted as an event so the UI
can stream the trace and animate the subgraph each tool touched. The loop also
accumulates evidence + subgraph across steps, which is what the answer card
shows as "the path behind this answer".

Two guards keep one answer honest and consistent:
* every narrowing filter the model sets on find_portfolio / prove_absence / receivables (role, dates,
  value bounds, reference, government level, categories, status) is checked for lexical support in
  what the user asked; an unsupported one gets a NOTE in the tool result asking for the call again
  without it (`unasked_filters`);
* the provider bucket that served the first step is pinned for the rest of the run, so a single
  answer is not stitched together from several models: a rate-limited pin is waited for, and the pin
  moves (once, with the reason recorded in `RunResult.switches`) only when the provider layer reports
  that the pinned bucket rejected the request or stays unavailable beyond the wait.

When the LLM becomes unavailable mid-run (rate limit, outage, rejected request), the loop still answers:
first a tool-free finalize call, and if that fails too, a deterministic answer built from the last
successful tool result (its value plus evidence), clearly marked as such. Error text that reaches events
or `RunResult.error` is redacted (`providers.redact`).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from bidbrain.agents import providers
from bidbrain.graph.client import Graph
from bidbrain.tools import TOOLS, call, compact, schemas
from bidbrain.agents.prompts import BRIEF

Emit = Callable[[dict], None]


@dataclass
class RunResult:
    agent: str
    final: str
    steps: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    subgraph: dict = field(default_factory=lambda: {"nodes": [], "edges": []})
    usage: dict = field(default_factory=lambda: {"prompt": 0, "completion": 0, "llm_calls": 0, "tool_calls": 0})
    ms: float = 0.0
    providers: list[str] = field(default_factory=list)
    error: str | None = None
    note: str | None = None        # e.g. "step budget exhausted; answered from the results so far"
    switches: list[dict] = field(default_factory=list)   # {step, from, to, why}: every change of model
    # the Q&A answer checks: reasked (kinds), ungrounded (no tool call), unverified (names / amounts absent from
    # every tool result), absent_named (missing items added from prove_absence)
    checks: dict = field(default_factory=dict)


# ── unasked-filter guard ────────────────────────────────────────────────────
# A model answering "what is the total value of X's works" sometimes adds role='Prime' or a years range on
# its own, silently shrinking the answer. After each call to a tool with narrowing filters, every filter
# the model set is checked for *lexical* support in what the user asked (this question and earlier user
# turns); an unsupported one earns a NOTE in the tool result the model reads. Scope arguments (client,
# person, via_work, measure, evidence) are never questioned: only filters that narrow a set.
# A date filter needs a year number, a month, or a relative-time phrase; "years" alone ("over the years",
# "10 years of experience") is not one.
_MONTHS = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
_DATES = (r"\b(?:19|20)\d{2}\b|\bfy\s*'?\d{2}|"
          rf"\b(?:{_MONTHS})\b(?=\s*(?:\d|')|\s+(?:of|this|last|next)\b)|"
          r"\b(?:last|past|previous|preceding|recent|coming|next|first|this|current|same|that)\s+(?:\w+\s+){0,2}?"
          r"(?:years?|months?|quarters?|decades?|fy|financial years?|fiscal years?)\b|"
          r"\b(?:years?|months?|decades?)\s+(?:ago|back|before|after|earlier|later|prior)\b|"
          r"\b(?:after|before|since|between|until|till|prior to|during|within|recent(?:ly)?|lately|so far|to date|"
          r"year[- ]to[- ]date|ytd|financial year|fiscal year|decade)\b")
# A value bound needs an amount or a comparison word; "value", "worth" or "cost" alone is what is being summed.
_AMOUNT = (r"\u20b9\s*\d|\b(?:rs\.?|inr)\s*\d|\d[\d,]*(?:\.\d+)?\s*(?:cr\b|crores?|lakhs?|lacs?|l\b|k\b|mn\b|m\b|bn\b|"
           r"million|billion|rupees)|\b\d{5,}\b|\b\d{1,3}(?:,\d{2,3}){2,}\b|"
           # amounts in words: "the six crore line", "fifty lakh", "a hundred crore"
           r"\b(?:a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|twenty[- ]five|"
           r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)\s+(?:crores?|cr\b|lakhs?|lacs?|million|billion)")
_COMPARE = (r"\babove\b|\bbelow\b|\bover\b(?!\s+(?:the\s+)?(?:last|past|years?|time|period|decade))|\bunder\b|"
            r"more than|less than|greater than|fewer than|bigger than|larger than|smaller than|at least|at most|"
            r"no (?:less|more) than|not (?:less|more) than|in excess of|up ?to\b|\bexceed|\bminimum|\bmaximum|"
            r"\bthreshold|\bcap\b|\bor (?:more|less|above|below|higher|lower)\b|\bupwards of|\bnorth of|\bcrore[- ]plus|"
            r"\bbetween\b|[<>]=?|\u2265|\u2264")
_EVIDENCE = {
    "role": r"\bprime\b|\bjv\b|joint[\s-]?venture|\bpartner|consortium|\bsole\b|\bsolo\b|\bon our own\b|"
            r"\bindependent|\brole\b|\blead (?:contractor|member|partner)",
    "dates": _DATES,
    "valid_on": _DATES + r"|\bvalid|\bexpir|\blaps|\bcurrent|\btoday\b|\bnow\b|\bon the bid\b",
    "value": _AMOUNT + "|" + _COMPARE,
    "reference": r"referenc|\bletters?\b|testimonial|recommendation",
    # client names ("... Corporation", "Public Works Department") are not evidence of a government-level filter
    "govt": r"\bgovt|government|\bcentral|\bstates?\b|\bpsus?\b|public[\s-]sector|municipal|civic|private|"
            r"ministry|\bunion\b|\blocal bod",
    "status": r"\bpaid\b|\bdue\b|overdue|outstanding|unpaid|partial|part[\s_-]?paid|settled|pending|\bopen\b|"
              r"\bstatus|\bcleared\b",
    "category": r"categor|\bsector|\btype|\bkind|\bsegment|\bsimilar\b|\bsame\b|\bsuch\b|\bline of",
    "exclude": r"\bexcept|exclud|other than|apart from|\bbesides\b|\bnot\b|\bnon[\s-]|\bwithout\b|\boutside\b|"
               r"\bbut\b|\bignor|\bleave out|\bleaving out|\bremov|\bminus\b|\bfilter(?:ing)? out|\bdrop|\bstrip(?:ping)? out|\bnet of\b|\bset aside|\bdisregard",
}
# argument -> evidence key, per tool with narrowing filters
NARROWING = {
    "find_portfolio": {"role": "role", "years": "dates", "completed_after": "dates", "completed_before": "dates",
                       "min_value": "value", "max_value": "value", "has_reference": "reference",
                       "govt_levels": "govt", "categories": "category", "exclude_categories": "category"},
    "prove_absence": {"category": "category", "valid_on": "valid_on"},
    "receivables": {"status": "status", "year": "dates"},
}
GOVT_LEVELS = {"central", "state", "psu", "municipal", "private"}
_CAT_STOP = {"and", "the", "works", "work", "large", "small", "other", "misc", "general"}
# a category token is also supported by words people use for it (never client names such as PHED / Jal Nigam)
_CAT_SYN = {"road": r"highway|carriageway|lane|pavement|\bnh|\bsh\b|overlay|resurfac", "highway": r"road|\bnh|lane|expressway",
            "expressway": r"highway|lane|ring road|access[- ]controlled", "bridge": r"flyover|\brob\b|viaduct|overpass",
            "flyover": r"bridge|\brob\b|overpass|elevated", "water": r"\bwtp|drinking|pipeline|pumping",
            "treatment": r"\bwtp|\bstp|plant", "supply": r"pipeline|distribution|hand ?pump", "sewerage": r"sewage|\bstp|drain",
            "drainage": r"drain|storm|sewer", "irrigation": r"canal|\bdam\b|barrage|lift", "tunnel": r"tunnell?ing|\btbm",
            "building": r"hospital|school|housing|office|complex|institution|centre|center|anganwadi|hostel",
            "industrial": r"plant|factory|\bepc\b|refiner", "maintenance": r"repair|patch|o&m|upkeep|resurfac"}


def _set(arg: str, v) -> bool:
    if v is None or v == "" or v == [] or v == {}:
        return False
    if arg in ("min_value", "max_value"):
        return not (isinstance(v, (int, float)) and v <= 0 and arg == "min_value")
    if arg == "govt_levels" and isinstance(v, list) and GOVT_LEVELS <= {str(x).lower() for x in v}:
        return False            # every level is no filter at all
    return True


def _category_supported(values, asked: str) -> bool:
    for v in values if isinstance(values, list) else [values]:
        for tok in re.findall(r"[a-z]{3,}", str(v).lower()):
            if tok in _CAT_STOP:
                continue
            stem = tok[:-1] if tok.endswith("s") and not tok.endswith("ss") else tok
            if stem in asked or re.search(_CAT_SYN.get(stem, r"(?!)"), asked):
                return True
    return False


_DASH = re.compile(r"[\u2010-\u2015\u2212]")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", _DASH.sub("-", str(text or "").lower()))


def strip_names(asked: str, names) -> str:
    """`asked` without the names of the entities the call is scoped to: 'Public Works Department, Govt of
    Gujarat' names a client, it does not ask for a government-level filter (nor 'Ring Road' for a roads one)."""
    out = _norm(asked)
    for n in sorted({_norm(x) for x in names or () if x and len(str(x).strip()) >= 3}, key=len, reverse=True):
        out = out.replace(n, " ")
    # a state government named as a client ("the Govt of Gujarat") is a name, not a level
    return re.sub(r"\b(?:govt\.?|government)\s+of\s+[a-z]+(?:\s+[a-z]+)?\b", " ", out)


def unasked_filters(tool: str, args: dict, asked: str, names=()) -> list[str]:
    """Narrowing arguments of a tool call that nothing in `asked` (the user's words) supports,
    each as `name=value`. Lexical and generous on purpose: it only flags a filter with no trace at all.
    `names` (the resolved client / person / work the call is scoped to) are removed from `asked` first."""
    spec = NARROWING.get(tool)
    if not spec or not isinstance(args, dict):
        return []
    asked = strip_names(asked, [*names, *(args.get(a) for a in _SCOPE_ARGS if isinstance(args.get(a), str))])
    out = []
    for arg, key in spec.items():
        v = args.get(arg)
        if not _set(arg, v):
            continue
        if key == "category":
            ok = re.search(_EVIDENCE["category"], asked) or _category_supported(v, asked)
            if arg == "exclude_categories":
                ok = ok and re.search(_EVIDENCE["exclude"], asked)
        else:
            ok = re.search(_EVIDENCE[key], asked)
        if not ok:
            out.append(f"{arg}={json.dumps(v, default=str)[:80]}")
    return out


_SCOPE_ARGS = ("client", "person", "via_work", "work")


def scope_names(args: dict, out: dict) -> list[str]:
    """Names of the entities a call was scoped to, as the user may have written them: the arguments, what
    they resolved to, and the scoped client's canonical name."""
    names = [args[a] for a in _SCOPE_ARGS if isinstance(args.get(a), str)]
    ra = out.get("resolved_as")
    for v in (ra.values() if isinstance(ra, dict) else []):
        if isinstance(v, dict) and isinstance(v.get("matched"), str):
            names.append(v["matched"])
    r = out.get("result")
    if isinstance(r, dict):
        if isinstance(r.get("client"), str):
            names.append(r["client"])
        if args.get("client") or args.get("via_work"):
            names += sorted({w["client"] for w in r.get("works") or [] if isinstance(w, dict) and w.get("client")})
    return names


def unasked_note(flags: list[str]) -> str:
    one = len(flags) == 1
    return (f"\nNOTE: you filtered by {', '.join(flags)}, which the question does not mention; repeat the call "
            f"without {'it' if one else 'them'} unless the user asked for {'it' if one else 'them'}.")


FINALIZE = ("You have no tool calls left. Do not call any tools. Using only the tool results above, "
            "reply now in the required final format.")


# ── answer checks (the Q&A agent: run_agent(..., ground=True)) ───────────────
# Three cheap, lexical checks on the final reply, each firing only on a clearly bad answer:
# * tool-free: the question names an entity or asks for a number, yet the model answered without one tool
#   call. It is re-asked once to use the tools; a second tool-free reply is marked UNGROUNDED in `note`.
# * grounding: proper names (two or more capitalised words), record ids (DOC-.., Pkg-NN) and rupee amounts
#   in the answer must appear in this run's tool results (or the question / conversation). Anything absent
#   is listed back to the model once; if it survives, `note` flags it as UNVERIFIED.
# * absence: when prove_absence ran, the answer must name what it found missing; names the answer left out
#   are inserted above the ANSWER line (never after it: `qa.parse_answer` reads the last ANSWER line).
TOOL_FREE_REASK = ("You answered without calling any tool, so nothing in that answer comes from the company "
                   "graph. Call the tools this question needs (resolve the names it mentions first), then reply "
                   "with the path you followed and the final ANSWER line.")


def grounding_reask(missing: list[str]) -> str:
    return (f"Check your answer: these names or amounts do not appear in any tool result: {'; '.join(missing)}. "
            "Use only what the tools returned (call a tool if you need to check one) and do not name clients, "
            "people, works, documents or amounts the tools did not return. Then reply again with the path you "
            "followed and the final ANSWER line.")


_NEEDS_NUMBER = re.compile(r"\bhow (?:many|much|long|often|old)\b|\b(?:total|sum|count|number of|value|worth|amount|"
                           r"average|mean|median|percent(?:age)?|share|rate|ratio|days?|gap|difference|largest|"
                           r"biggest|smallest|highest|lowest)\b|%|₹|\bcrores?\b|\blakhs?\b", re.I)


def needs_tools(question: str) -> bool:
    """Whether a question names an entity or asks for a number, so a tool-free answer to it cannot be right:
    a capitalised word inside a sentence, an acronym, a digit or a quoted name; or a count / amount word."""
    q = str(question or "")
    if _NEEDS_NUMBER.search(q) or re.search(r"\d|[\"“‘']\w", q):
        return True
    if re.search(r"\b[A-Z]{2,}\b", q):
        return True
    return any(not _at_sentence_start(q, m.start()) for m in re.finditer(r"\b[A-Z][a-z]+", q))


def _gnorm(text) -> str:
    return " " + re.sub(r"[^a-z0-9₹]+", " ", str(text or "").lower()).strip() + " "


_NAME_TOK = r"[A-Z][A-Za-z0-9'’.&/-]*"
_NAME_RX = re.compile(rf"(?<![A-Za-z0-9]){_NAME_TOK}(?:[ \t]+(?:(?:of|&|for|the|de|-|–|—)[ \t]+)?{_NAME_TOK})+")
_CONNECT = {"of", "&", "for", "the", "de", "-", "–", "—"}
# capitalised words that start a sentence or name a concept, never a client / person / work
_LEAD_STOP = set("""The A An This That These Those Its It Our Their His Her We I They You For In On From By With And
Of To At As If When Where All Both Each No Not None Only Then Next First Second Third Finally Also However So
Therefore Thus Using Use Used Based According Since After Before Because While Step Answer Final Note Path Total
Here There Yes Summary Result Results Explanation Approach Checked Called Found Resolved Queried Summed Counted
Computed Calling Checking Resolving Querying Looking Searching Of Among Across Between Under Over Per Via Out
Missing Absent Present Available Valid Open Past Current Overall Verdict Recommendation Case Clause""".split())
_GENERIC = set("""Step Answer Final Total Value Values Work Works Client Clients Prime JV Partner Graph Company Tool Tools
Result Results Category Categories Bid Bids Case Cases Clause Clauses Verdict Document Documents Evidence Reference
References Letter Letters Proof Absence Person People Lead Leads Credential Credentials Tender Tenders Invoice
Invoices Bond Bonds Decision Decisions Claim Claims Finding Findings Action Actions Open Past Note Path Count Sum
Average Median Rate Days Years Cr Crore Crores Lakh Lakhs Rs INR Yes No Not None Summary Recommendation""".split())
_ID_RX = re.compile(r"\b(?:DOC|CASE|ACT|FND|CL|OF|REQ|RFP)-[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9]|\bPkg-\d+\b")
_NUM = r"\d[\d,]*(?:\.\d+)?"
_MONEY_RX = re.compile(rf"(?:₹|\bRs\.?|\bINR)\s*({_NUM})(?:\s*(crores?|cr\b|lakhs?|lacs?))?|"
                       rf"(?<![\d.,])({_NUM})\s*(crores?|cr\b|lakhs?|lacs?)\b", re.I)


def _at_sentence_start(text: str, i: int) -> bool:
    before = text[:i].rstrip(" \t")
    return not before or before[-1] in "\n.!?:;(" or bool(re.search(r"(?:^|\n)\s*(?:[-*•]|\d+[.)])$", before))


def _money(text: str) -> list[tuple[str, float, float]]:
    """(as written, rupees, rounding tolerance) for every rupee amount in `text`."""
    out = []
    for m in _MONEY_RX.finditer(text or ""):
        num, scale = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        mult = 1.0
        if scale:
            mult = 1e7 if scale.lower().startswith(("crore", "cr")) else 1e5
        decimals = len(num.split(".", 1)[1]) if "." in num else 0
        out.append((m.group(0).strip(), v * mult, 0.5 * 10 ** -decimals * mult + 0.5))
    return out


def _corpus_numbers(corpus: str) -> list[float]:
    nums = {float(x.replace(",", "")) for x in re.findall(_NUM, corpus or "")}
    nums |= {v for _, v, _ in _money(corpus)}
    return sorted(nums)


def ungrounded(text: str, corpus: str) -> list[str]:
    """Proper names, record ids and rupee amounts in `text` that `corpus` (this run's tool results plus what the
    user said) does not contain. Lexical and generous: a name passes if it appears verbatim, or if every word of
    it does (the first word excepted at a sentence start); an amount passes if a number in the corpus rounds to it."""
    import bisect
    clean = re.sub(r"[*_`#>|]+", " ", str(text or ""))
    norm, words = _gnorm(corpus), set(_gnorm(corpus).split())
    missing: list[str] = []

    def add(x):
        if x not in missing:
            missing.append(x)

    pieces = []                            # a token ending a sentence ("Dept. Summing") ends the phrase too
    for m in _NAME_RX.finditer(clean):
        off = m.start()
        for piece in re.split(r"(?<=[.!?])[ \t]+", m.group(0)):
            pieces.append((off, piece))
            off += len(piece) + 1
    for start, piece in pieces:
        toks = [re.sub(r"['’]s$", "", t.rstrip(".,;:'’")) for t in piece.split()]
        while toks and (toks[0] in _LEAD_STOP or toks[0].lower() in _CONNECT):
            start = -1                     # whatever follows a stripped lead word is not sentence-initial
            toks = toks[1:]
        while toks and toks[-1].lower() in _CONNECT:
            toks = toks[:-1]
        named = [t for t in toks if t.lower() not in _CONNECT]
        if len(named) < 2 or all(not re.search(r"[a-z]", t) for t in named) or all(t in _GENERIC for t in named):
            continue
        phrase = " ".join(toks)
        if _gnorm(phrase) in norm:
            continue
        subs = [w for t in named for w in _gnorm(t).split() if len(w) >= 2]
        if start >= 0 and _at_sentence_start(clean, start):
            first = set(_gnorm(named[0]).split())
            subs = [w for w in subs if w not in first]
        if subs and all(w in words or w.isdigit() for w in subs):
            continue
        add(phrase)
    low = (corpus or "").lower()
    for m in _ID_RX.finditer(clean):
        if m.group(0).lower() not in low:
            add(m.group(0))
    nums = _corpus_numbers(corpus)
    for shown, v, tol in _money(clean):
        i = bisect.bisect_left(nums, v - tol)
        if not (i < len(nums) and nums[i] <= v + tol):
            add(shown)
    return missing


def _absent_names(out: dict) -> list[tuple[str, str | None]]:
    r = out.get("result") if isinstance(out, dict) else None
    items = r.get("absent") if isinstance(r, dict) else None
    names = []
    for a in items or []:
        if isinstance(a, dict):
            n = a.get("title") or a.get("name") or a.get("tender") or a.get("key")
            if n:
                names.append((str(n), a.get("key") or a.get("tender")))
        elif isinstance(a, str):
            names.append((a, a))
    return names


def _insert_before_answer(text: str, block: str) -> str:
    lines = (text or "").split("\n")
    idx = max((i for i, ln in enumerate(lines) if re.search(r"ANSWER:", ln, re.I)), default=None)
    if idx is None:
        return f"{block}\n\n{text}" if text else block
    return "\n".join([*lines[:idx], block, *lines[idx:]])


def name_absent(text: str, out: dict) -> tuple[str, int]:
    """`text` with the items prove_absence found missing that it does not already name, inserted above its
    ANSWER line; and how many were added."""
    norm, low = _gnorm(text), (text or "").lower()
    left = []
    for name, key in _absent_names(out):
        pkg = re.search(r"pkg-?\s*\d+", name, re.I)
        if _gnorm(name) in norm or (key and str(key).lower() in low) or \
                (pkg and re.search(rf"\b{re.escape(pkg.group(0).lower())}\b", low)):
            continue
        left.append(name)
    if not left:
        return text, 0
    shown = "; ".join(left[:30]) + (f"; and {len(left) - 30} more" if len(left) > 30 else "")
    return _insert_before_answer(text, f"Missing (listed by prove_absence): {shown}"), len(left)


def _add_note(res: "RunResult", text: str) -> None:
    res.note = f"{res.note}; {text}" if res.note else text


def _merge_subgraph(acc: dict, sg: dict) -> None:
    seen_n = {n["id"] for n in acc["nodes"]}
    for n in sg.get("nodes", []):
        if n["id"] not in seen_n:
            acc["nodes"].append(n)
            seen_n.add(n["id"])
    seen_e = {(e["source"], e["target"], e["type"]) for e in acc["edges"]}
    for e in sg.get("edges", []):
        k = (e["source"], e["target"], e["type"])
        if k not in seen_e:
            acc["edges"].append(e)
            seen_e.add(k)


# A finalize reply that does not answer ("I don't have enough information") is a failed finalize: the
# deterministic answer from the last tool result says more than that.
_NON_ANSWER = re.compile(r"not enough (?:info|data|evidence|detail)|insufficient (?:info|data|evidence)|"
                         r"(?:don'?t|do not|doesn'?t|does not) have (?:enough|sufficient|the (?:info|data|results?))|"
                         r"unable to (?:determine|answer|compute|find|provide)|cannot (?:be )?(?:determine|answer|"
                         r"compute|provide)|can'?t (?:determine|answer|compute|provide)|"
                         r"^\W*(?:unknown|n/?a|undetermined|indeterminate|unavailable)\W*$", re.I)


def non_answer(text: str) -> bool:
    """A reply that gives no answer: empty, a tool call written as text, or 'not enough information'."""
    if not (text or "").strip() or "tool_call>" in text:
        return True
    m = None
    for m in re.finditer(r"ANSWER:\s*(.+)", text, re.I):
        pass
    return bool(_NON_ANSWER.search(m.group(1).strip(" *_`") if m else text))


def _finalize(messages: list[dict], res: RunResult, agent: str, emit: Emit, why: str,
              prefer: str | None = None, step: int | None = None, can_fallback: bool = False,
              post: Callable[[str], str] | None = None) -> bool:
    """One tool-free call asking for the answer from what the tools already returned. With `can_fallback`
    (a deterministic answer exists), a reply that answers nothing counts as a failure."""
    try:
        rep = providers.chat([*messages, {"role": "user", "content": FINALIZE}], prefer=prefer)
    except providers.LLMUnavailable as exc:
        emit({"type": "warning", "agent": agent,
              "message": providers.redact(f"{why}; the final answer call failed too: {exc}")})
        return False
    _count(res, rep)
    _track(res, rep, prefer, step, emit, agent)
    if not rep.content or getattr(rep, "malformed", False) or (can_fallback and non_answer(rep.content)):
        emit({"type": "warning", "agent": agent,
              "message": f"{why}; the final answer call gave no answer, using the last tool result instead"})
        return False
    _add_note(res, f"{why}; answered from the results gathered so far")
    res.final = post(rep.content) if post else rep.content
    emit({"type": "final", "agent": agent, "text": res.final, "provider": rep.provider, "note": res.note})
    return True


def _count(res: RunResult, rep) -> None:
    res.usage["llm_calls"] += 1
    res.usage["prompt"] += rep.usage.get("prompt", 0)
    res.usage["completion"] += rep.usage.get("completion", 0)
    res.providers.append(rep.provider + (" (cached)" if rep.cached else ""))


def _track(res: RunResult, rep, sticky: str | None, step: int | None, emit: Emit, agent: str) -> str:
    """The bucket pinned after this reply. A reply from another bucket than the pin is a switch: it is
    recorded with the provider layer's reason and the pin moves to it, so an answer changes model at most
    once per failure instead of bouncing back (120b -> 20b -> 120b)."""
    if sticky is None or rep.provider == sticky:
        return rep.provider
    sw = {"step": step, "from": sticky, "to": rep.provider,
          "why": getattr(rep, "switched", None) or "the pinned model did not serve this step"}
    res.switches.append(sw)
    _add_note(res, f"model switch at step {(step or 0) + 1}: {sw['from']} -> {sw['to']} ({sw['why']})")
    emit({"type": "provider_switch", "agent": agent, **sw})
    return rep.provider


# what the model reads of a tool result, beyond registry.compact: an unresolved name must be visible to it
_FOR_MODEL = ("not_found", "nearest", "note", "warnings")


def _label_past_bids(x):
    """Bid nodes are past tender submissions (2019-2024 dossiers), never open bids: say so wherever one appears."""
    if isinstance(x, dict):
        y = {k: _label_past_bids(v) for k, v in x.items()}
        if y.get("label") == "Bid" and "kind" not in y:
            y["kind"] = "past_bid"
        return y
    if isinstance(x, list):
        return [_label_past_bids(v) for v in x]
    return x


def blast_summary(r: dict) -> str:
    """One line a model cannot misread: which open cases change, which rely on the fact, which past bids cited it."""
    flips = [f for f in r.get("verdict_flips") or [] if isinstance(f, dict)]
    cases: dict[str, list[str]] = {}
    for f in flips:
        cases.setdefault(f.get("case") or f.get("case_id") or "?", []).append(
            f"{f.get('clause')} {f.get('from')} -> {f.get('to')}")
    relying = sorted({d.get("case") or d.get("case_id") for d in r.get("cases_relying") or [] if isinstance(d, dict)})
    bids = [b.get("bid") for b in r.get("past_bids_citing") or [] if isinstance(b, dict)]
    parts = [f"{len(cases)} open case(s) affected (verdict would flip)"
             + (": " + "; ".join(f"{c} ({', '.join(v)})" for c, v in cases.items()) if cases else "")]
    parts.append(f"{len(relying)} case(s) currently rely on it" + (": " + "; ".join(map(str, relying)) if relying else ""))
    parts.append(f"{len(bids)} past bid(s) cited it (past submissions, not open bids)")
    parts.append(f"{r.get('clauses_rechecked', 0)} open clause(s) re-checked; nothing was changed")
    return ". ".join(parts)


def _for_model(out: dict, tool: str | None = None) -> str:
    r = out.get("result")
    if tool == "blast_radius" and isinstance(r, dict) and "verdict_flips" in r:
        out = {**out, "result": {"summary": blast_summary(r), **r}}
    if r is not None:
        out = {**out, "result": _label_past_bids(out["result"])}
    text = compact(out)
    extra = {k: out[k] for k in _FOR_MODEL if out.get(k) not in (None, [], "")}
    if extra and text.endswith("}"):
        text = text[:-1] + ("," if text != "{}" else "") + json.dumps(extra, ensure_ascii=False, default=str,
                                                                        separators=(",", ":"))[1:]
    return text


# the result field that answers the question, per tool (the first present one wins)
_ANSWER_KEYS = {"qualify_clause": ("verdict",), "prove_absence": ("absent_count",),
                "compliance_check": ("verdict",)}
_SCALAR_KEYS = ("value", "days", "rate_pct", "gap", "total")


def fallback_answer(last: dict | None, why: str) -> str | None:
    """A deterministic answer from the last successful tool result, used when no model can write one.
    It states the tool's own value and evidence, and says plainly that no model interpreted it."""
    if not last:
        return None
    out, r = last["out"], last["out"].get("result")
    value = None
    if isinstance(r, dict):
        keys = _ANSWER_KEYS.get(last["tool"], _SCALAR_KEYS)
        value = next((r[k] for k in keys if k in r and isinstance(r[k], (int, float, str))), None)
    lines = [f"The language model became unavailable ({why}) before it could write the answer, so this is the "
             f"last tool result exactly as the graph returned it, not interpreted by a model. Check it fits the question.",
             "", f"Last step: {last['tool']}({json.dumps(last['args'], default=str)[:300]}) -> {_summary(out)}"]
    if isinstance(r, dict):
        if r.get("claim"):
            lines.append(f"Claim: {r['claim']}" + (f" (verdict {r['verdict']})" if r.get("verdict") else ""))
        if r.get("basis"):
            lines.append(f"Basis: {r['basis']}")
        missing = [a.get("title") or a.get("name") or a.get("key") for a in r.get("absent") or [] if isinstance(a, dict)]
        if missing:
            lines.append("Missing: " + "; ".join(str(m) for m in missing[:20]))
    ev = [f"{e.get('doc_id')} p.{e.get('page')}" for e in out.get("evidence", [])[:8] if isinstance(e, dict)]
    if ev:
        lines.append("Evidence: " + ", ".join(ev))
    if isinstance(value, int) and not isinstance(value, bool):
        shown = str(value)
    elif isinstance(value, float):
        shown = f"{value:.2f}"
    else:
        shown = str(value) if value is not None else "UNAVAILABLE (no model to compute it; see the last tool result)"
    lines += ["", f"ANSWER: {shown}"]
    return "\n".join(lines)


def _calls_from_text(text: str) -> list[dict]:
    calls = []
    for i, m in enumerate(re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text or "", re.S)):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("name"):
            args = obj.get("arguments", {})
            calls.append({"id": f"text-call-{i}", "name": obj["name"],
                          "arguments": args if isinstance(args, str) else json.dumps(args)})
    return calls


def _deterministic(res: RunResult, last_ok: dict | None, why: str, note: str, emit: Emit, agent: str) -> bool:
    """Answer with the last successful tool result, no model involved. False if there is none."""
    text = fallback_answer(last_ok, why)
    if not text:
        return False
    res.final = text
    _add_note(res, f"{note}; deterministic answer from the last tool result ({last_ok['tool']}), no model involved")
    emit({"type": "final", "agent": agent, "text": text, "provider": "deterministic", "note": res.note,
          "deterministic": True})
    return True


def _salvage(g: Graph, messages: list[dict], res: RunResult, agent: str, emit: Emit, exc, sticky, step,
             last_ok: dict | None, post: Callable[[str], str] | None = None) -> None:
    """The LLM failed mid-run: answer anyway. A tool-free finalize call first; if that fails as well (or gives
    no answer), the deterministic answer from the last successful tool result. `res.error` stays set for the
    latter, so a caller that needs a model-written text (the Assembler) still falls back to its own template."""
    kind = getattr(exc, "kind", "unavailable")
    why = f"LLM {kind.replace('_', ' ')} mid-run"
    emit({"type": "warning", "agent": agent, "message": f"{why}: {res.error}; answering from the results so far"})
    if _finalize(messages, res, agent, emit, why, sticky, step, can_fallback=last_ok is not None, post=post):
        res.error = None
        return
    if not _deterministic(res, last_ok, kind.replace("_", " "), why, emit, agent):
        emit({"type": "error", "agent": agent, "message": res.error})


def _check_args(g: Graph, name: str, args: dict) -> str | None:
    """Argument problems a tool would not report itself (an unknown doc_type silently matches nothing)."""
    if name in ("vector_search", "fulltext_search") and args.get("doc_type"):
        from bidbrain.tools.read import doc_type_error
        return doc_type_error(g, args["doc_type"])
    return None


def run_agent(g: Graph, agent: str, system: str, user: str, tool_names: list[str],
              max_steps: int = 8, emit: Emit | None = None, history: list[dict] | None = None,
              ground: bool = False) -> RunResult:
    """Run one agent to an answer. `ground=True` (the Q&A agent) adds the answer checks: a tool-free answer to
    an entity / number question is re-asked once, names and amounts absent from every tool result are listed
    back once, and prove_absence's missing items are named. Each re-ask is free (it does not use a step)."""
    emit = emit or (lambda e: None)
    t0 = time.perf_counter()
    res = RunResult(agent=agent, final="")
    offered = [t for t in tool_names if t in TOOLS]
    tools = schemas(offered, brief=BRIEF)
    messages = [{"role": "system", "content": system}, *(history or []), {"role": "user", "content": user}]
    # what the user actually asked, for the unasked-filter guard: this turn and earlier user turns
    asked = "\n".join([*(m.get("content") or "" for m in history or [] if m.get("role") == "user"), user or ""])
    # what an answer may name: the instructions, the conversation, and every tool result of this run
    corpus = [system or "", *(str(m.get("content") or "") for m in history or []), user or ""]
    sticky = None       # the bucket pinned after the first step: one model per answer
    last_ok = None      # the last tool result without an error, for a deterministic answer if every model fails
    absence = None      # the last successful prove_absence result: its missing items must be named
    reasked: set[str] = set()
    pending: str | None = None      # an answer that was re-asked about, kept if the re-ask fails

    def post(text: str) -> str:
        """The answer checks that change no model output: notes, and the missing items named."""
        if not ground:
            return text
        if res.usage["tool_calls"] == 0:
            if needs_tools(user):
                res.checks["ungrounded"] = True
                _add_note(res, "UNGROUNDED: answered without calling any tool, so nothing in this answer was "
                               "checked against the company graph")
        elif missing := ungrounded(text, "\n".join(corpus)):
            res.checks["unverified"] = missing
            _add_note(res, "UNVERIFIED: these names or amounts do not appear in any tool result: "
                      + "; ".join(missing[:12]))
        if absence is not None:
            text, n = name_absent(text, absence)
            if n:
                res.checks["absent_named"] = n
                _add_note(res, f"named the {n} item(s) prove_absence lists as missing, which the answer left out")
        return text

    def accept(text: str, provider: str | None) -> None:
        res.final = post(text or "")
        emit({"type": "final", "agent": agent, "text": res.final, "provider": provider,
              **({"note": res.note} if res.note else {})})

    def reask(kind: str, text: str, prompt: str, why: str) -> None:
        nonlocal pending
        reasked.add(kind)
        res.checks.setdefault("reasked", []).append(kind)
        pending = text
        emit({"type": "warning", "agent": agent, "message": f"re-asking: {why}"})
        messages.append({"role": "assistant", "content": text or ""})
        messages.append({"role": "user", "content": prompt})

    emit({"type": "agent_start", "agent": agent})
    budget, step, done = max_steps, 0, False
    while step < budget:
        try:
            rep = providers.chat(messages, tools=tools, prefer=sticky)
        except providers.LLMUnavailable as exc:
            res.error = providers.redact(str(exc))
            if pending is not None:     # a re-ask failed: the answer we have stands, with its checks
                res.error = None
                _add_note(res, f"the re-ask failed (LLM {getattr(exc, 'kind', 'unavailable').replace('_', ' ')})")
                accept(pending, None)
            elif res.steps:             # evidence was gathered: it deserves an answer
                _salvage(g, messages, res, agent, emit, exc, sticky, step, last_ok, post)
            else:
                emit({"type": "error", "agent": agent, "message": res.error})
            done = True
            break
        _count(res, rep)
        sticky = _track(res, rep, sticky, step, emit, agent)
        if not rep.tool_calls and (getattr(rep, "malformed", False) or "tool_call>" in (rep.content or "")):
            # Qwen sometimes writes its call as text the server's hermes parser misses (or the server refuses
            # it): recover the call from the text, or (once) ask the same model for a well-formed one instead
            # of taking "</tool_call>" as the answer
            rep.tool_calls = _calls_from_text(rep.content)
            if not rep.tool_calls:
                if "malformed" not in reasked:
                    reasked.add("malformed")
                    _add_note(res, "re-asked after a malformed tool call")
                    messages.append({"role": "assistant", "content": rep.content or ""})
                    messages.append({"role": "user", "content": "That tool call was malformed. Call the tool again "
                                     "with valid JSON arguments, or give the final ANSWER line."})
                    budget += 1
                    step += 1
                    continue
                done = True             # malformed twice: never take a broken tool call as the answer
                if pending is not None:
                    accept(pending, rep.provider)
                elif not (res.steps and _finalize(messages, res, agent, emit, "malformed tool calls", sticky, step,
                                                  can_fallback=last_ok is not None, post=post)) \
                        and not _deterministic(res, last_ok, "malformed tool calls", "malformed tool calls", emit, agent):
                    res.error = "the model wrote only malformed tool calls"
                    emit({"type": "error", "agent": agent, "message": res.error})
                break
        if not rep.tool_calls:
            text = rep.content
            if ground and res.usage["tool_calls"] == 0 and needs_tools(user) and "tool_free" not in reasked:
                reask("tool_free", text, TOOL_FREE_REASK, "the answer used no tool")
                budget += 1
                step += 1
                continue
            if ground and res.usage["tool_calls"] and "grounding" not in reasked and \
                    (missing := ungrounded(text, "\n".join(corpus))):
                reask("grounding", text, grounding_reask(missing[:12]),
                      "not in any tool result: " + "; ".join(missing[:6]))
                budget += 1
                step += 1
                continue
            accept(text, rep.provider)
            done = True
            break
        if rep.content:
            emit({"type": "thought", "agent": agent, "text": rep.content})
        messages.append({"role": "assistant", "content": rep.content or None,
                         "tool_calls": [{"id": tc["id"], "type": "function",
                                         "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                                        for tc in rep.tool_calls]})
        for tc in rep.tool_calls:
            try:
                args = json.loads(tc["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            emit({"type": "tool_call", "agent": agent, "tool": tc["name"], "args": args, "step": step})
            if tc["name"] not in offered:   # registered or not, a tool this agent was not given must not run
                out = {"error": f"tool {tc['name']!r} is not available here", "available": offered}
            elif bad := _check_args(g, tc["name"], args):
                out = {"error": bad}
            else:
                out = call(g, tc["name"], args)
            res.usage["tool_calls"] += 1
            res.evidence += [e for e in out.get("evidence", []) if e not in res.evidence]
            _merge_subgraph(res.subgraph, out.get("subgraph", {}))
            step_rec = {"tool": tc["name"], "args": args, "ms": out.get("ms"), "error": out.get("error"),
                        "cypher": out.get("cypher", [])}
            flags = [] if "error" in out else unasked_filters(tc["name"], args, asked, scope_names(args, out))
            note = unasked_note(flags) if flags else ""
            if flags:
                step_rec["unasked_filters"] = flags
            if "error" not in out:
                last_ok = {"tool": tc["name"], "args": args, "out": out}
                if tc["name"] == "prove_absence":
                    absence = out
            res.steps.append(step_rec)
            emit({"type": "tool_result", "agent": agent, "tool": tc["name"], "ms": out.get("ms"),
                  "error": out.get("error"), "summary": _summary(out), "subgraph": out.get("subgraph"),
                  "evidence": out.get("evidence", [])[:12], "cypher": out.get("cypher", []),
                  **({"not_found": True} if out.get("not_found") else {}),
                  **({"note": note.strip(), "unasked_filters": flags} if flags else {})})
            seen = _for_model(out, tc["name"])
            corpus.append(seen)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": seen[:9000 - len(note)] + note})
        step += 1
    if not done:
        why = "step budget exhausted"
        if not _finalize(messages, res, agent, emit, why, sticky, budget, can_fallback=last_ok is not None, post=post):
            if pending is not None:
                _add_note(res, f"{why}; kept the answer given before the last re-ask")
                accept(pending, None)
            else:
                res.error = why
                if not _deterministic(res, last_ok, "step budget exhausted and the final answer call failed", why,
                                      emit, agent):
                    emit({"type": "error", "agent": agent, "message": res.error})
    res.ms = round((time.perf_counter() - t0) * 1000, 1)
    return res


def _summary(out: dict) -> str:
    if "error" in out:
        return f"error: {out['error']}"
    if out.get("not_found"):
        near = ", ".join(f"{n.get('name')} ({n.get('score')})" for n in out.get("nearest", [])[:3])
        return f"not found in the graph; nearest: {near}" if near else "not found in the graph"
    r = out.get("result")
    if isinstance(r, dict):
        for k in ("claim", "basis", "verdict"):
            if k in r:
                return str(r[k])[:200]
        if "n" in r and "total_value" in r:
            return f"{r['n']} works, total {r['total_value']:,}"
        if "value" in r and "op" in r:
            return f"{r['op']} = {r['value']}"
        return json.dumps(r, default=str)[:200]
    if isinstance(r, list):
        return f"{len(r)} rows"
    return str(r)[:200]
