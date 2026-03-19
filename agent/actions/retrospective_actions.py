"""Retrospective and review actions — meta-cognitive capabilities.

These actions power the retrospective and request_review flows.
They load historical data, apply recommendations, and compose reports.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)


def _clean_task_description(desc: str) -> str:
    """Clean markdown formatting artifacts from task descriptions.

    Strips bold markers, backtick wrapping, and truncates overly long
    recommendation text into concise actionable descriptions.
    """
    # Strip markdown bold markers
    desc = re.sub(r"\*\*([^*]+)\*\*", r"\1", desc)
    # Strip backtick wrapping around identifiers
    desc = re.sub(r"`([^`]+)`", r"\1", desc)
    # Strip leading "Recommendation:" prefix
    desc = re.sub(r"^Recommendation\s*:\s*", "", desc, flags=re.IGNORECASE)
    # Strip leading "Insert a dedicated ... task to" phrasing
    desc = re.sub(r"^Insert a dedicated \w+ task to\s*", "", desc, flags=re.IGNORECASE)
    # Capitalize first letter
    if desc and desc[0].islower():
        desc = desc[0].upper() + desc[1:]
    # Truncate to reasonable length (first sentence or 120 chars)
    if len(desc) > 120:
        # Try to cut at first sentence boundary
        sentence_end = re.search(r"[.!?]\s", desc[:120])
        if sentence_end:
            desc = desc[: sentence_end.start() + 1]
        else:
            desc = desc[:120].rsplit(" ", 1)[0] + "..."
    return desc.strip()


# ═══════════════════════════════════════════════════════════════════════
# load_retrospective_data
# ═══════════════════════════════════════════════════════════════════════


async def action_load_retrospective_data(step_input: StepInput) -> StepOutput:
    """Load mission history, task outcomes, timing data, and notes.

    Aggregates data from persistence for retrospective analysis.
    Uses effects.load_mission(), effects.list_artifacts(), effects.load_artifact().

    Publishes: mission_history, task_outcomes, learnings_archive, timing_data
    Result: completed_tasks count for gating
    """
    effects = step_input.effects
    if effects is None:
        return StepOutput(
            result={"completed_tasks": 0},
            observations="No effects interface — cannot load retrospective data",
        )

    mission = await effects.load_mission()
    if mission is None:
        return StepOutput(
            result={"completed_tasks": 0},
            observations="No mission found in persistence",
        )

    # Gather task outcomes
    task_outcomes = []
    completed_count = 0
    for task in mission.plan:
        outcome = {
            "id": task.id,
            "description": task.description,
            "flow": task.flow,
            "status": task.status,
            "frustration": task.frustration,
            "attempts": len(task.attempts),
            "summary": task.summary or "",
            "failure_reason": "",
        }
        if task.status == "complete":
            completed_count += 1
        elif task.status in ("failed", "blocked"):
            outcome["failure_reason"] = task.summary or "Unknown failure"
        task_outcomes.append(outcome)

    # Gather timing data from artifacts
    timing_data = {}
    include_artifacts = step_input.params.get("include_artifacts", True)
    if include_artifacts:
        try:
            artifact_files = await effects.list_artifacts()
            for af in artifact_files[-20:]:  # Last 20 artifacts max
                try:
                    # Extract task_id from filename pattern: {timestamp}_{task_id}.json
                    parts = af.rsplit("_", 1)
                    if len(parts) >= 2:
                        task_id = parts[-1].replace(".json", "")
                        artifact = await effects.load_artifact(task_id)
                        if artifact:
                            flow_name = (
                                artifact.flow_name
                                if hasattr(artifact, "flow_name")
                                else "unknown"
                            )
                            step_count = len(
                                artifact.steps_executed
                                if hasattr(artifact, "steps_executed")
                                else []
                            )
                            if flow_name not in timing_data:
                                timing_data[flow_name] = {
                                    "invocations": 0,
                                    "total_steps": 0,
                                }
                            timing_data[flow_name]["invocations"] += 1
                            timing_data[flow_name]["total_steps"] += step_count
                except Exception as e:
                    logger.debug("Failed to load artifact %s: %s", af, e)
        except Exception as e:
            logger.debug("Failed to list artifacts: %s", e)

    # Gather learnings from notes
    learnings_archive = []
    include_learnings = step_input.params.get("include_learnings", True)
    if include_learnings and hasattr(mission, "notes") and mission.notes:
        for note in mission.notes:
            learnings_archive.append(
                {
                    "type": note.category,
                    "content": note.content,
                    "source_flow": note.source_flow,
                    "timestamp": note.timestamp,
                }
            )

    # Build mission history summary
    mission_history = {
        "id": mission.id,
        "objective": mission.objective,
        "status": mission.status,
        "total_tasks": len(mission.plan),
        "completed": completed_count,
        "plan": [
            {
                "id": t.id,
                "description": t.description,
                "status": t.status,
                "flow": t.flow,
            }
            for t in mission.plan
        ],
    }

    return StepOutput(
        result={
            "completed_tasks": completed_count,
        },
        observations=f"Loaded retrospective data: {completed_count} completed tasks, "
        f"{len(learnings_archive)} learnings, {len(timing_data)} flow types in artifacts",
        context_updates={
            "mission_history": mission_history,
            "task_outcomes": task_outcomes,
            "learnings_archive": learnings_archive,
            "timing_data": timing_data,
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# apply_retrospective_recommendations
# ═══════════════════════════════════════════════════════════════════════


async def action_apply_retrospective_recommendations(
    step_input: StepInput,
) -> StepOutput:
    """Translate typed recommendations into mission state changes.

    Parses the LLM's recommendations and applies them:
    - add_task: creates new TaskRecord in mission.plan
    - reprioritize: updates task priorities
    - note_for_knowledge_base: adds a NoteRecord
    - adjust_approach / revise_plan: logged as notes for context

    Publishes: changes_applied
    """
    from agent.persistence.models import TaskRecord, NoteRecord

    effects = step_input.effects
    # Runtime stores inference results as inference_response
    recommendations = step_input.context.get(
        "recommendations", step_input.context.get("inference_response", "")
    )
    mission_history = step_input.context.get("mission_history", {})

    if not effects:
        return StepOutput(
            result={"changes_applied": False},
            observations="No effects — cannot apply recommendations",
        )

    mission = await effects.load_mission()
    if not mission:
        return StepOutput(
            result={"changes_applied": False},
            observations="No mission in persistence",
        )

    changes = []

    # Parse recommendations text for typed actions
    rec_text = str(recommendations)

    # Look for add_task recommendations
    add_task_matches = re.findall(
        r"(?:add_task|Add task)[:\s]*(.+?)(?:\n|$)", rec_text, re.IGNORECASE
    )
    for desc in add_task_matches:
        desc = desc.strip().strip('"').strip("'")
        # Fix 4: Clean markdown formatting from task descriptions
        desc = _clean_task_description(desc)
        if len(desc) > 10:
            from agent.actions.mission_actions import (
                _infer_flow_from_description,
                _is_duplicate_task,
            )

            inferred_flow = _infer_flow_from_description(desc)

            # Build inputs with proper fields for the inferred flow
            task_inputs: dict[str, Any] = {
                "reason": f"Retrospective recommendation: {desc}",
            }

            # Try to extract ALL mentioned file paths from the description
            all_files = re.findall(
                r"[`'\"]?([a-zA-Z0-9_/.-]+\.(?:py|js|ts|yaml|yml|md|toml|json|rs))[`'\"]?",
                desc,
            )
            # Deduplicate while preserving order
            seen_files: set[str] = set()
            unique_files: list[str] = []
            for f in all_files:
                if f not in seen_files:
                    seen_files.add(f)
                    unique_files.append(f)

            # Fix 2: Split multi-file refactor into individual tasks
            if len(unique_files) > 1 and inferred_flow == "refactor":
                for target in unique_files[:3]:  # cap at 3 per recommendation
                    split_desc = f"Refactor: clean up {target}"
                    split_inputs: dict[str, Any] = {
                        "reason": f"Retrospective recommendation: {desc}",
                        "target_file_path": target,
                    }
                    if _is_duplicate_task(mission, split_desc, "refactor", target):
                        changes.append(f"Skipped duplicate task: {split_desc[:60]}")
                        continue
                    new_task = TaskRecord(
                        description=split_desc,
                        flow="refactor",
                        priority=len(mission.plan),
                        inputs=split_inputs,
                    )
                    mission.plan.append(new_task)
                    changes.append(f"Added task: {split_desc[:60]}")
                continue

            # Single-file case: use the first match
            target_file = unique_files[0] if unique_files else ""
            if target_file:
                task_inputs["target_file_path"] = target_file
            elif inferred_flow in (
                "modify_file",
                "create_file",
                "create_tests",
                "refactor",
            ):
                # Fix 1: Skip vague tasks without targets for all file-targeting flows
                changes.append(f"Skipped vague task (no target file): {desc[:60]}")
                continue

            # Deduplication: skip if a similar task already exists
            if _is_duplicate_task(mission, desc, inferred_flow, target_file):
                changes.append(f"Skipped duplicate task: {desc[:60]}")
                continue

            new_task = TaskRecord(
                description=desc,
                flow=inferred_flow,
                priority=len(mission.plan),  # append at end
                inputs=task_inputs,
            )
            mission.plan.append(new_task)
            changes.append(f"Added task: {desc[:60]}")

    # Look for reprioritize recommendations
    reprioritize_matches = re.findall(
        r"(?:reprioritize|Reprioritize)[:\s]*(.+?)(?:\n|$)",
        rec_text,
        re.IGNORECASE,
    )
    for repri in reprioritize_matches:
        changes.append(f"Reprioritize noted: {repri.strip()[:60]}")

    # Look for knowledge base notes
    note_matches = re.findall(
        r"(?:note_for_knowledge_base|Knowledge base)[:\s]*(.+?)(?:\n|$)",
        rec_text,
        re.IGNORECASE,
    )
    for note_content in note_matches:
        note_content = note_content.strip().strip('"').strip("'")
        if len(note_content) > 5:
            note = NoteRecord(
                content=note_content,
                category="task_learning",
                source_flow="retrospective",
                tags=["retrospective"],
            )
            mission.notes.append(note)
            changes.append(f"Added note: {note_content[:60]}")

    # Look for approach adjustments
    approach_matches = re.findall(
        r"(?:adjust_approach|Adjust approach)[:\s]*(.+?)(?:\n|$)",
        rec_text,
        re.IGNORECASE,
    )
    for adj in approach_matches:
        adj = adj.strip()
        if len(adj) > 5:
            note = NoteRecord(
                content=f"Approach adjustment: {adj}",
                category="task_learning",
                source_flow="retrospective",
                tags=["retrospective", "approach"],
            )
            mission.notes.append(note)
            changes.append(f"Approach adjustment noted: {adj[:60]}")

    # Save updated mission
    if changes:
        await effects.save_mission(mission)

    return StepOutput(
        result={
            "changes_applied": len(changes) > 0,
            "change_count": len(changes),
        },
        observations=f"Applied {len(changes)} retrospective changes: "
        + "; ".join(changes[:5]),
        context_updates={
            "changes_applied": changes,
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# compose_director_report
# ═══════════════════════════════════════════════════════════════════════


async def action_compose_director_report(step_input: StepInput) -> StepOutput:
    """Format retrospective findings as a report and push as event.

    Publishes: director_report
    """
    from agent.persistence.models import Event

    effects = step_input.effects
    performance_analysis = step_input.context.get("performance_analysis", "")
    # Runtime stores inference results as inference_response
    recommendations = step_input.context.get(
        "recommendations", step_input.context.get("inference_response", "")
    )
    mission_health = step_input.context.get("mission_health", "unknown")
    mission_history = step_input.context.get("mission_history", {})

    objective = mission_history.get("objective", "Unknown mission")
    total = mission_history.get("total_tasks", 0)
    completed = mission_history.get("completed", 0)

    report = (
        f"# Retrospective Report\n\n"
        f"**Mission:** {objective}\n"
        f"**Progress:** {completed}/{total} tasks complete\n"
        f"**Health:** {mission_health}\n\n"
        f"## Analysis\n{performance_analysis}\n\n"
        f"## Recommendations\n{recommendations}\n"
    )

    # Push as event if effects available
    if effects:
        event = Event(
            type="user_message",
            payload={
                "message_type": "retrospective_report",
                "report": report,
                "mission_health": str(mission_health),
            },
        )
        await effects.push_event(event)

    return StepOutput(
        result={"report_generated": True},
        observations=f"Composed director report ({len(report)} chars), health={mission_health}",
        context_updates={
            "director_report": report,
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# submit_review_to_api (STUB — blocked on Phase 6 escalation)
# ═══════════════════════════════════════════════════════════════════════


async def action_submit_review_to_api(step_input: StepInput) -> StepOutput:
    """Submit code review request to senior dev via escalation API.

    STUB: Returns review_unavailable until Phase 6 (escalation effect)
    is implemented. When Phase 6 lands, this action will call
    effects.escalate_to_api() with the review request and system prompt.

    Publishes: review_response
    """
    review_request = step_input.context.get("review_request", "")
    review_files = step_input.context.get("review_files", {})

    # Phase 6 TODO: Replace this stub with actual escalation API call
    # system_prompt = step_input.params.get("system_prompt_override", "")
    # response = await effects.escalate_to_api(review_request, system_prompt)

    logger.info(
        "Review submission stub: %d files, request length %d chars",
        len(review_files) if isinstance(review_files, dict) else 0,
        len(str(review_request)),
    )

    return StepOutput(
        result={
            "response_received": False,
            "reason": "Review API not yet available (Phase 6 escalation required)",
        },
        observations="Review submission stub — escalation API not yet implemented",
        context_updates={
            "review_response": None,
        },
    )
