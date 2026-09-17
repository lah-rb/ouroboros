"""Selector size cache and the oversize park boundary (2026-09-16 fixes).

Two ways a reviewable paper was invisible to every curate lane for a whole run:
an EMPTY size measurement cached for the life of the process (a zombie re-armed
and OCR'd), and a compressed floor sitting between the largest seat's budget
and a 1.1 park margin (starved, never parked, never selectable).
"""

from __future__ import annotations

import json

import pytest

from agent.actions import curation_actions as ca
from agent.actions.curation_actions import (
    _CURATE_CLAIMS,
    _CURATE_DOC_CACHE,
    _CURATE_STARVED,
    _largest_seat_budget_chars,
    release_curate_keys,
    select_curate_paper,
)
from agent.actions.scholarly_actions import read_databank
from agent.effects.mock import MockEffects


def _rec(key: str, **extra) -> dict:
    return {
        "paper_key": key,
        "title": f"Paper {key}",
        "doi": f"10.1/{key}",
        "license": "cc-by",
        "year": 2024,
        "extraction_status": "extracted",
        "md_path": f"databank/markdown/{key}.md",
        "figure_count": 0,
        **extra,
    }


def _files(records: list[dict], markdown: dict[str, str]) -> dict[str, str]:
    files = {"databank/papers.jsonl": "\n".join(json.dumps(r) for r in records) + "\n"}
    for key, md in markdown.items():
        files[f"databank/markdown/{key}.md"] = md
    return files


def _clear():
    _CURATE_CLAIMS.clear()
    _CURATE_DOC_CACHE.clear()
    _CURATE_STARVED.clear()


@pytest.mark.asyncio
async def test_empty_measurement_is_not_cached_and_the_paper_appears_once_it_has_text():
    _clear()
    fx = MockEffects(files=_files([_rec("p")], {}))  # row says extracted, no file yet
    bank = await read_databank(fx)
    try:
        assert await select_curate_paper(fx, bank, 100_000) == ("", "")
        assert "p" not in _CURATE_DOC_CACHE  # empty → never stored
        # The markdown arrives (re-OCR, a repointed row) with the SAME record.
        await fx.write_file("databank/markdown/p.md", "spectra " * 400)
        key, doc = await select_curate_paper(fx, bank, 100_000)
        assert key == "p" and doc
        assert "p" in _CURATE_DOC_CACHE and _CURATE_DOC_CACHE["p"][2] > 0
    finally:
        release_curate_keys(["p"])
        _clear()


@pytest.mark.asyncio
async def test_cache_refreshes_when_the_record_document_state_changes(monkeypatch):
    _clear()
    calls: list[str] = []
    sizes = {"n": 0}

    async def fake_sizes(effects, key):
        calls.append(key)
        sizes["n"] += 1
        return (500, 400) if sizes["n"] == 1 else (5000, 4000)

    monkeypatch.setattr(ca, "_curate_doc_sizes", fake_sizes)
    fx = MockEffects(files=_files([_rec("q")], {"q": "x" * 500}))
    bank = await read_databank(fx)
    try:
        key, _ = await select_curate_paper(fx, bank, 100_000)
        assert key == "q" and calls == ["q"]
        release_curate_keys(["q"])
        # Same state → cache hit, no re-measure.
        key, _ = await select_curate_paper(fx, bank, 100_000)
        assert key == "q" and calls == ["q"]
        release_curate_keys(["q"])
        # A translation lands: the document changed → re-measured.
        bank2 = {k: dict(v) for k, v in bank.items()}
        bank2["q"]["md_en_path"] = "databank/markdown/q.en.md"
        bank2["q"]["translated"] = True
        key, _ = await select_curate_paper(fx, bank2, 100_000)
        assert key == "q" and calls == ["q", "q"]
        assert _CURATE_DOC_CACHE["q"][1:] == (5000, 4000)
    finally:
        release_curate_keys(["q"])
        _clear()


@pytest.mark.asyncio
async def test_floor_just_over_the_largest_seat_budget_is_parked_not_starved(
    monkeypatch,
):
    """A local-only fleet: the largest seat is the 65k local seat. A floor one
    character over that seat's budget can never be selected by any lane, so
    it is parked (visible, re-armable); a floor AT the budget is merely too
    big for this lane's budget and starves as before."""
    _clear()
    seat_budget = _largest_seat_budget_chars(ca._CURATE_SEAT_TOKENS)
    floors = {"over": seat_budget + 1, "at": seat_budget}

    async def fake_sizes(effects, key):
        return (floors[key] * 2, floors[key])

    monkeypatch.setattr(ca, "_curate_doc_sizes", fake_sizes)
    fx = MockEffects(
        files=_files([_rec("over"), _rec("at")], {"over": "o" * 10, "at": "a" * 10})
    )
    bank = await read_databank(fx)
    try:
        key, doc = await select_curate_paper(fx, bank, 100_000)
        assert (key, doc) == ("", "")
        after = await read_databank(fx)
        assert after["over"]["extraction_status"] == "curate_oversize"
        assert "seat" in after["over"]["failure_reason"]
        assert after["at"]["extraction_status"] == "extracted"
        assert _CURATE_STARVED.get("at", 0) >= 1 and "over" not in _CURATE_STARVED
    finally:
        _clear()
