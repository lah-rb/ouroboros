"""Content-goal fan-out (create_content_batch): sweep gate + burst action.

Pins: the sweep dispatches ONE content batch when >=2 data-file goals are
missing post-symbol-swarm (one-shot via the content_batch note; single
missing file and serial mode fall to the serial create path unchanged);
the burst action generates each file from its registry-enriched goal
description, fence-strips, parse-gates with _parse_data_file (one
error-threaded retry), books per-goal DirectiveReports, completes passing
goals, and leaves failures to the serial path; wiring — all three
controllers carry the needs_content_batch rule + dispatch step.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.contract_swarm_actions import action_swarm_generate_content
from agent.actions.mission_actions import action_structural_sweep_next
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    DirectiveReport,
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
    NoteRecord,
)

_GOOD_YAML = "rooms:\n  - id: cave\n    name: Cave\n"
_BAD_YAML = "rooms:\n  - id: [broken\n    name: Cave\n"


def _mission(tmp_path: Path, mode="batch", notes=None, data_files=None) -> MissionState:
    data_files = data_files if data_files is not None else ["rooms.yaml", "items.yaml"]
    goals = [
        GoalRecord(
            description=f"Create {f} with content: enriched brief for {f}",
            type="structural",
            associated_files=[f],
        )
        for f in data_files
    ]
    # a code goal with a report marks the symbol batch as attempted
    goals.append(
        GoalRecord(
            description="engine",
            type="structural",
            associated_files=["engine.py"],
            reports=[
                DirectiveReport(flow="build_structure", status="success", summary="s")
            ],
            status="complete",
        )
    )
    (tmp_path / "engine.py").write_text("x = 1\n")
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path), structural_mode=mode),
        architecture=ArchitectureState(
            run_command="python engine.py",
            creation_order=["engine.py"] + data_files,
            modules=[ModuleSpec(file="engine.py", responsibility="entry")],
        ),
        goals=goals,
        notes=notes or [],
    )


def _sweep_si(mission, effects=None) -> StepInput:
    return StepInput(
        context={"mission": mission},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="structural_sweep_next"),
        effects=effects or MockEffects(mission=mission),
    )


def _batch_si(mission, fx, tmp_path) -> StepInput:
    return StepInput(
        context={"mission": mission},
        inputs={"working_directory": str(tmp_path)},
        params={},
        meta=FlowMeta(flow_name="create_content_batch", step_id="fan_out_content"),
        effects=fx,
    )


# ── sweep gate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_dispatches_content_batch_when_two_data_files_missing(tmp_path):
    mission = _mission(tmp_path)
    out = await action_structural_sweep_next(_sweep_si(mission))
    assert out.result.get("needs_content_batch") is True
    cfg = out.context_updates["dispatch_config"]
    assert cfg["flow"] == "create_content_batch"
    assert sorted(cfg["goal_files"]) == ["items.yaml", "rooms.yaml"]


@pytest.mark.asyncio
async def test_sweep_content_batch_is_one_shot_via_note(tmp_path):
    mission = _mission(
        tmp_path,
        notes=[
            NoteRecord(
                content="done", category="codebase_observation", tags=["content_batch"]
            )
        ],
    )
    out = await action_structural_sweep_next(_sweep_si(mission))
    assert out.result.get("needs_content_batch") is None
    assert out.result.get("needs_create") is True  # serial fallback


@pytest.mark.asyncio
async def test_sweep_single_missing_data_file_stays_serial(tmp_path):
    mission = _mission(tmp_path, data_files=["rooms.yaml"])
    out = await action_structural_sweep_next(_sweep_si(mission))
    assert out.result.get("needs_content_batch") is None
    assert out.result.get("needs_create") is True


@pytest.mark.asyncio
async def test_sweep_serial_mode_never_content_batches(tmp_path):
    mission = _mission(tmp_path, mode="serial")
    out = await action_structural_sweep_next(_sweep_si(mission))
    assert out.result.get("needs_content_batch") is None


# ── the burst action ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_content_batch_generates_books_and_completes(tmp_path):
    mission = _mission(tmp_path)
    fx = MockEffects(
        mission=mission,
        inference_responses=[_GOOD_YAML, "```yaml\n" + _GOOD_YAML + "```"],
    )
    out = await action_swarm_generate_content(_batch_si(mission, fx, tmp_path))
    assert out.result["any_ok"] is True and out.result["n_targets"] == 2
    assert sorted(out.context_updates["files_changed"]) == [
        "items.yaml",
        "rooms.yaml",
    ]
    for f in ("rooms.yaml", "items.yaml"):
        goal = next(g for g in mission.goals if f in (g.associated_files or []))
        assert goal.status == "complete"
        assert goal.reports[-1].flow == "create_content_batch"
        assert goal.reports[-1].status == "success"
    assert any("content_batch" in (n.tags or []) for n in mission.notes)
    writes = [c for c in fx.calls if c.method == "write_file"]
    assert len(writes) == 2  # fence-stripped output written for both


@pytest.mark.asyncio
async def test_content_batch_retries_on_parse_failure_then_passes(tmp_path):
    mission = _mission(tmp_path, data_files=["rooms.yaml", "npcs.yaml"])
    fx = MockEffects(
        mission=mission,
        inference_responses=[_BAD_YAML, _GOOD_YAML, _GOOD_YAML],
    )
    out = await action_swarm_generate_content(_batch_si(mission, fx, tmp_path))
    assert out.result["any_ok"] is True
    n_inferences = sum(1 for c in fx.calls if c.method == "run_inference")
    assert n_inferences == 3  # one retry for the parse failure
    # both goals completed — the retry recovered the malformed first output
    assert all(
        g.status == "complete"
        for g in mission.goals
        if any(f.endswith(".yaml") for f in (g.associated_files or []))
    )


@pytest.mark.asyncio
async def test_content_batch_failure_leaves_goal_for_serial_path(tmp_path):
    mission = _mission(tmp_path, data_files=["rooms.yaml", "npcs.yaml"])
    fx = MockEffects(
        mission=mission,
        inference_responses=[_BAD_YAML, _BAD_YAML, _GOOD_YAML],
    )
    out = await action_swarm_generate_content(_batch_si(mission, fx, tmp_path))
    assert out.result["any_ok"] is True  # one of two landed
    failed_goal = next(
        g for g in mission.goals if "rooms.yaml" in (g.associated_files or [])
    )
    assert failed_goal.status != "complete"
    assert failed_goal.reports[-1].status == "failed"
    assert "syntax: rooms.yaml" in failed_goal.reports[-1].checks_failed
    # the one-shot note still books — the sweep falls back to serial create
    assert any("content_batch" in (n.tags or []) for n in mission.notes)


@pytest.mark.asyncio
async def test_content_batch_skips_existing_files(tmp_path):
    mission = _mission(tmp_path)
    (tmp_path / "rooms.yaml").write_text(_GOOD_YAML)
    fx = MockEffects(mission=mission, inference_responses=[_GOOD_YAML])
    out = await action_swarm_generate_content(_batch_si(mission, fx, tmp_path))
    assert out.result["n_targets"] == 1  # only items.yaml


# ── wiring ────────────────────────────────────────────────────────────


def test_all_three_controllers_carry_content_batch_dispatch():
    compiled = json.loads(
        (Path(__file__).parent.parent / "flows" / "compiled.json").read_text()
    )
    assert "create_content_batch" in compiled
    for ctrl in (
        "mission_control",
        "mission_control_contracted",
        "mission_control_swarm",
    ):
        steps = compiled[ctrl]["steps"]
        assert "dispatch_content_batch" in steps
        assert steps["dispatch_content_batch"]["tail_call"]["flow"] == (
            "create_content_batch"
        )
        rules = steps["structural_sweep_next"]["resolver"]["rules"]
        assert any("needs_content_batch" in r.get("condition", "") for r in rules)
