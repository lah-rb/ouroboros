"""A live generation is not idleness — the TTL monitor must not reap its own turn.

THE INCIDENT (hy3, 2026-08-09). `last_turn_at` only advances when a turn
COMPLETES, so during a session's first turn the idle clock stays frozen at
session start. The TTL monitor woke at exactly TTL, measured the whole
generation as idle time, and ended the session while it was still decoding:

    10:54:26  Session 5d77a2d0c91c41ab started (ttl=600s)
    11:04:26  ⏰ Session expired (TTL=600s, idle=600.0s, turns=0)   <- still decoding
    11:12:03  Generation complete: 13,439 tok, 1056.4s, 14.2 tok/s
    11:12:03  ❌ Session not found (active sessions: [])  x4

The generation finished into a dead session, and the four symbols queued
behind it in a multi-symbol patch each came back "Session not found" — a
5-symbol patch silently became a 1-symbol patch, reported as success.

Any turn longer than its TTL destroyed its own session. At 14 tok/s that is
routine, not exotic.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.session_manager import SessionState


class _Recorder:
    """Minimal stand-in for the manager's collaborators."""

    def __init__(self):
        self.ended: list[str] = []


def _session(ttl: int = 1) -> SessionState:
    return SessionState(instance=object(), ttl=ttl)


def test_a_fresh_session_is_not_in_flight():
    assert _session().in_flight == 0


@pytest.mark.asyncio
async def test_the_guard_marks_and_clears(monkeypatch):
    from core.session_manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    s = _session()
    async with mgr._turn_in_flight(s):
        assert s.in_flight == 1, "a decoding turn must be visible to the monitor"
    assert s.in_flight == 0


@pytest.mark.asyncio
async def test_the_idle_clock_restarts_when_the_turn_ENDS():
    """Not when it starts — otherwise a long turn is born already expired."""
    from core.session_manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    s = _session()
    before = s.last_turn_at
    async with mgr._turn_in_flight(s):
        await asyncio.sleep(0.05)
    assert s.last_turn_at > before


@pytest.mark.asyncio
async def test_a_failing_turn_still_clears_the_flag():
    """Otherwise one raised exception makes the session immortal — the
    opposite bug, and a permanently held pool instance."""
    from core.session_manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    s = _session()
    with pytest.raises(RuntimeError):
        async with mgr._turn_in_flight(s):
            raise RuntimeError("generation blew up")
    assert s.in_flight == 0
    assert s.last_turn_at > 0


@pytest.mark.asyncio
async def test_the_monitor_does_not_reap_a_session_mid_generation():
    """THE REGRESSION PIN. A turn that outlives its TTL must survive."""
    from core.session_manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._sessions = {}
    ended: list[str] = []

    async def _end(sid):
        ended.append(sid)
        mgr._sessions.pop(sid, None)

    mgr.end_session = _end

    s = _session(ttl=1)
    mgr._sessions["sid"] = s

    monitor = asyncio.create_task(mgr._ttl_monitor("sid", 1))
    async with mgr._turn_in_flight(s):
        # Three TTL windows of "the turn is still running".
        await asyncio.sleep(3.2)
        assert ended == [], "the monitor reaped a session that was still decoding"
        assert "sid" in mgr._sessions
    monitor.cancel()


@pytest.mark.asyncio
async def test_the_monitor_still_reaps_a_genuinely_idle_session():
    """The guard must not disable expiry — a client that walks away still
    has to release its pinned instance."""
    from core.session_manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._sessions = {}
    ended: list[str] = []

    async def _end(sid):
        ended.append(sid)
        mgr._sessions.pop(sid, None)

    mgr.end_session = _end

    s = _session(ttl=1)
    s.last_turn_at = time.monotonic() - 10  # idle well past the TTL
    mgr._sessions["sid"] = s

    monitor = asyncio.create_task(mgr._ttl_monitor("sid", 1))
    await asyncio.sleep(1.5)
    assert ended == ["sid"]
    monitor.cancel()


@pytest.mark.asyncio
async def test_expiry_follows_the_turn_rather_than_being_cancelled_by_it():
    """After a long turn ends, the session gets a FULL fresh idle window and
    then expires normally — the guard defers expiry, it does not cancel it."""
    from core.session_manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._sessions = {}
    ended: list[str] = []

    async def _end(sid):
        ended.append(sid)
        mgr._sessions.pop(sid, None)

    mgr.end_session = _end

    s = _session(ttl=1)
    mgr._sessions["sid"] = s
    monitor = asyncio.create_task(mgr._ttl_monitor("sid", 1))

    async with mgr._turn_in_flight(s):
        await asyncio.sleep(2.2)
    assert ended == [], "still alive while decoding"

    await asyncio.sleep(2.5)  # now genuinely idle
    assert ended == ["sid"], "expiry must resume once the turn is done"
    monitor.cancel()
