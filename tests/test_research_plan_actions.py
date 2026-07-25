"""Research planning + sweeps: plan parse, goal idempotency, dispatch rules.

Pins: tolerant plan parsing, signature-idempotent goal derivation
(aspects ARE the decomposition — no inference pass), discovery
completion rules (target met, MAX_DISCOVERY_ROUNDS cap), catalog batch
selection (needs_retag priority, batch cap, empty worklist completes
the corpus goal), and harvest reopen paths with the notes-freshen guard.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.research_plan_actions import (
    CORPUS_GOAL_SIGNATURE,
    MAX_DISCOVERY_ROUNDS,
    action_catalog_sweep_next,
    action_derive_research_goals,
    action_discovery_sweep_next,
    action_harvest_research_findings,
    action_parse_and_store_research_plan,
)
from agent.actions.scholarly_actions import read_databank
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    AspectSpec,
    DirectiveReport,
    MissionConfig,
    MissionState,
    NoteRecord,
    ResearchPlanState,
)
from tests.conftest import papers_bank as _bank


def _mission(aspects=None):
    return MissionState(
        objective="HEA research abstract",
        config=MissionConfig(working_directory="/tmp/x", flow_set="scraper"),
        research_plan=(
            ResearchPlanState(abstract="a", aspects=aspects) if aspects else None
        ),
    )


def _si(mission, effects=None, **ctx) -> StepInput:
    return StepInput(
        context={"mission": mission, **ctx},
        params={},
        meta=FlowMeta(flow_name="research_control", step_id="x"),
        effects=effects or MockEffects(),
    )


@pytest.mark.asyncio
async def test_plan_parse_and_goal_derivation_idempotent():
    m = _mission()
    plan_json = json.dumps(
        {
            "aspects": [
                {"name": "gb segregation", "queries": ["q1"], "target": 4},
                {"aspect": "phase stability"},  # drifted field names
                {"description": "nameless — dropped"},
            ],
            "notes": "n",
        }
    )
    out = await action_parse_and_store_research_plan(
        _si(m, inference_response=f"```json\n{plan_json}\n```")
    )
    assert out.result == {"plan_parsed": True, "aspect_count": 2}
    assert m.research_plan.abstract == "HEA research abstract"

    out = await action_derive_research_goals(_si(m))
    assert out.result["goal_count"] == 3  # 2 discovery + 1 corpus
    # Idempotent re-entry.
    out = await action_derive_research_goals(_si(m))
    assert out.result["goal_count"] == 0
    sigs = {g.finding_signature for g in m.goals}
    assert "aspect-discovery:gb-segregation" in sigs
    assert CORPUS_GOAL_SIGNATURE in sigs


@pytest.mark.asyncio
async def test_plan_parse_failure_is_explicit():
    out = await action_parse_and_store_research_plan(
        _si(_mission(), inference_response="not json at all")
    )
    assert out.result["plan_parsed"] is False


@pytest.mark.asyncio
async def test_discovery_dispatches_until_target_then_completes():
    m = _mission([AspectSpec(name="gb", coverage_target=2, seed_queries=["q"])])
    await action_derive_research_goals(_si(m))

    fx = MockEffects()  # empty databank -> 0/2
    out = await action_discovery_sweep_next(_si(m, effects=fx))
    assert out.result.get("needs_discover") is True
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "discover" and dc["aspect_name"] == "gb"
    assert dc["have_count"] == 0

    fx2 = MockEffects(
        files=_bank(
            [
                {"paper_key": "p1", "status": "candidate", "source_aspects": ["gb"]},
                {"paper_key": "p2", "status": "candidate", "source_aspects": ["gb"]},
            ]
        )
    )
    out = await action_discovery_sweep_next(_si(m, effects=fx2))
    assert out.result.get("sweep_complete") is True
    goal = next(g for g in m.goals if g.type == "discovery")
    assert goal.status == "complete"


@pytest.mark.asyncio
async def test_discovery_round_cap_prevents_thin_literature_loop():
    m = _mission([AspectSpec(name="gb", coverage_target=50)])
    await action_derive_research_goals(_si(m))
    goal = next(g for g in m.goals if g.type == "discovery")
    goal.reports = [
        DirectiveReport(flow="discover", status="success", summary="round")
        for _ in range(MAX_DISCOVERY_ROUNDS)
    ]
    out = await action_discovery_sweep_next(_si(m))
    assert out.result.get("sweep_complete") is True
    assert goal.status == "complete"  # gate will report residual shortfall


@pytest.mark.asyncio
async def test_catalog_sweep_prioritizes_retag_and_caps_batch():
    m = _mission([AspectSpec(name="gb")])
    await action_derive_research_goals(_si(m))
    records = [{"paper_key": f"c{i}", "status": "candidate"} for i in range(6)] + [
        {"paper_key": "r1", "status": "needs_retag"}
    ]
    fx = MockEffects(files=_bank(records))
    out = await action_catalog_sweep_next(_si(m, effects=fx))
    keys = out.context_updates["dispatch_config"]["paper_keys"]
    assert len(keys) == 5
    assert keys[0] == "r1"  # retag first


@pytest.mark.asyncio
async def test_catalog_sweep_completes_on_empty_worklist():
    m = _mission([AspectSpec(name="gb")])
    await action_derive_research_goals(_si(m))
    fx = MockEffects(files=_bank([{"paper_key": "p", "status": "cataloged"}]))
    out = await action_catalog_sweep_next(_si(m, effects=fx))
    assert out.result.get("sweep_complete") is True
    corpus = next(g for g in m.goals if g.type == "extraction")
    assert corpus.status == "complete"


@pytest.mark.asyncio
async def test_harvest_reopens_goals_and_marks_retag_with_note_freshen():
    m = _mission([AspectSpec(name="gb", coverage_target=5)])
    await action_derive_research_goals(_si(m))
    for g in m.goals:
        g.status = "complete"

    # Disk mission carries a gate-pushed note the context mission lacks.
    disk = m.model_copy(deep=True)
    disk.notes.append(
        NoteRecord(content="gate note", category="failure_analysis", source_flow="g")
    )
    fx = MockEffects(
        mission=disk,
        files=_bank([{"paper_key": "p1", "status": "cataloged", "tags": []}]),
    )
    gate_results = {
        "verdict": "fail",
        "blocking_issues": [
            {"class": "coverage", "aspect": "gb", "have": 1, "want": 5},
            {"class": "grounding", "paper_key": "p1", "aspect": "gb"},
        ],
    }
    out = await action_harvest_research_findings(
        _si(m, effects=fx, gate_results=gate_results)
    )
    assert out.result.get("harvested") is True
    disco = next(g for g in m.goals if g.type == "discovery")
    corpus = next(g for g in m.goals if g.type == "extraction")
    assert disco.status == "incomplete" and disco.reports == []
    assert corpus.status == "incomplete"
    bank = await read_databank(fx)
    assert bank["p1"]["status"] == "needs_retag"
    # Notes freshened from disk before save (lost-update guard).
    assert any(n.content == "gate note" for n in m.notes)
