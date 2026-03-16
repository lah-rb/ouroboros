"""YAML parser and validator for flow definitions.

Loads flow YAML files, validates them with Pydantic models, and performs
semantic validation (transition targets exist, context key reachability,
terminal states reachable, etc.)

Includes step template expansion: steps declaring `use: template_name`
are expanded against a StepTemplateRegistry before validation.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent.models import (
    FlowDefinition,
    ParamSchemaEntry,
    StepTemplate,
    StepTemplateRegistry,
)


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
    """Load all flow definitions from a directory and its subdirectories.

    Scans for .yaml and .yml files, loads each one.
    Skips registry.yaml and step_templates.yaml.
    Recurses into shared/ and tasks/ subdirectories.

    If a step_templates.yaml exists in shared/, loads it and uses
    template expansion when loading flows.

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

    # Load template registry if available
    template_registry = load_template_registry(str(directory))

    flows: dict[str, FlowDefinition] = {}
    skip_names = {"registry.yaml", "step_templates.yaml"}

    def _scan_dir(scan_path: Path) -> None:
        if not scan_path.is_dir():
            return
        for file_path in sorted(scan_path.iterdir()):
            if file_path.is_dir():
                # Recurse into subdirectories (shared/, tasks/)
                _scan_dir(file_path)
                continue
            if file_path.suffix not in (".yaml", ".yml"):
                continue
            if file_path.name in skip_names:
                continue

            if template_registry.templates:
                flow = load_flow_with_templates(str(file_path), template_registry)
            else:
                flow = load_flow(file_path)

            if flow.flow in flows:
                raise FlowLoadError(
                    f"Duplicate flow name {flow.flow!r}: "
                    f"found in both {flows[flow.flow]} and {file_path}"
                )
            flows[flow.flow] = flow

    _scan_dir(directory)
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
        name
        for name, step in flow.steps.items()
        if step.tail_call and not step.terminal
    ]
    exit_steps = terminal_steps + tail_call_steps
    if not exit_steps:
        errors.append(
            "Flow has no terminal or tail-call steps — execution can never end."
        )

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


# ── Step Template System ──────────────────────────────────────────────


def load_template_registry(flows_dir: str) -> StepTemplateRegistry:
    """Load step_templates.yaml from the shared directory.

    Args:
        flows_dir: Path to the flows directory.

    Returns:
        A StepTemplateRegistry (empty if no templates file exists).
    """
    templates_path = os.path.join(flows_dir, "shared", "step_templates.yaml")
    if not os.path.exists(templates_path):
        return StepTemplateRegistry(templates={})
    try:
        with open(templates_path, "r") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict):
            return StepTemplateRegistry(templates={})
        return StepTemplateRegistry(**raw)
    except Exception as e:
        raise FlowLoadError(
            f"Failed to load step templates from {templates_path}: {e}"
        ) from e


def load_flow_with_templates(
    flow_path: str,
    template_registry: StepTemplateRegistry,
) -> FlowDefinition:
    """Load a flow YAML, expanding step templates before validation.

    Steps that declare `use: template_name` are expanded against the
    template registry using merge semantics, then validated normally.

    Args:
        flow_path: Path to the flow YAML file.
        template_registry: The loaded StepTemplateRegistry.

    Returns:
        A validated FlowDefinition with templates expanded.
    """
    path = Path(flow_path)

    if not path.exists():
        raise FlowLoadError(f"Flow file not found: {path}")

    if path.suffix not in (".yaml", ".yml"):
        raise FlowLoadError(f"Flow file must be .yaml or .yml: {path}")

    try:
        with open(path, "r") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise FlowLoadError(f"YAML parse error in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise FlowLoadError(
            f"Flow file must contain a YAML mapping, got {type(raw).__name__}"
        )

    # Template expansion pass
    for step_name, step_def in raw.get("steps", {}).items():
        if not isinstance(step_def, dict):
            continue
        if "use" not in step_def:
            continue

        template_name = step_def.pop("use")
        template = template_registry.templates.get(template_name)
        if template is None:
            raise FlowValidationError(
                f"Step '{step_name}' references unknown template '{template_name}'"
            )

        merged = _merge_step_with_template(template, step_def)
        validate_params_against_schema(
            merged.get("params", {}),
            template.param_schema,
            step_name,
            template_name,
        )
        raw["steps"][step_name] = merged

    # Structural validation via Pydantic
    try:
        flow = FlowDefinition(**raw)
    except ValidationError as e:
        raise FlowLoadError(f"Flow definition validation error in {path}:\n{e}") from e

    # Semantic validation
    _validate_semantics(flow, path)

    return flow


def _merge_step_with_template(template: StepTemplate, step_overrides: dict) -> dict:
    """Apply merge semantics to produce a fully expanded step definition.

    REPLACE — step value wins entirely:
      action, description, flow, input_map, publishes

    DEEP MERGE — step values overlay template values:
      context (union of lists), params (step overrides keys), config (same)

    ALWAYS FROM STEP — template never carries:
      resolver, terminal, status, tail_call
    """
    merged: dict[str, Any] = {}

    # REPLACE fields: template provides defaults, step wins entirely
    for field in ("action", "description", "flow", "input_map", "publishes"):
        template_val = getattr(template, field)
        if field in step_overrides:
            merged[field] = step_overrides[field]
        elif template_val is not None:
            merged[field] = copy.deepcopy(template_val)

    # Context: deep merge (union of lists)
    template_ctx = copy.deepcopy(template.context) if template.context else {}
    step_ctx = step_overrides.get("context", {})
    merged_required = list(
        set(template_ctx.get("required", []) + step_ctx.get("required", []))
    )
    merged_optional = list(
        set(template_ctx.get("optional", []) + step_ctx.get("optional", []))
    )
    if merged_required or merged_optional:
        merged["context"] = {
            "required": merged_required,
            "optional": merged_optional,
        }

    # Params: deep merge (step values override matching keys)
    template_params = copy.deepcopy(template.params) if template.params else {}
    step_params = step_overrides.get("params", {})
    template_params.update(step_params)
    if template_params:
        merged["params"] = template_params

    # Config: deep merge
    template_config = copy.deepcopy(template.config) if template.config else {}
    step_config = step_overrides.get("config", {})
    template_config.update(step_config)
    if template_config:
        merged["config"] = template_config

    # Prompt: step wins if present, otherwise not set (templates don't carry prompts)
    if "prompt" in step_overrides:
        merged["prompt"] = step_overrides["prompt"]

    # Resolver, terminal, status, tail_call: always from step
    for field in ("resolver", "terminal", "status", "tail_call"):
        if field in step_overrides:
            merged[field] = step_overrides[field]

    return merged


def validate_params_against_schema(
    params: dict[str, Any],
    schema: dict[str, ParamSchemaEntry] | None,
    step_name: str,
    template_name: str,
) -> list[str]:
    """Validate merged params against template schema.

    Returns list of warnings (non-fatal). Raises FlowValidationError on hard failures.
    """
    if not schema:
        return []

    warnings: list[str] = []

    for param_name, entry in schema.items():
        value = params.get(param_name)

        # Check required params
        if entry.required and value is None and entry.default is None:
            raise FlowValidationError(
                f"Step '{step_name}' (template '{template_name}'): "
                f"required param '{param_name}' is missing"
            )

        # Apply defaults for missing optional params
        if value is None and entry.default is not None:
            params[param_name] = entry.default
            continue

        if value is None:
            continue

        # Skip Jinja2 template strings — validated at render time
        if isinstance(value, str) and "{{" in value:
            continue

        # Type checking
        type_map: dict[str, type | tuple] = {
            "string": str,
            "integer": int,
            "float": (int, float),
            "boolean": bool,
            "list": list,
            "dict": dict,
        }
        expected_type = type_map.get(entry.type)
        if expected_type and not isinstance(value, expected_type):
            raise FlowValidationError(
                f"Step '{step_name}' param '{param_name}': "
                f"expected {entry.type}, got {type(value).__name__}"
            )

        # Enum validation
        if entry.enum and value not in entry.enum:
            raise FlowValidationError(
                f"Step '{step_name}' param '{param_name}': "
                f"'{value}' not in allowed values {entry.enum}"
            )

        # Range validation
        if (
            entry.min is not None
            and isinstance(value, (int, float))
            and value < entry.min
        ):
            raise FlowValidationError(
                f"Step '{step_name}' param '{param_name}': "
                f"{value} is below minimum {entry.min}"
            )
        if (
            entry.max is not None
            and isinstance(value, (int, float))
            and value > entry.max
        ):
            raise FlowValidationError(
                f"Step '{step_name}' param '{param_name}': "
                f"{value} is above maximum {entry.max}"
            )

        # List constraints
        if entry.type == "list" and isinstance(value, list):
            if entry.min_items is not None and len(value) < entry.min_items:
                raise FlowValidationError(
                    f"Step '{step_name}' param '{param_name}': "
                    f"list has {len(value)} items, minimum is {entry.min_items}"
                )
            if entry.max_items is not None and len(value) > entry.max_items:
                raise FlowValidationError(
                    f"Step '{step_name}' param '{param_name}': "
                    f"list has {len(value)} items, maximum is {entry.max_items}"
                )

    return warnings
