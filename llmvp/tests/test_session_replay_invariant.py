"""Full-replay sessions append only onto a VERIFIED KV.

A replay session's live KV is trustworthy iff it holds exactly
[persona static] + token_history (backend.session_kv_matches). The old gate
— "history non-empty, KV non-empty, no dirty flag" — trusted a flag that four
paths never set:

  * an abandoned turn (GeneratorExit at the yield) left a ghost span;
  * a replay that failed AFTER the static restore left a static-only KV that
    the early flag-clear had already marked clean;
  * a budget/stop-text exit committed one token the binding never evaluated
    (it yields, then evaluates on resume), so history ran one token ahead;
  * a replay-mode snapshot fork seeded history but never prefilled it.

These tests drive the SessionManager through the REAL backend generate path
(generate_stream_async → generate_stream_sync) over a FakeGenLlama, which
keeps the binding's yield-then-eval order.
"""

from __future__ import annotations

import asyncio

import pytest

import core.session_manager as sm
from conftest import FakeGenLlama, make_pool_backend
from core.session_manager import SessionManager, SessionState

TURN = [1, 2, 3]  # tokenize_segments is stubbed to this per turn
STATIC = [50, 51, 52, 53]


class _State:
    def __init__(self, tokens):
        self.tokens = list(tokens)
        self.n_tokens = len(tokens)
        self.llama_state_size = 1


class KVInst(FakeGenLlama):
    def __init__(self, script, **kw):
        super().__init__(script, **kw)
        self.load_calls: list = []

    def load_state(self, st):
        self.load_calls.append(st)
        self.n_tokens = st.n_tokens
        self.input_ids[: st.n_tokens] = st.tokens


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
    import llama_cpp

    monkeypatch.setattr(llama_cpp, "llama_token_is_eog", lambda vocab, t: t == 0)
    monkeypatch.setattr(sm, "_get_format_renderer", lambda family: _Renderer())
    monkeypatch.setattr(sm, "get_cached_tokenizer", lambda: object())
    monkeypatch.setattr(sm, "tokenize_segments", lambda tok, segs: list(TURN))


def _setup(script=(7, 8, 0), statics=None, persona="default"):
    backend = make_pool_backend()
    backend._static_states = dict(statics or {"default": _State(STATIC)})
    inst = KVInst(list(script))
    inst._persona = persona
    inst.load_state(backend._persona_static(inst))  # what acquire leaves
    inst.load_calls.clear()  # ...and acquire's restore is not the session's
    mgr = SessionManager(backend)
    sess = SessionState(instance=inst)
    mgr._sessions["s1"] = sess
    return backend, mgr, inst, sess


def _turn(mgr, max_tokens=64):
    async def run():
        return [c async for c in mgr.session_turn("s1", "p", max_tokens=max_tokens)]

    return asyncio.run(run())


def test_clean_turns_append_only_the_new_turn():
    backend, mgr, inst, sess = _setup()
    _turn(mgr)
    assert inst.load_calls, "turn 0 establishes the static base"
    assert sess.token_history == TURN + [7, 8]
    assert backend.session_kv_matches(inst, sess.token_history)

    inst.load_calls.clear()
    inst.eval_calls.clear()
    _turn(mgr)
    assert inst.load_calls == [], "a verified KV must not be restored"
    assert inst.eval_calls[0] == TURN, "only the new turn is fed"


def test_budget_stop_commits_only_what_the_kv_holds():
    # max_tokens=2: token 8 is yielded, the loop breaks, and the binding never
    # evaluates it. History must not claim it.
    backend, mgr, inst, sess = _setup(script=(7, 8, 9))
    _turn(mgr, max_tokens=2)
    assert sess.token_history == TURN + [7]
    assert backend.session_kv_matches(inst, sess.token_history)

    inst.load_calls.clear()
    _turn(mgr, max_tokens=2)
    assert inst.load_calls == [], "the off-by-one no longer forces (or hides) drift"


def test_abandoned_turn_is_detected_and_replayed():
    backend, mgr, inst, sess = _setup(script=(7, 8, 9, 10, 0))
    _turn(mgr)  # clean turn 0
    history = list(sess.token_history)

    async def abandon():
        agen = mgr.session_turn("s1", "p", max_tokens=64)
        await agen.__anext__()  # one chunk, then the consumer walks away
        await agen.aclose()

    asyncio.run(abandon())
    assert sess.token_history == history, "an abandoned turn never commits"
    assert sess.kv_dirty is True
    assert not backend.session_kv_matches(inst, history), "ghost span in the KV"

    inst.load_calls.clear()
    inst.eval_calls.clear()
    _turn(mgr)
    assert len(inst.load_calls) == 1, "the ghost span forces a restore"
    assert inst.eval_calls[0] == history + TURN, "then a replay of the history"
    assert sess.kv_dirty is False, "and the committed turn clears the flag"


def test_failure_after_restore_does_not_leave_a_clean_looking_empty_kv():
    backend, mgr, inst, sess = _setup()
    _turn(mgr)
    history = list(sess.token_history)
    sess.kv_dirty = True
    inst.n_tokens += 1  # a real divergence: the KV is not static + history

    inst._n_ctx = len(STATIC) + 2  # the replay turn dies at the context guard
    with pytest.raises(ValueError, match="exceeds context"):
        _turn(mgr)
    assert sess.kv_dirty is True, "never cleared before a commit"

    inst._n_ctx = 4096
    inst.load_calls.clear()
    inst.eval_calls.clear()
    _turn(mgr)
    assert len(inst.load_calls) == 1
    assert inst.eval_calls[0] == history + TURN, "history replayed, not lost"


def test_replay_snapshot_fork_prefills_the_snapshot():
    backend, mgr, inst, _ = _setup()
    del mgr._sessions["s1"]
    backend._snap_registry["snap"] = {"dyn_tokens": [40, 41], "turn_count": 1}

    async def acquire(persona=None):
        return inst

    backend.acquire_instance = acquire

    async def run():
        info = await mgr.start_session(from_snapshot="snap")
        return info.session_id

    sid = asyncio.run(run())
    sess = mgr._sessions[sid]
    assert sess.kv_dirty is True
    inst.eval_calls.clear()

    async def turn():
        return [c async for c in mgr.session_turn(sid, "p", max_tokens=64)]

    asyncio.run(turn())
    assert inst.eval_calls[0] == [40, 41] + TURN, "the snapshot is prefilled"
    for task in mgr._expiry_tasks.values():
        task.cancel()


def test_restore_uses_the_slot_persona_static():
    user = _State([60, 61])
    backend, mgr, inst, sess = _setup(
        statics={"default": _State(STATIC), "user_sim": user}, persona="user_sim"
    )
    _turn(mgr)
    assert inst.load_calls == [user], "not the default persona's SOUL"
    assert backend.session_kv_matches(inst, sess.token_history)
