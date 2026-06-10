"""dispatch_history wiring: attach_directive_report appends a DispatchRecord,
the director_overview projection windows the last 5 with status, and the
renderer emits the terse `flow: target — status (goal id)` line."""

from __future__ import annotations

import pytest

from agent.actions.reporting_actions import action_attach_directive_report
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    DispatchRecord,
    GoalRecord,
    MissionConfig,
    MissionState,
)
from agent.projections import project_director_overview
from agent.renderers import render_director_overview
from agent.trace import step_context


def _mission(goals=None) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        goals=goals if goals is not None else [],
        config=MissionConfig(working_directory="/tmp/x"),
    )


def _si(mission, **ctx) -> StepInput:
    return StepInput(
        context={"mission": mission, **ctx},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="apply_last_result"),
        effects=MockEffects(),
    )


def _report(**overrides) -> dict:
    base = {
        "flow": "file_ops",
        "status": "failed",
        "summary": "patch applied but lint failed",
        "target_file": "parser.py",
    }
    base.update(overrides)
    return base


# ── append on report attach ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_attach_appends_dispatch_record():
    goal = GoalRecord(description="inventory works", type="functional")
    m = _mission([goal])
    si = _si(m, last_result={"directive_report": _report()}, last_goal_id=goal.id)
    with step_context(
        mission_id=m.id, cycle=7, flow="mission_control", step="apply_last_result"
    ):
        await action_attach_directive_report(si)
    assert len(m.dispatch_history) == 1
    rec = m.dispatch_history[-1]
    assert rec.cycle == 7
    assert rec.flow == "file_ops"
    assert rec.goal_id == goal.id
    assert rec.target_file_path == "parser.py"
    assert rec.result_status == "failed"


@pytest.mark.asyncio
async def test_no_report_appends_nothing():
    goal = GoalRecord(description="inventory works", type="functional")
    m = _mission([goal])
    si = _si(m, last_result={}, last_goal_id=goal.id)
    await action_attach_directive_report(si)
    assert m.dispatch_history == []


@pytest.mark.asyncio
async def test_unknown_goal_appends_nothing():
    goal = GoalRecord(description="inventory works", type="functional")
    m = _mission([goal])
    si = _si(m, last_result={"directive_report": _report()}, last_goal_id="nope")
    await action_attach_directive_report(si)
    assert m.dispatch_history == []


@pytest.mark.asyncio
async def test_target_falls_back_to_files_affected():
    goal = GoalRecord(description="inventory works", type="functional")
    m = _mission([goal])
    report = _report(target_file="", files_affected=["engine.py", "parser.py"])
    si = _si(m, last_result={"directive_report": report}, last_goal_id=goal.id)
    await action_attach_directive_report(si)
    assert m.dispatch_history[-1].target_file_path == "engine.py"


@pytest.mark.asyncio
async def test_cycle_defaults_to_zero_outside_step_context():
    goal = GoalRecord(description="inventory works", type="functional")
    m = _mission([goal])
    si = _si(m, last_result={"directive_report": _report()}, last_goal_id=goal.id)
    await action_attach_directive_report(si)
    assert m.dispatch_history[-1].cycle == 0


# ── projection + renderer ────────────────────────────────────────────


def test_projection_windows_last_five_with_status():
    m = _mission()
    for i in range(7):
        m.dispatch_history.append(
            DispatchRecord(
                cycle=i,
                flow="file_ops",
                goal_id=f"g{i}",
                target_file_path=f"f{i}.py",
                result_status="failed" if i % 2 else "success",
            )
        )
    overview = project_director_overview(m, {})
    entries = overview["dispatch_history"]
    assert len(entries) == 5
    assert entries[0]["goal_id"] == "g2"  # window starts at the 3rd of 7
    assert all("status" in e for e in entries)
    assert entries[-1]["status"] == "success"


def test_renderer_line_includes_status():
    m = _mission()
    m.dispatch_history.append(
        DispatchRecord(
            cycle=3,
            flow="file_ops",
            goal_id="abc123",
            target_file_path="parser.py",
            result_status="failed",
        )
    )
    overview = project_director_overview(m, {})
    brief = render_director_overview({"source": overview}, {})
    assert "file_ops: parser.py — failed (goal abc123)" in brief
