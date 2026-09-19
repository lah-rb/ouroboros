"""Renderer helpers: deterministic val split, repeats, question guarantee."""

from corpus_stage1 import REPEATS, is_val, record, summarize
from corpus_stage2 import _with_a_question
from templates import FRAMES


def test_val_split_is_deterministic_and_about_one_percent():
    ids = [f"paper:{i}" for i in range(20000)]
    flags = [is_val(i) for i in ids]
    assert flags == [is_val(i) for i in ids]
    assert 100 < sum(flags) < 300


def test_record_carries_repeats_and_license():
    r = record("hom:Quartz", "hom", "x" * 350, "restricted-hom", {"species": "Quartz"})
    assert (
        r["max_repeats"] == REPEATS["hom"] == 2  # v4 ran 3; §19: ≤ 2 copies
        and r["license"] == "restricted-hom"
        and r["tokens_est"] == 100
    )
    s = summarize([r, record("paper:a", "paper_markdown", "y" * 700, "cc-by", {})])
    assert (
        s["hom"]["weighted_tokens_est"] == 200
        and s["paper_markdown"]["weighted_tokens_est"] == 200
    )


def test_small_sources_draw_a_larger_val_fraction_nested_in_the_one_percent():
    from corpus_stage1 import VAL_FRACTION_BY_SOURCE

    ids = [f"hom:{i}" for i in range(20000)]
    one = [is_val(i) for i in ids]
    three = [is_val(i, VAL_FRACTION_BY_SOURCE["hom"]) for i in ids]
    assert 400 < sum(three) < 800 and all(t for o, t in zip(one, three) if o)
    r = record("hom:Quartz", "hom", "x" * 350, "restricted-hom", {"species": "Quartz"})
    assert r["val"] == is_val("hom:Quartz", VAL_FRACTION_BY_SOURCE["hom"])
    p = record("pack:k", "pack_prose", "z" * 350, "corpus", {}, val_key="paper:k")
    assert p["val"] == is_val("paper:k")


def test_stage2_always_includes_a_question_frame():
    for fid in (
        "raman:canonical:Quartz",
        "structure:Calcite",
        "formula:Anatase",
        "libs:Aegirine",
    ):
        for kind in ("raman_bands", "structure", "formula", "libs_lines"):
            picks = _with_a_question(fid, kind, 3, "s2")
            assert len(picks) == 3 and any(
                fr in FRAMES[kind]["questions"] for _, fr in picks
            )
