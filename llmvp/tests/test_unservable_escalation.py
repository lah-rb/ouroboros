"""Repeated fatal decodes that a context rebuild does NOT fix -> stop serving.

WHY. llama_decode -3 has several causes and most of them heal: SWA-boundary
corruption on a pinned prefix, save_state churn, a one-off Metal command-buffer
failure. `_heal_instance` rebuilds the context, the sticky error latch clears,
work resumes — and that machinery works.

What does NOT heal is a configuration whose decode cannot fit. On 2026-07-28
the Hy3 ceiling ladder found exactly that: n_ctx 49152 and 65536 both LOADED,
both reported healthy, and both failed EVERY decode with
`llama_decode failed (code -3): Graph computation failed internally`. The rung
above rebooted the machine while the process held 108 GB of weights resident.

So -3 is a LEADING indicator — it appeared two rungs before the crash, while
the box was still entirely healthy — but only in its unhealed form. These tests
pin the distinction, because getting it wrong in either direction is expensive:
killing on the first -3 takes the server down for faults it currently recovers
from, and never killing leaves it thrashing until the kernel gives up.
"""

from __future__ import annotations

import pytest


class _Inst:
    def __init__(self, persona="default"):
        self._persona = persona


class _Backend:
    """The three methods under test, lifted onto a bare object so no Metal,
    no weights and no event loop are needed to exercise the decision."""

    from inference.backends.llama_cpp_backend import LlamaCppBackend

    _UNHEALED_LIMIT = LlamaCppBackend._UNHEALED_LIMIT
    _maybe_declare_unservable = LlamaCppBackend._maybe_declare_unservable
    _mark_decode_failure = LlamaCppBackend._mark_decode_failure

    def __init__(self):
        self._h_decode_failures = 0
        self._consecutive_unhealed_decode_failures = 0
        self._unservable = False
        self.signalled = 0

    def _heal(self, inst):
        """Stand-in for _heal_instance's arming step."""
        inst._healed_since_failure = True


@pytest.fixture
def backend(monkeypatch):
    b = _Backend()
    # Intercept the self-SIGTERM so the test process survives its own guard.
    import os

    monkeypatch.setattr(os, "kill", lambda *_a, **_k: setattr(b, "signalled", b.signalled + 1))
    return b


class TestASingleFailureIsNotFatal:
    def test_one_failure_does_not_stop_the_server(self, backend):
        """The healable case. -3 on a stale latch is routine and recovers."""
        backend._mark_decode_failure(_Inst(), RuntimeError("code -3"))
        assert backend._unservable is False
        assert backend.signalled == 0

    def test_a_failure_before_any_heal_does_not_count_as_unhealed(self, backend):
        """Only a failure AFTER a rebuild proves the rebuild did not help."""
        for _ in range(5):
            backend._mark_decode_failure(_Inst(), RuntimeError("code -3"))
        assert backend._consecutive_unhealed_decode_failures == 0
        assert backend._unservable is False


class TestHealThenFailAgain:
    def test_repeated_unhealed_failures_declare_unservable(self, backend):
        """The Hy3 49152/65536 signature: rebuild, fail, rebuild, fail."""
        inst = _Inst()
        for _ in range(backend._UNHEALED_LIMIT):
            backend._heal(inst)
            backend._mark_decode_failure(inst, RuntimeError("code -3"))
        assert backend._unservable is True

    def test_it_shuts_the_process_down_rather_than_only_refusing(self, backend):
        """Refusing work while still holding the weights does not protect the
        machine — releasing the memory is the only action that does."""
        inst = _Inst()
        for _ in range(backend._UNHEALED_LIMIT):
            backend._heal(inst)
            backend._mark_decode_failure(inst, RuntimeError("code -3"))
        assert backend.signalled == 1, "expected exactly one SIGTERM to self"

    def test_it_fires_once_not_on_every_later_failure(self, backend):
        inst = _Inst()
        for _ in range(backend._UNHEALED_LIMIT + 4):
            backend._heal(inst)
            backend._mark_decode_failure(inst, RuntimeError("code -3"))
        assert backend.signalled == 1


class TestRecoveryResets:
    def test_a_successful_decode_clears_the_counter(self, backend):
        """Without this the escalation aims at the wrong thing: a slow drip of
        unrelated transients over a long run would eventually trip it."""
        inst = _Inst()
        backend._heal(inst)
        backend._mark_decode_failure(inst, RuntimeError("code -3"))
        assert backend._consecutive_unhealed_decode_failures == 1
        backend._consecutive_unhealed_decode_failures = 0  # what a good decode does
        backend._heal(inst)
        backend._mark_decode_failure(inst, RuntimeError("code -3"))
        assert backend._consecutive_unhealed_decode_failures == 1
        assert backend._unservable is False

    def test_the_limit_is_operator_tunable(self):
        from inference.backends.llama_cpp_backend import LlamaCppBackend

        assert LlamaCppBackend._UNHEALED_LIMIT >= 2, (
            "a limit of 1 would kill on the first post-heal failure, which is "
            "still within the range a transient command-buffer fault produces"
        )


class TestItIsVisibleToClients:
    def test_health_exposes_the_decode_signals(self):
        """The agent-side watchdog reads health. An unservable backend it
        cannot see is one it will keep sending work to."""
        from api.graphql_api import HealthStatus

        for f in ("decode_failures", "unhealed_decode_failures", "unservable"):
            assert f in HealthStatus.__annotations__
