"""Resolver dispatch — routes resolver definitions to their implementations.

All resolvers are now async to support LLM-driven resolvers that need
to call inference via the effects interface.
"""

from __future__ import annotations

from typing import Any

from agent.resolvers.rule import resolve_rule
from agent.resolvers.llm_menu import resolve_llm_menu, resolve_llm_multi_select


class ResolverError(Exception):
    """Raised when a resolver fails to determine a transition."""

    pass


async def resolve(
    resolver_def: dict,
    step_output: Any,
    context: dict,
    meta: dict,
    effects: Any = None,
) -> str:
    """Dispatch to the appropriate resolver based on type.

    Args:
        resolver_def: The resolver definition from the step (type, rules, etc.)
        step_output: The output from the step's action.
        context: The current context accumulator.
        meta: Flow execution metadata.
        effects: Effects interface (required for llm_menu resolver).

    Returns:
        The name of the next step to transition to.

    Raises:
        ResolverError: If no resolver matches or resolver type is unknown.
    """
    resolver_type = resolver_def.get("type")

    if resolver_type == "rule":
        # Rule resolver is synchronous — just call it directly
        return resolve_rule(resolver_def, step_output, context, meta)

    elif resolver_type == "llm_menu":
        # LLM menu resolver is async — needs effects for inference
        if effects is None:
            raise ResolverError(
                "LLM menu resolver requires effects interface for inference."
            )
        return await resolve_llm_menu(
            resolver_def, step_output, context, meta, effects=effects
        )

    elif resolver_type == "llm_multi_select":
        # Multi-select resolver — uses memoryful sessions
        if effects is None:
            raise ResolverError("LLM multi-select resolver requires effects interface.")
        return await resolve_llm_multi_select(
            resolver_def, step_output, context, meta, effects=effects
        )

    else:
        raise ResolverError(
            f"Unknown resolver type: {resolver_type!r}. "
            f"Available: ['rule', 'llm_menu', 'llm_multi_select']"
        )
