"""The three retirement fixes of 2026-09-19.

Eleven retired translations: nine on the image-tag census (the translator
drops the image-only <div> and keeps the caption), one on an echoed chunk
(numbers and tags preserved, so the per-span checks let it bank), one on a
numeric score whose bank already held a passing version. So: dropped image
blocks are put back mechanically before a span is judged; the span check also
judges the source script, on prose with tag paths stripped; and before a
retirement the best banked version of each chunk is assembled and gated.
"""

from __future__ import annotations

import json
import re

import pytest

from agent.actions.translation_actions import (
    _span_problem,
    _strip_img_tags,
    action_translate_drain_batch,
    chunk_markdown,
    reinsert_missing_imgs,
    translation_gate,
)
from tests.test_translation_bank_and_repair import (
    _Scripted,
    _clear,
    _para,
    _rec,
    _si,
    _side,
)

IMG1 = '<img src="../figures/p1/fig_01.png" alt="Image" width="60%" />'
IMG2 = '<img src="../figures/p1/fig_02.png" alt="Image" width="60%" />'
FIG_BLOCK = (
    '<div style="text-align: center;">{img}</div>\n\n\n'
    '<div style="text-align: center;">Fig. {n} {cap}</div>'
)
CYR_WORDS = "спектры комбинационного рассеяния кристалла образца серии дают отсчётов при температуре".split()


def _cyr_para(i: int, n: int = 150) -> str:
    return " ".join(
        f"{CYR_WORDS[j % len(CYR_WORDS)]} {CYR_WORDS[(j + 3) % len(CYR_WORDS)]} {i * 1000 + j} "
        f"{CYR_WORDS[(j + 5) % len(CYR_WORDS)]}"
        for j in range(n)
    )


def _english_with_numbers(span: str) -> str:
    nums = re.findall(r"\d+", span)
    return " ".join(
        f"the sample gave {x} counts and the series was measured with this method"
        for x in nums
    )


# ── reinsert_missing_imgs ──────────────────────────────────────────────


def test_missing_image_block_goes_back_before_its_translated_caption():
    src = (
        "Text before.\n\n"
        + FIG_BLOCK.format(img=IMG1, n=1, cap="Diagrama de Tanabe–Sugano")
        + "\n\nMore text.\n\n"
        + FIG_BLOCK.format(img=IMG2, n=2, cap="Espectro Raman")
        + "\n\nEnd."
    )
    out = (
        "Text before.\n\n"
        '<div style="text-align: center;">Fig. 1 Tanabe–Sugano diagram</div>\n\n'
        "More text.\n\n"
        '<div style="text-align: center;">Fig. 2 Raman spectrum</div>\n\nEnd.'
    )
    fixed, n = reinsert_missing_imgs(src, out)
    assert n == 2
    assert fixed.count(IMG1) == 1 and fixed.count(IMG2) == 1
    assert (
        fixed.index(IMG1)
        < fixed.index("Fig. 1")
        < fixed.index(IMG2)
        < fixed.index("Fig. 2")
    )
    assert fixed.endswith("End.")
    again, m = reinsert_missing_imgs(src, fixed)
    assert m == 0 and again == fixed  # idempotent


def test_missing_image_with_no_caption_match_is_appended():
    src = "Prose.\n\n" + f'<div style="text-align: center;">{IMG1}</div>' + "\n\nProse."
    fixed, n = reinsert_missing_imgs(src, "Prose. Prose.")
    assert n == 1 and fixed.rstrip().endswith(
        f'<div style="text-align: center;">{IMG1}</div>'
    )


def test_reinsertion_never_removes_or_duplicates():
    src = f"a {IMG1} b {IMG2} c"
    out = f"a {IMG1} b {IMG2} c extra"
    assert reinsert_missing_imgs(src, out) == (out, 0)


# ── language checks on prose, not tag paths ───────────────────────────


def test_gate_ignores_cyrillic_in_image_paths():
    src = _cyr_para(1, 40)
    english = _english_with_numbers(src)
    cyr_path_tag = '<img src="../figures/title_спектры_комбинационного_рассеяния_кристалла_ndf3/fig_08.png" alt="Image" />'
    out = english + "\n\n" + "\n\n".join([cyr_path_tag] * 6)
    src_with_tags = src + "\n\n" + "\n\n".join([cyr_path_tag] * 6)
    assert _strip_img_tags(out).count("спектры") == 0
    gate = translation_gate(src_with_tags, out)
    assert not any("not English" in p for p in gate["problems"]), gate


def test_span_check_flags_an_echoed_chunk_and_passes_english():
    src = _cyr_para(2, 60)
    assert "not English" in _span_problem(
        src, src
    )  # echo: numbers and tags all preserved
    assert _span_problem(src, _english_with_numbers(src)) == ""


# ── the drain ─────────────────────────────────────────────────────────


class _DropsImages(_Scripted):
    """Identity translation that loses every image-only div (what both models do)."""

    async def run_inference(self, prompt, config=None, **k):  # noqa: D102
        r = await super().run_inference(prompt, config, **k)
        if r.text:
            r.text = re.sub(
                r'<div style="text-align: center;"><img[^>]*></div>\n*', "", r.text
            )
        return r


@pytest.mark.asyncio
async def test_dropped_image_blocks_are_reinserted_and_the_paper_passes(monkeypatch):
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    src = (
        _para(1, n=120)
        + "\n\n"
        + FIG_BLOCK.format(img=IMG1, n=1, cap="Espectro")
        + "\n\n"
        + _para(2, n=120)
        + "\n\n"
        + FIG_BLOCK.format(img=IMG2, n=2, cap="Difractograma")
    )
    fx = _DropsImages(
        files={
            "databank/papers.jsonl": json.dumps(_rec()) + "\n",
            "databank/markdown/p1.md": src,
        }
    )
    out = await action_translate_drain_batch(_si(fx))
    assert out.result["status"] == "translated", out.result
    side = _side(fx)
    assert side["translation_quality"]["img_tags"] == [2, 2]
    en = fx._files["databank/markdown/p1.en.md"]
    assert en.count(IMG1) == 1 and en.count(IMG2) == 1
    assert en.index(IMG1) < en.index("Fig. 1") and en.index(IMG2) < en.index("Fig. 2")


class _EchoesOnce(_Scripted):
    """Returns the Cyrillic source the first time a span is asked, English after."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.asked: dict[str, int] = {}

    async def run_inference(self, prompt, config=None, **k):  # noqa: D102
        span = prompt.rsplit("):\n\n", 1)[-1].strip()
        self.asked[span[:40]] = self.asked.get(span[:40], 0) + 1

        class _R:
            error = None
            text = (
                span
                if (self.asked[span[:40]] == 1 and "спектры" in span)
                else (_english_with_numbers(span) if "спектры" in span else span)
            )

        return _R()


@pytest.mark.asyncio
async def test_an_echoed_chunk_is_retried_at_span_level(monkeypatch):
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    src = _para(1, n=220) + "\n\n" + _cyr_para(2, n=220)
    assert len(chunk_markdown(src)) >= 2
    fx = _EchoesOnce(
        files={
            "databank/papers.jsonl": json.dumps(_rec()) + "\n",
            "databank/markdown/p1.md": src,
        }
    )
    out = await action_translate_drain_batch(_si(fx))
    assert out.result["status"] == "translated", out.result
    cyr_key = next(k for k in fx.asked if "спектры" in k)
    assert fx.asked[cyr_key] == 2  # asked twice: the echo was caught at the span
    assert "спектры" not in _strip_img_tags(fx._files["databank/markdown/p1.en.md"])


@pytest.mark.asyncio
async def test_best_of_bank_rescues_a_paper_at_its_last_attempt(monkeypatch):
    """This pass echoes chunk 1 every time; an earlier epoch banked a good one."""
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    src = _para(1, n=220) + "\n\n" + _cyr_para(2, n=220)
    chunks = chunk_markdown(src)
    assert len(chunks) >= 2
    # an earlier epoch banked a good version of EVERY chunk: identity for the
    # English ones, a real translation for the Cyrillic ones
    good = {
        i: (_english_with_numbers(c) if "спектры" in c else c)
        for i, c in enumerate(chunks)
    }
    bank = "".join(
        json.dumps(
            {"idx": i, "n": len(chunks), "src_len": len(src), "attempt": 0, "text": t},
            ensure_ascii=False,
        )
        + "\n"
        for i, t in good.items()
    )
    fx = _Scripted(  # identity: the Cyrillic chunk comes back as an echo, always
        files={
            "databank/papers.jsonl": json.dumps(_rec()) + "\n",
            "databank/extraction.jsonl": json.dumps(
                {
                    "paper_key": "p1",
                    "extraction_status": "extract_lingual",
                    "md_path": "databank/markdown/p1.md",
                    "translate_attempts": 2,
                    "translate_epoch": 2,
                }
            )
            + "\n",
            "databank/markdown/p1.md": src,
            "databank/translations/p1.parts.jsonl": bank,
        }
    )
    out = await action_translate_drain_batch(_si(fx))
    assert out.result["status"] == "translated", out.result
    side = _side(fx)
    assert side["translated"] is True
    assert side["translation_quality"]["assembled_from"] == "best_of_bank"
    en = fx._files["databank/markdown/p1.en.md"]
    assert "спектры" not in en and "the sample gave" in en
