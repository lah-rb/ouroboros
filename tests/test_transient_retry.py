"""Transient infrastructure failures must not kill a mission.

THE INCIDENT (2026-07-26). A mistral boss run died five minutes in, with zero
goals, on this:

    [design_initial] Inference error: All inference instances are busy
                     (active=1, limit=1)
    [failed] Failed to design architecture and derive goals

A pressure probe had leaked a server-side prefill — its client timed out but
never cancelled, so a 57k-token eval kept the only seat. The boss asked for
that seat at the wrong moment and the mission ended. The seat was free minutes
later; the relaunch ran for hours.

WHY IT WAS FATAL rather than a lost cycle: `design_initial`'s resolver is

    {condition: "result.tokens_generated > 0", transition: "parse_architecture"},
    {condition: "true",                        transition: "failed"},   // terminal

"We never got an instance" and "the model generated nothing" both arrive with
tokens_generated == 0, so both route to a TERMINAL step. Five planning steps
share that shape (design_and_plan x2, replan x2, plan_research x1) — precisely
the steps whose failure kills the whole mission rather than one cycle.

The retry lives in the effect layer, not the flows: capacity is infrastructure,
not flow semantics, and requiring every flow author to handle "busy" is how one
gets missed.
"""

from __future__ import annotations

import pytest

from agent.effects.inference import InferenceEffect, InferenceResult


def _busy() -> InferenceResult:
    return InferenceResult(
        text="",
        tokens_generated=0,
        finished=False,
        error="GraphQL errors: All inference instances are busy [default] — "
        "try again later (active=1, limit=1)",
    )


def _ok(text: str = "architecture") -> InferenceResult:
    return InferenceResult(text=text, tokens_generated=42, finished=True)


def _effects(scripted: list[InferenceResult], retries: int = 3) -> InferenceEffect:
    fx = InferenceEffect(endpoint="http://unused")
    fx._transient_retries = retries
    fx._TRANSIENT_BACKOFF_S = (0.0, 0.0, 0.0)  # no real waiting in tests
    fx.attempts = 0
    seq = list(scripted)

    async def _once(client, body, response_key="completion", ceiling=None):
        fx.attempts += 1
        return seq[min(fx.attempts - 1, len(seq) - 1)]

    fx._request_once_with_watchdog = _once  # type: ignore[method-assign]
    return fx


async def _run(fx):
    return await fx._request_with_health_watchdog(None, {"query": "x"})


# ── the incident ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_busy_seat_that_frees_up_does_not_kill_the_request():
    """THE regression. Busy once, then the seat frees — the caller must see
    success, not a terminal failure."""
    fx = _effects([_busy(), _ok()])
    result = await _run(fx)
    assert result.error is None
    assert result.tokens_generated == 42
    assert fx.attempts == 2


@pytest.mark.asyncio
async def test_retries_are_bounded_and_the_last_error_is_returned():
    """Persistent capacity failure must still terminate — with the real error
    surfaced, not swallowed."""
    fx = _effects([_busy()], retries=3)
    result = await _run(fx)
    assert fx.attempts == 4  # initial + 3
    assert "busy" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_connection_errors_retry_too():
    fx = _effects(
        [
            InferenceResult(
                text="",
                tokens_generated=0,
                finished=False,
                error="Connection error: [Errno 61] Connection refused",
            ),
            _ok(),
        ]
    )
    assert (await _run(fx)).tokens_generated == 42
    assert fx.attempts == 2


# ── what must NOT retry ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_genuinely_empty_generation_is_not_retried():
    """The distinction the resolver could not make. An empty generation with
    no error is a REAL result — retrying it would burn budget re-asking a
    model that already answered."""
    empty = InferenceResult(text="", tokens_generated=0, finished=True)
    fx = _effects([empty])
    result = await _run(fx)
    assert fx.attempts == 1
    assert result.tokens_generated == 0


@pytest.mark.asyncio
async def test_watchdog_aborts_are_not_retried():
    """A watchdog cancel is a considered decision (stall or runaway). Retrying
    it would re-run the exact generation the watchdog just judged pathological."""
    aborted = InferenceResult(
        text="",
        tokens_generated=0,
        finished=False,
        error="Generation aborted by watchdog (stall or runaway)",
    )
    fx = _effects([aborted])
    await _run(fx)
    assert fx.attempts == 1


@pytest.mark.asyncio
async def test_timeouts_are_not_retried():
    """Deliberate: on a timeout the server may STILL be working, and retrying
    stacks load onto a busy box — that is exactly how the orphaned prefill
    which caused the incident came to exist."""
    timed_out = InferenceResult(
        text="", tokens_generated=0, finished=False, error="Timeout: read timed out"
    )
    fx = _effects([timed_out])
    await _run(fx)
    assert fx.attempts == 1


@pytest.mark.asyncio
async def test_graphql_schema_errors_are_not_retried():
    """A schema error is a bug, not weather — retrying just delays the report."""
    bad = InferenceResult(
        text="",
        tokens_generated=0,
        finished=False,
        error='GraphQL errors: Unknown field "nonsense" on type "Health"',
    )
    fx = _effects([bad])
    await _run(fx)
    assert fx.attempts == 1


@pytest.mark.asyncio
async def test_retries_can_be_disabled():
    fx = _effects([_busy(), _ok()], retries=0)
    result = await _run(fx)
    assert fx.attempts == 1
    assert "busy" in (result.error or "").lower()
