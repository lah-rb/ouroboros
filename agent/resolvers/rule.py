"""Rule-based transition resolver.

Evaluates conditions against the step's output, context accumulator,
and execution metadata using restricted eval(). Rules are evaluated
in order; the first matching condition wins.
"""

from typing import Any


class RuleResolverError(Exception):
    """Raised when rule resolution fails."""

    pass


def resolve_rule(
    resolver_def: dict,
    step_output: Any,
    context: dict,
    meta: dict,
) -> str:
    """Evaluate rule conditions and return the first matching transition.

    Conditions are Python expressions evaluated with a restricted namespace.
    No builtins are available — only result, context, meta, and effects.

    Args:
        resolver_def: The resolver definition containing 'rules' list.
        step_output: The StepOutput from the step's action.
        context: The current context accumulator.
        meta: Flow execution metadata.

    Returns:
        The transition target (step name) of the first matching rule.

    Raises:
        RuleResolverError: If no rule matches or a condition fails to evaluate.
    """
    rules = resolver_def.get("rules", [])
    if not rules:
        raise RuleResolverError("Rule resolver has no rules defined.")

    # Extract result dict from StepOutput (handle both model and raw dict)
    if hasattr(step_output, "result"):
        result = step_output.result
    elif isinstance(step_output, dict):
        result = step_output.get("result", {})
    else:
        result = {}

    # Build the restricted evaluation namespace
    namespace = _build_namespace(result, context, meta)

    for rule in rules:
        # Handle both RuleCondition model and raw dict
        if hasattr(rule, "condition"):
            condition = rule.condition
            transition = rule.transition
        elif isinstance(rule, dict):
            condition = rule["condition"]
            transition = rule["transition"]
        else:
            raise RuleResolverError(f"Invalid rule format: {rule!r}")

        try:
            matched = _eval_condition(condition, namespace)
        except Exception as e:
            raise RuleResolverError(
                f"Error evaluating condition {condition!r}: {e}"
            ) from e

        if matched:
            return transition

    raise RuleResolverError(
        f"No rule matched. Conditions evaluated: "
        f"{[r.condition if hasattr(r, 'condition') else r.get('condition') for r in rules]}"
    )


def _build_namespace(result: dict, context: dict, meta: dict) -> dict:
    """Build the restricted eval namespace.

    The namespace contains:
      - result: The step's output result dict
      - context: The context accumulator
      - meta: Flow execution metadata
      - null/None/true/false/True/False: convenience aliases
      - len: safe built-in for length checks
    """
    return {
        "result": _DotDict(result),
        "context": _DotDict(context),
        "input": _DotDict(context),  # Flow inputs live in the accumulator
        "meta": _DotDict(meta),
        # Convenience aliases for YAML-friendly conditions
        "null": None,
        "None": None,
        "true": True,
        "false": False,
        "True": True,
        "False": False,
        # Safe builtins
        "len": len,
        "sum": sum,
    }


def _eval_condition(condition: str, namespace: dict) -> bool:
    """Evaluate a condition string in a restricted namespace.

    No builtins are provided beyond what's in the namespace.
    This is safe because flow authors are trusted.
    """
    # Completely disable builtins
    return bool(eval(condition, {"__builtins__": {}}, namespace))


class _DotDict(dict):
    """A dict subclass that supports attribute access for dot notation in conditions.

    Enables conditions like `result.file_found == true` instead of
    `result['file_found'] == True`.

    Uses __getattribute__ to prioritize dict keys over dict methods,
    so that keys like 'items' or 'values' resolve to stored data
    rather than the dict method.
    """

    def __getattribute__(self, key: str) -> Any:
        # Don't intercept private/dunder attributes — dict internals need these
        if key.startswith("_"):
            return super().__getattribute__(key)
        # Check if it's a key stored in the dict
        try:
            value = self[key]
            if isinstance(value, dict) and not isinstance(value, _DotDict):
                return _DotDict(value)
            return value
        except KeyError:
            # Not a dict key — fall through to normal attribute resolution.
            # This handles actual dict methods (e.g., .get(), .keys() when
            # there's no stored key by that name) and returns None for
            # truly missing attributes.
            try:
                return super().__getattribute__(key)
            except AttributeError:
                return None

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value
