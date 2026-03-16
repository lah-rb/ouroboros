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
from typing import Any

from agent.actions.registry import ActionNotFoundError, ActionRegistry
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

    logger.info(
        "Starting flow %r at entry step %r",
        flow_def.flow,
        flow_def.entry,
    )

    # Main execution loop
    while execution.step_count < execution.max_steps:
        step_name = execution.current_step
        step_def = flow_def.steps[step_name]

        logger.debug(
            "Flow %r: executing step %r (step #%d)",
            flow_def.flow,
            step_name,
            execution.step_count + 1,
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

        # Execute the action — special handling for 'inference' and 'flow'
        if step_def.action == "inference":
            step_output = await _execute_inference_action(
                step_def=step_def,
                step_input=step_input,
                flow_def=flow_def,
                inputs=inputs,
                effects=effects,
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
                    "attempt": 1,
                    "step_count": execution.step_count,
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

        logger.debug(
            "Step %r → transition to %r",
            step_name,
            next_step,
        )
        execution.current_step = next_step

    # If we get here, we exceeded max steps
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

    # Execute the sub-flow recursively
    try:
        sub_result = await execute_flow(
            flow_def=target_flow_def,
            inputs=sub_inputs,
            action_registry=action_registry,
            effects=effects,
            flow_registry=flow_registry,
            max_steps=50,  # Sub-flows get a reduced step budget
        )
    except Exception as e:
        logger.warning("Sub-flow %s failed: %s", target_flow_name, e)
        return StepOutput(
            result={"status": "failed", "error": str(e)},
            observations=f"Sub-flow {target_flow_name} failed: {e}",
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

    # Call inference
    result = await effects.run_inference(
        prompt=rendered_prompt,
        config_overrides=config_overrides if config_overrides else None,
    )

    if result.error:
        return StepOutput(
            result={
                "text": "",
                "error": result.error,
                "tokens_generated": 0,
            },
            observations=f"Inference error: {result.error}",
            context_updates={
                "inference_response": result.text,
                "inference_error": result.error,
            },
        )

    return StepOutput(
        result={
            "text": result.text,
            "tokens_generated": result.tokens_generated,
            "finished": result.finished,
        },
        observations=f"Inference completed: {result.tokens_generated} tokens generated",
        context_updates={
            "inference_response": result.text,
        },
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
