"""Latch-aware decode-failure recovery: llama_decode -3/-2 marks the instance,
release/acquire heals it via a targeted context refresh, health counters
surface. House style: real backend, fakes, asyncio.run."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from inference.backends.llama_cpp_backend import (
    LlamaCppBackend,
    _install_ggml_log_forwarding,
)


class FakeState:
    n_tokens = 10
    llama_state_size = 1024


class FakeLlama:
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


def make_backend() -> LlamaCppBackend:
    config = SimpleNamespace(
        resources=SimpleNamespace(
            cpu_threads=1,
            max_concurrent_requests=1,
            jit_concurrency_limit=None,
            scale_wait_timeout=0.5,
            instance_idle_ttl=0.1,
        ),
        app=SimpleNamespace(backend_timeout=0.2),
    )
    b = LlamaCppBackend(config)
    b._check_is_hybrid = lambda inst: False
    b._create_primary_instance = lambda: FakeLlama()
    b._create_shared_instance = lambda primary, n_ctx_override=None: FakeLlama()

    def _warm_up(inst, idx=0, persona=None):
        inst._persona = persona or "default"
        if b._static_state is None:
            b._static_state = FakeState()

    b._warm_up_instance = _warm_up
    b._close_shared_context = lambda inst: None
    return b


def test_mark_decode_failure_flags_and_counts():
    b = make_backend()
    inst = FakeLlama()
    assert b._h_decode_failures == 0
    b._mark_decode_failure(inst, RuntimeError("llama_decode failed (code -3)"))
    assert inst._needs_context_refresh is True
    assert b._h_decode_failures == 1


def test_release_heals_flagged_instance():
    async def main():
        b = make_backend()
        await b.initialize()
        refreshed = []
        b._refresh_context_sync = lambda inst: refreshed.append(inst)

        inst = await b.acquire_instance()
        b._mark_decode_failure(inst, RuntimeError("code -3"))
        await b.release_instance(inst)

        assert refreshed == [inst], "release must run the targeted refresh"
        assert inst._needs_context_refresh is False
        assert b._h_latch_heals == 1
        h = b.get_health_status()
        assert h["decode_failures"] == 1 and h["latch_heals"] == 1

        # The healed instance serves again normally.
        inst2 = await b.acquire_instance()
        assert inst2 is inst
        await b.release_instance(inst2)

    asyncio.run(main())


def test_acquire_belt_heals_if_release_missed():
    async def main():
        b = make_backend()
        await b.initialize()
        refreshed = []
        b._refresh_context_sync = lambda inst: refreshed.append(inst)

        inst = await b.acquire_instance()
        await b.release_instance(inst)  # released healthy
        inst._needs_context_refresh = True  # poisoned while idle (edge)
        got = await b.acquire_instance()
        assert got is inst and refreshed == [inst]
        assert got._needs_context_refresh is False

    asyncio.run(main())


def test_failed_heal_keeps_flag_and_does_not_crash():
    async def main():
        b = make_backend()
        await b.initialize()

        def _boom(inst):
            raise RuntimeError("refresh exploded")

        b._refresh_context_sync = _boom
        inst = await b.acquire_instance()
        b._mark_decode_failure(inst, RuntimeError("code -3"))
        await b.release_instance(inst)  # must not raise
        assert inst._needs_context_refresh is True  # flag survives for retry
        assert b._h_latch_heals == 0

    asyncio.run(main())


def test_ggml_log_forwarding_installs_once(monkeypatch):
    import inference.backends.llama_cpp_backend as mod

    calls = []

    class FakeLlamaCpp:
        @staticmethod
        def ggml_log_callback(fn):
            return fn

        @staticmethod
        def llama_log_set(cb, ud):
            calls.append(cb)

    monkeypatch.setattr(mod, "_ggml_log_cb", None)
    monkeypatch.setitem(__import__("sys").modules, "llama_cpp", FakeLlamaCpp)
    _install_ggml_log_forwarding()
    _install_ggml_log_forwarding()  # idempotent
    assert len(calls) == 1
