"""Section-bounded pack windows: never cut a section, lose nothing, merge honestly."""

from __future__ import annotations

import pytest

from agent.actions.pack_windows import (
    MergeReport,
    merge_packs,
    split_sections,
    window_sections,
)


def _doc(n_sections: int, words_per: int, lead: str = "Title line\n\n") -> str:
    body = " ".join(["word"] * words_per)
    return lead + "".join(f"## Section {i}\n\n{body}\n\n" for i in range(n_sections))


def test_split_is_lossless_and_ordered():
    doc = _doc(5, 40)
    secs = split_sections(doc)
    assert "".join(secs) == doc
    assert secs[0].startswith(
        "Title line"
    ), "text before the first heading is its own section"
    assert [s.splitlines()[0] for s in secs[1:]] == [
        f"## Section {i}" for i in range(5)
    ]


def test_windows_never_cut_a_section_and_cover_every_section_once():
    # ~40 words ≈ 60 tokens a section; target 200 -> ~3 sections a window.
    doc = _doc(10, 40)
    wins = window_sections(doc, target_tokens=200, max_tokens=400)
    assert len(wins) > 1
    assert (
        "".join(w.text for w in wins) == doc
    ), "windows must tile the document exactly"
    for w in wins:
        # every window starts at a section start (a heading or the lead block)
        assert w.text.startswith("## Section") or w.text.startswith("Title line")
        assert w.tokens <= 200 or w.section_count == 1
    assert [w.index for w in wins] == list(range(len(wins)))


def test_an_oversize_section_is_its_own_window_and_flagged():
    big = "## Huge\n\n" + " ".join(["x"] * 3000) + "\n\n"
    doc = "## A\n\nsmall\n\n" + big + "## B\n\nsmall\n\n"
    wins = window_sections(doc, target_tokens=100, max_tokens=200)
    huge = [w for w in wins if w.first_heading == "Huge"]
    assert len(huge) == 1 and huge[0].oversize and huge[0].section_count == 1
    assert "".join(w.text for w in wins) == doc


def test_a_document_without_headings_is_one_window():
    doc = " ".join(["plain"] * 500)
    wins = window_sections(doc, target_tokens=50, max_tokens=100)
    assert len(wins) == 1 and wins[0].text == doc and wins[0].oversize


def test_bad_sizing_is_rejected():
    with pytest.raises(ValueError):
        window_sections("## a\n\nb", target_tokens=0, max_tokens=10)
    with pytest.raises(ValueError):
        window_sections("## a\n\nb", target_tokens=10, max_tokens=5)


def test_merge_concatenates_lists_dedupes_and_records_scalar_conflicts():
    a = {
        "raman_peak_wavenumber_cm-1": [{"peak_cm-1": 1091}, {"peak_cm-1": 1366}],
        "laser_nm": 532,
        "meta": {"instrument": "Renishaw"},
    }
    b = {
        "raman_peak_wavenumber_cm-1": [{"peak_cm-1": 1366}, {"peak_cm-1": 712}],
        "laser_nm": 785,  # disagreement across windows
        "meta": {"grating": "1800 l/mm"},
        "sample_count": 3,
    }
    rep = MergeReport()
    m = merge_packs([a, b], rep)
    assert m["raman_peak_wavenumber_cm-1"] == [
        {"peak_cm-1": 1091},
        {"peak_cm-1": 1366},
        {"peak_cm-1": 712},
    ], "lists concatenate with exact duplicates dropped, order kept"
    assert m["laser_nm"] == 532, "first value wins"
    assert rep.conflicts == [{"path": "laser_nm", "kept": 532, "dropped": 785}]
    assert m["meta"] == {"instrument": "Renishaw", "grating": "1800 l/mm"}
    assert m["sample_count"] == 3


def test_merge_promotes_a_scalar_that_meets_a_list_and_ignores_none():
    m = merge_packs([{"k": 1, "n": None}, {"k": [2, 3], "n": 5}])
    assert m["k"] == [1, 2, 3]
    assert m["n"] == 5
