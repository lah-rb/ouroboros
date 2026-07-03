"""Reporting chain actions — DirectiveReport production and goal attachment.

Tier Records Architecture:
  - compile_directive_report: inference-based summary for file_ops, diagnose_issue, interact
  - build_directive_report: mechanical summary for project_ops
  - attach_directive_report: receives report in mission_control, attaches to goal

These actions implement the Tier 3 → Tier 2 reporting chain.
Flow_directive flows produce a DirectiveReport before tail-calling back
to mission_control. Mission_control attaches the report to the goal.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Tier 3 → Tier 2: Directive Report Production
# ══════════════════════════════════════════════════════════════════════


def _coerce_files_list(files_changed: Any) -> list[str]:
    """Normalize files_changed (possibly scalar, None, list) to list[str]."""
    if isinstance(files_changed, list):
        return [str(f) for f in files_changed]
    if files_changed:
        return [str(files_changed)]
    return []


def _wrap_report(report: Any, flow_name: str, status: str) -> StepOutput:
    """Package a DirectiveReport into the standard StepOutput shape."""
    return StepOutput(
        result={"report_compiled": True},
        observations=f"{flow_name} report: {status}",
        context_updates={"directive_report": report.model_dump()},
    )


async def action_compile_directive_report(step_input: StepInput) -> StepOutput:
    """Produce a DirectiveReport using inference to summarize flow results.

    Used by inference-based flow_directive flows (file_ops, diagnose_issue,
    interact) as a step before tail-calling to mission_control.

    Reads accumulated context (files_changed, validation_results,
    terminal_output, edit_summary, session_summary, diagnosis, etc.)
    and produces a natural language summary via inference.

    Context optional: files_changed, validation_results, terminal_output,
        edit_summary, session_summary, diagnosis, bail_reason,
        inference_response, write_action
    Params: flow_name (str), status (str)
    Publishes: directive_report
    """
    effects = step_input.effects
    params = step_input.params
    ctx = step_input.context

    flow_name = params.get("flow_name", step_input.meta.flow_name or "unknown")
    status = params.get("status", "success")

    # Gather available context for summarization
    files_changed = ctx.get("files_changed", [])
    validation_results = ctx.get("validation_results", {})
    terminal_output = ctx.get("terminal_output", "")
    edit_summary = ctx.get("edit_summary", "")
    session_summary = ctx.get("session_summary", "")
    diagnosis = ctx.get("diagnosis", {})
    bail_reason = ctx.get("bail_reason", "")

    # Build evidence string for inference
    evidence_parts = []
    if files_changed:
        if isinstance(files_changed, list):
            evidence_parts.append(
                f"Files affected: {', '.join(str(f) for f in files_changed)}"
            )
        else:
            evidence_parts.append(f"Files affected: {files_changed}")
    if edit_summary:
        evidence_parts.append(f"Edit summary: {edit_summary}")
    if session_summary:
        evidence_parts.append(f"Session summary: {session_summary}")
    if bail_reason:
        evidence_parts.append(f"Bail reason: {bail_reason}")
    if diagnosis:
        if isinstance(diagnosis, dict):
            root_cause = diagnosis.get("root_cause", "")
            if root_cause:
                evidence_parts.append(f"Diagnosis: {root_cause}")
        else:
            evidence_parts.append(f"Diagnosis: {str(diagnosis)}")
    if terminal_output:
        # Truncate terminal output for the summary prompt
        output_snippet = str(terminal_output)
        evidence_parts.append(f"Terminal output (excerpt):\n{output_snippet}")
    if validation_results:
        if isinstance(validation_results, list):
            passed = [
                c for c in validation_results if isinstance(c, dict) and c.get("passed")
            ]
            failed = [
                c
                for c in validation_results
                if isinstance(c, dict) and not c.get("passed")
            ]
            if passed:
                evidence_parts.append(
                    f"Checks passed: {', '.join(c.get('name', '?') for c in passed)}"
                )
            if failed:
                evidence_parts.append(
                    f"Checks failed: {', '.join(c.get('name', '?') for c in failed)}"
                )

    evidence = (
        "\n".join(evidence_parts)
        if evidence_parts
        else "No detailed evidence available."
    )

    # Build checks lists — and reconstruct the gate's failure text from the
    # failed checks' captured stderr/stdout. The gate (run_checks) records each
    # check's error output on the check dict, but only `validation_results` is
    # threaded to this report step (not the formatted `validation_output`
    # string, and not `terminal_output` — that key is set only by the interact
    # flow's program run). So a file_ops/structural failure report otherwise
    # lands with an EMPTY terminal_output, and the re-diagnose that reads
    # last_report.terminal_output gets no live error: the diagnose seed drops
    # its "## What crashed"/"## Transcript" sections and the model fixates on a
    # stale prior-attempt headline. Proven cache-independent (reproduces
    # identically with flow_kv_cache off). Rebuild it here so the report
    # faithfully records what failed.
    checks_passed = []
    checks_failed = []
    gate_error_blocks: list[str] = []
    if isinstance(validation_results, list):
        for check in validation_results:
            if isinstance(check, dict):
                name = check.get("name", "unknown")
                if check.get("passed"):
                    checks_passed.append(name)
                else:
                    checks_failed.append(name)
                    detail = (
                        str(check.get("stderr", "") or "").strip()
                        or str(check.get("stdout", "") or "").strip()
                    )
                    gate_error_blocks.append(
                        f"[FAIL] {name}" + (f"\n{detail}" if detail else "")
                    )
    gate_error = "\n".join(gate_error_blocks)

    # Files affected as list of strings
    files_list = _coerce_files_list(files_changed)

    # Use inference to produce a summary
    summary = f"{flow_name} completed with status: {status}"
    if effects and evidence_parts:
        prompt = (
            f"Summarize what happened in this {flow_name} execution in 2-3 sentences.\n"
            f"Status: {status}\n\n"
            f"Evidence:\n{evidence}\n\n"
            f"Write a concise, factual summary. Focus on: what was accomplished, "
            f"what failed (if anything), and the current state. "
            f"Do NOT use JSON or structured format — just plain text."
        )
        try:
            result = await effects.run_inference(
                prompt=prompt,
                config_overrides={"temperature": 0.1},
            )
            if result.text and result.text.strip():
                summary = result.text.strip()
        except Exception as e:
            logger.warning("Directive report inference failed: %s", e)
            # Fall back to mechanical summary
            summary = "; ".join(evidence_parts) if evidence_parts else summary

    # Extract recommended_flow from diagnosis if present
    recommended_flow = ""
    # Phase A — pull structured operation spec from the diagnosis dict.
    # action_compile_diagnosis populates these from the flattened schema
    # conclude_diagnosis produces. Empty defaults keep non-diagnose reports
    # untouched.
    target_file = ""
    target_symbol = ""
    change_spec = ""
    diagnosis_kind = ""
    module_statement = ""
    # Multi-symbol patching (505 round) — carry related_symbols
    # through so the dispatcher can thread them into subsequent
    # fix dispatches. Non-diagnose flows leave this empty.
    related_symbols: list[str] = []
    if isinstance(diagnosis, dict):
        recommended_flow = diagnosis.get("recommended_flow", "") or ""
        target_file = str(diagnosis.get("target_file", "") or "")
        target_symbol = str(diagnosis.get("target_symbol", "") or "")
        change_spec = str(diagnosis.get("change_spec", "") or "")
        diagnosis_kind = str(diagnosis.get("kind", "") or "")
        module_statement = str(diagnosis.get("module_statement", "") or "")
        raw_rel = diagnosis.get("related_symbols", []) or []
        if isinstance(raw_rel, list):
            related_symbols = [str(s).strip() for s in raw_rel if str(s).strip()]

    # 902 round — when no ``diagnosis`` dict is in context (file_ops
    # flow case; the diagnosis dict only exists in diagnose_issue's
    # own reports), fall back to step_input.params. The file_ops
    # compile_report_success/failure steps pass the flow's inputs in
    # as params via $ref so these values are reachable here. Without
    # this fallback file_ops reports landed with empty target_file /
    # target_symbol, breaking the "## Prior attempts" target-repeat
    # signal downstream — the diagnose seed could only see
    # "patched main.py" instead of "patched main.py:_load_world_data",
    # collapsing 10+ symbol-level repeats into one file-level repeat
    # and the CRITICAL warning never became authoritative.
    params = step_input.params or {}
    if not target_file:
        target_file = str(params.get("target_file_path", "") or "")
    if not target_symbol:
        target_symbol = str(params.get("target_symbol", "") or "")
    if not change_spec:
        change_spec = str(params.get("change_spec", "") or "")
    if not diagnosis_kind:
        diagnosis_kind = str(params.get("diagnosis_kind", "") or "")
    if not module_statement:
        module_statement = str(params.get("module_statement", "") or "")
    if not related_symbols:
        params_rel = params.get("related_symbols", []) or []
        if isinstance(params_rel, list):
            related_symbols = [str(s).strip() for s in params_rel if str(s).strip()]

    # Read headline from context — interact's parse_evaluation step publishes
    # it alongside goal_met/summary from the evaluation JSON. Other flows
    # won't have it; default to empty string and propagate whatever's there.
    headline = ctx.get("headline", "") or ""

    from agent.persistence.models import DirectiveReport

    report = DirectiveReport(
        flow=flow_name,
        status=status,
        summary=summary,
        headline=headline,
        files_affected=files_list,
        checks_passed=checks_passed,
        checks_failed=checks_failed,
        terminal_output=(str(terminal_output) if terminal_output else gate_error),
        recommended_flow=recommended_flow,
        target_file=target_file,
        target_symbol=target_symbol,
        related_symbols=related_symbols,
        change_spec=change_spec,
        diagnosis_kind=diagnosis_kind,
        module_statement=module_statement,
    )

    return _wrap_report(report, flow_name, status)


async def action_build_directive_report(step_input: StepInput) -> StepOutput:
    """Produce a DirectiveReport mechanically from returns data.

    Used by mechanical flow_directive flows (project_ops) where inference
    summarization is unnecessary. Builds the report directly from
    structured context data.

    Context optional: files_changed, setup_result, env_config
    Params: flow_name (str), status (str)
    Publishes: directive_report
    """
    params = step_input.params
    ctx = step_input.context

    flow_name = params.get("flow_name", step_input.meta.flow_name or "unknown")
    status = params.get("status", "success")

    files_changed = ctx.get("files_changed", [])
    setup_result = ctx.get("setup_result", "")

    # Build mechanical summary
    parts = [f"{flow_name} completed with status: {status}"]
    if files_changed:
        if isinstance(files_changed, list):
            parts.append(f"Files: {', '.join(str(f) for f in files_changed)}")
        else:
            parts.append(f"Files: {files_changed}")
    if setup_result:
        parts.append(f"Setup: {setup_result}")

    summary = ". ".join(parts)

    files_list = _coerce_files_list(files_changed)

    from agent.persistence.models import DirectiveReport

    report = DirectiveReport(
        flow=flow_name,
        status=status,
        summary=summary,
        files_affected=files_list,
    )

    return _wrap_report(report, flow_name, status)


# ══════════════════════════════════════════════════════════════════════
# Tier 2: Goal Report Attachment (mission_control side)
# ══════════════════════════════════════════════════════════════════════


def structural_block_reason(goal: Any, checks_failed: list) -> str | None:
    """Why a structural goal must NOT auto-complete yet — or None to complete.

    - ``"syntax"`` — the file doesn't compile. Always blocks.
    - ``"import"`` — the file compiles but fails to import, AND the model hasn't
      yet had a fix-or-defer decision pass (``goal.import_reviewed`` is False).
      Surfacing import failures at the structural gate catches real import bugs
      (relative imports without a package, circular imports, importing a name
      that doesn't exist) where they're cheap to fix, instead of leaking into
      the functional phase as "program won't start". It's bounded to ONE pass:
      once reviewed, an unresolved import is accepted (an expected first-pass
      cross-module import resolves on its own; a genuine residual is caught by
      functional). This avoids looping on imports that can't be fixed yet.
    - lint failures never block (optional).
    """
    if any(c.startswith("syntax:") for c in checks_failed):
        return "syntax"
    if any(c.startswith("import:") for c in checks_failed):
        if not getattr(goal, "import_reviewed", False):
            return "import"
    return None


def _maybe_complete_goal(goal: Any) -> None:
    """Check if a structural goal should be marked complete based on its reports.

    Structural goals complete when file_ops reports success and no blocking
    validation check failed (see structural_block_reason: syntax always blocks;
    a never-reviewed import failure blocks for one decision pass).

    Functional goals are NOT completed here — they are completed by the
    functional sweep in mission_control v9, which invokes interact and
    checks goal_met through the flow's own evaluation step.

    Note: report history is preserved on the goal for retrospectives.
    The projections system (project_director_overview) already excludes
    completed goals' reports from the director's context, so no purging
    is needed here.
    """
    if not hasattr(goal, "reports") or not goal.reports:
        return

    goal_type = getattr(goal, "type", "")
    if goal_type != "structural":
        return  # Only structural goals auto-complete via report check

    last_report = goal.reports[-1]
    report_flow = getattr(last_report, "flow", "")
    report_status = getattr(last_report, "status", "")

    if report_flow == "file_ops" and report_status == "success":
        checks_failed = getattr(last_report, "checks_failed", [])
        if structural_block_reason(goal, checks_failed) is None:
            goal.status = "complete"
            # failed_attempts are NOT cleared here anymore: the archive
            # sweep relocates them to .agent/archive/goals/<id>.jsonl —
            # clearing was the codebase's one data-destruction site and
            # retry patterns are prime behavioral-mining material.
            logger.info(
                "Goal completed: %s (%d reports preserved)",
                getattr(goal, "description", "")[:60],
                len(goal.reports),
            )


async def action_attach_directive_report(step_input: StepInput) -> StepOutput:
    """Attach a DirectiveReport to the goal that was being advanced.

    Runs in mission_control after a flow_directive returns. Reads the
    directive_report from last_result and appends it to the goal's
    reports list. Checks if all goals are complete.

    This replaces action_update_task_status.

    Context required: mission
    Context optional: last_result, last_status, last_goal_id, events
    Publishes: mission
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")

    if not mission:
        return StepOutput(
            result={"needs_plan": True, "events_pending": False},
            observations="No mission in context",
            context_updates={"mission": mission},
        )

    # Check if goals exist
    if not mission.goals:
        return StepOutput(
            result={"needs_plan": True, "events_pending": False},
            observations="Mission has no goals — needs planning",
            context_updates={"mission": mission},
        )

    # Check for pending events
    events = step_input.context.get("events", [])
    if events:
        return StepOutput(
            result={"needs_plan": False, "events_pending": True},
            observations=f"{len(events)} events pending",
            context_updates={"mission": mission},
        )

    last_result = step_input.context.get("last_result", {})
    last_goal_id = step_input.context.get("last_goal_id", "")
    last_status = step_input.context.get("last_status", "")

    # Extract directive_report from last_result
    report_data = None
    if isinstance(last_result, dict):
        report_data = last_result.get("directive_report")

    # Attach report to goal if we have both
    if report_data and last_goal_id:
        from agent.persistence.models import DirectiveReport

        try:
            if isinstance(report_data, dict):
                report = DirectiveReport(**report_data)
            else:
                report = report_data
        except Exception as e:
            logger.warning("Could not parse directive_report: %s", e)
            report = None

        if report:
            from agent.persistence.models import DispatchRecord
            from agent.trace import get_step_context

            for goal in mission.goals:
                if goal.id == last_goal_id:
                    goal.reports.append(report)
                    sc = get_step_context() or {}
                    mission.dispatch_history.append(
                        DispatchRecord(
                            cycle=int(sc.get("cycle", 0) or 0),
                            flow=report.flow,
                            goal_id=last_goal_id,
                            target_file_path=report.target_file
                            or (
                                report.files_affected[0]
                                if report.files_affected
                                else ""
                            ),
                            result_status=report.status,
                        )
                    )
                    logger.info(
                        "Attached report to goal %s: %s (%s)",
                        goal.id,
                        report.flow,
                        report.status,
                    )
                    # ── Goal completion check ──
                    # Structural goals: complete when file_ops reports success
                    # and all validation checks pass.
                    # Functional goals: complete when interact reports goal_met.
                    if goal.status == "incomplete":
                        _maybe_complete_goal(goal)
                    break
            else:
                logger.warning("Goal %s not found — report not attached", last_goal_id)
    elif last_goal_id and not report_data:
        logger.info(
            "No directive_report in last_result for goal %s (status=%s)",
            last_goal_id,
            last_status,
        )

    # Mark environment verified after project_ops runs (success or failure).
    # If setup failed (e.g., pip install error), the startup goal's
    # deterministic test will catch it and route through diagnose_issue,
    # which can recommend another project_ops cycle with better context.
    # Without this, project_ops failure loops forever in the environment phase.
    if report_data and isinstance(report_data, dict):
        if report_data.get("flow") == "project_ops":
            if hasattr(mission, "environment_verified"):
                mission.environment_verified = True
                logger.info(
                    "Environment verified via project_ops (%s)",
                    report_data.get("status"),
                )
            # Workspace ledger (ops port): one durable provision line per
            # project_ops run, so setup planning and diagnose seeds see what
            # the environment already holds instead of re-deriving/re-doing
            # it. The coarse environment_verified flag says "ran once"; the
            # ledger says WHAT happened, per cycle, success or failure.
            if hasattr(mission, "add_ledger_entry"):
                from agent.trace import get_step_context

                sc = get_step_context() or {}
                mission.add_ledger_entry(
                    cycle=int(sc.get("cycle", 0) or 0),
                    kind="provision",
                    description=str(
                        report_data.get("summary") or "project environment setup"
                    ),
                    status=(
                        "success"
                        if report_data.get("status") == "success"
                        else "failed"
                    ),
                )

    # Archive sweep — the single choke point for all goal-completion
    # sites: completed goals' reports/attempts RELOCATE to append-only
    # JSONL (never deleted), rolling notes/dispatch overflow beyond
    # their caps. Runs every cycle in every flow set's control loop.
    if effects:
        try:
            from agent.persistence.archive import archive_mission_overflow

            pm = effects._get_persistence()
            archive_mission_overflow(pm.agent_dir, mission)
        except AttributeError:
            pass  # effects without a persistence dir (mock paths archive in-memory via save)
        except Exception:
            logger.exception("archive sweep failed — records stay in mission.json")

    # Save updated state
    if effects:
        await effects.save_mission(mission)

    # Check goal-level completion (used by check_phase)
    all_goals_complete = all(g.status == "complete" for g in mission.goals)

    return StepOutput(
        result={
            "needs_plan": False,
            "events_pending": False,
            "all_goals_complete": all_goals_complete,
        },
        observations=(
            f"Processed cycle result: goal={last_goal_id if last_goal_id else 'none'}, "
            f"status={last_status}"
            + (", ALL GOALS COMPLETE" if all_goals_complete else "")
        ),
        context_updates={"mission": mission},
    )


# ══════════════════════════════════════════════════════════════════════
# Goal Selection (replaces select_task_for_dispatch)
# ══════════════════════════════════════════════════════════════════════
