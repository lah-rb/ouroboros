"""Loader pipeline for CUE-exported flow definitions.

Pipeline:
  CUE export → JSON → load_flow_json() → FlowDefinition

Responsibilities:
  - Parse CUE-exported JSON into FlowDefinition (Pydantic)
  - Run semantic validation (transitions, context reachability, terminals)
  - Resolve $ref values in params / input_map at runtime
  - Run pre-compute formatters before prompt rendering
  - Render section-based YAML prompt templates
  - Assemble structured returns at terminal steps

CUE has already validated types, cross-field constraints, template
unification, and entry-step existence by the time Python sees the JSON.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from agent.errors import FlowRuntimeError

logger = logging.getLogger(__name__)


# ── Stage 1: Ref Resolution ─────────────────────────────────────────
#
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
    key_path = parts[1:]  # ["mission_id"] or ["dispatch_config", "flow"]

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


def resolve_params(
    params: dict[str, Any], namespaces: dict[str, Any]
) -> dict[str, Any]:
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
            raw_params = (
                step.params if isinstance(step.params, dict) else step.params or {}
            )

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

        # Propagate into namespaces so subsequent formatters in the
        # same chain can reference earlier outputs via $ref.
        # Example: step 1 writes "arch_fallback_context", step 2
        # reads {$ref: "context.arch_fallback_context"} to coalesce.
        namespaces["context"][output_key] = result

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
            raise FlowRuntimeError(f"Prompt template not found: {template_path}")

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

    def render_with_cache_split(
        self, template_id: str, namespaces: dict[str, Any]
    ) -> tuple[str, str]:
        """Render, splitting the LEADING run of ``cache: true`` sections (the
        invariant static head) from the rest (the dynamic tail).

        Returns ``(static_prefix, dynamic)`` such that
        ``static_prefix + dynamic == render(template_id, namespaces)`` exactly —
        the separator is baked into ``static_prefix`` — so feeding them to the
        per-flow KV cache is output-neutral. ``static_prefix`` is ``""`` when no
        leading cache section renders (caller then uses the normal path).

        Only the LEADING contiguous cache:true sections count: once a
        non-cache (or skipped) section appears, everything after is dynamic,
        because KV-prefix caching can only pin a prefix. So order matters —
        put the dynamic tail (e.g. per-cycle feedback) last for max coverage.
        """
        template = self.load_template(template_id)
        static_parts: list[str] = []
        dynamic_parts: list[str] = []
        in_static = True
        for section in template.get("sections", []):
            rendered = self._render_section(section, namespaces)
            if rendered is None:
                continue
            if in_static and section.get("cache") is True:
                static_parts.append(rendered)
            else:
                in_static = False
                dynamic_parts.append(rendered)
        static_prefix = "\n\n".join(static_parts)
        dynamic = "\n\n".join(dynamic_parts)
        if static_prefix and dynamic:
            # render() joins the two halves with "\n\n"; bake it into the prefix
            # so static_prefix + dynamic reconstructs the full prompt verbatim.
            return static_prefix + "\n\n", dynamic
        return static_prefix, dynamic

    def _render_section(self, section: dict, namespaces: dict[str, Any]) -> str | None:
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

        separator = section.get("separator", "\n")
        content_template = section.get("content", "")
        header = section.get("header", "")
        footer = section.get("footer", "")

        rendered_items = []
        for item in loop_source:
            # Create a temporary namespace with the loop variable.
            # Templates reference loop items as {loop.field} (or {loop} for
            # simple string lists).
            loop_namespaces = {**namespaces, "loop": item}

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


# ── Verbatim prompt text (for plain action code) ─────────────────────
#
# Some steps are plain `action:` steps that drive an inference effect
# themselves rather than going through `action: inference` + a
# prompt_template. Their prompt text still belongs in prompts/ — that is
# the auditable surface a reviewer diffs and the lint checks parse — but
# they need it as a STRING, not rendered against namespaces:
#
#   * several pin it as a `static_prefix` KV-cache head keyed on
#     md5(text)[:10], so the bytes must survive the trip untouched;
#   * most interpolate with str.format(), whose placeholders (`{brief}`)
#     are deliberately NOT the store's `{context.x}` syntax and must pass
#     through unresolved.
#
# Hence: read the file, return `content` exactly, interpolate nothing.
# Running these through PromptRenderer.render would return "" anyway —
# it renders `sections:`, and these files are the flat turn format.
#
# This replaces two ad-hoc loaders. The one it most improves on is
# contract_swarm_actions._load_prompt, which swallowed every error and
# returned "" — a typo'd id silently produced an EMPTY prompt rather
# than a failure.

_prompt_text_cache: dict[str, str] = {}
_prompt_text_dir: Path | None = None


def set_prompt_text_dir(prompts_dir: str | Path | None) -> None:
    """Point load_prompt_text at a directory (tests). None restores the
    default and clears the cache."""
    global _prompt_text_dir
    _prompt_text_dir = Path(prompts_dir) if prompts_dir is not None else None
    _prompt_text_cache.clear()


def _prompts_dir_for_text() -> Path:
    """Locate prompts/, anchored on the REPO ROOT rather than the cwd.

    Deliberately not the cwd probe _get_prompt_renderer uses: callers load
    these at module import time, long before init_prompt_renderer runs and
    from whatever directory the process happened to start in. The repo
    anchor is the only one that holds there.
    """
    if _prompt_text_dir is not None:
        return _prompt_text_dir
    anchored = Path(__file__).resolve().parent.parent / "prompts"
    if anchored.exists():
        return anchored
    for candidate in (Path("prompts"), Path("ouroboros/prompts")):
        if candidate.exists():
            return candidate
    return anchored


def load_prompt_text(template_id: str) -> str:
    """Return a flat prompt file's ``content`` verbatim.

    Args:
        template_id: e.g. "personas/diagnosis" -> prompts/personas/diagnosis.yaml

    Raises:
        FlowRuntimeError: file missing, unparseable, or ``content`` absent
            or empty. Failing loud is the point — see the module note above.
    """
    if template_id in _prompt_text_cache:
        return _prompt_text_cache[template_id]

    path = _prompts_dir_for_text() / f"{template_id}.yaml"
    if not path.exists():
        raise FlowRuntimeError(
            f"Prompt text not found: {template_id!r} (looked at {path})"
        )
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise FlowRuntimeError(
            f"Prompt text {template_id!r} is not valid YAML ({path}): {exc}"
        ) from exc

    content = (data or {}).get("content") if isinstance(data, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise FlowRuntimeError(
            f"Prompt text {template_id!r} has no non-empty 'content' key "
            f"({path}). This file must use the flat turn format."
        )

    _prompt_text_cache[template_id] = content
    return content


# ── Returns Assembly ───────────────────────────────────────────────
#
# Assembles structured return data from the flow's `returns` declaration,
# resolving each field's `from` path against the live accumulator.
#
# Called by loop.py at tail-call resolution and terminal step handling.


def assemble_returns(
    flow_def: Any,
    accumulator: dict[str, Any],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Assemble structured returns from a flow's returns declaration.

    Resolves each return field's `from` path against the accumulator
    and input namespaces. Validates that required fields are present.

    Args:
        flow_def: The FlowDefinition (must have a `returns` dict).
        accumulator: The current context accumulator at terminal step.
        inputs: The original flow inputs.

    Returns:
        Dict of field_name → resolved value. Missing optional fields
        are omitted (not set to None).
    """
    returns_decl = getattr(flow_def, "returns", None) or {}
    if not returns_decl:
        return {}

    namespaces = {
        "input": inputs,
        "context": accumulator,
    }

    assembled = {}
    for field_name, field_spec in returns_decl.items():
        if not isinstance(field_spec, dict):
            continue

        from_path = field_spec.get("from", "")
        is_optional = field_spec.get("optional", False)

        if not from_path:
            continue

        # Resolve the from path
        value = _resolve_ref({"$ref": from_path}, namespaces)

        if value is not None:
            assembled[field_name] = value
        elif not is_optional:
            # Required field missing — log warning but don't fail
            logger.warning(
                "Flow %r: required return field %r (from %r) resolved to None",
                getattr(flow_def, "flow", "unknown"),
                field_name,
                from_path,
            )
            assembled[field_name] = None

    return assembled


# ── Formatter Registry Initialization ────────────────────────────────


def _init_formatter_registry() -> None:
    """Populate the formatter registry from the formatters and renderers modules."""
    global _formatter_registry
    try:
        from agent.formatters import PRE_COMPUTE_FORMATTERS

        _formatter_registry.update(PRE_COMPUTE_FORMATTERS)
    except ImportError:
        logger.warning("Could not import formatters module — registry empty")

    # Register projection renderers
    try:
        from agent.renderers import RENDERER_REGISTRY

        _formatter_registry.update(RENDERER_REGISTRY)
    except ImportError:
        pass  # renderers not yet available — non-fatal


# Initialize on import
_init_formatter_registry()
