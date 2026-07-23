"""Pool scaling: generation guard, drain-abort, backoff, LRU reaping.

Exercises the real ``LlamaCppBackend`` with stubbed instance
creation/warm-up and plain fake instances — llama_cpp is never imported
(its import is lazy), so the asyncio scaling logic runs fast and
model-free. House style matches the existing suite: sync test functions
driving ``asyncio.run``.

The invariant under test: no GPU work (generation, eval, load_state,
save_state — every generation_guard span) may run concurrently with a
JIT scaling operation's spawn/warm-up/teardown.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from inference.backends.llama_cpp_backend import LlamaCppBackend
from inference.repetition import DegenerateGenerationError

# ── fakes ─────────────────────────────────────────────────────────────


class FakeState:
    n_tokens = 10
    llama_state_size = 1024


class FakeLlama:
    """Stands in for a Llama instance — every GPU op is a no-op."""

    def __init__(self):
        self._ctx = object()
        self._batch = object()

    def load_state(self, state):
        pass

    def save_state(self):
        return FakeState()

    def reset(self):
        pass

    def close(self):
        pass


def make_backend(
    jit_limit=None,
    max_concurrent=2,
    backend_timeout=0.2,
    scale_wait_timeout=0.5,
    instance_idle_ttl=0.1,
) -> LlamaCppBackend:
    """Real backend, stub creation/warm-up, fake instances, tiny timeouts."""
    config = SimpleNamespace(
        resources=SimpleNamespace(
            cpu_threads=1,
            max_concurrent_requests=max_concurrent,
            jit_concurrency_limit=jit_limit,
            scale_wait_timeout=scale_wait_timeout,
            instance_idle_ttl=instance_idle_ttl,
        ),
        app=SimpleNamespace(backend_timeout=backend_timeout),
    )
    backend = LlamaCppBackend(config)
    backend._check_is_hybrid = lambda inst: False
    backend._create_primary_instance = lambda: FakeLlama()

    backend.spawn_calls = 0

    def _create_shared(primary, n_ctx_override=None):
        backend.spawn_calls += 1
        return FakeLlama()

    backend._create_shared_instance = _create_shared

    def _warm_up(inst, idx=0, persona=None):
        if backend._static_state is None:
            backend._static_state = FakeState()

    backend._warm_up_instance = _warm_up
    backend.rewarm_calls = 0

    def _rewarm():
        backend.rewarm_calls += 1

    backend._rewarm_after_teardown = _rewarm
    backend._close_shared_context = lambda inst: None
    # Neutralize the cooldown so reap tests don't have to wait it out.
    backend._last_scale_up_time = time.monotonic() - 10_000
    return backend


def install_stream(backend, chunks=("a", "b"), error_after=None):
    """Patch generate_stream_sync with a canned sync generator."""

    def fake_stream(instance, prompt_tokens, max_tokens, temperature, **kwargs):
        for i, chunk in enumerate(chunks):
            if error_after is not None and i == error_after:
                raise DegenerateGenerationError("test-degenerate")
            yield chunk

    backend.generate_stream_sync = fake_stream


async def teardown(backend):
    await backend._cancel_scaler_task()


async def consume_first_then_hold(backend, proceed: asyncio.Event):
    """Start a stream, consume one chunk, hold mid-stream until told.

    Between chunks the generation_guard stays held — the generation is
    'active' from the pool's perspective, exactly like a slow client.
    """
    agen = backend.generate_stream_async(FakeLlama(), [1, 2, 3], 16, 0.4)
    first = await agen.__anext__()
    assert first == "a"

    async def finish():
        await proceed.wait()
        async for _ in agen:
            pass

    return asyncio.create_task(finish()), agen


# ── eager mode (production path) ──────────────────────────────────────


def test_eager_init_full_pool_and_identical_lifecycle():
    async def main():
        backend = make_backend(jit_limit=None, max_concurrent=2)
        await backend.initialize()
        assert len(backend._all_instances) == 2
        assert backend._pool_queue.qsize() == 2
        assert backend._scaler_task is None  # no scaler in eager mode
        assert backend._ready_event.is_set()
        health = backend.get_health_status()
        assert health["status"] == "ok"
        assert health["jit_enabled"] is False
        await teardown(backend)

    asyncio.run(main())


def test_eager_acquire_release_updates_checked_out_not_generations():
    async def main():
        backend = make_backend(jit_limit=None, max_concurrent=2)
        await backend.initialize()
        inst = await backend.acquire_instance()
        assert backend._checked_out == 1
        assert backend._active_generations == 0  # checkout is not GPU work
        await backend.release_instance(inst)
        assert backend._checked_out == 0
        await teardown(backend)

    asyncio.run(main())


# ── generation counter lifecycle ──────────────────────────────────────


def test_generation_counter_balances_on_stream_completion():
    async def main():
        backend = make_backend(jit_limit=None, max_concurrent=1)
        await backend.initialize()
        install_stream(backend)
        proceed = asyncio.Event()
        finisher, _ = await consume_first_then_hold(backend, proceed)
        assert backend._active_generations == 1
        assert not backend._drain_event.is_set()
        proceed.set()
        await finisher
        assert backend._active_generations == 0
        assert backend._drain_event.is_set()
        await teardown(backend)

    asyncio.run(main())


def test_generation_counter_balances_on_stream_abandon():
    async def main():
        backend = make_backend(jit_limit=None, max_concurrent=1)
        await backend.initialize()
        install_stream(backend)
        agen = backend.generate_stream_async(FakeLlama(), [1], 16, 0.4)
        await agen.__anext__()
        assert backend._active_generations == 1
        await agen.aclose()  # client disconnects mid-stream
        assert backend._active_generations == 0
        assert backend._drain_event.is_set()
        await teardown(backend)

    asyncio.run(main())


def test_generation_counter_balances_on_generation_error():
    async def main():
        backend = make_backend(jit_limit=None, max_concurrent=1)
        await backend.initialize()
        install_stream(backend, chunks=("a", "b"), error_after=1)
        agen = backend.generate_stream_async(FakeLlama(), [1], 16, 0.4)
        await agen.__anext__()
        with pytest.raises(DegenerateGenerationError):
            async for _ in agen:
                pass
        assert backend._active_generations == 0
        assert backend._drain_event.is_set()
        await teardown(backend)

    asyncio.run(main())


# ── scaling vs generations ────────────────────────────────────────────


def test_pinned_idle_checkout_does_not_block_scale_up():
    """DEFECT-3a regression: a session pinned between turns (checkout
    held, no generation running) must not stall the drain — scale-up
    proceeds immediately."""

    async def main():
        backend = make_backend(jit_limit=3, max_concurrent=1)
        await backend.initialize()
        pinned = await backend.acquire_instance()  # session pin, idle
        assert backend._checked_out == 1

        started = time.perf_counter()
        second = await backend.acquire_instance()  # queue empty → scale-up
        elapsed = time.perf_counter() - started

        assert len(backend._all_instances) == 3
        assert second is not None
        # Drain returned instantly — nowhere near the 0.2s drain timeout.
        assert elapsed < 0.15
        await backend.release_instance(pinned)
        await backend.release_instance(second)
        await teardown(backend)

    asyncio.run(main())


def test_scale_up_aborts_on_drain_timeout_without_spawning():
    """DEFECT-1 regression: an active generation that outlives the drain
    budget must ABORT the scale-up — no contexts spawned concurrently
    with GPU work — and set the retry backoff."""

    async def main():
        backend = make_backend(jit_limit=3, max_concurrent=1, backend_timeout=0.05)
        await backend.initialize()
        install_stream(backend)
        # Empty the queue so the scale-up's "instances became available"
        # double-check doesn't short-circuit before the drain.
        pinned = await backend.acquire_instance()
        proceed = asyncio.Event()
        finisher, _ = await consume_first_then_hold(backend, proceed)
        assert backend._active_generations == 1

        await backend._jit_batch_scale_up()

        assert backend.spawn_calls == 0  # nothing spawned under load
        assert len(backend._all_instances) == 1
        assert backend._scaling_gate.is_set()  # gate reopened via finally
        assert backend._scale_up_backoff_until > time.monotonic()

        proceed.set()
        await finisher
        await backend.release_instance(pinned)
        await teardown(backend)

    asyncio.run(main())


def test_scale_up_backoff_suppresses_retrigger():
    async def main():
        backend = make_backend(jit_limit=3, max_concurrent=1, backend_timeout=0.05)
        await backend.initialize()
        backend._scale_up_backoff_until = time.monotonic() + 60

        pinned = await backend.acquire_instance()  # drains queue
        # Next acquire: backoff suppresses scale-up; falls to slow path
        # and times out on the empty queue (backend_timeout 0.05s).
        with pytest.raises(RuntimeError, match="busy"):
            await backend.acquire_instance()
        assert backend.spawn_calls == 0
        await backend.release_instance(pinned)
        await teardown(backend)

    asyncio.run(main())


def test_scaling_gate_blocks_new_generation_until_open():
    async def main():
        backend = make_backend(jit_limit=None, max_concurrent=1)
        await backend.initialize()
        install_stream(backend)
        backend._scaling_gate.clear()

        chunks = []

        async def consume():
            async for c in backend.generate_stream_async(FakeLlama(), [1], 16, 0.4):
                chunks.append(c)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        assert chunks == []  # blocked at the gate
        assert backend._active_generations == 0

        backend._scaling_gate.set()
        await task
        assert chunks == ["a", "b"]
        await teardown(backend)

    asyncio.run(main())


def test_gate_generation_timeout_raises_clear_error():
    async def main():
        backend = make_backend(
            jit_limit=None, max_concurrent=1, scale_wait_timeout=0.05
        )
        await backend.initialize()
        install_stream(backend)
        backend._scaling_gate.clear()
        agen = backend.generate_stream_async(FakeLlama(), [1], 16, 0.4)
        with pytest.raises(RuntimeError, match="scale_wait_timeout"):
            await agen.__anext__()
        assert backend._active_generations == 0
        backend._scaling_gate.set()
        await teardown(backend)

    asyncio.run(main())


def test_generation_guard_reentrancy_no_deadlock_and_balanced_count():
    """A session turn holds the guard across its whole body; the generate
    wrapper re-enters inside it via ``_nested_guard=True``. The nested
    entry must not wait on the scaling gate (a scaler draining behind a
    closed gate would deadlock against us) and the counter must balance."""

    async def main():
        backend = make_backend(jit_limit=None, max_concurrent=1)
        await backend.initialize()
        install_stream(backend)

        async with backend.generation_guard():
            assert backend._active_generations == 1
            # A scaler closes the gate and starts draining — it is now
            # waiting for US. Our nested generate must proceed regardless
            # (this is exactly session_turn mid-turn).
            backend._scaling_gate.clear()
            chunks = []
            async for c in backend.generate_stream_async(
                FakeLlama(), [1], 16, 0.4, _nested_guard=True
            ):
                chunks.append(c)
                assert backend._active_generations == 2
            assert chunks == ["a", "b"]
            assert backend._active_generations == 1
            backend._scaling_gate.set()

        assert backend._active_generations == 0
        assert backend._drain_event.is_set()
        await teardown(backend)

    asyncio.run(main())


def test_concurrent_acquires_single_batch_spawn():
    async def main():
        backend = make_backend(jit_limit=3, max_concurrent=1)
        await backend.initialize()
        pinned = await backend.acquire_instance()  # empty the queue

        a, b = await asyncio.gather(
            backend.acquire_instance(), backend.acquire_instance()
        )
        # Batch spawns all remaining slots exactly once (TOCTOU
        # double-check stops the second caller from re-spawning).
        assert backend.spawn_calls == 2  # limit 3 − existing 1
        assert len(backend._all_instances) == 3
        assert a is not None and b is not None
        for inst in (pinned, a, b):
            await backend.release_instance(inst)
        await teardown(backend)

    asyncio.run(main())


# ── LRU reaping ───────────────────────────────────────────────────────


async def _scaled_up_backend():
    backend = make_backend(jit_limit=3, max_concurrent=1, instance_idle_ttl=0.05)
    await backend.initialize()
    pinned = await backend.acquire_instance()
    await backend._jit_batch_scale_up()
    await backend.release_instance(pinned)
    assert len(backend._all_instances) == 3
    backend._last_scale_up_time = time.monotonic() - 10_000  # bypass cooldown
    return backend


def test_scaler_tick_reaps_at_most_one_lru_and_respects_ttl():
    async def main():
        backend = await _scaled_up_backend()
        metas = backend._instance_meta
        non_primary = [
            i for i in backend._all_instances if i is not backend._primary_instance
        ]
        now = time.monotonic()
        # Staggered idle times: lru is far past the ttl, fresh is recent.
        lru, fresh = non_primary
        metas[id(lru)].last_released_at = now - 100
        metas[id(fresh)].last_released_at = now  # idle < ttl → protected

        await backend._scaler_tick()
        assert len(backend._all_instances) == 2
        assert lru not in backend._all_instances  # LRU picked
        assert fresh in backend._all_instances
        assert backend.rewarm_calls == 0  # pool not back to 1 yet

        await backend._scaler_tick()  # fresh still inside its ttl
        assert len(backend._all_instances) == 2

        await teardown(backend)

    asyncio.run(main())


def test_rewarm_only_when_pool_returns_to_one_and_health_fields_stable():
    async def main():
        backend = await _scaled_up_backend()
        now = time.monotonic()
        for inst in backend._all_instances:
            if inst is not backend._primary_instance:
                backend._instance_meta[id(inst)].last_released_at = now - 100

        await backend._scaler_tick()
        assert len(backend._all_instances) == 2
        assert backend.rewarm_calls == 0
        await backend._scaler_tick()
        assert len(backend._all_instances) == 1
        assert backend.rewarm_calls == 1  # exactly once, at pool=1

        health = backend.get_health_status()
        for key in (
            "status",
            "pool_size",
            "available_instances",
            "active_instances",
            "in_flight",
            "jit_enabled",
            "jit_limit",
        ):
            assert key in health, f"missing health field {key}"
        assert health["checked_out"] == 0
        assert len(health["instances"]) == 1
        assert health["instances"][0]["is_primary"] is True

        await teardown(backend)

    asyncio.run(main())


# ── resident-seq fallback: un-fragmentation gate ──────────────────────


class ShiftyCtx:
    def __init__(self, can_shift: bool):
        self._can = can_shift

    def memory_can_shift(self):
        return self._can


def test_resident_fallback_unfragments_context():
    # A model that cannot shift (iSWA without swa_full — the OLMo 2026-07-22
    # incident) must not keep the resident seq-band context: without
    # kv_unified, llama.cpp splits n_ctx per sequence, so n_seq_max=12 left
    # the fallback with a twelfth of the window (5,632 cells — smaller than
    # the first design prompt). The gate must force single-seq and rebuild.
    async def main():
        backend = make_backend(jit_limit=None, max_concurrent=1)
        primary = FakeLlama()
        primary._ctx = ShiftyCtx(False)
        primary.context_params = SimpleNamespace(n_seq_max=12)
        backend._create_primary_instance = lambda: primary
        backend._resident_requested = True
        rebuilds = []
        backend._refresh_context_sync = lambda inst: rebuilds.append(
            inst.context_params.n_seq_max
        )
        await backend.initialize()
        assert backend._resident_active is False
        assert rebuilds == [1]  # rebuilt after forcing single-seq
        await teardown(backend)

    asyncio.run(main())


def test_resident_active_keeps_band():
    # Shift-capable model: resident stays active, no rebuild happens.
    async def main():
        backend = make_backend(jit_limit=None, max_concurrent=1)
        primary = FakeLlama()
        primary._ctx = ShiftyCtx(True)
        primary.context_params = SimpleNamespace(n_seq_max=12)
        backend._create_primary_instance = lambda: primary
        backend._resident_requested = True
        rebuilds = []
        backend._refresh_context_sync = lambda inst: rebuilds.append(1)
        await backend.initialize()
        assert backend._resident_active is True
        assert rebuilds == []
        assert primary.context_params.n_seq_max == 12
        await teardown(backend)

    asyncio.run(main())


# ── abandoned-stream retirement (the copy_logits race) ────────────────
#
# Live failure (qwen3-next, July 22): the agent watchdog cancelled a 33k-
# token runaway; the sync generator's deferred GeneratorExit fired 1.1s
# into the NEXT request on the same instance and tore down shared sampling
# state under it → copy_logits(None) → no inference progress for 23 min.
# The fix: generate_stream_async deterministically closes the sync
# generator before the guard drops (finally, shielded), and retires any
# still-registered prior stream before a new stream's first token.


def test_close_stream_gen_suspended():
    closed = []

    def gen_fn():
        try:
            yield "a"
            yield "b"
        finally:
            closed.append(True)

    g = gen_fn()
    assert next(g) == "a"  # suspended at a yield
    assert LlamaCppBackend._close_stream_gen(g) is True
    assert closed == [True]


def test_close_stream_gen_waits_out_inflight_next():
    import threading

    release = threading.Event()
    closed = []

    def gen_fn():
        try:
            yield "a"
            release.wait(5)  # simulates a slow llama_decode inside next()
            yield "b"
        finally:
            closed.append(True)

    g = gen_fn()
    assert next(g) == "a"
    t = threading.Thread(target=lambda: next(g))
    t.start()
    time.sleep(0.05)  # the worker is now blocked inside next()
    threading.Timer(0.2, release.set).start()
    assert LlamaCppBackend._close_stream_gen(g, timeout_s=5) is True
    assert closed == [True]
    t.join(5)


def test_close_stream_gen_timeout_returns_false():
    import threading

    release = threading.Event()

    def gen_fn():
        yield "a"
        release.wait(10)
        yield "b"

    g = gen_fn()
    next(g)
    t = threading.Thread(target=lambda: next(g))
    t.start()
    time.sleep(0.05)
    assert LlamaCppBackend._close_stream_gen(g, timeout_s=0.3) is False
    release.set()  # let the thread finish; retire the generator for real
    t.join(5)
    LlamaCppBackend._close_stream_gen(g, timeout_s=1)


def test_abandoned_stream_closed_on_aclose():
    # Consumer abandons mid-stream (the watchdog-cancel path drives
    # aclose): the sync generator's finally must have run and the
    # registration cleared by the time aclose returns.
    async def main():
        backend = make_backend()
        await backend.initialize()
        state = {"closed": False}

        def fake_stream(instance, prompt_tokens, max_tokens, temperature, **kwargs):
            try:
                for i in range(1000):
                    yield f"c{i}"
            finally:
                state["closed"] = True

        backend.generate_stream_sync = fake_stream
        inst = FakeLlama()
        agen = backend.generate_stream_async(inst, [1], 16, 0.4)
        assert await agen.__anext__() == "c0"
        assert inst._active_stream_gen is not None
        await agen.aclose()
        assert state["closed"] is True
        assert inst._active_stream_gen is None
        await teardown(backend)

    asyncio.run(main())


def test_cancelled_consumer_closes_stream():
    # End-to-end task cancellation: after the cancelled task settles (plus
    # the loop draining asyncgen finalization), the stream must be retired.
    async def main():
        backend = make_backend()
        await backend.initialize()
        state = {"closed": False}

        def fake_stream(instance, prompt_tokens, max_tokens, temperature, **kwargs):
            try:
                for i in range(1000):
                    yield f"c{i}"
            finally:
                state["closed"] = True

        backend.generate_stream_sync = fake_stream
        inst = FakeLlama()
        started = asyncio.Event()

        async def consume():
            async for _ in backend.generate_stream_async(inst, [1], 16, 0.4):
                started.set()
                await asyncio.sleep(3600)  # slow client, cancelled mid-stream

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(40):  # asyncgen finalization is scheduled, not inline
            if state["closed"]:
                break
            await asyncio.sleep(0.05)
        assert state["closed"] is True
        assert inst._active_stream_gen is None
        await teardown(backend)

    asyncio.run(main())


def test_prior_abandoned_stream_retired_before_reuse():
    # Lazy-finalization escape hatch: a prior stream still registered on
    # the instance (its GC close never ran) is closed by the entry check
    # before the new stream produces its first token.
    async def main():
        backend = make_backend()
        await backend.initialize()
        closed = []

        def orphan_fn():
            try:
                yield "x"
                yield "y"
            finally:
                closed.append(True)

        orphan = orphan_fn()
        next(orphan)  # suspended mid-stream, GeneratorExit pending forever
        inst = FakeLlama()
        inst._active_stream_gen = orphan
        install_stream(backend, chunks=("a", "b"))
        out = []
        async for c in backend.generate_stream_async(inst, [1], 16, 0.4):
            out.append(c)
        assert out == ["a", "b"]
        assert closed == [True]  # retired before the new stream ran
        assert inst._active_stream_gen is None
        await teardown(backend)

    asyncio.run(main())


# ── adaptive head-swap beyond harmony ─────────────────────────────────


def _backend_with_family(family, levels, *, head_swap=True, resident=True):
    from unittest.mock import patch
    from types import SimpleNamespace

    backend = make_backend(jit_limit=None, max_concurrent=1)
    backend.config.model = SimpleNamespace(
        family=family,
        reasoning_head_swap=head_swap,
        thinking_mode="medium",
        resident_seq_cache=resident,
    )
    spec = SimpleNamespace(reasoning=SimpleNamespace(levels=levels))
    with patch("formats.registry.get_renderer", return_value=SimpleNamespace(s=spec)):
        LlamaCppBackend.__init__(backend, backend.config)
    return backend


def test_head_swap_enabled_for_family_declaring_levels():
    """A non-harmony family qualifies by declaring reasoning.levels — that map
    is what makes its levels renderable into the cached system head."""
    b = _backend_with_family(
        "gemma", {"low": "  ", "medium": "  ", "high": "<|think|>"}
    )
    assert b._reasoning_head_swap is True
    # BIMODAL collapse: low renders identically to the medium default, so only
    # ONE extra head is pinned, not two.
    assert b._reasoning_pin_levels == ["high"]


def test_head_swap_still_enabled_for_harmony_without_map():
    b = _backend_with_family("harmony", {})
    assert b._reasoning_head_swap is True
    assert b._reasoning_pin_levels == ["low", "high"]


def test_head_swap_disabled_for_family_without_map():
    b = _backend_with_family("chatml", {})
    assert b._reasoning_head_swap is False
