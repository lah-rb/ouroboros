"""A paper that makes the curate model degenerate is not handed back forever.

A transport fault declines the round without burning the paper — right for a
dropped connection, wrong for the engine's repetition guard: the same document
orbits the same model again and the selector returns it at once. Overnight
2026-09-19 the remote seat spent 3.5 hours on two such documents while 70
repacks waited. After three degenerate faults an ACCEPTED paper is booked
pack_failed with the count in pack_quality; an unreviewed one is skipped for
the process.
"""

from __future__ import annotations

import json

import pytest

from agent.actions import curation_actions as C
from agent.actions.curation_actions import action_curate_drain_batch
from agent.actions.scholarly_actions import read_databank
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

REMOTE = {"curate_remote": {"seat_tokens": 262144, "model": "qwen3-next-80b-a3"}}
ORBIT = "GraphQL errors: cycle period 7 x 12 (aborted after 4149 generated tokens)"


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


def _files(records: list[dict]) -> dict:
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
    for r in records:
        files[r["md_path"]] = "# Paper\n\n" + " ".join(
            f"Band {i} at {400 + i} cm-1 in sample S{i}." for i in range(400)
        )
    return files


def _si(fx):
    return StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="curate_drain", step_id="drain"),
        effects=fx,
    )


class _RemoteOrbiting(MockEffects):
    """The remote lane; every turn dies in the engine's repetition guard."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._llmvp_domains = dict(REMOTE)
        self._inference_domain = "curate_remote"
        self.n_calls = 0

    async def run_inference(self, *a, **k):  # noqa: D102
        self.n_calls += 1

        class _R:
            error = ORBIT
            text = ""

        return _R()


def _reset():
    C._CURATE_CLAIMS.clear()
    C._CURATE_DOC_CACHE.clear()
    C._CURATE_STARVED.clear()
    C._CURATE_BOOKED.clear()
    C._CURATE_DEGENERATE_FAULTS.clear()
    C._CURATE_SKIP.clear()


@pytest.mark.asyncio
async def test_three_degenerate_faults_book_an_accepted_paper_pack_failed():
    _reset()
    fx = _RemoteOrbiting(
        files=_files([_rec("p1", review_status="accepted", pack_status="needs_repack")])
    )
    seen = []
    for _ in range(3):
        out = await action_curate_drain_batch(_si(fx))
        seen.append(str(out.observations))
    assert "transport fault" in seen[0] and "transport fault" in seen[1]
    db = await read_databank(fx)
    rec = db["p1"]
    assert rec["pack_status"] == "pack_failed"
    assert rec["pack_quality"]["degenerate_faults"] == 3
    assert "cycle period" in rec["pack_quality"]["last_fault"]
    assert rec["review_status"] == "accepted"  # a pack failure, not a denial
    assert (
        rec.get("md_path") and rec.get("extraction_status") == "extracted"
    )  # full row kept
    # the paper has left the queue: the next round has nothing to take
    out = await action_curate_drain_batch(_si(fx))
    assert "nothing unclaimed" in str(out.observations)


@pytest.mark.asyncio
async def test_unreviewed_paper_is_skipped_for_the_process_after_three_faults():
    _reset()
    fx = _RemoteOrbiting(files=_files([_rec("p2")]))
    for _ in range(3):
        await action_curate_drain_batch(_si(fx))
    assert "p2" in C._CURATE_SKIP
    db = await read_databank(fx)
    assert (
        db["p2"].get("review_status") is None
    )  # nothing booked: a skip, not a verdict
    calls = fx.n_calls
    out = await action_curate_drain_batch(_si(fx))
    assert "nothing unclaimed" in str(out.observations)
    assert fx.n_calls == calls  # not asked again


@pytest.mark.asyncio
async def test_a_non_degenerate_transport_fault_is_not_counted():
    _reset()

    class _Disconnect(_RemoteOrbiting):
        async def run_inference(self, *a, **k):  # noqa: D102
            class _R:
                error = "GraphQL errors: client disconnected — generation retired server-side"
                text = ""

            return _R()

    fx = _Disconnect(
        files=_files([_rec("p3", review_status="accepted", pack_status="needs_repack")])
    )
    for _ in range(4):
        out = await action_curate_drain_batch(_si(fx))
        assert "transport fault" in str(out.observations)
    db = await read_databank(fx)
    assert db["p3"]["pack_status"] == "needs_repack"
    assert C._CURATE_DEGENERATE_FAULTS == {}
