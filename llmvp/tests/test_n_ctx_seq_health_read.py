"""The n_ctx_seq health read must never touch the live context.

Three identical SIGSEGVs killed the hy3 server (2026-08-07/08/09), all with
top frame libllama!llama_n_ctx_seq at KERN_INVALID_ADDRESS 0xc: get_status()
probed the LIVE context via ctypes on every health query, and a query landing
mid-rebuild (refresh or latch heal free the context before recreating it)
dereferenced the freed pointer. A native segfault is uncatchable by the
try/except that wrapped it. n_ctx_seq is immutable per built context, so the
value is cached at every build/rebuild and the health path reads the int.
"""

import inspect
from inference.backends import llama_cpp_backend as B


def test_health_read_does_not_probe_the_live_context():
    src = inspect.getsource(B.LlamaCppBackend.get_health_status)
    # strip comments — the fix's own comment names the removed call
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "_seq_ctx_limit(" not in code, (
        "get_health_status must read the cached _n_ctx_seq, never probe the live "
        "context — that probe segfaulted the server three times"
    )
    assert "_n_ctx_seq" in code


def test_every_build_and_rebuild_site_caches_the_value():
    src = inspect.getsource(B)
    # each _seq_ctx_limit( call on a real context is followed by a cache write
    assert (
        src.count("_n_ctx_seq = _seq_lim") >= 3
    ), "build, rebuild and primary-clamp sites must all cache _n_ctx_seq"


def test_cached_read_survives_a_torn_down_context():
    class _Inst:
        _n_ctx_seq = 32768
        _ctx = None  # freed mid-rebuild
        context_params = None

    class _B:
        _primary_instance = _Inst()

    # the cached read is a plain attribute fetch — no ctypes, no crash
    assert int(getattr(_B._primary_instance, "_n_ctx_seq", 0) or 0) == 32768
