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

    Returns:
        An ActionRegistry with all built-in actions registered.
    """
    from agent.actions.mission_actions import (
        action_load_mission_state,
        action_update_task_status,
        action_handle_events,
        action_assess_mission_progress,
        action_configure_task_dispatch,
        action_finalize_mission,
        action_enter_idle,
        action_create_plan_from_objective,
        action_execute_file_creation,
        action_run_tests,
    )

    registry = ActionRegistry()
    # Core actions
    registry.register("read_files", action_read_files)
    registry.register("write_file", action_write_file)
    registry.register("transform", action_transform)
    registry.register("log_completion", action_log_completion)
    registry.register("noop", action_noop)
    registry.register("check_condition", action_check_condition)
    # Mission control actions
    registry.register("load_mission_state", action_load_mission_state)
    registry.register("update_task_status", action_update_task_status)
    registry.register("handle_events", action_handle_events)
    registry.register("assess_mission_progress", action_assess_mission_progress)
    registry.register("configure_task_dispatch", action_configure_task_dispatch)
    registry.register("finalize_mission", action_finalize_mission)
    registry.register("enter_idle", action_enter_idle)
    # Plan and file creation actions
    registry.register("create_plan_from_objective", action_create_plan_from_objective)
    registry.register("execute_file_creation", action_execute_file_creation)
    registry.register("run_tests", action_run_tests)
    return registry
