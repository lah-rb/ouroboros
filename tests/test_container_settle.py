"""Container-aware settle: the backstop must DEFER while the container is working.

For tb tasks the PTY shell is a `docker exec` relay, so the host-pgrp CPU probe is
blind to the container's real work — a silent `--quiet` download looks dead and the
no-output backstop fires prematurely. `_poll_settle` now probes container activity
(`_container_activity` → (cpu_usec, io_net_bytes)) and defers the backstop while it's
active. These drive `_poll_settle` with a monkeypatched probe (no real container) and
assert: real compute OR real transfer ⇒ deferred; idle (incl. idle-daemon *noise*
below the per-signal thresholds) / probe-unavailable / local ⇒ backstop at the normal
deadline. The idle-noise case is the regression guard for the original bug — summing
cpu-µs (which grows by thousands even when idle) into one threshold read idle as busy.
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


def _ramp(per_sample_cpu, per_sample_bytes):
    """A probe that grows cpu/bytes by a fixed amount each call."""
    state = {"cpu": 0.0, "bytes": 0.0}

    def fn(name):
        state["cpu"] += per_sample_cpu
        state["bytes"] += per_sample_bytes
        return (state["cpu"], state["bytes"])

    return fn


def test_active_compute_defers_backstop(monkeypatch):
    # cpu climbs hard (real compute), bytes flat → busy via the cpu threshold.
    res, elapsed = _run_poll(monkeypatch, _ramp(P.CONTAINER_CPU_EPS_USEC * 2, 0.0))
    assert res["timed_out"] is True
    assert elapsed > 0.6, f"compute should defer past 200ms, got {elapsed:.2f}s"


def test_active_transfer_defers_backstop(monkeypatch):
    # bytes climb hard (a download), cpu flat → busy via the bytes threshold.
    res, elapsed = _run_poll(monkeypatch, _ramp(0.0, P.CONTAINER_BYTES_EPS * 2))
    assert res["timed_out"] is True
    assert elapsed > 0.6, f"transfer should defer past 200ms, got {elapsed:.2f}s"


def test_idle_daemon_noise_is_not_busy(monkeypatch):
    # THE REGRESSION GUARD: an idle container still ticks a little cpu + a few
    # bytes of keepalive/log chatter — both BELOW their thresholds. Must NOT defer
    # (the old summed-counter probe read this as busy and never settled).
    res, elapsed = _run_poll(
        monkeypatch, _ramp(P.CONTAINER_CPU_EPS_USEC // 10, P.CONTAINER_BYTES_EPS // 10)
    )
    assert res["timed_out"] is True
    assert elapsed < 0.55, f"idle noise must not defer, got {elapsed:.2f}s"


def test_flat_container_backstops_at_deadline(monkeypatch):
    res, elapsed = _run_poll(monkeypatch, lambda name: (5.0, 5.0))
    assert res["timed_out"] is True
    assert elapsed < 0.55, f"flat container should backstop ~deadline, got {elapsed:.2f}s"


def test_probe_unavailable_falls_back(monkeypatch):
    res, elapsed = _run_poll(monkeypatch, lambda name: None)
    assert res["timed_out"] is True
    assert elapsed < 0.55, f"None probe should not defer, got {elapsed:.2f}s"


def test_local_session_never_probes(monkeypatch):
    calls = {"n": 0}

    def spy(name):
        calls["n"] += 1
        return (calls["n"] * 1e6, calls["n"] * 1e6)

    res, elapsed = _run_poll(monkeypatch, spy, container_name="")
    assert res["timed_out"] is True
    assert calls["n"] == 0, "local session must not invoke the container probe"
    assert elapsed < 0.55
