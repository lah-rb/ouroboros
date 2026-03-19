"""LLM-driven menu resolver.

Presents the model with a constrained set of named options, each with a
description. The model picks one. Costs one inference call per resolution.

Supports both static options (defined in YAML) and dynamic options
(read from a context key via options_from).
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class LLMMenuResolverError(Exception):
    """Raised when LLM menu resolution fails."""

    pass


def _build_options_list(
    resolver_def: dict,
    context: dict,
) -> dict[str, str]:
    """Build the option name → description mapping.

    Supports static 'options' dict or dynamic 'options_from' context key.

    Returns:
        Dict mapping option name to description string.
    """
    # Dynamic options from context
    options_from = resolver_def.get("options_from")
    if options_from:
        # Navigate dotted path like "context.assessment.ready_tasks"
        parts = options_from.split(".")
        value = context
        for part in parts:
            if part == "context":
                continue  # skip the "context." prefix
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break

        if value is None:
            raise LLMMenuResolverError(
                f"options_from path {options_from!r} resolved to None. "
                f"Context keys: {list(context.keys())}"
            )

        # Value should be a list of dicts with 'id' and 'description',
        # or a list of strings
        if isinstance(value, list):
            options = {}
            for item in value:
                if isinstance(item, dict):
                    name = item.get("id", item.get("name", str(item)))
                    desc = item.get("description", str(item))
                    options[str(name)] = desc
                else:
                    options[str(item)] = str(item)
            return options

        raise LLMMenuResolverError(
            f"options_from {options_from!r} resolved to {type(value).__name__}, "
            f"expected a list."
        )

    # Static options from YAML
    static_options = resolver_def.get("options", {})
    if not static_options:
        raise LLMMenuResolverError("LLM menu resolver has no options defined.")

    options = {}
    for opt_name, opt_def in static_options.items():
        if isinstance(opt_def, dict):
            options[opt_name] = opt_def.get("description", opt_name)
        elif isinstance(opt_def, str):
            options[opt_name] = opt_def
        else:
            options[opt_name] = str(opt_def)

    return options


def _build_menu_prompt(
    resolver_prompt: str | None,
    options: dict[str, str],
    step_output_text: str | None = None,
) -> str:
    """Build the prompt that asks the model to choose an option.

    Args:
        resolver_prompt: The resolver's prompt string (optional context).
        options: Dict mapping option name to description.
        step_output_text: Optional text from the step's action output to
            include as context for the decision. When provided, the model
            can see what just happened before choosing.

    Returns:
        A prompt string instructing the model to pick exactly one option.
    """
    lines = []

    # Include step output as decision context when available
    if step_output_text:
        lines.append("Here is what just happened:")
        lines.append(step_output_text[:1500])
        lines.append("")

    if resolver_prompt:
        lines.append(resolver_prompt)
        lines.append("")

    lines.append(
        "Choose exactly ONE of the following options by responding with just the option name:"
    )
    lines.append("")

    for name, description in options.items():
        lines.append(f"  {name}: {description}")

    lines.append("")
    lines.append("Respond with ONLY the option name, nothing else.")

    return "\n".join(lines)


def _parse_choice(response_text: str, valid_options: set[str]) -> str | None:
    """Parse the model's response to extract a valid option name.

    Tries several strategies:
    1. Exact match (stripped).
    2. Case-insensitive match.
    3. First word match.
    4. Substring search for any option name.

    Returns:
        The matched option name, or None if no match found.
    """
    text = response_text.strip()

    # 1. Exact match
    if text in valid_options:
        return text

    # 2. Case-insensitive match
    lower_map = {opt.lower(): opt for opt in valid_options}
    if text.lower() in lower_map:
        return lower_map[text.lower()]

    # 3. First word match (model might add explanation)
    first_word = text.split()[0] if text.split() else ""
    # Strip punctuation from first word
    first_word_clean = re.sub(r"[^a-zA-Z0-9_]", "", first_word)
    if first_word_clean in valid_options:
        return first_word_clean
    if first_word_clean.lower() in lower_map:
        return lower_map[first_word_clean.lower()]

    # 4. Substring search — find any option name in the response
    for opt in valid_options:
        if opt in text or opt.lower() in text.lower():
            return opt

    return None


async def resolve_llm_menu(
    resolver_def: dict,
    step_output: Any,
    context: dict,
    meta: dict,
    effects: Any = None,
) -> str:
    """Resolve transition by asking the LLM to choose from a menu of options.

    Constructs a prompt listing the options, calls inference with low
    temperature and short max_tokens, parses the response, validates
    against the option set. Retries once on invalid response.

    Args:
        resolver_def: The resolver definition from the step.
        step_output: The output from the step's action.
        context: The current context accumulator.
        meta: Flow execution metadata.
        effects: Effects interface (must have run_inference method).

    Returns:
        The option name selected by the model. For static options,
        this is the option key (which maps to a step name or has a target).

    Raises:
        LLMMenuResolverError: If resolution fails after retry.
    """
    if effects is None:
        raise LLMMenuResolverError(
            "LLM menu resolver requires effects interface for inference."
        )

    if not hasattr(effects, "run_inference"):
        raise LLMMenuResolverError("Effects interface does not support run_inference.")

    # Build the options map
    options = _build_options_list(resolver_def, context)
    if not options:
        raise LLMMenuResolverError("No options available for LLM menu.")

    valid_option_names = set(options.keys())

    # Build the prompt, optionally including step output as context
    resolver_prompt = resolver_def.get("prompt")
    step_output_text = None
    if resolver_def.get("include_step_output", False) and step_output is not None:
        # Extract text from step output — try observations first, then result
        if hasattr(step_output, "observations") and step_output.observations:
            step_output_text = str(step_output.observations)
        elif hasattr(step_output, "result") and step_output.result:
            step_output_text = str(step_output.result)
        elif isinstance(step_output, str):
            step_output_text = step_output
    menu_prompt = _build_menu_prompt(resolver_prompt, options, step_output_text)

    # First attempt
    config = {"temperature": 0.1, "max_tokens": 50}
    result = await effects.run_inference(menu_prompt, config)

    if result.error:
        raise LLMMenuResolverError(f"Inference failed: {result.error}")

    choice = _parse_choice(result.text, valid_option_names)
    if choice is not None:
        logger.debug("LLM menu resolved: %r (first attempt)", choice)
        return _resolve_option_target(choice, resolver_def, options)

    # Retry with more constrained prompt
    logger.warning(
        "LLM menu: invalid response %r, retrying with constrained prompt",
        result.text[:100],
    )
    retry_prompt = (
        f"Invalid response. You must respond with exactly one of these words:\n"
        f"{', '.join(valid_option_names)}\n\n"
        f"Which one do you choose?"
    )
    retry_config = {"temperature": 0.0, "max_tokens": 20}
    retry_result = await effects.run_inference(retry_prompt, retry_config)

    if retry_result.error:
        raise LLMMenuResolverError(f"Inference retry failed: {retry_result.error}")

    choice = _parse_choice(retry_result.text, valid_option_names)
    if choice is not None:
        logger.debug("LLM menu resolved: %r (retry)", choice)
        return _resolve_option_target(choice, resolver_def, options)

    # Fall back to first option with warning
    fallback = next(iter(options))
    logger.warning(
        "LLM menu: retry also failed (got %r), falling back to first option: %r",
        retry_result.text[:100],
        fallback,
    )
    return _resolve_option_target(fallback, resolver_def, options)


def _resolve_option_target(
    choice: str,
    resolver_def: dict,
    options: dict[str, str],
) -> str:
    """Resolve the chosen option to a transition target step name.

    For static options, the option may have an explicit 'target' field.
    If not, the option name itself is the step name.

    For dynamic options (options_from), the option name/id is the step name.
    """
    static_options = resolver_def.get("options", {})

    if choice in static_options:
        opt_def = static_options[choice]
        if isinstance(opt_def, dict):
            # Explicit target overrides option name as step
            target = opt_def.get("target")
            if target:
                return target

    # Default: option name is the step name
    return choice
