"""Factor-4 reasoning strip — the guards, the KV surgery, and the wiring.

WHY THIS FILE EXISTS. `_maybe_strip_reasoning` (core/session_manager.py) had
ZERO coverage: it was the range 855-908 in the missing set. What stood in the
way was not difficulty but a pair of `LLMVP_THINK_STRIP=0` monkeypatches that
TESTING.md described as "disabled in every session test". Both turned out to
be vacuous — one was already gated off by `resident_strip_reasoning=False`,
and the other's stated reason ("needs a real tokenizer") was stale: the fake
returned at the precondition guard long before any tokenizer was touched.
Removing them (C8) moved 855-858 from missed to hit on its own. This file
covers the rest.

WHAT IT PROTECTS. The strip rewrites live KV: it truncates from `t0` to the
end and re-evals a clean answer there. Every guard below is a reason NOT to
do that, and the empty-content one carries a real incident in its source
comment — stripping a truncated turn writes an ANSWERLESS assistant turn into
the KV, and that compounds across turns. An untested guard whose failure mode
is silent, cumulative context corruption is the highest-value gap the
consolidation survey found.

LAYERS. The manager decides WHETHER to strip (`_maybe_strip_reasoning`); the
backend performs it (`LlamaCppBackend.strip_reasoning_replay`). Both are
tested here because a guard on either side alone is not the contract.
"""

from __future__ import annotations

import pytest

import core.session_manager as sm
from core.session_manager import SessionManager
from inference.backends.llama_cpp_backend import LlamaCppBackend
from tests.conftest import FakeTok, make_instance, make_config

# ── Manager: WHETHER to strip ─────────────────────────────────────────────


class _Backend:
    """Just enough backend for _maybe_strip_reasoning: a config and a
    recording strip_reasoning_replay."""

    def __init__(self, config):
        self.config = config
        self.calls: list[tuple] = []

    def strip_reasoning_replay(self, instance, t0, replay):
        self.calls.append((t0, list(replay)))
        return True


def _manager(family: str = "chatml", thinking: bool = True) -> SessionManager:
    cfg = make_config(model_extra={"family": family, "thinking": thinking})
    mgr = SessionManager.__new__(SessionManager)
    mgr._backend = _Backend(cfg)
    return mgr


def _instance(gen_tokens=(88, 10, 5, 6, 99, 7), gen_start_pos=20):
    """An instance carrying a completed generation the strip could act on."""
    inst = make_instance()
    inst._last_completion_tokens = list(gen_tokens) if gen_tokens is not None else None
    inst._last_gen_start_pos = gen_start_pos
    return inst


@pytest.fixture(autouse=True)
def _tokenizer(monkeypatch):
    monkeypatch.setattr(sm, "get_cached_tokenizer", lambda: FakeTok())
    monkeypatch.setattr(sm, "tokenize_segments", lambda tok, segs: [7, 7, 7])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gen_tokens,gen_start_pos,content,why",
    [
        pytest.param(
            None,
            20,
            "the answer",
            "no completion tokens recorded",
            id="no_last_completion_tokens",
        ),
        pytest.param(
            (),
            20,
            "the answer",
            "empty completion",
            id="empty_completion_tokens",
        ),
        pytest.param(
            (88, 10, 99),
            None,
            "the answer",
            "no recorded generation start",
            id="no_gen_start_pos",
        ),
        # THE INCIDENT GUARD. A turn truncated before emitting a final-channel
        # answer (max_tokens hit mid-analysis) has no clean answer to replay.
        # Stripping would replace the raw output with an EMPTY final channel —
        # an answerless assistant turn written into the KV, compounding across
        # every later turn in the session.
        pytest.param(
            (88, 10, 99),
            20,
            "",
            "truncated turn, no answer to replay",
            id="empty_content_is_the_incident_guard",
        ),
        pytest.param(
            (88, 10, 99),
            20,
            "   \n\t ",
            "whitespace-only is equally answerless",
            id="whitespace_only_content",
        ),
    ],
)
async def test_strip_is_skipped(gen_tokens, gen_start_pos, content, why):
    mgr = _manager()
    inst = _instance(gen_tokens, gen_start_pos)
    await mgr._maybe_strip_reasoning(inst, content)
    assert mgr._backend.calls == [], f"must not touch the KV — {why}"


@pytest.mark.asyncio
async def test_strip_skipped_when_family_has_no_reasoning_span():
    """A non-thinking turn yields no span, so there is nothing to excise."""
    mgr = _manager(thinking=False)
    await mgr._maybe_strip_reasoning(_instance(), "the answer")
    assert mgr._backend.calls == []


@pytest.mark.asyncio
async def test_strip_replays_the_clean_answer_at_the_span_start():
    """Happy path: the backend is handed the planned truncation point and the
    tokenized clean answer."""
    mgr = _manager()
    await mgr._maybe_strip_reasoning(_instance(), "the answer")
    assert len(mgr._backend.calls) == 1
    t0, replay = mgr._backend.calls[0]
    assert isinstance(t0, int)
    assert replay == [7, 7, 7]


# ── Backend: the KV surgery itself ────────────────────────────────────────


def _backend(decode_mode: str = "pool") -> LlamaCppBackend:
    # Config validation refuses batched without the resident single-context
    # seq machinery, so the batched arm has to carry the real trio rather
    # than a bare flag — that coupling is itself part of the contract.
    extra = (
        {"resident_seq_cache": True, "swa_full": True, "kv_unified": True}
        if decode_mode == "batched"
        else None
    )
    return LlamaCppBackend(make_config(decode_mode=decode_mode, model_extra=extra))


def test_replay_truncates_then_re_evals():
    """The two validated primitives, in order: tail rm at t0, then eval."""
    backend = _backend()
    inst = make_instance(n_tokens=8)
    assert backend.strip_reasoning_replay(inst, 5, [61, 62]) is True
    assert inst._ctx.ops == [("rm", 0, 5, -1)]
    assert inst.eval_calls == [[61, 62]]
    # n_tokens is truncated to t0 and then advanced by the replay.
    assert inst.n_tokens == 7


def test_replay_with_no_tokens_truncates_without_eval():
    backend = _backend()
    inst = make_instance(n_tokens=8)
    assert backend.strip_reasoning_replay(inst, 5, []) is True
    assert inst._ctx.ops == [("rm", 0, 5, -1)]
    assert inst.eval_calls == []
    assert inst.n_tokens == 5


@pytest.mark.parametrize(
    "t0,why",
    [
        pytest.param(8, "t0 at the end leaves nothing to strip", id="t0_at_end"),
        pytest.param(99, "t0 past the end", id="t0_beyond_end"),
        pytest.param(-1, "negative t0", id="t0_negative"),
    ],
)
def test_replay_refuses_out_of_range_t0(t0, why):
    backend = _backend()
    inst = make_instance(n_tokens=8)
    assert backend.strip_reasoning_replay(inst, t0, [61]) is False, why
    assert inst._ctx.ops == [], "must not touch the KV on a refused strip"


def test_replay_is_a_noop_for_hybrid_recurrent_models():
    """memory_can_shift() False (Qwen3.5/Qwen3-Next): the memory is a fixed
    recurrent state, not a removable KV span. Excision would corrupt it."""
    backend = _backend()
    inst = make_instance(n_tokens=8, can_shift=False)
    assert backend.strip_reasoning_replay(inst, 5, [61]) is False
    assert inst._ctx.ops == []


def test_replay_is_deferred_in_batched_mode():
    """Batched v1 has no prefill-only path on a seat's seq, so the strip is
    pool-only. Sessions simply keep raw turns rather than corrupt a seat."""
    backend = _backend(decode_mode="batched")
    inst = make_instance(n_tokens=8)
    assert backend.strip_reasoning_replay(inst, 5, [61]) is False
    assert inst._ctx.ops == []
