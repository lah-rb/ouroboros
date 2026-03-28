"""Flow executor — the core runtime that executes flow definitions.

Validates inputs → initializes accumulator → loops through steps
(build StepInput → execute action → merge context_updates → resolve
transition → repeat) until a terminal step is reached → returns FlowResult.

Phase 3 additions:
- Resolver dispatch is now async (supports LLM menu resolver).
- Special action type 'inference': renders prompt template, calls
  effects.run_inference(), wraps response in StepOutput.
- Effects interface passed through to resolvers for LLM-driven transitions.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent.actions.registry import ActionNotFoundError, ActionRegistry
from agent.trace import (
    StepStart,
    StepEnd,
    InferenceCall,
    FlowInvoke,
    FlowReturn,
    count_tokens,
)
from agent.models import (
    FlowDefinition,
    FlowExecution,
    FlowMeta,
    FlowResult,
    StepDefinition,
    StepInput,
    StepOutput,
)
from agent.resolvers import resolve, ResolverError
from agent.tail_call import FlowOutcome, FlowTailCall, FlowTermination
from agent.template import render_template, render_params

logger = logging.getLogger(__name__)


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


class FlowRuntimeError(Exception):
    """Raised when the flow runtime encounters an unrecoverable error."""

    pass


class MaxStepsExceeded(FlowRuntimeError):
    """Raised when a flow exceeds the maximum step count (infinite loop guard)."""

    pass


class MissingContextError(FlowRuntimeError):
    """Raised when a step's required context keys are not present."""

    pass


class MissingInputError(FlowRuntimeError):
    """Raised when a flow's required inputs are not provided."""

    pass


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

    def _action_type_for(action: str) -> str:
        if action == "inference":
            return "inference"
        elif action == "flow":
            return "flow"
        elif action == "noop":
            return "noop"
        return "action"

    # Main execution loop
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

        # Build StepInput with filtered context and effects
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
                    action_type=_action_type_for(step_def.action),
                    action=step_def.action,
                    context_consumed=list(step_input.context.keys()),
                    context_required=list(step_def.context.required),
                )
            )

        # Execute the action — special handling for 'inference' and 'flow'
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
            execution.observations.append(f"[{step_name}] {step_output.observations}")

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
                        step_duration_ms=((time.monotonic() - step_start_time) * 1000),
                    )
                )
            logger.info(
                "Flow %r reached terminal step %r with status %r",
                flow_def.flow,
                step_name,
                step_def.status,
            )
            return FlowResult(
                status=step_def.status or "completed",
                result=step_output.result,
                context=execution.accumulator,
                steps_executed=execution.steps_executed,
                observations=execution.observations,
            )

        # Check for tail-call step (non-terminal step with tail_call block)
        if step_def.tail_call:
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
                        resolver_decision=step_def.tail_call.get("flow", "unknown"),
                        step_duration_ms=((time.monotonic() - step_start_time) * 1000),
                    )
                )
            logger.info(
                "Flow %r: step %r triggers tail call to %r",
                flow_def.flow,
                step_name,
                step_def.tail_call.get("flow"),
            )
            return FlowResult(
                status=step_def.status or "tail_call",
                result=step_output.result,
                context=execution.accumulator,
                steps_executed=execution.steps_executed,
                observations=execution.observations,
                tail_call=step_def.tail_call,
            )

        # Resolve the next transition (now async to support LLM menu)
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
                )
            )

        logger.debug(
            "Step %r → transition to %r",
            step_name,
            next_step,
        )
        execution.current_step = next_step

    # If we get here, we exceeded max steps.
    # Before raising, clean up any active sessions that would have been
    # released by the flow's terminal step (which never ran).
    if effects is not None:
        for key in ("inference_session_id", "session_id", "edit_session_id"):
            sid = execution.accumulator.get(key, "")
            if not sid:
                continue
            try:
                if key == "session_id" and hasattr(effects, "close_terminal"):
                    await effects.close_terminal(sid)
                    logger.info(
                        "Cleaned up orphaned terminal %s before MaxStepsExceeded", sid
                    )
                if key in ("inference_session_id", "edit_session_id") and hasattr(
                    effects, "end_inference_session"
                ):
                    await effects.end_inference_session(sid)
                    logger.info(
                        "Cleaned up orphaned inference session %s before MaxStepsExceeded",
                        sid,
                    )
            except Exception:
                pass  # Best-effort cleanup

    raise MaxStepsExceeded(
        f"Flow {flow_def.flow!r} exceeded maximum step count ({max_steps}). "
        f"Steps executed: {execution.steps_executed}. "
        f"Last step: {execution.current_step!r}. "
        f"This likely indicates an infinite loop."
    )


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

    # Build sub-flow inputs from input_map templates
    template_vars = {
        "input": inputs,
        "context": accumulator,
        "meta": {
            "flow_name": flow_def.flow,
            "step_id": step_name,
        },
    }

    sub_inputs: dict[str, Any] = {}
    if step_def.input_map:
        for key, value_template in step_def.input_map.items():
            if isinstance(value_template, str) and "{{" in value_template:
                try:
                    resolved = render_template(value_template, template_vars)
                    sub_inputs[key] = resolved
                except Exception:
                    sub_inputs[key] = value_template
            else:
                sub_inputs[key] = value_template

    # Also pass through any params as additional inputs
    rendered_params = render_params(step_def.params, template_vars)
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
        # Note: if MaxStepsExceeded, session cleanup already happened
        # inside execute_flow before the exception was raised.

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

    if not step_def.prompt:
        raise FlowRuntimeError(
            f"Step with action 'inference' requires a 'prompt' field."
        )

    # Build template variables
    template_vars = {
        "input": inputs,
        "context": step_input.context,
        "meta": {
            "flow_name": flow_def.flow,
            "step_id": step_input.meta.step_id,
        },
    }

    # Render the prompt template
    rendered_prompt = render_template(step_def.prompt, template_vars)

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
    # one session (ast_edit_session, mission_control) publish it as session_id
    # or edit_session_id, which still works as the fallback.
    session_id = (
        step_input.context.get("inference_session_id")
        or step_input.context.get("edit_session_id")
        or step_input.context.get("session_id")
    )
    tokens_in = count_tokens(rendered_prompt)
    infer_start = time.monotonic()

    if session_id and hasattr(effects, "session_inference"):
        logger.debug(
            "Inference step %r using memoryful session %s",
            _step_name,
            session_id,
        )
        result = await effects.session_inference(
            session_id=session_id,
            prompt=rendered_prompt,
            config_overrides=config_overrides if config_overrides else None,
        )
    else:
        result = await effects.run_inference(
            prompt=rendered_prompt,
            config_overrides=config_overrides if config_overrides else None,
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

    _can_trace = hasattr(effects, "emit_trace")
    if _can_trace:
        await effects.emit_trace(
            InferenceCall(
                mission_id=_trace_mission_id,
                cycle=_trace_cycle,
                flow=flow_def.flow,
                step=_step_name,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                wall_ms=(time.monotonic() - infer_start) * 1000,
                temperature=_safe_float_temp(config_overrides.get("temperature", 0)),
                max_tokens=int(config_overrides.get("max_tokens", 0) or 0),
                purpose="session_inference" if session_id else "step_inference",
                thinking_content=thinking_content,
                prompt_content=prompt_content,
                response_content=response_content,
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
    4. Render params with Jinja2 templates.

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
    # Filter context to declared keys only
    declared_keys = set(step_def.context.required + step_def.context.optional)
    filtered_context = {
        key: accumulator[key] for key in declared_keys if key in accumulator
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

    # Build template variables for param rendering
    template_vars = {
        "input": inputs,
        "context": filtered_context,
        "meta": {
            "flow_name": flow_def.flow,
            "step_id": step_name,
        },
    }

    # Render params through Jinja2 templates
    rendered_params = render_params(step_def.params, template_vars)

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
    )
