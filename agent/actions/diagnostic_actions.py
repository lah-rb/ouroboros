"""Diagnostic actions — compile_diagnosis and read_investigation_targets.

These actions power the diagnose_issue and explore_spike flows.
They assemble structured outputs from inference results and read
targeted files for deep investigation.
"""

from __future__ import annotations

import logging

from agent.models import StepInput, StepOutput
from agent.markdown_fence import extract_first_text_content

logger = logging.getLogger(__name__)


async def action_compile_diagnosis(step_input: StepInput) -> StepOutput:
    """Assemble a structured diagnosis from inference outputs.

    Reads error_analysis, hypotheses, and evaluation from context.
    Produces a structured diagnosis dict with root cause, fix recommendation,
    rejected alternatives, confidence assessment, and recommended fix flow.

    The hypotheses text may contain a FIX_TYPE line indicating whether the
    fix requires file_ops (code/data editing) or project_ops (dependency
    installation, environment setup). Defaults to file_ops if not specified.

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

    # Read recommended fix flow from context (set by classify_fix_type LLM menu).
    # Falls back to "file_ops" if the menu step was skipped or produced no result.
    recommended_flow = step_input.context.get("recommended_flow", "file_ops")
    if recommended_flow not in ("file_ops", "project_ops"):
        recommended_flow = "file_ops"
    logger.info("Diagnosis recommended flow: %s", recommended_flow)

    # Phase A (patch redesign): read the structured fields that
    # conclude_diagnosis publishes from the flattened diagnosis schema.
    # Empty-string defaults keep this tolerant of older flow paths
    # that haven't migrated.
    target_file = step_input.context.get("target_file", "") or ""
    target_symbol = step_input.context.get("target_symbol", "") or ""
    change_spec = step_input.context.get("change_spec", "") or ""
    diagnosis_kind = step_input.context.get("diagnosis_kind", "") or ""
    module_statement = step_input.context.get("module_statement", "") or ""
    diagnosis_confidence = step_input.context.get("diagnosis_confidence", "") or ""
    root_cause_struct = step_input.context.get("root_cause", "") or ""
    # Multi-symbol patching (505 round). conclude_diagnosis publishes
    # this when the change spans multiple symbols whose contract
    # must stay consistent. Normalize to a list[str]; tolerate
    # missing/malformed outputs by defaulting to empty.
    related_symbols_raw = step_input.context.get("related_symbols", []) or []
    if isinstance(related_symbols_raw, list):
        related_symbols = [
            str(s).strip() for s in related_symbols_raw if str(s).strip()
        ]
    elif isinstance(related_symbols_raw, str):
        # Defensive: a single string might arrive if the model
        # emitted one symbol without brackets. Split on commas.
        related_symbols = [
            s.strip() for s in related_symbols_raw.split(",") if s.strip()
        ]
    else:
        related_symbols = []
    # Cap at 6 per CONCLUDE_PROMPT's guidance; if more came back,
    # something is off and a smaller batch is safer than a giant
    # one.
    related_symbols = related_symbols[:6]
    # Dedupe while preserving order, and drop the primary target
    # if the model helpfully re-listed it.
    seen = {target_symbol} if target_symbol else set()
    deduped = []
    for s in related_symbols:
        if s in seen:
            continue
        seen.add(s)
        deduped.append(s)
    related_symbols = deduped

    diagnosis = {
        "error_description": error_description,
        "root_cause": root_cause_struct or error_analysis,
        "hypotheses": hypotheses if include_rejected else "",
        "selected_fix": change_spec or hypotheses,
        "confidence": (
            diagnosis_confidence.lower()
            if diagnosis_confidence
            else ("low" if is_intractable else "medium")
        ),
        "is_intractable": is_intractable,
        "recommended_flow": recommended_flow,
        # Structured operation spec — the dispatcher reads these to
        # route cleanly without regex parsing of the prose summary.
        "target_file": target_file,
        "target_symbol": target_symbol,
        "related_symbols": related_symbols,
        "change_spec": change_spec,
        "kind": diagnosis_kind,
        "module_statement": str(module_statement),
    }

    status_msg = (
        "intractable — escalation recommended" if is_intractable else "complete"
    )

    return StepOutput(
        result={"diagnosis_complete": True, "is_intractable": is_intractable},
        observations=f"Diagnosis assembled: {status_msg} (recommended_flow={recommended_flow})",
        context_updates={"diagnosis": diagnosis},
    )


async def action_create_fix_task_from_diagnosis(step_input: StepInput) -> StepOutput:
    """Record a diagnosed fix as a note for the director to act on.

    In the tier records model, the director decides what to dispatch based
    on DirectiveReports attached to goals. This action records the diagnosis
    details as a note so they're available in the director's context.

    Context required: diagnosis

    Publishes: fix_task_created
    """
    effects = step_input.effects
    diagnosis = step_input.context.get("diagnosis", {})

    if not effects or not diagnosis:
        return StepOutput(
            result={"fix_task_created": False},
            observations="No effects or diagnosis — cannot record fix",
            context_updates={"fix_task_created": False},
        )

    # Skip if diagnosis is intractable
    if diagnosis.get("is_intractable", False):
        return StepOutput(
            result={"fix_task_created": False},
            observations="Diagnosis is intractable — not recording fix",
            context_updates={"fix_task_created": False},
        )

    # Extract fix details from the diagnosis
    selected_fix = str(diagnosis.get("selected_fix", ""))
    root_cause = str(diagnosis.get("root_cause", ""))
    target_file = str(step_input.context.get("target_file_path", ""))

    # Build note content from diagnosis
    note_parts = []
    if root_cause:
        cause_summary = extract_first_text_content(root_cause, max_length=200)
        if cause_summary:
            note_parts.append(f"Root cause: {cause_summary}")
    if selected_fix:
        fix_summary = extract_first_text_content(selected_fix, max_length=300)
        if fix_summary:
            note_parts.append(f"Recommended fix: {fix_summary}")
    if target_file:
        note_parts.append(f"Target: {target_file}")

    note_content = (
        "\n".join(note_parts)
        if note_parts
        else "Diagnosis completed — see directive report"
    )

    saved = await effects.push_note(
        content=note_content,
        category="failure_analysis",
        tags=[target_file] if target_file else [],
        source_flow="diagnose_issue",
    )

    logger.info(
        "Recorded diagnosis note: %s (target: %s)",
        note_content[:60],
        target_file,
    )

    return StepOutput(
        result={"fix_task_created": saved},
        observations=f"Recorded diagnosis: {note_content[:80]}",
        context_updates={"fix_task_created": saved},
    )
