"""Starting and stopping the worker pool around a mission run.

WHERE THIS LIVES AND WHY. The pool is scoped to a mission run, so it is
started and stopped around `run_agent` in mission_runner's `_run_with_drain`
— the one scope every entry point funnels through and whose `finally`
reliably runs. It must be created INSIDE the mission thread's own event
loop: that thread exists because anyio cancel scopes are task-bound, and
a pool created on the caller's loop could not be cancelled from within
the mission's.

WHY THIS IS A SEPARATE MODULE. mission_runner should not learn what a
lane is, and the pool should not learn how missions are launched. This
is the seam between them, and it is also the single place that decides
whether a given mission gets continuous workers at all.

OPT-IN BY FLOW SET. Only `scraper_v2` runs the pool. A v1 mission — the
one carrying the live corpus — gets exactly what it got before: no pool,
no capacity feed, no behavioural change of any kind. That is what makes
this safe to land while a mission is running.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)

# Flow sets whose drains are owned by the worker pool rather than by
# parallel branches inside the controller's window.
POOLED_FLOW_SETS = frozenset({"scraper_v2"})


async def _mission_flow_set(effects) -> str:
    try:
        mission = await effects.load_mission()
        return str(getattr(getattr(mission, "config", None), "flow_set", "") or "")
    except Exception:  # noqa: BLE001 — an unreadable mission simply gets no pool
        return ""


@contextlib.asynccontextmanager
async def worker_pool_for(
    effects,
    *,
    flows_dir: str,
    max_wall_clock_s: Optional[float] = None,
    working_directory: Optional[str] = None,
) -> AsyncIterator[Any]:
    """Run continuous lane workers for the duration of a pooled mission.

    Yields the pool, or None when this mission is not pooled. Every
    failure path yields None: a scheduler that cannot start must not stop
    a mission from running, because the controller alone is still a
    working pipeline — just a slower one.
    """
    flow_set = await _mission_flow_set(effects)
    if flow_set not in POOLED_FLOW_SETS:
        yield None
        return

    pool = None
    started_feed = False
    try:
        from agent.actions.registry import build_action_registry
        from agent.loop import _load_flows
        from agent.scheduler.capacity_model import CapacityModel
        from agent.scheduler.worker_pool import WorkerPool, lanes_for_scraper

        # The capacity feed is what makes admission real rather than a
        # guess. Without it the model degrades to width 1 — slower, never
        # broken — so a feed that will not start is not fatal.
        feed = None
        if hasattr(effects, "capacity_start"):
            effects.capacity_start()
            started_feed = True
            feed = getattr(effects, "_capacity", None)

        pool = WorkerPool(
            effects=effects,
            lanes=lanes_for_scraper(),
            capacity_model=CapacityModel(feed),
            flow_registry=_load_flows(flows_dir),
            action_registry=build_action_registry(),
            # Read off the effects rather than accepted from the caller.
            # Making the caller extract it is one more thing to get wrong,
            # and the first version of this wiring did get it wrong — it
            # passed a nonexistent attribute and silently handed the lanes
            # an empty path.
            inputs={
                "working_directory": (
                    working_directory
                    if working_directory is not None
                    else str(getattr(effects, "working_directory", "") or "")
                )
            },
            # The pool honours the run's wall clock too. The loop's budget
            # park only fires at a flow boundary, so without this a lane
            # would keep working past a budget the controller has stopped
            # respecting.
            deadline=(
                time.monotonic() + max_wall_clock_s if max_wall_clock_s else None
            ),
        )
        pool.start()
        logger.info(
            "worker pool started for %s: lanes=%s",
            flow_set,
            [ln.name for ln in pool.lanes],
        )
        yield pool
    except Exception:  # noqa: BLE001 — never block the mission on the pool
        logger.exception("worker pool failed to start — running controller-only")
        yield None
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.stop()
            logger.info(
                "worker pool stopped: %s",
                {n: s.units_done for n, s in pool.state.items()},
            )
        if started_feed and hasattr(effects, "capacity_stop"):
            with contextlib.suppress(Exception):
                await effects.capacity_stop()
