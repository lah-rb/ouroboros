"""Structural import-decision gate.

A structural goal must not auto-complete while a real import bug is unaddressed,
but it must NOT loop forever on an import that can't be fixed yet (an expected
first-pass cross-module import). The gate surfaces the import failure to the
model for exactly one fix-or-defer pass, then accepts.
"""

from agent.actions.reporting_actions import (
    _maybe_complete_goal,
    structural_block_reason,
)
from agent.persistence.models import DirectiveReport, GoalRecord


def _goal(
    checks_failed,
    *,
    gtype="structural",
    reviewed=False,
    flow="file_ops",
    status="success"
):
    g = GoalRecord(description="loads world", type=gtype)
    g.import_reviewed = reviewed
    g.reports.append(
        DirectiveReport(
            flow=flow, status=status, summary="", checks_failed=checks_failed
        )
    )
    return g


# ── structural_block_reason ──────────────────────────────────────────
def test_syntax_always_blocks():
    g = _goal(["syntax: main.py"])
    assert structural_block_reason(g, g.reports[-1].checks_failed) == "syntax"


def test_syntax_beats_import():
    g = _goal(["syntax: main.py", "import: main.py"])
    assert structural_block_reason(g, g.reports[-1].checks_failed) == "syntax"


def test_import_blocks_until_reviewed():
    g = _goal(["import: main.py"], reviewed=False)
    assert structural_block_reason(g, g.reports[-1].checks_failed) == "import"


def test_import_accepted_after_review():
    g = _goal(["import: main.py"], reviewed=True)
    assert structural_block_reason(g, g.reports[-1].checks_failed) is None


def test_clean_does_not_block():
    g = _goal([])
    assert structural_block_reason(g, g.reports[-1].checks_failed) is None


def test_lint_only_does_not_block():
    g = _goal(["lint: main.py"])
    assert structural_block_reason(g, g.reports[-1].checks_failed) is None


# ── _maybe_complete_goal end-to-end ──────────────────────────────────
def test_import_failure_blocks_completion():
    g = _goal(["import: main.py"], reviewed=False)
    _maybe_complete_goal(g)
    assert g.status == "incomplete", "unreviewed import bug must not auto-complete"


def test_import_failure_completes_after_review():
    g = _goal(["import: main.py"], reviewed=True)
    _maybe_complete_goal(g)
    assert g.status == "complete", "reviewed import is accepted (no infinite loop)"


def test_clean_report_completes():
    g = _goal([])
    _maybe_complete_goal(g)
    assert g.status == "complete"


def test_syntax_failure_blocks_completion_even_if_reviewed():
    g = _goal(["syntax: main.py"], reviewed=True)
    _maybe_complete_goal(g)
    assert g.status == "incomplete", "syntax always blocks regardless of review"


def test_functional_goal_not_auto_completed():
    g = _goal([], gtype="functional")
    _maybe_complete_goal(g)
    assert g.status == "incomplete", "functional goals complete via interact, not here"
