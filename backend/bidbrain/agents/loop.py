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
* the provider bucket that served the first step is preferred for the rest of the run, so a single
  answer is not stitched together from several models (the pool still takes over on a rate limit).
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


# ── unasked-filter guard ────────────────────────────────────────────────────
# A model answering "what is the total value of X's works" sometimes adds role='Prime' or a years range on
# its own, silently shrinking the answer. After each call to a tool with narrowing filters, every filter
# the model set is checked for *lexical* support in what the user asked (this question and earlier user
# turns); an unsupported one earns a NOTE in the tool result the model reads. Scope arguments (client,
# person, via_work, measure, evidence) are never questioned: only filters that narrow a set.
_DATES = (r"\b(?:19|20)\d{2}\b|\b(?:after|before|since|between|until|till|prior|during|decade|fy|financial|"
          r"years?|yearly|annual|months?|dated?|period|window|recent(?:ly)?|latest|earliest|older|newer|"
          r"old|new|ago|so far|to date)\b")
_EVIDENCE = {
    "role": r"\bprime\b|\bjv\b|joint[\s-]?venture|\bpartner|consortium|\bsole\b|\bsolo\b|\bon our own\b|"
            r"\bindependent|\brole\b|\blead (?:contractor|member|partner)",
    "dates": _DATES,
    "valid_on": _DATES + r"|\bvalid|\bexpir|\blaps|\bcurrent|\btoday\b|\bnow\b|\bon the bid\b",
    "value": r"\u20b9|\brs\.?\b|\binr\b|rupee|\bcr\b|crore|lakh|\blacs?\b|million|billion|\bvalu|worth|\bcost|"
             r"\bprice|\bamount|\babove\b|\bbelow\b|\bover\b|\bunder\b|more than|less than|at least|at most|"
             r"exceed|minimum|maximum|threshold|\blarg|\bsmall|\bbig|\bsize|\bbigger|\d\s*(?:cr|l|k|m|bn|mn)\b",
    "reference": r"referenc|\bletters?\b|testimonial|recommendation",
    # client names ("... Corporation", "Public Works Department") are not evidence of a government-level filter
    "govt": r"\bgovt|government|\bcentral|\bstates?\b|\bpsus?\b|public[\s-]sector|municipal|civic|private|"
            r"ministry|\bunion\b|\blocal bod",
    "status": r"\bpaid\b|\bdue\b|overdue|outstanding|unpaid|partial|part[\s_-]?paid|settled|pending|\bopen\b|"
              r"\bstatus|\bcleared\b",
    "category": r"categor|\bsector|\btype|\bkind|\bsegment|\bsimilar\b|\bsame\b|\bsuch\b|\bline of",
    "exclude": r"\bexcept|exclud|other than|apart from|\bbesides\b|\bnot\b|\bnon[\s-]|\bwithout\b|\boutside\b|"
               r"\bbut\b|\bignor|\bleave out",
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


def unasked_filters(tool: str, args: dict, asked: str) -> list[str]:
    """Narrowing arguments of a tool call that nothing in `asked` (the user's words) supports,
    each as `name=value`. Lexical and generous on purpose: it only flags a filter with no trace at all."""
    spec = NARROWING.get(tool)
    if not spec or not isinstance(args, dict):
        return []
    asked = (asked or "").lower()
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
              prefer: str | None = None) -> bool:
    """One tool-free call asking for the answer from what the tools already returned."""
    try:
        rep = providers.chat([*messages, {"role": "user", "content": FINALIZE}], prefer=prefer)
    except providers.LLMUnavailable as exc:
        emit({"type": "error", "agent": agent, "message": f"{why}; final answer call failed: {exc}"})
        return False
    _count(res, rep)
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
    sticky = None       # the bucket that served the first step: later steps prefer it (one model per answer)
    emit({"type": "agent_start", "agent": agent})
    for step in range(max_steps):
        try:
            rep = providers.chat(messages, tools=tools, prefer=sticky)
        except providers.LLMUnavailable as exc:
            res.error = str(exc)
            emit({"type": "error", "agent": agent, "message": str(exc)})
            # the endpoint rejected this particular request (e.g. a malformed generated tool call): a
            # tool-free request may still go through, and the evidence gathered so far deserves an answer
            if res.steps and getattr(exc, "kind", None) == "request" \
                    and _finalize(messages, res, agent, emit, f"LLM request failed mid-run ({exc})", sticky):
                res.error = None
            break
        _count(res, rep)
        sticky = sticky or rep.provider
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
            emit({"type": "tool_call", "agent": agent, "tool": tc["name"], "args": args, "step": step})
            if tc["name"] in offered:
                out = call(g, tc["name"], args)
            else:       # registered or not, a tool this agent was not given must not run
                out = {"error": f"tool {tc['name']!r} is not available here", "available": offered}
            res.usage["tool_calls"] += 1
            res.evidence += [e for e in out.get("evidence", []) if e not in res.evidence]
            _merge_subgraph(res.subgraph, out.get("subgraph", {}))
            step_rec = {"tool": tc["name"], "args": args, "ms": out.get("ms"), "error": out.get("error"),
                        "cypher": out.get("cypher", [])}
            flags = [] if "error" in out else unasked_filters(tc["name"], args, asked)
            note = unasked_note(flags) if flags else ""
            if flags:
                step_rec["unasked_filters"] = flags
            res.steps.append(step_rec)
            emit({"type": "tool_result", "agent": agent, "tool": tc["name"], "ms": out.get("ms"),
                  "error": out.get("error"), "summary": _summary(out), "subgraph": out.get("subgraph"),
                  "evidence": out.get("evidence", [])[:12], "cypher": out.get("cypher", []),
                  **({"note": note.strip(), "unasked_filters": flags} if flags else {})})
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": compact(out)[:9000 - len(note)] + note})
    else:
        if not _finalize(messages, res, agent, emit, "step budget exhausted", sticky):
            res.error = "step budget exhausted"
            emit({"type": "error", "agent": agent, "message": res.error})
    res.ms = round((time.perf_counter() - t0) * 1000, 1)
    return res


def _summary(out: dict) -> str:
    if "error" in out:
        return f"error: {out['error']}"
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
