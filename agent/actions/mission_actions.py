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

                # Record the attempt
                task.attempts.append(
                    AttemptRecord(
                        flow=task.flow,
                        status=last_status or "unknown",
                        summary=str(last_result)[:200] if last_result else "",
                    )
                )

                frustration[task.id] = task.frustration
                break

    # Save updated state
    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={
            "needs_plan": False,
            "task_completed": task_completed,
            "events_pending": False,
            "frustration_reset": frustration_was_elevated and task_completed,
        },
        observations=f"Updated task {last_task_id[:8] if last_task_id else 'none'}: "
        f"status={last_status}, completed={task_completed}",
        context_updates={"mission": mission, "frustration": frustration},
    )


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
        return StepOutput(
            result={"task_selected": False, "no_actionable_tasks": True},
            observations="No actionable tasks remaining",
        )

    # Build the menu prompt
    lines = ["Select the task to work on next:\n"]
    for i, task in enumerate(actionable):
        letter = chr(ord("a") + i)
        frust = f" [frustration: {task.frustration}]" if task.frustration > 0 else ""
        target = task.inputs.get("target_file_path", "")
        target_str = f" → {target}" if target else ""
        lines.append(f"{letter}) [{task.status}] {task.description}{target_str}{frust}")

    lines.append("\nPick the letter of the most impactful task to work on.")
    prompt = "\n".join(lines)

    # Grammar: constrain to valid letters
    n = len(actionable)
    last_letter = chr(ord("a") + n - 1)
    grammar = f"root ::= [a-{last_letter}]"

    try:
        result = await effects.session_inference(
            session_id,
            prompt,
            {"temperature": 0.1, "max_tokens": 5, "grammar": grammar},
        )
        response = result.text.strip().lower()
    except Exception as e:
        logger.error("Task selection failed: %s", e)
        # Fall back to first actionable task
        response = "a"

    # Map letter to task
    index = ord(response[0]) - ord("a") if response else 0
    if index < 0 or index >= len(actionable):
        index = 0

    selected = actionable[index]

    # Mark as in_progress
    for task in mission.plan:
        if task.id == selected.id:
            task.status = "in_progress"
            break

    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={"task_selected": True},
        observations=f"Selected task: {selected.description[:60]}",
        context_updates={
            "selected_task": selected,
            "selected_task_id": selected.id,
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
    """Present project files as an LLM menu for file-targeting flows.

    Only fires when the selected task's target_file_path is empty or
    doesn't exist on disk. Uses the memoryful session so the model
    already has context from the reason and task selection steps.

    Reads: context.selected_task, context.session_id, context.mission
    Publishes: dispatch_config (complete dispatch configuration)
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    session_id = step_input.context.get("session_id", "")
    selected_task = step_input.context.get("selected_task")
    dispatch_flow = (
        step_input.params.get("dispatch_flow", "")
        or step_input.context.get("dispatch_flow", "")
    )

    if not mission or not effects or not selected_task:
        return StepOutput(
            result={"file_selected": False},
            observations="Missing context for file selection",
        )

    task_inputs = selected_task.inputs or {}
    target_path = task_inputs.get("target_file_path", "")
    working_dir = mission.config.working_directory

    # Determine the flow to dispatch
    if not dispatch_flow:
        dispatch_flow = selected_task.flow or "create_file"

    # Check if we need file selection (only for flows that target existing files)
    needs_existing_file = dispatch_flow in {
        "modify_file",
        "refactor",
        "diagnose_issue",
    }

    # If we have a valid path, or flow doesn't need an existing file, skip selection
    if target_path and not needs_existing_file:
        return _build_dispatch_config(
            mission,
            selected_task,
            dispatch_flow,
            target_path,
        )

    # For existing-file flows, validate the path exists
    if target_path and needs_existing_file:
        full_path = os.path.join(working_dir, target_path)
        if await effects.file_exists(full_path):
            return _build_dispatch_config(
                mission,
                selected_task,
                dispatch_flow,
                target_path,
            )
        # Path invalid — fall through to menu selection

    # For create flows with a path from the architecture, use it directly
    if target_path and not needs_existing_file:
        return _build_dispatch_config(
            mission,
            selected_task,
            dispatch_flow,
            target_path,
        )

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
            return _build_dispatch_config(
                mission,
                selected_task,
                dispatch_flow,
                target_path,
            )
        return StepOutput(
            result={"file_selected": False, "error": "no_project_files"},
            observations=f"No project files found for {dispatch_flow}. "
            f"The project may be empty — consider creating files first.",
        )

    # Present file menu via memoryful session
    lines = [f"Select the target file for this {dispatch_flow} task:"]
    lines.append(f"Task: {selected_task.description}\n")

    for i, filepath in enumerate(file_list[:19]):  # Leave room for create option
        letter = chr(ord("a") + i)
        lines.append(f"{letter}) {filepath}")

    # Add "create new file" escape hatch for flows that target existing files
    n_files = min(len(file_list), 19)
    if needs_existing_file and n_files > 0:
        create_letter = chr(ord("a") + n_files)
        lines.append(f"{create_letter}) [CREATE NEW FILE] The file I need doesn't exist yet")
        n_options = n_files + 1
    else:
        create_letter = None
        n_options = n_files

    lines.append("\nPick the file that best matches the task.")
    prompt = "\n".join(lines)

    last_letter = chr(ord("a") + n_options - 1)
    grammar = f"root ::= [a-{last_letter}]"

    try:
        result = await effects.session_inference(
            session_id,
            prompt,
            {"temperature": 0.1, "max_tokens": 5, "grammar": grammar},
        )
        response = result.text.strip().lower()
    except Exception as e:
        logger.error("File selection failed: %s", e)
        return StepOutput(
            result={"file_selected": False, "error": "selection_failed"},
            observations=f"File selection failed: {e}",
        )

    index = ord(response[0]) - ord("a") if response else 0
    if index < 0 or index >= n_options:
        index = 0

    # Handle "create new file" selection
    if create_letter and response and response[0] == create_letter:
        # Follow-up prompt: ask what file path to create
        create_prompt = (
            f"You chose to create a new file instead of modifying an existing one.\n"
            f"Task: {selected_task.description}\n\n"
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
            # Basic sanitation — take first line, strip whitespace
            new_path = new_path.splitlines()[0].strip() if new_path else ""
        except Exception as e:
            logger.error("Create file path prompt failed: %s", e)
            new_path = ""

        if new_path:
            logger.info(
                "File selection redirected: %s → create_file for %s",
                dispatch_flow,
                new_path,
            )
            return _build_dispatch_config(
                mission,
                selected_task,
                "create_file",
                new_path,
            )
        # Fallback: if no path given, fall through to first file in list
        logger.warning("Create redirect failed — no path given, using first file")

    selected_path = file_list[index] if index < n_files else file_list[0]

    return _build_dispatch_config(
        mission,
        selected_task,
        dispatch_flow,
        selected_path,
    )


def _build_dispatch_config(
    mission,
    selected_task,
    dispatch_flow: str,
    target_file_path: str,
) -> StepOutput:
    """Build the flat dispatch_config for the tail-call to a task flow."""
    task_inputs = selected_task.inputs or {}

    # Gather relevant notes — architecture summary + recent observations
    relevant_notes = ""
    if hasattr(mission, "notes") and mission.notes:
        recent = sorted(mission.notes, key=lambda n: n.timestamp, reverse=True)[:8]
        relevant_notes = "\n".join(f"[{n.category}] {n.content[:200]}" for n in recent)

    # Include architecture summary if available
    if mission.architecture:
        arch = mission.architecture
        arch_summary = (
            f"Import scheme: {arch.import_scheme}. "
            f"Run command: {arch.run_command}. "
            f"Modules: {', '.join(arch.canonical_files())}."
        )

        # Include interface contracts relevant to the target file
        relevant_interfaces = [
            f"  {i.caller} → {i.callee}: {i.symbol}({i.signature})"
            for i in arch.interfaces
            if target_file_path and (
                i.caller == target_file_path or i.callee == target_file_path
            )
        ]
        if relevant_interfaces:
            arch_summary += "\nInterfaces:\n" + "\n".join(relevant_interfaces)

        # Include data shape contracts relevant to the target file
        relevant_shapes = [
            f"  {ds.file} → {ds.consumed_by}: {ds.structure}"
            for ds in arch.data_shapes
            if target_file_path and (
                ds.file == target_file_path or ds.consumed_by == target_file_path
            )
        ]
        if relevant_shapes:
            arch_summary += "\nData shapes (CRITICAL — match this format exactly):\n" + "\n".join(relevant_shapes)

        if relevant_notes:
            relevant_notes = f"[architecture] {arch_summary}\n{relevant_notes}"
        else:
            relevant_notes = f"[architecture] {arch_summary}"

    dispatch_config = {
        "flow": dispatch_flow,
        "task_id": selected_task.id,
        "task_description": selected_task.description,
        "mission_objective": mission.objective,
        "working_directory": mission.config.working_directory,
        "target_file_path": target_file_path,
        "reason": task_inputs.get("reason", "") or selected_task.description,
        "relevant_notes": relevant_notes,
        "mission_id": mission.id,
    }

    return StepOutput(
        result={"file_selected": True},
        observations=f"Dispatch ready: {dispatch_flow} → {target_file_path or '(project-level)'}",
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
        data_shapes.append(
            DataShapeContract(
                file=ds.get("file", ""),
                consumed_by=ds.get("consumed_by", ""),
                structure=ds.get("structure", ""),
            )
        )

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
            f"  {ds.file} → {ds.consumed_by}: {ds.structure}"
            for ds in arch.data_shapes
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
                flow="create_file",
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


def _infer_flow_hint(desc: str) -> str:
    """Lightweight flow hint from description. Used as a default that
    mission_control can override at dispatch time."""
    d = desc.lower()

    if any(
        kw in d for kw in ["design architecture", "design project", "directory layout"]
    ):
        return "design_architecture"
    if any(kw in d for kw in ["wire all", "integrate", "verify imports"]):
        return "integrate_modules"
    if any(kw in d for kw in ["run the", "verify", "end-to-end", "validate"]):
        return "validate_behavior"
    if any(kw in d for kw in ["test", "create tests"]):
        return "create_tests"
    if any(kw in d for kw in ["diagnose", "debug", "investigate"]):
        return "diagnose_issue"
    if any(kw in d for kw in ["refactor", "improve structure"]):
        return "refactor"
    if any(kw in d for kw in ["install", "dependency", "package"]):
        return "manage_packages"
    if any(kw in d for kw in ["document", "readme"]):
        return "document_project"
    if any(kw in d for kw in ["modify", "fix", "update", "change"]):
        return "modify_file"

    return "create_file"


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
