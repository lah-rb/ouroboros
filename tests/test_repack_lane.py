"""The remote repack lane: pack-only work on another engine, and NO-BURN (2026-10-01).

The repack_r* lanes run gemma-4-12b on the 3060 box against accepted papers
still owed a pack. They never review, they pack the raw doc with the verdict
on record carried verbatim, and they book a pack ONLY when it passed the
gates: a failed pack, an over-seat window, a non-English raw doc or an engine
fault books nothing and leaves the paper for the muse lanes.
"""

from __future__ import annotations

import json

import pytest

from agent.actions import curation_actions as ca
from agent.actions.curation_actions import (
    _CURATE_BOOKED,
    _CURATE_CLAIMS,
    _CURATE_DOC_CACHE,
    _REPACK_DECLINED,
    _select_repack_paper,
    action_repack_drain_batch,
)
from agent.actions.scholarly_actions import read_databank
from agent.effects.child import ChildEffects
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

MD = "# Results\nThe quartz Raman band sits at 465 cm-1 and calcite at 1086 cm-1.\n"


def _route(seat: int = 49152) -> dict:
    return {
        "repack_remote": {
            "endpoint": "http://192.168.1.76:8008/graphql",
            "model": "gemma-4-12b-3060",
            "seat_tokens": seat,
        }
    }


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


def _accepted(key: str, **extra) -> dict:
    return _rec(
        key,
        review_status="accepted",
        review_summary="quartz and calcite Raman",
        **extra,
    )


def _lane(records, markdown, responses=(), seat=49152):
    files = {"databank/papers.jsonl": "\n".join(json.dumps(r) for r in records) + "\n"}
    for key, md in markdown.items():
        files[f"databank/markdown/{key}.md"] = md
    base = MockEffects(
        files=files,
        pool_health={"kvPoolTokens": 65536},
        inference_responses=list(responses),
    )
    base._llmvp_domains = _route(seat)
    return base, ChildEffects(
        base, branch="lane:repack_r1", inference_domain="repack_remote"
    )


def _si(fx) -> StepInput:
    return StepInput(
        context={},
        params={},
        inputs={},
        meta=FlowMeta(flow_name="repack_drain", step_id="drain"),
        effects=fx,
    )


@pytest.fixture(autouse=True)
def _clean():
    for s in (_CURATE_CLAIMS, _CURATE_DOC_CACHE, _CURATE_BOOKED, _REPACK_DECLINED):
        s.clear()
    yield
    for s in (_CURATE_CLAIMS, _CURATE_DOC_CACHE, _CURATE_BOOKED, _REPACK_DECLINED):
        s.clear()


@pytest.mark.asyncio
async def test_only_accepted_papers_owed_a_pack_are_taken_repacks_first():
    records = [
        _accepted("small_unpacked"),
        _accepted("big_repack", pack_status="needs_repack"),
        _rec("unreviewed"),
        _rec("denied", review_status="denied"),
        _accepted("done", pack_status="packed"),
    ]
    md = {k["paper_key"]: MD for k in records}
    md["big_repack"] = MD * 20
    _, fx = _lane(records, md)
    bank = await read_databank(fx)
    assert await _select_repack_paper(fx, bank) == "big_repack"
    assert await _select_repack_paper(fx, bank) == "small_unpacked"
    assert await _select_repack_paper(fx, bank) == ""


@pytest.mark.asyncio
async def test_a_passing_pack_books_on_the_production_path_with_one_turn():
    base, fx = _lane(
        [_accepted("p", pack_status="needs_repack")],
        {"p": MD},
        responses=[json.dumps({"raman_peak_wavenumber_cm-1": [465, 1086]})],
    )
    out = await action_repack_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["paper_key"] == "p"
    turns = [c for c in base.calls if c.method == "run_inference"]
    assert len(turns) == 1, "a pack turn and no review turn"
    assert turns[0].args["config_overrides"]["domain"] == "repack_remote"
    rec = (await read_databank(fx))["p"]
    assert rec["pack_status"] == "packed"
    assert rec["review_summary"] == "quartz and calcite Raman"
    assert rec["pack_doc_form"] == "raw"
    assert rec["curation_method"].startswith("gemma-4-12b-3060+")
    assert not _CURATE_CLAIMS


@pytest.mark.asyncio
async def test_a_failed_pack_books_nothing_and_leaves_the_paper_for_muse():
    invented = json.dumps({"chemical_composition_mass_fraction_pct": [0.028, 0.004]})
    _, fx = _lane(
        [_accepted("p", pack_status="needs_repack")],
        {"p": MD},
        responses=[invented, invented],  # both attempts fabricate
    )
    out = await action_repack_drain_batch(_si(fx))
    assert out.result["outcomes"][0]["outcome"].startswith("declined: gates")
    rec = (await read_databank(fx))["p"]
    assert rec["pack_status"] == "needs_repack", "nothing booked: still owed"
    assert "p" in _REPACK_DECLINED and not _CURATE_CLAIMS
    again = await action_repack_drain_batch(_si(fx))
    assert again.result["attempted"] == 0 and again.result["reason"]


@pytest.mark.asyncio
async def test_a_window_over_the_lane_seat_is_declined_without_a_turn():
    base, fx = _lane([_accepted("p")], {"p": MD * 400}, seat=20_000)
    out = await action_repack_drain_batch(_si(fx))
    assert "seat" in out.result["outcomes"][0]["outcome"]
    assert not [c for c in base.calls if c.method == "run_inference"]
    assert (await read_databank(fx))["p"].get("pack_status", "") == ""


@pytest.mark.asyncio
async def test_a_non_english_raw_doc_is_declined_without_a_turn():
    russian = "Гидрометаллургическая переработка техногенных отходов германия. " * 20
    base, fx = _lane([_accepted("p")], {"p": russian})
    out = await action_repack_drain_batch(_si(fx))
    assert "non-English" in out.result["outcomes"][0]["outcome"]
    assert not [c for c in base.calls if c.method == "run_inference"]


@pytest.mark.asyncio
async def test_a_transport_fault_ends_the_round_and_the_paper_stays_eligible(
    monkeypatch,
):
    async def down(*a, **k):
        raise ca._CurateTransportFault(
            "Connection error: all connection attempts failed"
        )

    monkeypatch.setattr(ca, "_pack_only_raw", down)
    _, fx = _lane([_accepted("p", pack_status="needs_repack")], {"p": MD})
    out = await action_repack_drain_batch(_si(fx))
    assert out.result["attempted"] == 0 and "transport fault" in out.result["reason"]
    assert "p" not in _REPACK_DECLINED and not _CURATE_CLAIMS
    assert (await read_databank(fx))["p"]["pack_status"] == "needs_repack"


def test_lanes_exist_only_when_routed_and_asked_for(monkeypatch):
    from agent.scheduler import worker_pool as wp

    monkeypatch.setenv("OUROBOROS_REMOTE_REPACK_LANES", "1")
    assert wp._repack_lanes({}) == []
    assert wp._repack_lanes(None) == []
    (lane,) = wp._repack_lanes(_route())
    assert (lane.name, lane.flow, lane.resource, lane.domain) == (
        "repack_r1",
        "repack_drain",
        "remote_repack_seat",
        "repack_remote",
    )
    assert lane.est_kv == 0 and lane.seats == 0
    assert "repack_r1" in [x.name for x in wp._all_scraper_lanes(_route())]
    monkeypatch.setenv("OUROBOROS_REMOTE_REPACK_LANES", "0")
    assert wp._repack_lanes(_route()) == []
