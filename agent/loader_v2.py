"""New loader pipeline for CUE-exported flow definitions.

Replaces the YAML-based loader with a JSON pipeline:
  CUE export → JSON → load_flow_json() → FlowDefinition

Key changes from the YAML loader:
  - Input is JSON (from `cue export --out json`), not YAML
  - No template expansion pass — CUE handles unification at export time
  - No Jinja2 in structural fields — $ref values resolved at runtime
  - Prompt templates loaded from separate YAML files
  - Pre-compute formatters invoked before prompt rendering
  - Semantic validation preserved (transitions, context reachability, terminals)

Integration with existing runtime:
  - Output is the same FlowDefinition Pydantic model (with new fields)
  - _build_step_input() updated to resolve $ref values instead of Jinja2
  - _execute_inference_action() updated to use prompt template renderer
  - All other runtime machinery unchanged

This file is a SKETCH — it defines the pipeline stages, their interfaces,
and how they integrate. Not production code yet.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

# ── Stage 1: Load Flow JSON ──────────────────────────────────────────
#
# Replaces: load_flow() YAML parsing + template expansion
# Input:    JSON file from `cue export`
# Output:   FlowDefinition (Pydantic model)
#
# CUE has already:
#   - Validated types and cross-field constraints
#   - Unified step templates (no `use:` to expand)
#   - Verified entry step exists
#
# Python still needs to:
#   - Parse JSON into FlowDefinition
#   - Run semantic validation (transitions, context reachability)
#   - Register the flow in the flow registry


def load_flow_json(path: str | Path) -> "FlowDefinition":
    """Load a CUE-exported flow definition from JSON.

    Args:
        path: Path to the JSON file (output of `cue export --out json`)

    Returns:
        A validated FlowDefinition.
    """
    path = Path(path)

    if not path.exists():
        raise FlowLoadError(f"Flow file not found: {path}")

    # Accept both .json (CUE export) and .yaml/.yml (legacy)
    if path.suffix == ".json":
        with open(path, "r") as f:
            raw = json.load(f)
    elif path.suffix in (".yaml", ".yml"):
        # Backward compatibility during migration
        with open(path, "r") as f:
            raw = yaml.safe_load(f)
    else:
        raise FlowLoadError(f"Flow file must be .json or .yaml: {path}")

    # Pydantic validation (FlowDefinition model updated for new fields)
    flow = FlowDefinition(**raw)

    # Semantic validation (preserved from current loader)
    _validate_semantics(flow, path)

    return flow


# ── Stage 2: Ref Resolution ─────────────────────────────────────────
#
# Replaces: render_template() / render_params() Jinja2 rendering
# Called by: runtime._build_step_input() and runtime._execute_subflow()
# Input:    A value that may be a $ref dict, a literal, or nested
# Output:   The resolved value
#
# Resolution happens at RUNTIME, not load time, because refs reference
# input/context/meta which only exist during execution.
#
# Resolution rules:
#   1. If value is a dict with "$ref" key → resolve the ref
#   2. If value is a plain literal → return as-is
#   3. If value is a list/dict without "$ref" → recurse
#
# Ref resolution:
#   - Parse the dotted path: "input.mission_id" → namespace="input", path=["mission_id"]
#   - Navigate into the namespace dict following the path
#   - If the value is None/missing and "default" is set → return default
#   - If the value is None/missing and "fallback" is set → try each fallback in order
#   - If all resolution fails → return None (not an error — optional refs are common)


def resolve_value(value: Any, namespaces: dict[str, Any]) -> Any:
    """Resolve a value that may contain $ref references.

    Args:
        value: A literal, a $ref dict, or a nested structure containing refs.
        namespaces: Dict of available namespaces, e.g.:
            {"input": {...}, "context": {...}, "meta": {...}}

    Returns:
        The resolved value.
    """
    if isinstance(value, dict) and "$ref" in value:
        return _resolve_ref(value, namespaces)
    elif isinstance(value, dict):
        return {k: resolve_value(v, namespaces) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_value(item, namespaces) for item in value]
    else:
        return value


def _resolve_ref(ref: dict, namespaces: dict[str, Any]) -> Any:
    """Resolve a single $ref dict.

    Ref format: {"$ref": "namespace.dotted.path", "default": ..., "fallback": [...]}

    Args:
        ref: The $ref dict.
        namespaces: Available namespaces.

    Returns:
        The resolved value, or None if unresolvable.
    """
    path_str = ref["$ref"]
    parts = path_str.split(".")
    namespace_name = parts[0]  # "input", "context", or "meta"
    key_path = parts[1:]       # ["mission_id"] or ["dispatch_config", "flow"]

    # Navigate into the namespace
    current = namespaces.get(namespace_name)
    if current is None:
        return _apply_fallbacks(ref, namespaces)

    for part in key_path:
        if isinstance(current, dict):
            current = current.get(part)
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            current = None
        if current is None:
            break

    if current is not None:
        return current

    return _apply_fallbacks(ref, namespaces)


def _apply_fallbacks(ref: dict, namespaces: dict[str, Any]) -> Any:
    """Apply default or fallback chain when primary ref resolves to None."""
    if "default" in ref:
        return ref["default"]

    if "fallback" in ref:
        for fallback_item in ref["fallback"]:
            if isinstance(fallback_item, dict) and "$ref" in fallback_item:
                result = _resolve_ref(fallback_item, namespaces)
                if result is not None:
                    return result
            elif fallback_item is not None:
                # Literal fallback value
                return fallback_item

    return None


def resolve_params(params: dict[str, Any], namespaces: dict[str, Any]) -> dict[str, Any]:
    """Resolve all $ref values in a params dictionary.

    Drop-in replacement for render_params().

    Args:
        params: Step params dict (may contain $ref values).
        namespaces: Available namespaces.

    Returns:
        New dict with all refs resolved.
    """
    return {k: resolve_value(v, namespaces) for k, v in params.items()}


def resolve_input_map(
    input_map: dict[str, Any], namespaces: dict[str, Any]
) -> dict[str, Any]:
    """Resolve all $ref values in an input_map (tail_call or sub-flow).

    Drop-in replacement for Jinja2 rendering of input_map values.

    Args:
        input_map: Mapping of target input names → values (may contain $refs).
        namespaces: Available namespaces.

    Returns:
        New dict with all refs resolved to runtime values.
    """
    return {k: resolve_value(v, namespaces) for k, v in input_map.items()}


# ── Stage 3: Pre-Compute Formatters ─────────────────────────────────
#
# Replaces: Complex Jinja2 loops and filters inside prompt templates
# Called by: runtime._execute_inference_action() before prompt rendering
# Input:    pre_compute list from step definition + live context
# Output:   Additional context keys injected into the namespace
#
# Each pre_compute entry names a registered formatter function and an
# output_key. The formatter runs, and its return value is added to
# the context namespace under that key. The prompt template then
# references {context.output_key} as a simple string insertion.


# Formatter registry — maps names to callables
# Each formatter signature: (params: dict, namespaces: dict) -> str
_formatter_registry: dict[str, Any] = {}


def register_formatter(name: str, fn: Any) -> None:
    """Register a pre-compute formatter function.

    Args:
        name: Formatter name (matches pre_compute.formatter in flow defs).
        fn: Callable with signature (params: dict, namespaces: dict) -> str
    """
    _formatter_registry[name] = fn


def run_pre_compute(
    pre_compute_steps: list[dict],
    namespaces: dict[str, Any],
) -> dict[str, str]:
    """Run pre-compute formatters and return computed context keys.

    Args:
        pre_compute_steps: List of pre_compute dicts from step definition.
            Each has: formatter (str), output_key (str), params (dict).
        namespaces: Current input/context/meta namespaces.

    Returns:
        Dict of output_key → formatted string, to be merged into context.
    """
    computed = {}

    for step in pre_compute_steps:
        # Handle both Pydantic PreComputeStep models and raw dicts
        if isinstance(step, dict):
            formatter_name = step["formatter"]
            output_key = step["output_key"]
            raw_params = step.get("params", {})
        else:
            formatter_name = step.formatter
            output_key = step.output_key
            raw_params = step.params if isinstance(step.params, dict) else step.params or {}

        # Resolve any $refs in the formatter's params
        resolved_params = resolve_params(raw_params, namespaces)

        # Look up and call the formatter
        formatter_fn = _formatter_registry.get(formatter_name)
        if formatter_fn is None:
            raise FlowRuntimeError(
                f"Unknown pre-compute formatter: {formatter_name!r}. "
                f"Registered: {list(_formatter_registry.keys())}"
            )

        result = formatter_fn(resolved_params, namespaces)
        computed[output_key] = result

    return computed


# ── Stage 4: Prompt Template Rendering ───────────────────────────────
#
# Replaces: Jinja2 render_template() for prompt: | blocks
# Called by: runtime._execute_inference_action()
# Input:    Prompt template file (YAML sections) + resolved namespaces
# Output:   Assembled prompt string
#
# The renderer:
#   1. Loads the template YAML file by template ID
#   2. Walks sections in order
#   3. For each section:
#      a. Evaluates `when` condition (truthiness of a namespace value)
#      b. If `loop` is declared, iterates and expands per-item
#      c. Substitutes {input.X}, {context.X}, {meta.X} in content
#   4. Joins all rendered sections with double newlines
#   5. Returns the assembled string


class PromptRenderer:
    """Loads and renders structured prompt templates."""

    def __init__(self, prompts_dir: str | Path):
        """Initialize with the prompts directory path.

        Args:
            prompts_dir: Root directory containing prompt template files.
                         Templates are at <prompts_dir>/<template_id>.yaml
        """
        self.prompts_dir = Path(prompts_dir)
        self._cache: dict[str, dict] = {}

    def load_template(self, template_id: str) -> dict:
        """Load a prompt template by ID.

        Args:
            template_id: Template identifier, e.g. "create_file/generate_content"
                         Maps to <prompts_dir>/create_file/generate_content.yaml

        Returns:
            Parsed template dict with id, description, sections.
        """
        if template_id in self._cache:
            return self._cache[template_id]

        template_path = self.prompts_dir / f"{template_id}.yaml"
        if not template_path.exists():
            raise FlowRuntimeError(
                f"Prompt template not found: {template_path}"
            )

        with open(template_path, "r") as f:
            template = yaml.safe_load(f)

        self._cache[template_id] = template
        return template

    def render(self, template_id: str, namespaces: dict[str, Any]) -> str:
        """Render a prompt template against live namespaces.

        Args:
            template_id: Template identifier.
            namespaces: Dict with "input", "context", "meta" keys.

        Returns:
            Assembled prompt string.
        """
        template = self.load_template(template_id)
        rendered_sections = []

        for section in template.get("sections", []):
            rendered = self._render_section(section, namespaces)
            if rendered is not None:
                rendered_sections.append(rendered)

        return "\n\n".join(rendered_sections)

    def _render_section(
        self, section: dict, namespaces: dict[str, Any]
    ) -> str | None:
        """Render a single section, returning None if skipped.

        Handles three section types:
          - Static: always renders
          - Conditional: renders if `when` value is truthy
          - Loop: repeats content for each item in a list
        """
        # Check `when` condition
        if "when" in section:
            condition_value = self._resolve_path(section["when"], namespaces)
            if not condition_value:
                return None

        # Handle loop sections
        if "loop" in section:
            return self._render_loop_section(section, namespaces)

        # Render content with variable substitution
        content = section.get("content", "")
        return self._substitute(content, namespaces)

    def _render_loop_section(
        self, section: dict, namespaces: dict[str, Any]
    ) -> str | None:
        """Render a loop section by iterating over a list."""
        loop_source = self._resolve_path(section["loop"], namespaces)
        if not loop_source or not isinstance(loop_source, list):
            return None

        loop_var_name = section.get("loop_as", "item")
        separator = section.get("separator", "\n")
        content_template = section.get("content", "")
        header = section.get("header", "")
        footer = section.get("footer", "")

        rendered_items = []
        for item in loop_source:
            # Create a temporary namespace with the loop variable
            loop_namespaces = {**namespaces, "loop": item}

            # If item is a string (not a dict), make loop itself the value
            # so {loop} works for simple string lists
            rendered = self._substitute(content_template, loop_namespaces)
            rendered_items.append(rendered)

        body = separator.join(rendered_items)

        parts = []
        if header:
            parts.append(self._substitute(header, namespaces))
        parts.append(body)
        if footer:
            parts.append(self._substitute(footer, namespaces))

        return "\n".join(parts)

    # Compiled regex for variable substitution.
    # Only matches {namespace.path} where namespace is one of the four
    # known prefixes and there's at least one dot. This naturally avoids:
    #   - JSON examples: {"key": value}, {} → quotes/colons don't match
    #   - Placeholders: {file}, {module_name} → no dot, no namespace prefix
    #   - Code snippets: any braces without namespace.key pattern
    # No escape mechanism needed — the namespace prefix is the discriminator.
    _REF_PATTERN = re.compile(
        r"\{((?:input|context|meta|loop)\.[a-zA-Z_][a-zA-Z0-9_.]*)\}"
    )

    def _substitute(self, template: str, namespaces: dict[str, Any]) -> str:
        """Simple variable substitution in a content string.

        Replaces {input.X}, {context.X}, {meta.X}, {loop.X} with values.
        No expressions, no filters, no method calls.
        Missing values resolve to empty string.

        Literal braces in JSON examples, code snippets, and single-word
        placeholders pass through untouched because the regex requires
        a known namespace prefix followed by a dot.
        """

        def _replacer(match: re.Match) -> str:
            path = match.group(1)
            value = self._resolve_path(path, namespaces)
            if value is None:
                return ""
            return str(value)

        return self._REF_PATTERN.sub(_replacer, template)

    def _resolve_path(self, path: str, namespaces: dict[str, Any]) -> Any:
        """Resolve a dotted path against namespaces.

        "input.mission_id" → namespaces["input"]["mission_id"]
        "context.repo_map_formatted" → namespaces["context"]["repo_map_formatted"]
        "loop.path" → namespaces["loop"]["path"] or namespaces["loop"].path
        """
        parts = path.split(".")
        current = namespaces.get(parts[0])

        for part in parts[1:]:
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(part)
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                return None

        return current


# ── Stage 5: Result Formatter Resolution ─────────────────────────────
#
# Replaces: Jinja2-rendered last_result strings in tail_call.input_map
# Called by: runtime after flow step completes, before building tail_call
# Input:    result_formatter name + result_keys from tail_call definition
# Output:   Formatted last_result string
#
# The formatter registry is shared with pre_compute formatters.
# Result formatters follow the same (params, namespaces) -> str signature.


def format_result(
    tail_call: dict,
    namespaces: dict[str, Any],
) -> str | None:
    """Format a result message from a tail_call definition.

    If the tail_call has a result_formatter, runs it with the declared
    result_keys resolved from namespaces. Otherwise returns None
    (the runtime should use a default message or the raw status).

    Args:
        tail_call: The tail_call dict from the step definition.
        namespaces: Current input/context/meta namespaces.

    Returns:
        Formatted result string, or None if no formatter declared.
    """
    formatter_name = tail_call.get("result_formatter")
    if not formatter_name:
        return None

    result_keys = tail_call.get("result_keys", [])

    # Resolve result_keys into a params dict
    resolved_keys = {}
    for key_path in result_keys:
        parts = key_path.split(".", 1)
        if len(parts) == 2:
            value = _resolve_ref(
                {"$ref": key_path}, namespaces
            )
            resolved_keys[key_path] = value

    formatter_fn = _formatter_registry.get(formatter_name)
    if formatter_fn is None:
        return f"[unknown formatter: {formatter_name}]"

    return formatter_fn(resolved_keys, namespaces)


# ── Integration: Runtime Changes ─────────────────────────────────────
#
# The existing runtime.py needs these modifications:
#
# 1. _build_step_input():
#    - Replace render_params(step_def.params, template_vars)
#      with resolve_params(step_def.params, namespaces)
#    - Same namespace dict structure, different resolution function
#
# 2. _execute_inference_action():
#    - Before prompt rendering, run pre_compute formatters:
#        if step_def.pre_compute:
#            computed = run_pre_compute(step_def.pre_compute, namespaces)
#            step_input.context.update(computed)
#            namespaces["context"].update(computed)
#    - Replace render_template(step_def.prompt, template_vars)
#      with prompt_renderer.render(step_def.prompt_template.template, namespaces)
#    - prompt_renderer is an instance of PromptRenderer initialized with
#      the prompts directory path
#
# 3. _execute_subflow() / tail call resolution:
#    - Replace Jinja2 rendering of input_map values
#      with resolve_input_map(input_map, namespaces)
#    - Replace Jinja2 rendering of tail_call.flow
#      with resolve_value(tail_call["flow"], namespaces)
#    - After resolving input_map, call format_result() and inject
#      the result as "last_result" if a formatter was declared
#
# 4. models.py:
#    - Add to StepDefinition: prompt_template (optional dict),
#      pre_compute (optional list of dicts)
#    - Add to FlowDefinition: (no changes — steps map already flexible)
#    - Mark step_def.prompt as deprecated (still supported for migration)
#
# 5. load_all_flows():
#    - Scan for both .json and .yaml files
#    - .json files use load_flow_json()
#    - .yaml files use legacy load_flow() (migration period)
#    - No more template registry loading — CUE handles that


# ── Exception Classes ────────────────────────────────────────────────

class FlowLoadError(Exception):
    """Raised when a flow definition fails to load or validate."""
    pass

class FlowValidationError(FlowLoadError):
    """Raised when a flow definition fails semantic validation."""
    pass

class FlowRuntimeError(Exception):
    """Raised during flow execution (ref resolution, formatting, etc.)."""
    pass


# ── Semantic Validation ─────────────────────────────────────────────


def _validate_semantics(flow: "FlowDefinition", path: Path) -> None:
    """Validate flow semantics that CUE can't check.

    - All transition targets reference existing steps
    - At least one terminal step or tail_call exists
    - No unreachable steps (warning, not error)
    """
    step_names = set(flow.steps.keys())
    errors = []

    has_terminal = False
    reachable = {flow.entry}
    to_visit = [flow.entry]

    while to_visit:
        current = to_visit.pop()
        step = flow.steps.get(current)
        if not step:
            errors.append(f"Entry or transition target '{current}' not in steps")
            continue

        if step.terminal or step.tail_call:
            has_terminal = True

        if step.resolver:
            for rule in step.resolver.rules:
                target = rule.transition
                if target not in step_names:
                    errors.append(
                        f"Step '{current}': transition target '{target}' not found"
                    )
                if target not in reachable:
                    reachable.add(target)
                    to_visit.append(target)

            # LLM menu option targets
            if step.resolver.options:
                for opt_name, opt_def in step.resolver.options.items():
                    if isinstance(opt_def, dict):
                        target = opt_def.get("target", opt_name)
                    else:
                        target = opt_name
                    if target not in step_names:
                        errors.append(
                            f"Step '{current}': menu option '{opt_name}' target "
                            f"'{target}' not found"
                        )
                    if target not in reachable:
                        reachable.add(target)
                        to_visit.append(target)

            if step.resolver.default_transition:
                dt = step.resolver.default_transition
                if dt not in step_names:
                    errors.append(
                        f"Step '{current}': default_transition '{dt}' not found"
                    )
                if dt not in reachable:
                    reachable.add(dt)
                    to_visit.append(dt)

    if not has_terminal:
        errors.append("Flow has no terminal steps or tail_calls")

    unreachable = step_names - reachable
    if unreachable:
        import logging
        logging.getLogger(__name__).warning(
            "Flow %s (%s): unreachable steps: %s",
            flow.flow, path, unreachable,
        )

    if errors:
        raise FlowValidationError(
            f"Flow {flow.flow!r} ({path}): {'; '.join(errors)}"
        )


# ── Formatter Registry Initialization ────────────────────────────────


def _init_formatter_registry() -> None:
    """Populate the formatter registry from the formatters module."""
    global _formatter_registry
    try:
        from agent.formatters import PRE_COMPUTE_FORMATTERS, RESULT_FORMATTERS
        _formatter_registry.update(PRE_COMPUTE_FORMATTERS)
        _formatter_registry.update(RESULT_FORMATTERS)
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "Could not import formatters module — registry empty"
        )


# Initialize on import
_init_formatter_registry()


# ── Load All Flows ───────────────────────────────────────────────────


def load_all_flows(
    flows_dir: str | Path,
    prompts_dir: str | Path | None = None,
) -> tuple[dict[str, "FlowDefinition"], "PromptRenderer | None"]:
    """Load all flow definitions from a directory.

    Supports both CUE-exported JSON and legacy YAML files.
    Returns a dict of flow_name → FlowDefinition and a PromptRenderer.

    Args:
        flows_dir: Directory containing flow definition files.
        prompts_dir: Directory containing prompt templates (optional).

    Returns:
        Tuple of (flows dict, prompt renderer or None).
    """
    flows_dir = Path(flows_dir)
    flows = {}

    # Load JSON files (CUE exports)
    for json_path in sorted(flows_dir.glob("*.json")):
        try:
            flow = load_flow_json(json_path)
            flows[flow.flow] = flow
        except FlowLoadError as e:
            import logging
            logging.getLogger(__name__).error("Failed to load %s: %s", json_path, e)

    # Load YAML files (legacy, for flows not yet ported)
    for yaml_path in sorted(flows_dir.glob("*.yaml")):
        try:
            flow = load_flow_json(yaml_path)
            # Don't overwrite CUE versions
            if flow.flow not in flows:
                flows[flow.flow] = flow
        except FlowLoadError as e:
            import logging
            logging.getLogger(__name__).error("Failed to load %s: %s", yaml_path, e)

    # Initialize prompt renderer
    renderer = None
    if prompts_dir:
        renderer = PromptRenderer(prompts_dir)

    return flows, renderer

