"""LLM-driven menu resolver.

Presents the model with a constrained set of named options, each with a
description. Uses GBNF grammar-based constrained decoding to guarantee
valid output at the token level — the model can only produce a valid
letter key. No parsing, no retries, no fallback.

Supports both static options (defined in YAML) and dynamic options
(read from a context key via options_from).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent.trace import InferenceCall, count_tokens

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
) -> tuple[str, str, dict[str, str]]:
    """Build the constrained menu prompt with GBNF grammar.

    Returns:
        Tuple of (prompt_text, gbnf_grammar, letter_to_option_map)
    """
    lines = []

    if step_output_text:
        lines.append("Here is what just happened:")
        lines.append(step_output_text[:1500])
        lines.append("")

    if resolver_prompt:
        lines.append(resolver_prompt)
        lines.append("")

    lines.append("Choose ONE option by responding with its letter:")
    lines.append("")

    letters = "abcdefghijklmnopqrstuvwxyz"
    letter_map = {}

    for i, (name, description) in enumerate(options.items()):
        letter = letters[i]
        letter_map[letter] = name
        lines.append(f"  [{letter}] {description}")

    lines.append("")
    lines.append("Respond with ONLY the letter (a, b, c, etc.).")

    valid_letters = list(letter_map.keys())
    grammar_alternatives = " | ".join(f'"{l}"' for l in valid_letters)
    gbnf = f"root ::= {grammar_alternatives}"

    return "\n".join(lines), gbnf, letter_map


async def resolve_llm_menu(
    resolver_def: dict,
    step_output: Any,
    context: dict,
    meta: dict,
    effects: Any = None,
) -> str:
    """Resolve transition by asking the LLM to choose from a menu of options.

    Uses GBNF grammar-based constrained decoding to guarantee valid output.
    The model can only produce a single valid letter key — no parsing,
    no retries, no fallback needed.

    The resolver prompt supports Jinja2 template syntax. Templates are
    rendered against {context, input, meta} so the prompt can include
    dynamic information like ``{{ context.director_analysis }}``.

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
        LLMMenuResolverError: If resolution fails.
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

    # Render the resolver prompt through Jinja2 if it contains templates
    resolver_prompt = resolver_def.get("prompt")
    if resolver_prompt and ("{{" in resolver_prompt or "{%" in resolver_prompt):
        try:
            from agent.template import render_template

            template_vars = {"context": context, "input": {}, "meta": meta}
            resolver_prompt = render_template(resolver_prompt, template_vars)
        except Exception as e:
            logger.warning("Failed to render resolver prompt template: %s", e)
            # Fall back to raw prompt string

    # Build the prompt, optionally including step output as context
    step_output_text = None
    if resolver_def.get("include_step_output", False) and step_output is not None:
        # For inference steps, prefer the actual generated text over
        # the generic "Inference completed: N tokens" observations string
        if hasattr(step_output, "result") and isinstance(step_output.result, dict):
            text = step_output.result.get("text")
            if text:
                step_output_text = str(text)[:1500]
        if not step_output_text:
            if hasattr(step_output, "observations") and step_output.observations:
                step_output_text = str(step_output.observations)
            elif hasattr(step_output, "result") and step_output.result:
                step_output_text = str(step_output.result)[:1500]
        if not step_output_text and isinstance(step_output, str):
            step_output_text = step_output

    prompt, gbnf_grammar, letter_map = _build_menu_prompt(
        resolver_prompt, options, step_output_text
    )

    # Trace helpers
    _can_trace = hasattr(effects, "emit_trace")
    _t_mission = meta.get("mission_id", "") if meta else ""
    _t_cycle = meta.get("_trace_cycle", 0) if meta else 0
    _t_flow = meta.get("flow_name", "") if meta else ""
    _t_step = meta.get("step_id", "") if meta else ""

    config = {"temperature": 0.1, "max_tokens": 5, "grammar": gbnf_grammar}
    tokens_in = count_tokens(prompt)
    infer_start = time.monotonic()

    # Session-aware: if an inference session is in context, route through
    # the memoryful session to avoid deadlocking when the session has
    # already pinned the only available pool instance.
    # IMPORTANT: prefer inference_session_id over session_id — flows like
    # run_in_terminal publish BOTH a terminal session_id (for shell commands)
    # and an inference_session_id (for LLM calls).
    session_id = (
        context.get("inference_session_id")
        or context.get("edit_session_id")
        or context.get("session_id")
    )
    if session_id and hasattr(effects, "session_inference"):
        logger.debug(
            "LLM menu using memoryful session %s",
            session_id,
        )
        result = await effects.session_inference(session_id, prompt, config)
    else:
        result = await effects.run_inference(prompt, config)

    if _can_trace:
        # Capture prompt/response when --trace-prompts is set
        _prompt_content = ""
        _response_content = ""
        if hasattr(effects, "trace_prompts") and effects.trace_prompts:
            _prompt_content = prompt
            _response_content = result.text or ""

        await effects.emit_trace(
            InferenceCall(
                mission_id=_t_mission,
                cycle=_t_cycle,
                flow=_t_flow,
                step=_t_step,
                tokens_in=tokens_in,
                tokens_out=count_tokens(result.text) if result.text else 0,
                wall_ms=(time.monotonic() - infer_start) * 1000,
                temperature=0.1,
                max_tokens=5,
                purpose="llm_menu_resolve",
                prompt_content=_prompt_content,
                response_content=_response_content,
            )
        )

    if result.error:
        # Check for default_transition before raising
        default = resolver_def.get("default_transition")
        if default:
            logger.warning(
                "LLM menu inference failed (%s), using default_transition: %s",
                result.error,
                default,
            )
            return default
        raise LLMMenuResolverError(f"Inference failed: {result.error}")

    # Grammar constraint guarantees a valid letter
    letter = result.text.strip().lower() if result.text else ""
    if letter in letter_map:
        choice = letter_map[letter]
        logger.debug("LLM menu resolved: %r (letter %r)", choice, letter)
        return _resolve_option_target(choice, resolver_def, options)

    # Bandaid: chat template delimiter leak (e.g. "b<|" instead of "b").
    # Some models emit the start of their EOS token (<|im_end|>, <|endoftext|>)
    # after the constrained letter, before the stop token fully fires.
    # Recover by checking if the response starts with a valid letter.
    if letter and letter[0] in letter_map:
        recovered = letter[0]
        logger.warning(
            "LLM menu recovered valid letter %r from leaked response %r "
            "(likely chat template delimiter leak)",
            recovered,
            letter,
        )
        choice = letter_map[recovered]
        return _resolve_option_target(choice, resolver_def, options)

    # Model returned empty or invalid response despite grammar
    default = resolver_def.get("default_transition")
    if default:
        logger.warning(
            "LLM menu got invalid response %r, using default_transition: %s",
            letter,
            default,
        )
        return default

    # Final safety fallback (should never fire with grammar)
    logger.warning(
        "Grammar response %r not in letter_map, using first option as fallback", letter
    )
    fallback = next(iter(options))
    return _resolve_option_target(fallback, resolver_def, options)


async def resolve_llm_multi_select(
    resolver_def: dict,
    step_output: Any,
    context: dict,
    meta: dict,
    effects: Any = None,
) -> str:
    """Multi-select resolver using memoryful session.

    Opens a session, presents options with letter keys, collects
    selections one at a time. Empty response terminates selection.
    Grammar constraint changes each turn to exclude already-selected
    options and always include empty string.

    The selected items are stored in meta['_multi_select_result'] for
    the runtime to inject into context_updates.

    Args:
        resolver_def: The resolver definition with options and prompt.
        step_output: The output from the step's action.
        context: The current context accumulator.
        meta: Flow execution metadata (will receive _multi_select_result).
        effects: Effects interface (must have session methods).

    Returns:
        The transition target (from 'target' field, or 'items_selected'/'none_selected').
    """
    if effects is None:
        raise LLMMenuResolverError(
            "LLM multi-select resolver requires effects interface."
        )

    options = _build_options_list(resolver_def, context)
    if not options:
        raise LLMMenuResolverError("No options available for multi-select.")

    option_keys = list(options.keys())
    letters = "abcdefghijklmnopqrstuvwxyz"
    letter_to_option = {letters[i]: key for i, key in enumerate(option_keys)}
    selected: list[str] = []

    session_id = await effects.start_inference_session({"ttl_seconds": 120})

    try:
        # Build initial prompt
        prompt = _build_multi_select_prompt(
            resolver_def.get("prompt"),
            options,
            letter_to_option,
            selected=[],
        )

        while True:
            # Grammar: available letters + empty string
            available = [
                l for l, opt in letter_to_option.items() if opt not in selected
            ]
            if not available:
                break  # All selected

            grammar_parts = [f'"{l}"' for l in available] + ['""']
            gbnf = f"root ::= {' | '.join(grammar_parts)}"

            config = {"temperature": 0.1, "max_tokens": 5, "grammar": gbnf}
            result = await effects.session_inference(session_id, prompt, config)

            choice = result.text.strip().lower()

            if choice == "" or choice not in letter_to_option:
                break  # Empty string = done selecting

            selected_option = letter_to_option[choice]
            selected.append(selected_option)

            # Next turn prompt: just the status update
            selected_names = ", ".join(selected)
            prompt = (
                f"Selected so far: {selected_names}\nSelect next (empty to finish):"
            )

    finally:
        await effects.end_inference_session(session_id)

    # Store selections in meta for the runtime to inject
    meta["_multi_select_result"] = selected

    # Determine transition target
    return _resolve_multi_select_target(selected, resolver_def)


def _build_multi_select_prompt(
    resolver_prompt: str | None,
    options: dict[str, str],
    letter_map: dict[str, str],
    selected: list[str],
) -> str:
    """Build the multi-select prompt with letter options."""
    lines = []
    if resolver_prompt:
        lines.append(resolver_prompt)
        lines.append("")

    for letter, opt_key in letter_map.items():
        desc = options[opt_key]
        marker = " ✓" if opt_key in selected else ""
        lines.append(f"  [{letter}] {desc}{marker}")

    lines.append("")
    if selected:
        lines.append(f"Selected: {', '.join(selected)}")
    else:
        lines.append("Selected: none")
    lines.append("")
    lines.append("Enter a letter to select, or empty to finish.")

    return "\n".join(lines)


def _resolve_multi_select_target(
    selected: list[str],
    resolver_def: dict,
) -> str:
    """Determine the transition target for multi-select.

    Uses 'target' from resolver_def if specified, otherwise
    'items_selected' or 'none_selected' based on selection count.
    """
    explicit_target = resolver_def.get("target")
    if explicit_target:
        return explicit_target

    if selected:
        return resolver_def.get("target_selected", "items_selected")
    else:
        return resolver_def.get("target_none", "none_selected")


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
