"""YAML parser and validator for flow definitions.

Loads flow YAML files, validates them with Pydantic models, and performs
semantic validation (transition targets exist, context key reachability,
terminal states reachable, etc.)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent.models import FlowDefinition


class FlowLoadError(Exception):
    """Raised when a flow definition fails to load or validate."""

    pass


class FlowValidationError(FlowLoadError):
    """Raised when a flow definition fails semantic validation."""

    pass


def load_flow(path: str | Path) -> FlowDefinition:
    """Load and validate a flow definition from a YAML file.

    Performs two levels of validation:
    1. Structural: Pydantic model validation (types, required fields).
    2. Semantic: Graph validation (transitions, context keys, terminals).

    Args:
        path: Path to the YAML flow definition file.

    Returns:
        A validated FlowDefinition.

    Raises:
        FlowLoadError: If the file can't be read or parsed.
        FlowValidationError: If semantic validation fails.
    """
    path = Path(path)

    if not path.exists():
        raise FlowLoadError(f"Flow file not found: {path}")

    if not path.suffix in (".yaml", ".yml"):
        raise FlowLoadError(f"Flow file must be .yaml or .yml: {path}")

    # Parse YAML
    try:
        with open(path, "r") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise FlowLoadError(f"YAML parse error in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise FlowLoadError(
            f"Flow file must contain a YAML mapping, got {type(raw).__name__}"
        )

    # Structural validation via Pydantic
    try:
        flow = FlowDefinition(**raw)
    except ValidationError as e:
        raise FlowLoadError(f"Flow definition validation error in {path}:\n{e}") from e

    # Semantic validation
    _validate_semantics(flow, path)

    return flow


def load_flow_from_dict(data: dict[str, Any]) -> FlowDefinition:
    """Load and validate a flow definition from a dictionary.

    Useful for testing — same validation as load_flow but without file I/O.

    Args:
        data: Dictionary matching the flow YAML structure.

    Returns:
        A validated FlowDefinition.

    Raises:
        FlowLoadError: If structural validation fails.
        FlowValidationError: If semantic validation fails.
    """
    try:
        flow = FlowDefinition(**data)
    except ValidationError as e:
        raise FlowLoadError(f"Flow definition validation error:\n{e}") from e

    _validate_semantics(flow, source="<dict>")
    return flow


def load_all_flows(directory: str | Path) -> dict[str, FlowDefinition]:
    """Load all flow definitions from a directory.

    Scans for .yaml and .yml files, loads each one.
    Skips registry.yaml.

    Args:
        directory: Path to the flows directory.

    Returns:
        Dictionary mapping flow names to FlowDefinition objects.

    Raises:
        FlowLoadError: If the directory doesn't exist or any flow fails to load.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise FlowLoadError(f"Flows directory not found: {directory}")

    flows: dict[str, FlowDefinition] = {}

    for file_path in sorted(directory.iterdir()):
        if file_path.suffix not in (".yaml", ".yml"):
            continue
        if file_path.name == "registry.yaml":
            continue

        flow = load_flow(file_path)
        if flow.flow in flows:
            raise FlowLoadError(
                f"Duplicate flow name {flow.flow!r}: "
                f"found in both {flows[flow.flow]} and {file_path}"
            )
        flows[flow.flow] = flow

    return flows


# ── Semantic Validation ───────────────────────────────────────────────


def _validate_semantics(flow: FlowDefinition, source: str | Path = "<unknown>") -> None:
    """Perform semantic validation on a flow definition.

    Checks:
    1. All transition targets reference existing steps.
    2. At least one reachable terminal state exists.
    3. Required context keys have upstream publishers (best-effort).
    4. Non-terminal steps without resolvers are flagged.

    Args:
        flow: The flow definition to validate.
        source: The source file/identifier for error messages.

    Raises:
        FlowValidationError: If any semantic check fails.
    """
    errors: list[str] = []
    step_names = set(flow.steps.keys())

    # Check 1: All transition targets reference existing steps
    for step_name, step_def in flow.steps.items():
        if step_def.resolver:
            for rule in step_def.resolver.rules:
                if rule.transition not in step_names:
                    errors.append(
                        f"Step {step_name!r}: transition target "
                        f"{rule.transition!r} not found in steps."
                    )
            # Check llm_menu options with explicit targets
            if step_def.resolver.options:
                for opt_name, opt_def in step_def.resolver.options.items():
                    if isinstance(opt_def, dict):
                        target = opt_def.get("target")
                        if target and target not in step_names:
                            errors.append(
                                f"Step {step_name!r}: option {opt_name!r} "
                                f"target {target!r} not found in steps."
                            )

    # Check 2: At least one exit point exists (terminal or tail_call)
    terminal_steps = [name for name, step in flow.steps.items() if step.terminal]
    tail_call_steps = [
        name for name, step in flow.steps.items() if step.tail_call and not step.terminal
    ]
    exit_steps = terminal_steps + tail_call_steps
    if not exit_steps:
        errors.append("Flow has no terminal or tail-call steps — execution can never end.")

    # Check 3: Exit steps are reachable from entry
    reachable = _find_reachable_steps(flow)
    reachable_exits = [s for s in exit_steps if s in reachable]
    if exit_steps and not reachable_exits:
        errors.append(
            f"No exit steps are reachable from entry step {flow.entry!r}. "
            f"Exit steps: {exit_steps}. Reachable steps: {sorted(reachable)}."
        )

    # Check 4: Non-exit steps should have resolvers
    # (tail_call steps exit via tail call, so they don't need resolvers)
    for step_name, step_def in flow.steps.items():
        if not step_def.terminal and not step_def.tail_call and not step_def.resolver:
            errors.append(
                f"Step {step_name!r}: non-terminal step has no resolver — "
                f"execution will have no way to determine the next step."
            )

    # Check 5: Context key reachability (best-effort)
    _validate_context_keys(flow, errors)

    if errors:
        error_list = "\n  - ".join(errors)
        raise FlowValidationError(
            f"Semantic validation failed for flow {flow.flow!r} "
            f"(source: {source}):\n  - {error_list}"
        )


def _find_reachable_steps(flow: FlowDefinition) -> set[str]:
    """Find all steps reachable from the entry step via BFS."""
    visited: set[str] = set()
    queue = [flow.entry]

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        step = flow.steps.get(current)
        if not step:
            continue

        # Follow resolver transitions
        if step.resolver:
            for rule in step.resolver.rules:
                if rule.transition not in visited:
                    queue.append(rule.transition)
            # Follow option targets
            if step.resolver.options:
                for opt_name, opt_def in step.resolver.options.items():
                    if isinstance(opt_def, dict):
                        target = opt_def.get("target")
                        if target and target not in visited:
                            queue.append(target)
                    # Option names that match step names are implicit targets
                    if opt_name in flow.steps and opt_name not in visited:
                        queue.append(opt_name)

    return visited


def _validate_context_keys(flow: FlowDefinition, errors: list[str]) -> None:
    """Best-effort validation that required context keys have upstream publishers.

    This is a static check — it walks the graph and tracks which keys
    each step publishes, then checks if downstream required keys are covered.
    Since the actual execution path is dynamic, this can only catch obvious
    issues (e.g., a key required by step B is never published by any step
    that could precede B).
    """
    # Build map of what each step publishes
    publishers: dict[str, list[str]] = {}  # key → list of step names that publish it
    for step_name, step_def in flow.steps.items():
        for key in step_def.publishes:
            publishers.setdefault(key, []).append(step_name)

    # Also, flow inputs are implicitly available
    available_from_input = set(flow.input.required + flow.input.optional)

    # Check each step's required context keys
    for step_name, step_def in flow.steps.items():
        for required_key in step_def.context.required:
            if required_key in available_from_input:
                continue
            if required_key not in publishers:
                errors.append(
                    f"Step {step_name!r}: requires context key {required_key!r} "
                    f"but no step publishes it and it's not a flow input."
                )
