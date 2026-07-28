"""The watchdog acts on ITS request's numbers, and relays the server's verdict.

WHY THIS EXISTS. On 2026-07-28 a qwen3.6-35b arm lost 28 of its 61 minutes to
the health watchdog. Seven cancels; ONE was a real long generation and six were
requests that had produced nothing at all.

The generation tracker is a single-slot singleton, and health published
``tokensGenerated`` / ``generationActive`` without ever saying WHOSE. So when a
cancelled batch kept decoding server-side, every request queued behind it polled
health, read the orphan's counter, and killed itself — ``52,120`` reported
identically four times in a row, then ``54,250``. The run log line
``Content batch: 0/4 data files in 60s`` is four of those, 60s being exactly the
watchdog grace period: they burned their grace waiting in the queue and then
judged themselves on the first poll they got.

The one real cancel was worse. That generation was 57,003 tokens — ~46k of
chain-of-thought followed by a complete, correct eleven-file batch. It was
cancelled at 49,987 against a blind 49,152 ceiling, 200 seconds before the
server delivered the artifact. Nobody received it.

The ceiling was written for the Qwen3-Next repetition hang. LLMVP now catches
that itself, better: RepetitionGuard aborts token-level collapse within tens of
tokens and the long-cycle guard catches paragraph orbits every ~2k tokens, both
naming a reason and dumping the specimen. So the watchdog's job is liveness —
"has the server stopped making progress on MY request" — and content judgement
belongs to the server, whose verdict it now carries out to the flow instead of
approximating with a token count.

The numbers in these tests are the real ones from that run.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from agent.effects.inference import (
    COMPLETION_RUNAWAY_TOKEN_CEILING,
    InferenceEffect,
    _degenerate_reason,
    _request_identity,
)

MINE = "ouro-mine"
THEIRS = "ouro-someone-else"


def _effects(payloads, eval_stuck=None):
    """An InferenceEffect whose health polls return `payloads` in order.

    Timing knobs shrink to ~0 so the loop runs at test speed; the last payload
    repeats, so a loop that declines to cancel keeps polling and it is the
    test's timeout — not a lucky exhaustion of the script — that ends it.
    """
    fx = InferenceEffect(endpoint="http://unused")
    fx._watchdog_grace_s = 0
    fx._watchdog_poll_s = 0
    fx._watchdog_stall_s = 60
    fx._watchdog_eval_stuck_s = eval_stuck
    seq = list(payloads)
    fx.polls = 0

    async def _poll(query):
        fx.polls += 1
        return seq[min(fx.polls - 1, len(seq) - 1)]

    fx._poll_health = _poll  # type: ignore[method-assign]
    return fx


class _FakeTask:
    """The watchdog touches exactly `.done()` and `.cancel()`, so this observes
    the DECISION rather than inferring it from a real Task's state."""

    def __init__(self) -> None:
        self.cancels = 0

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancels += 1


async def _run_watchdog(fx, ceiling=None, request_id="", timeout=1.0):
    """Drive the REAL decision loop. Returns True iff it cancelled."""
    task = _FakeTask()
    wd = asyncio.create_task(fx._health_watchdog(task, ceiling, request_id))
    try:
        await asyncio.wait_for(wd, timeout=timeout)
    except asyncio.TimeoutError:
        wd.cancel()
        try:
            await wd
        except asyncio.CancelledError:
            pass
    return task.cancels > 0


def _gen(**kw):
    """A generating-health payload that CARRIES IDENTITY (the new server)."""
    base = {
        "generationActive": True,
        "tokensGenerated": 10,
        "secondsSinceLastToken": 0,
        "generationPhase": "generating",
        "promptTokens": 100,
        "elapsedSeconds": 5,
        "requestId": MINE,
        "thinkingComplete": False,
        "decodeMode": "pool",
        "engineActiveStreams": 1,
    }
    base.update(kw)
    return base


# ── the six collateral cancels ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_another_requests_runaway_does_not_cancel_mine():
    """THE bug. Health describes someone else's generation, 52,120 tokens deep
    — past the old ceiling — while my request has generated nothing. Six
    requests died this way in one run."""
    fx = _effects([_gen(requestId=THEIRS, tokensGenerated=52_120)])
    cancelled = await _run_watchdog(
        fx, ceiling=COMPLETION_RUNAWAY_TOKEN_CEILING, request_id=MINE
    )
    assert cancelled is False


@pytest.mark.asyncio
async def test_another_requests_stall_does_not_cancel_mine():
    """A stalled neighbour is not evidence about my request either."""
    stalled = _gen(requestId=THEIRS, tokensGenerated=42, secondsSinceLastToken=600)
    fx = _effects([stalled, stalled, stalled])
    assert await _run_watchdog(fx, request_id=MINE) is False


@pytest.mark.asyncio
async def test_queueing_behind_another_request_does_not_start_my_stall_clock():
    """The 60,027 ms cancels: a queued request used to burn its grace period
    waiting for the instance, then judge itself on its first poll. Time spent
    unidentified must not count toward MY stall."""
    theirs = _gen(requestId=THEIRS, tokensGenerated=9_000, secondsSinceLastToken=900)
    mine_live = _gen(requestId=MINE, tokensGenerated=5, secondsSinceLastToken=1)
    fx = _effects([theirs, theirs, theirs, mine_live])
    assert await _run_watchdog(fx, request_id=MINE) is False
    assert fx.polls >= 4, "watchdog stopped polling while queued"


@pytest.mark.asyncio
async def test_an_interleaved_foreign_poll_does_not_re_arm_my_stall_tracking():
    """The near-miss. Skipping a foreign snapshot must not RESET our stall
    state: `tokens > last_token_count` is how the loop decides the generation
    is advancing, so resetting the count to -1 makes the next matched poll take
    the advancing branch unconditionally and never look at the stall. A real
    stall would then be masked for as long as another request kept appearing.
    """
    mine_stalled = _gen(tokensGenerated=42, secondsSinceLastToken=120)
    theirs = _gen(requestId=THEIRS, tokensGenerated=9_000)
    # ALTERNATING, and ending on theirs (the helper repeats the last payload
    # forever). A reset would re-arm on every foreign poll, so the stall would
    # never be reached — whereas a single interleave only DELAYS the cancel by
    # one poll and would let the bug through.
    fx = _effects([mine_stalled, theirs] * 3)
    assert await _run_watchdog(fx, request_id=MINE) is True


# ── the one real cancel: a long generation is not a runaway ───────────────


@pytest.mark.asyncio
async def test_my_long_generation_past_the_ceiling_is_not_cancelled(caplog):
    """57,003 tokens, identified as mine, server guards silent. The server owns
    the degeneration verdict and has not called this degenerate — so it is a
    long generation, not a loop. Cancelling it discarded eleven complete
    files."""
    fx = _effects([_gen(tokensGenerated=57_003, thinkingComplete=False)])
    with caplog.at_level(logging.WARNING):
        cancelled = await _run_watchdog(
            fx, ceiling=COMPLETION_RUNAWAY_TOKEN_CEILING, request_id=MINE, timeout=0.5
        )
    assert cancelled is False
    assert "NOT " in caplog.text and "cancelling" in caplog.text, (
        "crossing the advisory ceiling must still be VISIBLE in the run log — "
        "silently ignoring it trades one blind spot for another"
    )


@pytest.mark.asyncio
async def test_the_advisory_is_logged_once_not_every_poll():
    """A 25-minute generation polls ~50 times. One line, not fifty."""
    fx = _effects([_gen(tokensGenerated=57_000 + n) for n in range(6)])
    records = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("agent.effects.inference")
    logger.addHandler(handler)
    try:
        await _run_watchdog(
            fx, ceiling=COMPLETION_RUNAWAY_TOKEN_CEILING, request_id=MINE, timeout=0.4
        )
    finally:
        logger.removeHandler(handler)
    advisories = [r for r in records if "advisory ceiling" in r.getMessage()]
    assert len(advisories) == 1, f"expected one advisory, got {len(advisories)}"


# ── liveness still bites, on MY numbers ──────────────────────────────────


@pytest.mark.asyncio
async def test_my_stalled_generation_is_still_cancelled():
    """The watchdog's irreducible job: a wedged server cannot report its own
    death, so nothing server-side can replace this."""
    stalled = _gen(tokensGenerated=42, secondsSinceLastToken=120)
    fx = _effects([stalled, stalled])
    assert await _run_watchdog(fx, request_id=MINE) is True


@pytest.mark.asyncio
async def test_my_stuck_eval_is_still_cancelled():
    fx = _effects(
        [_gen(generationPhase="eval", tokensGenerated=0, elapsedSeconds=400)],
        eval_stuck=300,
    )
    assert await _run_watchdog(fx, request_id=MINE) is True


@pytest.mark.asyncio
async def test_my_slow_but_advancing_generation_survives():
    fx = _effects(
        [
            _gen(tokensGenerated=n, secondsSinceLastToken=1)
            for n in (10, 5_000, 20_000, 45_000)
        ]
    )
    assert await _run_watchdog(fx, request_id=MINE) is False


# ── batched mode: the numbers belong to no single request ────────────────


@pytest.mark.asyncio
async def test_batched_blend_is_not_treated_as_mine():
    """Under decode_mode: batched every interleaved start() resets the shared
    status, so with >1 stream live the counter belongs to no one request (see
    GenerationTracker.finish's quiet=True note). The batched engine owns
    per-stream abandonment and force-windowing, so staying quiet loses nothing
    — but acting on a blended stall would kill a healthy stream."""
    blended = _gen(
        tokensGenerated=42,
        secondsSinceLastToken=600,
        decodeMode="batched",
        engineActiveStreams=4,
    )
    fx = _effects([blended, blended, blended])
    assert await _run_watchdog(fx, request_id=MINE) is False


@pytest.mark.asyncio
async def test_batched_with_a_single_stream_is_still_mine():
    """One stream in batched mode is not a blend — the shared status is
    accurate and the stall must still be caught."""
    stalled = _gen(
        tokensGenerated=42,
        secondsSinceLastToken=600,
        decodeMode="batched",
        engineActiveStreams=1,
    )
    fx = _effects([stalled, stalled])
    assert await _run_watchdog(fx, request_id=MINE) is True


# ── fallback: no identity available means the old heuristic ──────────────


@pytest.mark.asyncio
async def test_a_server_without_identity_keeps_the_token_ceiling():
    """An older LLMVP, or a remote-provider passthrough that never touches the
    tracker. No verdict is available, so the blind ceiling is still the best
    bound there is — retiring it everywhere would leave that path unguarded."""
    legacy = {
        "generationActive": True,
        "tokensGenerated": 60_000,
        "secondsSinceLastToken": 0,
        "generationPhase": "generating",
        "promptTokens": 100,
        "elapsedSeconds": 5,
    }
    fx = _effects([legacy])
    assert await _run_watchdog(fx, ceiling=49_152, request_id=MINE) is True


@pytest.mark.asyncio
async def test_no_request_id_of_our_own_keeps_the_token_ceiling():
    """Identity works only if BOTH sides have one. With nothing to match on we
    must not silently match everything."""
    fx = _effects([_gen(requestId=THEIRS, tokensGenerated=60_000)])
    assert await _run_watchdog(fx, ceiling=49_152, request_id="") is True


# ── identity: what we send, and what we match on ─────────────────────────


class TestRequestIdentity:
    def test_a_completion_is_stamped_with_a_fresh_id(self):
        body = {"query": "q", "variables": {"request": {"prompt": "hi"}}}
        rid = _request_identity(body)
        assert rid
        assert body["variables"]["request"]["requestId"] == rid

    def test_two_completions_get_different_ids(self):
        a = _request_identity({"variables": {"request": {"prompt": "x"}}})
        b = _request_identity({"variables": {"request": {"prompt": "x"}}})
        assert a != b

    def test_a_session_turn_matches_on_its_session_id(self):
        """The server labels session generations with the session id rather
        than inventing a second key — turns are sequential and single-driver.
        Stamping our own id here would match nothing, silently disabling the
        gate for every session turn."""
        body = {"variables": {"request": {"sessionId": "sess-7", "prompt": "hi"}}}
        assert _request_identity(body) == "sess-7"
        assert "requestId" not in body["variables"]["request"]

    def test_an_unrecognised_body_yields_no_identity(self):
        """No id rather than a guessed one: a wrong match is worse than none."""
        assert _request_identity({"variables": {"config": {}}}) == ""
        assert _request_identity({}) == ""


# ── the verdict relay ────────────────────────────────────────────────────


class TestDegenerateReason:
    @pytest.mark.parametrize(
        "message",
        [
            "GraphQL errors: run-length 48 of token 271",
            "GraphQL errors: cycle period 4 x 12",
            "long-cycle: long-cycle repetition: 3/2048 distinct 24B n-grams",
            "detokenization failed: invalid start byte",
        ],
    )
    def test_every_guard_reason_is_recognised(self, message):
        """One reason shape per server-side detector. A guard whose wording is
        not matched here goes out as a generic failure — which is exactly the
        state this replaces."""
        assert _degenerate_reason(message)

    def test_the_reason_is_carried_verbatim_from_its_marker(self):
        reason = _degenerate_reason(
            "GraphQL errors: long-cycle repetition: 3/2048 distinct"
        )
        assert reason == "long-cycle repetition: 3/2048 distinct"

    def test_an_ordinary_error_is_not_a_degeneration_verdict(self):
        assert _degenerate_reason("All inference instances are busy") == ""
        assert _degenerate_reason("Cannot connect to LLMVP") == ""
        assert _degenerate_reason("") == ""


class TestVerdictReachesTheResult:
    """Drives the real request path, not a reimplementation of it."""

    @staticmethod
    def _effect_returning(payload):
        fx = InferenceEffect(endpoint="http://unused")
        fx._watchdog_grace_s = 10  # watchdog must not interfere

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        class _Client:
            async def post(self, *a, **kw):
                return _Resp()

        async def _no_poll(query):
            return {}

        fx._poll_health = _no_poll  # type: ignore[method-assign]
        return fx, _Client()

    @pytest.mark.asyncio
    async def test_a_guard_abort_arrives_classified(self):
        fx, client = self._effect_returning(
            {"errors": [{"message": "cycle period 4 x 12"}]}
        )
        result = await fx._request_once_with_watchdog(
            client, {"query": "q", "variables": {"request": {"prompt": "p"}}}
        )
        assert result.degenerate is True
        assert result.degenerate_reason == "cycle period 4 x 12"
        assert result.error, "the raw error must still be carried"

    @pytest.mark.asyncio
    async def test_an_ordinary_graphql_error_is_not_classified(self):
        fx, client = self._effect_returning(
            {"errors": [{"message": "Unknown field 'nope'"}]}
        )
        result = await fx._request_once_with_watchdog(
            client, {"query": "q", "variables": {"request": {"prompt": "p"}}}
        )
        assert result.degenerate is False
        assert result.degenerate_reason == ""

    @pytest.mark.asyncio
    async def test_a_successful_completion_is_not_classified(self):
        fx, client = self._effect_returning(
            {
                "data": {
                    "completion": {
                        "text": "hello",
                        "tokensGenerated": 2,
                        "finished": True,
                    }
                }
            }
        )
        result = await fx._request_once_with_watchdog(
            client, {"query": "q", "variables": {"request": {"prompt": "p"}}}
        )
        assert result.text == "hello"
        assert result.degenerate is False
