"""Replay sessions on a hybrid use turn rollback, never a history replay.

Three uses of the per-turn recurrent checkpoint (inference/turn_checkpoint.py),
driven through the REAL session manager and backend generate path over a
hybrid memory double (conftest.HybridCtx — the recurrent half refuses a tail
rm until its state is restored):

* STRIP — a turn whose reasoning closed is rolled back and re-evaluated as its
  prompt up to the reasoning plus its clean answer; the history is never fed.
* DROP — a turn that produced no answer (a prefilled think that never closed:
  the 2026-09-22 engine.py turn, 78,120 tokens of unfinished thinking) is rolled
  out of the context and never enters history.
* REPAIR — a turn abandoned mid-stream is rolled back at the next turn's start
  instead of restoring the static base and replaying everything.
"""

from __future__ import annotations

import asyncio

import pytest

import core.session_manager as sm
from conftest import HybridLlama, make_config, make_pool_backend
from core.session_manager import SessionManager, SessionState

STATIC = [50, 51, 52, 53]
OPENER = [88, 10]  # the prefilled "<think>\n" — the last tokens of every turn
TURN = [1, 2, 3] + OPENER
CLOSE = 99  # "</think>"
ANSWER = [200, 201]  # what the stripped answer re-tokenizes to
PIECES = {7: b"let me think ", 99: b"</think>", 8: b"ans", 9: b"wer"}


class _State:
    def __init__(self, tokens):
        self.tokens = list(tokens)
        self.n_tokens = len(tokens)
        self.llama_state_size = 1


class Inst(HybridLlama):
    def __init__(self, script):
        super().__init__(script)
        self.load_calls: list = []

    def load_state(self, st):  # a whole-state set: every cache invalidated
        self.load_calls.append(st)
        self._ctx._invalidate()
        self._ctx.cells, self._ctx.recr = list(st.tokens), list(st.tokens)
        self.n_tokens = st.n_tokens
        self.input_ids[: st.n_tokens] = st.tokens

    def detokenize(self, tokens, prev_tokens=None, special=False):
        return b"".join(PIECES.get(int(t), b"x") for t in tokens)


class _Renderer:
    def render_turn_transition_segments(self):
        return []

    def render_user_segments(self, prompt):
        return [(prompt, False)]

    def render_generation_prompt_segments(self, reasoning=None):
        return []

    def prefills_think_opener(self, reasoning=None):
        return True  # every turn starts inside a prefilled think

    def delimiter_pattern(self):
        return "</think>"

    def stop_tokens(self, mode=None):
        return []


def _segments_to_tokens(tok, segs):
    # The stripped replay is [(prefix, framing), (content, text)]; every
    # other call here renders a turn.
    return list(ANSWER) if segs and segs[0] == ("", True) else list(TURN)


def _span(family, think_on, gen_tokens, gen_start, tokenizer):
    # reasoning_span's qwen38 answer (unit-tested in test_session_framing):
    # back over the prefilled opener, replay prefix "".
    return (gen_start - len(OPENER), "") if CLOSE in gen_tokens else None


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    import llama_cpp

    monkeypatch.setattr(llama_cpp, "llama_token_is_eog", lambda vocab, t: t == 0)
    monkeypatch.setattr(sm, "_get_format_renderer", lambda family: _Renderer())
    monkeypatch.setattr(sm, "get_cached_tokenizer", lambda: object())
    monkeypatch.setattr(sm, "tokenize_segments", _segments_to_tokens)
    monkeypatch.setattr(sm, "reasoning_span", _span)


def _setup(script, strip=True):
    cfg = make_config(
        model_extra={
            "family": "qwen38",
            "thinking": "on",
            "strip_prior_reasoning": strip,
        }
    )
    backend = make_pool_backend(cfg)
    backend._turn_rollback = "partial"
    backend._static_states = {"default": _State(STATIC)}
    inst = Inst(list(script))
    inst.load_state(backend._static_states["default"])
    inst.load_calls.clear()
    mgr = SessionManager(backend)
    sess = SessionState(instance=inst)
    mgr._sessions["s1"] = sess
    return backend, mgr, inst, sess


def _turn(mgr, max_tokens=64):
    async def run():
        return [c async for c in mgr.session_turn("s1", "p", max_tokens=max_tokens)]

    return asyncio.run(run())


def test_strip_rolls_back_and_replays_only_this_turn():
    backend, mgr, inst, sess = _setup([7, CLOSE, 8, 9, 0])
    _turn(mgr)  # turn 0 — thinks, closes, answers
    kept = TURN[: len(TURN) - len(OPENER)]
    assert sess.last_turn_stripped is True
    assert sess.token_history == kept + ANSWER
    assert inst._ctx.cells == STATIC + kept + ANSWER, "the KV holds the stripped turn"
    assert backend.session_kv_matches(inst, sess.token_history)
    # The strip re-evaluated ONLY the turn's kept prompt + answer.
    assert inst.eval_calls[-1] == kept + ANSWER

    inst.eval_calls.clear()
    inst.load_calls.clear()
    _turn(mgr)  # turn 1 appends onto the stripped history
    assert inst.load_calls == [], "no static restore, no history replay"
    assert inst.eval_calls[0] == TURN
    assert sess.token_history == 2 * (kept + ANSWER)


def test_strip_off_commits_the_raw_turn():
    backend, mgr, inst, sess = _setup([7, CLOSE, 8, 9, 0], strip=False)
    _turn(mgr)
    assert sess.last_turn_stripped is False
    assert sess.token_history == TURN + [7, CLOSE, 8, 9]


def test_an_unterminated_think_is_dropped_not_committed():
    # Cut at the budget mid-thought: no close tag, no answer.
    backend, mgr, inst, sess = _setup([7, 7, 7, 7])
    _turn(mgr, max_tokens=3)
    assert sess.last_turn_dropped is True
    assert sess.token_history == [], "an answerless turn never enters history"
    assert sess.turn_count == 0
    assert inst._ctx.cells == STATIC, "and it is rolled out of the KV"
    assert backend.session_kv_matches(inst, sess.token_history)


def test_dropping_can_be_turned_off():
    backend, mgr, inst, sess = _setup([7, 7, 7, 7])
    backend.config.model.drop_answerless_turns = False
    _turn(mgr, max_tokens=3)
    assert sess.last_turn_dropped is False
    assert sess.token_history == TURN + [7, 7]  # what the KV holds (off-by-one)


def test_an_abandoned_turn_is_repaired_by_rollback_not_replay():
    backend, mgr, inst, sess = _setup([7, CLOSE, 8, 9, 0])
    _turn(mgr)
    history = list(sess.token_history)

    async def abandon():
        agen = mgr.session_turn("s1", "p", max_tokens=64)
        await agen.__anext__()
        await agen.aclose()

    asyncio.run(abandon())
    assert not backend.session_kv_matches(inst, history), "ghost span"

    inst.load_calls.clear()
    inst.eval_calls.clear()
    _turn(mgr)
    assert inst.load_calls == [], "repaired by rollback — no static restore"
    assert inst.eval_calls[0] == TURN, "and no history replay"
    assert backend._h_turn_ckpt_restores >= 1


def test_no_checkpoint_falls_back_to_the_replay_it_always_did():
    backend, mgr, inst, sess = _setup([7, CLOSE, 8, 9, 0])
    backend._turn_rollback = "none"
    _turn(mgr)
    history = list(sess.token_history)
    assert history == TURN + [7, CLOSE, 8, 9], "raw commit"
    inst.n_tokens += 1  # divergence, and no checkpoint to roll back to
    inst.load_calls.clear()
    inst.eval_calls.clear()
    _turn(mgr)
    assert len(inst.load_calls) == 1, "static restore"
    assert inst.eval_calls[0] == history + TURN, "then the history replay"


# ── rewindSessionTurn: one level of undo, by turn id ──────────────────


def _rewind(mgr, turn_id):
    return asyncio.run(mgr.rewind_turn("s1", turn_id))


def test_rewind_takes_the_last_turn_back_without_a_replay():
    backend, mgr, inst, sess = _setup([7, CLOSE, 8, 9, 0], strip=False)
    _turn(mgr)
    first = list(sess.token_history)
    _turn(mgr)
    tid = sess.last_turn.turn_id

    r = _rewind(mgr, tid)
    assert r["ok"] and r["reason"] == "rolled_back"
    assert sess.token_history == first and sess.turn_count == 1
    assert backend.session_kv_matches(inst, first), "KV back at the boundary"

    inst.load_calls.clear()
    inst.eval_calls.clear()
    _turn(mgr)  # the retry appends — no restore, no replay
    assert inst.load_calls == [] and inst.eval_calls[0] == TURN


def test_rewind_refuses_any_turn_but_the_last():
    backend, mgr, inst, sess = _setup([7, CLOSE, 8, 9, 0], strip=False)
    _turn(mgr)
    old = sess.last_turn.turn_id
    _turn(mgr)
    r = _rewind(mgr, old)
    assert not r["ok"] and r["reason"] == "not_last_turn"
    assert sess.turn_count == 2


def test_a_dropped_turn_is_already_absent():
    # The server dropped the answerless turn at commit; a blind rewind of
    # "the last turn" must not take the good turn before it.
    backend, mgr, inst, sess = _setup([7, 7, 7, 7])
    _turn(mgr, max_tokens=3)
    r = _rewind(mgr, sess.last_turn.turn_id)
    assert r["ok"] and r["reason"] == "already_absent"
    assert sess.token_history == [] and sess.turn_count == 0


def test_rewind_without_a_checkpoint_truncates_and_replays_next_turn():
    backend, mgr, inst, sess = _setup([7, CLOSE, 8, 9, 0], strip=False)
    backend._turn_rollback = "none"
    _turn(mgr)
    first = list(sess.token_history)
    _turn(mgr)
    r = _rewind(mgr, sess.last_turn.turn_id)
    assert r["ok"] and r["reason"] == "history_truncated"
    assert sess.token_history == first and sess.kv_dirty is True

    inst.load_calls.clear()
    inst.eval_calls.clear()
    _turn(mgr)
    assert len(inst.load_calls) == 1 and inst.eval_calls[0] == first + TURN
