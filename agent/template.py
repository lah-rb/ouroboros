"""Jinja2 template rendering for flow step params and prompts.

Renders {{ input.x }}, {{ context.y }}, {{ meta.z }} style templates
against a variables dictionary built from flow inputs, context accumulator,
and execution metadata.
"""

from typing import Any

from jinja2 import (
    BaseLoader,
    Environment,
    StrictUndefined,
    TemplateSyntaxError,
    UndefinedError,
)

# Shared Jinja2 environment — no filesystem loader needed since
# all templates are inline strings from YAML definitions.
# StrictUndefined gives clear errors when templates reference missing variables.
_jinja_env = Environment(
    loader=BaseLoader(),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


class TemplateError(Exception):
    """Raised when template rendering fails."""

    pass


def render_template(template_str: str, variables: dict[str, Any]) -> str:
    """Render a Jinja2 template string against a variables dictionary.

    Args:
        template_str: A Jinja2 template string (e.g., "{{ input.target_file_path }}")
        variables: Dictionary of variable namespaces available in the template.
                   Typically includes 'input', 'context', 'meta', 'params', 'result'.

    Returns:
        The rendered string.

    Raises:
        TemplateError: If the template has syntax errors or references undefined variables.
    """
    try:
        template = _jinja_env.from_string(template_str)
        return template.render(**variables)
    except TemplateSyntaxError as e:
        raise TemplateError(f"Template syntax error: {e}") from e
    except UndefinedError as e:
        raise TemplateError(
            f"Template references undefined variable: {e}. "
            f"Available namespaces: {list(variables.keys())}"
        ) from e


def render_params(params: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    """Render all string values in a params dictionary.

    Recursively processes the params dict, rendering any string values
    that contain Jinja2 template syntax. Non-string values pass through unchanged.

    Args:
        params: The params dictionary from a step definition.
        variables: Template variables (input, context, meta, etc.)

    Returns:
        A new dictionary with all string values rendered.
    """
    return _render_value(params, variables)


def _render_value(value: Any, variables: dict[str, Any]) -> Any:
    """Recursively render template strings in a value."""
    if isinstance(value, str):
        if "{{" in value or "{%" in value:
            return render_template(value, variables)
        return value
    elif isinstance(value, dict):
        return {k: _render_value(v, variables) for k, v in value.items()}
    elif isinstance(value, list):
        return [_render_value(item, variables) for item in value]
    else:
        return value
