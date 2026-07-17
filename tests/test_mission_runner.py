"""The shared isolated mission-run harness: outcome classification."""

import asyncio
from unittest import mock

import pytest

from agent.mission_runner import MissionRunOutcome, run_mission_isolated


class FakeEffects:
    def __init__(self):
        self.drained = []

    async def end_open_inference_sessions(self):
        self.drained.append("sessions")

    async def mcp_disconnect_all(self):
        self.drained.append("mcp")


def _run(behavior):
    async def fake_run_agent(**kw):
        return behavior() if callable(behavior) else behavior

    fx = FakeEffects()
    with mock.patch("agent.loop.run_agent", new=fake_run_agent):
        out = run_mission_isolated(
            fx, mission_id="m1", entry_flow="mission_control", max_cycles=1
        )
    return out, fx


def test_normal_return_carries_result_and_drains():
    out, fx = _run("the-result")
    assert out == MissionRunOutcome(result="the-result")
    assert fx.drained == ["sessions", "mcp"]


def test_park_runtimeerror_classified_clean():
    def boom():
        raise RuntimeError(
            "Agent completed 3 cycles. Mission parked as paused — resume later"
        )

    out, fx = _run(boom)
    assert out.parked is True and out.error is None
    assert "parked as paused" in out.park_message
    assert fx.drained == ["sessions", "mcp"]  # drain ran on the park path too


def test_real_error_captured_not_raised():
    def boom():
        raise ValueError("actual bug")

    out, _ = _run(boom)
    assert isinstance(out.error, ValueError) and not out.parked


def test_keyboard_interrupt_reraises():
    def boom():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run(boom)


def test_cancelled_during_drain_is_swallowed():
    class CancellingEffects(FakeEffects):
        async def end_open_inference_sessions(self):
            raise asyncio.CancelledError

    async def fake_run_agent(**kw):
        return "ok"

    fx = CancellingEffects()
    with mock.patch("agent.loop.run_agent", new=fake_run_agent):
        out = run_mission_isolated(fx, mission_id="m1", entry_flow="mission_control")
    assert out.result == "ok"  # drain failure never masks the outcome
