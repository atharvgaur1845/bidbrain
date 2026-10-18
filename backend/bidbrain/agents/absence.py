"""Negative claims in a Q&A answer, and the answer's status.

An empty tool result is not proof that something does not exist: blast_radius returning empty lists, a vector
search with no hits or a lookup on a name that did not resolve all say "nothing found this attempt", not
"there is none". This module finds the negative / absence claims in an answer ("none", "no open tenders",
"would not be affected", "0 items", "never", "not found") and decides whether this run proved them:

* `prove_absence` with verdict PROVEN (its closure / orphan checks passed) proves an absence in its scope;
* a scoped count over a defined set proves one too: a find_portfolio / aggregate / list_cases result whose
  scope entity resolved (by resolve_entity or the tool's own `resolved_as`), with no name left unresolved;
* "X is not in the company graph" is supported by a resolve_entity (or tool) not_found for that name.

Anything else is unsupported: the loop re-asks once naming the absence tool, and if the negative survives,
`rewrite_unsupported` replaces it with a deterministic uncertainty statement (no model writes it).

`classify` gives every answer one status: VERIFIED_ABSENCE, NO_MATCH_THIS_ATTEMPT, ENTITY_UNRESOLVED,
INCOMPLETE_OR_CONTRADICTORY, NEEDS_REVIEW, or GROUNDED (an affirmative answer that every check passed).
"""
from __future__ import annotations

import re

VERIFIED_ABSENCE = "VERIFIED_ABSENCE"
NO_MATCH = "NO_MATCH_THIS_ATTEMPT"
ENTITY_UNRESOLVED = "ENTITY_UNRESOLVED"
INCOMPLETE = "INCOMPLETE_OR_CONTRADICTORY"
NEEDS_REVIEW = "NEEDS_REVIEW"
GROUNDED = "GROUNDED"

_NOUNS = (r"(?:tenders?|bids?|cases?|works?|projects?|jobs?|items?|records?|results?|rows?|matches|hits|documents?|"
          r"docs?|letters?|references?|clients?|people|persons?|leads?|credentials?|certificat\w*|pmps?|bonds?|"
          r"invoices?|clauses?|contracts?|entries|claims?|facts?|decisions?|findings?|actions?|impact|effect|"
          r"dependen\w*|evidence|change|changes|flips?|exposure|risk|proof)")
_NEG = re.compile(
    r"\bnone\b|\bnothing\b(?!\s+(?:was|is|has been|will be)\s+(?:changed|written|modified))|\bnever\b|\bnot found\b|"
    r"\bno(?:\s+\w+){0,3}?\s+" + _NOUNS + r"\b|"
    r"\b(?:zero|0)\s+(?:\w+\s+){0,3}?" + _NOUNS + r"\b|"
    r"\b(?:would|will|is|are|was|were)\s+(?:not|n't)\s+(?:be\s+)?(?:affected|impacted|hurt|harmed|at risk|exposed|"
    r"found|on file|present|available|cited|relied on|touched)\b|"
    r"\b(?:isn't|aren't|wasn't|weren't|won't be)\s+(?:affected|impacted|hurt|found|on file|present|available)\b|"
    r"\b(?:does|do|did)\s+(?:not|n't)\s+(?:exist|appear|have any|hold any|lead any|affect|impact)\b|"
    r"\bthere (?:are|is|were|was) no\b|\bno one\b|\bnobody\b|\bunaffected\b",
    re.I)
_NEG_ANSWER = re.compile(r"^\W*(?:no|none|nothing|zero|0|0 items|nil|n/?a|never|not found|no one|nobody)\W*$", re.I)
# "X is not in the company graph": a negative about an entity's existence, backed by a not_found
_NOT_IN_GRAPH = re.compile(r"\bnot (?:in|found in|present in|recorded in|known to) the (?:company )?(?:graph|record)|"
                           r"\bno (?:client|person|work|tender|entity|company|credential)s? (?:named|called|like|matching)\b",
                           re.I)
_ANSWER_LINE = re.compile(r"^\W*ANSWER:\s*(.*)$", re.I)
SCOPED_TOOLS = ("find_portfolio", "aggregate", "list_cases", "credential_timeline", "receivables", "who_knows")
UNCERTAIN_PREFIX = "Not established:"
VERIFIED_PREFIX = "Absence verified"
UNRESOLVED_PREFIX = "Not resolved:"


def negatives(text: str) -> list[str]:
    """The negative / absence claims in `text`, as the phrases that make them (the ANSWER value counts)."""
    out = []
    for ln in (text or "").splitlines():
        if ln.startswith((UNCERTAIN_PREFIX, VERIFIED_PREFIX, UNRESOLVED_PREFIX, "Missing (listed by prove_absence)",
                          "Now vs then:")):
            continue
        m = _ANSWER_LINE.match(ln)
        if m and _NEG_ANSWER.match(m.group(1).strip(" *_`.")):
            out.append(f"ANSWER: {m.group(1).strip()}")
            continue
        for x in _NEG.finditer(ln):
            out.append(x.group(0))
    return out


def _is_not_in_graph(phrase_line: str) -> bool:
    return bool(_NOT_IN_GRAPH.search(phrase_line))


# ── what this run established ───────────────────────────────────────────────

def _err_unresolved(err: str) -> str | None:
    m = re.search(r"no \w+ named like '([^']+)'|could not resolve \w+ '([^']+)'|no work with package number (\d+)",
                  err or "")
    return next((x for x in m.groups() if x), None) if m else None


def evidence_state(trail: list[dict]) -> dict:
    """What the run's tool calls establish about absences: PROVEN / UNPROVEN prove_absence results, scoped sets,
    names that did not resolve (not_found / ambiguous), and empty results."""
    proven, unproven, scoped, empty = [], [], [], []
    unresolved: dict[str, str] = {}     # lower(name) -> why
    resolved: set[str] = set()
    for t in trail:
        tool, args, out = t["tool"], t.get("args") or {}, t.get("out") or {}
        r = out.get("result")
        if out.get("error"):
            if name := _err_unresolved(str(out["error"])):
                why = "ambiguous" if "unambiguously" in str(out["error"]) else "not_found"
                unresolved[name.lower()] = why
            continue
        if tool == "resolve_entity":
            text = str(args.get("text") or "").lower()
            if out.get("not_found") or not r:
                unresolved[text] = "not_found"
            elif out.get("ambiguous") or (isinstance(r, list) and len(r) > 1 and r[0].get("ambiguous")):
                unresolved[text] = "ambiguous"
            else:
                resolved.add(text)
            continue
        if out.get("not_found"):
            for a in ("client", "person", "via_work", "work", "key", "text"):
                if isinstance(args.get(a), str):
                    unresolved[args[a].lower()] = "not_found"
            continue
        # a tool that succeeded resolved its scope arguments (it raises on a name it cannot resolve)
        for a in ("client", "person", "via_work", "work"):
            if isinstance(args.get(a), str) and args[a].strip():
                resolved.add(args[a].lower())
        if tool == "prove_absence" and isinstance(r, dict):
            (proven if r.get("verdict") == "PROVEN" else unproven).append(t)
        elif tool in SCOPED_TOOLS:
            scoped.append(t)
        if r in ([], {}, None) or (isinstance(r, dict) and (r.get("n") == 0 or _all_empty(r))):
            empty.append(t)
    for name in list(unresolved):       # narrowed later ("PHED" ambiguous, then "PHED Odisha" resolved)
        if any(name in r or (r and r in name) for r in resolved):
            unresolved.pop(name)
    # a scoped set counts only when its scope is a resolved entity (or needs none: list_cases)
    scoped = [t for t in scoped if t["tool"] == "list_cases" or resolved]
    dependents = sum(len((t["out"].get("result") or {}).get("dependents") or []) for t in trail
                     if t["tool"] == "blast_radius" and isinstance(t["out"].get("result"), dict))
    return {"dependents": dependents, "proven": proven, "unproven": unproven, "scoped": scoped, "empty": empty,
            "unresolved": unresolved, "resolved": sorted(resolved)}


def _all_empty(r: dict) -> bool:
    lists = [v for v in r.values() if isinstance(v, list)]
    return bool(lists) and all(not v for v in lists) and not any(
        isinstance(v, (int, float)) and not isinstance(v, bool) and v for k, v in r.items()
        if k not in ("clauses_rechecked",))


# which scoped set can back a negative about what (the first matching class decides; no match: any set)
_CLASS = [(re.compile(r"tender|bid|case|clause|affected|impacted|hurt|harm|at risk|exposed|flip|relied|depend|"
                      r"exposure|risk|touched", re.I), ("list_cases",)),
          (re.compile(r"invoice|billed|paid|receiv", re.I), ("receivables",)),
          (re.compile(r"credential|pmp|certific", re.I), ("credential_timeline",)),
          (re.compile(r"letter|reference|bond", re.I), ()),          # only prove_absence proves these
          (re.compile(r"work|project|job|client|lead|led|people|person", re.I),
           ("find_portfolio", "aggregate", "who_knows"))]


def _backing(neg: str, line: str, state: dict) -> list[dict]:
    """The scoped sets that can back this negative. An impact negative is never backed when blast_radius
    listed dependents: the proof of impact contradicts it."""
    if state["unresolved"]:
        return []
    if state.get("dependents") and (_CLASS[0][0].search(neg) or _CLASS[0][0].search(line)):
        return []
    for rx, tools in _CLASS:
        if rx.search(neg) or (neg.lower().startswith(("answer:", "none", "nothing", "never")) and rx.search(line)):
            return [t for t in state["scoped"] if t["tool"] in tools]
    return list(state["scoped"])


def unsupported(text: str, state: dict) -> list[str]:
    """The negative claims in `text` that nothing in `state` proves."""
    if state["proven"]:
        return []
    out = []
    for ln in (text or "").splitlines():
        for neg in negatives(ln):
            if _is_not_in_graph(ln) and any(w == "not_found" for w in state["unresolved"].values()):
                continue
            if _backing(neg, ln, state):
                continue
            out.append(neg)
    return out


def backing_sets(text: str, state: dict) -> list[dict]:
    """The scoped sets that back the negatives in `text` (for the verified line)."""
    used = []
    for ln in (text or "").splitlines():
        for neg in negatives(ln):
            used += [t for t in _backing(neg, ln, state) if t not in used]
    return used


def absence_hint(negs: list[str]) -> str:
    """Which absence tool would prove the negative, for the one re-ask."""
    shown = "; ".join(dict.fromkeys(negs[:6]))
    return ("Your answer states that something does not exist or is not affected (" + shown + "), but no tool in this "
            "run proved that absence: an empty result only means nothing was found this attempt. To state a "
            "negative, either call prove_absence (evidence: reference_letter | valid_credential | performance_bond) "
            "and quote its verdict, scope and checks, or resolve the entity with resolve_entity and count over a "
            "defined set (find_portfolio / aggregate op=count / list_cases). If neither applies, say what the "
            "tools returned and that absence is not established, without concluding 'none'. Then reply again "
            "with the path you followed and the final ANSWER line.")


# ── deterministic texts ─────────────────────────────────────────────────────

def _fmt_checks(r: dict) -> str:
    ck = r.get("checks") if isinstance(r, dict) else None
    return "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in ck.items()) if isinstance(ck, dict) else "none reported"


def _scope_text(t: dict) -> str:
    r, args = t["out"].get("result") or {}, t.get("args") or {}
    if t["tool"] == "list_cases":
        n = len(r) if isinstance(r, list) else len(r.get("cases") or []) if isinstance(r, dict) else "?"
        return f"this workspace's bid cases ({n} listed)"
    sc = (r.get("scope") if isinstance(r, dict) else None) or t["out"].get("scope") or \
        {k: v for k, v in args.items() if v not in (None, "", [])}
    if isinstance(sc, dict):
        sc = ", ".join(f"{k}={v}" for k, v in sc.items() if v not in (None, "", []))
    return str(sc or "the whole graph")


def uncertainty(state: dict, trail: list[dict]) -> str:
    """The deterministic statement that replaces an unproven negative."""
    parts = []
    if state["unresolved"]:
        parts.append("; ".join(f"'{n}' did not resolve to one graph entity ({w.replace('_', ' ')})"
                               for n, w in state["unresolved"].items()))
    for t in state["unproven"]:
        r = t["out"]["result"]
        parts.append(f"prove_absence returned {r.get('verdict')} over {_scope_text(t)} (checks: {_fmt_checks(r)})")
    ran = [t for t in trail if t["tool"] not in ("resolve_entity",)]
    if state.get("dependents"):
        parts.append(f"blast_radius lists {state['dependents']} dependent(s) with paths, which contradicts it")
    if empty := [t for t in state["empty"] if t not in state["unproven"]]:
        parts.append("empty result from " + ", ".join(sorted({t["tool"] for t in empty})) + " in this attempt")
    elif ran and not state["unproven"]:
        parts.append("tools run: " + ", ".join(dict.fromkeys(t["tool"] for t in ran)))
    why = "; ".join(parts) or "no tool result covers it"
    return (f"{UNCERTAIN_PREFIX} the tools did not prove this absence ({why}). An empty or unproven result shows only "
            "what this attempt returned; the absence is not established, so check the record before relying on it.")


def verified_line(state: dict, used: list[dict] | None = None) -> str:
    """Scope, checks and limits of a proven negative."""
    if state["proven"]:
        t = state["proven"][-1]
        r = t["out"]["result"]
        return (f"{VERIFIED_PREFIX} (prove_absence {r.get('verdict')}): scope {_scope_text(t)}, "
                f"{r.get('universe', '?')} item(s) checked; checks: {_fmt_checks(r)}. Limits: closed-world over the "
                "company graph as loaded; documents not ingested are not covered.")
    t = (used or state["scoped"])[-1]
    return (f"{VERIFIED_PREFIX} (scoped count via {t['tool']}): scope {_scope_text(t)}"
            + (f"; resolved: {', '.join(state['resolved'])}" if state["resolved"] else "")
            + ". Checks: the scope entity resolved by name; the count is over that defined set only. Limits: "
              "items outside the set, or not yet in the graph, are not covered.")


def rewrite_unsupported(text: str, state: dict, trail: list[dict]) -> tuple[str, int]:
    """`text` with each line that carries an unproven negative replaced (once) by the uncertainty statement;
    a negative ANSWER value becomes NOT ESTABLISHED. Returns the text and how many lines were replaced."""
    lines, out, n, said = (text or "").splitlines(), [], 0, False
    statement = uncertainty(state, trail)
    for ln in lines:
        negs = negatives(ln)
        if negs and not (_is_not_in_graph(ln) and "not_found" in state["unresolved"].values()):
            n += 1
            if _ANSWER_LINE.match(ln):
                if not said:
                    out.append(statement)
                    said = True
                out.append("ANSWER: NOT ESTABLISHED (no tool proved this absence)")
                continue
            if not said:
                out.append(statement)
                said = True
            continue
        out.append(ln)
    return "\n".join(out), n


def unresolved_line(state: dict) -> str:
    return (f"{UNRESOLVED_PREFIX} " + "; ".join(f"'{n}' ({w.replace('_', ' ')})" for n, w in state["unresolved"].items())
            + ". Nothing about an unresolved name was checked; narrow the name (or pick one candidate) and ask again.")


def classify(*, has_negative: bool, negative_ok: bool, state: dict, contradiction: bool, incomplete: bool,
             review: bool, tool_free: bool = False) -> str:
    if state["unresolved"]:
        return ENTITY_UNRESOLVED
    if tool_free:               # nothing was checked against the graph
        return NEEDS_REVIEW
    if has_negative:
        if negative_ok:
            return VERIFIED_ABSENCE
        return INCOMPLETE if (state["unproven"] or state.get("dependents") or contradiction or incomplete) \
            else NO_MATCH
    if state["unproven"] or contradiction or incomplete:
        return INCOMPLETE
    if review:
        return NEEDS_REVIEW
    return GROUNDED
