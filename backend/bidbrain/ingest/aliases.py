"""How people on a bid desk actually write entity names, and how we resolve them.

Questions say "phed odisha", "gujarat pw", "I&W Rajasthan" — never the legal
name. Aliases are *generated from the entity list itself* (initials, state
forms), never keyed on a question, the same rule the old engine followed
(`vendor_bidintel/src/answer.py::_state_forms`).

The resolver returns ranked candidates with scores rather than one answer: an
ambiguous mention ("Jal Nigam" — three states) must surface as ambiguous so the
agent asks or narrows, instead of silently picking whichever sorts first.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

_STOP = {"of", "and", "the", "&", "govt", "government", "co", "dept"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _words(s: str) -> list[str]:
    return re.findall(r"[A-Za-z]+", s)


def _initials(words: list[str]) -> str:
    return "".join(w[0] for w in words if w.lower() not in _STOP).lower()


def state_of_client(name: str) -> str | None:
    """'Public Works Department, Govt of Gujarat' -> 'Gujarat';
    'Gujarat Municipal Corporation' -> 'Gujarat'."""
    head, _, tail = name.partition(",")
    if tail:
        t = re.sub(r"(?i)\bgovt\.? of\b|\bgovernment of\b", "", tail).strip()
        return t or None
    m = re.match(r"(.+?) Municipal Corporation$", name)
    return m.group(1) if m else None


def govt_level(name: str) -> str:
    """Issuer class used by tender clauses ("works for a central govt client").

    A **naming rule**, not an extracted fact: the certificates' header line is
    the same template for every issuer, so the documents do not say. The rule is
    stored on the node as `govt_level_source = 'name-rule'` and documented in
    docs/EXPLAINER.md so nobody mistakes it for evidence.
    """
    n = name.lower()
    if "municipal corporation" in n:
        return "municipal"
    if "govt of" in n or n.startswith(("public health engineering dept", "jal nigam")):
        return "state"
    if n.startswith(("national ", "central ")) or "authority" in n:
        return "central"
    if "corporation" in n:
        return "psu"
    return "private"


def client_aliases(name: str) -> list[str]:
    head, _, tail = name.partition(",")
    state = state_of_client(name) or ""
    st_words = _words(state)
    st_forms = {state.lower()} if state else set()
    if len(st_words) > 1:
        st_forms.add("".join(w[0] for w in st_words).lower())      # UP, WB, TN
    elif st_words:
        st_forms.add(st_words[0][:3].lower())                       # guj, mah, raj
    head_words = _words(head if tail else name.replace(state, ""))
    acr = _initials(head_words)
    out = {name.lower(), head.lower().strip()}
    if acr and len(acr) >= 2:
        out.add(acr)
        for s in st_forms:
            out |= {f"{acr} {s}", f"{s} {acr}"}
    # common bid-desk shorthands
    shorthand = {"public works department": ["pwd", "pw"],
                 "public health engineering dept": ["phed", "phe"],
                 "irrigation & waterways dept": ["iwd", "i&w", "irrigation"],
                 "jal nigam": ["jn", "jal nigam"],
                 "municipal corporation": ["mc", "municipal corp"]}
    for long, shorts in shorthand.items():
        if long in name.lower():
            for sh in shorts:
                for s in st_forms:
                    out |= {f"{sh} {s}", f"{s} {sh}"}
    return sorted(a for a in out if a)


def resolve(text: str, candidates: dict[str, list[str]], limit: int = 5) -> list[dict]:
    """Rank `candidates` (key -> [canonical name, *aliases]) against a mention.

    Exact alias hit scores 100; otherwise the best rapidfuzz token-set /
    partial ratio over all forms. Returned with an `ambiguous` flag when the top
    two are within 3 points, which the agent is told to treat as "ask/narrow".
    """
    q = (text or "").lower().strip()
    nq = norm(q)
    scored = []
    for key, forms in candidates.items():
        best = 0.0
        for f in forms:
            fl = f.lower()
            if nq and nq == norm(fl):
                best = 100.0
                break
            best = max(best, fuzz.token_set_ratio(q, fl) * 0.97,
                       fuzz.partial_ratio(q, fl) * 0.9 if len(q) >= 4 else 0)
        scored.append({"key": key, "name": forms[0], "score": round(best, 1)})
    scored.sort(key=lambda r: -r["score"])
    top = scored[:limit]
    if len(top) > 1 and top[0]["score"] - top[1]["score"] < 3 and top[0]["score"] < 100:
        for r in top:
            r["ambiguous"] = True
    return top
