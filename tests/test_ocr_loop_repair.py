"""OCR loop repair helpers (tools/pdf_extract/ocr_loop_lib.py), 2026-09-19."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "tools", "pdf_extract")
)

import ocr_loop_lib as L  # noqa: E402

LOOP = " ".join(["concluyada en la"] * 30)  # 90 words, period 3
TABLE = (
    "<tr>"
    + "".join(
        "<td style='text-align: center; word-wrap: break-word;'>—</td>"
        for _ in range(20)
    )
    + "</tr>"
)
FIGS = "\n\n".join(
    '<div style="text-align: center;">*[figure removed by extraction filter]*</div>'
    for _ in range(25)
)
CJK = "この結晶の中心位置の位置と同じ位置と計算値と異なるのが、" * 12


def test_word_loop_found_and_collapsed_keeping_one_unit():
    text = "Antes. " + LOOP + " Después."
    spans = L.find_loops(text)
    # both detectors see this loop; they merge into one span carrying the word count
    assert len(spans) == 1 and spans[0]["words"] == 87
    out, cost = L.collapse_loops(text)
    assert cost["words"] == 87
    body = L._MARKER_RE.sub("", out)
    assert body.count("concluyada en la") == 1
    assert out.startswith("Antes.") and out.endswith("Después.")


def test_markup_repeats_are_not_loops():
    for text in (TABLE, FIGS, TABLE + "\n" + FIGS):
        assert L.find_loops(text) == []
        assert L.collapse_loops(text)[0] == text


def test_cjk_loop_found_by_the_character_pass():
    spans = L.find_loops("見出し\n\n" + CJK + "\n\n終わり")
    assert len(spans) == 1 and spans[0]["kind"] == "char" and spans[0]["chars"] > 0
    out, _ = L.collapse_loops("見出し\n\n" + CJK + "\n\n終わり")
    assert L._MARKER_RE.sub("", out).count("この結晶の中心位置") == 1
    assert out.endswith("終わり")


def test_pages_round_trip():
    pages = ["p0 text", "p1 text", "p2 " + LOOP]
    md = L.join_pages(pages)
    assert L.split_pages(md) == pages


def test_remap_imgs_same_count_substitutes_in_order():
    old = [
        '<img src="../figures/k/fig_07.png" alt="a" />',
        '<img src="../figures/k/fig_08.png" />',
    ]
    new = 'text <img src="../figures/k/fig_01.png"> more <img src="../figures/k/fig_02.png"> end'
    out, note = L.remap_imgs(new, old)
    assert note == "mapped"
    assert L.img_tags(out) == old


def test_remap_imgs_count_mismatch_keeps_old_tags_and_drops_new():
    old = ['<img src="../figures/k/fig_07.png" />']
    new = (
        'a <img src="../figures/k/fig_01.png"> b <img src="../figures/k/fig_02.png"> c'
    )
    out, note = L.remap_imgs(new, old)
    assert note.startswith("kept_old_tags")
    assert L.img_tags(out) == old
    assert "fig_01" not in out and "fig_02" not in out


def test_similarity_recognises_the_same_page_and_rejects_another():
    old = (
        "En las muestras de la serie F se ha identificado la presencia de fases de alta "
        "temperatura, anortita y gehlenita, salvo en F-05. " + LOOP
    )
    same = (
        "En las muestras de la serie F se ha identificado la presencia de fases de alta "
        "temperatura, anortita y gehlenita, salvo en F-05. Todas las demás contienen los "
        "mismos compuestos cristalinos."
    )
    other = "The Raman spectrum of quartz shows its main band at 464 cm-1 and a weaker band at 206."
    assert L.similarity(old, same) > 0.8
    assert L.similarity(old, other) < 0.1
    assert L.similarity(LOOP, "anything") == 1.0  # nothing clean to compare


@pytest.mark.parametrize("text", ["", "short text", " ".join(["n.d."] * 12)])
def test_short_or_empty_text_has_no_loops(text):
    assert L.find_loops(text) == []
    assert L.loop_cost(text) == {"words": 0, "chars": 0, "spans": 0}
