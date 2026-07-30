"""The effective session KV strategy must be a REPORTED FACT, not an inference.

WHY THIS EXISTS. hy3-reap-200b ran a full 2h tier arm on the full-replay path,
paying O(n^2) session re-prefill — 60.8% of run wall clock at the 58-minute mark
— and nothing in any log said so. The path had to be reverse-engineered from
`static=0 tok` buried in a generation line, and the one fact that would have
settled whether the flat path was even AVAILABLE (`memory_can_shift()`) had never
been queried, because the gate that asks ran only when resident was REQUESTED.

Three states must stay distinguishable, because only one of them is a bug:
  - never requested          -> resident inactive, and that may be correct
  - requested and denied     -> silent fallback, the OPEN_TASKS §4 wrinkle
  - requested and granted    -> flat
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from inference.backends.llama_cpp_backend import LlamaCppBackend


class ShiftCtx:
    """Context double whose can_shift answer is scripted."""

    def __init__(self, can_shift=True, raises=False):
        self._can_shift = can_shift
        self._raises = raises

    def memory_can_shift(self):
        if self._raises:
            raise RuntimeError("binding does not expose memory_can_shift")
        return self._can_shift


def _backend(resident_requested=False, full_replay=True) -> LlamaCppBackend:
    config = SimpleNamespace(
        resources=SimpleNamespace(
            cpu_threads=1,
            max_concurrent_requests=1,
            jit_concurrency_limit=None,
            scale_wait_timeout=0.5,
            instance_idle_ttl=0.1,
        ),
        app=SimpleNamespace(backend_timeout=0.2),
        model=SimpleNamespace(
            resident_seq_cache=resident_requested,
            session_full_replay=full_replay,
            session_snapshot_max=0,
            flow_kv_cache=False,
            flow_kv_cache_max=4,
            resident_session_flow_fork=False,
        ),
    )
    return LlamaCppBackend(config)


def _with_ctx(backend, can_shift=True, raises=False, n_ctx=32768, n_seq_max=1):
    backend._primary_instance = SimpleNamespace(
        _ctx=ShiftCtx(can_shift, raises),
        context_params=SimpleNamespace(n_ctx=n_ctx, n_seq_max=n_seq_max),
    )
    return backend


class TestAskCanShift:
    def test_reports_the_arch_answer(self):
        b = _with_ctx(_backend(), can_shift=True)
        assert b._ask_can_shift() is True

    def test_reports_a_negative_answer(self):
        b = _with_ctx(_backend(), can_shift=False)
        assert b._ask_can_shift() is False

    def test_an_unaskable_question_is_none_not_false(self):
        """None means UNKNOWN. Collapsing it to False would publish a measured
        'this arch cannot' from a question that was never answered — the same
        class of error as reading absent trace content as a negative finding."""
        b = _with_ctx(_backend(), raises=True)
        assert b._ask_can_shift() is None

    def test_asking_does_not_require_having_requested(self):
        """The whole point: a config with resident_seq_cache: false still learns
        whether the flat path was available to it."""
        b = _with_ctx(_backend(resident_requested=False), can_shift=True)
        assert b._resident_requested is False
        assert b._ask_can_shift() is True


class TestEffectiveStrategy:
    def test_resident_when_active(self):
        b = _with_ctx(_backend(resident_requested=True))
        b._resident_active = True
        assert b._session_strategy() == "resident"

    def test_full_replay_when_resident_inactive(self):
        b = _with_ctx(_backend(full_replay=True))
        assert b._session_strategy() == "full_replay"

    def test_legacy_when_neither_is_set(self):
        b = _with_ctx(_backend(resident_requested=False, full_replay=False))
        assert b._session_strategy() == "legacy_save_state"

    def test_requested_but_denied_still_reports_the_path_it_landed_on(self):
        """The §4 wrinkle: the request was granted by config and refused by the
        arch. Strategy must name where it ACTUALLY landed, not what was asked."""
        b = _with_ctx(_backend(resident_requested=True, full_replay=True),
                      can_shift=False)
        b._resident_active = False          # what the gate would have set
        b._session_can_shift = False
        assert b._session_strategy() == "full_replay"


class TestStrategyLogLine:
    def _log(self, backend, caplog) -> str:
        with caplog.at_level(logging.INFO):
            backend._log_session_strategy()
        return "\n".join(r.getMessage() for r in caplog.records)

    def test_names_strategy_and_the_arch_answer(self, caplog):
        b = _with_ctx(_backend(), can_shift=True)
        b._session_can_shift = True
        out = self._log(b, caplog)
        assert "session strategy: full_replay" in out
        assert "memory_can_shift=True" in out

    def test_flags_a_flat_path_left_on_the_table(self, caplog):
        """hy3 exactly: quadratic re-prefill by omission, not by necessity. This
        is the line that makes the cost visible without a forensic dig."""
        b = _with_ctx(_backend(resident_requested=False), can_shift=True)
        b._session_can_shift = True
        out = self._log(b, caplog)
        assert "resident AVAILABLE but not enabled" in out

    def test_does_not_cry_wolf_when_the_arch_genuinely_cannot(self, caplog):
        b = _with_ctx(_backend(resident_requested=False), can_shift=False)
        b._session_can_shift = False
        out = self._log(b, caplog)
        assert "AVAILABLE but not enabled" not in out

    def test_unknown_is_not_reported_as_an_opportunity(self, caplog):
        """An unanswered question must not become an actionable claim."""
        b = _with_ctx(_backend(resident_requested=False), raises=True)
        b._session_can_shift = None
        out = self._log(b, caplog)
        assert "memory_can_shift=unknown" in out
        assert "AVAILABLE but not enabled" not in out

    def test_reports_the_fragmented_per_seq_window(self, caplog):
        """n_ctx_seq = n_ctx / n_seq_max is a silent context amputation when it
        bites (OLMo's 65k became 5,632/seq, 2026-07-22), so it is reported even
        when it is benign."""
        b = _with_ctx(_backend(), n_ctx=32768, n_seq_max=2)
        b._session_can_shift = True
        out = self._log(b, caplog)
        assert "n_ctx_seq=16384" in out

    def test_a_broken_context_does_not_break_the_load(self, caplog):
        """Telemetry must never be the reason a server fails to start."""
        b = _backend()
        b._primary_instance = SimpleNamespace(_ctx=ShiftCtx(), context_params=None)
        out = self._log(b, caplog)
        assert "session strategy" in out

    def test_legacy_path_is_called_out_as_unsafe(self, caplog):
        b = _with_ctx(_backend(resident_requested=False, full_replay=False))
        out = self._log(b, caplog)
        assert "LEGACY save_state" in out


class TestTheAskIsUnconditional:
    """A source-order guard, deliberately.

    The entire point of this change is that `memory_can_shift()` is asked whether
    or not resident was requested — and that placement lives in `initialize()`,
    which cannot run without a real model. Nothing else in this file would notice
    the ask being moved back inside `if self._resident_requested:`, which is the
    exact regression that left hy3's answer unknown. So: assert the order."""

    def test_can_shift_is_asked_before_the_requested_branch(self):
        import inspect

        from inference.backends import llama_cpp_backend as mod

        src = inspect.getsource(mod)
        ask = src.index("self._session_can_shift = self._ask_can_shift()")
        gate = src.index("if self._resident_requested:\n            can_shift")
        assert ask < gate, (
            "the can_shift query must run BEFORE (and outside) the "
            "resident-requested branch, or configs with resident off learn nothing"
        )

    def test_the_strategy_line_is_logged_outside_the_branch(self):
        import inspect

        from inference.backends import llama_cpp_backend as mod

        src = inspect.getsource(mod)
        call = src.index("self._log_session_strategy()")
        # Eight spaces = method-body indent. Deeper would mean it sits inside the
        # resident branch and would not fire for a full_replay config.
        line_start = src.rindex("\n", 0, call) + 1
        assert src[line_start:call] == " " * 8, (
            "session-strategy logging must not be nested inside a conditional"
        )


class TestInfoDict:
    def test_probe_can_read_the_verdict_without_parsing_logs(self):
        b = _with_ctx(_backend(resident_requested=False), can_shift=True)
        b._session_can_shift = True
        info = {}
        info["session_strategy"] = b._session_strategy()
        info["session_can_shift"] = b._session_can_shift
        info["resident_requested"] = b._resident_requested
        assert info == {
            "session_strategy": "full_replay",
            "session_can_shift": True,
            "resident_requested": False,
        }

    def test_never_requested_and_requested_but_denied_are_distinguishable(self):
        never = _with_ctx(_backend(resident_requested=False), can_shift=False)
        never._session_can_shift = False
        denied = _with_ctx(_backend(resident_requested=True), can_shift=False)
        denied._session_can_shift = False
        # Same strategy, different diagnosis — only the second is a config bug.
        assert never._session_strategy() == denied._session_strategy()
        assert never._resident_requested != denied._resident_requested
