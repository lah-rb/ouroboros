"""curate_drain: seat-scoped selection + stateless review/pack + safe booking.

Pins the drain-specific contracts: the doc budget derives from the LIVE
cell and self-gates OFF below the whole-paper threshold (the 32k cell must
decline, not truncate), selection is smallest-first over papers that fit,
transport faults decline with NOTHING booked (the fig_review
transport-burn lesson), and model-quality outcomes (denied, packed) ride
the production booking path with claims released either way.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.curation_actions import (
    _CURATE_CLAIMS,
    _CURATE_DOC_CACHE,
    _curate_doc_budget_chars,
    action_curate_drain_batch,
    release_curate_keys,
    select_curate_paper,
)
from agent.actions.scholarly_actions import read_databank
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput


def _si(effects) -> StepInput:
    return StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="curate_drain", step_id="drain"),
        effects=effects,
    )


def _bank_files(records: list[dict], markdown: dict[str, str]) -> dict[str, str]:
    files = {"databank/papers.jsonl": "\n".join(json.dumps(r) for r in records) + "\n"}
    for key, md in markdown.items():
        files[f"databank/markdown/{key}.md"] = md
    return files


def _rec(key: str, **extra) -> dict:
    return {
        "paper_key": key,
        "title": f"Paper {key}",
        "doi": f"10.1/{key}",
        "license": "cc-by",
        "year": 2024,
        "extraction_status": "extracted",
        "figure_count": 0,
        **extra,
    }


def _clear_state():
    _CURATE_CLAIMS.clear()
    _CURATE_DOC_CACHE.clear()


# ── Budget derivation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_budget_self_gates_below_whole_paper_threshold():
    _clear_state()
    # Server unreachable → 0; the 32k production cell → 0 (below threshold);
    # the grown cell → a positive char budget.
    assert await _curate_doc_budget_chars(MockEffects(pool_health={})) == 0
    small = MockEffects(pool_health={"kvPoolTokens": 32768})
    assert await _curate_doc_budget_chars(small) == 0
    big = MockEffects(pool_health={"kvPoolTokens": 65536})
    assert await _curate_doc_budget_chars(big) > 90_000


@pytest.mark.asyncio
async def test_budget_env_override(monkeypatch):
    monkeypatch.setenv("OUROBOROS_CURATE_DOC_CHARS", "12345")
    assert await _curate_doc_budget_chars(MockEffects(pool_health={})) == 12345


# ── Selection ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_selection_smallest_fitting_claims_and_oversize_skip():
    _clear_state()
    fx = MockEffects(
        files=_bank_files(
            [
                _rec("small"),
                _rec("mid"),
                _rec("huge"),
                _rec("done", review_status="denied"),
            ],
            {"small": "x" * 100, "mid": "y" * 500, "huge": "z" * 9000},
        )
    )
    bank = await read_databank(fx)
    try:
        key, doc = await select_curate_paper(fx, bank, 1000)
        assert key == "small" and len(doc) == 100
        # Concurrent selector sees the unclaimed remainder; 'huge' never fits,
        # 'done' is review-terminal.
        key2, _ = await select_curate_paper(fx, bank, 1000)
        assert key2 == "mid"
        key3, _ = await select_curate_paper(fx, bank, 1000)
        assert key3 == ""
        release_curate_keys([key, key2])
        key4, _ = await select_curate_paper(fx, bank, 1000)
        assert key4 == "small"
    finally:
        _clear_state()


# ── Drain action ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_declines_on_small_cell_without_inference():
    _clear_state()
    fx = MockEffects(
        files=_bank_files([_rec("a")], {"a": "text"}),
        pool_health={"kvPoolTokens": 32768},
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 0
    assert "threshold" in out.result["reason"]
    assert not [c for c in fx.calls if c.method == "run_inference"]


@pytest.mark.asyncio
async def test_drain_accept_pack_books_production_path():
    _clear_state()
    md = "Raman spectra were collected at 532 nm on quartz."
    fx = MockEffects(
        files=_bank_files([_rec("p1")], {"p1": md}),
        pool_health={"kvPoolTokens": 65536},
        inference_responses=[
            json.dumps({"verdict": "accept", "summary": "relevant", "issues": []}),
            json.dumps({"laser_nm": 532}),
        ],
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 1
    assert out.result["outcomes"][0]["paper_key"] == "p1"
    bank = await read_databank(fx)
    rec = bank["p1"]
    assert rec["review_status"] == "accepted"
    assert rec["pack_status"] == "packed"
    assert rec["dataset_path"]
    assert not _CURATE_CLAIMS and "p1" not in _CURATE_DOC_CACHE


@pytest.mark.asyncio
async def test_drain_denied_books_without_pack_turn():
    _clear_state()
    fx = MockEffects(
        files=_bank_files([_rec("p1")], {"p1": "off-topic text"}),
        pool_health={"kvPoolTokens": 65536},
        inference_responses=[
            json.dumps(
                {
                    "verdict": "deny",
                    "summary": "unrelated",
                    "issues": ["not spectroscopy"],
                    "deny_category": "off_topic",
                }
            ),
        ],
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["outcome"]
    bank = await read_databank(fx)
    assert bank["p1"]["review_status"] == "denied"
    assert "pack_status" not in bank["p1"]
    assert len([c for c in fx.calls if c.method == "run_inference"]) == 1
    assert not _CURATE_CLAIMS


@pytest.mark.asyncio
async def test_drain_transport_fault_books_nothing():
    _clear_state()

    class _DownEffects(MockEffects):
        async def run_inference(self, *a, **k):  # noqa: D102
            raise ConnectionError("server unreachable")

    fx = _DownEffects(
        files=_bank_files([_rec("p1")], {"p1": "some text"}),
        pool_health={"kvPoolTokens": 65536},
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 0
    bank = await read_databank(fx)
    assert "review_status" not in bank["p1"]  # paper NOT burned
    assert not _CURATE_CLAIMS  # claim released for the next round


@pytest.mark.asyncio
async def test_drain_empty_response_defers_not_books():
    """An empty response is contention, not a verdict — the paper must NOT
    be booked review_failed (that exclusion is permanent); it defers."""
    _clear_state()
    fx = MockEffects(
        files=_bank_files([_rec("p1")], {"p1": "some text"}),
        pool_health={"kvPoolTokens": 65536},
        inference_responses=["", ""],  # both review attempts come back empty
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 0
    assert "transport" in out.result["reason"]
    bank = await read_databank(fx)
    assert "review_status" not in bank["p1"]
    assert not _CURATE_CLAIMS


@pytest.mark.asyncio
async def test_drain_budget_curates_multiple_papers_serially(monkeypatch):
    """OUROBOROS_CURATE_PAPERS is a real count: budget 2 curates two papers
    in one round (serially), not a kill-switch that still does one."""
    monkeypatch.setenv("OUROBOROS_CURATE_PAPERS", "2")
    _clear_state()
    md_a = "Raman at 532 nm on quartz."
    md_b = "XRD peak at 26.6 degrees for quartz."
    fx = MockEffects(
        files=_bank_files([_rec("a"), _rec("b")], {"a": md_a, "b": md_b}),
        pool_health={"kvPoolTokens": 65536},
        inference_responses=[
            json.dumps({"verdict": "accept", "summary": "ok", "issues": []}),
            json.dumps({"laser_nm": 532}),
            json.dumps(
                {
                    "verdict": "deny",
                    "summary": "no data",
                    "issues": [],
                    "deny_category": "no_usable_data",
                }
            ),
        ],
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 2
    keys = {o["paper_key"] for o in out.result["outcomes"]}
    assert keys == {"a", "b"}
    bank = await read_databank(fx)
    assert bank["a"]["review_status"] == "accepted"
    assert bank["b"]["review_status"] == "denied"
    assert not _CURATE_CLAIMS


@pytest.mark.asyncio
async def test_thin_bin_aspects_are_selected_before_bigger_thick_bin_papers():
    """Coverage priority outranks size; size still orders within a tier."""
    _clear_state()
    fx = MockEffects(
        files=_bank_files(
            [
                _rec("tiny_xrd", source_aspects=["XRD phase identification"]),
                _rec("big_libs", source_aspects=["LIBS mineral spectra"]),
                _rec("mid_libs", source_aspects=["emission_spectroscopy"]),
            ],
            {"tiny_xrd": "x" * 50, "big_libs": "y" * 900, "mid_libs": "z" * 300},
        )
    )
    bank = await read_databank(fx)
    try:
        # Both LIBS papers precede the much smaller XRD one...
        key1, _ = await select_curate_paper(fx, bank, 1000)
        key2, _ = await select_curate_paper(fx, bank, 1000)
        assert {key1, key2} == {"big_libs", "mid_libs"}
        # ...and within the priority tier, smallest still goes first.
        assert key1 == "mid_libs"
        key3, _ = await select_curate_paper(fx, bank, 1000)
        assert key3 == "tiny_xrd"
    finally:
        release_curate_keys([key1, key2, key3])


@pytest.mark.asyncio
async def test_priority_is_finite_and_untagged_papers_still_run():
    _clear_state()
    fx = MockEffects(
        files=_bank_files(
            [_rec("plain"), _rec("libs", source_aspects=["LIBS mineral spectra"])],
            {"plain": "x" * 100, "libs": "y" * 100},
        )
    )
    bank = await read_databank(fx)
    first, _ = await select_curate_paper(fx, bank, 1000)
    second, _ = await select_curate_paper(fx, bank, 1000)
    assert first == "libs" and second == "plain"
    release_curate_keys([first, second])
