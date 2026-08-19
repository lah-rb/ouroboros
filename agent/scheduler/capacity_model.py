"""Does this work fit right now? — an OPTIMIZER, never a guarantee.

THE SAFETY ARGUMENT, first, because everything else follows from it.

LLMVP's own admission control is the correctness backstop. `_size_against_pool`
admits at full budget, shrinks to fit above a floor, queues below it, and
fails only what can never fit. That logic runs on the server, holds the
authoritative numbers, and cannot be raced. This module exists purely to
stop us SENDING work that will queue — nothing here is load-bearing for
correctness, and any future change that makes this model authoritative
about remote state is a bug, not an optimization.

That asymmetry is what makes the reservation scheme safe. Holding a
reservation too long makes us conservative: we under-dispatch and lose
throughput. Dropping one too early makes us over-dispatch — and the
server absorbs it by shrinking or queueing. One error costs speed, the
other costs nothing but a queued request. So every ambiguous case here
resolves toward holding on.

THE RACE, precisely. We dispatch at t0 against snapshot seq=k. The server
admits at t1 and publishes seq=k+1 at t2. Between t0 and t2 the snapshot
under-reports our own draw, so a second dispatch in that window would
spend the same cells twice. Reservations cover exactly that gap.

Reservations retire on whichever comes first:
  * the request finishes (the common case, and exact);
  * a snapshot arrives with seq > issued_against + 1, meaning a full
    publish cycle has elapsed SINCE the dispatch, so the admit is
    necessarily reflected in what we are now reading;
  * a wall-clock settle timeout, which catches a request that died
    without telling us.

Note what is deliberately absent: matching reservations to individual
server streams. The snapshot carries no request id, and adding one would
couple this client to the server's internals — the coupling the whole
capacity design exists to avoid.

TWO LIMITS, NOT ONE. Seats are concurrency; cells are context. Free KV
does not manufacture a seat, and a free seat does not make a large prompt
fit. Work needs both, and the two exhaust independently.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# What a lane may run when we have NO capacity signal at all. One: the
# behaviour the system had before any of this existed, so the worst case
# of the whole capacity stack is "no faster than before".
DEGRADED_WIDTH = 1


@dataclass
class Reservation:
    token: str
    lane: str
    seats: int
    est_kv: int
    issued_at: float
    issued_against_seq: int


@dataclass
class Verdict:
    """Why admission was refused, in words a log can use."""

    admitted: bool
    reason: str = ""
    free_cells: int = 0
    free_seats: int = 0


class CapacityModel:
    """Local view of remote capacity, minus what we have already spent."""

    def __init__(self, feed=None) -> None:
        self._feed = feed
        self._pending: Dict[str, Reservation] = {}
        self._counter = 0
        self._reservation_settle_s = 30.0
        # Floor under the seq-based release: no reservation clears until
        # at least this long after dispatch, because `seq` is moved by
        # every seat and can advance in milliseconds (measured min gap
        # 46 ms). Sized above a dispatch -> admit round trip.
        self._min_settle_s = 5.0
        self._now = time.monotonic  # seam for tests

    # -- accounting ------------------------------------------------------

    def _reap(self, seq: int) -> None:
        """Drop reservations the server has certainly accounted for.

        THE SEQ RULE NEEDS A FLOOR, and the reason is measured. The rule
        reads "a publish cycle has elapsed since we dispatched, so our
        admit is in the numbers" — which silently assumes OUR dispatch is
        what moved `seq`. It is not: every seat's admits and retires move
        it. Observed beside the live mission, consecutive publishes came
        as little as 46 ms apart, so under any burst a reservation could
        clear seq+1 long before our own request reached the engine, and
        the model would hand the same cells out twice at exactly the
        moment the pool was busiest.

        So a seq-based release also has to be older than the dispatch ->
        admit round trip. Completion (the exact signal, and the common
        case) still releases immediately; the timeout still catches a
        request that died silently.
        """
        now = self._now()
        for token, r in list(self._pending.items()):
            age = now - r.issued_at
            settled_by_seq = (
                seq > r.issued_against_seq + 1 and age >= self._min_settle_s
            )
            settled_by_time = age > self._reservation_settle_s
            if settled_by_seq or settled_by_time:
                del self._pending[token]

    def effective(self) -> tuple[int, int]:
        """(free_cells, free_seats) after subtracting our own in-flight."""
        snap = self._feed.snapshot() if self._feed else None
        if snap is None:
            return (0, 0)
        self._reap(snap.seq)
        held_kv = sum(r.est_kv for r in self._pending.values())
        held_seats = sum(r.seats for r in self._pending.values())
        return (
            max(0, snap.free_cells - held_kv),
            max(0, snap.seats_free - held_seats),
        )

    # -- the gate --------------------------------------------------------

    def admit(self, lane: str, est_kv: int, seats: int = 1) -> Verdict:
        """May this unit of work start now?"""
        snap = self._feed.snapshot() if self._feed else None

        # No signal — the caller's own conservative default governs, and
        # it is the pre-capacity behaviour rather than a stall.
        if snap is None:
            inflight = len(self._pending)
            if inflight >= DEGRADED_WIDTH:
                return Verdict(False, "no capacity signal (degraded to width 1)")
            return Verdict(True, "degraded")

        # A PARKED ENGINE STILL SHOWS FREE CELLS. Dispatching into a
        # context rebuild is the bug `serving` exists to prevent.
        if not snap.serving:
            return Verdict(False, "server not serving (paused or rebuilding)")
        if snap.engine_fatal:
            return Verdict(False, f"engine fatal: {snap.engine_fatal[:60]}")

        free_cells, free_seats = self.effective()

        # WAITING > 0 MEANS STOP, not "try harder". The server's queue is
        # head-blocking by design, so adding load behind a queued request
        # only lengthens its wait.
        if snap.waiting > 0:
            return Verdict(
                False, f"server queue depth {snap.waiting}", free_cells, free_seats
            )

        # SEATS ARE THE TEXT POOL'S limit, and only work that runs there
        # may be refused for want of one. `seats=0` means "served by
        # something else" — paddle on its own device, muse's separate
        # vision contexts — and gating those on the text pool starves a
        # whole GPU on the other one's contention. Observed live: "ocr: no
        # free seat" refusing paddle work while paddle was idle.
        if seats and free_seats < seats:
            return Verdict(
                False, f"no free seat ({free_seats} free)", free_cells, free_seats
            )

        # Legacy snapshots know seats but not cells; admit on seats alone
        # rather than reading an unknown 0 as "full".
        # WORK THAT OPENS NO TEXT STREAM IS NOT BOUND BY THE TEXT POOL.
        # `est_kv == 0 and seats == 0` says this unit is served elsewhere
        # entirely — paddle OCR is a subprocess against its own resident
        # model, figure reads run on separate vision contexts. Neither
        # allocates a cell in the batched text cell, so charging them
        # min_admit_budget refuses them exactly when muse is busy. This is
        # the same mistake as gating them on seats, surviving one check
        # further down: observed live as "ocr: needs 512 cells, 0 free"
        # while paddle had capacity to spare.
        if snap.knows_kv and (est_kv or seats):
            needed = est_kv + snap.min_admit_budget
            if free_cells < needed:
                return Verdict(
                    False,
                    f"needs {needed} cells, {free_cells} free",
                    free_cells,
                    free_seats,
                )

        return Verdict(True, "fits", free_cells, free_seats)

    def reserve(self, lane: str, est_kv: int, seats: int = 1) -> str:
        """Record what we are about to spend, before the server sees it."""
        snap = self._feed.snapshot() if self._feed else None
        self._counter += 1
        token = f"{lane}-{self._counter}"
        self._pending[token] = Reservation(
            token=token,
            lane=lane,
            seats=seats,
            est_kv=est_kv,
            issued_at=self._now(),
            issued_against_seq=snap.seq if snap else 0,
        )
        return token

    def release(self, token: str) -> None:
        """The request finished — exact, and the common case."""
        self._pending.pop(token, None)

    def on_seat_busy(self) -> None:
        """The server said every seat is taken.

        A hard fact that outranks any snapshot: hold a full set of
        reservations so nothing else dispatches until the next publish
        corrects us.
        """
        snap = self._feed.snapshot() if self._feed else None
        total = snap.seats_total if snap else 1
        for _ in range(max(1, total)):
            self.reserve("seat-busy", est_kv=0, seats=1)

    @property
    def inflight(self) -> int:
        return len(self._pending)


# Margin over an EXACT token count. The count is of the text we hold; the
# server wraps it in a chat template (role markers, system framing) before
# decoding, and that wrapper is not ours to measure. 10% covers it with
# room, and unlike the char heuristic it scales with the thing it protects.
TOKENIZE_MARGIN = 1.10


def estimate_kv_draw(
    prompt_chars: int,
    max_tokens: int,
    static_prefix_tokens: int = 0,
    exact_prompt_tokens: int | None = None,
) -> int:
    """Cells one request will be charged.

    ENTITLEMENT, NOT USAGE — the engine charges `gen_start_pos +
    effective_max`, so the full max_tokens counts from the moment of
    admission whether or not it is generated. Sizing against expected
    output instead is how a pool ends up 59% phantom.

    TWO WAYS TO SIZE THE PROMPT, and they are not equally good.
    `exact_prompt_tokens` comes from the serving model's own tokenizer
    (Query.tokenCount) and is the right answer: only the server knows that
    muse counts against a 202,048-token vocabulary and paddle against
    103,424. Without it we fall back to chars x 13/40 — a fitted constant
    with no model in it, which mis-sizes anything that is not the English
    prose it was fitted on. Non-Latin text is the obvious break: the same
    character count is a very different token count in CJK.

    The resident static prefix is discounted either way: it is shared KV
    that a new stream forks rather than duplicates (a static token measured
    ~6% of a private one), so charging it over-bills every request by the
    same fixed amount.
    """
    if exact_prompt_tokens is not None:
        prompt_tokens = int(exact_prompt_tokens * TOKENIZE_MARGIN)
    else:
        prompt_tokens = (prompt_chars * 13) // 40
    return max(0, prompt_tokens - static_prefix_tokens) + max_tokens


async def size_request(
    effects,
    prompt: str,
    max_tokens: int,
    static_prefix_tokens: int = 0,
    model: str = "",
) -> tuple[int, str]:
    """(cells, how) for one request — exact if the server will say, else
    estimated. `how` is returned so a caller can log WHICH answer it got:
    a silent fallback to the heuristic is the failure mode that makes a
    sizing bug invisible.
    """
    counts: list[int] = []
    fn = getattr(effects, "token_count", None)
    if fn is not None:
        try:
            counts = await fn([prompt], model)
        except Exception:  # noqa: BLE001 — sizing never fails a dispatch
            counts = []
    if counts:
        return (
            estimate_kv_draw(
                len(prompt),
                max_tokens,
                static_prefix_tokens,
                exact_prompt_tokens=counts[0],
            ),
            "exact",
        )
    return (
        estimate_kv_draw(len(prompt), max_tokens, static_prefix_tokens),
        "estimated",
    )
