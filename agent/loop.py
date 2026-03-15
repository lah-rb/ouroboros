"""Thin outer process loop — bootstrap mission_control, follow tail calls.

This is the only "loop" in Ouroboros. It loads flows, builds the action
registry, and follows tail calls until a FlowTermination is reached.

The cycling behavior emerges from the flow graph: mission_control dispatches
a task flow, the task flow completes and tail-calls back to mission_control,
which dispatches the next task.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent.actions.registry import build_action_registry
from agent.loader import load_all_flows
from agent.models import FlowResult
from agent.runtime import execute_flow
from agent.tail_call import FlowOutcome, FlowTailCall, FlowTermination
from agent.template import render_template, render_params

logger = logging.getLogger(__name__)


def _resolve_tail_call(flow_result: FlowResult) -> FlowOutcome:
    """Convert a FlowResult into a FlowOutcome.

    If the FlowResult has a tail_call block, resolve it into a FlowTailCall.
    Otherwise, wrap it in a FlowTermination.
    """
    if flow_result.tail_call:
        tc = flow_result.tail_call
        target_flow = tc.get("flow", "")

        # Resolve all template values — flow name, input_map, delay
        template_vars = {
            "input": flow_result.context,
            "context": flow_result.context,
            "result": flow_result.result,
            "meta": flow_result.context.get("meta", {}),
        }

        # Resolve flow name (may be a template like {{ context.dispatch_config.flow }})
        if isinstance(target_flow, str) and "{{" in target_flow:
            try:
                target_flow = render_template(target_flow, template_vars)
            except Exception:
                pass  # Keep raw value, will fail with clear error downstream

        # Resolve input_map — values may be templates or direct values
        input_map = tc.get("input_map", {})
        resolved_inputs = {}

        for key, value in input_map.items():
            if isinstance(value, str) and "{{" in value:
                try:
                    resolved_inputs[key] = render_template(value, template_vars)
                except Exception:
                    resolved_inputs[key] = value
            else:
                resolved_inputs[key] = value

        delay = tc.get("delay")
        if isinstance(delay, str) and "{{" in delay:
            try:
                delay = float(render_template(delay, template_vars))
            except Exception:
                delay = None

        return FlowTailCall(
            target_flow=target_flow,
            inputs=resolved_inputs,
            delay_seconds=float(delay) if delay else None,
            source_flow=(
                flow_result.steps_executed[0] if flow_result.steps_executed else ""
            ),
            source_status=flow_result.status,
        )

    return FlowTermination(result=flow_result)


async def run_agent(
    mission_id: str,
    effects: Any,
    flows_dir: str = "flows",
    entry_flow: str = "mission_control",
    entry_inputs: dict[str, Any] | None = None,
    max_cycles: int = 50,
) -> FlowResult:
    """Run the agent loop — follow tail calls until termination.

    Args:
        mission_id: The mission to work on.
        effects: Effects interface instance.
        flows_dir: Directory containing flow YAML files.
        entry_flow: The flow to start with (default: mission_control).
        entry_inputs: Override initial inputs (default: {mission_id}).
        max_cycles: Maximum number of flow executions (safety limit).

    Returns:
        The final FlowResult when the agent terminates.
    """
    registry = load_all_flows(flows_dir)
    actions = build_action_registry()

    current_flow = entry_flow
    current_inputs = entry_inputs or {"mission_id": mission_id}
    cycle = 0

    logger.info("Agent starting: flow=%r, mission=%s", entry_flow, mission_id)

    while cycle < max_cycles:
        cycle += 1

        if current_flow not in registry:
            raise RuntimeError(
                f"Flow {current_flow!r} not found in registry. "
                f"Available: {list(registry.keys())}"
            )

        flow_def = registry[current_flow]
        logger.info(
            "Agent cycle %d: executing flow %r with inputs %s",
            cycle,
            current_flow,
            list(current_inputs.keys()),
        )

        flow_result = await execute_flow(
            flow_def=flow_def,
            inputs=current_inputs,
            action_registry=actions,
            effects=effects,
        )

        outcome = _resolve_tail_call(flow_result)

        if isinstance(outcome, FlowTermination):
            logger.info(
                "Agent terminated after %d cycles: status=%r",
                cycle,
                outcome.result.status,
            )
            return outcome.result

        # FlowTailCall — continue to next flow
        assert isinstance(outcome, FlowTailCall)
        logger.info(
            "Tail call: %r → %r (delay=%s)",
            current_flow,
            outcome.target_flow,
            outcome.delay_seconds,
        )

        if outcome.delay_seconds and outcome.delay_seconds > 0:
            logger.info(
                "Waiting %.1f seconds before next cycle...", outcome.delay_seconds
            )
            await asyncio.sleep(outcome.delay_seconds)

        current_flow = outcome.target_flow
        current_inputs = outcome.inputs

    raise RuntimeError(
        f"Agent exceeded maximum cycle count ({max_cycles}). "
        f"Last flow: {current_flow!r}. This may indicate a stuck loop."
    )
