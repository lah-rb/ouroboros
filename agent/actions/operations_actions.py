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
import re

from agent.llm_json import parse_llm_json
from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

TASK_GOAL_SIGNATURE = "ops-task"

# ── asym-probe applicability gate ─────────────────────────────────────────
# The asym-probe (a property-based differential test) only makes sense for
# rule-inference tasks: implement a function/callable whose behaviour is pinned
# by WORKED EXAMPLES that may under-determine the rule (grid-pattern-transform is
# the archetype — its symmetric example hides rot90-vs-fliplr). Everywhere else
# (install/extract/bucket, or a plain deterministic conversion with no example
# ambiguity) the probe adds an inference for no signal, so it stays gated OFF.
_SOLVER_FN_RE = re.compile(
    r"(?i)\b(implement|complete|write)\b.{0,80}?\b(function|method|solver?)\b"
    r"|\bdef\s+\w+\s*\(|\b\w+\s*\([^)]*\)\s*(?:->|:)\s*"  # a def / typed signature
)
_EXAMPLE_RE = re.compile(r"(?i)\bexamples?\b|input.{0,8}?output|=>|->")


async def action_detect_solver_task(step_input: StepInput) -> StepOutput:
    """Gate the asym-probe. Fire (run_probe=True) iff ALL hold:
      - the objective asks to implement a callable AND shows worked examples
        (the rule-inference shape the probe disambiguates),
      - the deterministic completion checks already pass (a COMPLETE candidate —
        don't waste the probe on a half-written file).
    RE-VERIFY each complete candidate (not once): a wrong first solution gets a
    property counterexample as feedback, and the NEXT candidate is re-probed so a
    still-wrong fix can't be certified unverified (the single-shot version let
    that through). The extra probe turn is cheap now that swa_full keeps the
    static prefill cached. Deterministic — zero inference; incomplete/non-eligible
    cycles still pay nothing.
    """
    mission = step_input.context.get("mission")
    obj = str(getattr(mission, "objective", "") or "") if mission else ""
    is_solver = bool(_SOLVER_FN_RE.search(obj) and _EXAMPLE_RE.search(obj))

    results = step_input.context.get("validation_results") or []
    checks_passed = bool(results) and all(
        r.get("passed") for r in results if r.get("required", True)
    )

    run_probe = is_solver and checks_passed
    return StepOutput(
        result={"run_probe": run_probe, "is_solver_task": is_solver},
        observations=(
            "asym-probe firing (complete candidate — re-verify)"
            if run_probe
            else f"asym-probe skipped (solver={is_solver}, checks_passed={checks_passed})"
        ),
    )


def _strip_probe_fence(text: str) -> str:
    """Pull a python script out of a fenced block if the model fenced it."""
    s = (text or "").strip()
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", s, re.DOTALL)
    return (m.group(1) if m else s).strip()


async def action_run_property_probe(step_input: StepInput) -> StepOutput:
    """Write the generated property test into the working directory, run it,
    and APPEND its pass/fail as a required validation_result so the existing
    judge/decide loop treats a property violation as 'not done' and loops with
    the discrepancy as feedback. Self-contained — no oracle, just the spec's
    own invariants checked on discriminating inputs.

    Context: inference_response (the generated test), working_directory,
    validation_results (the checks so far, appended to).
    """
    effects = step_input.effects
    wd = step_input.params.get("working_directory") or "."
    test_src = _strip_probe_fence(str(step_input.context.get("inference_response", "")))
    results = list(step_input.context.get("validation_results") or [])
    updates: dict = {"validation_results": results}

    if not test_src or effects is None:
        # No usable probe — don't manufacture a failure; leave checks unchanged.
        return StepOutput(
            result={"probe_passed": True, "probe_ran": False},
            observations="asym-probe produced no test — skipped",
            context_updates=updates,
        )

    test_name = "asym_probe_test.py"
    detail = ""
    try:
        await effects.write_file(test_name, test_src)
        # Run from the working dir so the script's own directory is the import
        # root (`import grid_transform` resolves to the candidate).
        res = await effects.run_command(["python3", test_name], working_dir=wd, timeout=30)
        passed = res.return_code == 0
        detail = ((res.stdout or "") + (res.stderr or "")).strip()
        # Clean up so the probe artifact never pollutes the graded workspace.
        await effects.run_command(["rm", "-f", test_name], working_dir=wd, timeout=10)
    except Exception as exc:  # never crash the cycle on a probe error
        return StepOutput(
            result={"probe_passed": True, "probe_ran": False},
            observations=f"asym-probe error ({exc}) — skipped",
            context_updates=updates,
        )

    results.append(
        {
            "name": "asym_property_probe",
            "command": f"python3 {test_name}",
            "passed": passed,
            "required": True,
            "stdout": (res.stdout or "")[:500],
            "stderr": (res.stderr or "")[:500],
            "return_code": res.return_code,
        }
    )
    updates["validation_results"] = results
    return StepOutput(
        result={"probe_passed": passed, "probe_ran": True},
        observations=f"asym-probe {'PASS' if passed else 'FAIL'}: {detail[:160]}",
        context_updates=updates,
    )


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
    # published results so we don't depend on a result-only field). An ops task
    # is certified done ONLY against a derived definition-of-done — never on the
    # judge alone. So BOTH "no criteria at all" (derivation hasn't succeeded —
    # e.g. a transient inference error returned empty) and "criteria exist but
    # produced no results" (the checks didn't run — a wiring failure) are NOT
    # done: loop, and the next cycle re-derives / re-runs. This closes the
    # judge-only escape hatch that would otherwise complete a task whose
    # definition-of-done was never established.
    results = step_input.context.get("validation_results") or []
    td = getattr(mission, "task_definition", None)
    criteria_defined = bool(getattr(td, "completion_criteria", None))
    if results:
        checks_passed = all(r.get("passed") for r in results if r.get("required", True))
        no_criteria = False
    else:
        checks_passed = False
        no_criteria = not criteria_defined

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
            # Prefer the judge's note; fall back to a reason-specific default.
            if no_criteria:
                default_fb = (
                    "The definition-of-done has not been derived yet — completion "
                    "cannot be certified. Keep working; criteria will be re-derived."
                )
            else:
                default_fb = (
                    "Some completion checks still fail — keep working toward them."
                )
            mission.task_definition.last_feedback = feedback or default_fb
            mission.task_definition.attempts += 1
        why = (
            "no definition-of-done"
            if no_criteria
            else ("checks fail" if not checks_passed else "judge: not yet done")
        )
        observation = f"Ops task not done ({why}) — looping with feedback"

    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"task_done": done},
        observations=observation,
        context_updates={"mission": mission},
    )
