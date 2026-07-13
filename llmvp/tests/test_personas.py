"""Multi-persona pooling: SOUL-per-slot config, persona-routed acquisition,
per-instance static identity, cross-persona snapshot guard.

House style matches test_pool_scaling: the real ``LlamaCppBackend`` with
stubbed creation/warm-up and fake instances (llama_cpp never imported),
sync test functions driving ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import Config, PersonaConfig
from inference.backends.llama_cpp_backend import LlamaCppBackend


# ── config layer ──────────────────────────────────────────────────────


def _mk_config(personas=None, slot_personas=None, n=2) -> Config:
    return Config.model_validate(
        {
            "app": {"host": "0.0.0.0", "port": 1, "log_level": "info"},
            "model": {
                "name": "m", "family": "chatml", "path": "/nonexistent.gguf",
                "n_ctx": 4096, "n_gpu_layers": 0, "seed": -1, "verbose": False,
            },
            "prompt": {"persona_file": "./knowledge/SOUL.md"},
            "generation": {},
            "knowledge": {"tokens_bin": "./data/m.tokens.bin", "token_limit": 1024},
            "resources": {
                "cpu_threads": 1,
                "max_concurrent_requests": n,
                **({"slot_personas": slot_personas} if slot_personas else {}),
            },
            "logging": {"enabled": False},
            **({"personas": personas} if personas else {}),
        }
    )


def test_absent_personas_is_default_for_all_slots():
    c = _mk_config(n=3)
    assert c.slot_persona_names() == ["default", "default", "default"]
    p = c.resolve_persona(None)
    assert str(p.persona_file).endswith("SOUL.md")
    assert str(p.tokens_bin).endswith("m.tokens.bin")


def test_slot_personas_resolve_and_carry_n_ctx():
    c = _mk_config(
        personas={"user_sim": {"persona_file": "./knowledge/USER_SIM.md",
                               "tokens_bin": "./data/m-user.tokens.bin",
                               "n_ctx": 2048}},
        slot_personas=["default", "user_sim"],
    )
    assert c.slot_persona_names() == ["default", "user_sim"]
    u = c.resolve_persona("user_sim")
    assert isinstance(u, PersonaConfig) and u.n_ctx == 2048


def test_slot_personas_length_mismatch_raises():
    c = _mk_config(
        personas={"user_sim": {"persona_file": "a", "tokens_bin": "b"}},
        slot_personas=["default", "user_sim"], n=3,
    )
    with pytest.raises(ValueError, match="must match"):
        c.slot_persona_names()


def test_unknown_slot_persona_raises():
    c = _mk_config(slot_personas=["default", "ghost"])
    with pytest.raises(KeyError, match="ghost"):
        c.slot_persona_names()


def test_duo_yaml_loads():
    duo = Path(__file__).parent.parent / "configs" / "gpt-oss-120b-a5-duo.yaml"
    from core.config import load_config

    c = load_config(duo)
    assert c.slot_persona_names() == ["default", "user_sim"]
    # n_ctx override is commented out in the yaml (known issue: a smaller
    # slot context dies with llama_decode -3 under swa_full+kv_unified).
    assert c.resolve_persona("user_sim").n_ctx is None


# ── backend: fakes (mirrors test_pool_scaling) ────────────────────────


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


def make_duo_backend() -> LlamaCppBackend:
    config = SimpleNamespace(
        resources=SimpleNamespace(
            cpu_threads=1,
            max_concurrent_requests=2,
            jit_concurrency_limit=None,
            scale_wait_timeout=0.5,
            instance_idle_ttl=0.1,
        ),
        app=SimpleNamespace(backend_timeout=0.2),
        slot_persona_names=lambda: ["default", "user_sim"],
        resolve_persona=lambda name: SimpleNamespace(
            persona_file="x", tokens_bin="y",
            n_ctx=2048 if name == "user_sim" else None,
        ),
    )
    backend = LlamaCppBackend(config)
    backend._check_is_hybrid = lambda inst: False
    backend._create_primary_instance = lambda: FakeLlama()
    backend._create_shared_instance = (
        lambda primary, n_ctx_override=None: FakeLlama()
    )

    def _warm_up(inst, idx=0, persona=None):
        persona = persona or backend._slot_personas[idx]
        inst._persona = persona
        inst._static_tokens = [1, 2, 3]
        inst._static_len = 3
        backend._static_states.setdefault(persona, FakeState())

    backend._warm_up_instance = _warm_up
    backend._close_shared_context = lambda inst: None
    return backend


def test_duo_init_routes_personas_to_slots_and_queues():
    async def main():
        b = make_duo_backend()
        await b.initialize()
        assert b._slot_personas == ["default", "user_sim"]
        assert sorted(b._persona_queues) == ["default", "user_sim"]
        assert b._persona_queues["default"].qsize() == 1
        assert b._persona_queues["user_sim"].qsize() == 1
        health = b.get_health_status()
        assert health["available_instances"] == 2
        assert health["personas"] == {"default": 1, "user_sim": 1}

        # Acquisition routes by persona; both can be held CONCURRENTLY
        # (the dual-pinned-session shape that deadlocks at pool size 1).
        agent = await b.acquire_instance()
        user = await b.acquire_instance(persona="user_sim")
        assert agent._persona == "default" and user._persona == "user_sim"
        h2 = b.get_health_status()
        assert h2["available_instances"] == 0

        # Release returns each to ITS queue.
        await b.release_instance(user)
        await b.release_instance(agent)
        assert b._persona_queues["default"].qsize() == 1
        assert b._persona_queues["user_sim"].qsize() == 1

        # Unknown persona is an explicit error.
        with pytest.raises(RuntimeError, match="unknown persona"):
            await b.acquire_instance(persona="ghost")

    asyncio.run(main())


def test_duo_busy_timeout_names_the_persona():
    async def main():
        b = make_duo_backend()
        await b.initialize()
        _held = await b.acquire_instance(persona="user_sim")
        with pytest.raises(RuntimeError, match=r"\[user_sim\]"):
            await b.acquire_instance(persona="user_sim")

    asyncio.run(main())


def test_jit_with_slot_personas_is_rejected():
    async def main():
        b = make_duo_backend()
        b._jit_enabled = True
        b._jit_limit = 2
        with pytest.raises(RuntimeError, match="eager"):
            await b.initialize()

    asyncio.run(main())


def test_cross_persona_snapshot_guard():
    b = make_duo_backend()
    inst = FakeLlama()
    inst._persona = "user_sim"
    entry = {"persona": "default", "dyn_tokens": [5], "static_len": 3}
    with pytest.raises(RuntimeError, match="cross-persona"):
        b._guard_snapshot_persona(inst, "k", entry)
    # Same persona passes.
    inst2 = FakeLlama()
    inst2._persona = "default"
    b._guard_snapshot_persona(inst2, "k", entry)
