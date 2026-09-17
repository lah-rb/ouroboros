"""A local over-seat refusal routes the paper to the larger seat, not the park.

The character estimator under-counts table-heavy documents by up to 2x, so a
local lane can be handed a paper the 65k pool refuses while the declared 262k
remote seat would hold it. Parking it lost it (three papers, 2026-09-17).
"""

from __future__ import annotations

import json

import pytest

from agent.actions.curation_actions import (
    _CURATE_CLAIMS,
    _CURATE_DOC_CACHE,
    _CURATE_STARVED,
    _larger_seat_for,
    _refusal_prompt_tokens,
    action_curate_drain_batch,
    release_curate_keys,
    select_curate_paper,
)
from agent.actions.scholarly_actions import read_databank
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

REMOTE = {"curate_remote": {"seat_tokens": 262144, "model": "qwen3-next-80b-a3"}}
REFUSAL = (
    "GraphQL errors: Request cannot fit the KV pool: prompt 52322 tokens + a "
    "minimum 512-token generation exceeds the 65536-cell pool even with every "
    "evictable stream"
)


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


def _files(records, markdown):
    # Extraction-owned fields live in the sidecar (as in production): the
    # papers-side mark rewrites the papers row, which drops them by design.
    files = {
        "databank/papers.jsonl": "\n".join(json.dumps(r) for r in records) + "\n",
        "databank/extraction.jsonl": "\n".join(
            json.dumps(
                {
                    "paper_key": r["paper_key"],
                    "extraction_status": r["extraction_status"],
                    "md_path": r["md_path"],
                    "figure_count": r["figure_count"],
                }
            )
            for r in records
        )
        + "\n",
    }
    for k, md in markdown.items():
        files[f"databank/markdown/{k}.md"] = md
    return files


def _si(fx):
    return StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="curate_drain", step_id="drain"),
        effects=fx,
    )


class _LocalRefusing(MockEffects):
    """A LOCAL lane (no domain) on a fleet that declares a remote seat."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._llmvp_domains = dict(REMOTE)
        self._inference_domain = ""

    async def run_inference(self, *a, **k):  # noqa: D102
        class _R:
            error = REFUSAL
            text = ""

        return _R()


def _clear():
    _CURATE_CLAIMS.clear()
    _CURATE_DOC_CACHE.clear()
    _CURATE_STARVED.clear()


def test_refusal_prompt_tokens_reads_every_spelling():
    assert _refusal_prompt_tokens(REFUSAL) == 52322
    assert (
        _refusal_prompt_tokens(
            "Combined prompt length (70868) exceeds the model's per-stream context limit"
        )
        == 70868
    )
    assert (
        _refusal_prompt_tokens("No room to generate: prompt occupies 130933 of 131072")
        == 130933
    )
    assert _refusal_prompt_tokens("something else entirely") == 0


def test_larger_seat_only_when_another_lane_has_one():
    local = _LocalRefusing(files={})
    assert _larger_seat_for(local, 52322) == 262144
    assert _larger_seat_for(local, 300_000) == 0  # too big for every seat
    assert _larger_seat_for(local, 0) == 0
    # The remote lane's own refusal: its seat IS the largest → park path.
    remote = _LocalRefusing(files={})
    remote._inference_domain = "curate_remote"
    assert _larger_seat_for(remote, 281_318) == 0
    # A local-only fleet: nothing larger exists.
    plain = MockEffects(files={})
    assert _larger_seat_for(plain, 52322) == 0


@pytest.mark.asyncio
async def test_local_refusal_marks_the_seat_needed_instead_of_parking():
    _clear()
    fx = _LocalRefusing(
        files=_files([_rec("p1")], {"p1": "Calcite bands at 1085 and 712 cm-1. " * 40}),
        pool_health={"kvPoolTokens": 65536},
    )
    out = await action_curate_drain_batch(_si(fx))
    assert out.result["attempted"] == 0
    bank = await read_databank(fx)
    assert bank["p1"]["extraction_status"] == "extracted"  # NOT curate_oversize
    # The refused prompt (52,322 + 512) is under the local seat's nominal
    # 65,536 — the pool is shared — so the mark clears the refusing lane.
    assert bank["p1"]["curate_min_seat_tokens"] == 65536 + 1
    assert not _CURATE_CLAIMS
    # The local lane now passes it; the remote lane takes it, remote-first.
    try:
        assert await select_curate_paper(fx, bank, 200_000) == ("", "")
        remote = _LocalRefusing(files=fx._files if hasattr(fx, "_files") else {})
        remote._inference_domain = "curate_remote"
        key, doc = await select_curate_paper(remote, bank, 900_000)
        assert key == "p1" and doc
    finally:
        release_curate_keys(["p1"])
        _clear()
