"""Flow executor — the core runtime that executes flow definitions.

Validates inputs → initializes accumulator → loops through steps
(build StepInput → execute action → merge context_updates → resolve
transition → repeat) until a terminal step is reached → returns FlowResult.

Uses the CUE pipeline: $ref resolution for params/input_map,
structured prompt templates with pre_compute formatters,
and result formatters for tail_call messages.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from agent.actions.registry import ActionNotFoundError, ActionRegistry
from agent.errors import (
    FlowRuntimeError,
    MaxStepsExceeded,
    MissingContextError,
    MissingInputError,
)
from agent.trace import (
    StepStart,
    StepEnd,
    InferenceCall,
    FlowInvoke,
    FlowReturn,
    count_tokens,
    step_context,
)
from agent.models import (
    FlowDefinition,
    FlowExecution,
    FlowMeta,
    FlowResult,
    StepDefinition,
    StepInput,
    StepOutput,
    action_type_for,
)
from agent.resolvers import resolve, ResolverError
from agent.loader import (
    resolve_params,
    resolve_input_map,
    resolve_value,
    run_pre_compute,
    assemble_returns,
    PromptRenderer,
)
from agent.turn_renderer import TurnRenderer

logger = logging.getLogger(__name__)

# ── Ambient context keys ─────────────────────────────────────────────
#
# Keys in this whitelist bypass the per-step context filter — they flow
# through every step of a flow execution without each step having to
# declare them in `context.optional`. The mechanism is deliberately
# narrow: ambient context is for cross-step bookkeeping that is an
# implementation detail of a subsystem (session memory, etc.), not a
# substitute for declared data plumbing.
#
# Adding a key here is an explicit act — resist making this a dumping
# ground. The name should read as "this is infrastructure, not
# application data." Before adding, ask: does every consumer of this
# key know it's ambient, or does declaring it in CUE make the data
# flow clearer? When in doubt, declare it.
#
# Current ambient keys:
#
#   session_injections — per-session prompt-injection queue. Producers
#     (actions that want to feed text into a memoryful session without
#     wasting an inference turn) call `agent.session_injections.queue()`
#     which writes this key. Consumers (actions about to call
#     session_inference) call `consume()` which reads and clears it.
#     Ambient because it is a correctness-critical side channel for
#     the session memory protocol, not application data the flow
#     author reasons about. See agent/session_injections.py.
#
#   inference_session_id — handle for a memoryful inference session
#     started by an upstream action (start_diagnosis_session,
#     start_interactive_session, etc.). Ambient because runtime's
#     turn-inference dispatcher (_execute_turn_inference) uses it to
#     decide between stateless `run_inference` and memoryful
#     `session_inference`. Forcing every turn step in every flow to
#     redeclare this in `context.optional` was actively harmful: the
#     a85f381e trace showed 14 diagnose_issue cycles where pick_file
#     and pick_action each hit a 180s timeout because the filter
#     stripped inference_session_id, the turn went stateless, seed
#     injections never consumed, and the model sat thinking on a
#     bare menu. Ambient is the right treatment — like
#     session_injections, this is infrastructure plumbing for the
#     session protocol, not flow-author-visible data.

_AMBIENT_CONTEXT_KEYS: frozenset[str] = frozenset(
    {"session_injections", "inference_session_id"}
)

# Module-level prompt renderer — initialized lazily
_prompt_renderer: PromptRenderer | None = None


def init_prompt_renderer(prompts_dir: str | Path = "prompts") -> None:
    """Initialize the prompt renderer with a specific directory."""
    global _prompt_renderer
    _prompt_renderer = PromptRenderer(prompts_dir)


def _get_prompt_renderer() -> PromptRenderer:
    """Get or initialize the prompt renderer."""
    global _prompt_renderer
    if _prompt_renderer is None:
        for candidate in [Path("prompts"), Path("ouroboros/prompts")]:
            if candidate.exists():
                _prompt_renderer = PromptRenderer(candidate)
                return _prompt_renderer
        _prompt_renderer = PromptRenderer(Path("prompts"))
    return _prompt_renderer


# Module-level turn renderer — initialized lazily, resolves to the same
# prompts/ directory as the legacy PromptRenderer but uses the new
# per-section template format (see agent/turn_renderer.py).
_turn_renderer: TurnRenderer | None = None


def _get_turn_renderer() -> TurnRenderer:
    """Get or initialize the turn renderer.

    Resolves to the same prompts directory the legacy renderer uses.
    Turn-format templates (single-section, `id:`+`content:` shape) live
    alongside legacy aggregated templates in prompts/; TurnRenderer
    rejects legacy format with a clear error if one is referenced.
    """
    global _turn_renderer
    if _turn_renderer is None:
        for candidate in [Path("prompts"), Path("ouroboros/prompts")]:
            if candidate.exists():
                _turn_renderer = TurnRenderer(candidate)
                return _turn_renderer
        _turn_renderer = TurnRenderer(Path("prompts"))
    return _turn_renderer


def render_turn_prompt(turn: Any, namespaces: dict[str, Any]) -> str:
    """Public helper for wrapper actions (Sites #10, #11 pattern).

    Renders a turn declaration into the full prompt string, using the
    same renderer the runtime's inference path uses. Wrapper actions
    that drive inference themselves (e.g., `rewrite_symbol_turn` with
    kind validation) call this to keep prompt construction in the
    schema rather than in Python.

    Args:
        turn: The TurnDefinition (typically step_input.turn).
        namespaces: {"input": ..., "context": ..., "meta": ...}

    Returns:
        The rendered prompt string.
    """
    return _get_turn_renderer().render(turn, namespaces)


def extract_turn_menu_choice(
    turn: Any, namespaces: dict[str, Any], response_text: str
) -> str | None:
    """Public helper for wrapper actions driving menu-shape turns.

    Parses a model response against the turn's resolved option keys,
    returning the matched key or None. Same resolution path the
    runtime's own turn dispatch uses — keys seen by the model are
    exactly keys the extractor matches against.
    """
    return _extract_menu_choice(turn, namespaces, response_text)


def _safe_float_temp(val: Any) -> float:
    """Safely convert a temperature value to float, handling t* specifiers."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        if val.startswith("t*"):
            try:
                return float(val[2:])
            except ValueError:
                return 0.0
        try:
            return float(val)
        except ValueError:
            return 0.0
    return 0.0


# ── Cache-aware token readers ─────────────────────────────────────────
#
# The InferenceResult may carry real backend token counts (cached_prefix /
# fresh_prefill / generated) once LLMVP reports them. These read with getattr
# defaults so they degrade gracefully: against a server that doesn't yet
# return the fields, the cache fields are 0 and tokens_in/out fall back to
# the whitespace approximation — no behavior change until the server upgrades.


def _cache_fields(result: Any) -> dict:
    """Cache-aware token fields for an InferenceCall, read off the result."""
    return {
        "cached_prefix_tokens": int(getattr(result, "cached_prefix_tokens", 0) or 0),
        "fresh_prefill_tokens": int(getattr(result, "fresh_prefill_tokens", 0) or 0),
        "generated_tokens": int(getattr(result, "generated_tokens", 0) or 0),
        "cache_hit": bool(getattr(result, "cache_hit", False)),
        "flow_key": str(getattr(result, "flow_key", "") or ""),
    }


def _real_in(result: Any, ws_in: int) -> int:
    """Real input tokens (cached_prefix + fresh_prefill) when the backend
    reports them, else the whitespace fallback."""
    cp = int(getattr(result, "cached_prefix_tokens", 0) or 0)
    fp = int(getattr(result, "fresh_prefill_tokens", 0) or 0)
    return (cp + fp) if (cp or fp) else ws_in


def _real_out(result: Any, ws_out: int) -> int:
    """Real generated tokens when the backend reports them, else whitespace."""
    gen = int(getattr(result, "generated_tokens", 0) or 0)
    return gen if gen else ws_out


async def execute_flow(
    flow_def: FlowDefinition,
    inputs: dict[str, Any],
    action_registry: ActionRegistry,
    max_steps: int = 100,
    effects: Any = None,
    flow_registry: dict[str, FlowDefinition] | None = None,
) -> FlowResult:
    """Execute a flow definition to completion.

    This is the main entry point for the flow engine. It:
    1. Validates that all required inputs are provided.
    2. Initializes the context accumulator with inputs.
    3. Loops: build StepInput → execute action → merge context_updates
       → resolve transition → repeat.
    4. Stops when a terminal step is reached.
    5. Returns a FlowResult with the terminal status and accumulated context.

    Args:
        flow_def: The validated flow definition to execute.
        inputs: Dictionary of input values for the flow.
        action_registry: Registry of available action callables.
        max_steps: Maximum number of steps before aborting (infinite loop guard).
        effects: Effects interface instance (LocalEffects, MockEffects, etc.).
            Passed to each step via StepInput.effects and to resolvers.

    Returns:
        FlowResult with the terminal status, result, and execution history.

    Raises:
        MissingInputError: If required inputs are not provided.
        MaxStepsExceeded: If step count exceeds max_steps.
        FlowRuntimeError: On other runtime errors.
    """
    # Validate required inputs
    _validate_inputs(flow_def, inputs)

    # Initialize execution state
    execution = FlowExecution(
        flow_name=flow_def.flow,
        current_step=flow_def.entry,
        accumulator=dict(inputs),  # Start accumulator with flow inputs
        max_steps=max_steps,
    )

    # Track per-step visit counts for retry-with-limit patterns
    step_visits: dict[str, int] = {}

    logger.info(
        "Starting flow %r at entry step %r",
        flow_def.flow,
        flow_def.entry,
    )

    # Extract trace context from synthetic inputs (set by loop.py)
    _trace_mission_id = inputs.get("mission_id", "")
    _trace_cycle = inputs.get("_trace_cycle", 0)
    _can_trace = effects is not None and hasattr(effects, "emit_trace")

    # Main execution loop
    try:
        while execution.step_count < execution.max_steps:
            step_name = execution.current_step
            step_def = flow_def.steps[step_name]

            # Increment visit count for this step
            step_visits[step_name] = step_visits.get(step_name, 0) + 1

            logger.debug(
                "Flow %r: executing step %r (step #%d, visit #%d)",
                flow_def.flow,
                step_name,
                execution.step_count + 1,
                step_visits[step_name],
            )

            # Build StepInput with filtered context and effects. Timed for the
            # finite breakdown — this runs before step_start_time, so without
            # capturing it the context-filter/$ref-resolve cost would vanish
            # into the residual.
            _input_build_start = time.monotonic()
            step_input = _build_step_input(
                step_def=step_def,
                step_name=step_name,
                flow_def=flow_def,
                accumulator=execution.accumulator,
                inputs=inputs,
                effects=effects,
            )

            # ── Trace: StepStart ─────────────────────────────────────
            step_start_time = time.monotonic()
            if _can_trace:
                await effects.emit_trace(
                    StepStart(
                        mission_id=_trace_mission_id,
                        cycle=_trace_cycle,
                        flow=flow_def.flow,
                        step=step_name,
                        action_type=action_type_for(step_def.action),
                        action=step_def.action,
                        context_consumed=list(step_input.context.keys()),
                        context_required=list(step_def.context.required),
                        input_build_ms=(step_start_time - _input_build_start) * 1000,
                    )
                )

            # Execute the action — special handling for 'inference' and 'flow'.
            #
            # The step_context binding is the runtime's half of the §4.7
            # contextvars plumbing: it makes mission_id/cycle/flow/step
            # available to any effect method called from inside the action,
            # without every effect signature having to accept a trace_context
            # parameter. The primary consumer today is
            # LocalEffects.session_inference — inference turns fired from
            # inside diagnosis/edit actions read the context to emit
            # attributed InferenceCall trace events.
            with step_context(
                mission_id=_trace_mission_id,
                cycle=_trace_cycle,
                flow=flow_def.flow,
                step=step_name,
            ):
                if step_def.action == "inference":
                    step_output = await _execute_inference_action(
                        step_def=step_def,
                        step_input=step_input,
                        flow_def=flow_def,
                        inputs=inputs,
                        effects=effects,
                        _trace_mission_id=_trace_mission_id,
                        _trace_cycle=_trace_cycle,
                        _step_name=step_name,
                    )
                elif step_def.action == "flow":
                    step_output = await _execute_subflow_action(
                        step_def=step_def,
                        step_name=step_name,
                        flow_def=flow_def,
                        accumulator=execution.accumulator,
                        inputs=inputs,
                        action_registry=action_registry,
                        effects=effects,
                        flow_registry=flow_registry,
                        _trace_mission_id=_trace_mission_id,
                        _trace_cycle=_trace_cycle,
                    )
                else:
                    try:
                        action_fn = action_registry.get(step_def.action)
                    except ActionNotFoundError as e:
                        raise FlowRuntimeError(f"Step {step_name!r}: {e}") from e
                    # Run pre_compute for custom actions before
                    # dispatch. The inference action path runs this
                    # internally (see _execute_inference_action line
                    # 837); custom action wrappers (rewrite_symbol_turn,
                    # select_symbol_turn, etc.) need it here or their
                    # declared pre_compute formatters never fire. b75
                    # regression: pre_compute was declared on the
                    # rewrite_symbol step but silently ignored — the
                    # instruction section's Ref-typed template field
                    # resolved empty and the section dropped (required
                    # defaults to False). The call_graph_block and
                    # kind_instruction_template keys vanished from
                    # rendered prompts entirely.
                    if step_def.pre_compute:
                        _pc_namespaces = {
                            "input": dict(inputs),
                            "context": dict(step_input.context),
                            "meta": {
                                "flow_name": flow_def.flow,
                                "step_id": step_input.meta.step_id,
                            },
                        }
                        _pc_computed = run_pre_compute(
                            step_def.pre_compute, _pc_namespaces
                        )
                        step_input.context.update(_pc_computed)
                    try:
                        step_output = await action_fn(step_input)
                    except Exception as e:
                        raise FlowRuntimeError(
                            f"Action {step_def.action!r} failed in step {step_name!r}: {e}"
                        ) from e

            # Record execution
            execution.steps_executed.append(step_name)
            execution.step_count += 1
            if step_output.observations:
                execution.observations.append(
                    f"[{step_name}] {step_output.observations}"
                )

            # Merge context updates into the accumulator
            if step_output.context_updates:
                execution.accumulator.update(step_output.context_updates)
                logger.debug(
                    "Step %r published context keys: %s",
                    step_name,
                    list(step_output.context_updates.keys()),
                )

            # Check for terminal step
            if step_def.terminal:
                # ── Trace: StepEnd (terminal) ─────────────────────────
                if _can_trace:
                    await effects.emit_trace(
                        StepEnd(
                            mission_id=_trace_mission_id,
                            cycle=_trace_cycle,
                            flow=flow_def.flow,
                            step=step_name,
                            published=list((step_output.context_updates or {}).keys()),
                            resolver_type="terminal",
                            resolver_decision=step_def.status or "completed",
                            step_duration_ms=(
                                (time.monotonic() - step_start_time) * 1000
                            ),
                        )
                    )
                logger.info(
                    "Flow %r reached terminal step %r with status %r",
                    flow_def.flow,
                    step_name,
                    step_def.status,
                )

                # Assemble structured returns from the flow's declaration
                structured_returns = assemble_returns(
                    flow_def,
                    execution.accumulator,
                    inputs,
                )

                # Merge structured returns into result dict
                result = dict(step_output.result)
                if structured_returns:
                    result["_returns"] = structured_returns

                return FlowResult(
                    status=step_def.status or "completed",
                    result=result,
                    context=execution.accumulator,
                    steps_executed=execution.steps_executed,
                    observations=execution.observations,
                )

            # Check for tail-call step (non-terminal step with tail_call block)
            if step_def.tail_call:
                # Resolve $ref in the tail_call flow name once so both the trace
                # event and the log line show the actual target flow rather than
                # the raw {'$ref': '...'} dict. The outer loop will re-resolve at
                # dispatch time — this is display-only.
                tail_flow_raw = step_def.tail_call.get("flow")
                resolved_tail = resolve_value(
                    tail_flow_raw,
                    {
                        "input": inputs,
                        "context": execution.accumulator,
                        "result": step_output.result,
                        "meta": {},
                    },
                )
                tail_flow_display = (
                    resolved_tail
                    if isinstance(resolved_tail, str) and resolved_tail
                    else str(tail_flow_raw) if tail_flow_raw is not None else "unknown"
                )

                # ── Trace: StepEnd (tail_call) ────────────────────────
                if _can_trace:
                    await effects.emit_trace(
                        StepEnd(
                            mission_id=_trace_mission_id,
                            cycle=_trace_cycle,
                            flow=flow_def.flow,
                            step=step_name,
                            published=list((step_output.context_updates or {}).keys()),
                            resolver_type="tail_call",
                            resolver_decision=tail_flow_display,
                            step_duration_ms=(
                                (time.monotonic() - step_start_time) * 1000
                            ),
                        )
                    )
                logger.info(
                    "Flow %r: step %r triggers tail call to %r",
                    flow_def.flow,
                    step_name,
                    tail_flow_display,
                )
                return FlowResult(
                    status=step_def.status or "tail_call",
                    result=step_output.result,
                    context=execution.accumulator,
                    steps_executed=execution.steps_executed,
                    observations=execution.observations,
                    tail_call=step_def.tail_call,
                )

            # Resolve the next transition (now async to support LLM menu).
            # Dispatch rule:
            #   - Turn-based step with action=="inference" (runtime owns
            #     turn execution) → turn.transitions route from the
            #     turn_outcome flag set by _execute_turn_inference.
            #   - Turn-based step with a custom action (Batch E pattern —
            #     wrapper action drives the turn itself) → step.resolver
            #     decides, reading action-wrapper result flags. The turn's
            #     transitions block is fallback-only for the runtime's
            #     built-in dispatch and doesn't bind wrapper-driven flows.
            # Finite-time breakdown: the transition-resolver span (rule eval or
            # llm_menu) after the action returns. An llm_menu resolve also emits
            # its own InferenceCall; this captures the resolver's own overhead.
            _resolver_start = time.monotonic()
            turn_runtime_owned = (
                step_def.turn is not None and step_def.action == "inference"
            )
            if turn_runtime_owned:
                next_step = _resolve_turn_transition(step_def.turn, step_output)
            else:
                if not step_def.resolver:
                    raise FlowRuntimeError(
                        f"Step {step_name!r} is not terminal and has no resolver — "
                        f"cannot determine next step."
                    )

                try:
                    next_step = await resolve(
                        resolver_def=step_def.resolver.model_dump(),
                        step_output=step_output,
                        context=execution.accumulator,
                        meta={
                            "flow_name": flow_def.flow,
                            "step_id": step_name,
                            "attempt": step_visits[step_name],
                            "step_count": execution.step_count,
                            "step_visits": dict(step_visits),
                        },
                        effects=effects,
                    )
                except ResolverError as e:
                    raise FlowRuntimeError(
                        f"Resolver failed for step {step_name!r}: {e}"
                    ) from e

            # Validate the transition target exists
            if next_step not in flow_def.steps:
                raise FlowRuntimeError(
                    f"Step {step_name!r}: resolver returned transition target "
                    f"{next_step!r} which doesn't exist in the flow."
                )

            # ── Trace: StepEnd (transition) ───────────────────────────
            if _can_trace:
                if turn_runtime_owned:
                    # Turn-based step: resolver_type is "turn_transitions",
                    # options are the keys in turn.transitions.options (if
                    # any) plus the default/no_answer sentinels.
                    resolver_type = "turn_transitions"
                    options = []
                    tt = step_def.turn.transitions
                    if tt.options:
                        options = list(tt.options.keys())
                    options += [tt.default, tt.no_answer]
                else:
                    resolver_type = step_def.resolver.type if step_def.resolver else ""
                    # Collect available transition options
                    options = []
                    if step_def.resolver:
                        rd = step_def.resolver.model_dump()
                        if rd.get("rules"):
                            options = [r.get("transition", "") for r in rd["rules"]]
                        elif rd.get("options"):
                            options = list(rd["options"].keys())
                await effects.emit_trace(
                    StepEnd(
                        mission_id=_trace_mission_id,
                        cycle=_trace_cycle,
                        flow=flow_def.flow,
                        step=step_name,
                        published=list((step_output.context_updates or {}).keys()),
                        resolver_type=resolver_type,
                        resolver_decision=next_step,
                        options_available=options,
                        step_duration_ms=((time.monotonic() - step_start_time) * 1000),
                        resolver_ms=((time.monotonic() - _resolver_start) * 1000),
                    )
                )

            logger.debug(
                "Step %r → transition to %r",
                step_name,
                next_step,
            )
            execution.current_step = next_step

    except Exception as e:
        # Any uncaught exception inside a step (action, resolver, or
        # sub-flow) would otherwise escape with orphaned sessions
        # still live in LLMVP's session manager. Release them before
        # re-raising so the backend's limited instance slots aren't
        # held by dead flow state. This mirrors the MaxStepsExceeded
        # cleanup below but covers the unbounded-exception case.
        logger.warning(
            "Flow %r aborted by exception at step %r: %s",
            flow_def.flow,
            execution.current_step,
            e,
        )
        await _cleanup_orphaned_sessions(
            execution.accumulator,
            effects,
            reason=f"flow {flow_def.flow!r} aborted by exception",
        )
        raise

    # If we get here, we exceeded max steps.
    # Before raising, clean up any active sessions that would have been
    # released by the flow's terminal step (which never ran).
    await _cleanup_orphaned_sessions(
        execution.accumulator,
        effects,
        reason=f"flow {flow_def.flow!r} exceeded max_steps",
    )

    raise MaxStepsExceeded(
        f"Flow {flow_def.flow!r} exceeded maximum step count ({max_steps}). "
        f"Steps executed: {execution.steps_executed}. "
        f"Last step: {execution.current_step!r}. "
        f"This likely indicates an infinite loop."
    )


async def _cleanup_orphaned_sessions(
    accumulator: dict,
    effects: Any,
    reason: str,
) -> None:
    """Release any inference/edit sessions still live in the accumulator.

    Called when a flow exits abnormally — via MaxStepsExceeded or an
    uncaught exception — so that the LLMVP backend's limited session
    slots are not held by orphaned handles. Best-effort: never raises,
    so it can be called from exception handlers without risk.

    MCP-managed terminal sessions are NOT ended here; they're tied to
    their parent action (close_interactive_session) and will clean up
    through their own lifecycle.
    """
    if effects is None:
        return
    for key in ("inference_session_id", "edit_session_id"):
        sid = accumulator.get(key, "")
        if not sid:
            continue
        if not hasattr(effects, "end_inference_session"):
            continue
        try:
            await effects.end_inference_session(sid)
            logger.info("Cleaned up orphaned inference session %s (%s)", sid, reason)
        except Exception:
            pass  # Best-effort


async def _execute_subflow_action(
    step_def: StepDefinition,
    step_name: str,
    flow_def: FlowDefinition,
    accumulator: dict[str, Any],
    inputs: dict[str, Any],
    action_registry: ActionRegistry,
    effects: Any,
    flow_registry: dict[str, FlowDefinition] | None,
    _trace_mission_id: str = "",
    _trace_cycle: int = 0,
) -> StepOutput:
    """Execute a sub-flow invocation (action='flow').

    Looks up the target sub-flow from the registry, resolves the input_map
    templates, recursively calls execute_flow, and converts the sub-flow
    result into a StepOutput for the parent flow.

    The sub-flow's terminal context keys that match the parent step's
    'publishes' list are propagated back as context_updates.
    """
    target_flow_name = step_def.flow
    if not target_flow_name:
        raise FlowRuntimeError(
            f"Step {step_name!r}: action='flow' requires a 'flow' field "
            f"naming the target sub-flow."
        )

    if flow_registry is None:
        raise FlowRuntimeError(
            f"Step {step_name!r}: action='flow' requires a flow_registry "
            f"but none was provided to execute_flow."
        )

    if target_flow_name not in flow_registry:
        raise FlowRuntimeError(
            f"Step {step_name!r}: target sub-flow {target_flow_name!r} "
            f"not found in registry. Available: {list(flow_registry.keys())}"
        )

    target_flow_def = flow_registry[target_flow_name]

    # Build sub-flow inputs from input_map via $ref resolution
    namespaces = {
        "input": inputs,
        "context": accumulator,
        "meta": {
            "flow_name": flow_def.flow,
            "step_id": step_name,
        },
    }

    sub_inputs: dict[str, Any] = {}
    if step_def.input_map:
        sub_inputs = resolve_input_map(step_def.input_map, namespaces)

    # Also pass through any params as additional inputs
    rendered_params = resolve_params(step_def.params, namespaces)
    for key, value in rendered_params.items():
        if key not in sub_inputs:
            sub_inputs[key] = value

    logger.info(
        "Sub-flow invocation: %s → %s (inputs: %s)",
        flow_def.flow,
        target_flow_name,
        list(sub_inputs.keys()),
    )

    # ── Trace: FlowInvoke ─────────────────────────────────────
    _can_trace = effects is not None and hasattr(effects, "emit_trace")
    if _can_trace:
        await effects.emit_trace(
            FlowInvoke(
                mission_id=_trace_mission_id,
                cycle=_trace_cycle,
                flow=flow_def.flow,
                step=step_name,
                child_flow=target_flow_name,
                child_inputs=list(sub_inputs.keys()),
            )
        )
    child_start = time.monotonic()

    # Execute the sub-flow recursively
    try:
        sub_result = await execute_flow(
            flow_def=target_flow_def,
            inputs=sub_inputs,
            action_registry=action_registry,
            effects=effects,
            flow_registry=flow_registry,
            max_steps=200,  # Sub-flows get a generous step budget
        )
    except Exception as e:
        logger.warning("Sub-flow %s failed: %s", target_flow_name, e)
        # Orphaned-session cleanup already ran inside execute_flow's
        # try/except wrapper (covering both MaxStepsExceeded and any
        # other uncaught exception) before the exception bubbled here.

        # ── Trace: FlowReturn (failed) ───────────────────────
        if _can_trace:
            await effects.emit_trace(
                FlowReturn(
                    mission_id=_trace_mission_id,
                    cycle=_trace_cycle,
                    flow=flow_def.flow,
                    child_flow=target_flow_name,
                    return_status="failed",
                    child_duration_ms=(time.monotonic() - child_start) * 1000,
                )
            )
        return StepOutput(
            result={"status": "failed", "error": str(e)},
            observations=f"Sub-flow {target_flow_name} failed: {e}",
        )

    # ── Trace: FlowReturn (success) ──────────────────────────
    if _can_trace:
        await effects.emit_trace(
            FlowReturn(
                mission_id=_trace_mission_id,
                cycle=_trace_cycle,
                flow=flow_def.flow,
                child_flow=target_flow_name,
                return_status=sub_result.status,
                child_duration_ms=(time.monotonic() - child_start) * 1000,
            )
        )

    # Extract published context keys from sub-flow result
    context_updates: dict[str, Any] = {}
    for key in step_def.publishes:
        if key in sub_result.context:
            context_updates[key] = sub_result.context[key]
        elif key in sub_result.result:
            context_updates[key] = sub_result.result[key]

    return StepOutput(
        result={"status": sub_result.status, **sub_result.result},
        observations=f"Sub-flow {target_flow_name}: {sub_result.status} "
        f"({len(sub_result.steps_executed)} steps)",
        context_updates=context_updates,
    )


async def _execute_inference_action(
    step_def: StepDefinition,
    step_input: StepInput,
    flow_def: FlowDefinition,
    inputs: dict[str, Any],
    effects: Any,
    _trace_mission_id: str = "",
    _trace_cycle: int = 0,
    _step_name: str = "",
) -> StepOutput:
    """Execute the special 'inference' action type.

    Renders the step's prompt template against context, calls
    effects.run_inference() with the step's config overrides,
    and wraps the response in a StepOutput.

    Args:
        step_def: The step definition (must have a prompt field).
        step_input: The built StepInput for this step.
        flow_def: The parent flow definition.
        inputs: The original flow inputs.
        effects: Effects interface (must have run_inference).

    Returns:
        StepOutput with the model's response as result.
    """
    if effects is None or not hasattr(effects, "run_inference"):
        raise FlowRuntimeError(
            f"Step with action 'inference' requires effects with run_inference. "
            f"Effects: {type(effects).__name__ if effects else 'None'}"
        )

    # Dispatch: turn-based steps use the new renderer + retry protocol;
    # legacy steps fall through to the prompt_template path below.
    if step_def.turn is not None:
        return await _execute_turn_inference(
            step_def=step_def,
            step_input=step_input,
            flow_def=flow_def,
            inputs=inputs,
            effects=effects,
            _trace_mission_id=_trace_mission_id,
            _trace_cycle=_trace_cycle,
            _step_name=_step_name,
        )

    if not step_def.prompt_template:
        raise FlowRuntimeError(
            "Step with action 'inference' requires a 'prompt_template' field "
            "(or a 'turn' field for new-format steps)."
        )

    # Build namespaces for resolution
    namespaces = {
        "input": inputs,
        "context": dict(step_input.context),
        "meta": {
            "flow_name": flow_def.flow,
            "step_id": step_input.meta.step_id,
        },
    }

    # Run pre_compute formatters — inject computed values into context. Timed
    # for the finite breakdown (carried on the InferenceCall below).
    pre_compute_ms = 0.0
    if step_def.pre_compute:
        _pc_start = time.monotonic()
        computed = run_pre_compute(step_def.pre_compute, namespaces)
        step_input.context.update(computed)
        namespaces["context"].update(computed)
        pre_compute_ms = (time.monotonic() - _pc_start) * 1000

    # Render the prompt. The split form also separates the leading cache:true
    # sections (the invariant static head) from the dynamic tail, so the LLMVP
    # backend can pin the head's KV per flow (opt-in via config.model.flow_kv_cache;
    # inert otherwise). static_prefix + dynamic == the full render exactly, so
    # output is unchanged whether or not caching is active.
    _render_start = time.monotonic()
    renderer = _get_prompt_renderer()
    flow_static_prefix, flow_dynamic = renderer.render_with_cache_split(
        step_def.prompt_template.template, namespaces
    )
    rendered_prompt = flow_static_prefix + flow_dynamic
    prompt_render_ms = (time.monotonic() - _render_start) * 1000

    # Build config overrides from merged step config
    config_overrides = {}
    if "temperature" in step_input.config:
        config_overrides["temperature"] = step_input.config["temperature"]
    if "max_tokens" in step_input.config:
        config_overrides["max_tokens"] = step_input.config["max_tokens"]

    # Call inference with tracing
    # Session-aware: if an inference session ID is in the step's context, route
    # through the memoryful session instead of making a stateless call.  This
    # avoids deadlocking when the session has already pinned the only pool instance.
    #
    # IMPORTANT: prefer inference_session_id over session_id.  Flows like
    # run_in_terminal publish BOTH a terminal session_id (for shell commands)
    # and an inference_session_id (for LLM calls).  Using the terminal ID
    # for inference causes "session not found" errors.  Flows that only have
    # one session (patch, mission_control) publish it as session_id
    # or edit_session_id, which still works as the fallback.
    session_id = (
        step_input.context.get("inference_session_id")
        or step_input.context.get("edit_session_id")
        or step_input.context.get("session_id")
    )
    tokens_in = count_tokens(rendered_prompt)
    # actually_session = did we ROUTE to a memoryful session (which self-emits
    # its own InferenceCall)? Distinct from "session_id is set": when session_id
    # is set but effects lacks session_inference we fall through to run_inference
    # and MUST trace here — gating on session_id alone left that inference
    # invisible (soundness fix).
    actually_session = bool(session_id) and hasattr(effects, "session_inference")
    infer_start = time.monotonic()

    if actually_session:
        logger.info(
            "Inference step %r using session %s",
            _step_name,
            session_id,
        )
        result = await effects.session_inference(
            session_id=session_id,
            prompt=rendered_prompt,
            config_overrides=config_overrides if config_overrides else None,
        )
    else:
        if session_id:
            logger.warning(
                "Inference step %r has session_id=%r but effects lacks session_inference",
                _step_name,
                session_id,
            )
        # Cache only when there's BOTH a static head AND a dynamic tail. When a
        # template's dynamic section is conditionally absent (e.g. feedback on
        # the first cycle) the whole prompt is static → flow_dynamic == "" → send
        # the full prompt normally (an empty prompt would be rejected). The
        # cycles that DO carry feedback then build/hit the same static head.
        run_kwargs: dict[str, Any] = {}
        prompt_to_send = rendered_prompt
        if flow_static_prefix and flow_dynamic:
            import hashlib

            # Key on flow:step + a hash of the static head, so different tasks
            # (different task_spec in the head) get distinct cache entries and a
            # hit always means the pinned prefix matches.
            digest = hashlib.md5(flow_static_prefix.encode("utf-8")).hexdigest()[:10]
            run_kwargs["static_prefix"] = flow_static_prefix
            run_kwargs["flow_key"] = f"{flow_def.flow}:{_step_name}:{digest}"
            prompt_to_send = flow_dynamic
        result = await effects.run_inference(
            prompt=prompt_to_send,
            config_overrides=config_overrides if config_overrides else None,
            **run_kwargs,
        )

    tokens_out = count_tokens(result.text) if result.text else 0

    # Fetch chain-of-thought content if tracing is enabled
    thinking_content = ""
    if hasattr(effects, "trace_thinking") and effects.trace_thinking:
        if hasattr(effects, "fetch_thinking"):
            try:
                thinking_content = await effects.fetch_thinking()
            except Exception:
                pass  # Non-critical — don't let thinking fetch break inference

    # Capture full prompt/response when --trace-prompts is set
    prompt_content = ""
    response_content = ""
    if hasattr(effects, "trace_prompts") and effects.trace_prompts:
        prompt_content = rendered_prompt
        response_content = result.text or ""

    # Trace this inference — but ONLY when we did NOT route to a session. The
    # session path (effects.session_inference) emits its own complete
    # InferenceCall, so emitting here too would double-log it (e.g. ops
    # judge_step, which reuses run_session's session). We gate on
    # actually_session (not session_id) so a session_id-set-but-no-session
    # fallthrough still gets traced. Mirrors the turn-based path below.
    _can_trace = hasattr(effects, "emit_trace") and not actually_session
    if _can_trace:
        await effects.emit_trace(
            InferenceCall(
                mission_id=_trace_mission_id,
                cycle=_trace_cycle,
                flow=flow_def.flow,
                step=_step_name,
                tokens_in=_real_in(result, tokens_in),
                tokens_out=_real_out(result, tokens_out),
                wall_ms=(time.monotonic() - infer_start) * 1000,
                temperature=_safe_float_temp(config_overrides.get("temperature", 0)),
                max_tokens=int(config_overrides.get("max_tokens", 0) or 0),
                purpose="step_inference",
                thinking_content=thinking_content,
                prompt_content=prompt_content,
                response_content=response_content,
                truncated=getattr(result, "truncated", False),
                prompt_render_ms=prompt_render_ms,
                pre_compute_ms=pre_compute_ms,
                **_cache_fields(result),
            )
        )

    if result.error:
        context_updates: dict[str, Any] = {
            "inference_response": result.text,
            "inference_error": result.error,
        }
        # Also publish under declared key names so downstream steps
        # can reference context by the semantic name in the YAML.
        for key in step_def.publishes:
            context_updates[key] = result.text
        return StepOutput(
            result={
                "text": "",
                "error": result.error,
                "tokens_generated": 0,
            },
            observations=f"Inference error: {result.error}",
            context_updates=context_updates,
        )

    context_updates = {
        "inference_response": result.text,
    }
    # Map inference response to each declared 'publishes' key so that
    # downstream steps can require the semantic name (e.g. 'connection_analysis')
    # instead of the generic 'inference_response'.
    for key in step_def.publishes:
        context_updates[key] = result.text
    return StepOutput(
        result={
            "text": result.text,
            "tokens_generated": result.tokens_generated,
            "finished": result.finished,
        },
        observations=f"Inference completed: {result.tokens_generated} tokens generated",
        context_updates=context_updates,
    )


# ══════════════════════════════════════════════════════════════════════
# Turn-based inference execution — Step C, Phase 4
#
# The new path. Invoked when a step declares `turn:` instead of
# `prompt_template:`. Renders via TurnRenderer, runs inference with
# turn.retries+1 attempts on empty response, and records a
# `turn_outcome` flag in the step output so the transition loop can
# route via turn.transitions (no_answer vs. default) rather than
# step.resolver.
# ══════════════════════════════════════════════════════════════════════


async def _execute_turn_inference(
    step_def: StepDefinition,
    step_input: StepInput,
    flow_def: FlowDefinition,
    inputs: dict[str, Any],
    effects: Any,
    _trace_mission_id: str = "",
    _trace_cycle: int = 0,
    _step_name: str = "",
) -> StepOutput:
    """Execute an inference step whose rendering is governed by a Turn.

    Differs from the legacy path in three ways:

    1. Prompt comes from TurnRenderer — banners, sections, envelope.
    2. Config overrides come from `turn.config` rather than the merged
       step_input.config (turn.config is authoritative for turn-based
       steps; flow.defaults still apply as a floor).
    3. Empty responses trigger retries up to `turn.retries` additional
       attempts. If all attempts come back empty, the step publishes
       `turn_outcome="no_answer"` and the transition loop routes to
       `turn.transitions.no_answer`. Non-empty responses publish
       `turn_outcome="default"` and route to `turn.transitions.default`.
    """
    assert step_def.turn is not None  # dispatched only when turn present
    turn = step_def.turn

    # Build namespaces (same shape as legacy path).
    namespaces = {
        "input": inputs,
        "context": dict(step_input.context),
        "meta": {
            "flow_name": flow_def.flow,
            "step_id": step_input.meta.step_id,
        },
    }

    # Pre-compute formatters — unchanged from legacy path. Timed for the
    # finite breakdown (one-time cost, attributed to the first attempt below).
    pre_compute_ms = 0.0
    if step_def.pre_compute:
        _pc_start = time.monotonic()
        computed = run_pre_compute(step_def.pre_compute, namespaces)
        step_input.context.update(computed)
        namespaces["context"].update(computed)
        pre_compute_ms = (time.monotonic() - _pc_start) * 1000

    # Render the prompt via TurnRenderer.
    _render_start = time.monotonic()
    turn_renderer = _get_turn_renderer()
    rendered_prompt = turn_renderer.render(turn, namespaces)
    prompt_render_ms = (time.monotonic() - _render_start) * 1000

    # Config overrides come from turn.config, with flow.defaults as
    # a floor for anything turn.config doesn't override.
    merged_config: dict[str, Any] = {
        **flow_def.defaults.config,
        **turn.config,
    }
    config_overrides: dict[str, Any] = {}
    if "temperature" in merged_config:
        config_overrides["temperature"] = merged_config["temperature"]
    if "max_tokens" in merged_config:
        config_overrides["max_tokens"] = merged_config["max_tokens"]

    # Session routing — unchanged from legacy path. Session inference
    # when a session id is in context, stateless otherwise.
    session_id = (
        step_input.context.get("inference_session_id")
        or step_input.context.get("edit_session_id")
        or step_input.context.get("session_id")
    )

    # Session injections — seed prompts and error-correction notices
    # queued by upstream actions (e.g. start_diagnosis_session,
    # run_session's start_session) expect to be prepended to the next
    # session_inference prompt. Consume them here, once, before the
    # retry loop — retries all share the same combined prompt, and the
    # queue drains via context_updates so a single seed isn't replayed
    # across later steps.
    injection_clears: dict[str, Any] = {}
    injection_ms = 0.0
    if session_id:
        from agent.session_injections import consume as consume_injections

        _inj_start = time.monotonic()
        rendered_prompt, injection_clears = consume_injections(
            step_input.context, rendered_prompt
        )
        injection_ms = (time.monotonic() - _inj_start) * 1000

    # Did we route to a memoryful session (which self-emits its InferenceCall)?
    # Gate tracing on this, not on session_id, so a session_id-set-but-no-
    # session fallthrough is still traced (soundness fix; mirrors legacy path).
    actually_session = bool(session_id) and hasattr(effects, "session_inference")

    # Retry loop: turn.retries additional attempts on empty response.
    # retries=0 means one attempt total (no retries). retries=3 (default)
    # means up to 4 attempts.
    tokens_in = count_tokens(rendered_prompt)
    max_attempts = turn.retries + 1
    result: Any = None
    attempts_made = 0
    last_error: str | None = None

    for attempt in range(max_attempts):
        attempts_made = attempt + 1
        infer_start = time.monotonic()

        if actually_session:
            if attempt == 0:
                logger.info(
                    "Turn inference step %r using session %s",
                    _step_name,
                    session_id,
                )
            result = await effects.session_inference(
                session_id=session_id,
                prompt=rendered_prompt,
                config_overrides=config_overrides if config_overrides else None,
            )
        else:
            if session_id and attempt == 0:
                logger.warning(
                    "Turn inference step %r has session_id=%r but effects lacks session_inference",
                    _step_name,
                    session_id,
                )
            result = await effects.run_inference(
                prompt=rendered_prompt,
                config_overrides=config_overrides if config_overrides else None,
            )

        tokens_out = count_tokens(result.text) if result.text else 0

        # Fetch chain-of-thought when tracing is enabled.
        thinking_content = ""
        if hasattr(effects, "trace_thinking") and effects.trace_thinking:
            if hasattr(effects, "fetch_thinking"):
                try:
                    thinking_content = await effects.fetch_thinking()
                except Exception:
                    pass

        # Capture full prompt/response when --trace-prompts is set.
        prompt_content = ""
        response_content = ""
        if hasattr(effects, "trace_prompts") and effects.trace_prompts:
            prompt_content = rendered_prompt
            response_content = result.text or ""

        # Trace this attempt — but only for the stateless `run_inference`
        # path. The session path (`effects.session_inference`) emits its
        # own InferenceCall trace event with correct timing and context,
        # so duplicating it here would produce two rows per inference in
        # the trace (inflating cost reports). See LocalEffects.session_inference.
        if hasattr(effects, "emit_trace") and not actually_session:
            # One-time setup costs (render/pre_compute/injection) happen before
            # the retry loop — attribute them to the first attempt only so
            # retries don't multi-count them.
            _setup = attempt == 0
            await effects.emit_trace(
                InferenceCall(
                    mission_id=_trace_mission_id,
                    cycle=_trace_cycle,
                    flow=flow_def.flow,
                    step=_step_name,
                    tokens_in=_real_in(result, tokens_in),
                    tokens_out=_real_out(result, tokens_out),
                    wall_ms=(time.monotonic() - infer_start) * 1000,
                    temperature=_safe_float_temp(
                        config_overrides.get("temperature", 0)
                    ),
                    max_tokens=int(config_overrides.get("max_tokens", 0) or 0),
                    purpose="step_inference",
                    thinking_content=thinking_content,
                    prompt_content=prompt_content,
                    response_content=response_content,
                    truncated=getattr(result, "truncated", False),
                    prompt_render_ms=prompt_render_ms if _setup else 0.0,
                    pre_compute_ms=pre_compute_ms if _setup else 0.0,
                    injection_ms=injection_ms if _setup else 0.0,
                    **_cache_fields(result),
                )
            )

        if result.error:
            last_error = result.error
            # Inference error — break out and return error outcome.
            # Don't burn retries on infrastructure failures.
            break

        # Empty response → retry if budget remains.
        response_text = result.text or ""
        if response_text.strip():
            # For menu shapes, also require the response to parse into
            # a valid option choice. An unparseable menu response gets
            # retried — same as if it were empty — because a menu that
            # can't yield a choice is indistinguishable from no answer.
            if turn.response_shape in ("menu_single", "menu_compound"):
                menu_choice = _extract_menu_choice(turn, namespaces, response_text)
                if menu_choice is not None:
                    last_error = None
                    break
                logger.info(
                    "Turn inference step %r: menu response unparseable on "
                    "attempt %d/%d — %s",
                    _step_name,
                    attempt + 1,
                    max_attempts,
                    "retrying" if attempt + 1 < max_attempts else "exhausted retries",
                )
                continue
            # Non-menu shape with non-empty text — accept and exit loop.
            last_error = None
            break
        logger.info(
            "Turn inference step %r: empty response on attempt %d/%d — %s",
            _step_name,
            attempt + 1,
            max_attempts,
            "retrying" if attempt + 1 < max_attempts else "exhausted retries",
        )

    # Classify outcome and build publish map.
    response_text = result.text if result and result.text else ""
    non_empty = bool(response_text.strip())

    # Menu shapes: attempt one last parse to know whether we have a
    # valid choice or not. If we broke out of the loop on a successful
    # parse, this re-runs cheaply; if we exhausted retries without a
    # parse, this returns None and we classify as no_answer.
    menu_choice: str | None = None
    if turn.response_shape in ("menu_single", "menu_compound") and non_empty:
        menu_choice = _extract_menu_choice(turn, namespaces, response_text)

    if result and result.error:
        turn_outcome = "no_answer"
        observations = (
            f"Turn inference error after {attempts_made} attempt(s): {last_error}"
        )
    elif turn.response_shape in ("menu_single", "menu_compound"):
        if menu_choice is not None:
            turn_outcome = f"option:{menu_choice}"
            observations = (
                f"Turn inference selected option {menu_choice!r} "
                f"in {attempts_made} attempt(s)"
            )
        else:
            turn_outcome = "no_answer"
            observations = (
                f"Turn inference exhausted {attempts_made} attempt(s) "
                f"without a parseable menu choice"
            )
    elif non_empty:
        turn_outcome = "default"
        observations = (
            f"Turn inference completed in {attempts_made} attempt(s): "
            f"{result.tokens_generated} tokens"
        )
    else:
        turn_outcome = "no_answer"
        observations = (
            f"Turn inference exhausted {attempts_made} attempt(s) with empty response"
        )

    context_updates: dict[str, Any] = {
        "inference_response": response_text,
        # Generation health signals for downstream actions. A multi-file
        # batch generation that hit the token ceiling (finish_reason ==
        # length) is sliced for whatever completed; the slicer reports
        # truncation so missing files route to per-file creation instead
        # of being misread as the model omitting them.
        "inference_truncated": bool(getattr(result, "truncated", False)),
        "inference_tokens_generated": result.tokens_generated if result else 0,
    }
    # Drain the session_injections queue once it's been consumed —
    # otherwise the seed prompt would replay on every subsequent
    # session_inference in the same flow.
    if injection_clears:
        context_updates.update(injection_clears)
    for key in step_def.publishes:
        context_updates[key] = response_text

    # For menu shapes with publish_selection declared, also publish the
    # selected option key under that context name. The full response
    # text stays in inference_response and `publishes` as usual, but
    # downstream steps reading e.g. `selected_fix_target` get just the
    # option key — no JSON unwrapping.
    if menu_choice is not None:
        publish_selection = getattr(turn.response, "publish_selection", None)
        if publish_selection:
            context_updates[publish_selection] = menu_choice

            # For menu_compound turns, also publish the option's argument
            # under `{publish_selection}_arg`. The chosen option's arg is
            # parsed from the response JSON — menu_compound schema is
            # `{"choice": "<key>", "<arg_name>": "<value>"}`. Without this
            # publish, downstream actions like execute_investigation_tool
            # (which reads `pick_action_choice_arg`) would have no way to
            # get the arg value short of reparsing inference_response
            # themselves. The a85f381e trace revealed the gap:
            # pick_action's `__run_command__` choice published the verb
            # but the command string was lost, so execute_investigation_tool
            # silently no-op'd.
            if turn.response_shape == "menu_compound":
                arg_value = _extract_menu_arg(turn, menu_choice, response_text)
                if arg_value is not None:
                    context_updates[f"{publish_selection}_arg"] = arg_value

        # ── Acceptance signal (e39 round) ──────────────────────
        #
        # Queue a session injection telling the model on its NEXT
        # turn that this menu choice was accepted. e39 evidence:
        # the model's CoT in cycle 22 invented a "previous response
        # malformed" narrative across three consecutive trace turns
        # even though every response was being accepted and executed.
        # Without explicit acceptance feedback the model has no
        # signal distinguishing "system accepted my reply, now
        # asking again with new context" from "system rejected my
        # reply, asking me to retry." It defaulted to the latter
        # interpretation, eventually gave up and forced conclude
        # with hallucinated content.
        #
        # The injection lands in the next session_inference call
        # via session_injections.consume(), which prepends queued
        # messages before the rendered prompt. Quiet, low-cost
        # signal — costs maybe 20 tokens per turn, prevents the
        # rejection-spiral failure mode entirely.
        #
        # Only sessions with an inference_session_id receive the
        # injection (stateless inference doesn't need it because
        # there's no KV cache to confuse the model). Skip when
        # consumer flow is single-turn — there's no "next turn"
        # to inject into.
        from agent.session_injections import queue as queue_injection

        session_id = (
            step_input.context.get("inference_session_id")
            or step_input.context.get("diagnosis_session_id")
            or step_input.context.get("edit_session_id")
            or step_input.context.get("session_id")
        )
        if session_id:
            arg_suffix = ""
            if turn.response_shape == "menu_compound":
                arg_value_for_msg = _extract_menu_arg(turn, menu_choice, response_text)
                if arg_value_for_msg:
                    arg_suffix = f" with argument {arg_value_for_msg!r}"
            queue_injection(
                context_updates,
                step_input.context,
                f"[Your previous selection of {menu_choice!r}"
                f"{arg_suffix} was accepted and executed.]",
            )

    return StepOutput(
        result={
            "text": response_text,
            "tokens_generated": result.tokens_generated if result else 0,
            "finished": result.finished if result else False,
            "turn_outcome": turn_outcome,
            "attempts": attempts_made,
            "error": last_error if last_error else None,
        },
        observations=observations,
        context_updates=context_updates,
    )


def _extract_menu_choice(
    turn: Any,  # TurnDefinition
    namespaces: dict[str, Any],
    response_text: str,
) -> str | None:
    """Parse a menu-shape response and return the chosen option key.

    Resolves the valid option keys from the turn's options source
    (projection / context / embedded + stock) via the same logic the
    renderer used — so the key list the parser matches against is
    identical to what the model saw.

    Then delegates to agent.resolvers.llm_menu.extract_choice which:
      - parses the response as JSON (tolerant of LLM quirks)
      - reads the "choice" field
      - normalizes case and separators to match an option key

    Returns the matched option key, or None if the response didn't
    yield a valid choice. The caller uses None to trigger retry or
    classify as no_answer after exhaustion.
    """
    renderer = _get_turn_renderer()
    try:
        options = renderer.resolve_options(turn, namespaces)
    except Exception as e:
        # Renderer couldn't resolve options — e.g., projection missing
        # or source misconfigured. Without valid keys we can't match a
        # choice, so return None and let the no_answer path route on.
        logger.warning("Menu choice extraction: option resolution failed: %s", e)
        return None

    valid_keys = [opt["key"] for opt in options]
    if not valid_keys:
        return None

    from agent.resolvers.llm_menu import extract_choice

    return extract_choice(response_text, valid_keys)


def _extract_menu_arg(
    turn: Any,  # TurnDefinition
    menu_choice: str,
    response_text: str,
) -> str | None:
    """Extract the argument for a menu_compound choice from the response.

    menu_compound schema is ``{"choice": "<key>", "<arg_name>": "<value>"}``.
    After the choice has been resolved to a known option key, look up
    that option's `arg.name` (the schema declares it), then pull that
    key out of the parsed JSON.

    Returns:
        The arg value as a string, or None if the arg can't be extracted
        (choice has no arg declared, option not in the turn's options map,
        JSON unparseable, or the arg key is missing from the response).
        None tells the caller "no arg to publish" — not an error.
    """
    # Options map is on turn.response.options; for stock-merged turns it
    # lives on the expanded options rendered at resolve time. We look at
    # the full options list (user-defined + stock) the same way the
    # renderer and choice extractor do.
    renderer = _get_turn_renderer()
    try:
        options = renderer.resolve_options(turn, {})
    except Exception:
        # Mirror _extract_menu_choice: if option resolution fails,
        # we can't know the arg name, so skip publishing.
        return None

    arg_name: str | None = None
    for opt in options:
        if opt.get("key") == menu_choice:
            arg_spec = opt.get("arg")
            if isinstance(arg_spec, dict):
                arg_name = arg_spec.get("name")
            break

    if not arg_name:
        # Chosen option doesn't declare an arg — nothing to publish.
        return None

    from agent.llm_json import parse_llm_json

    data = parse_llm_json(response_text)
    if not isinstance(data, dict):
        return None

    value = data.get(arg_name)
    if value is None:
        return None
    return str(value)


def _resolve_turn_transition(
    turn: Any,  # TurnDefinition — forward reference-safe
    step_output: StepOutput,
) -> str:
    """Pick the next step for a turn-based step based on outcome flags.

    Reads `step_output.result["turn_outcome"]`:
      - "no_answer" → `turn.transitions.no_answer`
      - "default"   → `turn.transitions.default`
      - anything else (e.g., a menu "option:<key>" outcome, reserved for
        future phases) → `turn.transitions.options[key]` with fallback
        to default

    The turn-outcome flag is set by _execute_turn_inference. For menu
    shapes (later phases), the outcome will also encode which option
    the model chose so transitions.options can be consulted.
    """
    outcome = step_output.result.get("turn_outcome", "default")

    if outcome == "no_answer":
        return turn.transitions.no_answer

    # Option-key outcomes reserved for menu-site phases. Format:
    # "option:<key>". The options map on TurnTransitions holds per-key
    # targets; keys without an entry fall back to default.
    if isinstance(outcome, str) and outcome.startswith("option:"):
        option_key = outcome.removeprefix("option:")
        options_map = turn.transitions.options or {}
        if option_key in options_map:
            return options_map[option_key]
        return turn.transitions.default

    # Default path for any non-"no_answer" outcome (including the
    # plain "default" sentinel and any future additions that route
    # through default fallback).
    return turn.transitions.default


def _validate_inputs(flow_def: FlowDefinition, inputs: dict[str, Any]) -> None:
    """Validate that all required flow inputs are provided.

    Raises:
        MissingInputError: If any required input is missing.
    """
    missing = [key for key in flow_def.input.required if key not in inputs]
    if missing:
        raise MissingInputError(
            f"Flow {flow_def.flow!r} requires inputs {missing} "
            f"but they were not provided. "
            f"Provided: {list(inputs.keys())}"
        )


def _build_step_input(
    step_def: StepDefinition,
    step_name: str,
    flow_def: FlowDefinition,
    accumulator: dict[str, Any],
    inputs: dict[str, Any],
    effects: Any = None,
) -> StepInput:
    """Build a StepInput for a step, with filtered context and rendered params.

    1. Filter the accumulator to only the keys the step declares (required + optional).
    2. Validate that all required context keys are present.
    3. Merge flow-level and step-level config.
    4. Resolve $ref values in params.

    Args:
        step_def: The step definition.
        step_name: The step's name (for error messages and metadata).
        flow_def: The parent flow definition (for defaults and input).
        accumulator: The current context accumulator.
        inputs: The original flow inputs.
        effects: Effects interface instance.

    Returns:
        A StepInput ready for the action callable.

    Raises:
        MissingContextError: If required context keys are missing.
    """
    # Filter context to declared keys, plus ambient keys (see
    # _AMBIENT_CONTEXT_KEYS module doc). Ambient keys flow through
    # every step without needing declaration — reserved for
    # infrastructure-level cross-step state like session_injections.
    # Required-key validation below is unchanged: ambient status
    # does not let a key satisfy a required declaration.
    declared_keys = set(step_def.context.required + step_def.context.optional)
    effective_keys = declared_keys | _AMBIENT_CONTEXT_KEYS
    filtered_context = {
        key: accumulator[key] for key in effective_keys if key in accumulator
    }

    # Validate required context keys
    missing = [key for key in step_def.context.required if key not in accumulator]
    if missing:
        raise MissingContextError(
            f"Step {step_name!r} requires context keys {missing} "
            f"but they are not in the accumulator. "
            f"Available keys: {list(accumulator.keys())}"
        )

    # Merge config: flow defaults + step overrides
    merged_config = {**flow_def.defaults.config, **step_def.config}

    # Build namespaces for $ref resolution
    namespaces = {
        "input": inputs,
        "context": filtered_context,
        "meta": {
            "flow_name": flow_def.flow,
            "step_id": step_name,
        },
    }

    # Resolve $ref values in params
    rendered_params = resolve_params(step_def.params, namespaces)

    return StepInput(
        task=step_def.description,
        context=filtered_context,
        config=merged_config,
        params=rendered_params,
        meta=FlowMeta(
            flow_name=flow_def.flow,
            step_id=step_name,
        ),
        effects=effects,
        turn=step_def.turn,
        # Pass-through flow inputs. Custom actions that render turn
        # prompts (rewrite_symbol_turn, capture_bail_turn, etc.) use
        # this to populate ``namespaces["input"]`` for template
        # substitution — without it, ``{input.change_spec}`` and
        # similar references resolved to empty strings even when
        # the flow invocation correctly passed the values through.
        inputs=dict(inputs),
    )
