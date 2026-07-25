"""The swarm/model context split (2026-07-24 context-ladder finding).

n_ctx = the SWARM max context (KV cell allocation; the shared pool in
batched mode, bounded only by wired memory). model_max_context = the MODEL
max context (trained per-stream range). Pins: the stream_context_limit
property returns min(pool, trained) and degrades to n_ctx; seats and
health carry the per-stream ceiling while kv_pool_tokens keeps reporting
the pool.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import ModelConfig  # noqa: E402
from inference.backends.llama_cpp_backend import LlamaCppBackend  # noqa: E402


def _model_cfg(n_ctx, model_max_context=None):
    return ModelConfig(
        name="m",
        family="harmony",
        path="/tmp/m.gguf",
        n_ctx=n_ctx,
        model_max_context=model_max_context,
        n_gpu_layers=0,
        seed=1,
        verbose=False,
    )


def test_stream_limit_defaults_to_pool():
    assert _model_cfg(131072).stream_context_limit == 131072


def test_stream_limit_is_min_of_pool_and_trained():
    # The 393k-pool case: allocation 3x the trained range.
    assert _model_cfg(393216, 131072).stream_context_limit == 131072
    # A pool smaller than the trained range keeps the pool bound.
    assert _model_cfg(49152, 131072).stream_context_limit == 49152


def _backend(model_ns) -> LlamaCppBackend:
    config = SimpleNamespace(
        resources=SimpleNamespace(
            cpu_threads=1,
            max_concurrent_requests=2,
            jit_concurrency_limit=None,
            scale_wait_timeout=0.5,
            instance_idle_ttl=0.1,
        ),
        app=SimpleNamespace(backend_timeout=0.2),
        model=model_ns,
    )
    return LlamaCppBackend(config)


def test_backend_stream_limit_reads_property_with_fallback():
    be = _backend(_model_cfg(393216, 131072))
    assert be._stream_ctx_limit() == 131072
    # bare test doubles without the property fall back to n_ctx
    be2 = _backend(SimpleNamespace(n_ctx=8192))
    assert be2._stream_ctx_limit() == 8192


def test_health_reports_both_context_limits():
    be = _backend(_model_cfg(393216, 131072))
    info = be.get_health_status()
    assert info["kv_pool_tokens"] == 393216  # the swarm max context (pool)
    assert info["model_max_context"] == 131072  # the model max context


# ── KV preflight (reboot #3's guard) ──────────────────────────────────


def test_kv_bytes_from_header_matches_measured_anchors():
    # gpt-oss-120b: 36L x 8kvh x (64+64)d = 72KB/tok → 28.3GB @393216
    gpt_oss = LlamaCppBackend.kv_bytes_from_header(393216, [8] * 36, 64, 64)
    assert abs(gpt_oss / 1e9 - 29.0) < 1.0  # 73728 B/tok x 393216
    # gemma-4: 60L, kvh [16,16,16,16,16,4]x10, 512+512d = 1.68MB/tok
    kvh = ([16] * 5 + [4]) * 10
    gemma = LlamaCppBackend.kv_bytes_from_header(262144, kvh, 512, 512)
    assert abs(gemma / 1e9 - 451.0) < 2.0  # reboot #3's arithmetic
    assert LlamaCppBackend.kv_bytes_from_header(32768, kvh, 512, 512) / 1e9 < 60


def test_kv_preflight_refuses_oversize_and_passes_safe(tmp_path, monkeypatch):
    import pytest

    gguf_path = tmp_path / "m.gguf"
    gguf_path.write_bytes(b"x" * 1024)  # weights size ~0GB for the test

    def _fake_header_read(self):
        # emulate the gemma-4 header via the pure helper inputs
        kvh = ([16] * 5 + [4]) * 10
        return kvh, 512, 512

    be = _backend(_model_cfg(262144, 262144))
    be.config.model.swa_full = True
    be.config.model.path = gguf_path

    # monkeypatch the gguf read by substituting the whole preflight's
    # header extraction: patch GGUFReader at the import site with a stub.
    class _StubField:
        def __init__(self, v):
            self._v = v

        def contents(self):
            return self._v

    class _StubReader:
        def __init__(self, path):
            pass

        def get_field(self, key):
            vals = {
                "general.architecture": "gemma4",
                "gemma4.block_count": 60,
                "gemma4.attention.head_count_kv": ([16] * 5 + [4]) * 10,
                "gemma4.attention.key_length": 512,
                "gemma4.attention.value_length": 512,
            }
            return _StubField(vals[key]) if key in vals else None

    import gguf

    monkeypatch.setattr(gguf, "GGUFReader", _StubReader)
    with pytest.raises(RuntimeError, match="KV preflight REFUSED"):
        be._kv_preflight()

    # safe geometry passes silently
    be.config.model.n_ctx = 32768
    be._kv_preflight()

    # non-swa_full configs are exempt (windowed allocations are small)
    be.config.model.n_ctx = 262144
    be.config.model.swa_full = False
    be._kv_preflight()
