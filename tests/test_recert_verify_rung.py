"""Verify-only re-certification rung in structural_sweep_next.

A regression-reopened structural goal whose reports were archived at its
earlier completion arrives at the sweep evidence-less. Before the rung,
it could never take the cheap auto-complete branch (which requires
in-memory reports) and always fell through to a generic fixing dispatch
— the bossgame2_adaptive long run shows 541 fixing dispatches, 0 cheap
auto-completes, 486 whole-file rewrites. The rung re-runs the
deterministic checks and re-certifies on pass with zero LLM work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.actions.mission_actions import action_structural_sweep_next
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
)


def _mission(tmp_path: Path, goals) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path)),
        architecture=ArchitectureState(
            run_command="python main.py",
            creation_order=[g.associated_files[0] for g in goals],
            modules=[
                ModuleSpec(file=g.associated_files[0], responsibility="r")
                for g in goals
            ],
        ),
        goals=goals,
    )


def _si(mission, files: dict[str, str] | None = None) -> StepInput:
    # Real files on tmp_path drive the sweep's existence check; the mock's
    # in-memory store serves action_run_batch_file_checks' read_file calls.
    effects = MockEffects(mission=mission, files=files or {})
    return StepInput(
        context={"mission": mission},
        effects=effects,
        meta=FlowMeta(flow_name="mission_control", step_id="structural_sweep_next"),
    )


def _reopened_goal(fname: str) -> GoalRecord:
    """The post-archive regression shape: no reports, provenance present."""
    return GoalRecord(
        description=f"goal for {fname}",
        type="structural",
        associated_files=[fname],
        status="incomplete",
        regression_reopened=True,
        reports_archived=3,
        last_completed_at="2026-07-23T17:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_reopened_goal_with_passing_checks_recertifies(tmp_path):
    (tmp_path / "models.py").write_text("X = 1\n")
    goal = _reopened_goal("models.py")
    mission = _mission(tmp_path, [goal])
    out = await action_structural_sweep_next(_si(mission))
    assert goal.status == "complete"
    assert goal.regression_reopened is False
    # No fixing dispatch was produced for it.
    assert (out.context_updates or {}).get("dispatch_config") is None
    assert out.result.get("sweep_complete") is True


@pytest.mark.asyncio
async def test_reopened_goal_with_failing_checks_dispatches_with_gate_output(
    tmp_path,
):
    # Data file with invalid YAML: the deterministic parse check fails,
    # so the rung must NOT auto-complete — and the fixing dispatch must
    # carry the fresh gate output and the re-cert framing.
    (tmp_path / "world.yaml").write_text("rooms: [unclosed\n")
    goal = _reopened_goal("world.yaml")
    mission = _mission(tmp_path, [goal])
    out = await action_structural_sweep_next(
        _si(mission, files={"world.yaml": "rooms: [unclosed\n"})
    )
    assert goal.status == "incomplete"
    dc = (out.context_updates or {}).get("dispatch_config")
    assert dc is not None and dc["target_file_path"] == "world.yaml"
    assert "re-certification" in dc["flow_directive"]
    assert dc.get("error_output")  # fresh parse detail, not empty


@pytest.mark.asyncio
async def test_goal_without_regression_provenance_skips_rung(tmp_path):
    # Evidence-less incomplete goal that was NEVER completed (no archived
    # reports, no flag): the rung must not fire — it would certify work
    # that never passed a gate. Falls to the normal fixing path.
    (tmp_path / "models.py").write_text("X = 1\n")
    goal = GoalRecord(
        description="never verified",
        type="structural",
        associated_files=["models.py"],
        status="incomplete",
    )
    mission = _mission(tmp_path, [goal])
    out = await action_structural_sweep_next(_si(mission))
    assert goal.status == "incomplete"
    dc = (out.context_updates or {}).get("dispatch_config")
    assert dc is not None  # dispatched to repair, not silently completed
