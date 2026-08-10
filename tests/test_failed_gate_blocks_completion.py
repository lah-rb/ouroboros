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
