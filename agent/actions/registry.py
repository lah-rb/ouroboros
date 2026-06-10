"""Action registry — register, lookup, and execute actions.

Actions are async callables with signature (StepInput) -> StepOutput.
The registry maps action names (referenced in flow YAML) to their implementations.
"""

from __future__ import annotations

import os
from typing import Awaitable, Callable

from agent.models import StepInput, StepOutput

# Type alias for action callables
ActionCallable = Callable[[StepInput], Awaitable[StepOutput]]


class ActionNotFoundError(Exception):
    """Raised when a requested action is not in the registry."""

    pass


class ActionRegistry:
    """Registry mapping action names to async callable implementations.

    Actions are referenced by name in flow YAML step definitions.
    The registry provides lookup and execution.
    """

    def __init__(self) -> None:
        self._actions: dict[str, ActionCallable] = {}

    def register(self, name: str, action: ActionCallable) -> None:
        """Register an action callable under a name.

        Args:
            name: The action name as referenced in flow YAML.
            action: An async callable with signature (StepInput) -> StepOutput.
        """
        self._actions[name] = action

    def get(self, name: str) -> ActionCallable:
        """Look up an action by name.

        Args:
            name: The action name.

        Returns:
            The registered action callable.

        Raises:
            ActionNotFoundError: If no action is registered under that name.
        """
        if name not in self._actions:
            raise ActionNotFoundError(
                f"Action {name!r} not found. "
                f"Registered actions: {list(self._actions.keys())}"
            )
        return self._actions[name]

    def has(self, name: str) -> bool:
        """Check if an action is registered."""
        return name in self._actions

    @property
    def registered_actions(self) -> list[str]:
        """List all registered action names."""
        return list(self._actions.keys())


# ── Built-in Actions ──────────────────────────────────────────────────


async def action_read_files(step_input: StepInput) -> StepOutput:
    """Read a file via the effects interface, falling back to direct I/O.

    Uses step_input.effects.read_file() when effects are available.
    Falls back to direct os.path / open() for backward compatibility.

    Params:
        target: Path to the file to read.
        discover_imports: If true, attempt basic import discovery (stub).

    Publishes:
        target_file: dict with 'path' and 'content' keys.
        related_files: list (empty for now).

    Result:
        file_found: bool indicating whether the file exists.
    """
    target = step_input.params.get("target", "")

    if not target:
        return StepOutput(
            result={"file_found": False},
            observations="No target file path provided.",
            context_updates={},
        )

    # Use effects interface if available
    if step_input.effects is not None:
        fc = await step_input.effects.read_file(target)
        if fc.exists:
            return StepOutput(
                result={"file_found": True},
                observations=f"Read {fc.size} characters from {target}",
                context_updates={
                    "target_file": {"path": fc.path, "content": fc.content},
                    "related_files": [],
                },
            )
        else:
            return StepOutput(
                result={"file_found": False},
                observations=f"File not found: {target}",
                context_updates={},
            )

    # Fallback: direct I/O (Phase 1 compat)
    try:
        if os.path.exists(target):
            with open(target, "r") as f:
                content = f.read()
            return StepOutput(
                result={"file_found": True},
                observations=f"Read {len(content)} characters from {target}",
                context_updates={
                    "target_file": {"path": target, "content": content},
                    "related_files": [],
                },
            )
        else:
            return StepOutput(
                result={"file_found": False},
                observations=f"File not found: {target}",
                context_updates={},
            )
    except Exception as e:
        return StepOutput(
            result={"file_found": False},
            observations=f"Error reading {target}: {e}",
            context_updates={},
        )


async def action_log_completion(step_input: StepInput) -> StepOutput:
    """Terminal action that logs completion and produces a summary.

    Publishes:
        summary: A summary string of the flow execution.
    """
    summary = step_input.params.get("message", "Flow completed successfully.")

    return StepOutput(
        result={"completed": True},
        observations=summary,
        context_updates={
            "summary": summary,
        },
    )


async def action_noop(step_input: StepInput) -> StepOutput:
    """No-op action — does nothing, returns empty result.

    Useful for testing transitions without side effects.
    """
    return StepOutput(
        result={},
        observations="No-op action executed.",
        context_updates={},
    )


def build_action_registry() -> ActionRegistry:
    """Create an ActionRegistry pre-loaded with built-in actions.

    Returns:
        An ActionRegistry with all built-in actions registered.
    """
    # ── Mission control ───────────────────────────────────────────
    from agent.actions.mission_actions import (
        action_load_mission_state,
        action_handle_events,
        action_finalize_mission,
        action_enter_idle,
        action_check_architecture_drift,
        action_parse_and_store_architecture,
        # Context Contract Architecture
        action_derive_project_goals,
        # Pipeline v9 actions
        action_check_pipeline_phase,
        action_structural_sweep_next,
        action_functional_sweep_next,
        action_harvest_quality_findings,
        action_quality_sweep_next,
        # Fix target resolution
        action_apply_fix_target,
    )

    # ── Diagnostic actions ────────────────────────────────────────
    from agent.actions.diagnostic_actions import (
        action_compile_diagnosis,
        action_create_fix_task_from_diagnosis,
    )

    # ── Integration actions ───────────────────────────────────────
    from agent.actions.integration_actions import (
        action_apply_multi_file_changes,
    )

    # ── Research actions ──────────────────────────────────────────
    from agent.actions.research_actions import (
        action_build_and_query_repomap,
        action_validate_cross_file_consistency,
    )

    # ── Interactive terminal actions (MCP-based) ────────────────────
    from agent.actions.interactive_actions import (
        action_start_interactive_session,
        action_send_interaction,
        action_close_interactive_session,
        action_flush_transient_files,
        action_relaunch_program,
        action_execute_commands_batch_mcp,
        action_end_inference_session,
    )

    # ── AST-aware editing actions ─────────────────────────────────
    from agent.actions.ast_actions import (
        action_extract_symbol_bodies,
        action_start_edit_session,
        action_select_symbol_turn,
        action_prepare_next_rewrite,
        action_load_next_file,
        action_write_patched_file,
        action_build_call_graph,
        action_rewrite_symbol_turn,
        action_capture_bail_turn,
        action_finalize_edit_session,
        action_close_edit_session,
        action_insert_new_symbol,
        action_prepare_insert_context,
    )

    # ── Data-file surgical patching (data_ops) ───────────────────
    from agent.actions.data_ops_actions import (
        action_apply_data_ops,
        action_translate_data_ops_turn,
    )

    # ── Module-frame editor (import trigger + frame edit) ─────────
    from agent.actions.frame_actions import (
        action_check_import_fix,
        action_prepare_frame,
        action_rewrite_frame_turn,
        action_splice_frame,
    )

    # ── Refinement actions ────────────────────────────────────────
    from agent.actions.refinement_actions import (
        action_push_note,
        action_scan_project,
        action_extract_search_queries,
        action_exa_search,
        action_run_validation_checks,
        action_log_validation_notes,
        action_execute_project_setup,
        action_apply_quality_gate_results,
    )

    registry = ActionRegistry()

    # ── Core built-in actions ─────────────────────────────────────
    registry.register("read_files", action_read_files)
    registry.register("log_completion", action_log_completion)
    registry.register("noop", action_noop)

    # ── Mission control ────────────────────────────────────────────
    registry.register("load_mission_state", action_load_mission_state)
    registry.register("handle_events", action_handle_events)
    registry.register("finalize_mission", action_finalize_mission)
    registry.register("enter_idle", action_enter_idle)
    # Memoryful director session
    # Architecture state management
    registry.register("check_architecture_drift", action_check_architecture_drift)
    registry.register(
        "parse_and_store_architecture", action_parse_and_store_architecture
    )
    # Goal derivation
    registry.register("derive_project_goals", action_derive_project_goals)
    # Creation order sweep (legacy, delegates to structural_sweep_next)
    # Pipeline v9 actions
    registry.register("check_pipeline_phase", action_check_pipeline_phase)
    registry.register("structural_sweep_next", action_structural_sweep_next)
    registry.register("functional_sweep_next", action_functional_sweep_next)
    registry.register("harvest_quality_findings", action_harvest_quality_findings)
    registry.register("quality_sweep_next", action_quality_sweep_next)
    # Fix target resolution — menu assembly moved to fix_target_menu projection
    registry.register("apply_fix_target", action_apply_fix_target)

    # ── File operations ───────────────────────────────────────────

    # ── Refinement ────────────────────────────────────────────────
    registry.register("push_note", action_push_note)
    registry.register("scan_project", action_scan_project)
    registry.register("extract_search_queries", action_extract_search_queries)
    registry.register("exa_search", action_exa_search)
    registry.register("run_validation_checks", action_run_validation_checks)
    registry.register("log_validation_notes", action_log_validation_notes)
    registry.register("execute_project_setup", action_execute_project_setup)
    registry.register("apply_quality_gate_results", action_apply_quality_gate_results)

    # ── Diagnostic actions ────────────────────────────────────────
    registry.register("compile_diagnosis", action_compile_diagnosis)
    registry.register(
        "create_fix_task_from_diagnosis", action_create_fix_task_from_diagnosis
    )

    # ── Integration actions ───────────────────────────────────────
    registry.register("apply_multi_file_changes", action_apply_multi_file_changes)

    # ── Research actions ──────────────────────────────────────────
    registry.register("build_and_query_repomap", action_build_and_query_repomap)
    registry.register(
        "validate_cross_file_consistency", action_validate_cross_file_consistency
    )

    # ── Interactive terminal actions (MCP-based) ────────────────────
    registry.register("start_interactive_session", action_start_interactive_session)
    registry.register("send_interaction", action_send_interaction)
    registry.register("close_interactive_session", action_close_interactive_session)
    registry.register("flush_transient_files", action_flush_transient_files)
    registry.register("relaunch_program", action_relaunch_program)
    registry.register("execute_commands_batch", action_execute_commands_batch_mcp)
    registry.register("end_inference_session", action_end_inference_session)

    # ── AST-aware editing actions ─────────────────────────────────
    registry.register("extract_symbol_bodies", action_extract_symbol_bodies)
    registry.register("start_edit_session", action_start_edit_session)
    registry.register("select_symbol_turn", action_select_symbol_turn)
    registry.register("prepare_next_rewrite", action_prepare_next_rewrite)
    registry.register("load_next_file", action_load_next_file)
    registry.register("write_patched_file", action_write_patched_file)
    registry.register("build_call_graph", action_build_call_graph)
    registry.register("insert_new_symbol", action_insert_new_symbol)
    registry.register("prepare_insert_context", action_prepare_insert_context)
    registry.register("rewrite_symbol_turn", action_rewrite_symbol_turn)
    registry.register("capture_bail_turn", action_capture_bail_turn)
    registry.register("finalize_edit_session", action_finalize_edit_session)
    registry.register("close_edit_session", action_close_edit_session)

    # ── Data-file surgical patching (data_ops) ────────────────────
    registry.register("translate_data_ops_turn", action_translate_data_ops_turn)
    registry.register("apply_data_ops", action_apply_data_ops)

    # ── Module-frame editor ───────────────────────────────────────
    registry.register("check_import_fix", action_check_import_fix)
    registry.register("prepare_frame", action_prepare_frame)
    registry.register("rewrite_frame_turn", action_rewrite_frame_turn)
    registry.register("splice_frame", action_splice_frame)

    # ── Pipeline actions ──────────────────────────────────────────
    from agent.actions.pipeline_actions import (
        action_lookup_validation_env,
        action_run_validation_checks_from_env,
        action_check_data_file,
        action_persist_validation_env,
        action_log_validation_notes,
        # A1: Dependency coverage check
        action_check_dependency_coverage,
        action_parse_dep_check_result,
        action_collect_env_field,
        action_parse_inference_json,
    )

    registry.register("lookup_validation_env", action_lookup_validation_env)
    registry.register(
        "run_validation_checks_from_env", action_run_validation_checks_from_env
    )
    registry.register("check_data_file", action_check_data_file)
    registry.register("persist_validation_env", action_persist_validation_env)
    registry.register("log_validation_notes", action_log_validation_notes)
    # A1: Dependency coverage check
    registry.register("check_dependency_coverage", action_check_dependency_coverage)
    registry.register("parse_dep_check_result", action_parse_dep_check_result)
    registry.register("collect_env_field", action_collect_env_field)
    registry.register("parse_inference_json", action_parse_inference_json)

    # Deterministic evaluation (interact flow — run_commands path)
    from agent.actions.pipeline_actions import action_evaluate_deterministic_result

    registry.register(
        "evaluate_deterministic_result", action_evaluate_deterministic_result
    )

    # ── Tier Records: Reporting Chain ────────────────────────────────
    from agent.actions.reporting_actions import (
        action_compile_directive_report,
        action_build_directive_report,
        action_attach_directive_report,
    )

    registry.register("compile_directive_report", action_compile_directive_report)
    registry.register("build_directive_report", action_build_directive_report)
    registry.register("attach_directive_report", action_attach_directive_report)

    # ── Diagnosis session actions (v12: trace-and-conclude + systemic scan) ─────
    from agent.actions.diagnosis_session_actions import (
        action_start_diagnosis_session,
        action_execute_symbol_trace,
        action_conclude_diagnosis,
        action_systemic_scan,
    )

    registry.register("start_diagnosis_session", action_start_diagnosis_session)
    registry.register("execute_symbol_trace", action_execute_symbol_trace)
    registry.register("conclude_diagnosis", action_conclude_diagnosis)
    registry.register("systemic_scan", action_systemic_scan)

    return registry
