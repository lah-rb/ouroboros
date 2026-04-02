"""Diagnostic actions — compile_diagnosis and read_investigation_targets.

These actions power the diagnose_issue and explore_spike flows.
They assemble structured outputs from inference results and read
targeted files for deep investigation.
"""

from __future__ import annotations

import re
import logging
from typing import Any

from agent.models import StepInput, StepOutput
from agent.markdown_fence import extract_first_text_content

logger = logging.getLogger(__name__)


async def action_compile_diagnosis(step_input: StepInput) -> StepOutput:
    """Assemble a structured diagnosis from inference outputs.

    Reads error_analysis, hypotheses, and evaluation from context.
    Produces a structured diagnosis dict with root cause, fix recommendation,
    rejected alternatives, and confidence assessment.

    Params:
        include_rejected_hypotheses: bool — include rejected alternatives
        mark_as_intractable: bool — flag diagnosis as intractable

    Publishes: diagnosis
    """
    error_analysis = step_input.context.get("error_analysis", "")
    hypotheses = step_input.context.get("hypotheses", "")
    error_description = step_input.context.get("error_description", "")

    include_rejected = step_input.params.get("include_rejected_hypotheses", True)
    is_intractable = step_input.params.get("mark_as_intractable", False)

    diagnosis = {
        "error_description": error_description,
        "root_cause": error_analysis,
        "hypotheses": hypotheses if include_rejected else "",
        "selected_fix": hypotheses,
        "confidence": "low" if is_intractable else "medium",
        "is_intractable": is_intractable,
    }

    status_msg = (
        "intractable — escalation recommended" if is_intractable else "complete"
    )

    return StepOutput(
        result={"diagnosis_complete": True, "is_intractable": is_intractable},
        observations=f"Diagnosis assembled: {status_msg}",
        context_updates={"diagnosis": diagnosis},
    )


async def action_create_fix_task_from_diagnosis(step_input: StepInput) -> StepOutput:
    """Create a follow-up fix task in the mission plan from a completed diagnosis.

    Reads the structured diagnosis from context, extracts the recommended fix,
    and inserts a new TaskRecord so mission_control can dispatch it.

    Context required: diagnosis
    Optional: mission (loaded from persistence if not in context)

    Publishes: fix_task_created
    """
    from agent.persistence.models import TaskRecord
    from agent.actions.mission_actions import _is_duplicate_task

    effects = step_input.effects
    diagnosis = step_input.context.get("diagnosis", {})

    if not effects or not diagnosis:
        return StepOutput(
            result={"fix_task_created": False},
            observations="No effects or diagnosis — cannot create fix task",
            context_updates={"fix_task_created": False},
        )

    # Skip if diagnosis is intractable
    if diagnosis.get("is_intractable", False):
        return StepOutput(
            result={"fix_task_created": False},
            observations="Diagnosis is intractable — not creating fix task",
            context_updates={"fix_task_created": False},
        )

    mission = await effects.load_mission()
    if not mission:
        return StepOutput(
            result={"fix_task_created": False},
            observations="No mission in persistence",
            context_updates={"fix_task_created": False},
        )

    # Extract fix details from the diagnosis
    selected_fix = str(diagnosis.get("selected_fix", ""))
    root_cause = str(diagnosis.get("root_cause", ""))
    target_file = str(step_input.context.get("target_file_path", ""))

    # If no target file from input, try to extract from diagnosis text
    if not target_file:
        file_match = re.search(
            r"(?:in|modify|fix|update|change)\s+[`'\"]?([a-zA-Z0-9_/.-]+\.py)[`'\"]?",
            selected_fix,
        )
        if file_match:
            target_file = file_match.group(1)

    # Build description from diagnosis
    desc_parts = []
    if target_file:
        desc_parts.append(f"Fix issue in {target_file}")
    else:
        desc_parts.append("Fix diagnosed issue")
    if root_cause:
        # Extract first substantive text from the model's markdown output
        cause_summary = extract_first_text_content(root_cause, max_length=100)
        if cause_summary:
            desc_parts.append(f"— {cause_summary}")
    fix_description = " ".join(desc_parts)

    # Determine flow type — route through file_ops which handles
    # create vs modify routing and validation lifecycle
    flow = "file_ops"

    # Deduplication check
    if _is_duplicate_task(mission, fix_description, flow, target_file):
        return StepOutput(
            result={"fix_task_created": False},
            observations=f"Duplicate fix task already exists for {target_file}",
            context_updates={"fix_task_created": False},
        )

    # Create the fix task with high priority
    reason = f"Diagnosed root cause: {root_cause[:200]}\n\nRecommended fix: {selected_fix[:300]}"
    task = TaskRecord(
        description=fix_description,
        flow=flow,
        priority=0,  # High priority — fix should run next
        inputs={
            "target_file_path": target_file,
            "reason": reason,
        },
    )
    mission.plan.append(task)
    await effects.save_mission(mission)

    logger.info(
        "Created fix task from diagnosis: %s (target: %s)",
        fix_description[:60],
        target_file,
    )

    return StepOutput(
        result={"fix_task_created": True, "fix_task_id": task.id},
        observations=f"Created fix task: {fix_description[:80]}",
        context_updates={"fix_task_created": True},
    )


async def action_read_investigation_targets(step_input: StepInput) -> StepOutput:
    """Read full content of files identified in an investigation plan.

    Parses file paths from the investigation_plan text in context,
    reads each via effects.read_file(), and returns a deep_context dict.

    Params:
        max_files: int — maximum number of files to read (default 5)
        max_bytes_per_file: int — max bytes per file (default 50000)

    Publishes: deep_context
    """
    effects = step_input.effects
    if effects is None:
        return StepOutput(
            result={"files_read": 0},
            observations="No effects interface — cannot read files",
            context_updates={"deep_context": {}},
        )

    plan_text = step_input.context.get("investigation_plan", "")
    project_context = step_input.context.get("project_manifest", {})

    max_files = int(step_input.params.get("max_files", 5))
    max_bytes = int(step_input.params.get("max_bytes_per_file", 50000))

    # Extract file paths from the plan text
    # Match patterns like: path/to/file.py, src/module.py, etc.
    path_pattern = r"(?:^|\s|[`\"'])([a-zA-Z0-9_./-]+\.(?:py|yaml|yml|md|toml|json|js|ts|rs|cfg|txt|ini))"
    found_paths = re.findall(path_pattern, plan_text)

    # Also check if any paths from project_manifest are mentioned
    if project_context and isinstance(project_context, dict):
        manifest_paths = list(project_context.keys())
        for mp in manifest_paths:
            # Check if the manifest path, basename, or stem appears in the plan
            basename = mp.split("/")[-1]
            stem = basename.rsplit(".", 1)[0] if "." in basename else basename
            if basename in plan_text or mp in plan_text or stem in plan_text:
                if mp not in found_paths:
                    found_paths.append(mp)

    # Deduplicate while preserving order
    seen = set()
    unique_paths = []
    for p in found_paths:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)

    # Cap at max_files
    targets = unique_paths[:max_files]

    deep_context: dict[str, str] = {}
    files_read = 0

    for target_path in targets:
        try:
            fc = await effects.read_file(target_path)
            if fc.exists:
                content = fc.content
                if len(content) > max_bytes:
                    content = content[:max_bytes] + "\n# ... truncated ..."
                deep_context[target_path] = content
                files_read += 1
                logger.debug(
                    "Read investigation target: %s (%d chars)",
                    target_path,
                    len(content),
                )
            else:
                logger.debug("Investigation target not found: %s", target_path)
        except Exception as e:
            logger.warning("Failed to read investigation target %s: %s", target_path, e)

    return StepOutput(
        result={"files_read": files_read, "targets_requested": len(targets)},
        observations=f"Read {files_read}/{len(targets)} investigation targets",
        context_updates={"deep_context": deep_context},
    )
