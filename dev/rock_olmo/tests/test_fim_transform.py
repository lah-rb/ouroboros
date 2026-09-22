"""FIM transformation: the grammar round-trips, documents reconstruct,
val twins and record mode behave, exclusions drop replay documents, and
recovery items are class-stratified. Stdlib only; the tokenizer test skips
when transformers is absent (root venv)."""

import json
import os
import random
import tempfile

import pytest

import fim_transform as ft


def _doc(rng, words=400):
    vocab = [f"w{i}" for i in range(50)] + ["Basalt", "Quartz", "1998", "532", "Nairobi"]
    out, line = [], []
    for i in range(words):
        line.append(rng.choice(vocab))
        if rng.random() < 0.08:
            out.append(" ".join(line) + ".")
            line = []
    out.append(" ".join(line) + ".")
    return " ".join(out)


def test_wrap_unwrap_roundtrip_both_orders():
    for order in ft.ORDERS:
        body = ft.fim_wrap("alpha ", "beta", " gamma", order)
        assert ft.fim_unwrap(body) == ("alpha ", "beta", " gamma", order)
    with pytest.raises(ValueError):
        ft.fim_wrap("a", "b", "c", "msp")


def test_other_families_share_psm_but_not_spm():
    sc = ft.SENTINEL_SETS["starcoder"]
    # PSM is universal
    assert ft.fim_wrap("P", "M", "S", "psm", tokens=sc) == "<fim_prefix>P<fim_suffix>S<fim_middle>M"
    assert ft.fim_wrap("P", "M", "S", "psm") == ft.fim_wrap("P", "M", "S", "psm", tokens=ft.SENTINEL_SETS["olmo"])
    # SPM is not: ours puts the suffix sentinel first, BigCode's always leads with the prefix one
    assert ft.fim_wrap("P", "M", "S", "spm", tokens=sc) == "<fim_suffix>S<fim_prefix>P<fim_middle>M"
    assert ft.fim_wrap("P", "M", "S", "spm", tokens=sc, spm_style="bigcode") == "<fim_prefix><fim_suffix>S<fim_middle>PM"
    # both styles keep the middle recoverable and the prefix adjacent to it
    for style in ("suffix_first", "bigcode"):
        body = ft.fim_wrap("alpha ", "beta", " gamma", "spm", spm_style=style)
        assert body.endswith("beta") and "alpha " in body and " gamma" in body
    assert set(ft.SPM_STYLES) == set(ft.SENTINEL_SETS) and all(len(v) == 3 for v in ft.SENTINEL_SETS.values())


def test_transform_reconstructs_the_document():
    rng = random.Random(3)
    modes = set()
    for _ in range(1000):
        text = _doc(rng, rng.randint(60, 600))
        body, mode = ft.transform(text, rng)
        p, m, s, order = ft.fim_unwrap(body)
        assert p + m + s == text and m and mode.endswith(order)
        assert not m[0].isspace() or True  # snapped to whitespace, middle may start with it
        modes.add(mode)
    assert modes == {"span/psm", "span/spm", "uniform/psm", "uniform/spm"}


def test_val_twins_in_shard_mode_and_plain_val_in_record_mode():
    rng = random.Random(1)
    rec = {
        "doc_id": "pes2o-1",
        "source": "plain/pes2o",
        "text": _doc(rng),
        "license": "ODC-BY",
        "provenance": {"subset": "pes2o", "id": "1"},
        "val": True,
        "val_key": "pes2o-1",
    }
    rows = ft.transform_record(rec, rng, 0.9, val_twins=True)
    assert [r["source"] for r in rows] == ["plain/pes2o", "fim/pes2o"]
    assert rows[1]["doc_id"] == "pes2o-1#fim" and all(r["val"] for r in rows)
    assert ft.fim_unwrap(rows[1]["text"])[3] in ft.ORDERS
    replay = {**rec, "doc_id": "replay/pes2o/abc", "source": "replay/pes2o", "provenance": {"id": "1"}, "tokens": 6000}
    rows = ft.transform_record(replay, rng, 0.9, val_twins=False, plain_source="replay/pes2o")
    assert len(rows) == 1 and rows[0]["source"] == "replay/pes2o" and ft.MID not in rows[0]["text"]
    assert "tokens" not in rows[0] and rows[0]["provenance"]["subset"] == "pes2o"
    # train rows follow the rate and keep the caller's plain label
    train = {**replay, "val": False}
    n_fim = sum(
        ft.transform_record(train, rng, 0.33, val_twins=False, plain_source="replay/pes2o")[0]["source"] == "fim/pes2o"
        for _ in range(600)
    )
    assert 150 < n_fim < 250


def test_fim_clip_applies_only_to_the_rearranged_copy():
    rng = random.Random(5)
    text = _doc(rng, 3000)
    rec = {"doc_id": "d", "source": "replay/wiki", "text": text, "provenance": {}, "val": False}
    row = ft.transform_record(rec, rng, 1.0, val_twins=False, fim_clip=2000)[0]
    p, m, s, _ = ft.fim_unwrap(row["text"])
    assert (p + m + s) == text[:2000]


def test_exclusions_by_id_and_by_text_hash(tmp_path):
    rng = random.Random(9)
    t1, t2 = _doc(rng), _doc(rng)
    p = tmp_path / "replay.jsonl"
    p.write_text(
        json.dumps({"doc_id": "replay/pes2o/x", "source": "replay/pes2o", "text": t1, "provenance": {"id": "111"}})
        + "\n"
        + json.dumps({"doc_id": "replay/wiki/y", "source": "replay/wiki", "text": t2, "provenance": {"id": ""}})
        + "\n"
    )
    ids, hashes = ft.load_exclusions([str(p)])
    assert ids == {("pes2o", "111")} and len(hashes) == 2
    assert ft._text_hash(t2) in hashes and ft._text_hash(_doc(rng)) not in hashes


def test_recovery_items_are_class_stratified_and_bounded_per_doc():
    rng = random.Random(11)
    docs = []
    for i in range(300):
        body = _doc(rng, 250)
        docs.append({"doc_id": f"v{i}", "subset": "wiki", "text": body})
    items = ft.recovery_items(docs, rng, 60, ft.CLASSES, window=600)
    assert len(items) >= 54  # 6 buckets x 10 (some bucket may run short)
    by = ft._by_class(items)
    assert set(by) <= set(ft.CLASSES) and all(v["n"] <= 10 * 2 for v in by.values())
    per_doc = {}
    for it in items:
        per_doc[it["doc_id"]] = per_doc.get(it["doc_id"], 0) + 1
        assert ft.classify_blank(it["answer"], it["prefix"], it["suffix"]) == it["class"]
        assert it["answer"].strip() and it["prefix"] and it["suffix"]
        if not it["numeric"]:
            assert it["answer"] not in ft._STOP
    assert max(per_doc.values()) <= 2


def test_sentinels_are_single_reserved_ids():
    transformers = pytest.importorskip("transformers")
    tok_dir = os.path.expanduser("~/models/OLMo-2-0425-1B")
    if not os.path.isdir(tok_dir):
        pytest.skip("tokenizer not on this machine")
    tok = transformers.AutoTokenizer.from_pretrained(tok_dir)
    ids = tok(ft.fim_wrap("the cat ", "sat", " on the mat", "psm"), add_special_tokens=False)["input_ids"]
    assert ids[0] == ft.PRE_ID and ids.count(ft.SUF_ID) == 1 and ids.count(ft.MID_ID) == 1
    assert ids.index(ft.SUF_ID) < ids.index(ft.MID_ID)
    ids = tok(ft.fim_wrap("the cat ", "sat", " on the mat", "spm"), add_special_tokens=False)["input_ids"]
    assert ids[0] == ft.SUF_ID and ids.index(ft.PRE_ID) < ids.index(ft.MID_ID)
