"""Container-aware settle: the backstop must DEFER while the container is working.

For tb tasks the PTY shell is a `docker exec` relay, so the host-pgrp CPU probe is
blind to the container's real work — a silent `--quiet` download looks dead and the
no-output backstop fires prematurely. `_poll_settle` now probes container activity
(`_container_activity`) and defers the backstop while it's active. These tests drive
`_poll_settle` directly with a monkeypatched probe (no real container needed) and
assert: active ⇒ deferred to the ceiling; idle / probe-unavailable ⇒ fires at the
normal deadline (today's behavior); empty container_name ⇒ never probes.
"""

from __future__ import annotations

import asyncio
import os

from mcp_servers.terminal import pty_session as P


def _run_poll(monkeypatch, activity_fn, container_name="c", settle_ms=150, timeout_ms=200):
    monkeypatch.setattr(P, "CONTAINER_SAMPLE_MIN_INTERVAL_S", 0.02)
    monkeypatch.setattr(P, "CONTAINER_HARD_MAX_MS", 1000)  # 1s ceiling for the test
    monkeypatch.setattr(P, "_container_activity", activity_fn)
    mgr = P.PTYSessionManager()
    sess = P.SessionInfo(
        session_id="t",
        pid=os.getpid(),
        master_fd=-1,
        shell_pgid=os.getpid(),
        working_directory=".",
        container_name=container_name,
    )
    loop = asyncio.new_event_loop()
    try:
        t0 = loop.time()
        res = loop.run_until_complete(
            mgr._poll_settle(sess, 0, settle_ms=settle_ms, timeout_ms=timeout_ms)
        )
        return res, loop.time() - t0
    finally:
        loop.close()


def test_active_container_defers_backstop(monkeypatch):
    # A monotonically growing counter = the container is working (a silent
    # download). The no-output backstop must NOT fire at the 200ms deadline; it
    # defers toward the 1s ceiling.
    state = {"n": 0}

    def active(name):
        state["n"] += 1
        return float(state["n"] * 1_000_000)

    res, elapsed = _run_poll(monkeypatch, active)
    assert res["timed_out"] is True
    assert elapsed > 0.6, f"backstop should have deferred past 200ms, got {elapsed:.2f}s"


def test_idle_container_backstops_at_deadline(monkeypatch):
    # Flat counter = idle/hung; the backstop fires at the normal ~200ms deadline.
    res, elapsed = _run_poll(monkeypatch, lambda name: 5.0)
    assert res["timed_out"] is True
    assert elapsed < 0.55, f"idle container should backstop ~deadline, got {elapsed:.2f}s"


def test_probe_unavailable_falls_back(monkeypatch):
    # None (no docker / container gone / parse miss) ⇒ no signal ⇒ today's behavior.
    res, elapsed = _run_poll(monkeypatch, lambda name: None)
    assert res["timed_out"] is True
    assert elapsed < 0.55, f"None probe should not defer, got {elapsed:.2f}s"


def test_local_session_never_probes(monkeypatch):
    # container_name == "" ⇒ the probe is never called; behaves exactly as before.
    calls = {"n": 0}

    def spy(name):
        calls["n"] += 1
        return float(calls["n"] * 1_000_000)

    res, elapsed = _run_poll(monkeypatch, spy, container_name="")
    assert res["timed_out"] is True
    assert calls["n"] == 0, "local session must not invoke the container probe"
    assert elapsed < 0.55
