"""Model registry + hotswap orchestration (MULTI_MODEL_PLAN.md Phase 1).

Pins the swap lifecycle with the heavy parts stubbed (no weights): the
raise-vs-report contract, gate behavior on every exit path, the
model-bound-global resets, rollback, and the stale-capture regressions
that motivated the live-config view (a frozen module-level snapshot or
cached fsm family silently outliving set_config)."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import model_registry, model_swap  # noqa: E402
from core.config import Config, get_config, set_config  # noqa: E402
from core.model_swap import ModelSwapInProgress  # noqa: E402


def _config_dict(name: str, family: str = "chatml", gguf: str = "/tmp/x.gguf"):
    return {
        "app": {"host": "127.0.0.1", "port": 8008, "log_level": "info"},
        "model": {
            "name": name,
            "family": family,
            "path": gguf,
            "n_ctx": 4096,
            "n_gpu_layers": -1,
            "seed": -1,
            "verbose": False,
        },
        "prompt": {},
        "generation": {"max_tokens_default": 111 if family == "chatml" else 222},
        "knowledge": {"tokens_bin": "./data/test.tokens.bin", "token_limit": 1024},
        "resources": {"cpu_threads": 1, "max_concurrent_requests": 1},
        "logging": {"enabled": False, "directory": "./logs"},
    }


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """A registry catalog of two valid configs + one broken, with pointer."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "archive").mkdir()
    gguf = tmp_path / "weights.gguf"
    gguf.touch()
    import os

    os.truncate(gguf, 50_000_000)  # sparse — st_size 50MB, no disk cost

    (configs / "alpha.yaml").write_text(
        yaml.safe_dump(_config_dict("alpha-model", "chatml", str(gguf)))
    )
    (configs / "beta.yaml").write_text(
        yaml.safe_dump(_config_dict("beta-model", "tekken", str(gguf)))
    )
    (configs / "broken.yaml").write_text(":::not yaml at all\n\t{")
    (configs / "reference.yaml").write_text("# annotated doc, never swappable\n")
    (configs / "archive" / "old.yaml").write_text(
        yaml.safe_dump(_config_dict("old", "chatml", str(gguf)))
    )
    pointer = tmp_path / "active_config.txt"
    pointer.write_text("alpha\n")

    monkeypatch.setattr(model_registry, "CONFIGS_DIR", configs)
    monkeypatch.setattr(model_registry, "POINTER_FILE", pointer)
    return SimpleNamespace(dir=configs, pointer=pointer, gguf=gguf)


@pytest.fixture
def active_alpha(catalog):
    """Global config = the catalog's alpha entry; restored after the test."""
    previous = get_config() if hasattr(get_config, "_config") else None
    set_config(Config(**_config_dict("alpha-model", "chatml", str(catalog.gguf))))
    yield
    if previous is not None:
        set_config(previous)


@pytest.fixture
def swap_harness(catalog, active_alpha, monkeypatch):
    """Stub the heavy lifecycle pieces; record every call."""
    calls = []

    async def fake_shutdown():
        calls.append("shutdown_backend")

    async def fake_init():
        calls.append("initialize_server")

    import core.lifecycle as lifecycle

    monkeypatch.setattr(model_swap, "shutdown_backend_async", fake_shutdown)
    monkeypatch.setattr(lifecycle, "initialize_server_async", fake_init)
    monkeypatch.setattr(
        model_swap, "_reset_model_globals", lambda: calls.append("reset_globals")
    )
    monkeypatch.setattr(model_swap, "get_backend", lambda: None)
    return calls


# ── Registry ─────────────────────────────────────────────────────────


def test_list_models_catalog_shape(catalog):
    entries = {e.name: e for e in model_registry.list_models()}
    assert set(entries) == {"alpha", "beta", "broken"}  # no reference, no archive
    assert entries["alpha"].active and not entries["beta"].active
    assert entries["alpha"].family == "chatml"
    assert entries["alpha"].weights_present
    assert entries["alpha"].gguf_size_gb > 0
    assert entries["broken"].error is not None


def test_resolve_unknown_and_excluded(catalog):
    with pytest.raises(KeyError):
        model_registry.resolve("nope")
    with pytest.raises(KeyError):
        model_registry.resolve("reference")
    assert model_registry.resolve("beta").name == "beta.yaml"


def test_pointer_roundtrip(catalog):
    assert model_registry.active_name() == "alpha"
    model_registry.write_pointer("beta")
    assert model_registry.active_name() == "beta"


# ── Swap lifecycle ───────────────────────────────────────────────────


def test_swap_happy_path(catalog, swap_harness):
    result = asyncio.run(model_swap.swap_model("beta", drain_s=0))
    assert result["ok"] and not result["noop"] and not result["rolled_back"]
    assert result["previous"] == "alpha"
    # Teardown before globals reset before re-init, exactly once each.
    assert swap_harness == ["shutdown_backend", "reset_globals", "initialize_server"]
    assert get_config().model.name == "beta-model"
    assert model_registry.active_name() == "beta"
    assert model_swap.swap_in_progress() is None


def test_swap_noop_same_name(catalog, swap_harness):
    result = asyncio.run(model_swap.swap_model("alpha"))
    assert result["ok"] and result["noop"]
    assert swap_harness == []  # nothing torn down
    assert model_registry.active_name() == "alpha"


def test_swap_unknown_name_raises_before_teardown(catalog, swap_harness):
    with pytest.raises(KeyError):
        asyncio.run(model_swap.swap_model("nope"))
    assert swap_harness == []
    assert model_swap.swap_in_progress() is None


def test_swap_invalid_target_config_raises_before_teardown(catalog, swap_harness):
    with pytest.raises(Exception):
        asyncio.run(model_swap.swap_model("broken"))
    assert swap_harness == []
    assert get_config().model.name == "alpha-model"
    assert model_swap.swap_in_progress() is None


def test_swap_rollback_on_failed_init(catalog, active_alpha, monkeypatch):
    import core.lifecycle as lifecycle

    attempts = []

    async def flaky_init():
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise RuntimeError("target model exploded")

    async def fake_shutdown():
        pass

    monkeypatch.setattr(model_swap, "shutdown_backend_async", fake_shutdown)
    monkeypatch.setattr(lifecycle, "initialize_server_async", flaky_init)
    monkeypatch.setattr(model_swap, "_reset_model_globals", lambda: None)
    monkeypatch.setattr(model_swap, "get_backend", lambda: None)

    result = asyncio.run(model_swap.swap_model("beta", drain_s=0))
    assert not result["ok"] and result["rolled_back"]
    assert "target model exploded" in result["error"]
    assert get_config().model.name == "alpha-model"  # old config restored
    assert model_registry.active_name() == "alpha"  # pointer untouched
    assert model_swap.swap_in_progress() is None


def test_swap_double_failure_reports_modelless(catalog, active_alpha, monkeypatch):
    import core.lifecycle as lifecycle

    async def always_fails():
        raise RuntimeError("no model for you")

    async def fake_shutdown():
        pass

    monkeypatch.setattr(model_swap, "shutdown_backend_async", fake_shutdown)
    monkeypatch.setattr(lifecycle, "initialize_server_async", always_fails)
    monkeypatch.setattr(model_swap, "_reset_model_globals", lambda: None)
    monkeypatch.setattr(model_swap, "get_backend", lambda: None)

    result = asyncio.run(model_swap.swap_model("beta", drain_s=0))
    assert not result["ok"] and not result["rolled_back"]
    assert "modelless" in result["error"]
    assert model_swap.swap_in_progress() is None


# ── Drain ────────────────────────────────────────────────────────────


def test_drain_clean_when_quiet():
    backend = SimpleNamespace(_checked_out=0, _active_generations=0)
    info = asyncio.run(model_swap._drain(backend, None, drain_s=5))
    assert info == {"forced": False, "expired_sessions": 0, "evicted_streams": 0}


def test_drain_forces_after_deadline(monkeypatch):
    monkeypatch.setattr(model_swap, "_FORCE_CLEAR_SETTLE_S", 0.0)

    class Mgr:
        async def expire_all_sessions(self, reason):
            backend._checked_out = 0  # expiry releases the seat
            return 3

    backend = SimpleNamespace(_checked_out=1, _active_generations=0)
    backend._engine = SimpleNamespace(evict_all_streams=lambda reason: 2)
    info = asyncio.run(model_swap._drain(backend, Mgr(), drain_s=0))
    assert info == {"forced": True, "expired_sessions": 3, "evicted_streams": 2}


# ── The gate ─────────────────────────────────────────────────────────


def test_gate_rejects_requests_mid_swap(monkeypatch):
    from core.inference import _get_backend

    monkeypatch.setattr(model_swap, "_state", "loading")
    with pytest.raises(ModelSwapInProgress):
        asyncio.run(_get_backend())


def test_concurrent_swap_rejected(catalog, active_alpha, monkeypatch):
    import core.lifecycle as lifecycle

    release = asyncio.Event()

    async def slow_init():
        await release.wait()

    async def fake_shutdown():
        pass

    monkeypatch.setattr(model_swap, "shutdown_backend_async", fake_shutdown)
    monkeypatch.setattr(lifecycle, "initialize_server_async", slow_init)
    monkeypatch.setattr(model_swap, "_reset_model_globals", lambda: None)
    monkeypatch.setattr(model_swap, "get_backend", lambda: None)

    async def scenario():
        first = asyncio.create_task(model_swap.swap_model("beta", drain_s=0))
        await asyncio.sleep(0.05)  # let it reach the slow init
        with pytest.raises(ModelSwapInProgress):
            await model_swap.swap_model("beta")
        release.set()
        return await first

    result = asyncio.run(scenario())
    assert result["ok"]


# ── Stale-capture regressions (the pre-req fixes) ────────────────────


def test_config_view_follows_swap(catalog, active_alpha):
    """resolve_max_tokens and the fsm family must see the NEW config after
    set_config — the old module-level snapshot froze them at import."""
    from core.inference import _get_fsm_family, resolve_max_tokens

    assert resolve_max_tokens(None) == 111
    assert _get_fsm_family() == "chatml"
    set_config(Config(**_config_dict("beta-model", "tekken", str(catalog.gguf))))
    assert resolve_max_tokens(None) == 222
    assert _get_fsm_family() == "tekken"


def test_explicit_zero_temperature_survives(catalog, active_alpha):
    """temperature=0.0 means greedy and must NOT fall through to the
    default (`requested or default` swallowed it — found when the
    cross-process probe's local leg couldn't produce a deterministic
    baseline). None still resolves to the config default."""
    from core.inference import resolve_temperature

    assert resolve_temperature(0.0) == 0.0
    assert resolve_temperature(None) > 0.0


def test_tokenizer_cache_reset():
    import inference.tokenizer as tok

    tok._tokenizer_cache["/some/model.gguf"] = object()
    tok.reset_tokenizer_cache()
    assert tok._tokenizer_cache == {}


def test_metadata_keyed_by_model_path(catalog, active_alpha):
    """Phase 2a: the store is keyed by the ACTIVE model path — after a
    set_config to a different model, the previous model's metadata is
    invisible (lifecycle re-reads), and swapping back restores it."""
    from inference import metadata as md

    sentinel = object()
    md._model_metadata.clear()
    md.set_model_metadata(sentinel)
    assert md.get_model_metadata() is sentinel

    alpha_cfg = get_config()
    beta_gguf = str(Path(alpha_cfg.model.path).parent / "other.gguf")
    set_config(Config(**_config_dict("beta-model", "tekken", beta_gguf)))
    assert md.get_model_metadata() is None  # new model => no entry, no reset needed

    set_config(alpha_cfg)
    assert md.get_model_metadata() is sentinel  # swap back finds its own entry

    md.reset_model_metadata()
    assert md.get_model_metadata() is None
