"""Single-agent Q&A over the graph (the /ask page and the benchmark's 'BidBrain' system)."""
from __future__ import annotations

import re

from bidbrain.agents.loop import RunResult, run_agent
from bidbrain.agents.prompts import QA_SYSTEM, QA_TOOLS
from bidbrain.graph.client import Graph

ANSWER_RE = re.compile(r"ANSWER:\s*(.+)", re.I)
MARKDOWN_RE = re.compile(r"[*`]+|^\s*(?:#+|>)\s*", re.M)
UNIT = [(re.compile(r"\b(?:crores?|cr)\b\.?", re.I), 10**7), (re.compile(r"\b(?:lakhs?|lacs?)\b", re.I), 10**5)]


def parse_answer(text: str):
    """The value after the last `ANSWER:` line, as a number when it is one.

    Shared by BidBrain and every baseline, so formatting quirks cost no system more than another:
    markdown emphasis (`**ANSWER:** 12`), a trailing gloss (`2,942,400,000 (₹294.24 Cr)`), and a value
    written in crore or lakh (`₹294.24 Cr`) all reduce to the same integer rupees.
    """
    text = MARKDOWN_RE.sub("", text or "")
    m = None
    for m in ANSWER_RE.finditer(text):
        pass
    raw = (m.group(1) if m else text.strip().splitlines()[-1] if text.strip() else "").strip().strip("_").strip()
    head = re.split(r"\s*[(\[]|\s+[-–—]\s+|;", raw, maxsplit=1)[0]
    scale = next((k for rx, k in UNIT if rx.search(head)), 1)
    num = re.sub(r"[,\s₹]|INR|Rs\.?|%|days?|years?|crores?|cr\b\.?|lakhs?|lacs?", "", head, flags=re.I)
    try:
        f = float(num)
    except ValueError:
        return raw, raw
    if scale > 1:
        return round(f * scale), raw
    return int(f) if f.is_integer() and "." not in num else f, raw


def ask(g: Graph, question: str, emit=None, history: list[dict] | None = None, max_steps: int = 10) -> dict:
    res: RunResult = run_agent(g, "qa", QA_SYSTEM, question, QA_TOOLS, max_steps=max_steps,
                               emit=emit, history=history)
    value, raw = parse_answer(res.final)
    return {"answer": value, "answer_text": raw, "explanation": res.final, "steps": res.steps,
            "evidence": res.evidence, "subgraph": res.subgraph, "usage": res.usage, "ms": res.ms,
            "providers": res.providers, "error": res.error}
