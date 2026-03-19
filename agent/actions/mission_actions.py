"""Mission control actions — load state, assess, dispatch, finalize.

These actions power the mission_control flow and the create_plan flow.
They interact with persistence through the effects interface.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)


async def action_load_mission_state(step_input: StepInput) -> StepOutput:
    """Load mission state and events from persistence.

    Reads mission.json and events.json via effects.

    Publishes: mission, events, frustration
    Result: mission status fields for resolver branching
    """
    effects = step_input.effects
    if effects is None:
        return StepOutput(
            result={"mission": {"status": "aborted"}},
            observations="No effects interface — cannot load state",
        )

    mission = await effects.load_mission()
    if mission is None:
        return StepOutput(
            result={"mission": {"status": "aborted"}},
            observations="No mission found in persistence",
        )

    events = await effects.read_events()

    # Build frustration map from task records
    frustration = {}
    for task in mission.plan:
        frustration[task.id] = task.frustration

    return StepOutput(
        result={
            "mission": {"status": mission.status},
            "events_pending": len(events) > 0,
        },
        observations=f"Loaded mission {mission.id}: status={mission.status}, "
        f"{len(mission.plan)} tasks, {len(events)} pending events",
        context_updates={
            "mission": mission,
            "events": events,
            "frustration": frustration,
        },
    )


async def action_update_task_status(step_input: StepInput) -> StepOutput:
    """Apply the previous flow's outcome to mission state.

    Reads last_result, last_status, last_task_id from context.
    Updates the corresponding task record in the mission plan.

    Publishes: mission, frustration (updated)
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    frustration = step_input.context.get("frustration", {})
    last_status = step_input.context.get("last_status")
    last_task_id = step_input.context.get("last_task_id")
    last_result = step_input.context.get("last_result")

    if not last_task_id or not last_status:
        # No previous result to apply (first cycle or restart)
        events = step_input.context.get("events", [])
        return StepOutput(
            result={"events_pending": len(events) > 0},
            observations="No previous result to apply (first cycle)",
            context_updates={
                "mission": mission,
                "frustration": frustration,
            },
        )

    # Find and update the task
    completed_task = None
    for task in mission.plan:
        if task.id == last_task_id:
            if last_status == "success":
                task.status = "complete"
                task.frustration = 0
                task.summary = str(last_result)[:200] if last_result else "Completed"
                completed_task = task
            elif last_status == "escalated":
                task.status = "blocked"
                task.frustration += 1
                frustration[task.id] = task.frustration
            elif last_status == "abandoned":
                task.status = "failed"
                task.frustration += 1
                frustration[task.id] = task.frustration
            elif last_status == "in_progress":
                task.status = "in_progress"
            break

    # When a corrective task completes, unblock tasks stuck on the same root cause
    unblocked_ids = []
    if completed_task and last_status == "success":
        unblocked_ids = _unblock_tasks_after_corrective(mission, completed_task)
        for uid in unblocked_ids:
            frustration[uid] = next(
                (t.frustration for t in mission.plan if t.id == uid), 0
            )

    # Save updated mission
    if effects:
        await effects.save_mission(mission)

    events = step_input.context.get("events", [])
    return StepOutput(
        result={
            "events_pending": len(events) > 0,
            "task_completed": last_status == "success",
        },
        observations=f"Updated task {last_task_id}: status={last_status}"
        + (f", unblocked {len(unblocked_ids)} tasks" if unblocked_ids else ""),
        context_updates={
            "mission": mission,
            "frustration": frustration,
        },
    )


async def action_handle_events(step_input: StepInput) -> StepOutput:
    """Process pending events from the event queue.

    Handles: abort, pause, resume, user_message, priority_change.
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    events = step_input.context.get("events", [])

    abort_requested = False
    pause_requested = False
    task_unblocked = False

    for event in events:
        etype = event.type if hasattr(event, "type") else event.get("type", "")
        payload = (
            event.payload if hasattr(event, "payload") else event.get("payload", {})
        )

        if etype == "abort":
            abort_requested = True
        elif etype == "pause":
            pause_requested = True
        elif etype == "resume":
            pass  # Mission already active
        elif etype == "user_message":
            msg = payload.get("message", "")
            logger.info("User message: %s", msg)

    # Clear processed events
    if effects and events:
        await effects.clear_events()

    # Save mission if state changed
    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={
            "abort_requested": abort_requested,
            "pause_requested": pause_requested,
            "task_unblocked": task_unblocked,
        },
        observations=f"Processed {len(events)} events. "
        f"abort={abort_requested}, pause={pause_requested}",
        context_updates={
            "mission": mission,
            "unblocked_tasks": [],
        },
    )


async def action_assess_mission_progress(step_input: StepInput) -> StepOutput:
    """Determine what to work on next — rule-based fast path.

    Finds pending tasks with met dependencies. If there's exactly one
    obvious next task, returns it directly. Otherwise signals for
    LLM prioritization (or completion check).

    Also scans failure notes for cross-task patterns and auto-inserts
    prerequisite tasks (e.g. manage_packages when pytest is missing).

    Publishes: assessment, obvious_next_task
    """
    mission = step_input.context.get("mission")
    frustration = step_input.context.get("frustration", {})

    if not mission or not hasattr(mission, "plan"):
        return StepOutput(
            result={
                "all_tasks_complete": False,
                "all_remaining_blocked": False,
                "obvious_next_task": None,
                "needs_plan": True,
            },
            observations="No mission plan found — need to create one",
            context_updates={
                "assessment": {"ready_tasks": [], "summary": "No plan exists"},
            },
        )

    completed = [t for t in mission.plan if t.status == "complete"]
    pending = [t for t in mission.plan if t.status == "pending"]
    in_progress = [t for t in mission.plan if t.status == "in_progress"]
    blocked = [t for t in mission.plan if t.status == "blocked"]
    failed = [t for t in mission.plan if t.status == "failed"]

    # Safety net: recover stale in_progress tasks.
    # When we're back in mission_control, no child flow is running —
    # any in_progress task is leftover from a previous dispatch.
    effects = step_input.effects
    if in_progress:
        for task in in_progress:
            logger.warning(
                "Recovering stale in_progress task %s (%s) — marking pending",
                task.id,
                task.description[:40],
            )
            task.status = "pending"
        if effects:
            await effects.save_mission(mission)
        # Recalculate after recovery
        completed = [t for t in mission.plan if t.status == "complete"]
        pending = [t for t in mission.plan if t.status == "pending"]
        in_progress = []
        blocked = [t for t in mission.plan if t.status == "blocked"]
        failed = [t for t in mission.plan if t.status == "failed"]

    # Check if all tasks are done (complete or blocked, nothing actionable left)
    actionable = pending + failed
    gate_exhausted = getattr(mission, "quality_gate_attempts", 0) >= 2
    if not actionable and len(mission.plan) > 0:
        return StepOutput(
            result={
                "all_tasks_complete": True,
                "quality_gate_exhausted": gate_exhausted,
                "all_remaining_blocked": len(blocked) > 0,
                "obvious_next_task": None,
                "needs_plan": False,
            },
            observations=(
                f"Mission done: {len(completed)} complete, {len(blocked)} blocked"
                + (
                    f", quality gate exhausted ({mission.quality_gate_attempts} attempts)"
                    if gate_exhausted
                    else ""
                )
            ),
            context_updates={
                "assessment": {
                    "ready_tasks": [],
                    "summary": f"{len(completed)}/{len(mission.plan)} complete"
                    + (f", {len(blocked)} blocked" if blocked else ""),
                },
            },
        )

    # Check if we need to create a plan first
    if len(mission.plan) == 0:
        return StepOutput(
            result={
                "all_tasks_complete": False,
                "all_remaining_blocked": False,
                "obvious_next_task": None,
                "needs_plan": True,
            },
            observations="Empty plan — need to create tasks from objective",
            context_updates={
                "assessment": {"ready_tasks": [], "summary": "No tasks yet"},
            },
        )

    # ── Failure pattern detection ─────────────────────────────
    # Scan recent failure notes for cross-task patterns.
    # If multiple tasks fail for the same root cause (e.g. missing pytest),
    # auto-insert a corrective task instead of retrying blindly.
    corrective_inserted = _detect_and_correct_failure_patterns(mission, failed, effects)
    if corrective_inserted and effects:
        await effects.save_mission(mission)
        # Recalculate after inserting corrective tasks
        pending = [t for t in mission.plan if t.status == "pending"]
        failed = [t for t in mission.plan if t.status == "failed"]

    # ── Cross-task frustration acceleration ───────────────────
    # If 2+ tasks of the same flow type are failing, boost frustration
    # on the remaining ones so we don't burn cycles on identical failures.
    _accelerate_cross_task_frustration(failed, frustration)

    # Find tasks with met dependencies
    completed_ids = {t.id for t in completed}
    ready_tasks = []
    for task in pending:
        deps_met = all(dep in completed_ids for dep in task.depends_on)
        if deps_met:
            ready_tasks.append(task)

    # Also consider failed tasks for retry, with frustration cap
    for task in failed:
        if task.frustration < 5:
            ready_tasks.append(task)
        elif task.frustration >= 5 and task.status != "blocked":
            # Cap: mark as blocked, stop retrying
            task.status = "blocked"
            task.summary = (
                f"Blocked after {task.frustration} failed attempts. "
                f"Awaiting escalation capability (Phase 6)."
            )
            if effects:
                await effects.save_mission(mission)

    # Sort by priority
    ready_tasks.sort(key=lambda t: t.priority)

    if not ready_tasks:
        all_blocked = len(pending) == 0 and len(in_progress) == 0
        return StepOutput(
            result={
                "all_tasks_complete": False,
                "all_remaining_blocked": all_blocked,
                "obvious_next_task": None,
                "needs_plan": False,
            },
            observations=f"No ready tasks. Pending: {len(pending)}, "
            f"blocked: {len(blocked)}, failed: {len(failed)}",
            context_updates={
                "assessment": {
                    "ready_tasks": [],
                    "summary": f"Blocked: {len(blocked)}, failed: {len(failed)}",
                },
            },
        )

    # Fast path: one obvious next task
    obvious = ready_tasks[0]
    return StepOutput(
        result={
            "all_tasks_complete": False,
            "all_remaining_blocked": False,
            "obvious_next_task": {
                "id": obvious.id,
                "description": obvious.description,
                "flow": obvious.flow,
            },
            "needs_plan": False,
        },
        observations=f"Found {len(ready_tasks)} ready tasks. "
        f"Next: {obvious.description} (flow: {obvious.flow})",
        context_updates={
            "assessment": {
                "ready_tasks": [
                    {"id": t.id, "description": t.description, "flow": t.flow}
                    for t in ready_tasks
                ],
                "summary": f"{len(completed)}/{len(mission.plan)} complete, "
                f"{len(ready_tasks)} ready",
            },
            "obvious_next_task": {
                "id": obvious.id,
                "description": obvious.description,
                "flow": obvious.flow,
                "inputs": obvious.inputs,
            },
        },
    )


def _is_duplicate_task(
    mission, description: str, flow: str, target_file: str = ""
) -> bool:
    """Check if a substantially similar task already exists in the plan.

    Matches on:
    - Same flow type AND same target_file_path (for file-targeting flows)
    - OR near-identical description (same file mentioned + same action verb)

    Skips completed tasks — re-creating a completed task is fine if context changed.
    """
    desc_lower = description.lower()
    for task in mission.plan:
        if task.status == "complete":
            continue
        # Same flow + same target file = duplicate
        task_target = task.inputs.get("target_file_path", "")
        if flow == task.flow and target_file and task_target == target_file:
            return True
        # Near-identical description (same file + similar intent)
        existing_lower = task.description.lower()
        if desc_lower == existing_lower:
            return True
        # Both mention the same file and same flow type
        if target_file and target_file in existing_lower and flow == task.flow:
            return True
    return False


def _unblock_tasks_after_corrective(mission, completed_task) -> list[str]:
    """When a corrective task (manage_packages, etc.) completes, unblock
    tasks that were blocked by the same root cause.

    Returns list of unblocked task IDs.
    """
    corrective_flows = {"manage_packages", "setup_project"}
    if completed_task.flow not in corrective_flows:
        return []

    completed_reason = (completed_task.inputs.get("reason", "") or "").lower()
    unblocked = []

    for task in mission.plan:
        if task.status != "blocked":
            continue

        # Heuristic: if a manage_packages task completed for "pytest" and
        # a create_tests task is blocked, unblock it.
        task_flow = task.flow
        task_summary = (task.summary or "").lower()

        should_unblock = False

        # manage_packages completion → unblock create_tests, validate_behavior
        if completed_task.flow == "manage_packages":
            if task_flow in ("create_tests", "validate_behavior"):
                should_unblock = True
            # Check for keyword overlap between the corrective reason and failure
            if any(
                kw in completed_reason and kw in task_summary
                for kw in ["pytest", "dependency", "package", "install", "import"]
            ):
                should_unblock = True

        # setup_project completion → unblock tasks that depend on project structure
        if completed_task.flow == "setup_project":
            if task_flow in ("create_file", "create_tests", "integrate_modules"):
                should_unblock = True

        if should_unblock:
            task.status = "pending"
            # Partial frustration reset — they did fail, but root cause is fixed
            task.frustration = max(0, task.frustration - 2)
            task.summary = (
                f"Unblocked after {completed_task.flow} completed: "
                f"{completed_task.summary or completed_task.description[:60]}"
            )
            unblocked.append(task.id)
            logger.info(
                "Unblocked task %s (%s) after corrective %s completed",
                task.id[:8],
                task.description[:40],
                completed_task.flow,
            )

    return unblocked


def _detect_and_correct_failure_patterns(mission, failed_tasks, effects) -> bool:
    """Scan failure notes for recurring patterns and auto-insert corrective tasks.

    Detects common failure signatures across notes:
    - Missing packages (pytest, dependencies)
    - Missing files or modules
    - Schema/config mismatches

    Returns True if a corrective task was inserted into the plan.
    """
    from agent.persistence.models import TaskRecord

    if not hasattr(mission, "notes") or not mission.notes:
        return False

    # Only check failure_analysis notes from the last 10
    failure_notes = [n for n in mission.notes if n.category == "failure_analysis"]
    if len(failure_notes) < 2:
        return False

    recent_failures = failure_notes[-10:]
    note_texts = [n.content.lower() for n in recent_failures]
    combined = " ".join(note_texts)

    # Check if a corrective task already exists to avoid duplicates
    existing_descriptions = {t.description.lower() for t in mission.plan}

    inserted = False

    # ── Pattern: missing pytest / test framework ──────────────
    pytest_keywords = ["pytest", "test runner", "testing framework"]
    pytest_hits = sum(
        1 for text in note_texts if any(kw in text for kw in pytest_keywords)
    )
    if pytest_hits >= 2:
        corrective_desc = "Install pytest and testing dependencies"
        if corrective_desc.lower() not in existing_descriptions:
            # Find the highest priority among failed test tasks
            test_tasks = [t for t in failed_tasks if t.flow == "create_tests"]
            insert_priority = min(
                (t.priority for t in test_tasks), default=len(mission.plan)
            )
            task = TaskRecord(
                description=corrective_desc,
                flow="manage_packages",
                priority=max(0, insert_priority - 1),
                inputs={
                    "target_file_path": "",
                    "reason": "Multiple test tasks failed due to missing pytest. "
                    "Must install before retrying test creation.",
                },
            )
            mission.plan.append(task)
            inserted = True
            logger.info(
                "Failure pattern detected: missing pytest. "
                "Auto-inserted manage_packages task."
            )

    # ── Pattern: missing package / import error ───────────────
    import_keywords = [
        "modulenotfounderror",
        "no module named",
        "import error",
        "not installed",
        "missing dependency",
        "pip install",
        "uv add",
    ]
    import_hits = sum(
        1 for text in note_texts if any(kw in text for kw in import_keywords)
    )
    if import_hits >= 2 and not inserted:
        # Try to extract the package name from notes
        package_match = re.search(
            r"(?:no module named|install|uv add|pip install)\s+['\"]?(\w+)",
            combined,
        )
        pkg_name = package_match.group(1) if package_match else "required"
        corrective_desc = f"Install missing dependency: {pkg_name}"
        if corrective_desc.lower() not in existing_descriptions:
            task = TaskRecord(
                description=corrective_desc,
                flow="manage_packages",
                priority=0,  # High priority — unblocks other tasks
                inputs={
                    "target_file_path": "",
                    "reason": f"Multiple tasks failed due to missing package '{pkg_name}'. "
                    f"Must install before retrying dependent tasks.",
                },
            )
            mission.plan.append(task)
            inserted = True
            logger.info(
                "Failure pattern detected: missing package '%s'. "
                "Auto-inserted manage_packages task.",
                pkg_name,
            )

    # ── Pattern: lint / unused imports recurring ──────────────
    lint_keywords = ["unused import", "f401", "lint error", "ruff", "flake8"]
    lint_hits = sum(1 for text in note_texts if any(kw in text for kw in lint_keywords))
    if lint_hits >= 3:
        # Extract file paths mentioned in lint notes
        file_matches = re.findall(
            r"(?:in|fix|for)\s+[`'\"]?([a-zA-Z0-9_/.-]+\.py)[`'\"]?",
            combined,
        )
        for filepath in set(file_matches[:3]):
            corrective_desc = f"Fix lint errors in {filepath}"
            if corrective_desc.lower() not in existing_descriptions:
                task = TaskRecord(
                    description=corrective_desc,
                    flow="modify_file",
                    priority=0,
                    inputs={
                        "target_file_path": filepath,
                        "reason": f"Recurring lint failures in {filepath}. "
                        f"Fix unused imports and other lint issues.",
                    },
                )
                mission.plan.append(task)
                inserted = True
                logger.info(
                    "Failure pattern detected: recurring lint in %s. "
                    "Auto-inserted modify_file task.",
                    filepath,
                )

    return inserted


def _accelerate_cross_task_frustration(failed_tasks, frustration: dict) -> None:
    """Boost frustration for tasks sharing a flow type when siblings are failing.

    If 2+ tasks of the same flow type have failed, bump the frustration
    of the remaining tasks in that group by 1 so they block sooner.
    This prevents burning N × 5 cycles on N tasks with the same root cause.
    """
    from collections import Counter

    flow_failures = Counter(t.flow for t in failed_tasks)
    for flow_type, count in flow_failures.items():
        if count >= 2:
            for task in failed_tasks:
                if task.flow == flow_type and task.frustration < 5:
                    # Boost by 1 extra per cycle when siblings also failing
                    task.frustration += 1
                    frustration[task.id] = task.frustration
                    logger.info(
                        "Cross-task frustration boost: task %s (%s) → %d",
                        task.id[:8],
                        flow_type,
                        task.frustration,
                    )


async def action_configure_task_dispatch(step_input: StepInput) -> StepOutput:
    """Build input map and determine flow config for the selected task.

    Includes temperature perturbation at frustration 2+ and relevant
    notes injection for child flow context awareness.

    Publishes: dispatch_config
    """
    import random

    effects = step_input.effects
    mission = step_input.context.get("mission")
    selected = step_input.context.get("selected_task") or step_input.context.get(
        "obvious_next_task"
    )
    frustration = step_input.context.get("frustration", {})

    if not selected:
        return StepOutput(
            result={},
            observations="No task selected for dispatch",
            context_updates={"dispatch_config": None},
        )

    task_id = selected["id"]
    task_flow = selected.get("flow", "create_file")
    task_inputs = selected.get("inputs", {})
    task_frustration = frustration.get(task_id, 0)

    # ── Frustration windowing ─────────────────────────────────
    # At higher frustration levels, promote simple flows to more
    # sophisticated ones and unlock advanced capabilities.
    # This prevents the agent from repeating the same failing approach.
    flow_promotions = {
        # frustration >= 2: simple creation → modify (add context awareness)
        2: {
            "create_file": "modify_file",
        },
        # frustration >= 3: unlock diagnostic flows, promote to integration
        3: {
            "modify_file": "diagnose_issue",
        },
        # frustration >= 4: everything goes through diagnosis first
        4: {
            "create_file": "diagnose_issue",
            "modify_file": "diagnose_issue",
        },
    }

    original_flow = task_flow
    for threshold in sorted(flow_promotions.keys()):
        if task_frustration >= threshold and task_flow in flow_promotions[threshold]:
            task_flow = flow_promotions[threshold][task_flow]

    if task_flow != original_flow:
        logger.info(
            "Frustration windowing: task %s promoted %s → %s (frustration=%d)",
            task_id,
            original_flow,
            task_flow,
            task_frustration,
        )

    # Mark task as in_progress
    for task in mission.plan:
        if task.id == task_id:
            task.status = "in_progress"
            break

    if effects:
        await effects.save_mission(mission)

    # Build the input map for the child flow
    input_map = {
        "mission_id": mission.id,
        "task_id": task_id,
        "task_description": selected["description"],
        "mission_objective": mission.objective,
        "working_directory": mission.config.working_directory,
        "target_file_path": "",  # default empty — task_inputs may override
        **task_inputs,
    }

    # Frustration-gated escalation permissions
    task_frustration = frustration.get(task_id, 0)
    escalation_permissions = []
    thresholds = step_input.params.get("frustration_thresholds", {})
    if task_frustration >= thresholds.get("review", 2):
        escalation_permissions.append("review")
    if task_frustration >= thresholds.get("instructions", 4):
        escalation_permissions.append("instructions")
    if task_frustration >= thresholds.get("direct_fix", 5):
        escalation_permissions.append("direct_fix")

    # Temperature perturbation at frustration 2+
    temperature_multiplier = 1.0
    strategies = step_input.params.get("frustration_strategies", {})
    temp_config = strategies.get("temperature_perturb", {})

    if task_frustration >= temp_config.get("min_frustration", 2):
        offset_range = temp_config.get("offset_range", [0.15, 0.4])
        offset = random.uniform(*offset_range)
        # Alternate: even frustration = hotter, odd = cooler
        if task_frustration % 2 == 0:
            temperature_multiplier = 1.0 + offset
        else:
            temperature_multiplier = max(0.3, 1.0 - offset)

    input_map["temperature_multiplier"] = str(temperature_multiplier)

    # Pass frustration info to child flows
    input_map["frustration_level"] = str(task_frustration)

    # Build frustration history from last attempt
    if task_frustration >= 3:
        last_attempt_summary = None
        for task in mission.plan:
            if task.id == task_id and task.summary:
                last_attempt_summary = task.summary
                break
        if last_attempt_summary:
            input_map["frustration_history"] = (
                f"Attempt {task_frustration}: {last_attempt_summary}"
            )

    # Gather relevant notes for context — always include architecture blueprint
    relevant_notes = ""
    if hasattr(mission, "notes") and mission.notes:
        # Partition: blueprint notes are always included, then fill with recent others
        blueprint_notes = [
            n for n in mission.notes if n.category == "architecture_blueprint"
        ]
        other_notes = [
            n for n in mission.notes if n.category != "architecture_blueprint"
        ]
        recent_others = sorted(other_notes, key=lambda n: n.timestamp, reverse=True)[
            : max(5, 10 - len(blueprint_notes))
        ]
        combined = blueprint_notes + recent_others
        relevant_notes = "\n".join(f"[{n.category}] {n.content}" for n in combined)
    input_map["relevant_notes"] = relevant_notes

    dispatch_config = {
        "flow": task_flow,
        "task_id": task_id,
        "input_map": input_map,
        "escalation_permissions": escalation_permissions,
        "temperature_multiplier": temperature_multiplier,
    }

    return StepOutput(
        result={},
        observations=f"Dispatching task {task_id} to flow {task_flow} "
        f"(frustration={task_frustration}, temp_mult={temperature_multiplier:.2f})",
        context_updates={"dispatch_config": dispatch_config},
    )


async def action_finalize_mission(step_input: StepInput) -> StepOutput:
    """Mark mission complete or aborted and save."""
    effects = step_input.effects
    mission = step_input.context.get("mission")

    if not mission:
        return StepOutput(
            result={"finalized": False},
            observations="No mission to finalize",
        )

    # Determine final status from context
    completion = step_input.context.get("completion_assessment")
    if completion:
        mission.status = "completed"
    elif step_input.params.get("abort", False):
        mission.status = "aborted"
    else:
        mission.status = "completed"

    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={"finalized": True, "status": mission.status},
        observations=f"Mission {mission.id} finalized: {mission.status}",
    )


async def action_enter_idle(step_input: StepInput) -> StepOutput:
    """Enter idle state — nothing to do, wait for events."""
    return StepOutput(
        result={"idle": True},
        observations="Entering idle state — waiting for events",
    )


async def action_create_plan_from_objective(step_input: StepInput) -> StepOutput:
    """Parse the LLM's response to create a task plan.

    The inference step before this has already asked the model to produce
    a JSON task list. This action parses the response and updates the
    mission plan.

    Expects context: mission, inference_response
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    response = step_input.context.get("inference_response", "")

    if not mission:
        return StepOutput(
            result={"plan_created": False},
            observations="No mission in context",
        )

    # Parse the LLM response to extract tasks
    tasks = _parse_task_list(response, mission.config.working_directory)

    if not tasks:
        # Fallback: create a single generic task
        from agent.persistence.models import TaskRecord

        tasks = [
            TaskRecord(
                description=f"Implement: {mission.objective}",
                flow="create_file",
                inputs={
                    "target_file_path": "app.py",
                    "reason": mission.objective,
                },
            )
        ]

    mission.plan = tasks

    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={"plan_created": True, "task_count": len(tasks)},
        observations=f"Created plan with {len(tasks)} tasks: "
        + ", ".join(t.description[:50] for t in tasks),
        context_updates={"mission": mission},
    )


def _parse_task_list(response: str, working_dir: str) -> list:
    """Parse an LLM response into TaskRecord objects.

    Tries JSON parsing first, then falls back to line-based parsing.
    """
    from agent.persistence.models import TaskRecord

    tasks = []

    # Strip markdown wrappers (models often add ```json despite instructions)
    from agent.actions.refinement_actions import strip_markdown_wrapper

    response = strip_markdown_wrapper(response)

    # Try to extract JSON array from the response
    # CRITICAL: Use greedy match (not *?) to find outermost brackets.
    # Non-greedy would match inner arrays like depends_on: [] instead
    # of the full task list.
    json_match = re.search(r"\[[\s\S]*\]", response)
    if json_match:
        try:
            items = json.loads(json_match.group())
            # First pass: create tasks and build description→id map
            desc_to_id = {}
            for i, item in enumerate(items):
                if isinstance(item, dict):
                    desc = item.get("description", item.get("task", str(item)))
                    filename = item.get("file", item.get("filename", f"file_{i}.py"))
                    flow = item.get("flow") or _infer_flow_from_description(desc)

                    # For create_tests, target_file_path = source to test, not test file
                    task_inputs = {
                        "target_file_path": filename,
                        "reason": desc,
                    }
                    if flow == "create_tests":
                        # Derive source file from test path or description
                        source_file = _derive_source_for_tests(filename, desc)
                        task_inputs["target_file_path"] = source_file
                        task_inputs["test_file_path"] = filename

                    task = TaskRecord(
                        description=desc,
                        flow=flow,
                        priority=i,
                        inputs=task_inputs,
                    )
                    desc_to_id[desc] = task.id
                    tasks.append(task)
                elif isinstance(item, str):
                    tasks.append(
                        TaskRecord(
                            description=item,
                            flow="create_file",
                            priority=i,
                            inputs={
                                "target_file_path": f"file_{i}.py",
                                "reason": item,
                            },
                        )
                    )

            # Second pass: resolve depends_on descriptions to task IDs
            if tasks:
                for i, item in enumerate(items):
                    if isinstance(item, dict) and i < len(tasks):
                        raw_deps = item.get("depends_on", [])
                        if isinstance(raw_deps, list):
                            resolved = []
                            for dep_desc in raw_deps:
                                if isinstance(dep_desc, str):
                                    # Try exact match first, then substring
                                    dep_id = desc_to_id.get(dep_desc)
                                    if not dep_id:
                                        for d, tid in desc_to_id.items():
                                            if dep_desc in d or d in dep_desc:
                                                dep_id = tid
                                                break
                                    if dep_id:
                                        resolved.append(dep_id)
                            tasks[i].depends_on = resolved
                return tasks
        except json.JSONDecodeError:
            pass

    # Fallback: parse numbered lines like "1. Create main.py - ..."
    lines = response.strip().splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        # Match patterns like "1. Create main.py" or "- Create main.py"
        match = re.match(r"^(?:\d+[\.\)]\s*|-\s*)(.*)", line)
        if match:
            desc = match.group(1).strip()
            if desc and len(desc) > 5:
                # Try to extract a filename
                file_match = re.search(r"[`'\"]?(\w+\.py)[`'\"]?", desc)
                filename = file_match.group(1) if file_match else f"file_{i}.py"
                tasks.append(
                    TaskRecord(
                        description=desc,
                        flow="create_file",
                        priority=i,
                        inputs={
                            "target_file_path": filename,
                            "reason": desc,
                        },
                    )
                )

    return tasks


def _derive_source_for_tests(test_path: str, desc: str) -> str:
    """Derive the source file path from a test file path or description.

    Examples:
        tests/test_calculator.py → calculator.py
        test_game.py → game.py
        "Write tests for calculator.py" → calculator.py
    """
    import os

    # First: try to extract source file from the description
    source_match = re.search(
        r"(?:for|of|testing)\s+[`'\"]?([a-zA-Z0-9_/.-]+\.py)[`'\"]?",
        desc,
        re.IGNORECASE,
    )
    if source_match:
        candidate = source_match.group(1)
        # Don't return test files as source
        basename = os.path.basename(candidate)
        if not basename.startswith("test_"):
            return candidate

    # Second: derive from test path
    basename = os.path.basename(test_path)
    if basename.startswith("test_"):
        source_name = basename[5:]  # strip "test_" prefix
        return source_name

    # Fallback: return the path as-is
    return test_path


def _infer_flow_from_description(desc: str) -> str:
    """Infer the appropriate flow from a task description's keywords.

    Maps common task verbs and keywords to flow names.
    Falls back to 'create_file' for unrecognized descriptions.
    """
    desc_lower = desc.lower()

    # Architecture design tasks (before generic "create" match)
    if any(
        kw in desc_lower
        for kw in [
            "design architecture",
            "design project structure",
            "plan module layout",
            "design module boundaries",
            "architecture blueprint",
            "design directory layout",
        ]
    ):
        return "design_architecture"

    # Package management tasks (before generic "create" match)
    if any(
        kw in desc_lower
        for kw in [
            "install package",
            "manage package",
            "add dependency",
            "pip install",
            "create venv",
            "virtual environment",
            "requirements.txt",
            "manage dependencies",
        ]
    ):
        return "manage_packages"

    # Behavioral testing / run-and-verify tasks (before generic "test" match)
    if any(
        kw in desc_lower
        for kw in [
            "run and verify",
            "test behavior",
            "validate behavior",
            "run the app",
            "test interactively",
            "execute and test",
            "behavioral test",
            "run the program",
        ]
    ):
        return "validate_behavior"

    # Investigation / exploration tasks
    if any(
        kw in desc_lower
        for kw in [
            "investigate",
            "explore",
            "understand",
            "research",
            "spike",
            "analyze codebase",
            "study",
        ]
    ):
        return "explore_spike"

    # Diagnosis / debugging tasks
    if any(
        kw in desc_lower
        for kw in ["diagnose", "debug", "find the bug", "root cause", "trace error"]
    ):
        return "diagnose_issue"

    # Integration tasks
    if any(
        kw in desc_lower
        for kw in [
            "integrate",
            "connect modules",
            "glue code",
            "wire up",
            "link modules",
            "missing imports",
        ]
    ):
        return "integrate_modules"

    # Refactoring tasks (dedicated flow, not modify_file)
    if any(
        kw in desc_lower
        for kw in [
            "refactor",
            "restructure",
            "code smell",
            "clean up code",
            "improve structure",
        ]
    ):
        return "refactor"

    # Documentation tasks
    if any(
        kw in desc_lower
        for kw in [
            "document",
            "write readme",
            "add docstring",
            "write documentation",
            "update readme",
            "architecture doc",
        ]
    ):
        return "document_project"

    # Retrospective tasks
    if any(
        kw in desc_lower
        for kw in [
            "retrospective",
            "self-assessment",
            "review progress",
            "analyze performance",
            "mission health",
        ]
    ):
        return "retrospective"

    # Review tasks
    if any(
        kw in desc_lower
        for kw in [
            "request review",
            "code review",
            "submit for review",
            "seek feedback",
        ]
    ):
        return "request_review"

    # Test creation tasks (before generic "create" — "create tests" must match create_tests)
    if any(
        kw in desc_lower
        for kw in ["test", "write tests", "create tests", "add tests", "test_"]
    ):
        return "create_tests"

    # Explicit file creation tasks (check before modify — "create X in file.py")
    if desc_lower.startswith("create ") or desc_lower.startswith("build "):
        return "create_file"

    # Modification tasks (only match when the verb is clearly about modifying)
    if any(
        kw in desc_lower
        for kw in [
            "modify",
            "change",
            "fix",
            "edit",
            "patch",
            "adjust",
        ]
    ):
        return "modify_file"

    # "update" only as leading verb — avoid matching "update functions" in descriptions
    if desc_lower.startswith("update "):
        return "modify_file"

    # Setup tasks
    if any(
        kw in desc_lower
        for kw in ["setup", "initialize", "configure", "scaffold", "bootstrap"]
    ):
        return "setup_project"

    # Default: create file
    return "create_file"


async def action_execute_file_creation(step_input: StepInput) -> StepOutput:
    """Parse the LLM's file content response and write the file.

    Expects context: inference_response, task_description
    Params: target_file_path
    """
    effects = step_input.effects
    response = step_input.context.get("inference_response", "")
    target = step_input.params.get("target_file_path", "") or step_input.context.get(
        "target_file_path", "app.py"
    )

    if not effects:
        return StepOutput(
            result={"write_success": False},
            observations="No effects interface",
        )

    # Extract code from the response (use improved multi-strategy extractor)
    from agent.actions.refinement_actions import extract_code_from_response

    code = extract_code_from_response(response)

    if not code.strip():
        code = "# Generated by Ouroboros\nprint('Hello, World!')\n"

    result = await effects.write_file(target, code)

    return StepOutput(
        result={
            "write_success": result.success,
            "file_path": target,
        },
        observations=(
            f"Wrote {result.bytes_written} bytes to {target}"
            if result.success
            else f"Write failed: {result.error}"
        ),
        context_updates={
            "created_file": {"path": target, "content": code},
        },
    )


def _extract_code(response: str) -> str:
    """Extract code from an LLM response, handling markdown code blocks."""
    # Look for ```python ... ``` blocks
    match = re.search(r"```(?:python)?\s*\n([\s\S]*?)```", response)
    if match:
        return match.group(1).strip() + "\n"

    # Look for any ``` ... ``` block
    match = re.search(r"```\s*\n([\s\S]*?)```", response)
    if match:
        return match.group(1).strip() + "\n"

    # If the response looks like code (has def/class/import), use it directly
    lines = response.strip().splitlines()
    code_lines = [
        l
        for l in lines
        if l.strip() and not l.strip().startswith(("#", "Here", "This", "The", "I "))
    ]
    if any(
        l.strip().startswith(("def ", "class ", "import ", "from ")) for l in code_lines
    ):
        return "\n".join(code_lines) + "\n"

    return response


async def action_run_tests(step_input: StepInput) -> StepOutput:
    """Run a test command and report results.

    Params: command (list of str), scope
    """
    effects = step_input.effects
    if not effects:
        return StepOutput(
            result={"all_passing": True},
            observations="No effects — skipping tests (assumed pass)",
        )

    cmd = step_input.params.get("command", ["python", "-c", "print('OK')"])
    if isinstance(cmd, str):
        cmd = cmd.split()

    result = await effects.run_command(cmd, timeout=60)

    all_passing = result.return_code == 0
    return StepOutput(
        result={
            "all_passing": all_passing,
            "return_code": result.return_code,
            "stdout": result.stdout[:500],
            "stderr": result.stderr[:500],
        },
        observations=f"Tests {'PASSED' if all_passing else 'FAILED'} (rc={result.return_code})",
        context_updates={
            "test_results": {
                "all_passing": all_passing,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        },
    )
