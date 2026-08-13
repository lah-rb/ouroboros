"""Phase 2b residency: the registry, the memory governor, and strict routing.

Model-free by construction — every backend here is a stub, so the suite runs
without weights and without Metal. What is under test is the ADMISSION
ARITHMETIC and the failure semantics, which is where this feature can hurt:
a governor that guesses low hard-reboots a 128GB machine, and a router that
falls back silently answers with the wrong model in the right format.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import resident_models as rm  # noqa: E402

# ── fixtures ──────────────────────────────────────────────────────────


def _cfg(name: str, *, n_ctx: int = 8192, path: str = "/nonexistent.gguf"):
    """A duck-typed Config good enough for the governor and the registry."""
    model = types.SimpleNamespace(
        name=name,
        path=path,
        n_ctx=n_ctx,
        mmproj_path=None,
        probe_verified_weights_bytes=None,
    )
    generation = types.SimpleNamespace(max_tokens_default=512, temperature_default=0.7)
    return types.SimpleNamespace(model=model, generation=generation)


class FakeBackend:
    def __init__(self, config, *, fail_shutdown: bool = False):
        self.config = config
        self.fail_shutdown = fail_shutdown
        self.shutdown_calls = 0

    async def initialize(self):
        return None

    async def shutdown(self):
        self.shutdown_calls += 1
        if self.fail_shutdown:
            raise RuntimeError("teardown exploded")


@pytest.fixture(autouse=True)
def clean_registry():
    rm._reset_for_tests()
    yield
    rm._reset_for_tests()


# ── the governor ──────────────────────────────────────────────────────


def test_wired_limit_is_positive_and_sane():
    """Whatever sysctl says, the budget must be a real number of bytes."""
    assert rm.wired_limit_bytes() > 16 * 1024**3


def test_estimate_counts_weights_mmproj_and_kv(monkeypatch):
    cfg = _cfg("m", n_ctx=1000)
    cfg.model.mmproj_path = "/proj.gguf"

    fake_backend_mod = types.SimpleNamespace(
        LlamaCppBackend=types.SimpleNamespace(
            weights_bytes_total=staticmethod(lambda p: 1_000)
        )
    )
    monkeypatch.setitem(
        sys.modules, "inference.backends.llama_cpp_backend", fake_backend_mod
    )
    monkeypatch.setitem(
        sys.modules,
        "core.context_probe",
        types.SimpleNamespace(kv_bytes_per_token=lambda p, m: 7),
    )
    monkeypatch.setattr(rm.os.path, "getsize", lambda p: 500)

    # weights 1000 + mmproj 500 + text kv 7*1000 + vision kv 7*8192*1
    # (vision_n_ctx and vision_pool_size fall back to their defaults here)
    assert rm.estimate_footprint_bytes(cfg) == 1_000 + 500 + 7_000 + 7 * 8192


def test_estimate_prices_the_vision_pool(monkeypatch):
    """The pool is real KV and is often LARGER than the weights: paddle at
    vision_n_ctx 32768 x 4 is 2.25 GB against 1.36 GB of weights+projector.
    Charging only the text n_ctx would let the governor admit a model whose
    actual footprint it never saw."""
    cfg = _cfg("m", n_ctx=100)
    cfg.model.mmproj_path = "/proj.gguf"
    cfg.model.vision_n_ctx = 1000
    cfg.model.vision_pool_size = 4

    monkeypatch.setitem(
        sys.modules,
        "inference.backends.llama_cpp_backend",
        types.SimpleNamespace(
            LlamaCppBackend=types.SimpleNamespace(
                weights_bytes_total=staticmethod(lambda p: 0)
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "core.context_probe",
        types.SimpleNamespace(kv_bytes_per_token=lambda p, m: 10),
    )
    monkeypatch.setattr(rm.os.path, "getsize", lambda p: 0)

    # text kv 10*100 + vision kv 10*1000*4
    assert rm.estimate_footprint_bytes(cfg) == 1_000 + 40_000


def test_no_mmproj_means_no_vision_kv_charged(monkeypatch):
    """A text-only model must not be charged for a pool it cannot build."""
    cfg = _cfg("m", n_ctx=100)
    cfg.model.vision_pool_size = 8  # ignored: no projector, so no vision

    monkeypatch.setitem(
        sys.modules,
        "inference.backends.llama_cpp_backend",
        types.SimpleNamespace(
            LlamaCppBackend=types.SimpleNamespace(
                weights_bytes_total=staticmethod(lambda p: 0)
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "core.context_probe",
        types.SimpleNamespace(kv_bytes_per_token=lambda p, m: 10),
    )
    assert rm.estimate_footprint_bytes(cfg) == 1_000


def test_estimate_prefers_measured_weights_over_file_size(monkeypatch):
    """probe_verified_weights_bytes is MEASURED; file size is an upper bound."""
    cfg = _cfg("m", n_ctx=10)
    cfg.model.probe_verified_weights_bytes = 42

    monkeypatch.setitem(
        sys.modules,
        "inference.backends.llama_cpp_backend",
        types.SimpleNamespace(
            LlamaCppBackend=types.SimpleNamespace(
                weights_bytes_total=staticmethod(lambda p: 999_999)
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "core.context_probe",
        types.SimpleNamespace(kv_bytes_per_token=lambda p, m: 1),
    )
    assert rm.estimate_footprint_bytes(cfg) == 42 + 10


def test_unreadable_kv_geometry_raises_rather_than_assuming_zero(monkeypatch):
    """An unknown KV cost must never be admitted as free — that is the
    arithmetic that produced three hard reboots."""
    monkeypatch.setitem(
        sys.modules,
        "inference.backends.llama_cpp_backend",
        types.SimpleNamespace(
            LlamaCppBackend=types.SimpleNamespace(
                weights_bytes_total=staticmethod(lambda p: 1)
            )
        ),
    )

    def boom(p, m):
        raise OSError("no header")

    monkeypatch.setitem(
        sys.modules,
        "core.context_probe",
        types.SimpleNamespace(kv_bytes_per_token=boom),
    )
    with pytest.raises(ValueError, match="KV geometry"):
        rm.estimate_footprint_bytes(_cfg("m"))


def test_admission_refuses_when_over_budget(monkeypatch):
    # limit 20 - headroom 8 = 12 GB budget; a 15 GB model does not fit.
    monkeypatch.setattr(rm, "wired_limit_bytes", lambda: 20 * 1024**3)
    monkeypatch.setattr(rm, "HEADROOM_BYTES", 8 * 1024**3)
    monkeypatch.setattr(rm, "estimate_footprint_bytes", lambda c: 15 * 1024**3)

    fake_factory = types.SimpleNamespace(get_backend=lambda: None)
    monkeypatch.setitem(
        sys.modules, "inference.backends", types.SimpleNamespace(factory=fake_factory)
    )
    ok, why, facts = rm.admission_check(_cfg("big"))
    assert ok is False
    assert "budget" in why
    assert facts["requestedBytes"] == 15 * 1024**3
    assert facts["budgetBytes"] == 12 * 1024**3


def test_admission_admits_what_fits(monkeypatch):
    """The governor must not be trivially satisfied by refusing everything."""
    monkeypatch.setattr(rm, "wired_limit_bytes", lambda: 20 * 1024**3)
    monkeypatch.setattr(rm, "HEADROOM_BYTES", 8 * 1024**3)
    monkeypatch.setattr(rm, "estimate_footprint_bytes", lambda c: 10 * 1024**3)
    monkeypatch.setitem(
        sys.modules,
        "inference.backends",
        types.SimpleNamespace(factory=types.SimpleNamespace(get_backend=lambda: None)),
    )
    ok, why, _ = rm.admission_check(_cfg("fits"))
    assert ok is True
    assert why == ""


def test_admission_counts_the_primary(monkeypatch):
    """The primary is the biggest resident model; ignoring it would admit a
    second model into memory that is already spoken for."""
    monkeypatch.setattr(rm, "wired_limit_bytes", lambda: 40 * 1024**3)
    monkeypatch.setattr(rm, "HEADROOM_BYTES", 8 * 1024**3)
    monkeypatch.setattr(rm, "estimate_footprint_bytes", lambda c: 20 * 1024**3)

    primary = FakeBackend(_cfg("primary"))
    monkeypatch.setitem(
        sys.modules,
        "inference.backends",
        types.SimpleNamespace(
            factory=types.SimpleNamespace(get_backend=lambda: primary)
        ),
    )
    # budget = 40 - 8 = 32; primary 20 + want 20 = 40 > 32 -> refuse
    ok, why, _ = rm.admission_check(_cfg("second"))
    assert ok is False
    assert "already hot" in why


def test_admission_refuses_when_primary_cannot_be_sized(monkeypatch):
    """An unsizable primary means an unknown budget — refuse, don't guess."""
    monkeypatch.setattr(rm, "wired_limit_bytes", lambda: 100 * 1024**3)

    def sizer(cfg):
        if cfg.model.name == "primary":
            raise ValueError("no header")
        return 1

    monkeypatch.setattr(rm, "estimate_footprint_bytes", sizer)
    primary = FakeBackend(_cfg("primary"))
    monkeypatch.setitem(
        sys.modules,
        "inference.backends",
        types.SimpleNamespace(
            factory=types.SimpleNamespace(get_backend=lambda: primary)
        ),
    )
    ok, why, _ = rm.admission_check(_cfg("second"))
    assert ok is False
    assert "unknown budget" in why


# ── registry lifecycle ────────────────────────────────────────────────


def _install_load_stubs(monkeypatch, *, active="primary", fail_shutdown=False):
    monkeypatch.setattr(rm, "estimate_footprint_bytes", lambda c: 1024)
    monkeypatch.setattr(rm, "wired_limit_bytes", lambda: 100 * 1024**3)
    monkeypatch.setitem(
        sys.modules,
        "inference.backends",
        types.SimpleNamespace(factory=types.SimpleNamespace(get_backend=lambda: None)),
    )
    # Attributes on the REAL module: `from core import model_registry` inside
    # a function reads the package attribute and never re-consults
    # sys.modules["core.model_registry"], so replacing that key does nothing.
    from core import model_registry

    monkeypatch.setattr(model_registry, "active_name", lambda: active)
    monkeypatch.setattr(
        model_registry,
        "list_models",
        lambda: [types.SimpleNamespace(name="secondary")],
    )
    made = {}

    def create_backend(cfg):
        made[cfg.model.name] = FakeBackend(cfg, fail_shutdown=fail_shutdown)
        return made[cfg.model.name]

    monkeypatch.setitem(
        sys.modules,
        "inference.backends.factory",
        types.SimpleNamespace(create_backend=create_backend, get_backend=lambda: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "core.config",
        types.SimpleNamespace(
            Config=lambda **kw: _cfg(kw.get("_name", "secondary")),
            resolve_config_path=lambda n: Path("/tmp/fake.yaml"),
        ),
    )
    monkeypatch.setitem(
        sys.modules, "yaml", types.SimpleNamespace(safe_load=lambda fh: {})
    )
    monkeypatch.setattr(
        Path, "open" if False else "exists", lambda self: True, raising=False
    )
    return made


def test_load_refuses_the_active_primary(monkeypatch):
    _install_load_stubs(monkeypatch, active="secondary")
    with pytest.raises(ValueError, match="already served"):
        asyncio.run(rm.load("secondary"))


def test_load_then_get_then_unload(monkeypatch, tmp_path):
    cfg_file = tmp_path / "fake.yaml"
    cfg_file.write_text("{}")
    made = _install_load_stubs(monkeypatch)
    monkeypatch.setitem(
        sys.modules,
        "core.config",
        types.SimpleNamespace(
            Config=lambda **kw: _cfg("secondary"),
            resolve_config_path=lambda n: cfg_file,
        ),
    )

    backend = asyncio.run(rm.load("secondary"))
    assert rm.is_resident("secondary")
    assert rm.get_resident("secondary") is backend
    assert [e["name"] for e in rm.list_resident()] == ["secondary"]

    asyncio.run(rm.unload("secondary"))
    assert not rm.is_resident("secondary")
    assert made["secondary"].shutdown_calls == 1


def test_double_load_is_idempotent(monkeypatch, tmp_path):
    cfg_file = tmp_path / "fake.yaml"
    cfg_file.write_text("{}")
    _install_load_stubs(monkeypatch)
    monkeypatch.setitem(
        sys.modules,
        "core.config",
        types.SimpleNamespace(
            Config=lambda **kw: _cfg("secondary"),
            resolve_config_path=lambda n: cfg_file,
        ),
    )
    a = asyncio.run(rm.load("secondary"))
    b = asyncio.run(rm.load("secondary"))
    assert a is b
    assert len(rm.list_resident()) == 1


def test_failed_unload_burns_the_name(monkeypatch, tmp_path):
    """The reboot-class guard: a teardown that raised may have orphaned wired
    memory, so re-loading must be refused rather than stacking a copy."""
    cfg_file = tmp_path / "fake.yaml"
    cfg_file.write_text("{}")
    _install_load_stubs(monkeypatch, fail_shutdown=True)
    monkeypatch.setitem(
        sys.modules,
        "core.config",
        types.SimpleNamespace(
            Config=lambda **kw: _cfg("secondary"),
            resolve_config_path=lambda n: cfg_file,
        ),
    )
    asyncio.run(rm.load("secondary"))
    with pytest.raises(RuntimeError, match="teardown exploded"):
        asyncio.run(rm.unload("secondary"))
    assert not rm.is_resident("secondary")  # popped even though teardown failed
    with pytest.raises(RuntimeError, match="stack a second copy"):
        asyncio.run(rm.load("secondary"))


def test_unload_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        asyncio.run(rm.unload("nope"))


def test_get_resident_counts_requests(monkeypatch, tmp_path):
    cfg_file = tmp_path / "fake.yaml"
    cfg_file.write_text("{}")
    _install_load_stubs(monkeypatch)
    monkeypatch.setitem(
        sys.modules,
        "core.config",
        types.SimpleNamespace(
            Config=lambda **kw: _cfg("secondary"),
            resolve_config_path=lambda n: cfg_file,
        ),
    )
    asyncio.run(rm.load("secondary"))
    rm.get_resident("secondary")
    rm.get_resident("secondary")
    assert rm.list_resident()[0]["requests"] == 2


def test_resident_bytes_sums_entries(monkeypatch, tmp_path):
    cfg_file = tmp_path / "fake.yaml"
    cfg_file.write_text("{}")
    _install_load_stubs(monkeypatch)
    monkeypatch.setitem(
        sys.modules,
        "core.config",
        types.SimpleNamespace(
            Config=lambda **kw: _cfg("secondary"),
            resolve_config_path=lambda n: cfg_file,
        ),
    )
    assert rm.resident_bytes() == 0
    asyncio.run(rm.load("secondary"))
    assert rm.resident_bytes() == 1024
