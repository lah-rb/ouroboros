"""The pool's lifecycle around a mission run.

The property that matters most is a NEGATIVE one: a v1 mission — the one
carrying the live corpus — must get no pool, no capacity feed, and no
behavioural change whatsoever. Opt-in by flow set is what makes it safe
to land this while a mission is running.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.scheduler.lifecycle import POOLED_FLOW_SETS, worker_pool_for


class _Fx:
    """Minimal effects: a flow set, and a record of what got started."""

    def __init__(self, flow_set):
        self._flow_set = flow_set
        self.capacity_started = False
        self.capacity_stopped = False
        self.working_directory = "/tmp/wd"

    async def load_mission(self):
        class _Cfg:
            flow_set = self._flow_set

        class _M:
            config = _Cfg()

        return _M()

    def capacity_start(self):
        self.capacity_started = True

    async def capacity_stop(self):
        self.capacity_stopped = True


@pytest.mark.asyncio
async def test_a_v1_mission_gets_no_pool_and_no_feed():
    """THE safety property. v1 is frozen and running; nothing here may
    touch it."""
    fx = _Fx("scraper")
    async with worker_pool_for(fx, flows_dir="flows") as pool:
        assert pool is None
    assert not fx.capacity_started, "a v1 mission must not even open a feed"


@pytest.mark.asyncio
async def test_an_unknown_or_unreadable_mission_gets_no_pool():
    class _Broken:
        working_directory = ""

        async def load_mission(self):
            raise RuntimeError("no mission")

    async with worker_pool_for(_Broken(), flows_dir="flows") as pool:
        assert pool is None


@pytest.mark.asyncio
async def test_a_v2_mission_starts_lanes_and_stops_them_cleanly():
    fx = _Fx("scraper_v2")
    async with worker_pool_for(fx, flows_dir="flows") as pool:
        assert pool is not None
        assert {ln.name for ln in pool.lanes} == {
            "ocr",
            "figtext",
            "translate",
            "translate2",
            "curate",
            "curate2",
            "curate3",
            "curate4",
            "recover",
            "biblio",
        }
        # Lanes reach the real drain flows by name from the global
        # registry — they are not forked into v2.
        assert {ln.flow for ln in pool.lanes} <= set(pool.flow_registry)
        assert pool.inputs["working_directory"] == "/tmp/wd"
    assert pool.stopping, "the pool must be stopped on the way out"
    assert fx.capacity_stopped


@pytest.mark.asyncio
async def test_the_mission_still_runs_when_the_pool_cannot_start(monkeypatch):
    """The controller alone is a working pipeline, just a slower one. A
    scheduler that fails to start must never take the mission with it."""
    import agent.scheduler.lifecycle as lc

    def boom(*a, **k):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr("agent.loop._load_flows", boom)
    async with worker_pool_for(_Fx("scraper_v2"), flows_dir="flows") as pool:
        assert pool is None  # degraded, not raised


@pytest.mark.asyncio
async def test_the_deadline_is_handed_to_the_pool():
    fx = _Fx("scraper_v2")
    async with worker_pool_for(fx, flows_dir="flows", max_wall_clock_s=123) as pool:
        assert pool.deadline is not None


def test_only_v2_is_pooled():
    assert POOLED_FLOW_SETS == {"scraper_v2"}


@pytest.mark.asyncio
async def test_an_exception_from_the_mission_passes_through_untouched():
    """CAUGHT LIVE on the first v2 run. The startup guard originally
    wrapped the `yield`, so it caught whatever the mission body raised —
    including the budget-park RuntimeError that is the NORMAL end of a
    bounded run — logged it as a startup failure, and then yielded a
    second time. That breaks the context-manager protocol and replaces
    the real error with 'generator didn't stop after athrow()', which is
    how a clean cycle-limit park came back looking like a crash.
    """
    fx = _Fx("scraper_v2")
    with pytest.raises(RuntimeError, match="Mission parked as paused"):
        async with worker_pool_for(fx, flows_dir="flows"):
            raise RuntimeError("Agent completed 3 cycles. Mission parked as paused")
    # And the pool still shut down on the way out.
    assert fx.capacity_stopped


@pytest.mark.asyncio
async def test_the_pool_stops_even_when_the_mission_raises():
    fx = _Fx("scraper_v2")
    seen = {}
    with pytest.raises(ValueError):
        async with worker_pool_for(fx, flows_dir="flows") as pool:
            seen["pool"] = pool
            raise ValueError("boom")
    assert seen["pool"] is not None and seen["pool"].stopping
