"""Chunk-level re-translation (2026-10-04): carry translated work onto a changed
source and translate only what changed.

An OCR page repair rewrote sources under 199 finished translations and under
pending banks; the drain bound every banked chunk to (chunk count, source
length), so one repaired page cost the whole paper. agent/translation_rebank
carries the unchanged units across, and the drain translates the rest by a
stored chunk plan, patching a finished translation without un-booking it.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.translation_actions import (
    _TRANSLATE_CLAIMS,
    _TRANSLATE_DEFERRED,
    _load_plan,
    _parts_path,
    _retranslation_pending,
    action_translate_drain_batch,
    chunk_markdown,
    select_translation_paper,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.translation_rebank import (
    PAGE_SEP,
    align_pages,
    chunks_from_plan,
    plan_fits,
    plan_rebank,
    units_from_bank,
    units_from_translation,
)


def _page(i: int, n: int = 40) -> str:
    """A page with its own numbers: every page is ANCHORED."""
    return " ".join(
        f"sample{j} of series {i} yields {i * 1000 + j} counts" for j in range(n)
    )


def _prose(i: int) -> str:
    """A page with no number at all: an UNANCHORED page."""
    return " ".join(
        ["the method of analysis was applied to the samples"] * (20 + 7 * i)
    )


def _english(text: str) -> str:
    return text.replace("sample", "Sample").replace("method", "Method")


# ── plan ──────────────────────────────────────────────────────────────────


def test_a_plan_tiles_its_source_exactly():
    src = "a\n\nbb\n\n---\n\nccc"
    plan = [1, 2, 8]
    assert plan_fits(src, plan)
    assert chunks_from_plan(src, plan) == ["a", "bb", "---\n\nccc"]
    assert "\n\n".join(chunks_from_plan(src, plan)) == src
    assert not plan_fits(src, [1, 2, 7])  # short
    assert not plan_fits(src, [2, 1, 8])  # cuts where there is no blank line
    assert not plan_fits(src, [])


# ── alignment ─────────────────────────────────────────────────────────────


def test_alignment_survives_a_dropped_page_separator():
    pages = [_page(i) for i in range(6)]
    segs = pages[:2] + [pages[2] + "\n\n" + pages[3]] + pages[4:]
    runs = align_pages(pages, [_english(s) for s in segs])
    assert [r["pages"] for r in runs] == [(0, 1), (1, 2), (2, 4), (4, 5), (5, 6)]


def test_a_patch_carries_unchanged_pages_and_leaves_the_changed_one():
    pages = [_page(i) for i in range(6)]
    old = PAGE_SEP.join(pages)
    en = _english(old)
    new_pages = list(pages)
    new_pages[3] = _page(3) + " repaired"
    new = PAGE_SEP.join(new_pages)

    units, stats = units_from_translation(old, en)
    assert stats["trusted"] == 6
    res = plan_rebank(old, new, units)
    chunks = chunks_from_plan(new, res["plan"])
    assert "\n\n".join(chunks) == new
    missing = [i for i in range(len(chunks)) if i not in res["parts"]]
    assert len(missing) == 1 and "repaired" in chunks[missing[0]]
    # Each carried English unit is the translation of exactly its chunk.
    for i, t in res["parts"].items():
        assert t == _english(chunks[i])
    assert res["changed_pages"] == [3]


def test_unanchored_prose_with_a_dropped_separator_is_carried_as_one_unit():
    """Pages 1-4 have no numbers; one separator inside them was dropped, so
    which two merged is unknowable page by page — but the stretch between the
    two anchored pages maps as a whole."""
    pages = [_page(0)] + [_prose(i) for i in range(1, 5)] + [_page(5)]
    old = PAGE_SEP.join(pages)
    segs = [pages[0], pages[1], pages[2] + "\n\n" + pages[3], pages[4], pages[5]]
    en = PAGE_SEP.join(_english(s) for s in segs)

    units, stats = units_from_translation(old, en)
    assert stats["merged_stretches"] == 1
    assert stats["trusted"] == stats["units"] == 3
    res = plan_rebank(old, old, units)
    assert len(res["parts"]) == len(res["plan"])  # nothing to translate

    changed = list(pages)
    changed[2] = _prose(2) + " repaired"
    res = plan_rebank(old, PAGE_SEP.join(changed), units)
    chunks = chunks_from_plan(PAGE_SEP.join(changed), res["plan"])
    dirty = "".join(c for i, c in enumerate(chunks) if i not in res["parts"])
    assert "repaired" in dirty and _prose(4) in dirty  # the whole stretch


def test_a_misaligned_or_untranslated_unit_is_never_carried():
    pages = [_page(i) for i in range(4)]
    old = PAGE_SEP.join(pages)
    segs = [_english(p) for p in pages]
    segs[2] = "Nur ein deutscher Satz ohne Zahlen, der nichts übersetzt hat. " * 40
    units, stats = units_from_translation(old, PAGE_SEP.join(segs))
    carried = [u for u in units if u[2] is not None]
    # Conservative by design: the alignment may fold the German page into a
    # neighbour, which then fails its own numbers and is re-translated too.
    assert len(carried) >= 2
    assert all("deutscher" not in u[2] for u in carried)
    for s, e, en in carried:
        assert en == _english(old[s:e])


def test_a_stale_bank_is_carried_wherever_its_pages_did_not_change():
    pages = [_page(i, 110) for i in range(8)]  # ~6 kB a page: chunks span pages
    old = PAGE_SEP.join(pages)
    old_chunks = chunk_markdown(old)
    parts = {i: _english(c) for i, c in enumerate(old_chunks)}
    new_pages = list(pages)
    new_pages[4] = _page(4, 60) + " repaired"
    new = PAGE_SEP.join(new_pages)

    res = plan_rebank(old, new, units_from_bank(old, parts))
    chunks = chunks_from_plan(new, res["plan"])
    assert "\n\n".join(chunks) == new
    assert res["carried"] >= len(old_chunks) - 2
    for i, t in res["parts"].items():
        assert t == _english(chunks[i])
    assert all("repaired" not in chunks[i] for i in res["parts"])


def test_a_page_count_change_is_refused():
    old = PAGE_SEP.join(_page(i) for i in range(3))
    new = PAGE_SEP.join(_page(i) for i in range(4))
    assert "error" in plan_rebank(old, new, units_from_bank(old, {}))


# ── the drain ─────────────────────────────────────────────────────────────


class _Identity(MockEffects):
    """'Translates' by capitalising, and counts what it was asked."""

    asked: list[str]

    async def run_inference(self, prompt, config=None, **k):  # noqa: D102
        span = prompt.rsplit("):\n\n", 1)[-1].strip()
        self.asked.append(span)

        class _R:
            error = None
            text = _english(span)

        if "BAD" in span:
            _R.error, _R.text = "cycle period 3 x 12", ""
        return _R()


def _patched_paper(new_page_3: str):
    pages = [_page(i, 110) for i in range(6)]
    old = PAGE_SEP.join(pages)
    en = _english(old)
    new_pages = list(pages)
    new_pages[3] = new_page_3
    new = PAGE_SEP.join(new_pages)
    units, _ = units_from_translation(old, en)
    res = plan_rebank(old, new, units)
    bank = [json.dumps({"plan": res["plan"], "src_len": len(new)})] + [
        json.dumps(
            {
                "idx": i,
                "n": len(res["plan"]),
                "src_len": len(new),
                "attempt": 1,
                "text": t,
            }
        )
        for i, t in res["parts"].items()
    ]
    rec = {
        "paper_key": "p1",
        "extraction_status": "extracted",
        "md_path": "databank/markdown/p1.md",
        "md_en_path": "databank/markdown/p1.en.md",
        "translated": True,
        "retranslate": True,
        "translate_epoch": 1,
        "translate_attempts": 0,
        "review_status": "accepted",
        "pack_status": "packed",
        "language": "es",
    }
    fx = _Identity(
        files={
            "databank/papers.jsonl": json.dumps(rec) + "\n",
            "databank/markdown/p1.md": new,
            "databank/markdown/p1.en.md": en,
            _parts_path("p1"): "\n".join(bank) + "\n",
        }
    )
    fx.asked = []
    return fx, new, res


def _si(fx) -> StepInput:
    return StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="translate_drain", step_id="drain"),
        effects=fx,
    )


def _side(fx) -> dict:
    return json.loads(fx._files["databank/extraction.jsonl"].strip().splitlines()[-1])


def _clear():
    _TRANSLATE_CLAIMS.clear()
    _TRANSLATE_DEFERRED.clear()


@pytest.mark.asyncio
async def test_the_drain_patches_only_the_changed_span_and_leaves_the_pack(monkeypatch):
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    fx, new, res = _patched_paper(_page(3, 110) + " repaired")
    assert await _load_plan(fx, "p1", new) == res["plan"]

    out = await action_translate_drain_batch(_si(fx))
    assert out.result["status"] == "patched"
    # Only the span carrying the changed page was sent to the model.
    assert len(fx.asked) == len(res["plan"]) - len(res["parts"])
    assert all("repaired" in c or "series 3" in c for c in fx.asked)
    assert fx._files["databank/markdown/p1.en.md"] == _english(new)
    side = _side(fx)
    assert side["translated"] is True and side["retranslate"] is False
    assert side["translation_quality"]["patched_at"]
    # No repack queued: the papers side was never appended to.
    assert fx._files["databank/papers.jsonl"].count("\n") == 1


@pytest.mark.asyncio
async def test_a_patch_that_cannot_finish_keeps_the_english_it_would_replace(
    monkeypatch,
):
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    fx, new, _ = _patched_paper("BAD " + _page(3, 110))
    before = fx._files["databank/markdown/p1.en.md"]
    status = ""
    for _ in range(8):  # rounds until the chunk exhausts retries and salvage
        _clear()
        out = await action_translate_drain_batch(_si(fx))
        status = out.result.get("status", "")
        if status in ("patch_failed", "patched"):
            break
    assert status == "patch_failed"
    assert fx._files["databank/markdown/p1.en.md"] == before
    side = _side(fx)
    assert side["translated"] is True and side["extraction_status"] == "extracted"
    assert side["retranslate"] is False and side["retranslate_failed"]
    assert fx._files["databank/papers.jsonl"].count("\n") == 1


@pytest.mark.asyncio
async def test_a_patch_without_a_plan_for_its_source_stands_down():
    _clear()
    fx, new, _ = _patched_paper(_page(3, 110) + " repaired")
    fx._files["databank/markdown/p1.md"] = new + " moved again"
    await action_translate_drain_batch(_si(fx))
    assert fx.asked == []
    side = _side(fx)
    assert side["retranslate"] is False and "re-bank" in side["retranslate_failed"]
    assert side["translated"] is True


def test_patches_queue_behind_papers_with_no_english_at_all():
    db = {
        "patch": {
            "retranslate": True,
            "translated": True,
            "md_path": "x",
            "review_status": "accepted",
        },
        "first": {
            "extraction_status": "extract_lingual",
            "md_path": "y",
            "review_status": "accepted",
        },
    }
    _clear()
    assert _retranslation_pending(db["patch"])
    assert select_translation_paper(db) == "first"
    del db["first"]
    assert select_translation_paper(db) == "patch"
