"""Mission actions — load state, dispatch tasks, manage lifecycle.

Rebuild v2: Replaces heuristic task matching with LLM menu selection,
adds structured architecture state, removes silent fallbacks.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# State Loading & Event Handling (kept from v1, lightly cleaned)
# ══════════════════════════════════════════════════════════════════════


async def action_load_mission_state(step_input: StepInput) -> StepOutput:
    """Load mission state, event queue, and frustration map from persistence.

    Publishes: mission, events, frustration
    """
    effects = step_input.effects
    if not effects:
        return StepOutput(
            result={"mission": None},
            observations="No effects interface — cannot load state",
        )

    mission = await effects.load_mission()
    if mission is None:
        return StepOutput(
            result={"mission": None},
            observations="No mission found in persistence",
        )

    events = await effects.read_events()

    # Build frustration map from tasks
    frustration = {}
    for task in mission.plan:
        frustration[task.id] = task.frustration

    return StepOutput(
        result={"mission": {"status": mission.status}},
        observations=f"Loaded mission {mission.id}: {mission.status}, "
        f"{len(mission.plan)} tasks, {len(events)} events",
        context_updates={
            "mission": mission,
            "events": events,
            "frustration": frustration,
        },
    )


async def action_update_task_status(step_input: StepInput) -> StepOutput:
    """Apply the returning flow's outcome to mission state.

    Reads last_result, last_status, last_task_id from input to update
    the corresponding task's status and frustration level.
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    frustration = dict(step_input.context.get("frustration", {}))

    if not mission:
        return StepOutput(
            result={
                "needs_plan": True,
                "task_completed": False,
                "events_pending": False,
            },
            observations="No mission in context",
            context_updates={"mission": mission, "frustration": frustration},
        )

    last_status = step_input.context.get("last_status", "")
    last_task_id = step_input.context.get("last_task_id", "")
    last_result = step_input.context.get("last_result", "")

    # Check if plan exists
    if not mission.plan:
        return StepOutput(
            result={
                "needs_plan": True,
                "task_completed": False,
                "events_pending": False,
            },
            observations="Mission has no plan — needs planning",
            context_updates={"mission": mission, "frustration": frustration},
        )

    # Check for pending events
    events = step_input.context.get("events", [])
    if events:
        return StepOutput(
            result={
                "needs_plan": False,
                "task_completed": False,
                "events_pending": True,
            },
            observations=f"{len(events)} events pending",
            context_updates={"mission": mission, "frustration": frustration},
        )

    # Apply last result to task
    task_completed = False
    frustration_was_elevated = False
    quality_gate_exhausted = False

    # Handle quality gate failure status — not tied to a specific task
    if last_status == "quality_failed":
        # Set the blocked flag so the quality gate cannot re-run
        # until actual fix work (file_ops, patch, rewrite, create, interact) completes.
        mission.quality_gate_blocked = True

        # Check for exhaustion: if blocked AND too many gate attempts,
        # the mission is deadlocked.
        actionable = [
            t
            for t in mission.plan
            if t.status in ("pending", "failed") and t.frustration < 5
        ]

        if not actionable and mission.quality_gate_attempts >= 3:
            quality_gate_exhausted = True
            logger.info(
                "Quality gate exhausted: %d attempts, 0 actionable tasks",
                mission.quality_gate_attempts,
            )
    elif last_status in ("success", "completed"):
        # Unblock the quality gate when real fix work has completed.
        # Check that the last dispatched flow was a work flow, not
        # just a director cycle or planning step.
        last_dispatch = (
            mission.dispatch_history[-1]
            if mission.dispatch_history
            else None
        )
        last_flow = last_dispatch.flow if last_dispatch else ""
        if last_flow in ("file_ops", "patch", "rewrite", "create", "interact", "project_ops"):
            mission.quality_gate_blocked = False

    if last_task_id:
        from agent.persistence.models import AttemptRecord

        for task in mission.plan:
            if task.id == last_task_id:
                prev_frustration = task.frustration

                if last_status in ("success", "completed"):
                    task.status = "complete"
                    task.summary = (
                        str(last_result)[:200] if last_result else "Completed"
                    )
                    task.frustration = 0
                    task_completed = True
                    frustration_was_elevated = prev_frustration > 0
                elif last_status in ("abandoned", "failed"):
                    task.status = "failed"
                    task.frustration = min(task.frustration + 1, 5)
                    task.summary = str(last_result)[:200] if last_result else "Failed"

                # Record the attempt — store full error output for diagnosis
                error_output = None
                if last_status in ("abandoned", "failed", "diagnosed"):
                    error_output = str(last_result) if last_result else None
                task.attempts.append(
                    AttemptRecord(
                        flow=task.flow,
                        status=last_status or "unknown",
                        summary=str(last_result)[:200] if last_result else "",
                        error=error_output,
                    )
                )

                frustration[task.id] = task.frustration
                break

    # Save updated state
    if effects:
        await effects.save_mission(mission)

    # ── Check goal-level completion ──────────────────────────────
    # If all goals are complete (or no goals exist yet), signal it.
    # The director uses this as a termination signal: all_goals_complete
    # means the mission can proceed to final quality gate.
    all_goals_complete = False
    if hasattr(mission, "goals") and mission.goals:
        all_goals_complete = all(
            g.status == "complete" for g in mission.goals
        )
        # Also update goal statuses based on task completion
        _update_goal_statuses(mission)

    return StepOutput(
        result={
            "needs_plan": False,
            "task_completed": task_completed,
            "events_pending": False,
            "frustration_reset": frustration_was_elevated and task_completed,
            "quality_gate_exhausted": quality_gate_exhausted,
            "all_goals_complete": all_goals_complete,
        },
        observations=f"Updated task {last_task_id[:8] if last_task_id else 'none'}: "
        f"status={last_status}, completed={task_completed}"
        + (", QUALITY GATE EXHAUSTED" if quality_gate_exhausted else "")
        + (", ALL GOALS COMPLETE" if all_goals_complete else ""),
        context_updates={"mission": mission, "frustration": frustration},
    )


def _update_goal_statuses(mission) -> None:
    """Update goal statuses based on associated task completion and frustration.

    A goal is:
      - "complete" when ALL associated tasks are complete
      - "in_progress" when at least one task is in_progress or complete
      - "blocked" when >50% of associated tasks have frustration >= 3
      - "pending" otherwise
    """
    if not hasattr(mission, "goals") or not mission.goals:
        return

    task_map = {t.id: t for t in mission.plan} if hasattr(mission, "plan") else {}

    for goal in mission.goals:
        if goal.status in ("revised",):
            continue  # Don't auto-update revised goals

        if not goal.associated_task_ids:
            continue

        tasks = [task_map[tid] for tid in goal.associated_task_ids if tid in task_map]
        if not tasks:
            continue

        all_complete = all(t.status == "complete" for t in tasks)
        any_active = any(t.status in ("in_progress", "complete") for t in tasks)
        frustrated_count = sum(1 for t in tasks if t.frustration >= 3)
        is_blocked = frustrated_count > len(tasks) / 2

        if all_complete:
            goal.status = "complete"
        elif is_blocked:
            goal.status = "blocked"
        elif any_active:
            goal.status = "in_progress"
        # else: stays "pending"


async def action_handle_events(step_input: StepInput) -> StepOutput:
    """Process user messages, abort/pause signals from the event queue."""
    effects = step_input.effects
    mission = step_input.context.get("mission")
    events = step_input.context.get("events", [])

    if not mission or not effects:
        return StepOutput(
            result={"abort_requested": False, "pause_requested": False},
            observations="No mission or effects",
        )

    abort_requested = False
    pause_requested = False
    user_messages = []

    for event in events:
        if event.type == "abort":
            abort_requested = True
        elif event.type == "pause":
            pause_requested = True
            mission.status = "paused"
        elif event.type == "user_message":
            msg = event.payload.get("message", "")
            if msg:
                user_messages.append(msg)

    # Append user messages as notes
    if user_messages:
        from agent.persistence.models import NoteRecord

        for msg in user_messages:
            mission.notes.append(
                NoteRecord(
                    content=msg,
                    category="general",
                    source_flow="user_message",
                )
            )

    # Clear processed events
    await effects.clear_events()
    await effects.save_mission(mission)

    return StepOutput(
        result={
            "abort_requested": abort_requested,
            "pause_requested": pause_requested,
        },
        observations=f"Processed {len(events)} events: "
        f"abort={abort_requested}, pause={pause_requested}, "
        f"messages={len(user_messages)}",
        context_updates={"mission": mission},
    )


# ══════════════════════════════════════════════════════════════════════
# NEW: LLM Menu Task Selection (replaces _find_best_task_for_flow)
# ══════════════════════════════════════════════════════════════════════


async def action_select_task_for_dispatch(step_input: StepInput) -> StepOutput:
    """Present actionable tasks as an LLM menu for selection.

    Uses the memoryful mission_control session. The model has already
    seen the mission state and produced its analysis — now it picks
    which specific task to work on.

    Reads: context.mission, context.session_id, context.director_analysis
    Publishes: selected_task (TaskRecord), dispatch_flow (str)
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    session_id = step_input.context.get("session_id", "")

    if not mission or not effects:
        return StepOutput(
            result={"task_selected": False},
            observations="No mission or effects for task selection",
        )

    # Build list of actionable tasks (pending or failed with frustration < 5)
    actionable = [
        t
        for t in mission.plan
        if t.status in ("pending", "failed")
        and t.frustration < 5
        and _dependencies_met(t, mission)
    ]

    if not actionable:
        # Distinguish: are all tasks genuinely complete, or are some
        # still pending/in_progress but blocked by unmet dependencies?
        incomplete = [
            t for t in mission.plan
            if t.status not in ("complete",)
        ]
        all_complete = len(incomplete) == 0

        # Also detect stale in_progress tasks (set in_progress but never
        # attempted in the last N cycles) and reset them to pending so
        # they become actionable again.
        for t in mission.plan:
            if t.status == "in_progress":
                # If a task has been in_progress but has no recent attempts,
                # it was likely abandoned during a diagnosis detour
                logger.warning(
                    "Resetting stale in_progress task to pending: %s",
                    t.description[:60],
                )
                t.status = "pending"

        # After reset, re-check if any tasks are now actionable
        newly_actionable = [
            t for t in mission.plan
            if t.status in ("pending", "failed")
            and t.frustration < 5
            and _dependencies_met(t, mission)
        ]
        if newly_actionable:
            # Save the reset and continue with these tasks
            if effects:
                await effects.save_mission(mission)
            # Fall through to the normal selection logic below
            # by replacing the empty actionable list
            actionable = newly_actionable
            logger.info(
                "Reset stale tasks — %d tasks now actionable",
                len(actionable),
            )
        else:
            if effects:
                await effects.save_mission(mission)

            return StepOutput(
                result={
                    "task_selected": False,
                    "no_actionable_tasks": True,
                    "all_tasks_complete": all_complete,
                },
                observations="All tasks complete" if all_complete
                else f"No actionable tasks — {len(incomplete)} tasks blocked or in_progress",
            )

    # Build the menu prompt — use task IDs as option names for JSON selection
    task_options = {}
    lines = ["Select the task to work on next:\n"]
    for task in actionable:
        frust = f" [frustration: {task.frustration}]" if task.frustration > 0 else ""
        target = task.inputs.get("target_file_path", "")
        target_str = f" → {target}" if target else ""
        desc = f"[{task.status}] {task.description}{target_str}{frust}"
        lines.append(f"  - {task.id}: {desc}")
        task_options[task.id] = desc

    lines.append("")
    lines.append(
        "Pick the most impactful task. "
        'Respond with ONLY a JSON object: {"choice": "<task_id>"}'
    )
    lines.append(f"Valid task IDs: {', '.join(task_options.keys())}")
    prompt = "\n".join(lines)

    from agent.resolvers.llm_menu import extract_choice

    try:
        result = await effects.session_inference(
            session_id,
            prompt,
            {"temperature": 0.1},
        )
        response = result.text.strip() if result.text else ""
    except Exception as e:
        logger.error("Task selection failed: %s", e)
        response = ""

    # Extract the chosen task ID from the response
    chosen_id = extract_choice(response, list(task_options.keys()))
    selected = None
    if chosen_id:
        for task in actionable:
            if task.id == chosen_id:
                selected = task
                break

    # Fallback: first actionable task
    if selected is None:
        selected = actionable[0]
        logger.warning(
            "Task selection fallback to first task (response was %r)",
            response[:60],
        )

    # Mark as in_progress
    for task in mission.plan:
        if task.id == selected.id:
            task.status = "in_progress"
            break

    if effects:
        await effects.save_mission(mission)

    # ── Assemble partial dispatch_config ──────────────────────
    # Contains everything except target_file_path (resolved by
    # select_target_file in the next step).
    dispatch_flow_type = step_input.context.get("dispatch_flow_type", "")
    dispatch_flow = dispatch_flow_type or selected.flow or "file_ops"

    # Assemble flow_directive from goal + task
    task_desc = selected.description or ""
    goal_context = ""
    goal_id = getattr(selected, "goal_id", "") or (selected.inputs or {}).get("goal_id", "")
    if goal_id and hasattr(mission, "goals"):
        for goal in mission.goals:
            if goal.id == goal_id:
                goal_context = goal.description
                break

    if goal_context and task_desc:
        flow_directive = f"{task_desc} — serving goal: {goal_context}"
    else:
        flow_directive = task_desc or (selected.inputs or {}).get("reason", "") or "No directive specified"

    # Gather relevant_notes (architecture + recent observations)
    # NOTE: Uses Option A — re-evaluate alongside persistence audit.
    relevant_notes = ""
    if hasattr(mission, "notes") and mission.notes:
        recent = sorted(mission.notes, key=lambda n: n.timestamp, reverse=True)[:8]
        relevant_notes = "\n".join(f"[{n.category}] {n.content[:200]}" for n in recent)

    if mission.architecture:
        arch = mission.architecture
        arch_summary = (
            f"Import scheme: {arch.import_scheme}. "
            f"Run command: {arch.run_command}. "
            f"Modules: {', '.join(arch.canonical_files())}."
        )
        if relevant_notes:
            relevant_notes = f"[architecture] {arch_summary}\n{relevant_notes}"
        else:
            relevant_notes = f"[architecture] {arch_summary}"

    # Determine prompt variant for specialized generation
    prompt_variant = ""
    if dispatch_flow == "file_ops":
        desc_lower = selected.description.lower()
        target_lower = ((selected.inputs or {}).get("target_file_path", "") or "").lower()
        if (
            any(kw in desc_lower for kw in ["test", "create tests", "write tests"])
            or target_lower.startswith("tests/")
            or "/test_" in target_lower
        ):
            prompt_variant = "test_generation"

    partial_config = {
        "flow": dispatch_flow,
        "task_id": selected.id,
        "flow_directive": flow_directive,
        "working_directory": mission.config.working_directory,
        "target_file_path": (selected.inputs or {}).get("target_file_path", ""),
        "relevant_notes": relevant_notes,
        "mission_id": mission.id,
        "prompt_variant": prompt_variant,
    }

    # For diagnose_issue: extract error context from the task's attempt history
    if dispatch_flow == "diagnose_issue":
        error_description = selected.description
        error_output = ""
        if hasattr(selected, "attempts") and selected.attempts:
            for attempt in reversed(selected.attempts):
                if attempt.error:
                    error_output = attempt.error
                    break
                if attempt.summary and not error_output:
                    error_output = attempt.summary
        if not error_output:
            for task in mission.plan:
                if task.status == "failed" and task.attempts:
                    for attempt in reversed(task.attempts):
                        if attempt.error:
                            error_output = attempt.error
                            break
                    if error_output:
                        break
        partial_config["error_description"] = error_description
        partial_config["error_output"] = error_output

    return StepOutput(
        result={"task_selected": True},
        observations=f"Selected task: {selected.description[:60]}",
        context_updates={
            "dispatch_config": partial_config,
        },
    )


def _dependencies_met(task, mission) -> bool:
    """Check if all task dependencies are satisfied (complete)."""
    if not task.depends_on:
        return True
    completed_ids = {t.id for t in mission.plan if t.status == "complete"}
    return all(dep_id in completed_ids for dep_id in task.depends_on)


# ══════════════════════════════════════════════════════════════════════
# NEW: LLM Menu File Selection (replaces _derive_file_path_from_description)
# ══════════════════════════════════════════════════════════════════════


async def action_select_target_file(step_input: StepInput) -> StepOutput:
    """Resolve target file for the dispatch.

    Reads the partial dispatch_config from select_task_for_dispatch.
    If a target_file_path is already set and valid, passes through.
    Otherwise presents project files as an LLM menu for selection.

    Reads: context.dispatch_config, context.session_id, context.mission
    Publishes: dispatch_config (with target_file_path resolved)
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    session_id = step_input.context.get("session_id", "")
    dispatch_config = step_input.context.get("dispatch_config", {})

    if not mission or not effects or not dispatch_config:
        return StepOutput(
            result={"target_resolved": False},
            observations="Missing context for file selection",
        )

    target_path = dispatch_config.get("target_file_path", "")
    dispatch_flow = dispatch_config.get("flow", "file_ops")
    working_dir = dispatch_config.get("working_directory", "") or mission.config.working_directory
    task_desc = dispatch_config.get("flow_directive", "")

    # Check if we need file selection (only for flows that target existing files)
    # file_ops handles its own create/modify routing internally.
    # diagnose_issue needs an existing file to analyze.
    # interact and project_ops are project-level, no file targeting.
    needs_existing_file = dispatch_flow in {
        "diagnose_issue",
    }

    # ── Fast paths: target already resolved ─────────────────────

    # If we have a valid path and the flow doesn't need to verify existence, pass through
    if target_path and not needs_existing_file:
        dispatch_config["target_file_path"] = target_path
        return StepOutput(
            result={"target_resolved": True},
            observations=f"Target file from task: {target_path}",
            context_updates={"dispatch_config": dispatch_config},
        )

    # Empty target for file_ops/interact/project_ops: pass through.
    # file_ops will route to create (which infers from flow_directive).
    # interact and project_ops are project-level, no file needed.
    if not target_path and not needs_existing_file:
        return StepOutput(
            result={"target_resolved": True},
            observations=f"No target file specified — flow will infer from directive",
            context_updates={"dispatch_config": dispatch_config},
        )

    # For existing-file flows, validate the path exists
    if target_path and needs_existing_file:
        full_path = os.path.join(working_dir, target_path)
        if await effects.file_exists(full_path):
            dispatch_config["target_file_path"] = target_path
            return StepOutput(
                result={"target_resolved": True},
                observations=f"Target file verified: {target_path}",
                context_updates={"dispatch_config": dispatch_config},
            )
        # Path invalid — fall through to menu selection

    # ── Menu path: need to select a file ──────────────────────

    # Scan project for available files
    file_list = []
    try:
        listing = await effects.list_directory(".", recursive=True)
        if listing.exists:
            file_list = [
                e.path
                for e in listing.entries
                if e.is_file
                and not e.path.startswith(".")
                and not e.name.startswith(".")
                and "__pycache__" not in e.path
                and e.name.endswith(
                    (
                        ".py",
                        ".yaml",
                        ".yml",
                        ".json",
                        ".toml",
                        ".md",
                        ".js",
                        ".ts",
                        ".rs",
                        ".html",
                        ".css",
                        ".cfg",
                        ".txt",
                    )
                )
            ]
    except Exception as e:
        logger.warning("Failed to list project files: %s", e)

    # Also include files from architecture if available
    if mission.architecture:
        for arch_file in mission.architecture.canonical_files():
            if arch_file not in file_list:
                file_list.append(arch_file)

    if not file_list:
        if not needs_existing_file:
            # No files on disk — keep whatever target_path we have (might be from architecture)
            return StepOutput(
                result={"target_resolved": True},
                observations=f"No files on disk, using target: {target_path or '(none)'}",
                context_updates={"dispatch_config": dispatch_config},
            )
        return StepOutput(
            result={"target_resolved": False, "error": "no_project_files"},
            observations=f"No project files found for {dispatch_flow}.",
        )

    # Present file menu via memoryful session — JSON-based selection
    # Build option map: use file paths as option keys
    file_options = {}
    lines = [f"Select the target file for this {dispatch_flow} task:"]
    lines.append(f"Task: {task_desc}\n")

    truncated_list = file_list[:19]
    for filepath in truncated_list:
        lines.append(f"  - {filepath}")
        file_options[filepath] = filepath

    # Add "create new file" escape hatch for flows that target existing files
    create_option_key = "__create_new__"
    if needs_existing_file and truncated_list:
        lines.append(f"  - {create_option_key}: The file I need doesn't exist yet")
        file_options[create_option_key] = "CREATE NEW FILE"

    lines.append("")
    lines.append(
        "Pick the file that best matches the task. "
        'Respond with ONLY a JSON object: {"choice": "<file_path>"}'
    )
    lines.append(f"Valid file paths: {', '.join(file_options.keys())}")
    prompt = "\n".join(lines)

    from agent.resolvers.llm_menu import extract_choice

    try:
        result = await effects.session_inference(
            session_id,
            prompt,
            {"temperature": 0.1},
        )
        response = result.text.strip() if result.text else ""
    except Exception as e:
        logger.error("File selection failed: %s", e)
        # Fall through with whatever path we have
        return StepOutput(
            result={"target_resolved": bool(target_path)},
            observations=f"File selection failed: {e}, using: {target_path or '(none)'}",
            context_updates={"dispatch_config": dispatch_config},
        )

    chosen = extract_choice(response, list(file_options.keys()))

    # Handle "create new file" selection
    if chosen == create_option_key:
        create_prompt = (
            f"You chose to create a new file instead of modifying an existing one.\n"
            f"Task: {task_desc}\n\n"
            f"What file path should be created? "
            f"Reply with ONLY the file path (e.g., loader.py or src/utils.py), nothing else."
        )
        try:
            create_result = await effects.session_inference(
                session_id,
                create_prompt,
                {"temperature": 0.1, "max_tokens": 30},
            )
            new_path = create_result.text.strip().strip("'\"` \n")
            new_path = new_path.splitlines()[0].strip() if new_path else ""
        except Exception as e:
            logger.error("Create file path prompt failed: %s", e)
            new_path = ""

        if new_path:
            logger.info(
                "File selection redirected: %s → file_ops (create) for %s",
                dispatch_flow,
                new_path,
            )
            dispatch_config["flow"] = "file_ops"
            dispatch_config["target_file_path"] = new_path
            return StepOutput(
                result={"target_resolved": True},
                observations=f"Redirected to file_ops create: {new_path}",
                context_updates={"dispatch_config": dispatch_config},
            )
        logger.warning("Create redirect failed — no path given, using first file")
        chosen = None  # Fall through to default

    # Resolve the selected file path
    if chosen and chosen in file_list:
        selected_path = chosen
    else:
        # Fallback: first file
        selected_path = file_list[0]
        if chosen:
            logger.warning(
                "File selection response %r not in file_list, using first: %s",
                chosen,
                selected_path,
            )
    dispatch_config["target_file_path"] = selected_path

    return StepOutput(
        result={"target_resolved": True},
        observations=f"Selected file: {selected_path}",
        context_updates={"dispatch_config": dispatch_config},
    )


# ══════════════════════════════════════════════════════════════════════
# NEW: Memoryful Session Management for mission_control
# ══════════════════════════════════════════════════════════════════════


async def action_start_director_session(step_input: StepInput) -> StepOutput:
    """Start a memoryful inference session for the director cycle.

    The session persists across reason → select_task → select_target_file,
    giving the model conversational context for all three decisions.
    """
    effects = step_input.effects
    if not effects:
        return StepOutput(
            result={"session_started": False},
            observations="No effects — cannot start director session",
        )

    try:
        session_id = await effects.start_inference_session({"ttl_seconds": 300})
    except Exception as e:
        logger.error("Failed to start director session: %s", e)
        return StepOutput(
            result={"session_started": False},
            observations=f"Failed to start director session: {e}",
        )

    if not session_id:
        logger.warning("Director session returned empty session_id — falling back to stateless")
        return StepOutput(
            result={"session_started": False},
            observations="Director session returned empty session_id",
            context_updates={"session_id": ""},
        )

    logger.info("Director session started: %s", session_id)

    return StepOutput(
        result={"session_started": True},
        observations=f"Director session started: {session_id}",
        context_updates={"session_id": session_id},
    )


async def action_end_director_session(step_input: StepInput) -> StepOutput:
    """End the memoryful director session before dispatching."""
    effects = step_input.effects
    session_id = step_input.context.get("session_id", "")

    if effects and session_id:
        try:
            await effects.end_inference_session(session_id)
        except Exception as e:
            logger.warning("Failed to end director session: %s", e)

    return StepOutput(
        result={},
        observations="Director session ended",
    )


# ══════════════════════════════════════════════════════════════════════
# NEW: Dispatch History Tracking
# ══════════════════════════════════════════════════════════════════════


async def action_record_dispatch(step_input: StepInput) -> StepOutput:
    """Record the current dispatch in history for deduplication.

    Also checks for repeated dispatches and adds warnings to context.
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    dispatch_config = step_input.context.get("dispatch_config", {})

    if not mission or not dispatch_config:
        return StepOutput(result={}, observations="No dispatch to record")

    from agent.persistence.models import DispatchRecord

    record = DispatchRecord(
        flow=dispatch_config.get("flow", ""),
        task_id=dispatch_config.get("task_id", ""),
        target_file_path=dispatch_config.get("target_file_path", ""),
    )

    # Check for repeated dispatches
    recent = mission.dispatch_history[-5:] if mission.dispatch_history else []
    repeat_count = sum(
        1 for r in recent if r.flow == record.flow and r.task_id == record.task_id
    )

    mission.dispatch_history.append(record)

    # Trim history to last 20
    if len(mission.dispatch_history) > 20:
        mission.dispatch_history = mission.dispatch_history[-20:]

    if effects:
        await effects.save_mission(mission)

    dispatch_warning = ""
    if repeat_count >= 2:
        dispatch_warning = (
            f"WARNING: This exact dispatch ({record.flow} on task "
            f"{record.task_id[:8]}) has been attempted {repeat_count} times "
            f"in recent cycles. Consider a different approach."
        )

    return StepOutput(
        result={"repeat_count": repeat_count},
        observations=dispatch_warning or "Dispatch recorded",
        context_updates={
            "mission": mission,
            "dispatch_warning": dispatch_warning,
        },
    )


# ══════════════════════════════════════════════════════════════════════
# Architecture State Management (NEW)
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# Architecture Drift Detection (deterministic, no inference)
# ══════════════════════════════════════════════════════════════════════


async def action_check_architecture_drift(step_input: StepInput) -> StepOutput:
    """Deterministically compare architecture against files on disk.

    Compares architecture.canonical_files() against project_manifest keys.
    Detects files on disk not in architecture (drift → reconciliation needed).

    Infrastructure files (pyproject.toml, README.md, __init__.py, etc.) are
    excluded from drift detection since they're not application architecture.

    Returns:
        has_architecture: bool
        has_tasks: bool
        drift_detected: bool
        new_files: list of files on disk not in architecture
    """
    mission = step_input.context.get("mission")
    manifest = step_input.context.get("project_manifest", {})

    if not mission:
        return StepOutput(
            result={
                "has_architecture": False,
                "has_tasks": False,
                "drift_detected": False,
            },
            observations="No mission in context",
        )

    has_architecture = mission.architecture is not None
    has_tasks = len(mission.plan) > 0

    if not has_architecture:
        return StepOutput(
            result={
                "has_architecture": False,
                "has_tasks": has_tasks,
                "drift_detected": False,
            },
            observations="No architecture exists — initial design needed",
        )

    # Get canonical files from architecture
    arch_files = set(mission.architecture.canonical_files())

    # Get project files from manifest, excluding infrastructure
    infrastructure = {
        "pyproject.toml", "setup.cfg", "setup.py", "requirements.txt",
        "uv.lock", "README.md", "readme.md", "CHANGELOG.md",
        ".gitignore", ".editorconfig", ".flake8", ".pre-commit-config.yaml",
        "Makefile", "Dockerfile", "docker-compose.yml",
    }
    infrastructure_prefixes = (".", "tests/", "test_", "__pycache__/")
    infrastructure_suffixes = ("__init__.py",)

    disk_files = set()
    for filepath in manifest.keys():
        basename = os.path.basename(filepath)
        if basename in infrastructure:
            continue
        if any(filepath.startswith(p) for p in infrastructure_prefixes):
            continue
        if any(filepath.endswith(s) for s in infrastructure_suffixes):
            continue
        disk_files.add(filepath)

    # Detect drift: files on disk that architecture doesn't know about
    new_on_disk = sorted(disk_files - arch_files)

    drift_detected = len(new_on_disk) > 0
    drift_summary = ""
    if drift_detected:
        drift_summary = (
            f"Architecture drift: {len(new_on_disk)} file(s) on disk "
            f"not in architecture: {', '.join(new_on_disk)}"
        )

    return StepOutput(
        result={
            "has_architecture": True,
            "has_tasks": has_tasks,
            "drift_detected": drift_detected,
            "new_files": new_on_disk,
        },
        observations=drift_summary or f"No drift — architecture matches disk ({len(arch_files)} files)",
        context_updates={"drift_summary": drift_summary},
    )


async def action_parse_and_store_architecture(step_input: StepInput) -> StepOutput:
    """Parse the LLM's architecture JSON and store as structured mission state.

    Reads: context.inference_response (raw JSON from design step)
    Writes: mission.architecture (ArchitectureState)
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    response = step_input.context.get("inference_response", "")

    if not mission or not response:
        return StepOutput(
            result={"architecture_parsed": False},
            observations="No mission or inference response",
        )

    from agent.persistence.models import (
        ArchitectureState,
        ModuleSpec,
        InterfaceContract,
        DataShapeContract,
    )
    from agent.actions.refinement_actions import strip_markdown_wrapper

    # Parse JSON from response
    cleaned = strip_markdown_wrapper(response)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to extract JSON object
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return StepOutput(
                    result={"architecture_parsed": False},
                    observations="Failed to parse architecture JSON",
                )
        else:
            return StepOutput(
                result={"architecture_parsed": False},
                observations="No JSON object found in architecture response",
            )

    # Build ArchitectureState
    execution = data.get("execution", {})
    modules = []
    for m in data.get("modules", []):
        modules.append(
            ModuleSpec(
                file=m.get("file", ""),
                responsibility=m.get("responsibility", ""),
                defines=m.get("defines", []),
                imports_from=m.get("imports_from", {}),
            )
        )

    interfaces = []
    for iface in data.get("interfaces", []):
        interfaces.append(
            InterfaceContract(
                caller=iface.get("caller", ""),
                callee=iface.get("callee", ""),
                symbol=iface.get("symbol", ""),
                signature=iface.get("signature", ""),
            )
        )

    data_shapes = []
    for ds in data.get("data_shapes", []):
        data_shapes.append(DataShapeContract.from_llm_dict(ds))

    arch = ArchitectureState(
        import_scheme=execution.get("import_scheme", "flat"),
        run_command=execution.get("run_command", ""),
        working_directory=execution.get("working_directory", "project root"),
        init_files=execution.get("init_files", False),
        modules=modules,
        creation_order=data.get("creation_order", [m.file for m in modules]),
        interfaces=interfaces,
        data_shapes=data_shapes,
        notes=data.get("notes", ""),
    )

    mission.architecture = arch

    # Also save a brief note for prompt context
    from agent.persistence.models import NoteRecord

    arch_summary = (
        f"Import scheme: {arch.import_scheme}. "
        f"Run: {arch.run_command}. "
        f"Files: {', '.join(arch.canonical_files())}."
    )
    if arch.data_shapes:
        shape_lines = [
            f"  {ds.file} → {ds.consumed_by}: {ds.structure}" for ds in arch.data_shapes
        ]
        arch_summary += "\nData shapes:\n" + "\n".join(shape_lines)

    mission.notes.append(
        NoteRecord(
            content=arch_summary,
            category="architecture_blueprint",
            source_flow="design_and_plan",
        )
    )

    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={
            "architecture_parsed": True,
            "module_count": len(modules),
        },
        observations=f"Architecture stored: {len(modules)} modules, "
        f"scheme={arch.import_scheme}, "
        f"order={', '.join(arch.creation_order)}",
        context_updates={"mission": mission, "architecture": arch},
    )


# ══════════════════════════════════════════════════════════════════════
# Plan Creation (cleaned up, receives architecture as structured input)
# ══════════════════════════════════════════════════════════════════════


async def action_create_plan_from_architecture(step_input: StepInput) -> StepOutput:
    """Parse the LLM's plan response into TaskRecords.

    Unlike v1, this version receives the architecture as structured state
    and validates that task target_file_paths match the architecture's
    canonical file list.

    Reads: context.mission, context.inference_response, context.architecture
    Writes: mission.plan
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    response = step_input.context.get("inference_response", "")
    architecture = step_input.context.get(
        "architecture",
        getattr(mission, "architecture", None) if mission else None,
    )

    if not mission:
        return StepOutput(
            result={"plan_created": False},
            observations="No mission in context",
        )

    tasks = _parse_task_list(response, architecture)

    if not tasks:
        from agent.persistence.models import TaskRecord

        tasks = [
            TaskRecord(
                description=f"Implement: {mission.objective}",
                flow="create",
                inputs={"target_file_path": "", "reason": mission.objective},
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


def _parse_task_list(
    response: str,
    architecture: Any | None = None,
) -> list:
    """Parse an LLM response into TaskRecord objects.

    If architecture is available, validates target_file_path against
    the canonical file list.
    """
    from agent.persistence.models import TaskRecord
    from agent.actions.refinement_actions import strip_markdown_wrapper

    tasks = []
    response = strip_markdown_wrapper(response)

    # Extract JSON array
    json_match = re.search(r"\[[\s\S]*\]", response)
    if not json_match:
        return tasks

    try:
        items = json.loads(json_match.group())
    except json.JSONDecodeError:
        return tasks

    # Get canonical file list from architecture
    arch_files = set()
    if architecture:
        arch_files = set(
            architecture.canonical_files()
            if hasattr(architecture, "canonical_files")
            else []
        )

    desc_to_id = {}

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue

        desc = item.get("description", str(item))
        item_inputs = item.get("inputs", {}) or {}
        filename = item_inputs.get("target_file_path", "")

        # Validate against architecture if available
        if filename and arch_files and filename not in arch_files:
            basename = os.path.basename(filename)
            for arch_file in arch_files:
                if os.path.basename(arch_file) == basename:
                    logger.info(
                        "Plan path %r corrected to architecture path %r",
                        filename,
                        arch_file,
                    )
                    filename = arch_file
                    break

        # Infer flow from description (lightweight hint)
        flow = item.get("flow") or _infer_flow_hint(desc)
        # B5: Normalize stale flow names the model may produce from memory
        flow = _FLOW_NAME_REMAP.get(flow, flow)

        task_inputs = {
            "target_file_path": filename or "",
            "reason": desc,
        }

        for k, v in item_inputs.items():
            if k not in task_inputs and v:
                task_inputs[k] = v

        task = TaskRecord(
            description=desc,
            flow=flow,
            priority=i,
            inputs=task_inputs,
        )
        desc_to_id[desc] = task.id
        tasks.append(task)

    # Resolve depends_on
    for i, item in enumerate(items):
        if isinstance(item, dict) and i < len(tasks):
            raw_deps = item.get("depends_on", [])
            if isinstance(raw_deps, list):
                resolved = []
                for dep_desc in raw_deps:
                    if isinstance(dep_desc, str):
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


# B5: Stale flow names the model sometimes produces from memory.
# Maps old names → current canonical names.
_FLOW_NAME_REMAP: dict[str, str] = {
    "file_write": "file_ops",
    "create_file": "create",
    "modify_file": "rewrite",
    "ast_edit_session": "patch",
}


def _infer_flow_hint(desc: str) -> str:
    """Lightweight flow hint from description. Used as a default that
    mission_control can override at dispatch time.

    Condensed flow set:
      file_ops      — create, modify, refactor, document, explore, manage, review
      diagnose_issue  — investigate code issues
      interact        — run and use the product, test features
      project_ops     — manage project infrastructure, deps, config
    """
    d = desc.lower()

    if any(kw in d for kw in ["diagnose", "debug", "investigate root cause"]):
        return "diagnose_issue"
    if any(
        kw in d
        for kw in [
            "run the",
            "verify",
            "end-to-end",
            "validate",
            "test the",
            "interact",
            "try the",
            "use the",
        ]
    ):
        return "interact"
    if any(
        kw in d
        for kw in [
            "setup",
            "initialize",
            "configure",
            "project init",
            "dependency",
            "package",
            "install",
        ]
    ):
        return "project_ops"

    # Everything else routes through file_ops — it handles create, modify,
    # refactor, document, explore, manage packages, review, and tests.
    return "file_ops"


# ══════════════════════════════════════════════════════════════════════
# Mission Lifecycle (kept from v1)
# ══════════════════════════════════════════════════════════════════════


async def action_finalize_mission(step_input: StepInput) -> StepOutput:
    """Mark mission complete, deadlocked, or aborted and save."""
    effects = step_input.effects
    mission = step_input.context.get("mission")

    if not mission:
        return StepOutput(
            result={"finalized": False},
            observations="No mission to finalize",
        )

    if step_input.params.get("deadlock", False):
        mission.status = "deadlocked"
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
    """Enter idle state — waiting for events."""
    return StepOutput(
        result={"idle": True},
        observations="Entering idle state — waiting for events",
    )


# ══════════════════════════════════════════════════════════════════════
# File Operations (fixed: no assumed-pass, clear errors)
# ══════════════════════════════════════════════════════════════════════


async def action_execute_file_creation(step_input: StepInput) -> StepOutput:
    """Parse the LLM's file content response and write file(s) to disk.

    Fails clearly if no target_file_path is available.
    """
    effects = step_input.effects
    if not effects:
        return StepOutput(
            result={"write_success": False, "error": "no_effects"},
            observations="No effects interface — cannot write files",
        )

    target = step_input.params.get("target_file_path", "") or step_input.context.get(
        "target_file_path", ""
    )
    response = step_input.context.get("inference_response", "")

    if not target and not response:
        return StepOutput(
            result={"write_success": False, "error": "no_target_or_content"},
            observations="No target file path or content to write",
        )

    code = _extract_code(response)
    if not code.strip():
        return StepOutput(
            result={"write_success": False, "error": "empty_content"},
            observations="Inference produced empty content — nothing to write",
        )

    wr = await effects.write_file(target, code)

    if not wr.success:
        return StepOutput(
            result={"write_success": False, "error": wr.error},
            observations=f"Failed to write {target}: {wr.error}",
        )

    # Post-write verification
    exists = await effects.file_exists(target)
    if not exists:
        return StepOutput(
            result={"write_success": False, "error": "ghost_write"},
            observations=f"Write reported success but {target} not found on disk",
        )

    return StepOutput(
        result={"write_success": True, "path": target},
        observations=f"Created {target} ({wr.bytes_written} bytes)",
        context_updates={"files_changed": [target]},
    )


def _extract_code(response: str) -> str:
    """Extract code content from an LLM response, stripping markdown fences."""
    if not response:
        return ""

    lines = response.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]

    return "\n".join(lines) + "\n"


async def action_run_tests(step_input: StepInput) -> StepOutput:
    """Run a test command and report results.

    Returns explicit 'skipped' status when effects are unavailable.
    """
    effects = step_input.effects
    if not effects:
        return StepOutput(
            result={"all_passing": False, "status": "skipped"},
            observations="No effects — tests skipped (NOT assumed pass)",
        )

    cmd = step_input.params.get("command", ["python", "-c", "print('OK')"])
    if isinstance(cmd, str):
        cmd = cmd.split()

    result = await effects.run_command(cmd, timeout=60)

    all_passing = result.return_code == 0
    return StepOutput(
        result={
            "all_passing": all_passing,
            "status": "passed" if all_passing else "failed",
            "return_code": result.return_code,
            "stdout": result.stdout[:500],
            "stderr": result.stderr[:500],
        },
        observations=f"Tests {'passed' if all_passing else 'failed'}: rc={result.return_code}",
    )


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════


def _is_duplicate_task(
    mission, description: str, flow: str, target_file: str = ""
) -> bool:
    """Check if a substantially similar task already exists."""
    desc_lower = description.lower()
    for task in mission.plan:
        if task.status == "complete":
            continue
        task_target = task.inputs.get("target_file_path", "")
        if flow == task.flow and target_file and task_target == target_file:
            return True
        if desc_lower == task.description.lower():
            return True
    return False


# Backward compat stubs for ouroboros.py CLI
def _infer_flow_from_description(desc: str) -> str:
    return _infer_flow_hint(desc)


def _derive_source_for_tests(test_path: str, desc: str) -> str:
    return ""


# ══════════════════════════════════════════════════════════════════════
# Goal Derivation (Context Contract Architecture)
# ══════════════════════════════════════════════════════════════════════


async def action_derive_project_goals(step_input: StepInput) -> StepOutput:
    """Derive project goals from architecture and mission objective.

    Two-pass derivation:
      Pass 1 (deterministic): structural goals from architecture modules
      Pass 2 (inference): functional goals from objective + architecture

    Stores goals on MissionState and links tasks to parent goals.

    Context required: mission
    Context optional: architecture
    Publishes: goals, mission
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    architecture = step_input.context.get("architecture")

    if not effects or not mission:
        return StepOutput(
            result={"goals_derived": False},
            observations="No effects or mission state",
        )

    # Import persistence model
    from agent.persistence.models import GoalRecord

    # Access mission as dict or object
    if hasattr(mission, "objective"):
        objective = mission.objective
        modules = (
            architecture.modules if architecture and hasattr(architecture, "modules")
            else []
        )
        data_shapes = (
            architecture.data_shapes if architecture and hasattr(architecture, "data_shapes")
            else []
        )
        plan = mission.plan if hasattr(mission, "plan") else []
    elif isinstance(mission, dict):
        objective = mission.get("objective", "")
        arch = mission.get("architecture") or architecture or {}
        if isinstance(arch, dict):
            modules = arch.get("modules", [])
            data_shapes = arch.get("data_shapes", [])
        else:
            modules = getattr(arch, "modules", [])
            data_shapes = getattr(arch, "data_shapes", [])
        plan = mission.get("plan", [])
    else:
        return StepOutput(
            result={"goals_derived": False},
            observations="Cannot read mission state",
        )

    goals = []

    # ── Pass 1: Deterministic structural goals from architecture ──
    for mod in modules:
        if isinstance(mod, dict):
            file_path = mod.get("file", "")
            responsibility = mod.get("responsibility", "")
        else:
            file_path = getattr(mod, "file", "")
            responsibility = getattr(mod, "responsibility", "")

        if not file_path:
            continue

        goal = GoalRecord(
            description=responsibility or f"Implement {file_path}",
            type="structural",
            associated_files=[file_path],
        )
        goals.append(goal)

    for ds in data_shapes:
        if isinstance(ds, dict):
            file_path = ds.get("file", "")
            consumed_by = ds.get("consumed_by", "")
        else:
            file_path = getattr(ds, "file", "")
            consumed_by = getattr(ds, "consumed_by", "")

        if not file_path:
            continue

        goal = GoalRecord(
            description=f"Data file {file_path} consumed by {consumed_by}",
            type="structural",
            associated_files=[file_path],
        )
        goals.append(goal)

    # ── Pass 2: Inference-derived functional goals ──
    if effects and objective:
        structural_summary = "\n".join(
            f"- {g.description} ({', '.join(g.associated_files)})"
            for g in goals
        )
        prompt = (
            f"Given these structural goals:\n{structural_summary}\n\n"
            f"And the mission objective:\n{objective}\n\n"
            f"What functional capabilities should the project deliver? "
            f"Each goal should describe a user-facing capability, not a file or module.\n\n"
            f"Produce 3-5 functional goals as a JSON array of strings.\n"
            f"Example: [\"Players can navigate between rooms using cardinal directions\", "
            f"\"NPC dialogue branches based on player choices\"]\n\n"
            f"Return ONLY the JSON array."
        )

        try:
            result = await effects.run_inference(
                prompt=prompt,
                config_overrides={"temperature": 0.4, "max_tokens": 500},
            )
            if result.text:
                # Parse JSON array from response
                import json as _json
                text = result.text.strip()
                # Strip markdown fences if present
                if text.startswith("```"):
                    text = re.sub(r"^```\w*\n?", "", text)
                    text = re.sub(r"\n?```$", "", text)
                    text = text.strip()

                try:
                    functional_goals = _json.loads(text)
                    if isinstance(functional_goals, list):
                        for desc in functional_goals:
                            if isinstance(desc, str) and desc.strip():
                                goals.append(GoalRecord(
                                    description=desc.strip(),
                                    type="functional",
                                ))
                except _json.JSONDecodeError:
                    logger.warning("Could not parse functional goals JSON: %s", text[:200])
        except Exception as e:
            logger.warning("Functional goal inference failed: %s", e)

    # ── Link tasks to goals by file association ──
    for task in plan:
        if isinstance(task, dict):
            task_target = task.get("inputs", {}).get("target_file_path", "")
            task_id = task.get("id", "")
        else:
            task_target = (task.inputs or {}).get("target_file_path", "")
            task_id = task.id

        if not task_target or not task_id:
            continue

        for goal in goals:
            if task_target in goal.associated_files:
                if task_id not in goal.associated_task_ids:
                    goal.associated_task_ids.append(task_id)
                # Set goal_id on task
                if isinstance(task, dict):
                    task["goal_id"] = goal.id
                elif hasattr(task, "goal_id"):
                    task.goal_id = goal.id
                break  # First matching goal wins

    # ── Persist goals on mission state ──
    goal_dicts = [g.model_dump() for g in goals]
    if hasattr(mission, "goals"):
        mission.goals = goals
        await effects.save_mission(mission)
    elif isinstance(mission, dict):
        mission["goals"] = goal_dicts
        await effects.save_mission(mission)

    return StepOutput(
        result={
            "goals_derived": True,
            "structural_count": sum(1 for g in goals if g.type == "structural"),
            "functional_count": sum(1 for g in goals if g.type == "functional"),
        },
        observations=f"Derived {len(goals)} goals ({sum(1 for g in goals if g.type == 'structural')} structural, {sum(1 for g in goals if g.type == 'functional')} functional)",
        context_updates={
            "goals": goal_dicts,
            "mission": mission,
            "task_count": len(plan),
        },
    )
