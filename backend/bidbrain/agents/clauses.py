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
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "seven": 7, "ten": 10}


def _money(s: str) -> int | None:
    m = re.search(r"(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d+)?)\s*(Cr|crore|Lakh|lakh)", s, re.I)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return int(round(v * (10_000_000 if m.group(2).lower().startswith("cr") else 100_000)))


def _cats(s: str) -> list[str]:
    low = s.lower().replace("&", "and")
    out = [c for c in CATS if c in low]
    for w, cs in CAT_WORDS.items():          # "roads, highways or expressways", "sewerage and drainage"
        if w in low:
            out += [c for c in cs if c not in out and not (c == "large bridges" and "large" not in low
                                                            and "major" not in low)]
    return out


def _num(pattern: str, s: str, default=None):
    m = re.search(pattern, s, re.I)
    if not m:
        return default
    g = m.group(1).lower()
    return int(g) if g.isdigit() else WORDNUM.get(g, default)


def parse_rules(text: str) -> list[dict]:
    """Split on numbered/lettered items and classify each line."""
    parts = re.split(r"\n\s*(?=(?:\(?[ivx]+\)|\d+(?:\.\d+)*[.)]?|[A-H][.)])\s)", "\n" + text)
    clauses = []
    for i, raw in enumerate(p.strip() for p in parts):
        if not raw or len(raw) < 12 or raw.lower().startswith(("section", "eligibility", "qualifying", "pre-qual")):
            continue
        low = raw.lower()
        ref = re.match(r"\(?([ivx]+|\d+(?:\.\d+)*|[A-H])[.)]?\s", raw)
        cid = ref.group(1) if ref else f"C{i}"
        money = _money(raw)
        if "turnover" in low:
            yrs = _num(r"last\s+(\w+)\s+(?:financial\s+)?years", raw, 3) or 3
            if "each" in low:
                clauses.append({"id": cid, "text": raw, "kind": "turnover", "params": {"min_each_year": money, "years": yrs}})
            else:
                clauses.append({"id": cid, "text": raw, "kind": "turnover", "params": {"min_average": money, "years": yrs}})
        elif "similar" in low or "experience" in low and ("work" in low or "project" in low) and "pmp" not in low:
            p = {"categories": _cats(raw), "lookback_years": _num(r"last\s+(\w+)\s+years", raw, 7) or 7}
            if "80%" in raw or "80 %" in raw or "40%" in raw:
                est = re.search(r"estimated cost(?: of)?\s*((?:INR|Rs\.?|₹)\s*[\d,.]+\s*(?:Cr|crore|lakh))", raw, re.I)
                p["estimated_cost"] = _money(est.group(1)) if est else money
            elif len(amts := [_money(a) for a in re.findall(r"(?:INR|Rs\.?|₹)\s*[\d,.]+\s*(?:Cr|crore|lakh)", raw, re.I)]) == 3 \
                    and amts[0] and amts[1] and amts[2] and amts[0] > amts[1] > amts[2]:
                p["estimated_cost"] = int(round(amts[0] / 0.8))      # CPWD 80/60/40 written as amounts
            elif money:
                p.update(min_value=money, count=_num(r"\b(one|two|three|\d)\b[^.]{0,30}work", raw, 1) or 1)
            levels = [lv for lv, words in {"central": ["central"], "state": ["state"], "psu": ["psu"]}.items()
                      if any(w in low for w in words)]
            if levels:
                p["govt_levels"] = levels
            if "reference letter" in low:
                p["require_reference"] = False   # documents to enclose, checked by the AbsenceAuditor
            clauses.append({"id": cid, "text": raw, "kind": "similar_works", "params": p})
        elif "pmp" in low or "project manager" in low and "certif" in low:
            p = {"credential": "PMP" if "pmp" in low else "Six Sigma",
                 "min_experience_years": _num(r"(\d+)\s*years", raw, None),
                 "count": _num(r"\b(one|two|three|\d)\s+project managers", raw, 1) or 1}
            if cats := _cats(raw.split("experience")[-1] if "experience" in low else raw):
                p["categories"] = cats
            clauses.append({"id": cid, "text": raw, "kind": "key_personnel", "params": p})
        elif re.search(r"iso\s*\d+|cpwd|nabl", low):
            for std in re.findall(r"(ISO\s*\d+|CPWD|NABL)", raw, re.I):
                clauses.append({"id": f"{cid}-{std.replace(' ', '')}", "text": raw, "kind": "certification",
                                "params": {"standard": std.upper().replace("ISO", "ISO ").replace("  ", " ").strip()}})
        elif "years" in low and ("business" in low or "existence" in low or "incorporat" in low):
            clauses.append({"id": cid, "text": raw, "kind": "years_in_business",
                            "params": {"min_years": _num(r"(\d+)\s*years", raw, 0)}})
        else:
            clauses.append({"id": cid, "text": raw, "kind": "other", "params": {}})
    return clauses


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
        out.append({"id": str(c.get("id", len(out) + 1)), "text": c.get("text", ""), "kind": c["kind"],
                    "params": c.get("params") or {}})
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
