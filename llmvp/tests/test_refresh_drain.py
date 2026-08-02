"""Drain-refresh state machine (context auto-refresh under load).

Pins the 2026-07-16 machinery: when the timed refresh cap trips while the
pool is BUSY, the admission gate closes, in-flight work gets the drain
window, stragglers force-clear (sessions via the expirer hook, streams via
engine eviction), and the gate reopens on every exit path. Style follows
test_pool_scaling.py: the REAL backend with stubbed instances + tiny
timeouts, no model.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.backends.llama_cpp_backend import LlamaCppBackend  # noqa: E402


def _backend(drain_s=0.5) -> LlamaCppBackend:
    config = SimpleNamespace(
        resources=SimpleNamespace(
            cpu_threads=1,
            max_concurrent_requests=2,
            jit_concurrency_limit=None,
            scale_wait_timeout=0.5,
            instance_idle_ttl=0.1,
        ),
        app=SimpleNamespace(backend_timeout=0.2),
        model=SimpleNamespace(
            context_refresh_interval=75,
            context_refresh_seconds=1800,
            context_refresh_drain_s=drain_s,
        ),
    )
    be = LlamaCppBackend(config)
    be._refresh_drain_s = drain_s
    be._refresh_settle_s = 0.2  # keep the give-up path fast under test
    return be


def test_drain_returns_true_when_pool_clears_in_window():
    be = _backend(drain_s=2.0)
    be._checked_out = 1
    be._active_generations = 1

    async def scenario():
        async def release_soon():
            await asyncio.sleep(0.15)
            be._checked_out = 0
            be._active_generations = 0

        asyncio.create_task(release_soon())
        return await be._drain_for_refresh("test")

    assert asyncio.run(scenario()) is True
    # the drain clears the gate; the CALLER's finally reopens it
    assert not be._refresh_admission_gate.is_set()


def test_drain_deadline_force_clears_sessions_and_streams():
    be = _backend(drain_s=0.3)
    be._checked_out = 1
    be._active_generations = 1
    expired = []

    async def expirer(reason):
        expired.append(reason)
        be._checked_out = 0
        return 1

    be._session_expirer = expirer

    evicted = []

    class FakeEngine:
        def evict_all_streams(self, reason):
            evicted.append(reason)
            be._active_generations = 0
            return 1

    be._engine = FakeEngine()
    assert asyncio.run(be._drain_for_refresh("test")) is True
    assert len(expired) == 1 and "drain deadline" in expired[0]
    assert len(evicted) == 1


def test_proactive_drain_defers_instead_of_forcing():
    """POLITE REFRESH (operator, 2026-08-03): a proactive reason at the
    finish deadline with work still live must DEFER — no session expiry, no
    stream eviction. The bartowski laguna run made the cost of forcing
    concrete: a 52k-token coherent batch generation evicted 2/3 through by
    the 30-min cap, with the retry doomed to the same wall."""
    be = _backend(drain_s=0.2)
    be._checked_out = 1
    be._active_generations = 1
    expired, evicted = [], []

    async def expirer(reason):  # pragma: no cover — must NOT fire
        expired.append(reason)
        return 1

    be._session_expirer = expirer

    class FakeEngine:
        def evict_all_streams(self, reason):  # pragma: no cover — must NOT fire
            evicted.append(reason)
            return 1

    be._engine = FakeEngine()
    assert asyncio.run(be._drain_for_refresh("proactive-timed-drain")) is False
    assert expired == [] and evicted == []
    # recovery reasons still force (the existing force test covers "test",
    # which does not carry the proactive prefix)


def test_drain_gives_up_when_pool_never_clears():
    be = _backend(drain_s=0.2)
    be._checked_out = 1  # nothing ever releases; no expirer registered
    assert asyncio.run(be._drain_for_refresh("test")) is False


def test_refresh_decision_timed_drain_fires_under_load():
    import time as _time

    be = _backend(drain_s=300.0)
    be._decode_mode = "batched"
    be._refresh_seconds = 0  # cap already elapsed
    be._h_requests_since_refresh = 5
    be._last_refresh_monotonic = _time.monotonic() - 10
    be._checked_out = 2  # busy — the old code deferred here forever
    assert be._refresh_decision() == "proactive-timed-drain"
    # drain disabled → legacy defer
    be._refresh_drain_s = 0.0
    assert be._refresh_decision() is None


def test_admission_gate_blocks_then_releases_generation():
    be = _backend(drain_s=0.5)
    be._ready_event.set()
    be._refresh_admission_gate.clear()

    async def scenario():
        results = []

        async def gated():
            await be._gate_generation()
            results.append("through")

        t = asyncio.create_task(gated())
        await asyncio.sleep(0.1)
        assert results == []  # still gated
        be._refresh_admission_gate.set()
        await asyncio.wait_for(t, timeout=1.0)
        return results

    assert asyncio.run(scenario()) == ["through"]
