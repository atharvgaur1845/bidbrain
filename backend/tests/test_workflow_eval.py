"""Fast subset of the deterministic workflow evaluation (eval/workflow.py): temporal + task completion.

Throwaway graphs only (session copies, a private company graph); no LLM. The full run is
`python -m eval.workflow`.
"""
from __future__ import annotations

import pytest

from bidbrain.graph.client import Graph, db

# cases that fail because of a product behaviour diagnosed in the workflow report (not a harness problem)
KNOWN_PRODUCT_FAILURES = {"tc.renewal_actions_match_lapsing"}


@pytest.fixture(scope="module")
def result():
    assert Graph().exists(), "load the base graph first: python -m bidbrain.ingest.load_graph"
    from eval import workflow
    return workflow.run(["temporal", "task_completion"], write=False)


def test_rows_are_well_formed(result):
    rows = result["rows"]
    assert len(rows) >= 15
    for r in rows:
        assert set(r) == {"id", "dimension", "description", "passed", "expected", "observed", "ms"}
        assert isinstance(r["passed"], bool) and r["ms"] >= 0
    assert len({r["id"] for r in rows}) == len(rows)
    assert not [r for r in rows if r["id"].endswith(".crashed")]


def test_every_case_passes_except_diagnosed_product_failures(result):
    failed = {r["id"] for r in result["rows"] if not r["passed"]}
    assert failed <= KNOWN_PRODUCT_FAILURES, failed


def test_cleans_up_and_never_touches_shared_graphs(result):
    assert not [n for n in db().list_graphs() if n.startswith("wfeval_")]
    guard = next(r for r in result["rows"] if r["id"] == "is.base_and_org_untouched")
    assert guard["passed"], guard
    assert result["summary"]["llm_calls"] == 0
