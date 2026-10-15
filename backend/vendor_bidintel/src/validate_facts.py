"""Check the reconstructed fact table against the sample questions' own workings.

Each sample carries a "Values read from the documents: ..." line listing the
individual contract values its answer was derived from. Those are gold values
read from the shipped documents, so they are an independent audit of the
extraction — separate from whether the final arithmetic is right.

Reports, per sample, whether the stated value multiset is a subset of what the
fact table holds for the client or person the question names.
"""
from __future__ import annotations

import json
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
FACTS = REPO / "results" / "raw" / "facts.json"
SAMPLES = REPO / "dataset" / "sample_questions.json"


def stated_values(question: dict) -> list[int]:
    for step in question.get("reasoning_steps", []):
        if "Values read from the documents" in step:
            return [int(v.replace(",", "")) for v in re.findall(r"\d[\d,]{4,}", step)]
    return []


def main() -> int:
    facts = json.loads(FACTS.read_text(encoding="utf-8"))
    samples = json.loads(SAMPLES.read_text(encoding="utf-8"))["questions"]
    all_values = sorted(w["value"] for w in facts["works"] if w["value"])

    checked = missing = 0
    for q in samples:
        want = stated_values(q)
        if not want:
            continue
        checked += 1
        absent = [v for v in want if v not in all_values]
        if absent:
            missing += 1
            print(f"  {q['qid']}  {len(absent)}/{len(want)} values not in fact table: "
                  f"{', '.join(f'{v:,}' for v in absent)}")

    print(f"\nsamples carrying stated values: {checked}")
    print(f"samples with every stated value present: {checked - missing}/{checked}")
    print(f"distinct work values in fact table: {len(set(all_values))} over {len(all_values)} works")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
