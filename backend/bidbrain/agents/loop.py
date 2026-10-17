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


def _finalize(messages: list[dict], res: RunResult, agent: str, emit: Emit, why: str,
              prefer: str | None = None, step: int | None = None) -> bool:
    """One tool-free call asking for the answer from what the tools already returned."""
    try:
        rep = providers.chat([*messages, {"role": "user", "content": FINALIZE}], prefer=prefer)
    except providers.LLMUnavailable as exc:
        emit({"type": "warning", "agent": agent,
              "message": providers.redact(f"{why}; the final answer call failed too: {exc}")})
        return False
    _count(res, rep)
    _track(res, rep, prefer, step, emit, agent)
    if not rep.content:
        return False
    res.final, res.note = rep.content, f"{why}; answered from the results gathered so far"
    emit({"type": "final", "agent": agent, "text": rep.content, "provider": rep.provider, "note": res.note})
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
    emit({"type": "provider_switch", "agent": agent, **sw})
    return rep.provider


# what the model reads of a tool result, beyond registry.compact: an unresolved name must be visible to it
_FOR_MODEL = ("not_found", "nearest", "note", "warnings")


def _for_model(out: dict) -> str:
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


def _salvage(g: Graph, messages: list[dict], res: RunResult, agent: str, emit: Emit, exc, sticky, step,
             last_ok: dict | None) -> None:
    """The LLM failed mid-run: answer anyway. A tool-free finalize call first; if that fails as well, the
    deterministic answer from the last successful tool result. `res.error` stays set for the latter, so a
    caller that needs a model-written text (the Assembler) still falls back to its own template."""
    kind = getattr(exc, "kind", "unavailable")
    why = f"LLM {kind.replace('_', ' ')} mid-run"
    emit({"type": "warning", "agent": agent, "message": f"{why}: {res.error}; answering from the results so far"})
    if _finalize(messages, res, agent, emit, why, sticky, step):
        res.error = None
        return
    text = fallback_answer(last_ok, kind.replace("_", " "))
    if text:
        res.final = text
        res.note = f"{why}; deterministic answer from the last tool result ({last_ok['tool']}), no model involved"
        emit({"type": "final", "agent": agent, "text": text, "provider": "deterministic", "note": res.note,
              "deterministic": True})
    else:
        emit({"type": "error", "agent": agent, "message": res.error})


def _check_args(g: Graph, name: str, args: dict) -> str | None:
    """Argument problems a tool would not report itself (an unknown doc_type silently matches nothing)."""
    if name in ("vector_search", "fulltext_search") and args.get("doc_type"):
        from bidbrain.tools.read import doc_type_error
        return doc_type_error(g, args["doc_type"])
    return None


def run_agent(g: Graph, agent: str, system: str, user: str, tool_names: list[str],
              max_steps: int = 8, emit: Emit | None = None, history: list[dict] | None = None) -> RunResult:
    emit = emit or (lambda e: None)
    t0 = time.perf_counter()
    res = RunResult(agent=agent, final="")
    offered = [t for t in tool_names if t in TOOLS]
    tools = schemas(offered, brief=BRIEF)
    messages = [{"role": "system", "content": system}, *(history or []), {"role": "user", "content": user}]
    # what the user actually asked, for the unasked-filter guard: this turn and earlier user turns
    asked = "\n".join([*(m.get("content") or "" for m in history or [] if m.get("role") == "user"), user or ""])
    sticky = None       # the bucket pinned after the first step: one model per answer
    last_ok = None      # the last tool result without an error, for a deterministic answer if every model fails
    emit({"type": "agent_start", "agent": agent})
    for step in range(max_steps):
        try:
            rep = providers.chat(messages, tools=tools, prefer=sticky)
        except providers.LLMUnavailable as exc:
            res.error = providers.redact(str(exc))
            if res.steps:       # evidence was gathered: it deserves an answer
                _salvage(g, messages, res, agent, emit, exc, sticky, step, last_ok)
            else:
                emit({"type": "error", "agent": agent, "message": res.error})
            break
        _count(res, rep)
        sticky = _track(res, rep, sticky, step, emit, agent)
        if not rep.tool_calls and "tool_call>" in (rep.content or ""):
            # Qwen sometimes writes its call as text the server's hermes parser misses: recover the call
            # from the text, or (once) ask for a well-formed one instead of taking "</tool_call>" as the answer
            rep.tool_calls = _calls_from_text(rep.content)
            if not rep.tool_calls and not res.note:
                res.note = "re-asked after a malformed tool call"
                messages.append({"role": "assistant", "content": rep.content})
                messages.append({"role": "user", "content": "That tool call was malformed. Call the tool again "
                                 "with valid JSON arguments, or give the final ANSWER line."})
                continue
        if not rep.tool_calls:
            res.final = rep.content
            emit({"type": "final", "agent": agent, "text": rep.content, "provider": rep.provider})
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
            res.steps.append(step_rec)
            emit({"type": "tool_result", "agent": agent, "tool": tc["name"], "ms": out.get("ms"),
                  "error": out.get("error"), "summary": _summary(out), "subgraph": out.get("subgraph"),
                  "evidence": out.get("evidence", [])[:12], "cypher": out.get("cypher", []),
                  **({"not_found": True} if out.get("not_found") else {}),
                  **({"note": note.strip(), "unasked_filters": flags} if flags else {})})
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": _for_model(out)[:9000 - len(note)] + note})
    else:
        if not _finalize(messages, res, agent, emit, "step budget exhausted", sticky, max_steps):
            res.error = "step budget exhausted"
            text = fallback_answer(last_ok, "step budget exhausted and the final answer call failed")
            if text:
                res.final = text
                res.note = "step budget exhausted; deterministic answer from the last tool result, no model involved"
                emit({"type": "final", "agent": agent, "text": text, "provider": "deterministic",
                      "note": res.note, "deterministic": True})
            else:
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
