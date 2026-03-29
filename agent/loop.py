"""Thin outer process loop — bootstrap mission_control, follow tail calls.

This is the only "loop" in Ouroboros. It loads flows from CUE-exported JSON,
builds the action registry, and follows tail calls until a FlowTermination
is reached.

The cycling behavior emerges from the flow graph: mission_control dispatches
a task flow, the task flow completes and tail-calls back to mission_control,
which dispatches the next task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from agent.actions.registry import build_action_registry
from agent.loader_v2 import (
    load_flow_json,
    resolve_value,
    resolve_input_map,
    format_result,
    FlowLoadError,
)
from agent.models import FlowDefinition, FlowResult
from agent.runtime import execute_flow, init_prompt_renderer
from agent.tail_call import FlowOutcome, FlowTailCall, FlowTermination
from agent.trace import CycleStart, CycleEnd

logger = logging.getLogger(__name__)


def _load_flows(flows_dir: str) -> dict[str, FlowDefinition]:
    """Load all flows from CUE-exported JSON.

    Expects either:
      - flows_dir/compiled.json  (single file with all flows)
      - flows_dir/cue/*.json     (individual flow files)

    Args:
        flows_dir: Directory containing flow definitions.

    Returns:
        Dict of flow_name → FlowDefinition.
    """
    flows_dir = Path(flows_dir)
    flows: dict[str, FlowDefinition] = {}

    # Option 1: Single compiled.json (output of `cue export --out json`)
    compiled = flows_dir / "compiled.json"
    if compiled.exists():
        with open(compiled) as f:
            data = json.load(f)

        # compiled.json contains a dict of flow_name → flow_definition
        if isinstance(data, dict):
            for name, flow_data in data.items():
                if isinstance(flow_data, dict) and "flow" in flow_data:
                    try:
                        flow = FlowDefinition(**flow_data)
                        flows[flow.flow] = flow
                    except Exception as e:
                        logger.error("Failed to load flow %r: %s", name, e)
        logger.info("Loaded %d flows from %s", len(flows), compiled)
        return flows

    # Option 2: Individual JSON files
    cue_dir = flows_dir / "cue"
    json_dir = cue_dir if cue_dir.exists() else flows_dir

    for json_path in sorted(json_dir.glob("*.json")):
        try:
            flow = load_flow_json(json_path)
            flows[flow.flow] = flow
        except FlowLoadError as e:
            logger.error("Failed to load %s: %s", json_path, e)

    logger.info("Loaded %d flows from %s", len(flows), json_dir)
    return flows


def _resolve_tail_call(flow_result: FlowResult) -> FlowOutcome:
    """Convert a FlowResult into a FlowOutcome.

    If the FlowResult has a tail_call block, resolve $ref values in the
    flow name and input_map, apply result_formatter, and return a
    FlowTailCall. Otherwise, wrap in FlowTermination.
    """
    if flow_result.tail_call:
        tc = flow_result.tail_call

        # Build namespaces for $ref resolution
        namespaces = {
            "input": flow_result.context,
            "context": flow_result.context,
            "result": flow_result.result,
            "meta": flow_result.context.get("meta", {}),
        }

        # Resolve flow name ($ref or literal)
        target_flow = resolve_value(tc.get("flow", ""), namespaces)

        # Resolve input_map
        input_map = tc.get("input_map", {})
        resolved_inputs = resolve_input_map(input_map, namespaces)

        # Apply result_formatter if present
        result_msg = format_result(tc, namespaces)
        if result_msg and "last_result" not in resolved_inputs:
            resolved_inputs["last_result"] = result_msg

        # Resolve delay
        delay = tc.get("delay")
        if isinstance(delay, dict) and "$ref" in delay:
            delay = resolve_value(delay, namespaces)

        return FlowTailCall(
            target_flow=str(target_flow),
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
    prompts_dir: str = "prompts",
    entry_flow: str = "mission_control",
    entry_inputs: dict[str, Any] | None = None,
    max_cycles: int = 50,
) -> FlowResult:
    """Run the agent loop — follow tail calls until termination.

    Args:
        mission_id: The mission to work on.
        effects: Effects interface instance.
        flows_dir: Directory containing flow definitions.
        prompts_dir: Directory containing prompt templates.
        entry_flow: The flow to start with (default: mission_control).
        entry_inputs: Override initial inputs (default: {mission_id}).
        max_cycles: Maximum number of flow executions (safety limit).

    Returns:
        The final FlowResult when the agent terminates.
    """
    registry = _load_flows(flows_dir)
    actions = build_action_registry()
    init_prompt_renderer(prompts_dir)

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

        # ── Trace: CycleStart ────────────────────────────────────
        cycle_start_time = time.monotonic()
        if effects and hasattr(effects, "emit_trace"):
            await effects.emit_trace(
                CycleStart(
                    mission_id=mission_id,
                    cycle=cycle,
                    flow=current_flow,
                    entry_inputs=list(current_inputs.keys()),
                )
            )

        logger.info(
            "Agent cycle %d: executing flow %r with inputs %s",
            cycle,
            current_flow,
            list(current_inputs.keys()),
        )

        instrumented_inputs = {
            **current_inputs,
            "_trace_cycle": cycle,
        }

        try:
            flow_result = await execute_flow(
                flow_def=flow_def,
                inputs=instrumented_inputs,
                action_registry=actions,
                effects=effects,
                flow_registry=registry,
            )
        except Exception:
            if effects and hasattr(effects, "flush_traces"):
                await effects.flush_traces()
            raise

        outcome = _resolve_tail_call(flow_result)

        # ── Trace: CycleEnd + flush ──────────────────────────────
        if effects and hasattr(effects, "emit_trace"):
            is_tail_call = isinstance(outcome, FlowTailCall)
            await effects.emit_trace(
                CycleEnd(
                    mission_id=mission_id,
                    cycle=cycle,
                    flow=current_flow,
                    outcome="tail_call" if is_tail_call else "termination",
                    target_flow=(outcome.target_flow if is_tail_call else None),
                    status=(None if is_tail_call else outcome.result.status),
                    cycle_duration_ms=((time.monotonic() - cycle_start_time) * 1000),
                )
            )
            await effects.flush_traces()

        if isinstance(outcome, FlowTermination):
            logger.info(
                "Agent terminated after %d cycles: status=%r",
                cycle,
                outcome.result.status,
            )
            return outcome.result

        assert isinstance(outcome, FlowTailCall)
        logger.info(
            "Tail call: %r → %r (delay=%s)",
            current_flow,
            outcome.target_flow,
            outcome.delay_seconds,
        )

        if outcome.delay_seconds and outcome.delay_seconds > 0:
            await asyncio.sleep(outcome.delay_seconds)

        current_flow = outcome.target_flow
        current_inputs = outcome.inputs

    raise RuntimeError(
        f"Agent exceeded maximum cycle count ({max_cycles}). "
        f"Last flow: {current_flow!r}."
    )
