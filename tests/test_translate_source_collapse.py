"""Source-loop collapse before translation (2026-09-19).

A degenerate OCR run in the SOURCE — one phrase dozens of times — is
translated faithfully and then killed by the engine's repetition guard, on
every model. The drain collapses prose runs over ``_SOURCE_LOOP_WORDS`` (and
character loops the word census cannot see) into the extraction marker
BEFORE prompting, judges the paper against the collapsed source, and never
touches markup: HTML table cells and the figure-removed marker repeat
legitimately.
"""

from __future__ import annotations

import json
import re

import pytest

from agent.actions.translation_actions import (
    _SOURCE_LOOP_WORDS,
    action_translate_drain_batch,
    collapse_source_loops,
)
from tests.test_translation_bank_and_repair import (
    _Scripted,
    _clear,
    _fx,
    _para,
    _rec,
    _si,
    _side,
)

LOOP = " ".join(["concluyada en la"] * 30)  # 90 words, period 3
_MARKER = re.compile(r"\*\[degenerate OCR run collapsed:.*?\]\*", re.S)


def _outside_markers(text: str) -> str:
    """The marker quotes the collapsed unit; count the unit outside it."""
    return _MARKER.sub("", text)


TABLE_ROW = (
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
CJK_LOOP = "この結晶の中心位置の位置と同じ位置と計算値と異なるのが、" * 12


def test_prose_loop_is_collapsed_into_the_marker():
    out, rep = collapse_source_loops("Antes. " + LOOP + " Después.")
    assert rep["words"] == 90 - 3  # the run minus the one unit kept
    assert _SOURCE_LOOP_WORDS < 90
    assert _outside_markers(out).count("concluyada en la") == 1
    assert "degenerate OCR run collapsed" in out
    assert out.startswith("Antes.") and out.endswith("Después.")


def test_table_cells_and_figure_markers_are_never_collapsed():
    for text in (TABLE_ROW, FIGS, TABLE_ROW + "\n" + FIGS):
        out, rep = collapse_source_loops(text)
        assert out == text
        assert rep == {"words": 0, "chars": 0}


def test_cjk_loop_without_spaces_is_collapsed_by_the_character_pass():
    out, rep = collapse_source_loops("### 3. 構造決定\n\n" + CJK_LOOP + "\n\n終わり")
    assert rep["chars"] > 0
    assert _outside_markers(out).count("この結晶の中心位置") == 1
    assert "degenerate OCR run collapsed" in out
    assert out.endswith("終わり")


def test_short_repeats_are_left_alone():
    text = " ".join(["n.d."] * 12) + " " + " ".join(["muy"] * 10)
    out, rep = collapse_source_loops(text)
    assert out == text and rep == {"words": 0, "chars": 0}


@pytest.mark.asyncio
async def test_drain_translates_the_collapsed_source_and_records_it(monkeypatch):
    """The model never sees the loop, the paper passes, and the record says
    what was cut."""
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    src = _para(1) + "\n\n" + LOOP + "\n\n" + _para(2)
    seen: list[str] = []

    class _Spy(_Scripted):
        async def run_inference(self, prompt, config=None, **k):  # noqa: D102
            seen.append(prompt)
            return await super().run_inference(prompt, config, **k)

    fx = _Spy(
        files={
            "databank/papers.jsonl": json.dumps(_rec()) + "\n",
            "databank/markdown/p1.md": src,
        }
    )
    out = await action_translate_drain_batch(_si(fx))
    assert out.result["status"] == "translated", out.result
    assert seen, "no inference happened"
    assert all(_outside_markers(p).count("concluyada en la") <= 1 for p in seen)
    assert any("degenerate OCR run collapsed" in p for p in seen)
    side = _side(fx)
    assert side["translated"] is True
    assert side["translation_quality"]["source_collapsed"]["words"] > 0
    en = fx._files["databank/markdown/p1.en.md"]
    assert _outside_markers(en).count("concluyada en la") == 1
    assert "degenerate OCR run collapsed" in en


@pytest.mark.asyncio
async def test_drain_leaves_a_clean_paper_unmarked(monkeypatch):
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    fx = _fx(_para(1) + "\n\n" + TABLE_ROW + "\n\n" + _para(2), _rec())
    out = await action_translate_drain_batch(_si(fx))
    assert out.result["status"] == "translated", out.result
    side = _side(fx)
    assert "source_collapsed" not in side["translation_quality"]
    assert TABLE_ROW in fx._files["databank/markdown/p1.en.md"]
