"""Ops flow set actions — task intake, definition-of-done, completion judge.

The ops mission is a single terminal task worked until done. These pin the
three deterministic contracts: one idempotent task goal + TaskState from the
objective; the definition-of-done parsed/stored from the criteria inference;
and the conservative "done" rule — checks pass AND judge confirms, else loop
with feedback (the anti-placeholder posture: don't declare done on weak
self-criteria).
"""

from __future__ import annotations

import json

import pytest

from agent.actions.operations_actions import (
    TASK_GOAL_SIGNATURE,
    action_derive_task_goal,
    action_judge_task_completion,
    action_store_completion_criteria,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import GoalRecord, MissionConfig, MissionState, TaskState


def _mission(objective="create a config.yaml that enables logging", task=False):
    m = MissionState(
        objective=objective,
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="ops"),
    )
    if task:
        m.task_definition = TaskState(task_spec=objective)
        m.goals.append(
            GoalRecord(
                description=f"Accomplish: {objective}",
                type="task_exec",
                finding_signature=TASK_GOAL_SIGNATURE,
            )
        )
    return m


def _si(mission, **ctx) -> StepInput:
    return StepInput(
        context={"mission": mission, **ctx},
        params={},
        meta=FlowMeta(flow_name="ops_control", step_id="x"),
        effects=MockEffects(),
    )


# ── task intake ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_derive_task_goal_creates_one_idempotently():
    m = _mission()
    out = await action_derive_task_goal(_si(m))
    assert out.result == {"goals_ready": True, "criteria_needed": True, "created": 1}
    assert m.task_definition is not None
    assert sum(1 for g in m.goals if g.type == "task_exec") == 1
    # Re-run: no duplicate, criteria still pending (none stored yet).
    out2 = await action_derive_task_goal(_si(m))
    assert out2.result == {"goals_ready": True, "criteria_needed": True, "created": 0}
    assert sum(1 for g in m.goals if g.type == "task_exec") == 1


@pytest.mark.asyncio
async def test_derive_task_goal_criteria_satisfied_once_stored():
    m = _mission(task=True)
    m.task_definition.completion_criteria = [{"command": "test -f config.yaml"}]
    out = await action_derive_task_goal(_si(m))
    assert out.result["criteria_needed"] is False


# ── definition of done ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_criteria_filters_junk():
    m = _mission(task=True)
    resp = json.dumps(
        {
            "checks": [
                {"command": "test -f config.yaml", "description": "config exists"},
                {"description": "no command — dropped"},
                {"command": "  ", "description": "blank command — dropped"},
            ]
        }
    )
    out = await action_store_completion_criteria(_si(m, inference_response=resp))
    assert out.result["criteria_count"] == 1
    c = m.task_definition.completion_criteria[0]
    assert c["command"] == "test -f config.yaml" and c["required"] is True


@pytest.mark.asyncio
async def test_store_criteria_tolerates_bare_array_first_object():
    # parse_llm_json collapses a top-level array to its first object; the store
    # must still capture that one check rather than zero.
    m = _mission(task=True)
    resp = json.dumps([{"command": "grep -q logging config.yaml"}])
    out = await action_store_completion_criteria(_si(m, inference_response=resp))
    assert out.result["criteria_count"] == 1


# ── completion judge ──────────────────────────────────────────────────


def _vr(*passed_required):
    return [{"passed": p, "required": True} for p in passed_required]


@pytest.mark.asyncio
async def test_judge_done_requires_checks_and_judge():
    m = _mission(task=True)
    out = await action_judge_task_completion(
        _si(
            m,
            validation_results=_vr(True, True),
            inference_response=json.dumps({"task_complete": True, "feedback": ""}),
        )
    )
    assert out.result["task_done"] is True
    assert next(g for g in m.goals if g.type == "task_exec").status == "complete"


@pytest.mark.asyncio
async def test_judge_not_done_when_a_check_fails():
    m = _mission(task=True)
    out = await action_judge_task_completion(
        _si(
            m,
            validation_results=_vr(True, False),  # a required check failed
            inference_response=json.dumps({"task_complete": True, "feedback": ""}),
        )
    )
    assert out.result["task_done"] is False  # checks gate it despite judge
    assert next(g for g in m.goals if g.type == "task_exec").status == "incomplete"
    assert m.task_definition.last_feedback  # feedback stored for next loop
    assert m.task_definition.attempts == 1


@pytest.mark.asyncio
async def test_judge_not_done_stores_judge_feedback():
    m = _mission(task=True)
    out = await action_judge_task_completion(
        _si(
            m,
            validation_results=_vr(True),
            inference_response=json.dumps(
                {"task_complete": False, "feedback": "logging key still missing"}
            ),
        )
    )
    assert out.result["task_done"] is False
    assert m.task_definition.last_feedback == "logging key still missing"


@pytest.mark.asyncio
async def test_judge_with_no_criteria_is_not_done():
    # Integrity: an ops task is never certified done without a derived
    # definition-of-done. Zero criteria (derivation hasn't succeeded — e.g. a
    # transient inference error) must NOT complete on the judge alone, even
    # when the judge says complete. Closes the judge-only escape hatch.
    m = _mission(task=True)  # task_definition present, completion_criteria empty
    out = await action_judge_task_completion(
        _si(
            m,
            validation_results=[],  # no deterministic checks ran
            inference_response=json.dumps({"task_complete": True, "feedback": ""}),
        )
    )
    assert out.result["task_done"] is False
    assert next(g for g in m.goals if g.type == "task_exec").status == "incomplete"
    assert "definition-of-done" in m.task_definition.last_feedback


@pytest.mark.asyncio
async def test_judge_not_done_when_criteria_defined_but_no_results():
    # Silent-bypass guard: criteria EXIST but produced no results (the checks
    # didn't run — a wiring failure). The judge must not declare done on its
    # own; the deterministic gate is the point. Regression for the M3 bug where
    # run_checks couldn't see mission and ran zero checks.
    m = _mission(task=True)
    m.task_definition.completion_criteria = [
        {"command": "test -f config.yaml", "required": True}
    ]
    out = await action_judge_task_completion(
        _si(
            m,
            validation_results=[],  # checks didn't run despite criteria existing
            inference_response=json.dumps({"task_complete": True, "feedback": ""}),
        )
    )
    assert out.result["task_done"] is False
    assert next(g for g in m.goals if g.type == "task_exec").status == "incomplete"
