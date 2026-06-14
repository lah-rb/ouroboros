"""Ops flow set — a single terminal task, worked until done.

The ops mission promotes `run_session` to a mission type: the objective IS
the task statement; the agent drives a terminal to accomplish it and judges
completion against a "definition of done" — a list of observable shell checks
derived once up front (terminal-bench grades by final state, so the agent's
self-checks mirror the grader's shape).

Composed: the done-checker is `action_run_validation_checks` (reused
verbatim); the criteria/check/judge pipeline mirrors the quality gate's
plan_checks → execute_checks → summarize.
"""

from __future__ import annotations

import logging

from agent.llm_json import parse_llm_json
from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

TASK_GOAL_SIGNATURE = "ops-task"


async def action_derive_task_goal(step_input: StepInput) -> StepOutput:
    """Bootstrap the single task goal + TaskState from the objective (idempotent).

    The objective is the task statement — no planning turn. Mirrors
    derive_extraction_goals' signature-idempotency. Reports whether the
    definition-of-done still needs deriving (empty completion_criteria).

    Context: mission
    Result: goals_ready, criteria_needed, created
    Publishes: mission
    """
    from agent.persistence.models import GoalRecord, TaskState

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"goals_ready": False}, observations="No mission")

    task_spec = str(getattr(mission, "objective", "") or "").strip()
    if not task_spec:
        return StepOutput(
            result={"goals_ready": False},
            observations="No objective — nothing for the ops flow set to do",
        )

    if getattr(mission, "task_definition", None) is None:
        mission.task_definition = TaskState(task_spec=task_spec)

    created = 0
    if not any(
        getattr(g, "finding_signature", "") == TASK_GOAL_SIGNATURE
        for g in mission.goals
    ):
        mission.goals.append(
            GoalRecord(
                description=f"Accomplish: {task_spec}",
                type="task_exec",
                status="incomplete",
                interaction_mode="exploratory",
                finding_signature=TASK_GOAL_SIGNATURE,
            )
        )
        created = 1
        if effects:
            await effects.save_mission(mission)

    criteria_needed = not mission.task_definition.completion_criteria
    return StepOutput(
        result={
            "goals_ready": True,
            "criteria_needed": criteria_needed,
            "created": created,
        },
        observations=(
            f"Ops task goal {'created' if created else 'present'}; "
            f"definition-of-done {'pending' if criteria_needed else 'set'}"
        ),
        context_updates={"mission": mission},
    )


async def action_store_completion_criteria(step_input: StepInput) -> StepOutput:
    """Parse the derived definition-of-done and store it on the TaskState.

    The criteria are a JSON array of shell checks (same shape as the quality
    gate's validation strategy); they are the stable target the work loop
    checks against each cycle.

    Context: mission, inference_response
    Result: criteria_count
    Publishes: mission
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission or getattr(mission, "task_definition", None) is None:
        return StepOutput(
            result={"criteria_count": 0}, observations="No task_definition"
        )

    parsed = parse_llm_json(str(step_input.context.get("inference_response", "")))
    # Prefer {"checks": [...]} (what the prompt emits and the reused check-runner
    # consumes). parse_llm_json collapses a top-level array to its first object,
    # so also accept a bare list and a lone check dict.
    if isinstance(parsed, dict):
        items = parsed.get("checks") or ([parsed] if parsed.get("command") else [])
    elif isinstance(parsed, list):
        items = parsed
    else:
        items = []
    criteria = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and str(item.get("command") or "").strip():
            criteria.append(
                {
                    "command": str(item["command"]).strip(),
                    "name": str(item.get("description") or item["command"])[:80],
                    "required": True,
                }
            )

    mission.task_definition.completion_criteria = criteria
    if effects:
        await effects.save_mission(mission)
    if not criteria:
        logger.warning("Definition-of-done parsed to 0 checks for ops task")
    return StepOutput(
        result={"criteria_count": len(criteria)},
        observations=f"Definition-of-done: {len(criteria)} completion check(s)",
        context_updates={"mission": mission},
    )


async def action_judge_task_completion(step_input: StepInput) -> StepOutput:
    """Decide whether the task is done, conservatively.

    "Done" requires BOTH the deterministic completion checks to pass
    (all_required_passing, from run_validation_checks) AND the judge
    inference to confirm (catches criteria that were too weak). Otherwise the
    judge's one-line feedback is stored on the TaskState to seed the next
    run_session, and the task goal stays incomplete (the loop continues until
    done or the budget caps).

    Context: mission, inference_response, validation_results (from checks)
    Result: task_done
    Publishes: mission
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"task_done": False}, observations="No mission")

    # Deterministic signal from the completion checks (computed from the
    # published results so we don't depend on a result-only field).
    results = step_input.context.get("validation_results") or []
    td = getattr(mission, "task_definition", None)
    criteria_defined = bool(getattr(td, "completion_criteria", None))
    if results:
        checks_passed = all(r.get("passed") for r in results if r.get("required", True))
    elif criteria_defined:
        # Criteria EXIST but produced no results — the checks didn't actually
        # run (a wiring/exec failure). The deterministic gate is the whole point
        # of an ops task, so don't let the judge declare done over a silent
        # bypass; loop instead (the next cycle re-runs the checks).
        checks_passed = False
    else:
        checks_passed = True  # genuinely no deterministic criteria — defer to judge

    parsed = parse_llm_json(str(step_input.context.get("inference_response", "")))
    parsed = parsed if isinstance(parsed, dict) else {}
    judge_done = bool(parsed.get("task_complete"))
    feedback = str(parsed.get("feedback") or "").strip()

    task_goal = next(
        (
            g
            for g in mission.goals
            if getattr(g, "finding_signature", "") == TASK_GOAL_SIGNATURE
        ),
        None,
    )

    done = checks_passed and judge_done
    if done and task_goal is not None:
        task_goal.status = "complete"
        observation = "Ops task complete (checks pass + judge confirms)"
    else:
        if getattr(mission, "task_definition", None) is not None:
            # Prefer the judge's note; fall back to "checks still failing".
            mission.task_definition.last_feedback = feedback or (
                "Some completion checks still fail — keep working toward them."
            )
            mission.task_definition.attempts += 1
        why = "checks fail" if not checks_passed else "judge: not yet done"
        observation = f"Ops task not done ({why}) — looping with feedback"

    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"task_done": done},
        observations=observation,
        context_updates={"mission": mission},
    )
