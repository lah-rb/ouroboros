"""Batched vision under tensor split waits for a seat instead of failing (2026-10-01).

A seat-wait timeout used to end the batched attempt and fall back to the dedicated
vision pool, which tensor split cannot build (one llama context per model), so every
figure read on a saturated server failed. Run v50g lost ~85 % of its figure readings.
"""

from __future__ import annotations

import asyncio
import types

import pytest

import core.inference as inf

BUSY = "All inference instances are busy [vision] — try again later (active=1, limit=8)"


class _Backend:
    def __init__(self, busy_times: int, error: str = BUSY):
        self.calls = 0
        self.busy_times = busy_times
        self.error = error

    async def acquire_instance(self, persona=None):
        assert persona == "vision"
        self.calls += 1
        if self.calls <= self.busy_times:
            raise RuntimeError(self.error)
        return "seat"


def _mcfg(split):
    return types.SimpleNamespace(split_mode=split)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(inf, "_VISION_SEAT_RETRY_PAUSE_S", 0.0)


def test_tensor_split_waits_through_busy_seats():
    be = _Backend(busy_times=3)
    assert asyncio.run(inf._acquire_vision_seat(be, _mcfg("tensor"))) == "seat"
    assert be.calls == 4


def test_other_splits_keep_the_pool_fallback_path():
    """Layer split can build the pool, so one wait then the old fallback."""
    be = _Backend(busy_times=1)
    with pytest.raises(RuntimeError, match="busy"):
        asyncio.run(inf._acquire_vision_seat(be, _mcfg("layer")))
    assert be.calls == 1


def test_a_non_busy_error_is_not_waited_out():
    be = _Backend(busy_times=5, error="CUDA error: out of memory")
    with pytest.raises(RuntimeError, match="CUDA"):
        asyncio.run(inf._acquire_vision_seat(be, _mcfg("tensor")))
    assert be.calls == 1


def test_the_wait_is_capped(monkeypatch):
    monkeypatch.setattr(inf, "_VISION_SEAT_WAIT_S", 0.0)
    be = _Backend(busy_times=100)
    with pytest.raises(RuntimeError, match="busy"):
        asyncio.run(inf._acquire_vision_seat(be, _mcfg("tensor")))
