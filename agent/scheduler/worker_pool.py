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

from agent.scheduler.capacity_claim import Claim, claim_scope

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
    #: Does this lane size its unit against whatever is free, rather than a
    #: fixed estimate? A dynamic lane claims the pool it was admitted
    #: against and hands back what its work did not need
    #: (agent/scheduler/capacity_claim.py). For these lanes `est_kv` is the
    #: MINIMUM VIABLE UNIT — the admission gate, not the expected draw.
    dynamic_kv: bool = False


# Measured N* per resource comes from the throughput sweep; until it runs
# these are the conservative shapes already proven in production.
DEFAULT_LANE_MAX_INFLIGHT: Dict[str, int] = {
    # 2 -> 5 (2026-08-22): the cap predates the 6-seat split engine; at 2
    # the curate pair saturated it and curate3 AND both translate lanes
    # sat blocked all night — the cap, not the pool, was the ceiling.
    # 5 leaves one seat for the acquire flow's catalog turns; the
    # engine's admission remains the correctness backstop.
    "text_seat": 7,  # tracks the 8-seat engine (one seat spare for catalog turns)
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
    fast_units: int = 0  # consecutive suspiciously-quick "successes"
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
        # A real unit is a document, a figure batch, or a translation
        # round — none complete in under a second.
        self._min_unit_s = 1.0
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
            try:
                snap = self._emit_report()
                await self._emit_capacity_trace(snap)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — telemetry never kills itself
                # Unguarded, a raise here ends the reporting task while the
                # pool keeps running, and the SILENCE looks exactly like a
                # healthy quiet pool. That is the worst possible failure
                # mode for the one thing whose job is to tell us otherwise.
                logger.exception("lane report failed (continuing)")

    def _emit_report(self):
        """Log one lane/capacity line. Returns the snapshot for the tracer."""
        snap = (
            self.model._feed.snapshot() if getattr(self.model, "_feed", None) else None
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
        return snap

    async def _emit_capacity_trace(self, snap) -> None:
        """Persist the same reading the log line shows.

        The log line answers "is anything moving" for a human watching
        live; this answers it for anyone reading the run afterwards. They
        are deliberately fed from ONE snapshot read — two reads would let
        the log and the trace disagree about the same instant, and the
        resulting "the log said seats were free" argument is unwinnable.

        Never raises: `effects` is None in unit tests and a degraded feed
        returns no snapshot at all, and neither is a reason to take down
        the reporting task (see the sibling guard in `_report_loop`).
        """
        if snap is None or self.effects is None:
            return
        emit = getattr(self.effects, "emit_trace", None)
        if emit is None:
            return
        pool = int(getattr(snap, "kv_pool_tokens", 0) or 0)
        live = int(getattr(snap, "live_occupancy", 0) or 0)
        pinned = int(getattr(snap, "pinned_occupancy", 0) or 0)
        try:
            from agent.trace import CapacitySample

            await emit(
                CapacitySample(
                    # The pool is handed a working_directory, not an id;
                    # LocalEffects already tracks the run's mission id from
                    # the first event that carried one, so borrow that
                    # rather than threading a second copy through.
                    mission_id=str(
                        self.inputs.get("mission_id")
                        or getattr(self.effects, "_traced_mission_id", "")
                        or ""
                    ),
                    cycle=-1,  # out-of-band: never 0, which is a real cycle
                    seats_total=int(getattr(snap, "seats_total", 0) or 0),
                    seats_free=int(getattr(snap, "seats_free", 0) or 0),
                    kv_pool_tokens=pool,
                    free_cells=int(getattr(snap, "free_cells", 0) or 0),
                    live_occupancy=live,
                    pinned_occupancy=pinned,
                    pool_slack=int(getattr(snap, "pool_slack", 0) or 0),
                    active_streams=int(getattr(snap, "active_streams", 0) or 0),
                    waiting=int(getattr(snap, "waiting", 0) or 0),
                    serving=bool(getattr(snap, "serving", True)),
                    source=str(getattr(snap, "source", "") or ""),
                    seq=int(getattr(snap, "seq", 0) or 0),
                    # free_cells clamps at 0, so the overshoot that idles
                    # seats is only visible as a ratio above 1.0.
                    occupancy_ratio=round((live + pinned) / pool, 4) if pool else 0.0,
                    lanes={
                        n: f"{s.units_done}d/{s.units_idle}i/{s.units_failed}f"
                        + (f"/{s.inflight} live" if s.inflight else "")
                        for n, s in self.state.items()
                    },
                    last_refusal=self._last_refusal or "",
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — telemetry never kills itself
            logger.debug("capacity trace emit skipped", exc_info=True)

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
        """One lane, until stopped.

        THE WHOLE BODY IS GUARDED, including the idle wait. An earlier
        version guarded only the unit and left `_idle` outside, so a raise
        there killed the lane SILENTLY — the task holds a reference in
        self._tasks, so asyncio never reports the exception, and the pool
        went on looking alive with a dead lane inside it. The comment said
        "a lane never dies" while the code allowed exactly that.
        """
        st = self.state[lane.name]
        reason = "stopping"
        try:
            while not self._stopping.is_set():
                if self.deadline is not None and self._now() > self.deadline:
                    reason = "deadline"
                    return
                try:
                    ran = await self._one_unit(lane, st)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    st.units_failed += 1
                    logger.warning(
                        "lane %s: unit failed (%s: %s)",
                        lane.name,
                        type(exc).__name__,
                        str(exc)[:160],
                    )
                    ran = False
                # RATE FLOOR, independent of the work verdict. Whatever
                # `_did_work` concludes, a unit that returned in under a
                # second did not do a document's worth of work, and a lane
                # that believes otherwise burns a CPU against a broken
                # dependency. This is the backstop for a
                # classification bug rather than a substitute for one:
                # 2,864 rounds ran against a dead server before it existed.
                elapsed = self._now() - st.last_dispatch_at
                if ran and elapsed < self._min_unit_s:
                    st.fast_units += 1
                    if st.fast_units >= 3:
                        logger.warning(
                            "lane %s: %d units in under %.1fs each — treating "
                            "as a stall and backing off",
                            lane.name,
                            st.fast_units,
                            self._min_unit_s,
                        )
                        ran = False
                elif ran:
                    st.fast_units = 0

                if not ran:
                    # Nothing to do, or nothing fits. Either way, wait —
                    # and prefer waiting on the CAPACITY signal, because a
                    # stream retiring is the event that changes the answer.
                    try:
                        await self._idle(lane)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "lane %s: idle wait failed (%s) — backing off",
                            lane.name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(lane.idle_backoff_s)
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception:  # noqa: BLE001
            reason = "CRASHED"
            logger.exception("lane %s died", lane.name)
            raise
        finally:
            # A lane leaving is always worth a line: a pool with a dead
            # lane looks identical to a pool with an idle one.
            logger.info(
                "lane %s exited (%s): %dd/%di/%df",
                lane.name,
                reason,
                st.units_done,
                st.units_idle,
                st.units_failed,
            )

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
            claim_kv = lane.est_kv
            if lane.dynamic_kv:
                # Claim the pool we were just admitted against — the SAME
                # reading admit() used, so the next lane's admit() sees it
                # already spent rather than racing on a stale number. The
                # unit trims this as soon as it knows its real cost.
                claim_kv = max(lane.est_kv, int(verdict.free_cells or 0))
            token = self.model.reserve(lane.name, claim_kv, lane.seats)

        st.inflight += 1
        st.last_dispatch_at = self._now()
        self._resource_inflight[lane.resource] = (
            self._resource_inflight.get(lane.resource, 0) + 1
        )
        claim = None
        if token is not None and self.model is not None:
            claim = Claim(
                tokens=int(claim_kv), lane=lane.name, _model=self.model, _token=token
            )
        try:
            with claim_scope(claim):
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

    ATTEMPTED IS NOT ACCOMPLISHED. An earlier version counted `attempted`
    alone, so when the inference server died the OCR drain kept reporting
    "attempted 4", failing instantly, and the lane looped as fast as the
    toolchain could fail — 2,864 rounds against a dead server, hammering
    a machine that could not answer. A round only counts as work if it
    was neither an explicit DECLINE (drains set `reason` when they stand
    down) nor an outright FAILURE.
    """
    for blob in (result, *(v for v in context.values() if isinstance(v, dict))):
        if not isinstance(blob, dict):
            continue
        if blob.get("reason"):
            continue  # the drain said why it did nothing
        if str(blob.get("status") or "") == "failed":
            continue  # it tried and nothing landed
        if blob.get("outcomes"):
            return True
        # "mined"/"promoted"/"recovered": the network lanes' work verbs.
        # Live 2026-08-21: the biblio lane's first 8 rounds mined 90
        # papers and promoted 63 candidates while the report showed
        # 0d/8i — productive rounds counted idle, backoff throttled a
        # healthy lane, and the telemetry lied in the pessimistic
        # direction (the one direction this classifier must not lie in
        # is the OTHER one; still, a lane that works should count).
        for key in (
            "done",
            "figures",
            "chunks",
            "attempted_papers",
            "attempted",
            "mined",
            "promoted",
            "recovered",
        ):
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
        # ── TRANSLATE LANES CLOSED (2026-08-22 overnight, operator) ──
        # Two findings closed them: (1) the curator reviews originals
        # fine — _build_doc_for falls back to the source md, and all 22
        # untranslated non-en denials were substantive content verdicts,
        # so translation adds NO review value; (2) 5 of 27 verdicted
        # translations were spent on papers the curator then denied.
        # Translation is training-form work and belongs AFTER acceptance
        # (~halves the remaining translate load: 296 pending non-en x
        # ~accept-rate instead of all of them). Re-open as a post-accept
        # gated lane when the review drain closes. The .parts.jsonl
        # banking keeps every in-flight chunk durable meanwhile.
        # Lane(
        #     name="translate",
        #     flow="translate_drain",
        #     resource="text_seat",
        #     est_kv=12_000,
        # ),
        # Second translate lane on the SAME drain: the pool's recent
        # occupancy (p50 0.37, seats free 94% of ticks, 2026-08-21) says
        # the seats are under-used while 278 lingual papers queue — lane
        # SERIALISM, not seat count, was the binding limit. Two lanes of
        # one drain are safe by construction: _TRANSLATE_CLAIMS is shared
        # in-process, so they claim different papers; the deferral set
        # rotates both past wedged papers. This is also the seat-count
        # experiment run on existing seats — if the doubled lanes push
        # occupancy p50 back above ~0.9, a fifth engine seat earns its
        # place at the next server restart.
        # Lane(
        #     name="translate2",
        #     flow="translate_drain",
        #     resource="text_seat",
        #     est_kv=12_000,
        # ),
        Lane(
            name="curate",
            flow="curate_drain",
            resource="text_seat",
            # MINIMUM VIABLE UNIT, not the expected draw: the smallest
            # doc worth curating (4k tok) plus one turn's overhead
            # (14k). Keep in step with _CURATE_MIN_DOC_TOKENS +
            # _CURATE_TURN_OVERHEAD_TOKENS in curation_actions.py.
            est_kv=18_000,
            dynamic_kv=True,
            idle_backoff_s=30.0,
        ),
        # Second curate lane (overnight guidance, 2026-08-22): review is
        # the corpus long tail (~1,100 pending), and the layer-split pool
        # (131k cells) ran with ~80k cells free while a single curate
        # lane churned 20 units/window on the auto-scaled doc budget.
        # Shared _CURATE_CLAIMS keeps the lanes on different papers; the
        # engine QUEUE verdict arbitrates when two big docs collide.
        Lane(
            name="curate2",
            flow="curate_drain",
            resource="text_seat",
            # MINIMUM VIABLE UNIT, not the expected draw: the smallest
            # doc worth curating (4k tok) plus one turn's overhead
            # (14k). Keep in step with _CURATE_MIN_DOC_TOKENS +
            # _CURATE_TURN_OVERHEAD_TOKENS in curation_actions.py.
            est_kv=18_000,
            dynamic_kv=True,
            idle_backoff_s=30.0,
        ),
        # Third curate lane (2026-08-22 03:xx): two lanes ran 59d/59d
        # zero-idle at ~70 verdicts/h while 4 seats and 92k cells sat
        # free — the pool still outruns curate submission.
        Lane(
            name="curate3",
            flow="curate_drain",
            resource="text_seat",
            # MINIMUM VIABLE UNIT, not the expected draw: the smallest
            # doc worth curating (4k tok) plus one turn's overhead
            # (14k). Keep in step with _CURATE_MIN_DOC_TOKENS +
            # _CURATE_TURN_OVERHEAD_TOKENS in curation_actions.py.
            est_kv=18_000,
            dynamic_kv=True,
            idle_backoff_s=30.0,
        ),
        Lane(
            name="curate4",
            flow="curate_drain",
            resource="text_seat",
            # MINIMUM VIABLE UNIT, not the expected draw: the smallest
            # doc worth curating (4k tok) plus one turn's overhead
            # (14k). Keep in step with _CURATE_MIN_DOC_TOKENS +
            # _CURATE_TURN_OVERHEAD_TOKENS in curation_actions.py.
            est_kv=18_000,
            dynamic_kv=True,
            idle_backoff_s=30.0,
        ),
        # OA recovery: pure network I/O (Wayback / CORE / meta-tag routes)
        # — no muse seat, no KV, paced by the shared per-host politeness
        # state. Long idle backoff: each record is walked ONCE (stamped),
        # so once the pool is swept the lane is a cheap periodic no-op
        # until new unresolved records arrive from cataloging.
        Lane(
            name="recover",
            flow="oa_recover_drain",
            resource="network",
            est_kv=0,
            seats=0,
            idle_backoff_s=300.0,
        ),
        # Bibliography snowball: mine accepted papers' reference DOIs and
        # walk the repeatedly-cited backlog into candidates. Network-only,
        # like recover; long backoff — its work arrives at curation speed
        # (a few accepted papers an hour), not network speed.
        Lane(
            name="biblio",
            flow="biblio_drain",
            resource="network",
            est_kv=0,
            seats=0,
            idle_backoff_s=600.0,
        ),
    ]
