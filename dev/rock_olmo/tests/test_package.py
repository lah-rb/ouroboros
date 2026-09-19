"""Packing: paragraph-bounded blocks, EOS per document, stage-2 masks, determinism.
Runs under the rock venv (numpy; tokenizer not needed — synthetic ids)."""

import json
import os
import random
import tempfile

import numpy as np

from package import Packer, expand_repeats, first_fit_bins, write_shards
from packed_dataset import EOS_ID, PAD_ID, shards_for


def _para(n, base=1000):
    return [base + i for i in range(n)]


def test_paragraphs_never_split_and_eos_once_per_document():
    pk = Packer(seq=16)
    pk.add_document(
        "a", [_para(6), _para(6), _para(6)]
    )  # 6+6 fit, third overflows -> new block
    pk.add_document("b", [_para(3)])
    pk.finish()
    b0, b1 = pk.blocks
    assert list(b0[:12]) == _para(6) + _para(6) and all(x == PAD_ID for x in b0[12:])
    assert list(b1[:7]) == _para(6) + [EOS_ID] and list(b1[7:11]) == _para(3) + [EOS_ID]
    assert sum(1 for blk in pk.blocks for x in blk if x == EOS_ID) == 2
    assert pk.pad_tokens == 4 + 5 and pk.hard_cuts == 0
    assert all(m == 1 for m in pk.masks[0][:12]) and all(
        m == 0 for m in pk.masks[0][12:]
    )


def test_oversized_paragraph_is_hard_cut_and_counted():
    pk = Packer(seq=8)
    pk.add_document("big", [_para(20)])
    pk.finish()
    assert pk.hard_cuts == 1 and len(pk.blocks) == 3
    flat = [x for blk in pk.blocks for x in blk if x != PAD_ID]
    assert flat == _para(20) + [EOS_ID]


def _blocks_holding(pk, doc_id):
    return [i for i, spans in enumerate(pk.spans) if any(s[0] == doc_id for s in spans)]


def test_atomic_document_never_straddles():
    # default rule: 11 tokens in block 0, then b's first paragraph (4) fits
    # and its second (4+EOS) does not -> b straddles two blocks
    pk = Packer(seq=16)
    pk.add_document("a", [_para(10)])
    pk.add_document("b", [_para(4), _para(4)])
    pk.finish()
    assert _blocks_holding(pk, "b") == [0, 1]
    # atomic: b (9 tokens with EOS) fits a block but not the remainder -> flush first
    pk = Packer(seq=16)
    pk.add_document("a", [_para(10)])
    pk.add_document("b", [_para(4), _para(4)], atomic=True)
    pk.finish()
    assert _blocks_holding(pk, "b") == [1] and pk.atomic_overflow == 0
    assert list(pk.blocks[1][:9]) == _para(4) + _para(4) + [EOS_ID]
    # longer than a block: falls back to the paragraph rule, counted
    pk = Packer(seq=16)
    pk.add_document("c", [_para(10), _para(10)], atomic=True)
    pk.finish()
    assert pk.atomic_overflow == 1 and _blocks_holding(pk, "c") == [0, 1]


def test_first_fit_bins_fill_blocks_and_isolate_oversize():
    rng = random.Random(3)
    lengths = [rng.randint(100, 2000) for _ in range(5000)] + [5000]
    bins = first_fit_bins(lengths, 4096)
    assert sorted(i for b in bins for i in b) == list(range(len(lengths)))
    assert all(sum(lengths[i] for i in b) <= 4096 for b in bins if len(b) > 1)
    assert [5000] in [[lengths[i] for i in b] for b in bins]  # oversize alone
    used = sum(lengths[:-1])
    pad = len([b for b in bins if lengths[b[0]] <= 4096]) * 4096 - used
    assert pad / (used + pad) < 0.05  # sequential packing of the same sizes pads ~15 %
    # every bin is packable as a whole by the Packer (atomic docs never straddle)
    pk = Packer(seq=4096)
    for b in bins[:50]:
        for i in b:
            pk.add_document(f"d{i}", [_para(lengths[i] - 1)], atomic=True)
        pk.finish()
    assert all(len(_blocks_holding(pk, f"d{i}")) == 1 for b in bins[:50] for i in b if lengths[i] <= 4096)


def test_stage2_masks_prompt_and_trains_completion_plus_eos():
    pk = Packer(seq=12)
    assert pk.add_example("x", [1, 2, 3], [7, 8])
    assert not pk.add_example("too_big", list(range(20)), [1])
    pk.finish()
    ids, mask = pk.blocks[0], pk.masks[0]
    assert list(ids[:6]) == [1, 2, 3, 7, 8, EOS_ID] and list(mask[:6]) == [
        0,
        0,
        0,
        1,
        1,
        1,
    ]
    assert all(m == 0 for m in mask[6:])


def test_repeats_and_caps_are_applied_per_source():
    docs = [
        {"doc_id": f"d{i}", "source": "hom" if i < 3 else "replay/x", "max_repeats": 3}
        for i in range(6)
    ]
    toks = [100] * 6
    idx, acct = expand_repeats(
        docs, toks, {"replay/x": {"repeats": 1, "cap_tokens": 150}}, random.Random(1)
    )
    assert acct["hom"]["repeats"] == 3 and acct["hom"]["weighted_tokens"] == 900
    assert acct["replay/x"]["docs"] == 1 and acct["replay/x"]["weighted_tokens"] == 100
    assert len(idx) == 9 + 1


def test_shards_round_trip_and_are_deterministic():
    def build(seed):
        pk = Packer(seq=8)
        for i in range(30):
            pk.add_document(f"d{i}", [_para(3, base=i * 10)])
        pk.finish()
        d = tempfile.mkdtemp()
        st = write_shards(d, "train", pk, random.Random(seed))
        return d, st, pk

    d1, st1, _ = build(7)
    d2, st2, _ = build(7)
    s1 = shards_for(d1, "train", 8)
    s2 = shards_for(d2, "train", 8)
    assert st1 == st2 and np.array_equal(np.asarray(s1[0].ids), np.asarray(s2[0].ids))
    assert s1[0].mask is not None and s1[0].mask.shape == s1[0].ids.shape
    spans = json.load(open(os.path.join(d1, "train-000.idx.json")))
    assert len(spans) == len(s1[0]) and all(
        isinstance(sp[0], str) for blk in spans for sp in blk
    )
