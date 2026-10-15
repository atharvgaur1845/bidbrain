"""Single-agent Q&A over the graph (the /ask page and the benchmark's 'BidBrain' system)."""
from __future__ import annotations

import re

from bidbrain.agents.loop import RunResult, run_agent
from bidbrain.agents.prompts import QA_SYSTEM, QA_TOOLS
from bidbrain.graph.client import Graph

ANSWER_RE = re.compile(r"ANSWER:\s*(.+)", re.I)


def parse_answer(text: str):
    m = None
    for m in ANSWER_RE.finditer(text or ""):
        pass
    raw = (m.group(1) if m else (text or "").strip().splitlines()[-1] if text else "").strip()
    num = re.sub(r"[,\s₹]|INR|Rs\.?|%|days?", "", raw, flags=re.I)
    try:
        f = float(num)
        return int(f) if f.is_integer() and "." not in num else f, raw
    except ValueError:
        return raw, raw


def ask(g: Graph, question: str, emit=None, history: list[dict] | None = None, max_steps: int = 10) -> dict:
    res: RunResult = run_agent(g, "qa", QA_SYSTEM, question, QA_TOOLS, max_steps=max_steps,
                               emit=emit, history=history)
    value, raw = parse_answer(res.final)
    return {"answer": value, "answer_text": raw, "explanation": res.final, "steps": res.steps,
            "evidence": res.evidence, "subgraph": res.subgraph, "usage": res.usage, "ms": res.ms,
            "providers": res.providers, "error": res.error}
