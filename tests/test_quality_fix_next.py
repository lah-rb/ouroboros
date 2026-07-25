"""Goal-driven quality findings: harvester + quality sweep.

The quality gate's findings become classified goals (functional -> functional
sweep + interact re-test; quality -> quality sweep, complete-on-patch). The
harvester creates a goal per finding (dedup by signature: re-open a completed
goal whose finding the gate re-reports; skip one already in flight). The quality
sweep drives diagnose -> file_ops -> complete-on-patch per quality goal.
"""

from __future__ import annotations

import pytest

from agent.actions.mission_actions import (
    _fileops_dispatch_from_quality_diagnosis,
    _quality_finding_signature,
    action_harvest_quality_findings,
    action_quality_sweep_next,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    DirectiveReport,
    GoalRecord,
    MissionConfig,
    MissionState,
)
from tests.conftest import quality_gate_result as _qg


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
        meta=FlowMeta(flow_name="mission_control", step_id="x"),
        effects=MockEffects(),
    )


# ── Harvester ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_harvest_creates_one_goal_per_finding_classified():
    m = _mission()
    qr = _qg(
        {"issue": "use command has no effect", "class": "functional"},
        {"issue": "'are here' should be 'is here'", "class": "quality"},
    )
    out = await action_harvest_quality_findings(_si(m, **qr))
    assert out.result.get("harvested") is True
    types = {g.type for g in m.goals if g.origin == "quality_gate"}
    assert types == {"functional", "quality"}
    # signatures recorded for dedup; all incomplete
    assert all(g.finding_signature and g.status == "incomplete" for g in m.goals)


@pytest.mark.asyncio
async def test_harvest_reopens_completed_goal_on_recurring_finding():
    sig = _quality_finding_signature({"issue": "use has no effect"})
    done = GoalRecord(
        description="use has no effect",
        type="functional",
        status="complete",
        origin="quality_gate",
        finding_signature=sig,
    )
    m = _mission([done])
    out = await action_harvest_quality_findings(
        _si(m, **_qg({"issue": "use has no effect", "class": "functional"}))
    )
    assert out.result.get("harvested") is True
    assert done.status == "incomplete"  # reopened — the fix didn't hold
    assert len(m.goals) == 1  # not duplicated


@pytest.mark.asyncio
async def test_harvest_skips_in_flight_goal():
    sig = _quality_finding_signature({"issue": "x"})
    inflight = GoalRecord(
        description="x",
        type="quality",
        status="incomplete",
        origin="quality_gate",
        finding_signature=sig,
    )
    m = _mission([inflight])
    await action_harvest_quality_findings(
        _si(m, **_qg({"issue": "x", "class": "quality"}))
    )
    assert len(m.goals) == 1  # not recreated


@pytest.mark.asyncio
async def test_harvest_no_findings_finalizes():
    m = _mission()
    out = await action_harvest_quality_findings(
        _si(m, quality_results={"fix_tasks": []})
    )
    assert out.result.get("done") is True


# ── Quality sweep ────────────────────────────────────────────────────────


def _qgoal(reports=None) -> GoalRecord:
    return GoalRecord(
        description="dialogue repeats forever",
        type="quality",
        status="incomplete",
        origin="quality_gate",
        finding_signature="sig",
        reports=reports or [],
    )


@pytest.mark.asyncio
async def test_sweep_fresh_goal_dispatches_diagnose():
    g = _qgoal()
    out = await action_quality_sweep_next(_si(_mission([g])))
    assert out.result.get("needs_fix") is True
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "diagnose_issue"
    assert dc["goal_id"] == g.id  # bound to the goal


@pytest.mark.asyncio
async def test_sweep_after_diagnose_dispatches_file_ops():
    g = _qgoal(
        reports=[
            DirectiveReport(
                flow="diagnose_issue",
                status="success",
                summary="dialogue loop in engine",
                target_file="engine.py",
                target_symbol="GameEngine._talk",
                change_spec="advance npc_states",
            )
        ]
    )
    out = await action_quality_sweep_next(_si(_mission([g])))
    assert out.result.get("needs_fix") is True
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "file_ops"
    assert dc["target_file_path"] == "engine.py"
    assert dc["goal_id"] == g.id


@pytest.mark.asyncio
async def test_sweep_after_file_ops_completes_goal():
    g = _qgoal(
        reports=[DirectiveReport(flow="file_ops", status="success", summary="patched")]
    )
    out = await action_quality_sweep_next(_si(_mission([g])))
    assert g.status == "complete"  # complete-on-patch
    assert not out.result.get("needs_fix")


@pytest.mark.asyncio
async def test_sweep_diagnose_no_target_best_effort_completes():
    g = _qgoal(
        reports=[
            DirectiveReport(
                flow="diagnose_issue",
                status="success",
                summary="",
                target_file="<file>",
            )
        ]
    )
    out = await action_quality_sweep_next(_si(_mission([g])))
    assert g.status == "complete"  # avoid spin; gate re-reports -> harvest reopens
    assert not out.result.get("needs_fix")


@pytest.mark.asyncio
async def test_sweep_no_incomplete_quality_goals_regates():
    g = GoalRecord(description="done", type="quality", status="complete")
    out = await action_quality_sweep_next(_si(_mission([g])))
    assert out.result.get("sweep_complete") is True


def test_fileops_mapping_handles_object_report_and_rejects_junk():
    obj = DirectiveReport(
        flow="diagnose_issue",
        status="success",
        summary="s",
        target_file="engine.py",
        target_symbol="X.y",
    )
    dc = _fileops_dispatch_from_quality_diagnosis(
        obj, goal_id="g1", goal_description="d"
    )
    assert dc["flow"] == "file_ops" and dc["goal_id"] == "g1"
    assert (
        _fileops_dispatch_from_quality_diagnosis(
            DirectiveReport(
                flow="diagnose_issue",
                status="success",
                summary="s",
                target_file="path/to/file.py",
            )
        )
        is None
    )
