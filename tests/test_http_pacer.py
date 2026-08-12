"""Pacer tests — the properties that make concurrent acquisition safe.

The old pacing read mission state, slept, issued, then wrote the timestamp
back. Serially that under-paced (it measured response-to-request); concurrently
it was a burst generator, because every in-flight caller read the same
`last_ts`, computed the same wait, and fired together. These tests pin the
behaviours that fix means.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent.actions.http_pacer import HostPacer

URL = "https://api.example.org/works"
OTHER = "https://api.other.org/works"


@pytest.mark.asyncio
async def test_concurrent_callers_are_spaced_not_bursted():
    """THE regression this module exists for.

    Five coroutines hit one host at once. Under the old read-modify-write they
    all saw the same last_ts and fired simultaneously. The pacer must hand out
    five spaced slots instead.
    """
    pacer = HostPacer(default_interval=0.05)
    starts: list[float] = []

    async def call():
        await pacer.reserve(URL)
        starts.append(time.monotonic())

    await asyncio.gather(*(call() for _ in range(5)))

    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(gaps) == 4
    # Allow scheduler slop but require real separation — a burst would show
    # gaps near zero.
    assert all(g >= 0.03 for g in gaps), gaps


@pytest.mark.asyncio
async def test_hosts_are_paced_independently():
    """One slow host must not throttle a different one."""
    pacer = HostPacer(default_interval=0.05)
    t0 = time.monotonic()
    await asyncio.gather(pacer.reserve(URL), pacer.reserve(OTHER))
    # Two different hosts: both first calls are free, so this is fast.
    assert time.monotonic() - t0 < 0.04


@pytest.mark.asyncio
async def test_slot_is_reserved_before_the_request_not_after():
    """The cursor advances at reserve() time.

    The old code stamped the timestamp AFTER the response returned, so spacing
    drifted with latency. Two sequential reserves with no intervening request
    must still be separated by the interval.
    """
    pacer = HostPacer(default_interval=0.05)
    await pacer.reserve(URL)
    t0 = time.monotonic()
    await pacer.reserve(URL)
    assert time.monotonic() - t0 >= 0.03


@pytest.mark.asyncio
async def test_retry_after_is_honoured_as_given_not_floored_at_ten():
    """The old floor was max(retry_after, 10.0) — a flat 10s even when the API
    asked for 1s. Across 714 429s that was ~2.0h of an 8h budget."""
    pacer = HostPacer(default_interval=0.01)
    assert await pacer.note_throttled(URL, 1.0) == 1.0
    assert await pacer.note_throttled(URL, 0.0) == 1.0  # floor, not 10
    assert await pacer.note_throttled(URL, 999.0) == 60.0  # cap


@pytest.mark.asyncio
async def test_repeated_429s_widen_the_host_then_decay_back():
    """A 429 is the server saying our configured number is wrong."""
    pacer = HostPacer(default_interval=1.0)
    await pacer.reserve(URL)
    base = pacer.stats()["api.example.org"]["interval"]

    for _ in range(3):
        await pacer.note_throttled(URL, 0.0)
    widened = pacer.stats()["api.example.org"]["interval"]
    assert widened > base

    for _ in range(20):
        await pacer.note_ok(URL)
    assert pacer.stats()["api.example.org"]["interval"] == pytest.approx(base, abs=1e-6)


@pytest.mark.asyncio
async def test_widening_is_bounded():
    """Adaptive must not become an unbounded crawl that hides a broken run."""
    pacer = HostPacer(default_interval=1.0)
    await pacer.reserve(URL)
    for _ in range(200):
        await pacer.note_throttled(URL, 0.0)
    assert pacer.stats()["api.example.org"]["interval"] <= 8.0


@pytest.mark.asyncio
async def test_counters_survive_concurrency():
    """Lost-update was the other half of the read-modify-write bug."""
    pacer = HostPacer(default_interval=0.001)
    await asyncio.gather(*(pacer.reserve(URL) for _ in range(25)))
    assert pacer.total_requests() == 25
    assert pacer.stats()["api.example.org"]["requests"] == 25
