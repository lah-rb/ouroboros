"""`synth` flow set — registration, compiled wiring, and the no-ladder contract.

The synthetic-corpus template bank runs as its own controller loop beside
scraper_v2/ops (operator ruling 2026-09-11): rounds until every spec cell
meets its target, pause/abort through the shared event templates, and no
phase ladder at all.
"""

from __future__ import annotations

from agent.flow_sets import FLOW_SETS, SYNTH_PHASES, evaluate_phases, get_flow_set
from agent.persistence.models import MissionConfig, MissionState
from agent.scheduler.lifecycle import POOLED_FLOW_SETS
from tests.conftest import compiled_flows as _compiled


def test_synth_registered_standalone_and_unpooled():
    assert "synth" in FLOW_SETS
    assert get_flow_set("synth").entry_flow == "synth_control"
    # A controller-driven set: no worker pool, no lane roster change.
    assert "synth" not in POOLED_FLOW_SETS
    # No ladder: a lone terminal rule (the AUTO_PHASES safety-net shape).
    assert [r.kind for r in SYNTH_PHASES] == ["terminal"]


def test_empty_phase_ladder_reads_complete():
    m = MissionState(
        objective="bank templates",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="synth"),
    )
    phase, _obs = evaluate_phases(m, SYNTH_PHASES)
    assert phase == "complete"


def test_mission_config_carries_synth_knobs():
    cfg = MissionConfig(
        working_directory="/tmp/x",
        flow_set="synth",
        synth={"cloud_share": 0.2},
        llmvp_domains={"synth_cloud": {"model": "boss-haiku"}},
    )
    assert cfg.synth == {"cloud_share": 0.2}
    assert cfg.llmvp_domains["synth_cloud"]["model"] == "boss-haiku"
    # Older mission.json files carry no block: the default is a working config.
    assert MissionConfig(working_directory="/tmp/x").synth == {}


def test_compiled_synth_control_wiring():
    c = _compiled()
    f = c["synth_control"]
    steps = f["steps"]
    assert f["entry"] == "load_state"
    assert set(steps) == {
        "load_state",
        "process_events",
        "plan_round",
        "generate_round",
        "check_done",
        "next_round",
        "completed",
        "idle",
        "aborted",
    }
    ls = {
        r["condition"]: r["transition"]
        for r in steps["load_state"]["resolver"]["rules"]
    }
    assert ls["result.mission.status == 'active'"] == "process_events"
    assert ls["result.mission.status == 'paused'"] == "idle"
    pe = {
        r["condition"]: r["transition"]
        for r in steps["process_events"]["resolver"]["rules"]
    }
    assert pe["true"] == "plan_round"
    assert pe["result.abort_requested == true"] == "aborted"
    pr = {
        r["condition"]: r["transition"]
        for r in steps["plan_round"]["resolver"]["rules"]
    }
    assert pr["result.units_ready == true"] == "generate_round"
    assert pr["result.bank_complete == true"] == "completed"
    assert pr["true"] == "idle"
    assert steps["plan_round"]["publishes"] == ["synth_units"]
    assert steps["generate_round"]["action"] == "synth_generate_round"
    assert steps["generate_round"]["publishes"] == ["synth_summary"]
    assert "synth_units" in steps["generate_round"]["context"]["required"]
    cd = {
        r["condition"]: r["transition"]
        for r in steps["check_done"]["resolver"]["rules"]
    }
    assert cd["result.done == true"] == "completed"
    assert cd["result.paused == true"] == "idle"
    assert cd["true"] == "next_round"
    # The active-loop tail call re-enters THIS controller with the mission id.
    tc = steps["next_round"]["tail_call"]
    assert tc["flow"] == "synth_control"
    assert tc["input_map"] == {"mission_id": {"$ref": "input.mission_id"}}
    assert tc["delay"] == 1
    assert steps["idle"]["tail_call"]["flow"] == "synth_control"
    assert (
        steps["completed"]["terminal"] is True
        and steps["completed"]["status"] == "completed"
    )
    assert steps["aborted"]["status"] == "aborted"


def test_actions_registered():
    from agent.actions.registry import build_action_registry

    reg = build_action_registry()
    names = set(getattr(reg, "_actions", None) or getattr(reg, "actions", {}) or [])
    if not names and hasattr(reg, "list_actions"):
        names = set(reg.list_actions())
    for a in ("synth_plan_round", "synth_generate_round", "synth_check_done"):
        assert reg.get(a) is not None if hasattr(reg, "get") else a in names
