"""Action registry — register, lookup, and execute actions.

Actions are async callables with signature (StepInput) -> StepOutput.
The registry maps action names (referenced in flow YAML) to their implementations.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

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


async def action_write_file(step_input: StepInput) -> StepOutput:
    """Write content to a file via the effects interface.

    Requires effects to be available.

    Params:
        path: Path to write to.
        content: Content to write (or read from context key 'content_key').
        content_key: Context key containing the content to write.

    Result:
        write_success: bool
    """
    path = step_input.params.get("path", "")
    content = step_input.params.get("content", "")

    # Allow content to come from a context key
    content_key = step_input.params.get("content_key")
    if content_key and content_key in step_input.context:
        content = step_input.context[content_key]

    if not path:
        return StepOutput(
            result={"write_success": False},
            observations="No path provided for write_file.",
        )

    if step_input.effects is None:
        return StepOutput(
            result={"write_success": False},
            observations="No effects interface available for write_file.",
        )

    wr = await step_input.effects.write_file(path, content)
    return StepOutput(
        result={"write_success": wr.success},
        observations=(
            f"Wrote {wr.bytes_written} bytes to {path}"
            if wr.success
            else f"Write failed: {wr.error}"
        ),
        context_updates={"write_result": {"path": path, "success": wr.success}},
    )


async def action_transform(step_input: StepInput) -> StepOutput:
    """Passthrough/transform action for testing.

    Copies specified context keys to output, optionally with transformations.

    Params:
        pass_through: list of context keys to copy to context_updates.
        set_values: dict of key→value pairs to add to context_updates.
    """
    context_updates = {}

    # Pass through specified context keys
    pass_through = step_input.params.get("pass_through", [])
    for key in pass_through:
        if key in step_input.context:
            context_updates[key] = step_input.context[key]

    # Set explicit values
    set_values = step_input.params.get("set_values", {})
    context_updates.update(set_values)

    return StepOutput(
        result={"transformed": True},
        observations=f"Passed through {len(pass_through)} keys, set {len(set_values)} values.",
        context_updates=context_updates,
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


async def action_check_condition(step_input: StepInput) -> StepOutput:
    """Evaluates a simple condition from params and returns the result.

    Params:
        field: The context key to check.
        expected: The expected value.

    Result:
        condition_met: bool
    """
    field = step_input.params.get("field", "")
    expected = step_input.params.get("expected")
    actual = step_input.context.get(field)

    return StepOutput(
        result={"condition_met": actual == expected},
        observations=f"Checked {field}: actual={actual!r}, expected={expected!r}",
        context_updates={},
    )


def build_action_registry() -> ActionRegistry:
    """Create an ActionRegistry pre-loaded with built-in actions.

    v2: Adds new LLM menu-based dispatch actions, removes dead heuristic
    matchers. Keeps all working actions from research, integration,
    diagnostic, terminal, and AST modules.

    Returns:
        An ActionRegistry with all built-in actions registered.
    """
    # ── Mission control v2 actions ────────────────────────────────
    from agent.actions.mission_actions import (
        action_load_mission_state,
        action_update_task_status,
        action_handle_events,
        action_finalize_mission,
        action_enter_idle,
        action_execute_file_creation,
        action_run_tests,
        # NEW v2 actions
        action_select_task_for_dispatch,
        action_select_target_file,
        action_start_director_session,
        action_end_director_session,
        action_record_dispatch,
        action_parse_and_store_architecture,
        action_create_plan_from_architecture,
    )

    # ── Diagnostic actions ────────────────────────────────────────
    from agent.actions.diagnostic_actions import (
        action_compile_diagnosis,
        action_create_fix_task_from_diagnosis,
        action_read_investigation_targets,
    )

    # ── Integration actions ───────────────────────────────────────
    from agent.actions.integration_actions import (
        action_apply_multi_file_changes,
        action_run_project_tests,
        action_check_remaining_smells,
        action_restore_file_from_context,
        action_check_remaining_doc_tasks,
        action_compile_integration_report,
    )

    # ── Retrospective actions ─────────────────────────────────────
    from agent.actions.retrospective_actions import (
        action_load_retrospective_data,
        action_apply_retrospective_recommendations,
        action_compose_director_report,
        action_submit_review_to_api,
    )

    # ── Research actions ──────────────────────────────────────────
    from agent.actions.research_actions import (
        action_build_and_query_repomap,
        action_run_git_investigation,
        action_format_technical_query,
        action_validate_cross_file_consistency,
        action_select_relevant_files,
    )

    # ── Terminal session actions ───────────────────────────────────
    from agent.actions.terminal_actions import (
        action_start_terminal_session,
        action_send_terminal_command,
        action_close_terminal_session,
    )

    # ── AST-aware editing actions ─────────────────────────────────
    from agent.actions.ast_actions import (
        action_extract_symbol_bodies,
        action_start_edit_session,
        action_select_symbol_turn,
        action_prepare_next_rewrite,
        action_rewrite_symbol_turn,
        action_finalize_edit_session,
        action_close_edit_session,
    )

    # ── Refinement actions (trimmed — removed fallback validation) ─
    from agent.actions.refinement_actions import (
        action_push_note,
        action_scan_project,
        action_extract_search_queries,
        action_curl_search,
        action_run_validation_checks,
        action_load_file_contents,
        action_apply_plan_revision,
        action_log_validation_notes,
        action_execute_project_setup,
        action_apply_quality_gate_results,
        action_validate_created_files,
    )

    registry = ActionRegistry()

    # ── Core built-in actions ─────────────────────────────────────
    registry.register("read_files", action_read_files)
    registry.register("write_file", action_write_file)
    registry.register("transform", action_transform)
    registry.register("log_completion", action_log_completion)
    registry.register("noop", action_noop)
    registry.register("check_condition", action_check_condition)

    # ── Mission control v2 ────────────────────────────────────────
    registry.register("load_mission_state", action_load_mission_state)
    registry.register("update_task_status", action_update_task_status)
    registry.register("handle_events", action_handle_events)
    registry.register("finalize_mission", action_finalize_mission)
    registry.register("enter_idle", action_enter_idle)
    # NEW: memoryful director session
    registry.register("start_director_session", action_start_director_session)
    registry.register("end_director_session", action_end_director_session)
    # NEW: LLM menu-based dispatch (replaces configure_task_dispatch)
    registry.register("select_task_for_dispatch", action_select_task_for_dispatch)
    registry.register("select_target_file", action_select_target_file)
    registry.register("record_dispatch", action_record_dispatch)
    # NEW: architecture state management
    registry.register(
        "parse_and_store_architecture", action_parse_and_store_architecture
    )
    registry.register(
        "create_plan_from_architecture", action_create_plan_from_architecture
    )

    # ── File operations ───────────────────────────────────────────
    registry.register("execute_file_creation", action_execute_file_creation)
    registry.register("run_tests", action_run_tests)

    # ── Refinement (trimmed) ──────────────────────────────────────
    registry.register("push_note", action_push_note)
    registry.register("scan_project", action_scan_project)
    registry.register("extract_search_queries", action_extract_search_queries)
    registry.register("curl_search", action_curl_search)
    registry.register("run_validation_checks", action_run_validation_checks)
    registry.register("load_file_contents", action_load_file_contents)
    registry.register("apply_plan_revision", action_apply_plan_revision)
    registry.register("log_validation_notes", action_log_validation_notes)
    registry.register("execute_project_setup", action_execute_project_setup)
    registry.register("apply_quality_gate_results", action_apply_quality_gate_results)
    registry.register("validate_created_files", action_validate_created_files)
    # NOTE: run_fallback_validation REMOVED — use validate_created_files instead
    # NOTE: accumulate_correction_history REMOVED — create_file no longer has correction loop

    # ── Diagnostic actions ────────────────────────────────────────
    registry.register("compile_diagnosis", action_compile_diagnosis)
    registry.register(
        "create_fix_task_from_diagnosis", action_create_fix_task_from_diagnosis
    )
    registry.register("read_investigation_targets", action_read_investigation_targets)

    # ── Integration actions ───────────────────────────────────────
    registry.register("apply_multi_file_changes", action_apply_multi_file_changes)
    registry.register("run_project_tests", action_run_project_tests)
    registry.register("check_remaining_smells", action_check_remaining_smells)
    registry.register("restore_file_from_context", action_restore_file_from_context)
    registry.register("check_remaining_doc_tasks", action_check_remaining_doc_tasks)
    registry.register("compile_integration_report", action_compile_integration_report)

    # ── Retrospective actions ─────────────────────────────────────
    registry.register("load_retrospective_data", action_load_retrospective_data)
    registry.register(
        "apply_retrospective_recommendations",
        action_apply_retrospective_recommendations,
    )
    registry.register("compose_director_report", action_compose_director_report)
    registry.register("submit_review_to_api", action_submit_review_to_api)

    # ── Research actions ──────────────────────────────────────────
    registry.register("build_and_query_repomap", action_build_and_query_repomap)
    registry.register("run_git_investigation", action_run_git_investigation)
    registry.register("format_technical_query", action_format_technical_query)
    registry.register(
        "validate_cross_file_consistency", action_validate_cross_file_consistency
    )
    registry.register("select_relevant_files", action_select_relevant_files)

    # ── Terminal session actions ───────────────────────────────────
    registry.register("start_terminal_session", action_start_terminal_session)
    registry.register("send_terminal_command", action_send_terminal_command)
    registry.register("close_terminal_session", action_close_terminal_session)

    # ── AST-aware editing actions ─────────────────────────────────
    registry.register("extract_symbol_bodies", action_extract_symbol_bodies)
    registry.register("start_edit_session", action_start_edit_session)
    registry.register("select_symbol_turn", action_select_symbol_turn)
    registry.register("prepare_next_rewrite", action_prepare_next_rewrite)
    registry.register("rewrite_symbol_turn", action_rewrite_symbol_turn)
    registry.register("finalize_edit_session", action_finalize_edit_session)
    registry.register("close_edit_session", action_close_edit_session)

    # ── CUE Migration: New Actions ─────────────────────────────────
    from agent.actions.pipeline_actions import (
        action_lookup_validation_env,
        action_run_validation_checks_from_env,
        action_persist_validation_env,
        action_check_retry_budget,
        action_git_log_summary,
        action_log_validation_notes,
    )

    registry.register("lookup_validation_env", action_lookup_validation_env)
    registry.register("run_validation_checks_from_env", action_run_validation_checks_from_env)
    registry.register("persist_validation_env", action_persist_validation_env)
    registry.register("check_retry_budget", action_check_retry_budget)
    registry.register("git_log_summary", action_git_log_summary)
    registry.register("log_validation_notes", action_log_validation_notes)

    return registry
