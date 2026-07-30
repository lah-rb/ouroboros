"""The per-stream guard must know the TRUE per-seq window.

WHY. Under ``kv_unified: false`` llama.cpp splits the allocation across
sequences (``n_ctx_seq ≈ n_ctx / n_seq_max``), but every LLMVP guard keyed off
``inst._n_ctx = min(n_ctx, model_max_context)`` — blind to the divisor. The
stock resident band is 12 seqs, and every historical casualty is exactly /12:
OLMo 65,536 → 5,632 (first design prompt failed to decode), qwen3.5 264,192 →
22,016 (silent amputation), and the 2026-07-29 sweep's hy3 row, whose 2,730-cell
window fell below its own 1,793-token static prefix and died with
``Llama.eval(decode): Failed completely even with batch size 1``.

The binding exposes ``llama_n_ctx_seq`` and it is correct in BOTH modes (returns
n_ctx under kv_unified). These tests pin the helper and the clamp sites.
"""

from __future__ import annotations

from types import SimpleNamespace

from inference.backends.llama_cpp_backend import LlamaCppBackend


class _Ctx:
    def __init__(self, n_ctx_seq=None, raises=False):
        self._n = n_ctx_seq
        self._raises = raises

    def n_ctx_seq(self):
        if self._raises:
            raise AttributeError("older binding")
        return self._n


def _params(n_ctx=32768, n_seq_max=12, kv_unified=False):
    return SimpleNamespace(n_ctx=n_ctx, n_seq_max=n_seq_max, kv_unified=kv_unified)


class TestSeqCtxLimit:
    def test_asks_the_context_first(self):
        """llama_n_ctx_seq is authoritative — it already accounts for
        kv_unified, rounding, and anything a future llama.cpp changes. The
        binding answer here deliberately DISAGREES with the arithmetic
        (32768/12 = 2730), so a mutant that skips the binding cannot pass by
        coincidence."""
        got = LlamaCppBackend._seq_ctx_limit(_Ctx(2816), _params())
        assert got == 2816, "the context's own answer must win over arithmetic"

    def test_the_hy3_arithmetic_fallback(self):
        """32,768 / 12 = 2,730 — the exact window that killed the sweep row.
        The fallback must reproduce it, not the /2 the plan assumed."""
        got = LlamaCppBackend._seq_ctx_limit(_Ctx(raises=True), _params())
        assert got == 32768 // 12 == 2730

    def test_kv_unified_means_no_division(self):
        got = LlamaCppBackend._seq_ctx_limit(
            _Ctx(raises=True), _params(n_ctx=524288, n_seq_max=131, kv_unified=True)
        )
        assert got == 524288

    def test_unknown_is_zero_never_a_guess(self):
        """0 = could not determine. Callers skip zero in their min() — an
        unknown must never masquerade as a measurement."""
        assert LlamaCppBackend._seq_ctx_limit(None, None) == 0
        assert LlamaCppBackend._seq_ctx_limit(_Ctx(raises=True), None) == 0

    def test_a_raising_params_object_is_unknown_not_n_ctx(self):
        """The exception path must return 0, not a guess. A params object whose
        attributes RAISE (a half-built test double, a ctypes struct mid-teardown)
        exercised the branch a None params never reaches."""

        class _Hostile:
            @property
            def n_ctx(self):
                raise RuntimeError("mid-teardown")

            n_seq_max = property(n_ctx.fget)
            kv_unified = property(n_ctx.fget)

        got = LlamaCppBackend._seq_ctx_limit(_Ctx(raises=True), _Hostile())
        assert got == 0, "an exception must yield UNKNOWN, never a fabricated window"

    def test_zero_from_the_binding_falls_through_to_arithmetic(self):
        got = LlamaCppBackend._seq_ctx_limit(_Ctx(0), _params(n_ctx=65536, n_seq_max=12))
        assert got == 65536 // 12

    def test_the_two_historical_amputations(self):
        assert LlamaCppBackend._seq_ctx_limit(
            _Ctx(raises=True), _params(n_ctx=65536, n_seq_max=12)
        ) == 5461  # OLMo (llama.cpp rounded up to 5,632; the guard needs <=)
        assert LlamaCppBackend._seq_ctx_limit(
            _Ctx(raises=True), _params(n_ctx=264192, n_seq_max=12)
        ) == 22016  # qwen3.5 — exact


class TestClampSites:
    """The helper is only useful if every _n_ctx establishment site uses it.
    Source-order guards, because three of the four sites need a real model to
    integration-test and a silent revert at any one re-opens the blind spot."""

    def _src(self):
        import inspect

        from inference.backends import llama_cpp_backend as mod

        return inspect.getsource(mod)

    def test_shared_instance_clamps(self):
        src = self._src()
        assert "_seq_lim = self._seq_ctx_limit(ctx, params)" in src
        assert "min(c for c in (_base_ctx, _lim, _seq_lim) if c)" in src

    def test_refresh_reclamps_from_the_rebuilt_context(self):
        """A refresh can change seq geometry (the un-fragment path rebuilds
        with n_seq_max=1 through here) — a stale _n_ctx re-opens the hole."""
        src = self._src()
        assert "_seq_lim = self._seq_ctx_limit(inst._ctx, inst.context_params)" in src

    def test_primary_is_clamped_too(self):
        """The primary IS pool slot 0 and upstream Llama set its _n_ctx to the
        TOTAL allocation — before this, a session pinned to slot 0 on a
        fragmented context had no guard at all."""
        src = self._src()
        assert "_p._n_ctx = min(c for c in (int(_p._n_ctx), _lim, _seq_lim) if c)" in src

    def test_health_reports_the_real_window(self):
        src = self._src()
        assert 'info["n_ctx_seq"]' in src

    def test_strategy_line_prefers_the_measured_window(self):
        src = self._src()
        assert "seq_win or (n_ctx // n_seq)" in src
