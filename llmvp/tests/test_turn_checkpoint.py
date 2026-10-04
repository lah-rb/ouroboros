"""Per-turn recurrent-state checkpoint: roll a hybrid session turn back.

The mechanism (inference/turn_checkpoint.py): capture the recurrent state with
PARTIAL_ONLY at the turn boundary; to roll back, restore it FIRST — the tail
rm is refused while the recurrent cell sits past the boundary — then remove
the attention/indexer tail. Every refusal happens before any mutation; every
failure after the first mutation resets the instance (a half-restored hybrid
KV is the one state nothing downstream can detect).
"""

from __future__ import annotations

import pytest

from conftest import HybridCtx, HybridLlama, make_pool_backend
from inference.turn_checkpoint import (
    MODE_PARTIAL,
    MODE_SEQ_RM,
    RestoreOutcome,
    TurnCheckpointStore,
)

PROMPT = [10, 11, 12, 13, 14]


def _inst_at(n=5):
    inst = HybridLlama([7, 8, 0])
    inst.eval(PROMPT[:n])
    return inst


def _mutations(ctx):
    return [op for op in ctx.ops if op[0] in ("set", "rm", "clear")]


def test_the_fake_refuses_a_naive_tail_rm_like_the_real_memory():
    inst = _inst_at()
    inst.eval([20, 21])
    assert inst._ctx.memory_seq_rm(0, 5, -1) is False
    assert inst._ctx.cells == PROMPT + [20, 21], "refusal changes nothing"


def test_restore_then_rm_rolls_the_turn_back_exactly():
    inst = _inst_at()
    store = TurnCheckpointStore(MODE_PARTIAL)
    ck = store.capture(inst)
    assert ck is not None and ck.pos == 5 and ck.nbytes == 64
    inst.eval([20, 21, 22])  # the turn

    inst._ctx.ops.clear()
    assert store.restore(inst, 5) is RestoreOutcome.OK
    assert [op[0] for op in _mutations(inst._ctx)] == ["set", "rm"]
    assert inst._ctx.cells == PROMPT and inst._ctx.recr == PROMPT
    assert inst.n_tokens == 5


def test_the_checkpoint_survives_its_own_rollback():
    # set_data_ext invalidates registered caches — the store must re-arm, or
    # a second rollback of the same boundary (a retried turn) would fail.
    inst = _inst_at()
    store = TurnCheckpointStore(MODE_PARTIAL)
    store.capture(inst)
    for tail in ([20, 21], [30]):
        inst.eval(tail)
        assert store.restore(inst, 5) is RestoreOutcome.OK
    assert inst._ctx.cells == PROMPT


@pytest.mark.parametrize(
    "spoil",
    ["no_checkpoint", "other_pos", "context_replaced", "prefix_changed", "size"],
)
def test_refusals_touch_nothing(spoil):
    inst = _inst_at()
    store = TurnCheckpointStore(MODE_PARTIAL)
    if spoil != "no_checkpoint":
        store.capture(inst)
    inst.eval([20, 21])
    pos = 5
    if spoil == "other_pos":
        pos = 4
    elif spoil == "context_replaced":
        fresh = HybridCtx()
        fresh.absorb(inst._ctx.cells)
        inst._ctx = fresh
    elif spoil == "prefix_changed":
        inst.input_ids[2] = 99
    elif spoil == "size":
        inst._ctx.state_size = 65
    before = (list(inst._ctx.cells), list(inst._ctx.recr), inst.n_tokens)
    inst._ctx.ops.clear()
    assert store.restore(inst, pos) is RestoreOutcome.REFUSED_UNTOUCHED
    assert _mutations(inst._ctx) == []
    assert (inst._ctx.cells, inst._ctx.recr, inst.n_tokens) == before


def test_a_failed_write_resets_instead_of_leaving_a_half_restored_kv():
    inst = _inst_at()
    store = TurnCheckpointStore(MODE_PARTIAL)
    store.capture(inst)
    inst.eval([20, 21])
    inst._ctx.short_write = True
    assert store.restore(inst, 5) is RestoreOutcome.FAILED_RESET
    assert inst.n_tokens == 0 and inst._ctx.cells == []
    assert store.checkpoint is None


def test_short_read_never_arms():
    inst = _inst_at()
    inst._ctx.short_read = True
    store = TurnCheckpointStore(MODE_PARTIAL)
    assert store.capture(inst) is None
    assert store.restore(inst, 5) is RestoreOutcome.REFUSED_UNTOUCHED


def test_a_cleared_context_invalidates_the_checkpoint():
    inst = _inst_at()
    store = TurnCheckpointStore(MODE_PARTIAL)
    store.capture(inst)
    inst.reset()  # memory_clear — the binding's reset, load_state, a bad decode
    assert store.checkpoint is None


def test_seq_rm_mode_never_serializes_state():
    # PARTIAL_ONLY on a plain attention cache is IGNORED by llama.cpp and
    # would serialize the whole KV — so this mode must never ask for state.
    inst = HybridLlama([7, 0])
    inst._ctx = HybridCtx(recurrent=False)  # attention-only: the rm is exact
    inst.eval(PROMPT)
    store = TurnCheckpointStore(MODE_SEQ_RM)
    store.capture(inst)
    inst.eval([20])
    assert store.restore(inst, 5) is RestoreOutcome.OK
    assert inst._ctx.cells == PROMPT
    assert not [op for op in inst._ctx.ops if op[0] in ("size", "get", "set")]


# ── backend wiring ────────────────────────────────────────────────────


@pytest.fixture
def eog(monkeypatch):
    import llama_cpp

    monkeypatch.setattr(llama_cpp, "llama_token_is_eog", lambda vocab, t: t == 0)


def _partial_backend():
    backend = make_pool_backend()
    backend._turn_rollback = MODE_PARTIAL
    return backend


def test_replay_turn_checkpoints_at_the_boundary_inside_its_prompt(eog):
    backend = _partial_backend()
    inst = HybridLlama([7, 8, 0])
    history, turn = [40, 41, 42], [1, 2]
    list(
        backend.generate_stream_sync(
            inst,
            history + turn,
            64,
            0.7,
            stop_texts=[],
            static_in_prompt=False,
            turn_checkpoint_at=3,
        )
    )
    assert inst.eval_calls[0] == history, "history first, alone"
    assert inst.eval_calls[1] == turn, "then generate() gets the turn"
    assert inst._turn_ckpt_ok is True
    assert backend.turn_checkpoint_pos(inst) == 3
    assert backend._h_turn_ckpt_saves == 1

    # And the turn rolls back to exactly the history.
    assert backend.rollback_turn(inst, 3) is True
    assert inst._ctx.cells == history and inst.n_tokens == 3
    assert backend._h_turn_ckpt_restores == 1


def test_append_turn_checkpoints_the_live_position_without_splitting(eog):
    backend = _partial_backend()
    inst = HybridLlama([7, 0])
    inst.eval([40, 41])
    list(
        backend.generate_stream_sync(
            inst,
            [1, 2],
            64,
            0.7,
            stop_texts=[],
            static_in_prompt=False,
            turn_checkpoint_at=2,
        )
    )
    assert inst.eval_calls[-2] == [1, 2], "no split: the prompt evaluates whole"
    assert backend.turn_checkpoint_pos(inst) == 2


def test_no_rollback_mode_means_no_checkpoint_and_false(eog):
    backend = make_pool_backend()  # _turn_rollback "none" until resolved
    inst = HybridLlama([7, 0])
    list(
        backend.generate_stream_sync(
            inst,
            [1, 2],
            64,
            0.7,
            stop_texts=[],
            static_in_prompt=False,
            turn_checkpoint_at=0,
        )
    )
    assert inst._turn_ckpt_ok is False
    assert backend.rollback_turn(inst, 0) is False
    assert not [op for op in inst._ctx.ops if op[0] in ("size", "get", "set")]


def test_release_forgets_the_checkpoint():
    backend = _partial_backend()
    inst = _inst_at()
    backend._turn_store(inst).capture(inst)
    backend.drop_turn_checkpoint(inst)
    assert backend.turn_checkpoint_pos(inst) is None


def test_resolve_mode_hybrid_is_partial_batched_is_none():
    from types import SimpleNamespace

    backend = make_pool_backend()
    backend._primary_instance = SimpleNamespace(_ctx=HybridCtx())
    backend._is_hybrid = True
    backend._resolve_turn_rollback()
    assert backend._turn_rollback == "partial"

    backend._decode_mode = "batched"
    backend._resolve_turn_rollback()
    assert backend._turn_rollback == "none"


def test_kill_switch_resolves_none(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("LLMVP_TURN_ROLLBACK", "0")
    backend = make_pool_backend()
    backend._primary_instance = SimpleNamespace(_ctx=HybridCtx())
    backend._is_hybrid = True
    backend._resolve_turn_rollback()
    assert backend._turn_rollback == "none"
