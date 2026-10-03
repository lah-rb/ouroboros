"""DFlash speculative decoding in the batched engine (2026-10-02).

The engine drafts k tokens per covered DECODING stream, appends them after the
stream's last token, then samples row by row and keeps going while the sampled
token IS the next draft (llama-server's sample-and-accept, distribution
preserving). Positions are booked before each token is consumed and rows past
the last consumed token are rolled back -- mirrored onto the drafter's KV by
the attachment. Driven with fakes: no llama_cpp, no GPU.
"""

from __future__ import annotations

import pytest

from tests.test_batched_engine import (
    EOG,
    FakeCtx,
    FakeSampler,
    _engine_with,
    _req,
)


class FakeSpec:
    enabled = True
    n_max = 3

    def __init__(self, drafts=(), covered=True, raise_on_draft=False):
        self.drafts = [list(d) for d in drafts]
        self.cov = covered
        self.raise_on_draft = raise_on_draft
        self.items = []
        self.records = []
        self.kinds = []

    def covered(self, seq, n_past):
        return self.cov

    def draft(self, items):
        if self.raise_on_draft:
            raise RuntimeError("drafter blew up")
        self.items.append(list(items))
        out = {}
        for seq, n_past, last, k in items:
            out[seq] = self.drafts.pop(0)[:k] if self.drafts else []
        return out

    def record(self, drafted, accepted, kind="default"):
        self.records.append((drafted, accepted))
        self.kinds.append(kind)


def _running(spec, script, *, max_tokens=16, decode_script=None):
    """A stream past prefill: prompt [100,101,102] on seq 0 at positions 2..4,
    first sampled token 10 (the stream's last_token)."""
    ctx = FakeCtx(decode_script)
    sampler = FakeSampler([10, *script])
    eng = _engine_with(ctx, samplers={"a": sampler})
    eng._spec = spec
    req = _req("a", [100, 101, 102], max_tokens=max_tokens)
    req._stream_id = "a"
    eng._admit(req)
    eng._step()  # prefill; samples 10
    return eng, ctx, sampler, req


def test_full_acceptance_emits_every_draft_plus_the_bonus_token():
    spec = FakeSpec(drafts=[[11, 12, 13]])
    eng, ctx, sampler, req = _running(spec, [11, 12, 13, 14])
    eng._step()
    assert ctx.decoded_batches[1] == [
        (10, 5, (0,), True),
        (11, 6, (0,), True),
        (12, 7, (0,), True),
        (13, 8, (0,), True),
    ]
    s = eng._streams["a"]
    assert sampler.sampled_at[1:] == [0, 1, 2, 3]
    assert s.completion_tokens == [10, 11, 12, 13, 14]
    assert s.last_token == 14 and s.n_past == 9
    assert s.slot.input_ids[-4:] == [10, 11, 12, 13]
    assert ctx.seq_rm_calls == [], "nothing to roll back"
    assert spec.records == [(3, 3)]
    assert spec.items == [[(0, 5, 10, 3)]]


def test_a_rejected_draft_rolls_back_the_rows_past_the_correction():
    spec = FakeSpec(drafts=[[11, 99, 98]])
    eng, ctx, sampler, req = _running(spec, [11, 12, 15])
    eng._step()
    s = eng._streams["a"]
    assert s.completion_tokens == [10, 11, 12]
    assert s.last_token == 12
    assert s.n_past == 7, "history: 10@5, 11@6; 12 is not fed yet"
    assert ctx.seq_rm_calls == [(0, 7, -1)]
    assert s.slot.input_ids[-2:] == [10, 11]
    assert spec.records == [(3, 1)]
    eng._step()  # the next step feeds 12 at 7 (drafts exhausted -> plain)
    assert ctx.decoded_batches[2][0] == (12, 7, (0,), True)


def test_eog_inside_an_accepted_run_books_the_position_before_it():
    spec = FakeSpec(drafts=[[11, EOG, 13]])
    eng, ctx, sampler, req = _running(spec, [11, EOG])
    eng._step()
    assert req.out.done and req.out.error is None
    assert "a" not in eng._streams
    slot = req.slot
    assert slot.n_tokens == 7, "10@5 and 11@6 are history; EOG never is"
    assert ctx.seq_rm_calls == [(0, 7, -1)]
    assert spec.records == [(3, 2)]


def test_drafts_never_run_past_the_stream_budget():
    spec = FakeSpec(drafts=[[11, 12, 13]])
    # budget 3: 10 is already out, so at most 3 - 1 - 1 = 1 draft
    eng, ctx, sampler, req = _running(spec, [11, 12], max_tokens=3)
    eng._step()
    assert spec.items == [[(0, 5, 10, 1)]]
    assert req.out.done, "10, 11, 12 = the whole budget"
    assert req.slot._last_end_reason == "length"


def test_an_uncovered_seq_decodes_plainly():
    spec = FakeSpec(drafts=[[11, 12, 13]], covered=False)
    eng, ctx, sampler, req = _running(spec, [11])
    eng._step()
    assert ctx.decoded_batches[1] == [(10, 5, (0,), True)]
    assert spec.items == [] and spec.records == []


def test_kv_pressure_rolls_the_drafts_back_with_the_step():
    spec = FakeSpec(drafts=[[11, 12, 13]])
    eng, ctx, sampler, req = _running(spec, [11, 12, 13, 14], decode_script=[0, 1])
    eng._step()  # pressure: every row of the step (drafts included) is undone
    assert ctx.seq_rm_calls[0] == (0, 5, -1)
    # nothing left to shrink, so the ladder force-windows the stream -- at the
    # position BEFORE the step, with no drafted token left in its history
    assert req.out.done and req.slot._last_end_reason == "kv_pressure_truncated"
    assert req.slot.n_tokens == 5
    assert req.slot.input_ids[-1] == 102
    assert spec.records == []


def test_a_drafter_exception_never_fails_the_stream():
    spec = FakeSpec(raise_on_draft=True)
    eng, ctx, sampler, req = _running(spec, [11])
    eng._step()
    assert ctx.decoded_batches[1] == [(10, 5, (0,), True)]
    assert eng._streams["a"].completion_tokens == [10, 11]


# ── the attachment: decode hook + KV mirror (no model) ───────────────


class _Ctx:
    def __init__(self, ret=0):
        self.ret = ret
        self.calls = []

    def decode(self, batch):
        self.calls.append(("decode", batch))
        return self.ret

    def memory_seq_rm(self, *a):
        self.calls.append(("rm", a))
        return True

    def memory_seq_cp(self, *a):
        self.calls.append(("cp", a))

    def memory_seq_add(self, *a):
        self.calls.append(("add", a))

    def memory_seq_div(self, *a):
        self.calls.append(("div", a))

    def memory_seq_keep(self, *a):
        self.calls.append(("keep", a))

    def memory_clear(self, *a):
        self.calls.append(("clear", a))


def _bare_drafter():
    from inference.speculative_dflash import DFlashDrafter

    d = object.__new__(DFlashDrafter)
    d.enabled = True
    d.disabled_reason = ""
    d.h_failures = 0
    d._originals = {}
    d.target = None
    d.ctx = _Ctx()
    d.processed = []
    d.process = lambda batch: d.processed.append(batch)
    return d


def test_the_attachment_injects_after_good_decodes_and_mirrors_every_kv_op():
    d = _bare_drafter()
    tgt = _Ctx()
    d._attach(tgt)
    assert tgt.decode("B1") == 0 and d.processed == ["B1"]
    tgt.ret = 1
    assert tgt.decode("B2") == 1 and d.processed == ["B1"], "no inject on pressure"
    tgt.memory_seq_rm(3, 10, -1)
    tgt.memory_seq_cp(8, 3, -1, -1)
    tgt.memory_seq_add(3, 20, 40, -10)
    tgt.memory_clear(True)
    assert d.ctx.calls == [
        ("rm", (3, 10, -1)),
        ("cp", (8, 3, -1, -1)),
        ("add", (3, 20, 40, -10)),
        ("clear", (True,)),
    ]
    d._detach()
    tgt.memory_seq_rm(1, 0, -1)
    assert len(d.ctx.calls) == 4, "detached: no more mirroring"
    assert type(tgt).decode is _Ctx.decode and "decode" not in vars(tgt)


def test_a_process_fault_never_reaches_the_target_and_eventually_disables():
    from inference.speculative_dflash import MAX_FAILURES

    d = _bare_drafter()

    def boom(batch):
        raise RuntimeError("inject failed")

    d.process = boom
    tgt = _Ctx()
    d._attach(tgt)
    for _ in range(MAX_FAILURES):
        assert tgt.decode("B") == 0
    assert not d.enabled and "inject failed" in d.disabled_reason


def test_covered_rows_skips_a_seq_the_drafter_lost():
    from inference.speculative_dflash import covered_rows

    pos_max = {0: 4, 1: 9, 2: -1}.__getitem__
    rows = [(5, [0]), (6, [0]), (12, [1]), (13, [1]), (0, [2]), (1, [2])]
    assert covered_rows(rows, pos_max) == [0, 1, 4, 5], "seq 1 has a gap at 10"


# ── config ────────────────────────────────────────────────────────────


def test_dflash_config_rules():
    from core.config import Config
    from tests.test_batched_engine import _BATCHED_MODEL_FLAGS, _cfg

    ok = Config.model_validate(
        _cfg(
            "batched",
            {
                **_BATCHED_MODEL_FLAGS,
                "speculative_type": "dflash",
                "speculative_draft_model": "/m/dflash.gguf",
            },
        )
    )
    assert ok.model.speculative_n_max == 3
    assert ok.model.speculative_draft_cache_type == "q8_0"
    with pytest.raises(ValueError, match="batched"):
        Config.model_validate(
            _cfg(
                "pool",
                {"speculative_type": "dflash", "speculative_draft_model": "/m/d.gguf"},
            )
        )
    with pytest.raises(ValueError, match="speculative_draft_model"):
        Config.model_validate(
            _cfg("batched", {**_BATCHED_MODEL_FLAGS, "speculative_type": "dflash"})
        )
    with pytest.raises(ValueError, match="only 'dflash'"):
        Config.model_validate(
            _cfg(
                "batched",
                {**_BATCHED_MODEL_FLAGS, "speculative_type": "eagle3",
                 "speculative_draft_model": "/m/d.gguf"},  # fmt: skip
            )
        )


def test_every_draft_row_is_an_output_the_anchor_included():
    """Live 2026-10-02: with the anchor row's logits off, llama.cpp dropped it
    before the drafter's last layer; the non-causal block then predicted one
    position behind ([t1, t1, t2]) and acceptance fell from 54% to 20%."""
    from types import SimpleNamespace

    d = _bare_drafter()
    rows = []

    class _B:
        def __init__(self):
            self.batch = SimpleNamespace(n_tokens=0, pos=[0] * 64)

        def reset(self):
            self.batch.n_tokens = 0
            rows.clear()

        def add_token(self, token, pos, seqs, logits):
            self.batch.pos[self.batch.n_tokens] = pos
            self.batch.n_tokens += 1
            rows.append((token, pos, tuple(seqs), logits))

    d._b_draft = _B()
    d.n_batch = 64
    d.mask = 999
    d.ctx = SimpleNamespace(ctx=object(), memory_seq_rm=lambda *a: True)
    d._lc = SimpleNamespace(llama_decode=lambda ctx, batch: 1)  # stop after building
    d.h_failures = 0
    d.draft([(4, 50, 7, 3)])
    assert rows == [
        (7, 50, (4,), True),
        (999, 51, (4,), True),
        (999, 52, (4,), True),
        (999, 53, (4,), True),
    ]


def test_each_verify_step_is_counted_under_its_stream_kind():
    spec = FakeSpec(drafts=[[11, 12, 13]])
    eng, ctx, sampler, req = _running(spec, [11, 12, 13, 14])
    eng._step()
    assert spec.kinds == [req.persona or "default"]


def test_stats_split_acceptance_by_kind():
    """2026-10-03: the pooled rate could not say whether vision answers draft
    as well as prose; the stats line now carries both."""
    d = _bare_drafter()
    d.n_max = 3
    d.h_drafted = d.h_accepted = d.h_steps = 0
    d.h_uncovered = d.h_injected_rows = d.h_skipped_rows = 0
    d.h_by_kind = {}
    for acc in (3, 3, 1):
        d.record(3, acc, "default")
    for acc in (0, 2):
        d.record(3, acc, "vision")
    st = d.stats()
    assert st["accepted"] == 9 and st["verify_steps"] == 5
    v = st["by_kind"]["vision"]
    assert v["drafted"] == 6 and v["accepted"] == 2 and v["accept_rate"] == 0.3333
    assert v["tokens_per_step"] == 2.0, "(2 accepted + 2 bonus) / 2 steps"
    assert v["accepted_hist"] == [1, 0, 1, 0]
    assert st["by_kind"]["default"]["accepted_hist"] == [0, 1, 0, 2]
