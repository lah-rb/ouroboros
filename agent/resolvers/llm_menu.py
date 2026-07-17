"""LLM-driven menu resolver.

Presents the model with a constrained set of named options, each with a
description. The model responds with its choice as a JSON object. No GBNF
grammar — works naturally with Harmony channel models where grammar
constraints conflict with special token generation.

Extraction pipeline:
  1. FSM strips delimiters/thinking (server-side, already done by inference)
  2. Parse the JSON choice from the response via extract_choice()
  3. Retry loop (3 attempts) for robustness
  4. default_transition as final safety net

JSON-only extraction: if the model cannot produce a valid JSON object
with a "choice" key, the response is rejected. No substring matching,
no fuzzy heuristics. Models that cannot produce simple JSON are not fit
for agent work. Parse failures surface upstream issues (delimiter
stripping, prompt clarity, thinking token leakage) instead of silently
masking them.

Supports both static options (defined in CUE) and dynamic options
(read from a context key via options_from).
"""

from __future__ import annotations

import logging
import time
from typing import Any


from agent.trace import InferenceCall, count_tokens, trace_enabled

logger = logging.getLogger(__name__)


class LLMMenuResolverError(Exception):
    """Raised when LLM menu resolution fails."""

    pass


# ── Shared choice extraction ─────────────────────────────────────────


def extract_choice(response: str, valid_options: list[str]) -> str | None:
    """Extract a choice from a model response against known option names.

    JSON-only extraction: parses the response as JSON (with minor repair
    for common LLM quirks like trailing commas or missing quotes), then
    looks for a "choice" key whose value matches a valid option.

    If the model did not produce a valid JSON object with a "choice" key,
    returns None — letting the retry loop and default_transition handle it.

    Args:
        response: The model's response text (already FSM-stripped).
        valid_options: List of valid option names/keys.

    Returns:
        The matched option name, or None if no match found.
    """
    if not response or not valid_options:
        return None

    from agent.llm_json import parse_llm_json

    data = parse_llm_json(response)
    if not isinstance(data, dict):
        return None

    choice = data.get("choice")
    if not choice:
        return None

    return _match_option(str(choice), valid_options)


def _match_option(candidate: str, valid_options: list[str]) -> str | None:
    """Try to match a candidate string against valid options.

    Handles case differences and underscore/space/hyphen normalization.
    Normalizes both candidate and option for comparison.
    """
    candidate_clean = candidate.strip().lower()
    # Try raw match first (preserves hyphens, dots, slashes in IDs/paths)
    for opt in valid_options:
        if candidate_clean == opt.lower():
            return opt

    # Normalize separators and try again
    candidate_norm = candidate_clean.replace(" ", "_").replace("-", "_")
    for opt in valid_options:
        opt_norm = opt.lower().replace(" ", "_").replace("-", "_")
        if candidate_norm == opt_norm:
            return opt
        # Also try without separators for "file ops" → "fileops" → "file_ops"
        if candidate_norm.replace("_", "") == opt_norm.replace("_", ""):
            return opt
    return None


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

    # Static options from CUE
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


def build_menu_prompt(
    options: dict[str, str],
    *,
    context_line: str = "",
    instruction: str = "",
    style: str = "named",
) -> str:
    """Build a standardized menu prompt for LLM selection.

    Public API — used by both the CUE resolver path (_build_menu_prompt)
    and inline menu builders in mission_actions.py.

    Follows the --- ACTION REQUIRED --- menu schema:
      1. Header with action signal
      2. Optional context summary line
      3. Optional instruction
      4. Options in the chosen style
      5. JSON response instruction
      6. Input affordance cursor (>)

    Args:
        options: Mapping of option_key → description.
        context_line: One-line situational summary (optional).
        instruction: Directive text before the options (optional).
        style: How to render options:
            "named"   — "1) file_ops — desc"  (key is a semantic name, number is visual aid)
            "indexed" — "1) desc"             (key IS a number, description is the full label)
            "direct"  — "models.py — desc"    (key is the value itself, no numbering)

    Returns:
        The complete menu prompt string.
    """
    lines = ["--- ACTION REQUIRED ---"]

    if context_line:
        lines.append(context_line)

    lines.append("")

    if instruction:
        lines.append(instruction)
        lines.append("")

    lines.append("Choose ONE of these options:")
    lines.append("")

    for i, (name, description) in enumerate(options.items(), 1):
        if style == "named":
            # "1) file_ops         — Create, modify, refactor..."
            lines.append(f"  {i}) {name:17s} — {description}")
        elif style == "indexed":
            # "1) [pending] Create data models module → models.py"
            lines.append(f"  {name}) {description}")
        else:  # "direct"
            # "models.py            — models.py"
            lines.append(f"  {name:20s} — {description}")

    lines.append("")
    lines.append('Respond with {"choice": "<option_name>"}')
    lines.append("> ")

    return "\n".join(lines)


def _build_menu_prompt(
    resolver_prompt: str | None,
    options: dict[str, str],
    step_output_text: str | None = None,
) -> str:
    """Build a menu prompt for the CUE resolver path.

    Wraps build_menu_prompt with backward-compatible handling of
    step_output_text and resolver_prompt fields from CUE definitions.

    Returns:
        The prompt text.
    """
    # Build context line from step output (skip noop sentinels)
    context_line = ""
    if step_output_text and "no-op" not in step_output_text.lower():
        context_line = step_output_text[:200]

    return build_menu_prompt(
        options,
        context_line=context_line,
        instruction=resolver_prompt or "",
    )


async def resolve_llm_menu(
    resolver_def: dict,
    step_output: Any,
    context: dict,
    meta: dict,
    effects: Any = None,
) -> str:
    """Resolve transition by asking the LLM to choose from a menu of options.

    Asks the model to respond with a JSON object naming its choice.
    The FSM handles delimiter stripping on the server side. The response
    is parsed with extract_choice() which tries JSON parsing, quoted
    string extraction, and fuzzy matching as fallbacks.

    Retries up to 3 times before falling through to default_transition.

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

            # The accumulator contains flow inputs alongside context keys.
            # Pass it as both context and input so templates can reference
            # either namespace: {{context.diagnosis_context}} or
            # {{input.flow_directive}} both resolve correctly.
            template_vars = {"context": context, "input": context, "meta": meta}
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

    prompt = _build_menu_prompt(resolver_prompt, options, step_output_text)
    option_names = list(options.keys())

    # Trace helpers
    _can_trace = trace_enabled(effects)
    _t_mission = meta.get("mission_id", "") if meta else ""
    _t_cycle = meta.get("_trace_cycle", 0) if meta else 0
    _t_flow = meta.get("flow_name", "") if meta else ""
    _t_step = meta.get("step_id", "") if meta else ""

    # No max_tokens cap — let the model generate until it naturally
    # completes. Harmony models need to finish their analysis channel
    # thinking before emitting the final channel with the JSON choice.
    # Any cap risks truncating mid-thought, producing CoT as content.
    config = {"temperature": 0.1}
    tokens_in = count_tokens(prompt)
    infer_start = time.monotonic()

    # Session-aware: prefer inference_session_id over session_id to avoid
    # deadlocking when a terminal session has pinned the only pool instance.
    session_id = (
        context.get("inference_session_id")
        or context.get("edit_session_id")
        or context.get("session_id")
    )

    logger.info(
        "LLM menu resolve: %d options, session=%s, step=%s",
        len(options),
        session_id or "(stateless)",
        meta.get("step_id", "?") if meta else "?",
    )

    # ── Attempt loop: retry on invalid responses ──────────────────
    # All attempts use the session if available. On retries, we prepend
    # a correction note so the KV cache has new content (sending the
    # exact same prompt in a session produces empty output because the
    # model thinks it already answered). Falling back to stateless
    # inference would deadlock when pool_size=1 since the session holds
    # the only instance.
    max_attempts = 3
    last_response = ""
    for attempt in range(max_attempts):
        if attempt > 0:
            logger.warning(
                "LLM menu attempt %d/%d (previous response was %r)",
                attempt + 1,
                max_attempts,
                last_response[:80],
            )
            infer_start = time.monotonic()

        # Build the prompt — on retries, add correction with examples
        attempt_prompt = prompt
        if attempt > 0:
            example_option = option_names[0] if option_names else "option_name"
            attempt_prompt = (
                f"Your previous response was:\n"
                f"  {repr(last_response[:100])}\n\n"
                f"This is NOT valid. You must respond with ONLY a JSON object.\n\n"
                f"WRONG (extra text before JSON):\n"
                f'  Continue.{{"choice": "{example_option}"}}\n\n'
                f"WRONG (wrong key or structure):\n"
                f'  {{"action": "read_output"}}\n\n'
                f"CORRECT:\n"
                f'  {{"choice": "{example_option}"}}\n\n'
                f"Valid choices: {', '.join(option_names)}\n\n"
                f"Respond with ONLY the JSON object, nothing else.\n"
            )

        if session_id and hasattr(effects, "session_inference"):
            result = await effects.session_inference(session_id, attempt_prompt, config)
        else:
            result = await effects.run_inference(attempt_prompt, config)

        if _can_trace:
            _prompt_content = ""
            _response_content = ""
            if hasattr(effects, "trace_prompts") and effects.trace_prompts:
                _prompt_content = prompt if attempt == 0 else "(retry)"
                _response_content = result.text or ""

            await effects.emit_trace(
                InferenceCall(
                    mission_id=_t_mission,
                    cycle=_t_cycle,
                    flow=_t_flow,
                    step=_t_step,
                    tokens_in=tokens_in if attempt == 0 else 0,
                    tokens_out=count_tokens(result.text) if result.text else 0,
                    wall_ms=(time.monotonic() - infer_start) * 1000,
                    temperature=0.1,
                    max_tokens=0,
                    purpose="llm_menu_resolve",
                    prompt_content=_prompt_content,
                    response_content=_response_content,
                    truncated=getattr(result, "truncated", False),
                )
            )

        if result.error:
            logger.warning(
                "LLM menu inference error on attempt %d: %s", attempt + 1, result.error
            )
            last_response = ""
            continue

        last_response = result.text.strip() if result.text else ""
        choice = extract_choice(last_response, option_names)

        if choice:
            logger.debug(
                "LLM menu resolved: %r (attempt %d, raw response %r)",
                choice,
                attempt + 1,
                last_response[:60],
            )

            # Publish the selected option key to context if configured.
            publish_key = resolver_def.get("publish_selection")
            if publish_key:
                context[publish_key] = choice

            return _resolve_option_target(choice, resolver_def)

        # Invalid response — will retry if attempts remain
        logger.warning(
            "LLM menu could not extract choice from response %r on attempt %d/%d",
            last_response[:80],
            attempt + 1,
            max_attempts,
        )

    # ── All attempts exhausted ───────────────────────────────────
    publish_key = resolver_def.get("publish_selection")

    default = resolver_def.get("default_transition")
    if default:
        logger.warning(
            "LLM menu exhausted %d attempts, using default_transition: %s",
            max_attempts,
            default,
        )
        if publish_key:
            for opt_key, opt_val in options.items():
                opt_target = (
                    opt_val.get("target", "") if isinstance(opt_val, dict) else ""
                )
                if opt_target == default:
                    context[publish_key] = opt_key
                    logger.info(
                        "LLM menu default: publishing %s=%r (first option targeting %r)",
                        publish_key,
                        opt_key,
                        default,
                    )
                    break
            else:
                first_key = next(iter(options))
                context[publish_key] = first_key
                logger.warning(
                    "LLM menu default: no option targets %r, publishing first: %s=%r",
                    default,
                    publish_key,
                    first_key,
                )
        return default

    # Final safety fallback
    logger.warning(
        "LLM menu exhausted %d attempts with no default_transition, using first option",
        max_attempts,
    )
    fallback = next(iter(options))
    if publish_key:
        context[publish_key] = fallback
    return _resolve_option_target(fallback, resolver_def)


def _resolve_option_target(
    choice: str,
    resolver_def: dict,
) -> str:
    """Resolve the chosen option to a transition target step name.

    Two modes (set via 'mode' field in resolver_def):

      "route" (default) — the choice determines the transition. For static
          options, an explicit 'target' field overrides the option key.
          For dynamic options, the option key is used as the step name.

      "select" — the choice is a data value (already published via
          publish_selection). Transition always goes to default_transition.
          Used for dynamic menus where option keys are filenames, goal IDs,
          or other non-step data.
    """
    mode = resolver_def.get("mode", "route")

    if mode == "select":
        default = resolver_def.get("default_transition")
        if default:
            return default
        raise LLMMenuResolverError("llm_menu mode='select' requires default_transition")

    # mode == "route": option key determines the transition
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
