"""Translation drain, bank-and-repair policy (operator ruling 2026-09-17).

Follows the OCR drain: bounded resumable rounds, every failure RECORDED in the
parts bank and retried behind untried chunks, a salvage pass in smaller pieces,
gaps kept in the source language and flagged for repair, and retirement only
when nothing translated or over half the paper would be gaps.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.translation_actions import (
    _GAP_CLOSE,
    _TRANSLATE_CLAIMS,
    _TRANSLATE_DEFERRED,
    _load_failures,
    _parts_path,
    action_translate_drain_batch,
    chunk_markdown,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

ERR = "GraphQL errors: cycle period 3 x 12 (aborted after 900 generated tokens)"


def _para(i: int, marker: str = "", n: int = 300) -> str:
    return " ".join(
        f"{marker}sample{j} of series {i} yields {i * 1000 + j} counts"
        for j in range(n)
    )


def _rec(**extra) -> dict:
    return {
        "paper_key": "p1",
        "extraction_status": "extract_lingual",
        "md_path": "databank/markdown/p1.md",
        "review_status": "accepted",
        "language": "ru",
        **extra,
    }


class _Scripted(MockEffects):
    """Identity translations, except spans carrying BAD (always fail) or BIG
    (fail only as a whole chunk — pieces under 8,000 chars translate)."""

    async def run_inference(self, prompt, config=None, **k):  # noqa: D102
        span = prompt.rsplit("):\n\n", 1)[-1].strip()

        class _R:
            error = None
            text = span

        if "BAD" in span or ("BIG" in span and len(span) > 8000):
            _R.error = ERR
            _R.text = ""
        return _R()


def _fx(src: str, rec: dict, extra_files: dict | None = None) -> _Scripted:
    files = {
        "databank/papers.jsonl": json.dumps(rec) + "\n",
        "databank/markdown/p1.md": src,
    }
    files.update(extra_files or {})
    return _Scripted(files=files)


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
async def test_failing_chunk_is_banked_and_the_paper_completes_with_a_gap(monkeypatch):
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    src = "\n\n".join([_para(0), _para(1, "BAD"), _para(2)])
    chunks = chunk_markdown(src)
    assert len(chunks) == 3
    fx = _fx(src, _rec())

    out1 = await action_translate_drain_batch(_si(fx))
    assert out1.result["status"] == "progress"
    assert out1.result["banked"] == 2 and out1.result["failing"] == 1
    side = _side(fx)
    # Progress beside a failure costs NO attempt; the failure is on record.
    assert int(side.get("translate_attempts") or 0) == 0
    assert side["extraction_status"] == "extract_lingual"
    assert "banked" in side["failure_reason"]
    assert await _load_failures(fx, "p1", 3, len(src), 0) == {1: [ERR]}
    assert "p1" in _TRANSLATE_DEFERRED and not _TRANSLATE_CLAIMS

    out2 = await action_translate_drain_batch(
        _si(fx)
    )  # only the bad chunk: no progress
    assert out2.result["status"] == "progress"
    assert _side(fx)["translate_attempts"] == 1

    out3 = await action_translate_drain_batch(_si(fx))  # third failure → salvage → gap
    assert out3.result["status"] == "translated"
    side = _side(fx)
    assert side["extraction_status"] == "extracted" and side["translated"] is True
    q = side["translation_quality"]
    assert q["chunks"] == 3 and q["gap_chunks"] == 1 and q["gaps"][0]["idx"] == 1
    assert q["numeric_preservation"] == 1.0
    en = (await fx.read_file(side["md_en_path"])).content
    assert "translation gap: chunk 2 of 3" in en and _GAP_CLOSE in en
    assert chunks[1] in en and chunks[0] in en and chunks[2] in en
    # The bank is KEPT for repair when gaps remain.
    bank = (await fx.read_file(_parts_path("p1"))).content.strip().splitlines()
    assert any('"failed": true' in ln for ln in bank) and any(
        '"text"' in ln for ln in bank
    )
    assert not _TRANSLATE_CLAIMS and "p1" not in _TRANSLATE_DEFERRED
    _clear()


@pytest.mark.asyncio
async def test_salvage_translates_a_failing_chunk_in_pieces(monkeypatch):
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    # Chunk 1 is three ~3.5k blocks: fails whole (>8k chars), passes in pieces.
    big_blocks = [_para(10 + b, "BIG", n=85) for b in range(3)]
    src = "\n\n".join([_para(0), *big_blocks, _para(2)])
    chunks = chunk_markdown(src)
    assert len(chunks) == 3 and len(chunks[1]) > 8000
    fx = _fx(src, _rec())
    for _ in range(2):
        out = await action_translate_drain_batch(_si(fx))
        assert out.result["status"] == "progress"
    out = await action_translate_drain_batch(_si(fx))
    assert out.result["status"] == "translated"
    side = _side(fx)
    assert side["translation_quality"]["gap_chunks"] == 0
    en = (await fx.read_file(side["md_en_path"])).content
    assert "translation gap" not in en and chunks[1] in en
    # A clean pass reclaims the bank.
    assert not (await fx.read_file(_parts_path("p1"))).content.strip()
    _clear()


@pytest.mark.asyncio
async def test_nothing_banked_at_the_cap_retires_the_paper_but_keeps_the_bank(
    monkeypatch,
):
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    src = "\n\n".join([_para(0, "BAD"), _para(1, "BAD")])
    fx = _fx(src, _rec(translate_attempts=2, translate_epoch=2, pack_status=""))
    out = await action_translate_drain_batch(_si(fx))
    assert out.result["status"] == "failed"
    side = _side(fx)
    assert side["extraction_status"] == "translate_failed"
    assert side["translate_attempts"] == 3
    assert side["translation_quality"] == {
        "chunks": 2,
        "banked": 0,
        "failed_chunks": [0, 1],
    }
    bank = (await fx.read_file(_parts_path("p1"))).content.strip().splitlines()
    assert len(bank) == 2 and all('"failed": true' in ln for ln in bank)
    papers = json.loads(fx._files["databank/papers.jsonl"].strip().splitlines()[-1])
    assert papers["pack_status"] == "pack_failed"
    _clear()


DOWN = "Connection error: All connection attempts failed"


class _Down(MockEffects):
    """LLMVP unreachable: every chunk fails the way run v50c's outage did."""

    async def run_inference(self, prompt, config=None, **k):  # noqa: D102
        class _R:
            error = DOWN
            text = ""

        return _R()


@pytest.mark.asyncio
async def test_a_dead_server_spends_no_attempt_and_banks_nothing(monkeypatch):
    """Run v50c (2026-09-29): five hours against a dead server spent an attempt
    on ~100 papers and retired two. A transport-only round must leave the
    paper exactly as it was, one attempt short of retirement or not."""
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    src = "\n\n".join([_para(0), _para(1)])
    rec = _rec(translate_attempts=2, translate_epoch=2, pack_status="needs_repack")
    fx = _Down(
        files={
            "databank/papers.jsonl": json.dumps(rec) + "\n",
            "databank/markdown/p1.md": src,
        }
    )
    out = await action_translate_drain_batch(_si(fx))
    assert out.result["status"] == "deferred"
    assert "databank/extraction.jsonl" not in fx._files  # nothing booked
    parts = fx._files.get(_parts_path("p1"), "")
    assert '"failed": true' not in parts  # nothing banked
    papers = json.loads(fx._files["databank/papers.jsonl"].strip().splitlines()[-1])
    assert papers["pack_status"] == "needs_repack"
    assert "p1" in _TRANSLATE_DEFERRED and not _TRANSLATE_CLAIMS
    _clear()


@pytest.mark.asyncio
async def test_banked_transport_failures_do_not_gap_a_chunk():
    """Parts files written during the outage hold connection errors; three of
    them would gap a chunk. They are ignored; real failures still count."""
    src = "x" * 100
    lines = [
        {"idx": 1, "n": 3, "src_len": 100, "attempt": 0, "failed": True, "reason": r}
        for r in (DOWN, DOWN, "Server disconnected without sending a response.", ERR)
    ]
    fx = MockEffects(
        files={_parts_path("p1"): "".join(json.dumps(d) + "\n" for d in lines)}
    )
    assert await _load_failures(fx, "p1", 3, len(src), 0) == {1: [ERR]}


@pytest.mark.asyncio
async def test_more_than_half_gaps_retires_with_the_gap_list_recorded(monkeypatch):
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    src = "\n\n".join([_para(0), _para(1, "BAD"), _para(2, "BAD"), _para(3, "BAD")])
    # Chunks 1–3 already carry three recorded failures each (epoch 0).
    seeded = "".join(
        json.dumps(
            {
                "idx": i,
                "n": 4,
                "src_len": len(src),
                "attempt": 0,
                "failed": True,
                "reason": ERR,
            }
        )
        + "\n"
        for i in (1, 2, 3)
        for _ in range(3)
    )
    fx = _fx(src, _rec(), {_parts_path("p1"): seeded})
    out = await action_translate_drain_batch(_si(fx))
    assert out.result["status"] == "failed"
    side = _side(fx)
    assert side["extraction_status"] == "translate_failed"
    assert "would be gaps" in side["failure_reason"]
    assert side["translation_quality"]["gap_chunks"] == 3
    assert [g["idx"] for g in side["translation_quality"]["gaps"]] == [1, 2, 3]
    bank = (await fx.read_file(_parts_path("p1"))).content.strip().splitlines()
    assert any('"text"' in ln for ln in bank)  # chunk 0 banked and kept
    _clear()


@pytest.mark.asyncio
async def test_a_completed_translation_reopens_a_pack_failed_paper(monkeypatch):
    _clear()
    monkeypatch.setenv("OUROBOROS_TRANSLATE_CHUNKS", "8")
    src = _para(0)
    fx = _fx(src, _rec(pack_status="pack_failed"))
    out = await action_translate_drain_batch(_si(fx))
    assert out.result["status"] == "translated"
    papers = json.loads(fx._files["databank/papers.jsonl"].strip().splitlines()[-1])
    assert papers["pack_status"] == "needs_repack"
    _clear()
