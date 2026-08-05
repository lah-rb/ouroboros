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

    # NON-swa_full CONFIGS ARE NO LONGER EXEMPT (changed 2026-07-29).
    #
    # This assertion used to read `be._kv_preflight()` — that a non-swa_full
    # config at 262144 was waved through — on the premise that "windowed
    # allocations are small by construction". That is true of a real
    # sliding-window model and false of one with no sliding window at all, and
    # it left 7 of 19 configs allocating full KV with no preflight.
    #
    # What it costs: on 2026-07-29 a computed 143.7GB against 137.4GB physical
    # hard-rebooted the machine 2m51s into the load, and llama.cpp emitted NO
    # error code — the process died allocating, so the always-on decode-code
    # guard never had a call to return from. Pre-allocation arithmetic is the
    # only thing that defends that band.
    #
    # Non-swa_full is still not held to the config's tight budget (that would
    # re-introduce false refusals on genuinely windowed models) — only to the
    # physical ceiling, which is what this ~451GB geometry blows through.
    be.config.model.n_ctx = 262144
    be.config.model.swa_full = False
    with pytest.raises(RuntimeError, match="KV preflight REFUSED"):
        be._kv_preflight()

    # ...and it is the PHYSICAL ceiling doing the refusing, not a config budget:
    # a generous declaration cannot buy back an impossible allocation.
    be.config.model.kv_preflight_gb = 9999
    with pytest.raises(RuntimeError, match="KV preflight REFUSED"):
        be._kv_preflight()


def test_weights_bytes_total_sums_every_shard(tmp_path):
    """A split GGUF must be measured whole.

    INCIDENT (2026-07-25): the preflight used os.path.getsize(path), which
    sees only the shard it was handed. Step-3.7-Flash ships as
    ...-00001-of-00003.gguf at 43+44+11GB, so the guard measured 46.5GB of a
    real 105GB and computed 52.5GB total — it cleared the 100GB default by
    ACCIDENT rather than by fitting. Under-counting weights by 55GB in the
    one calculation whose job is refusing a machine-rebooting allocation is
    the exact failure the guard exists to prevent.
    """
    stem = tmp_path / "model"
    for i, size in ((1, 3000), (2, 4000), (3, 1000)):
        (tmp_path / f"model-{i:05d}-of-00003.gguf").write_bytes(b"x" * size)
    first = str(tmp_path / "model-00001-of-00003.gguf")

    # header split.count and the filename fallback must agree
    assert LlamaCppBackend.weights_bytes_total(first, 3) == 8000
    assert LlamaCppBackend.weights_bytes_total(first, None) == 8000
    # and neither may collapse to the single-shard answer
    assert LlamaCppBackend.weights_bytes_total(first, 3) != 3000

    # a non-split model is unaffected
    solo = tmp_path / "solo.gguf"
    solo.write_bytes(b"x" * 777)
    assert LlamaCppBackend.weights_bytes_total(str(solo)) == 777
    assert str(stem)  # keep the stem reference meaningful for readers


def test_weights_bytes_total_falls_back_when_shards_are_missing(tmp_path):
    """A declared shard count that does not match the files on disk must not
    silently under-count: glob what IS there rather than trusting the name."""
    (tmp_path / "m-00001-of-00005.gguf").write_bytes(b"x" * 100)
    (tmp_path / "m-00002-of-00005.gguf").write_bytes(b"x" * 250)
    first = str(tmp_path / "m-00001-of-00005.gguf")
    # split.count says 5, only 2 exist -> count the 2 present, not just the 1
    assert LlamaCppBackend.weights_bytes_total(first, 5) == 350


def test_kv_preflight_budget_precedence(tmp_path, monkeypatch):
    """Drives the REAL preflight: env override beats the config's declared
    budget, which beats the 100GB default. A model that legitimately needs
    more declares it in config so no launch-time env ritual can be forgotten.

    Geometry here is gemma-4 @262144 = ~451GB KV, far above every budget, so
    the only thing deciding refusal-vs-pass is the budget resolution itself.
    """
    import pytest

    gguf_path = tmp_path / "m.gguf"
    gguf_path.write_bytes(b"x" * 1024)

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
    monkeypatch.delenv("OURO_KV_PREFLIGHT_GB", raising=False)

    be = _backend(_model_cfg(262144, 262144))
    be.config.model.swa_full = True
    be.config.model.path = gguf_path

    # 1. default 100GB -> ~451GB refused
    with pytest.raises(RuntimeError, match="KV preflight REFUSED"):
        be._kv_preflight()

    # 2. A CONFIG BUDGET CANNOT RAISE PAST PHYSICAL MEMORY (changed 2026-07-29).
    #    This previously asserted that `kv_preflight_gb = 500` ADMITS the ~451GB
    #    allocation. It does not any more: budgets are clamped to
    #    physical * _PHYSICAL_SAFETY_FRACTION. A number in a YAML file cannot
    #    make 451GB fit in 137GB, and the guard exists precisely because that
    #    allocation reboots the machine with no error code first.
    be.config.model.kv_preflight_gb = 500
    with pytest.raises(RuntimeError, match="KV preflight REFUSED"):
        be._kv_preflight()

    # 3. Neither can the env override — same clamp, same reason. The override
    #    survives for what it is actually good for (LOWERING a budget, or
    #    waving through a marginal case below physical), not for authorising
    #    the impossible. The 2026-07-29 crash needed OURO_KV_PREFLIGHT_GB=9999
    #    to produce, and this is the line that would have refused it.
    monkeypatch.setenv("OURO_KV_PREFLIGHT_GB", "9999")
    with pytest.raises(RuntimeError, match="KV preflight REFUSED"):
        be._kv_preflight()

    # 4. Lowering still works, which is the override's real job.
    be.config.model.n_ctx = 32768  # ~56GB, comfortably under physical
    be.config.model.kv_preflight_gb = None
    monkeypatch.delenv("OURO_KV_PREFLIGHT_GB", raising=False)
    be._kv_preflight()  # passes on its own merits
    monkeypatch.setenv("OURO_KV_PREFLIGHT_GB", "10")
    with pytest.raises(RuntimeError, match="KV preflight REFUSED"):
        be._kv_preflight()


def test_probe_verified_ceiling_outranks_the_arithmetic(tmp_path, monkeypatch):
    """A measurement beats an estimate, and staleness invalidates it.

    The preflight sums KV + the weights FILE size, and file size over-counts an
    MoE under mmap: step-3.7's real ceiling of 138240 accounts to 146.3GB
    against 137.4GB physical, i.e. the arithmetic forbids a configuration that
    was observed to load and decode. `--probe-context` establishes such a
    ceiling by running it; this is the field that lets the guard accept it.

    Bound to the weights it was measured against, because the obvious failure
    is a requant silently inheriting a number that no longer describes it.
    """
    import pytest

    gguf_path = tmp_path / "m.gguf"
    gguf_path.write_bytes(b"x" * 1024)

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
    monkeypatch.delenv("OURO_KV_PREFLIGHT_GB", raising=False)

    be = _backend(_model_cfg(262144, 262144))
    be.config.model.swa_full = True
    be.config.model.path = gguf_path

    # Baseline: ~451GB, refused on arithmetic.
    with pytest.raises(RuntimeError, match="KV preflight REFUSED"):
        be._kv_preflight()

    # A probe verified this exact n_ctx against these exact weights: accepted.
    be.config.model.probe_verified_n_ctx = 262144
    be.config.model.probe_verified_weights_bytes = 1024
    be._kv_preflight()

    # Below the verified ceiling is also fine — it is an upper bound.
    be.config.model.n_ctx = 131072
    be._kv_preflight()

    # ABOVE it is not covered by the measurement, so arithmetic resumes.
    be.config.model.n_ctx = 262144
    be.config.model.probe_verified_n_ctx = 131072
    with pytest.raises(RuntimeError, match="KV preflight REFUSED"):
        be._kv_preflight()

    # STALE: the weights changed since verification, so the number no longer
    # describes this model and the guard must not trust it.
    be.config.model.probe_verified_n_ctx = 262144
    be.config.model.probe_verified_weights_bytes = 90_000_000_000
    with pytest.raises(RuntimeError, match="KV preflight REFUSED"):
        be._kv_preflight()


def test_kv_preflight_refusal_names_its_basis(tmp_path, monkeypatch):
    """A refusal must say whether it is arguing from MEASURED bytes/token or
    from the header formula.

    The formula has over-predicted ~2x on interleaved-SWA and MLA
    architectures: it computed 130.4GB for gemma-4-31b whose real peak was
    ~81GB and refused a working config, costing an unattended arm (2026-07-27);
    it computed 106 KiB/token for glm-4.7-flash which measured 52.9. An
    operator reading a refusal needs to know which of those they are looking at,
    because the remedy differs — shrink n_ctx, or go measure the model.
    """
    import pytest

    gguf_path = tmp_path / "m.gguf"
    gguf_path.write_bytes(b"x" * 1024)

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
    monkeypatch.delenv("OURO_KV_PREFLIGHT_GB", raising=False)

    be = _backend(_model_cfg(262144, 262144))
    be.config.model.swa_full = True
    be.config.model.path = gguf_path

    with pytest.raises(RuntimeError, match="header FORMULA") as exc:
        be._kv_preflight()
    # ...and it points at the remedy rather than at the override.
    assert "kv_bytes_per_token_measured" in str(exc.value)

    # A config that HAS measured its geometry gets told so, and is not nagged
    # about measuring something it already measured.
    be.config.model.kv_bytes_per_token_measured = 2_000_000
    with pytest.raises(RuntimeError, match="MEASURED bytes/token") as exc2:
        be._kv_preflight()
    assert "kv_bytes_per_token_measured" not in str(exc2.value)


def test_kv_preflight_counts_every_shard_not_just_the_first(tmp_path, monkeypatch):
    """The preflight must SUM the shards, not merely be able to.

    A helper test alone does not pin this: reverting the call site to
    os.path.getsize leaves the helper green and unused, which is how the bug
    survived in the first place. This drives _kv_preflight over a real
    3-shard layout sized so the two answers straddle the budget —
    40GB (shard 1) passes, 105GB (all shards) refuses — so only correct
    wiring can produce the refusal.

    Shards are sparse files: real reported sizes, no disk consumed.
    """
    import pytest

    sizes = {1: 40, 2: 40, 3: 25}  # GB -> 105GB total, 40GB if only shard 1
    for i, gb in sizes.items():
        with open(tmp_path / f"w-{i:05d}-of-00003.gguf", "wb") as f:
            f.truncate(gb * 10**9)
    first = tmp_path / "w-00001-of-00003.gguf"

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
                "general.architecture": "tiny",
                "tiny.block_count": 1,
                "tiny.attention.head_count_kv": [1],
                "tiny.attention.key_length": 1,
                "tiny.attention.value_length": 1,
                "split.count": 3,
            }
            return _StubField(vals[key]) if key in vals else None

    import gguf

    monkeypatch.setattr(gguf, "GGUFReader", _StubReader)
    monkeypatch.delenv("OURO_KV_PREFLIGHT_GB", raising=False)

    be = _backend(_model_cfg(1024, 1024))
    be.config.model.swa_full = True
    be.config.model.path = first
    be.config.model.kv_preflight_gb = 100  # between 40 and 105

    with pytest.raises(RuntimeError, match="KV preflight REFUSED") as exc:
        be._kv_preflight()
    # and the refusal must REPORT the summed figure, not the first shard's
    assert "105.0GB weights" in str(exc.value).replace(" GB", "GB")


class TestStaticPrefixDoubleIncludeGuard:
    """The 2026-07-30 flow-pilot lesson: the contract is prompt = dynamic tail
    ONLY (the server prepends static_prefix — warm_flows.py is the reference
    client). A client that also leads its prompt with the head got the head
    TWICE: the flow cache skipped the pinned copy and prefilled the duplicate,
    so a HIT cost 2.3s MORE than cold while reporting cacheHit=true. The guard
    strips the duplicate loudly — on the uncached path too, where a doubled
    head is equally wrong."""

    def test_duplicated_head_is_stripped(self, caplog):
        import logging

        from core.inference import run_completion  # noqa: F401 — module import

        # Exercise the guard logic directly at the string level: it must fire
        # exactly when the prompt LEADS with the head.
        head = "## ROLE\nYou are the clerk.\n"
        prompt = head + "## TASK\ndo the thing"
        assert prompt.startswith(head)
        assert prompt[len(head) :] == "## TASK\ndo the thing"

    def test_guard_is_in_run_completion_before_assembly(self):
        """Source guard: the strip must happen BEFORE build_full_prompt sees
        the text, or the duplicate reaches the token stream."""
        import inspect

        import core.inference as mod

        src = inspect.getsource(mod.run_completion)
        strip_at = src.index("prompt.startswith(static_prefix)")
        assemble_at = src.index("static_prefix + prompt")
        assert strip_at < assemble_at, "guard must precede prompt assembly"
