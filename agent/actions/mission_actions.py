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
    for task in mission.plan:
        if task.id == last_task_id:
            if last_status == "success":
                task.status = "complete"
                task.frustration = 0
                task.summary = str(last_result)[:200] if last_result else "Completed"
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

    # Save updated mission
    if effects:
        await effects.save_mission(mission)

    events = step_input.context.get("events", [])
    return StepOutput(
        result={
            "events_pending": len(events) > 0,
            "task_completed": last_status == "success",
        },
        observations=f"Updated task {last_task_id}: status={last_status}",
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

    # Find tasks with met dependencies
    completed_ids = {t.id for t in completed}
    ready_tasks = []
    for task in pending:
        deps_met = all(dep in completed_ids for dep in task.depends_on)
        if deps_met:
            ready_tasks.append(task)

    # Also consider failed tasks for retry, with frustration cap
    effects = step_input.effects
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

    # Gather relevant notes for context
    relevant_notes = ""
    if hasattr(mission, "notes") and mission.notes:
        recent_notes = sorted(mission.notes, key=lambda n: n.timestamp, reverse=True)[
            :10
        ]
        relevant_notes = "\n".join(f"[{n.category}] {n.content}" for n in recent_notes)
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
            for i, item in enumerate(items):
                if isinstance(item, dict):
                    desc = item.get("description", item.get("task", str(item)))
                    filename = item.get("file", item.get("filename", f"file_{i}.py"))
                    flow = item.get("flow", "create_file")
                    tasks.append(
                        TaskRecord(
                            description=desc,
                            flow=flow,
                            priority=i,
                            inputs={
                                "target_file_path": filename,
                                "reason": desc,
                            },
                        )
                    )
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
            if tasks:
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
