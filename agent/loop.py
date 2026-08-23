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
from agent.loader import (
    resolve_value,
    resolve_input_map,
    assemble_returns,
)
from agent.errors import FlowRuntimeError
from agent.models import FlowDefinition, FlowResult
from agent.runtime import execute_flow, init_prompt_renderer
from agent.turn_renderer import EmptyMenuError
from agent.tail_call import FlowOutcome, FlowTailCall, FlowTermination
from agent.trace import CycleStart, CycleEnd, trace_enabled

logger = logging.getLogger(__name__)


async def _materialize_projections(
    flow_def: FlowDefinition,
    inputs: dict[str, Any],
    effects: Any,
) -> dict[str, Any]:
    """Materialize projections declared by the flow.

    For each projection in flow_def.projections, resolves params from
    the current inputs, calls the registered materializer against
    MissionState, and injects the result as an additional input.

    This is the single point where MissionState is loaded for read
    purposes. Actions that need to mutate state still receive mission
    through the context accumulator (Pattern A).

    Args:
        flow_def: The flow about to execute.
        inputs: Current flow inputs (for $ref resolution).
        effects: Effects interface (for load_mission).

    Returns:
        Augmented inputs dict with projection results injected.
    """
    if not flow_def.projections:
        return inputs

    from agent.projections import materialize

    mission = await effects.load_mission()
    if not mission:
        logger.warning(
            "Flow %r declares projections but no mission found — skipping",
            flow_def.flow,
        )
        return inputs

    augmented = dict(inputs)
    for proj_name, proj_def in flow_def.projections.items():
        materializer_name = proj_def.get("materializer", "")
        raw_params = proj_def.get("params", {})
        required = proj_def.get("required", True)

        # Resolve $ref params against current inputs
        resolved_params = resolve_input_map(
            raw_params,
            {
                "input": inputs,
                "context": {},
                "meta": {},
            },
        )

        try:
            projected = materialize(materializer_name, mission, resolved_params)
            augmented[proj_name] = projected
            logger.debug(
                "Materialized projection %r for flow %r",
                proj_name,
                flow_def.flow,
            )
        except Exception as e:
            if required:
                raise RuntimeError(
                    f"Required projection {proj_name!r} failed for "
                    f"flow {flow_def.flow!r}: {e}"
                ) from e
            logger.warning(
                "Optional projection %s failed for flow %s: %s",
                proj_name,
                flow_def.flow,
                e,
            )

    return augmented


def _load_flows(flows_dir: str) -> dict[str, FlowDefinition]:
    """Load all flows from CUE-exported compiled.json.

    Args:
        flows_dir: Directory containing flows/compiled.json.

    Returns:
        Dict of flow_name → FlowDefinition.
    """
    flows_dir = Path(flows_dir)
    flows: dict[str, FlowDefinition] = {}

    compiled = flows_dir / "compiled.json"
    if not compiled.exists():
        logger.error(
            "compiled.json not found in %s — run 'ouroboros.py cue-compile' first",
            flows_dir,
        )
        return flows

    with open(compiled) as f:
        data = json.load(f)

    if not isinstance(data, dict):
        logger.error("compiled.json is not a dict of flow_name → flow_definition")
        return flows

    for name, flow_data in data.items():
        if isinstance(flow_data, dict) and "flow" in flow_data:
            try:
                flow = FlowDefinition(**flow_data)
                flows[flow.flow] = flow
            except Exception as e:
                logger.error("Failed to load flow %r: %s", name, e)

    logger.info("Loaded %d flows from %s", len(flows), compiled)
    return flows


async def _route_director_return(
    outcome: "FlowTailCall", effects: Any
) -> "FlowTailCall":
    """Route a director-targeted tail call to the mission's own controller.

    The shared return templates hardcode ``flow: "mission_control"``, so
    sub-flows shared across flow sets always "return" there by name. A
    flow set with its own controller (contract_swarm) must regain
    control instead: when the target is ANY registered director flow,
    re-resolve it to the entry flow of the mission's CURRENT flow set
    (read live — classify rewrites flow_set mid-run for the auto set).
    Identity for same-controller returns and on any read failure.
    """
    from agent.flow_sets import FLOW_SETS, get_flow_set

    director_flows = {spec.entry_flow for spec in FLOW_SETS.values()}
    if outcome.target_flow not in director_flows:
        return outcome
    try:
        mission = await effects.load_mission()
        entry = get_flow_set(getattr(mission.config, "flow_set", "")).entry_flow
    except Exception:  # noqa: BLE001 — routing must never kill the loop
        return outcome
    if entry != outcome.target_flow and entry in director_flows:
        logger.info(
            "Director return %r routed to %r (mission flow set)",
            outcome.target_flow,
            entry,
        )
        outcome.target_flow = entry
    return outcome


def _resolve_tail_call(
    flow_result: FlowResult,
    flow_def: FlowDefinition,
    inputs: dict[str, Any],
) -> FlowOutcome:
    """Convert a FlowResult into a FlowOutcome.

    If the FlowResult has a tail_call block, resolve $ref values in the
    flow name and input_map, assemble structured returns as last_result,
    and return a FlowTailCall. Otherwise, wrap in FlowTermination.

    Args:
        flow_result: The result from execute_flow.
        flow_def: The flow definition (for returns declaration).
        inputs: The original flow inputs (for returns resolution).
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

        # Assemble structured returns as last_result
        if flow_def.returns and "last_result" not in resolved_inputs:
            structured_result = assemble_returns(
                flow_def,
                flow_result.context,
                inputs,
            )
            if structured_result:
                resolved_inputs["last_result"] = structured_result

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
    max_cycles: int | None = 50,
    max_wall_clock_s: float | None = None,
) -> FlowResult:
    """Run the agent loop — follow tail calls until termination.

    Args:
        mission_id: The mission to work on.
        effects: Effects interface instance.
        flows_dir: Directory containing flow definitions.
        prompts_dir: Directory containing prompt templates.
        entry_flow: The flow to start with (default: mission_control).
        entry_inputs: Override initial inputs (default: {mission_id}).
        max_cycles: Maximum number of work flow executions; None runs
            until the mission's flow terminates (run_until="completed").
        max_wall_clock_s: Park the mission as paused once elapsed wall
            time exceeds this, checked at work-flow boundaries — a
            running flow is never interrupted mid-dispatch.

    Returns:
        The final FlowResult when the agent terminates.
    """
    registry = _load_flows(flows_dir)
    actions = build_action_registry()
    init_prompt_renderer(prompts_dir)

    current_flow = entry_flow
    current_inputs = entry_inputs or {"mission_id": mission_id}
    cycle = 0
    started_at = time.monotonic()

    logger.info("Agent starting: flow=%r, mission=%s", entry_flow, mission_id)

    # Track consecutive entry_flow runs to catch self-loops. Unbounded
    # runs (max_cycles=None) still need this livelock guard; the counter
    # resets whenever a work flow executes, so 50 back-to-back entry
    # runs without a dispatch is pathological at any budget.
    consecutive_entry = 0
    max_consecutive_entry = (max_cycles + 3) if max_cycles is not None else 50

    while True:
        # Clean pause drain: a mission paused out-of-band (`mission pause`
        # while we run) exits here at the next cycle boundary — the
        # in-flight dispatch finishes, then we stop — instead of the
        # controller idling into the livelock guard below (the old drain:
        # 51 no-work spins ending in a scary RuntimeError, ~5 min).
        try:
            _m = await effects.load_mission()
        except Exception:  # noqa: BLE001 — status probe must never kill the loop
            _m = None
        if _m is not None and getattr(_m, "status", "") == "paused":
            logger.info(
                "Mission %s is paused — draining cleanly after %d cycle(s).",
                mission_id,
                cycle,
            )
            # League accounting: `tier pause` rides this path — book the
            # cycles this process ran so a contemplator resume subtracts
            # them from its 30-cycle budget.
            try:
                _m.cycles_consumed = int(getattr(_m, "cycles_consumed", 0) or 0) + cycle
                await effects.save_mission(_m)
            except Exception:  # noqa: BLE001 — accounting must not kill the drain
                logger.exception("Failed to book cycles on paused drain")
            # Return a real FlowResult: callers (cmd_start, mission_runner)
            # read .status/.steps_executed off the return value — a bare
            # break fell off the function returning None and crashed the
            # CLI's termination summary ('NoneType' has no attribute
            # 'status', observed live 2026-07-21).
            return FlowResult(
                status="paused_drain",
                observations=[
                    f"Mission paused out-of-band — drained cleanly after "
                    f"{cycle} work cycle(s)."
                ],
            )

        # Safety: catch entry flow self-loops
        if current_flow == entry_flow:
            consecutive_entry += 1
            if consecutive_entry > max_consecutive_entry:
                raise RuntimeError(
                    f"Entry flow {entry_flow!r} ran {consecutive_entry} times "
                    f"without dispatching work. Possible infinite loop."
                )
        else:
            consecutive_entry = 0

        if current_flow not in registry:
            raise RuntimeError(
                f"Flow {current_flow!r} not found in registry. "
                f"Available: {list(registry.keys())}"
            )

        flow_def = registry[current_flow]

        # ── Trace: CycleStart ────────────────────────────────────
        cycle_start_time = time.monotonic()
        if trace_enabled(effects):
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

        # Finite-time breakdown: projection + tail-resolution are cycle-level
        # work outside any step — time them so cycle_duration_ms decomposes
        # exhaustively (carried on CycleEnd below).
        projection_ms = 0.0
        tail_resolution_ms = 0.0
        try:
            # Materialize projections before flow execution
            _proj_start = time.monotonic()
            instrumented_inputs = await _materialize_projections(
                flow_def,
                instrumented_inputs,
                effects,
            )
            projection_ms = (time.monotonic() - _proj_start) * 1000

            flow_result = await execute_flow(
                flow_def=flow_def,
                inputs=instrumented_inputs,
                action_registry=actions,
                effects=effects,
                flow_registry=registry,
            )
            _tail_start = time.monotonic()
            outcome = _resolve_tail_call(flow_result, flow_def, instrumented_inputs)
            tail_resolution_ms = (time.monotonic() - _tail_start) * 1000
        except FlowRuntimeError as exc:
            # A flow could not execute correctly — its infinite-loop safety
            # tripped (MaxStepsExceeded) or it routed to a step whose required
            # context wasn't satisfied (MissingContextError/MissingInputError).
            # Don't crash the whole mission: treat it as a failed cycle and
            # return to the entry flow so it can re-plan or park. The cycle
            # budget still bounds total work, so a persistently-broken flow
            # degrades to a paused mission rather than a hard error that loses
            # all progress. (Overnight suites died to single flow crashes —
            # a looping diagnose, then a data-file self-correct — before this.)
            # Genuine code bugs (bare exceptions) still propagate below.
            if effects and hasattr(effects, "flush_traces"):
                await effects.flush_traces()
            logger.warning(
                "Flow %r raised a flow-runtime error — failing this cycle and "
                "returning to %r so the mission continues. (%s)",
                current_flow,
                entry_flow,
                exc,
            )
            failed_inputs: dict[str, Any] = {
                "mission_id": mission_id,
                "last_status": "failed",
            }
            if "goal_id" in current_inputs:
                failed_inputs["goal_id"] = current_inputs["goal_id"]
            outcome = FlowTailCall(target_flow=entry_flow, inputs=failed_inputs)
        except EmptyMenuError as exc:
            # A menu turn's options resolved to nothing AT RUNTIME (the options_from
            # projection yielded an empty set) — data-dependent, not a static config
            # bug. Crashing loses all run progress (a high-tier mission busted at 0
            # files this way). Pause the mission cleanly so it stays resumable —
            # the same clean-pause contract as budget exhaustion below.
            #
            # FUTURE (once the in-process context refresh proves out): before pausing,
            # attempt backend.refresh_context() + one retry — an empty menu can stem
            # from degraded ("soured") model output — and pause only if it still fails.
            # FUTURE: per-goal circuit breaker — pause just this goal rather than the
            # whole run, so sibling goals keep progressing (bigger change, deferred).
            if effects and hasattr(effects, "flush_traces"):
                await effects.flush_traces()
            try:
                _m = await effects.load_mission()
                if _m is not None and getattr(_m, "status", "") == "active":
                    _m.status = "paused"
                    _m.cycles_consumed = (
                        int(getattr(_m, "cycles_consumed", 0) or 0) + cycle
                    )
                    await effects.save_mission(_m)
            except Exception:
                logger.exception(
                    "Failed to park mission %s as paused on empty-menu render",
                    mission_id,
                )
            logger.warning(
                "Empty-menu render in flow %r — parked mission %s as paused "
                "(resumable) instead of crashing the run. (%s)",
                current_flow,
                mission_id,
                exc,
            )
            raise RuntimeError(
                f"Menu turn could not be rendered (no options) in flow "
                f"{current_flow!r}. Mission parked as paused — resume with "
                f"`mission resume` or `start`."
            )
        except Exception:
            if effects and hasattr(effects, "flush_traces"):
                await effects.flush_traces()
            raise

        # ── Director-return routing ──────────────────────────────
        # The shared return templates name mission_control LITERALLY, so
        # every code_core sub-flow "returns" there by name. Route a
        # director-targeted tail call to the MISSION'S OWN controller
        # (the entry flow of its CURRENT flow set) so alternative
        # controllers — contract_swarm's mission_control_swarm — keep
        # control after their sub-flows return. The auto→code_core
        # handoff is unaffected: once classify rewrites flow_set, the
        # current set's entry IS mission_control. (Found by the swarm
        # toy smoke: one sub-flow return silently handed the mission
        # back to plain mission_control, which dispatched
        # build_structure — the wrong structural path.)
        if isinstance(outcome, FlowTailCall):
            outcome = await _route_director_return(outcome, effects)

        # ── Context Tier Enforcement (belt-and-suspenders) ───────
        # CUE validates at compile time; this catches dynamic violations.
        if isinstance(outcome, FlowTailCall) and outcome.target_flow in registry:
            target_def = registry[outcome.target_flow]
            target_tier = getattr(target_def, "context_tier", "")
            if (
                target_tier == "flow_directive"
                and "flow_directive" not in outcome.inputs
            ):
                logger.warning(
                    "Tier violation: flow %r requires flow_directive but none "
                    "provided by tail-call from %r",
                    outcome.target_flow,
                    current_flow,
                )
            if target_tier == "session_task" and "mission_objective" in outcome.inputs:
                logger.warning(
                    "Tier noise: flow %r operates at session_task tier but "
                    "received mission_objective from %r — this context will be ignored",
                    outcome.target_flow,
                    current_flow,
                )

        # ── Trace: CycleEnd + flush ──────────────────────────────
        if trace_enabled(effects):
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
                    projection_ms=projection_ms,
                    tail_resolution_ms=tail_resolution_ms,
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

        # ── Cycle budget check ───────────────────────────────────
        # Count work flow executions (everything except the entry flow).
        # The entry flow runs for free — it's bookkeeping (recording
        # results, picking next task). The budget limits how many work
        # flows actually execute.
        #
        # Check AFTER a work flow completes, before following the
        # tail-call. The entry flow will have already run and recorded
        # the result by the time we reach the next work flow dispatch.
        if current_flow != entry_flow:
            cycle += 1
        out_of_cycles = max_cycles is not None and cycle >= max_cycles
        elapsed_s = time.monotonic() - started_at
        out_of_time = max_wall_clock_s is not None and elapsed_s >= max_wall_clock_s
        if (out_of_cycles or out_of_time) and outcome.target_flow == entry_flow:
            # Budget exhausted. Park the mission as paused so
            # `mission resume` / `start` can pick it up later, rather
            # than leaving it 'active' after the error exit. General by design:
            # a budget-exhausted mission being resumable is strictly better than
            # erroring + left active — applies to any long sweep, and is what
            # lets the quality-fix loop continue across `--max-cycles` windows.
            #
            # PARK AT THE NEXT RESUMABLE POINT (epoch v2.0, 2026-08-02): the
            # guard used to be `!= entry_flow`, which meant "let one more
            # entry-flow pass run" — and that pass DECIDES: it re-certifies,
            # commits a dispatch_config, and only then parks, systematically
            # stopping at the worst possible instant (devstral shipped a
            # broken engine with its repair dispatched and undone; the v2
            # smoke's courtesy-fix rule exists because of it). Parking at the
            # work→entry boundary instead stops BEFORE anything is decided:
            # the finished work flow's tail-call inputs are persisted as
            # `pending_return`, and resume replays them into the entry flow so
            # the report books exactly as if the process had continued.
            # Wall clock still only fires at a work-flow boundary — an
            # in-flight dispatch always finishes (overshoot ≤ one work flow);
            # a work→work chain defers the park to its first entry return.
            if out_of_cycles:
                budget_msg = f"Cycle limit: {max_cycles}."
            else:
                budget_msg = (
                    f"Wall-clock limit: {max_wall_clock_s:.0f}s "
                    f"(elapsed {elapsed_s:.0f}s)."
                )
            try:
                _m = await effects.load_mission()
                if _m is not None and getattr(_m, "status", "") == "active":
                    # The park lands at the work→entry boundary, BEFORE the
                    # entry flow books the finished flow's report. pending_return
                    # persists the tail-call inputs so resume can replay them
                    # and the report books exactly as if the process continued.
                    # cycles_consumed: lifetime work-cycle accounting for the
                    # league protocol (in-process counter resets per run_agent).
                    #
                    # Op-based park (mission-ops pilot): status/pending_return
                    # are idempotent sets, cycles_consumed is a real counter
                    # increment — the op shapes exactly. Do NOT pre-mutate the
                    # in-memory object alongside mission_apply: increments are
                    # not idempotent and would double-apply through effects
                    # whose store shares the object (MockEffects).
                    try:
                        _pending = dict(outcome.inputs or {})
                    except Exception:  # noqa: BLE001 — additive, best-effort
                        _pending = {}
                    _mission_apply = getattr(effects, "mission_apply", None)
                    _parked = None
                    if _mission_apply is not None:
                        from agent.persistence.models import (
                            CounterIncOp,
                            FieldSetOp,
                            MissionStatusOp,
                        )

                        _parked = await _mission_apply(
                            [
                                MissionStatusOp(status="paused"),
                                FieldSetOp(key="pending_return", value=_pending),
                                CounterIncOp(field="cycles_consumed", n=cycle),
                            ]
                        )
                    if _parked is None:
                        # Effects double without mission_apply — legacy path.
                        _m.status = "paused"
                        _m.pending_return = _pending
                        _m.cycles_consumed = (
                            int(getattr(_m, "cycles_consumed", 0) or 0) + cycle
                        )
                        await effects.save_mission(_m)
                    logger.info(
                        "Budget exhausted (%d cycles, %.0fs) — parked mission "
                        "%s as paused",
                        cycle,
                        elapsed_s,
                        mission_id,
                    )
                    # §19: a parked run still stages an artifact that gets
                    # judged. If the seam gate never ran once, that artifact
                    # ships with ZERO cross-module checks — say so in the log
                    # (both sweeps' zero-run arms were only discovered by a
                    # cross-arm grep after judging).
                    try:
                        from agent.actions.mission_actions import (
                            _SEAM_GATE_MEMO,
                        )

                        _runs = _SEAM_GATE_MEMO.get(str(getattr(_m, "id", "")), {}).get(
                            "runs", 0
                        )
                        _py = {
                            f
                            for g in (getattr(_m, "goals", []) or [])
                            if getattr(g, "type", "") == "structural"
                            for f in (g.associated_files or [])
                            if str(f).endswith(".py")
                        }
                        if _runs == 0 and len(_py) >= 2:
                            logger.warning(
                                "Seam gate NEVER RAN this mission (%d "
                                "structural .py files) — the parked artifact "
                                "ships with zero cross-module checks "
                                "(OPEN_TASKS §19)",
                                len(_py),
                            )
                    except Exception:  # noqa: BLE001 — advisory only
                        pass
            except Exception:
                logger.exception(
                    "Failed to park mission as paused on budget exhaustion"
                )
            raise RuntimeError(
                f"Agent completed {cycle} work cycle(s). {budget_msg} "
                f"Mission parked as paused — resume with `mission resume` or `start`."
            )

        if outcome.delay_seconds and outcome.delay_seconds > 0:
            await asyncio.sleep(outcome.delay_seconds)

        current_flow = outcome.target_flow
        current_inputs = outcome.inputs
