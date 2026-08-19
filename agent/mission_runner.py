"""One mission-run harness for every entry point.

The CLI and all five adapter runners previously each hand-rolled the same
sequence — wrap run_agent with a drain finally, run it on an isolated event
loop, and classify the budget-park RuntimeError — in three drifting
isolation styles (raw Thread, asyncio.to_thread, bare asyncio.run on the
caller's thread). This module is the single copy, standardized on the
dedicated-thread form:

run_agent's MCP/PTY machinery opens anyio cancel scopes bound to the
creating task. Sharing a thread with the caller's async context lets an
ambient cancellation (a bench harness timeout, a CLI teardown) enter a
scope in one task and exit in another — RuntimeError("attempted to exit
cancel scope in a different task") / a CancelledError that errors the run
*after the mission already completed*. A dedicated thread has no ambient
scopes, so every caller gets the isolation the tb/tau adapters had to
learn one lost results.json at a time.

Sync callers call :func:`run_mission_isolated` directly; async callers wrap
it in ``await asyncio.to_thread(run_mission_isolated, ...)``.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from typing import Any, Optional

from agent.effects.teardown import drain_effects
from agent.paths import repo_root

PARK_MARKER = "parked as paused"


def build_and_save_mission(
    agent_dir: str,
    objective: str,
    *,
    working_directory: Optional[str] = None,
    flow_set: str = "code_core",
    status: str = "active",
    pending_directive: Optional[str] = None,
    **config_fields: Any,
):
    """Create + persist a mission — the boilerplate every adapter repeated.

    ``agent_dir`` is where .agent/ lives (the PersistenceManager root);
    ``working_directory`` is what the mission CONFIG records as the
    workspace (defaults to agent_dir — the container adapters split the
    two: host-side persistence, container-side working dir). Extra
    keyword fields flow into MissionConfig verbatim (task_profile,
    llmvp_endpoint, web_research, held_out_tests, ...). Entry-flow
    selection stays caller-side — it is genuinely per-adapter routing.
    Returns the saved MissionState.
    """
    from agent.persistence.manager import PersistenceManager
    from agent.persistence.models import MissionConfig, MissionState

    pm = PersistenceManager(agent_dir)
    pm.init_agent_dir()
    mission = MissionState(
        objective=objective,
        status=status,
        config=MissionConfig(
            working_directory=working_directory or agent_dir,
            flow_set=flow_set,
            **config_fields,
        ),
    )
    if pending_directive is not None:
        mission.pending_directive = pending_directive
    pm.save_mission(mission)
    return mission


@dataclass
class MissionRunOutcome:
    """How the mission loop ended.

    Exactly one of the three shapes holds:
    - ``result`` set: the loop returned normally (AgentResult).
    - ``parked``: budget stop — run_agent raised its RuntimeError after
      parking the mission. A clean stop, not a crash; graders/callers
      evaluate the final state regardless.
    - ``error`` set: a real failure. KeyboardInterrupt is never stored —
      it re-raises so interactive callers keep their Ctrl-C handling.
    """

    result: Any = None
    parked: bool = False
    error: Optional[BaseException] = None
    park_message: str = ""


def run_mission_isolated(
    effects: Any,
    *,
    mission_id: str,
    entry_flow: str,
    max_cycles: Optional[int] = None,
    max_wall_clock_s: Optional[float] = None,
    flows_dir: Optional[str] = None,
    prompts_dir: Optional[str] = None,
    entry_inputs: Optional[dict] = None,
) -> MissionRunOutcome:
    """Run one mission to termination on a dedicated thread + event loop.

    Blocks until the mission ends. Sessions/MCP are drained inside the loop
    (best-effort) whatever the exit path.
    """
    from agent.loop import run_agent

    root = repo_root()
    flows = flows_dir or os.path.join(root, "flows")
    prompts = prompts_dir or os.path.join(root, "prompts")

    async def _run_with_drain():
        from agent.scheduler.lifecycle import worker_pool_for

        try:
            # Continuous lane workers, for flow sets that use them. A
            # non-pooled mission (every v1 mission, including the live
            # corpus run) gets None and behaves exactly as before. The
            # pool is created HERE so it lives on this thread's own event
            # loop — the reason the mission runs on its own thread at all
            # is that anyio cancel scopes are task-bound.
            async with worker_pool_for(
                effects,
                flows_dir=flows,
                max_wall_clock_s=max_wall_clock_s,
            ):
                return await run_agent(
                    mission_id=mission_id,
                    effects=effects,
                    flows_dir=flows,
                    prompts_dir=prompts,
                    entry_flow=entry_flow,
                    entry_inputs=entry_inputs,
                    max_cycles=max_cycles,
                    max_wall_clock_s=max_wall_clock_s,
                )
        finally:
            # Outside the pool's scope on purpose: the pool releases its
            # claims and stops its lanes first, then sessions and MCP are
            # drained.
            await drain_effects(effects)

    box: dict[str, Any] = {}

    def _mission_thread() -> None:
        try:
            box["result"] = asyncio.run(_run_with_drain())
        except BaseException as e:  # noqa: BLE001 — classified below
            box["exc"] = e

    t = threading.Thread(target=_mission_thread, name="ouroboros-mission", daemon=True)
    t.start()
    t.join()

    exc = box.get("exc")
    if exc is None:
        return MissionRunOutcome(result=box.get("result"))
    if isinstance(exc, KeyboardInterrupt):
        raise KeyboardInterrupt
    if isinstance(exc, RuntimeError) and PARK_MARKER in str(exc):
        return MissionRunOutcome(parked=True, park_message=str(exc))
    return MissionRunOutcome(error=exc)
