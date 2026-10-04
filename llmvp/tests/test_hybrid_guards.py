"""Hybrid-memory guards found while planning turn rollback (2026-09-22).

1. The resident degenerate purge rewound `n_tokens` over a `memory_seq_rm`
   that a hybrid's recurrent half REFUSES (it returns False and changes
   nothing). qwen3-next runs resident, so a degenerate turn there left the
   positions out of step with the cells. The purge now checks the answer; a
   refused purge closes the session instead of decoding on a poisoned KV.

2. The M-RoPE resident exception keys on rope type, and the GDN qwens and
   qwen4exp are IMROPE too — on a HYBRID memory, where can_shift=False does
   mean the resident seq ops fail. The exception is for attention caches.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

import core.session_manager as sm
from core.session_manager import SessionManager, SessionState
from inference.repetition import DegenerateGenerationError


class _Renderer:
    def render_turn_transition_segments(self):
        return []

    def render_user_segments(self, prompt):
        return [(prompt, False)]

    def render_generation_prompt_segments(self, reasoning=None):
        return []

    def prefills_think_opener(self, reasoning=None):
        return False

    def stop_tokens(self, mode=None):
        return []


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(sm, "_get_format_renderer", lambda family: _Renderer())
    monkeypatch.setattr(sm, "get_cached_tokenizer", lambda: object())
    monkeypatch.setattr(sm, "tokenize_segments", lambda tok, segs: [1, 2, 3])


class _Ctx:
    def __init__(self, rm_answer):
        self.rm_answer = rm_answer
        self.rms: list = []

    def memory_seq_rm(self, seq, p0, p1):
        self.rms.append((seq, p0, p1))
        return self.rm_answer


class _ResidentBackend:
    _resident_active = True

    def __init__(self):
        self.config = SimpleNamespace(model=SimpleNamespace(family="chatml"))

    def generate_stream_async(self, instance, prompt_tokens, **kw):
        async def degen():
            instance.n_tokens += len(prompt_tokens) + 40  # the span it decoded
            yield "loop"
            raise DegenerateGenerationError("long-cycle", tokens_generated=40)

        return degen()


def _run_degenerate(rm_answer):
    backend = _ResidentBackend()
    mgr = SessionManager(backend)
    inst = SimpleNamespace(_ctx=_Ctx(rm_answer), n_tokens=100, _n_ctx=100_000)
    sess = SessionState(instance=inst)
    mgr._sessions["s1"] = sess

    async def turn():
        async for _ in mgr.session_turn("s1", "p", max_tokens=64):
            pass

    with pytest.raises(DegenerateGenerationError):
        asyncio.run(turn())
    return mgr, inst, sess, turn


def test_accepted_purge_rewinds_to_the_pre_turn_position():
    _, inst, sess, _ = _run_degenerate(rm_answer=True)
    assert inst._ctx.rms == [(0, 100, -1)]
    assert inst.n_tokens == 100
    assert sess.kv_lost is False


def test_refused_purge_never_rewinds_and_closes_the_session():
    _, inst, sess, turn = _run_degenerate(rm_answer=False)
    assert inst.n_tokens == 143, "positions must stay with the untouched cells"
    assert sess.kv_lost is True
    with pytest.raises(RuntimeError, match="lost its KV"):
        asyncio.run(turn())


def test_mrope_resident_exception_excludes_hybrid_memory():
    from inference.backends import llama_cpp_backend as mod

    src = inspect.getsource(mod)
    hybrid_known = src.index("self._is_hybrid = self._check_is_hybrid(")
    exclusion = src.index("mrope = mrope and not self._is_hybrid")
    grant = src.index("self._resident_active = can_shift or mrope")
    assert hybrid_known < exclusion < grant
