"""Inference health watchdog — the only bound on a hanging request.

Agent inference runs with `httpx timeout=None`. That is deliberate (a long
prefill on a big prompt is not an error), but it means the watchdog is the
ONLY thing that can end a wedged request. 237 consecutive lines of it were
uncovered — the classic "only runs when something is already going wrong"
path, where nobody exercises it by hand.

BOTH failure directions are expensive, which is why each decision needs
pinning rather than just "it cancels eventually":

* too AGGRESSIVE — the 2026-07-23 incident recorded in the source: a fixed
  300s eval cancel discarded NINE completed generations at 90%+, and each
  cancel briefly wedged the single instance for the retry. Dense mistral
  prefills ~45 tok/s, so a 15k prompt legitimately needs ~335s.
* too LAX — an infinite hang. Nothing else is watching.

Enabled by two changes in agent/effects/inference.py: the watchdog was a
NESTED CLOSURE (unreachable from a test) and is now a method, and its three
timing constants were function-local (unshrinkable) and are now instance
attributes, which is what TESTING.md already asks for. Tests drive the real
decision loop with scripted health payloads through the `_poll_health` seam —
no HTTP, no server.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.effects.inference import InferenceEffect


def _effects(payloads, eval_stuck=None):
    """An InferenceEffect whose health polls return `payloads` in order.

    Timing knobs are shrunk to ~0 so the loop runs at test speed; the last
    payload repeats so a loop that declines to cancel simply keeps polling
    (and the test's timeout, not a lucky exhaustion, is what fails it).
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
    """Stands in for the request task. The watchdog touches exactly two
    things on it — `.done()` and `.cancel()` — so this observes the DECISION
    directly, rather than inferring it from a real Task's state (which is not
    `cancelled()` until it has actually processed the cancellation)."""

    def __init__(self) -> None:
        self.cancels = 0

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancels += 1


async def _run_watchdog(fx, ceiling=None, timeout=1.0):
    """Drive the real decision loop. Returns True iff it cancelled."""
    task = _FakeTask()
    wd = asyncio.create_task(fx._health_watchdog(task, ceiling))
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
    """A generating-health payload with sane defaults."""
    base = {
        "generationActive": True,
        "tokensGenerated": 10,
        "secondsSinceLastToken": 0,
        "generationPhase": "generating",
        "promptTokens": 100,
        "elapsedSeconds": 5,
    }
    base.update(kw)
    return base


# ── decision 1: runaway token ceiling ─────────────────────────────────────


@pytest.mark.asyncio
async def test_cancels_a_runaway_generation_past_the_ceiling():
    """Tokens still ADVANCING, so stall detection can never fire on this —
    a repetition loop is only catchable by the ceiling."""
    fx = _effects([_gen(tokensGenerated=9999)])
    assert await _run_watchdog(fx, ceiling=500) is True


@pytest.mark.asyncio
async def test_does_not_cancel_below_the_ceiling():
    fx = _effects([_gen(tokensGenerated=100)])
    assert await _run_watchdog(fx, ceiling=500) is False


@pytest.mark.asyncio
async def test_no_ceiling_means_no_runaway_cancel():
    """A request type without a ceiling must not inherit one."""
    fx = _effects([_gen(tokensGenerated=10**6)])
    assert await _run_watchdog(fx, ceiling=None) is False


# ── decision 2: eval phase stuck ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancels_when_eval_exceeds_the_limit():
    fx = _effects(
        [_gen(generationPhase="eval", tokensGenerated=0, elapsedSeconds=400)],
        eval_stuck=300,
    )
    assert await _run_watchdog(fx) is True


@pytest.mark.asyncio
async def test_slow_eval_under_the_limit_is_left_alone():
    """The 2026-07-23 incident direction: a legitimately slow prefill must
    NOT be cancelled."""
    fx = _effects(
        [_gen(generationPhase="eval", tokensGenerated=0, elapsedSeconds=200)],
        eval_stuck=300,
    )
    assert await _run_watchdog(fx) is False


@pytest.mark.asyncio
async def test_server_advertised_expected_eval_raises_the_limit():
    """When the server offers expectedEvalSeconds (its measured worst case for
    THIS prompt), it gets 2x headroom — the server owns model-speed knowledge.
    400s elapsed would trip the 300s floor but must not trip 2 x 250s."""
    fx = _effects(
        [
            _gen(
                generationPhase="eval",
                tokensGenerated=0,
                elapsedSeconds=400,
                expectedEvalSeconds=250,
            )
        ],
        eval_stuck=300,
    )
    assert await _run_watchdog(fx) is False


# ── decision 3: token stall ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancels_when_tokens_stop_advancing():
    """Same token count twice AND the server confirms the stall."""
    stalled = _gen(tokensGenerated=42, secondsSinceLastToken=120)
    fx = _effects([stalled, stalled])
    assert await _run_watchdog(fx) is True


@pytest.mark.asyncio
async def test_advancing_tokens_reset_the_stall_tracking():
    """A slow but live generation must survive indefinitely."""
    fx = _effects(
        [_gen(tokensGenerated=n, secondsSinceLastToken=1) for n in (10, 20, 30, 40)]
    )
    assert await _run_watchdog(fx) is False


@pytest.mark.asyncio
async def test_stall_below_threshold_does_not_cancel():
    stalled = _gen(tokensGenerated=42, secondsSinceLastToken=5)
    fx = _effects([stalled, stalled])
    assert await _run_watchdog(fx) is False


# ── decision 4: finish, and the degradation paths ─────────────────────────


@pytest.mark.asyncio
async def test_returns_quietly_once_generation_finishes():
    """Not a cancel: the watchdog stands down and lets the request land."""
    fx = _effects(
        [_gen(tokensGenerated=10), {"generationActive": False, "tokensGenerated": 10}]
    )
    assert await _run_watchdog(fx) is False


@pytest.mark.asyncio
async def test_a_failing_health_poll_never_kills_the_request():
    """A health-check error is OUR problem, not the generation's — killing a
    healthy request over it would be the worst possible trade."""
    fx = InferenceEffect(endpoint="http://unused")
    fx._watchdog_grace_s = 0
    fx._watchdog_poll_s = 0

    async def _boom(query):
        raise RuntimeError("health endpoint down")

    fx._poll_health = _boom  # type: ignore[method-assign]
    assert await _run_watchdog(fx, timeout=0.5) is False


@pytest.mark.asyncio
async def test_empty_health_downgrades_the_query_once_then_keeps_polling():
    """An older server rejects expectedEvalSeconds, which surfaces as empty
    data. The watchdog must fall back to the legacy query rather than poll a
    dead one for the whole run — a silent no-op watchdog is an unbounded
    request."""
    fx = _effects([{}, {}, _gen(tokensGenerated=5)])
    await _run_watchdog(fx, timeout=0.5)
    assert fx.polls >= 2, "watchdog stopped polling after an empty health payload"
