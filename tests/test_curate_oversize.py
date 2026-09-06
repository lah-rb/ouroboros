"""Script-aware curate sizing + the oversize park (the poison-pill fix).

Pins the 2026-08-26 incident contracts: a CJK-heavy doc is sized by its
TRUE token cost, not its char count (a 139,705-char 49%-CJK doc admitted at
70,868 tokens against a 65,536 seat while the char model claimed 42k); a
doc whose deepest compression still exceeds the seat is parked VISIBLY as
curate_oversize instead of loop-selecting (208 of 419 rounds burned); the
engine's own over-seat admission refusal — deterministic — books the same
park instead of taking the decline-and-reselect path built for transients;
and a genuine transient still declines with NOTHING booked (every one-time
transport faulter from the 2026-08-25 run was later accepted).
"""

from __future__ import annotations

import json

import pytest

from agent.actions.curation_actions import (
    _CURATE_CHARS_PER_TOKEN,
    _CURATE_CLAIMS,
    _CURATE_DOC_CACHE,
    _CURATE_STARVED,
    _effective_chars,
    _estimate_doc_tokens,
    action_curate_drain_batch,
    release_curate_keys,
    select_curate_paper,
)
from agent.actions.scholarly_actions import read_databank
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

CJK_MONSTER = "試料の分析結果を以下に示す。" * 6000  # ~84k chars, all CJK
LATIN_SMALL = "quartz raman shift measured at 464 wavenumbers. " * 40


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
    _CURATE_STARVED.clear()


# ── The estimator ────────────────────────────────────────────────────


def test_estimator_latin_matches_the_char_model():
    text = "the quick brown fox jumps over the lazy dog. " * 200
    assert _estimate_doc_tokens(text) == int(len(text) / _CURATE_CHARS_PER_TOKEN)
    # Effective chars equal real chars to within int rounding.
    assert abs(_effective_chars(text) - len(text)) <= 4


def test_estimator_cjk_counts_true_token_cost():
    text = "試" * 1400
    est = _estimate_doc_tokens(text)
    assert 950 <= est <= 1050  # ~1.4 chars/token, not len/3.3 = 424
    # A CJK doc's effective size is ~2.4x its char count — this inflation
    # is exactly what the char-only fit test was blind to.
    assert _effective_chars(text) > 2 * len(text)


# ── Selection-time park ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_select_parks_an_over_seat_cjk_floor():
    _clear_state()
    fx = MockEffects(files=_bank_files([_rec("c1")], {"c1": CJK_MONSTER}))
    bank = await read_databank(fx)
    # A budget generous enough that the OLD char test would have selected
    # it (raw ~84k chars <= 200k) — the park must fire on the SEAT, first.
    key, doc = await select_curate_paper(fx, bank, 200_000)
    assert (key, doc) == ("", "")
    after = await read_databank(fx)
    assert after["c1"]["extraction_status"] == "curate_oversize"
    assert "seat" in after["c1"]["failure_reason"]
    assert not _CURATE_CLAIMS
    assert "c1" not in _CURATE_STARVED
    assert "c1" not in _CURATE_DOC_CACHE


@pytest.mark.asyncio
async def test_select_still_serves_latin_beside_the_parked():
    _clear_state()
    fx = MockEffects(
        files=_bank_files(
            [_rec("c1"), _rec("p1")],
            {"c1": CJK_MONSTER, "p1": LATIN_SMALL},
        )
    )
    bank = await read_databank(fx)
    key, doc = await select_curate_paper(fx, bank, 200_000)
    assert key == "p1" and doc
    after = await read_databank(fx)
    assert after["c1"]["extraction_status"] == "curate_oversize"
    assert after["p1"]["extraction_status"] == "extracted"
    release_curate_keys([key])


@pytest.mark.asyncio
async def test_parked_paper_is_not_reselected():
    _clear_state()
    fx = MockEffects(files=_bank_files([_rec("c1")], {"c1": CJK_MONSTER}))
    bank = await read_databank(fx)
    await select_curate_paper(fx, bank, 200_000)  # parks it
    bank = await read_databank(fx)  # overlay now shows curate_oversize
    key, _doc = await select_curate_paper(fx, bank, 200_000)
    assert key == ""
    # Exactly one sidecar row: the park booked once, not per round.
    fc = await fx.read_file("databank/extraction.jsonl")
    rows = [json.loads(x) for x in fc.content.splitlines() if x.strip()]
    assert (
        len([r for r in rows if r.get("extraction_status") == "curate_oversize"]) == 1
    )


# ── Drain-time backstop: the engine's own refusal ────────────────────


class _OverSeatEffects(MockEffects):
    async def run_inference(self, *a, **k):  # noqa: D102
        class _R:
            error = (
                "GraphQL errors: Combined prompt length (70868) exceeds "
                "the model's per-stream context limit of 65536 tokens."
            )
            text = ""

        return _R()


@pytest.mark.asyncio
async def test_drain_books_engine_over_seat_refusal():
    _clear_state()
    fx = _OverSeatEffects(
        files=_bank_files([_rec("p1")], {"p1": LATIN_SMALL}),
        pool_health={"kvPoolTokens": 65536},
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 0
    bank = await read_databank(fx)
    assert bank["p1"]["extraction_status"] == "curate_oversize"
    assert "over-seat" in bank["p1"]["failure_reason"]
    assert not _CURATE_CLAIMS


class _TransientDownEffects(MockEffects):
    async def run_inference(self, *a, **k):  # noqa: D102
        class _R:
            error = "connection reset by peer"
            text = ""

        return _R()


@pytest.mark.asyncio
async def test_drain_transient_fault_still_declines_without_booking():
    _clear_state()
    fx = _TransientDownEffects(
        files=_bank_files([_rec("p1")], {"p1": LATIN_SMALL}),
        pool_health={"kvPoolTokens": 65536},
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 0
    assert "transport" in out.result["reason"]
    bank = await read_databank(fx)
    assert bank["p1"]["extraction_status"] == "extracted"  # NOT booked
    assert "review_status" not in bank["p1"]
    assert not _CURATE_CLAIMS


# ── fig review outage guard ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_fig_review_dead_server_declines_instead_of_booking():
    """Zero tool reports + unreachable server = decline, never a verdict.
    The tool exits empty when every vision call fails to connect; booking
    'no report from tool' mass-marked 965 papers on 08-22 and 437 on
    08-26. A dead server must cost a round, not the batch's papers."""
    from agent.actions.curation_actions import action_fig_review_batch

    class _DeadServer(MockEffects):
        async def run_command(self, *a, **k):  # noqa: D102
            class _R:
                stdout = ""  # tool produced no reports
                timed_out = False
                return_code = 1

            return _R()

        async def inference_pool_health(self):  # noqa: D102
            raise ConnectionError("server down")

    fx = _DeadServer(files=_bank_files([_rec("p1", figure_count=4)], {"p1": "x"}))
    si = _si(fx)
    si.inputs.update({"paper_keys": ["p1"], "working_directory": "/tmp/x"})
    out = await action_fig_review_batch(si)
    assert "unreachable" in (out.result or {}).get("reason", "")
    bank = await read_databank(fx)
    assert bank["p1"].get("figtext_status") is None  # NOT booked failed


@pytest.mark.asyncio
async def test_park_carries_the_full_sidecar_row_not_a_stub():
    """A park must not erase the paper's extraction record.

    The sidecar is last-row-wins, so a three-field park row shadowed
    md_path, figure_count and extraction_quality -- and translated /
    md_en_path on translated papers. Measured 2026-09-06: 0 of 28 parked
    rows still carried extraction_quality; the history beneath each did.
    """
    _CURATE_CLAIMS.clear()
    # figtext must be terminal or the paper is skipped before the size check
    files = _bank_files(
        [_rec("c1", figtext_status="figtext_done")], {"c1": CJK_MONSTER}
    )
    prior = {
        "paper_key": "c1",
        "extraction_status": "extracted",
        "md_path": "databank/markdown/c1.md",
        "figure_count": 25,
        "extraction_method": "paddle",
        "extraction_quality": {"numeric_match_rate": 0.99},
        "translated": True,
        "md_en_path": "databank/markdown/c1.en.md",
    }
    files["databank/extraction.jsonl"] = json.dumps(prior) + "\n"
    fx = MockEffects(files=files)
    bank = await read_databank(fx)
    key, _ = await select_curate_paper(fx, bank, 10_000)
    assert key == ""  # the monster was parked, not selected
    rows = [
        json.loads(ln)
        for ln in (await fx.read_file("databank/extraction.jsonl")).content.splitlines()
        if ln.strip()
    ]
    last = rows[-1]
    assert last["extraction_status"] == "curate_oversize"
    assert last["failure_reason"].startswith("curation:")
    for field in (
        "md_path",
        "figure_count",
        "extraction_method",
        "extraction_quality",
        "translated",
        "md_en_path",
    ):
        assert last[field] == prior[field], f"park erased {field}"
    _CURATE_CLAIMS.clear()


@pytest.mark.asyncio
async def test_an_accepted_paper_beyond_every_seat_is_pack_only_not_parked():
    """The review needs the whole doc under a seat; the pack does not. An
    accepted paper whose deepest compression still exceeds every seat must
    be selected for a pack-only turn over raw text, not parked."""
    _CURATE_CLAIMS.clear()
    fx = MockEffects(
        files=_bank_files(
            [
                _rec(
                    "c1",
                    review_status="accepted",
                    review_summary="in scope",
                    figtext_status="figtext_done",
                )
            ],
            {"c1": CJK_MONSTER},
        )
    )
    bank = await read_databank(fx)
    key, doc = await select_curate_paper(fx, bank, 10_000)
    assert key == "c1" and doc == "", "pack-only is signalled by an empty doc"
    after = await read_databank(fx)
    assert after["c1"]["extraction_status"] != "curate_oversize", "must not be parked"
    _CURATE_CLAIMS.clear()
