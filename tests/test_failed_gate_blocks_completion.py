"""A failed quality gate means there is more to do — never mission success.

FOUND LIVE (gpt-oss-medium, tier_20260809-143338). The gate failed at
`parse_dep_result` — step 9 of 10, an undeclared dependency — which routes
straight to `gate_fail`, skipping the rung that BUILDS `quality_results`.
The mission-side harvester therefore saw no findings, returned `done: True`,
and mission_control routed that to `completed`:

    Flow 'quality_gate' reached terminal step 'gate_fail' with status 'failed'
    ...
    Agent terminated after 0 cycles: status='completed'

A gate failure and mission success in the same cycle. The one defect that
would stop a stranger running the artifact was detected, written into a step
observation, and discarded.

`harvest_quality_findings` is reachable ONLY when dispatch_quality_gate
returned non-success, so "no findings" never means "nothing wrong" — it means
the gate died early. It now files the failure itself as a goal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.mission_actions import action_harvest_quality_findings
from agent.actions.pipeline_actions import action_parse_dep_check_result
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import MissionConfig, MissionState

ROOT = Path(__file__).resolve().parents[1]


def _mission() -> MissionState:
    return MissionState(
        objective="build a game",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
    )


def _si(context) -> StepInput:
    return StepInput(
        context=dict(context),
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="harvest_quality_findings"),
        effects=MockEffects(),
    )


@pytest.mark.asyncio
async def test_a_failed_gate_with_no_findings_does_not_finalize():
    m = _mission()
    out = await action_harvest_quality_findings(
        _si({"mission": m, "gate_failure_reason": "undeclared dependencies — pyyaml"})
    )
    assert out.result["done"] is False, (
        "done=True routes straight to `completed` — a failed gate would finish "
        "the mission, which is the bug this file exists for"
    )
    filed = [g for g in m.goals if g.description.startswith("Quality gate failed")]
    assert len(filed) == 1
    assert "pyyaml" in filed[0].description, "the goal must name the actual defect"
    assert filed[0].status == "incomplete"


@pytest.mark.asyncio
async def test_the_same_failure_reopens_one_goal_rather_than_multiplying():
    m = _mission()
    ctx = {"mission": m, "gate_failure_reason": "undeclared dependencies — pyyaml"}
    await action_harvest_quality_findings(_si(ctx))
    m.goals[0].status = "complete"
    await action_harvest_quality_findings(_si(ctx))
    filed = [g for g in m.goals if g.description.startswith("Quality gate failed")]
    assert len(filed) == 1, "a repeatedly-failing gate must not breed goals"
    assert filed[0].status == "incomplete", "it must reopen"


@pytest.mark.asyncio
async def test_it_still_files_something_when_the_reason_is_unknown():
    """Any early-exit branch, not just the dependency one, must block."""
    m = _mission()
    out = await action_harvest_quality_findings(_si({"mission": m}))
    assert out.result["done"] is False
    assert [g for g in m.goals if g.description.startswith("Quality gate failed")]


@pytest.mark.asyncio
async def test_the_dep_check_names_the_missing_packages():
    """The narrow half: parse_dep_result must publish a reason specific enough
    to act on, since its branch skips the findings rung entirely."""
    out = await action_parse_dep_check_result(
        StepInput(
            context={
                "inference_response": json.dumps(
                    {
                        "missing_dependencies": ["pyyaml"],
                        "details": [
                            {"file": "world.py", "import": "yaml", "package": "pyyaml"}
                        ],
                        "install_command": "uv add pyyaml",
                    }
                )
            },
            params={},
            meta=FlowMeta(flow_name="quality_gate", step_id="parse_dep_result"),
            effects=MockEffects(),
        )
    )
    assert out.result["deps_ok"] is False
    reason = out.context_updates["gate_failure_reason"]
    assert "pyyaml" in reason
    assert "uv add pyyaml" in reason


class TestTheReasonSurvivesTheSubflowBoundary:
    """Every mission_control variant must carry it, or the harvester files a
    generic goal. The linter caught three variants I had missed."""

    @staticmethod
    def _compiled():
        return json.loads((ROOT / "flows" / "compiled.json").read_text())

    @pytest.mark.parametrize(
        "flow",
        [
            "mission_control",
            "mission_control_swarm",
            "mission_control_contracted",
            "mission_control_integrated",
        ],
    )
    def test_each_variant_publishes_and_declares_it(self, flow):
        steps = self._compiled()[flow]["steps"]
        assert "gate_failure_reason" in (
            steps["dispatch_quality_gate"].get("publishes") or []
        )
        assert "gate_failure_reason" in (
            steps["harvest_quality_findings"]["context"].get("optional") or []
        )

    def test_the_gate_returns_it(self):
        assert "gate_failure_reason" in self._compiled()["quality_gate"]["returns"]


# ══════════════════════════════════════════════════════════════════════
# Fix in place, or stop reopening — the two ways out of a failed gate
# ══════════════════════════════════════════════════════════════════════
#
# Operator, 2026-08-10: "there is no meaningful distinction between a
# 'quality' goal and a functional goal. The standard is to take care of the
# problem in place, or wholesale the finding to the functional phase."
#
# _fileops_dispatch_from_quality_diagnosis returned None for project_ops, so a
# quality finding whose fix is NOT code (a manifest, tooling, the environment)
# had nowhere to go. Invisible while a failed gate ended the mission; once the
# failure persisted as a goal it became a spin:
#   gate fails → files goal → diagnose says project_ops → dropped → gate fails…


def test_a_project_ops_verdict_dispatches_in_place_instead_of_being_dropped():
    from agent.actions.mission_actions import _fileops_dispatch_from_quality_diagnosis

    cfg = _fileops_dispatch_from_quality_diagnosis(
        {
            "recommended_flow": "project_ops",
            "summary": "pytest is imported by tests/ but absent from pyproject",
            "target_file": "",
        },
        goal_id="g1",
        goal_description="Quality gate failed: undeclared dependencies — pytest",
    )
    assert cfg is not None, "project_ops used to return None — the spin"
    assert cfg["flow"] == "project_ops"
    # No file target: project_ops owns the manifest, and a target only misleads
    # the module-frame editor (the "assert shutil.which('python')" incident).
    assert cfg["target_file_path"] == ""
    assert "pytest" in cfg["flow_directive"]


def test_a_code_verdict_still_dispatches_file_ops():
    from agent.actions.mission_actions import _fileops_dispatch_from_quality_diagnosis

    cfg = _fileops_dispatch_from_quality_diagnosis(
        {"recommended_flow": "file_ops", "target_file": "game.py", "summary": "x"},
        goal_id="g1",
    )
    assert cfg["flow"] == "file_ops"
    assert cfg["target_file_path"] == "game.py"


@pytest.mark.asyncio
async def test_an_unfixable_finding_stops_reopening_and_raises_the_dispute():
    """Even with project_ops wired, a finding nothing can dispatch must not
    reopen forever. Past the ceiling the mission finishes and the finding
    survives as a warning — the authored-test quarantine's contract."""
    from agent.persistence.models import FailedAttempt

    from agent.actions.mission_actions import _GATE_GOAL_REOPEN_CEILING

    m = _mission()
    ctx = {"mission": m, "gate_failure_reason": "something nothing can fix"}
    await action_harvest_quality_findings(_si(ctx))
    goal = m.goals[0]
    goal.status = "complete"
    goal.failed_attempts = [
        FailedAttempt(
            target_file="",
            flow="diagnose_issue",
            reason="no dispatchable fix",
            diagnosis_summary="s",
        )
        for _ in range(_GATE_GOAL_REOPEN_CEILING)
    ]

    out = await action_harvest_quality_findings(_si(ctx))

    assert out.result["done"] is True, "the mission must be allowed to finish"
    assert goal.status == "complete", "it must not be reopened again"
    warn = [w for w in m.pending_warnings if w.kind == "quality_gate_unfixable"]
    assert warn, "the finding must survive as a warning, not vanish"


@pytest.mark.asyncio
async def test_the_signature_ignores_the_drifting_remedy_text():
    """LIVE REGRESSION (2026-08-10): the reason ends with the LLM's suggested
    remedy, and that phrasing drifted between rounds — "fix: pip install
    pytest" became "fix: uv add pytest". The signature moved with it, so a
    SECOND goal was filed instead of the first reopening. Goals bred, by the
    same prose-keying mechanism OPEN_TASKS §24 records for coverage
    findings. Identity is the defect; the remedy is advice."""
    m = _mission()
    await action_harvest_quality_findings(
        _si(
            {
                "mission": m,
                "gate_failure_reason": (
                    "undeclared dependencies — pytest is imported but not in "
                    "the manifest; fix: pip install pytest"
                ),
            }
        )
    )
    m.goals[0].status = "complete"
    await action_harvest_quality_findings(
        _si(
            {
                "mission": m,
                "gate_failure_reason": (
                    "undeclared dependencies — pytest is imported but not in "
                    "the manifest; fix: uv add pytest"
                ),
            }
        )
    )
    filed = [g for g in m.goals if g.description.startswith("Quality gate failed")]
    assert len(filed) == 1, f"same defect, drifting remedy — bred {len(filed)} goals"
    assert filed[0].status == "incomplete"
