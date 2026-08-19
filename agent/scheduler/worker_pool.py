"""Continuous lane workers — the replacement for the parallel barrier.

WHAT WAS WRONG. Drains rode a `parallel` step: N branch flows gathered
under one `asyncio.gather`, so a window lasted as long as its SLOWEST
branch and every lane did exactly ONE unit of work inside it. A 26-minute
paddle batch of German dissertations meant 26 minutes in which muse ran
one curate paper and then idled. Every fix attempted from inside that
shape — bigger budgets, partial progress, smaller batches — was really an
attempt to guess how long the window would be.

WHAT THIS DOES INSTEAD. Each lane is a long-lived task that loops:

    claim a unit -> wait until it FITS -> run it -> book it -> release

There is no window and no barrier. A lane that finishes early starts its
next unit immediately; a lane with nothing to do sleeps without holding
anyone. Budgets stop being window guesses and become what they always
should have been: how much work one unit is.

PER-LANE, NOT WORK-STEALING. The lanes already have disjoint claim sets,
disjoint resources, and different starvation policies (needs-reextract
first for OCR, smallest-first for curate, relevance-first for translate).
Work-stealing solves imbalance between INTERCHANGEABLE workers; these are
not interchangeable, and a global queue would re-derive per-item what the
four existing selectors already know.

BACKPRESSURE IS THE ADMISSION GATE, and it comes from the server: a lane
waits on `CapacityModel.admit`, which reads a snapshot LLMVP pushed. When
nothing fits, the lane waits for the next capacity change — which is a
stream retiring, i.e. exactly the event that could make it fit — rather
than polling a timer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Lane:
    """One resource-bound stream of work."""

    name: str
    flow: str  # the drain flow this lane runs
    resource: str  # "text_seat" | "vision_ctx" | "paddle" | "network"
    # Estimated KV a unit draws. Lanes whose work never touches the text
    # seats (paddle OCR, vision figure reads) declare 0 and are gated on
    # their own resource instead.
    est_kv: int = 0
    # TEXT SEATS this unit occupies. 0 for lanes served by something other
    # than the batched text pool — paddle OCR runs on its own device and
    # figure reads run on muse's separate vision contexts. Declaring 1 for
    # those refuses them whenever muse's text seats fill, which starves
    # GPU1 work on a GPU0 constraint; caught on the first mileage run,
    # where "ocr: no free seat" was refusing paddle work while paddle sat
    # idle. Those lanes are bounded by max_inflight for their own
    # resource instead.
    seats: int = 1
    # How long to wait before re-checking an empty queue. Idle lanes must
    # not spin: there is no work-arrival signal from the databank.
    idle_backoff_s: float = 20.0


# Measured N* per resource comes from the throughput sweep; until it runs
# these are the conservative shapes already proven in production.
DEFAULT_LANE_MAX_INFLIGHT: Dict[str, int] = {
    "text_seat": 2,
    "vision_ctx": 1,
    "paddle": 1,
    "network": 1,
}


@dataclass
class LaneState:
    last_dispatch_at: float = 0.0
    units_done: int = 0  # rounds that actually moved work
    units_idle: int = 0  # rounds that found nothing to do
    units_failed: int = 0
    inflight: int = 0


class WorkerPool:
    """Runs lanes until told to stop.

    Lifecycle is an async context manager so that a `finally` somewhere
    above cannot forget to stop it — the drain path for the mission thread
    is the only thing that reliably runs at shutdown.
    """

    def __init__(
        self,
        effects,
        lanes: List[Lane],
        *,
        capacity_model=None,
        flow_registry: Optional[dict] = None,
        action_registry: Any = None,
        inputs: Optional[dict] = None,
        max_inflight: Optional[Dict[str, int]] = None,
        deadline: Optional[float] = None,
    ) -> None:
        self.effects = effects
        self.lanes = list(lanes)
        self.model = capacity_model
        self.flow_registry = flow_registry or {}
        self.action_registry = action_registry
        self.inputs = dict(inputs or {})
        self.max_inflight = dict(max_inflight or DEFAULT_LANE_MAX_INFLIGHT)
        self.deadline = deadline

        self.state: Dict[str, LaneState] = {ln.name: LaneState() for ln in lanes}
        self._tasks: List[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._resource_inflight: Dict[str, int] = {}

        # Timing knobs as instance attributes (TESTING.md) so tests drive
        # the real loop rather than a rewritten one.
        self._admit_wait_s = 5.0
        self._stop_grace_s = 30.0
        self._report_every_s = 300.0
        self._last_refusal = ""
        self._now = time.monotonic

    # -- lifecycle -------------------------------------------------------

    async def __aenter__(self) -> "WorkerPool":
        self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    def start(self) -> None:
        for lane in self.lanes:
            self._tasks.append(
                asyncio.create_task(self._run_lane(lane), name=f"lane:{lane.name}")
            )
        # A pool that only reports at shutdown is unobservable for exactly
        # as long as it matters. One line per interval is enough to answer
        # "is anything moving, and what is refusing admission".
        self._tasks.append(asyncio.create_task(self._report_loop(), name="lane:stats"))

    async def _report_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._report_every_s
                )
                return  # stopping
            except asyncio.TimeoutError:
                pass
            snap = (
                self.model._feed.snapshot()
                if getattr(self.model, "_feed", None)
                else None
            )
            logger.info(
                "lanes %s | capacity %s | last refusal: %s",
                {
                    n: f"{s.units_done}d/{s.units_idle}i/{s.units_failed}f"
                    + (f" [{s.inflight} live]" if s.inflight else "")
                    for n, s in self.state.items()
                },
                (
                    f"free={snap.free_cells} seats={snap.seats_free}"
                    f" wait={snap.waiting} src={snap.source}"
                    if snap
                    else "no signal (degraded)"
                ),
                self._last_refusal or "none",
            )

    async def stop(self) -> None:
        """Ask lanes to finish the unit in hand, then cancel what is left.

        A unit in flight is real work with a claim held against it, so it
        gets a grace period to book. Past that the task is cancelled, and
        the drain action's own `finally` releases its claim — the same
        release path a crash would take.
        """
        self._stopping.set()
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=self._stop_grace_s)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    # -- the loop --------------------------------------------------------

    async def _run_lane(self, lane: Lane) -> None:
        st = self.state[lane.name]
        while not self._stopping.is_set():
            if self.deadline is not None and self._now() > self.deadline:
                logger.info("lane %s: run deadline reached, stopping", lane.name)
                return
            try:
                ran = await self._one_unit(lane, st)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a lane never dies
                st.units_failed += 1
                logger.warning(
                    "lane %s: unit failed (%s: %s)",
                    lane.name,
                    type(exc).__name__,
                    str(exc)[:160],
                )
                ran = False
            if not ran:
                # Nothing to do, or nothing fits. Either way, wait — and
                # prefer waiting on the CAPACITY signal, because a stream
                # retiring is the event that changes the answer.
                await self._idle(lane)

    async def _idle(self, lane: Lane) -> None:
        feed = getattr(self.model, "_feed", None)
        waited = None
        if feed is not None and hasattr(feed, "wait_for_change"):
            waited = await feed.wait_for_change(timeout=lane.idle_backoff_s)
        if waited is None:
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=lane.idle_backoff_s
                )
            except asyncio.TimeoutError:
                pass

    async def _one_unit(self, lane: Lane, st: LaneState) -> bool:
        """Admit, run, book. Returns whether work actually happened."""
        if not self._may_dispatch(lane):
            return False

        token = None
        if self.model is not None:
            verdict = self.model.admit(lane.name, lane.est_kv, lane.seats)
            if not verdict.admitted:
                # Kept for the periodic report: "nothing is moving" and
                # "nothing is being ADMITTED" are different diagnoses, and
                # only the refusal reason separates them.
                self._last_refusal = f"{lane.name}: {verdict.reason}"
                logger.debug("lane %s not admitted: %s", lane.name, verdict.reason)
                return False
            token = self.model.reserve(lane.name, lane.est_kv, lane.seats)

        st.inflight += 1
        st.last_dispatch_at = self._now()
        self._resource_inflight[lane.resource] = (
            self._resource_inflight.get(lane.resource, 0) + 1
        )
        try:
            result = await self._run_flow(lane)
        finally:
            st.inflight -= 1
            self._resource_inflight[lane.resource] = max(
                0, self._resource_inflight.get(lane.resource, 1) - 1
            )
            if token is not None and self.model is not None:
                self.model.release(token)

        # A drain that declined (nothing pending, tool missing, server
        # unreachable) did no work — treat it as idle so the lane backs off
        # instead of spinning on an empty queue. Counted separately from
        # work: a shutdown line reporting "9 units" for nine declines
        # against an empty databank is the kind of number that misleads
        # exactly when someone is trying to find out why nothing happened.
        did = bool(result)
        if did:
            st.units_done += 1
        else:
            st.units_idle += 1
        return did

    def _may_dispatch(self, lane: Lane) -> bool:
        cap = self.max_inflight.get(lane.resource, 1)
        return self._resource_inflight.get(lane.resource, 0) < cap

    async def _run_flow(self, lane: Lane) -> bool:
        """Run this lane's drain flow once. True if it did work.

        Runs the real FLOW rather than calling the action directly, so
        trace emission, the ChildEffects ownership contract and branch
        stamping all keep working exactly as they do under the parallel
        step — and the drains stay inspectable as flows.
        """
        from agent.effects.child import ChildEffects
        from agent.runtime import execute_flow

        flow_def = self.flow_registry.get(lane.flow)
        if flow_def is None:
            raise KeyError(f"lane {lane.name}: unknown flow {lane.flow!r}")

        child_fx = ChildEffects(self.effects, branch=f"lane:{lane.name}")
        out = await execute_flow(
            flow_def=flow_def,
            inputs=dict(self.inputs),
            action_registry=self.action_registry,
            effects=child_fx,
            flow_registry=self.flow_registry,
            max_steps=200,
        )
        return _did_work(dict(out.result or {}), dict(out.context or {}))


def _did_work(result: dict, context: dict) -> bool:
    """Did this drain round actually move anything?

    The drains report their own idleness in summaries — `attempted`,
    `attempted_papers`, `figures`, `chunks` — and a lane that treats a
    decline as work would spin on an empty queue at full speed.
    """
    for blob in (result, *(v for v in context.values() if isinstance(v, dict))):
        for key in ("attempted", "attempted_papers", "figures", "chunks", "done"):
            try:
                if int(blob.get(key) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def lanes_for_scraper() -> List[Lane]:
    """The scraper's four drains as lanes.

    est_kv values are the measured p95 context per turn plus that lane's
    per-call budget; they size ADMISSION, not the request itself.
    """
    return [
        # Paddle OCR: its own device, no text seat. One at a time — the
        # tool is a subprocess and the 3060 serves one page batch.
        Lane(name="ocr", flow="ocr_drain", resource="paddle", est_kv=0, seats=0),
        # Figure reads run on muse's vision contexts, which are separate
        # from the batched text cell (measured vision/text serialization
        # 0.068 — effectively free against text).
        Lane(
            name="figtext",
            flow="figtext_drain",
            resource="vision_ctx",
            est_kv=0,
            seats=0,
        ),
        # Both of these hold a text seat and real KV.
        Lane(
            name="translate",
            flow="translate_drain",
            resource="text_seat",
            est_kv=12_000,
        ),
        Lane(
            name="curate",
            flow="curate_drain",
            resource="text_seat",
            est_kv=20_000,
            idle_backoff_s=30.0,
        ),
    ]
