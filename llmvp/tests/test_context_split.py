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
