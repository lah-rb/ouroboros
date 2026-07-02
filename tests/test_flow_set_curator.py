"""Curator flow set: registration, phases, compiled wiring.

Stage three of the corpus pipeline. Pins the phase-name contract with
CURATOR_PHASES, the dispatch input_maps, and the structural invariant
that no curator flow declares a prompt-template turn step — session
turns are fired from actions (data-dependent content), so the flows
themselves stay deterministic.
"""

from __future__ import annotations

import json
import os

import pytest

from agent.actions.mission_actions import action_check_pipeline_phase
from agent.flow_sets import FLOW_SETS, get_flow_set
from agent.models import FlowMeta, StepInput
from agent.persistence.models import GoalRecord, MissionConfig, MissionState


def _mission(goals=None) -> MissionState:
    return MissionState(
        objective="curate corpus",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="curator"),
        goals=goals or [],
    )


def _si(mission) -> StepInput:
    return StepInput(
        context={"mission": mission},
        params={},
        meta=FlowMeta(flow_name="curate_control", step_id="check_phase"),
        effects=None,
    )


def test_curator_registered_with_entry_flow():
    assert "curator" in FLOW_SETS
    assert get_flow_set("curator").entry_flow == "curate_control"


@pytest.mark.asyncio
async def test_phase_order_fig_review_then_curate_then_gate():
    fig = GoalRecord(description="f", type="fig_review", status="incomplete")
    cur = GoalRecord(description="c", type="curate", status="incomplete")

    out = await action_check_pipeline_phase(_si(_mission([fig, cur])))
    assert out.result["phase"] == "fig_review"

    fig2 = GoalRecord(description="f", type="fig_review", status="complete")
    out = await action_check_pipeline_phase(_si(_mission([fig2, cur])))
    assert out.result["phase"] == "curate"

    cur2 = GoalRecord(description="c", type="curate", status="complete")
    out = await action_check_pipeline_phase(_si(_mission([fig2, cur2])))
    assert out.result["phase"] == "curate_gate"


def _compiled():
    with open(os.path.join("flows", "compiled.json")) as f:
        return json.load(f)


def test_compiled_control_routing_matches_phases():
    steps = _compiled()["curate_control"]["steps"]
    rules = steps["check_phase"]["resolver"]["rules"]
    transitions = {r["condition"]: r["transition"] for r in rules}
    assert transitions["result.phase == 'fig_review'"] == "fig_review_sweep_next"
    assert transitions["result.phase == 'curate'"] == "curate_sweep_next"
    assert transitions["result.phase == 'curate_gate'"] == "dispatch_curate_gate"
    fig = steps["dispatch_fig_review"]["tail_call"]
    assert fig["flow"] == "fig_review" and "paper_keys" in fig["input_map"]
    cur = steps["dispatch_curate"]["tail_call"]
    assert cur["flow"] == "curate_paper" and "paper_key" in cur["input_map"]


def test_curate_paper_wiring_book_result_is_single_exit():
    steps = _compiled()["curate_paper"]["steps"]
    # Every non-return step routes into book_result — the structural
    # guarantee that session end + snapshot purge run on all paths.
    ingest = {r["transition"] for r in steps["ingest_review"]["resolver"]["rules"]}
    assert ingest == {"pack_data", "book_result"}
    pack = {r["transition"] for r in steps["pack_data"]["resolver"]["rules"]}
    assert pack == {"book_result"}
    assert steps["book_result"]["action"] == "curate_book_result"


def test_curator_flows_declare_no_turn_steps():
    compiled = _compiled()
    for flow in ("curate_control", "fig_review", "curate_paper", "curate_gate"):
        for name, step in compiled[flow]["steps"].items():
            assert not step.get("turn"), (
                f"{flow}.{name} declares a prompt-template turn — curator "
                f"session turns are action-driven by design"
            )
            assert step.get("action") != "inference"
