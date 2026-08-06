"""The behavioural evaluator must never see the acceptance-check results.

Found live on the hy3 run-to-completion (2026-08-06). The `examine` goal was
regression-reopened carrying a check derived from an earlier pass:

    examine sword  ->  'Rusty Sword: a weapon, +15 attack.' in output

A later fix edited world.json and moved the sword, so the check now tests a
STALE WORLD-LAYOUT ASSUMPTION, not the examine capability. The designed
wear-out path is reconcile_acceptance: behaviour passes, check fails, the
per-check conflict counter increments, and at K the check disarms.

It could never fire. evaluate_outcome's prompt included the check results as
an "Acceptance checks" evidence block, and the evaluator dutifully folded the
failed check into its verdict — answering goal_met=false while its own
headline said "Examine works but sword description format fails acceptance
check". reconcile's route requires goal_met==true AND acceptance_ok==false,
so the DOUBLE-VETO made it unreachable: acceptance_conflicts stayed {} across
every round and the stale check held the goal hostage.

The ops rule is "checks pass AND judge confirms" — two INDEPENDENT signals,
ANDed in the resolver. Feeding one signal into the other makes them dependent
and silently disables the only self-healing path a grounded check has.

These tests pin the compiled flow graph, not the CUE text, so a refactor that
reintroduces the coupling fails here regardless of how it is spelled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def interact():
    compiled = json.loads((ROOT / "flows" / "compiled.json").read_text())
    return compiled["interact"]


class TestTheEvaluatorIsIndependent:
    def test_no_prompt_section_references_the_check_results(self, interact):
        """The whole fix: the evaluator judges SESSION BEHAVIOUR alone."""
        step = interact["steps"]["evaluate_outcome"]
        refs = json.dumps(step.get("turn", {}).get("sections", []))
        assert "acceptance_summary" not in refs, (
            "feeding the deterministic verdict to the behavioural evaluator "
            "recreates the double-veto and starves the staleness counter"
        )

    def test_the_summary_is_not_even_in_its_context(self, interact):
        ctx = interact["steps"]["evaluate_outcome"].get("context", {})
        declared = (ctx.get("required") or []) + (ctx.get("optional") or [])
        assert "acceptance_summary" not in declared

    def test_the_summary_is_no_longer_published(self, interact):
        """Its only consumer was the prompt; unconsumed it would be a dead
        publish, and republishing it invites the next coupling."""
        pubs = interact["steps"]["acceptance_verdict"].get("publishes") or []
        assert "acceptance_summary" not in pubs
        assert "acceptance_ok" in pubs, "the deterministic veto signal must survive"


class TestTheDeterministicVetoStillGates:
    """Removing the prompt coupling must NOT weaken the gate itself."""

    def test_success_requires_both_signals(self, interact):
        rules = interact["steps"]["parse_evaluation"]["resolver"]["rules"]
        success = [r for r in rules if r["transition"] == "end_eval_session_success"]
        assert success, "a success route must exist"
        cond = success[0]["condition"]
        assert (
            "goal_met" in cond and "acceptance_ok" in cond
        ), "ops rule: checks pass AND judge confirms"

    def test_disagreement_routes_to_reconcile(self, interact):
        """Behaviour passed, check failed -> the counter's ONLY entry point.
        This is the route the double-veto made unreachable."""
        rules = interact["steps"]["parse_evaluation"]["resolver"]["rules"]
        reconcile = [r for r in rules if r["transition"] == "reconcile_acceptance"]
        assert reconcile, "the wear-out path must exist"
        cond = reconcile[0]["condition"]
        assert "goal_met" in cond and "acceptance_ok" in cond

    def test_behavioural_failure_stays_terminal(self, interact):
        """A real regression (goal_met false) must never reach the disarm
        counter — that asymmetry is what makes disarming safe."""
        rules = interact["steps"]["parse_evaluation"]["resolver"]["rules"]
        assert rules[-1]["transition"] == "end_eval_session_failure"
        assert rules[-1]["condition"].strip() in ("true", "True")
