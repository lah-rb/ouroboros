"""Failed-goal diagnosis fan-out (diagnose_batch): candidates, gate, burst.

Pins: candidate selection (gate-failed structural goals with the file on
disk; diagnose-family reports, triaged-ledger entries, and import-decision
cases excluded); the sweep dispatches ONE triage burst at >=2 candidates
and stays serial below; a confident worker books the diagnose-family
report contract that the next sweep maps straight to a file_ops patch
dispatch; an unconfident worker books nothing, its goal id lands in the
triage ledger, and the goal takes the interactive diagnose path; wiring —
all three controllers carry the needs_diagnose_batch rule + step.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.contract_swarm_actions import (
    _diagnose_batch_candidates,
    action_swarm_diagnose_batch,
)
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

_CONFIDENT = (
    '```json\n{"confident": true, "root_cause": "cave id list is malformed", '
    '"target_symbol": "rooms", "change_spec": "close the bracket on the cave '
    'id entry", "diagnosis_kind": "fix", "module_statement": "", '
    '"related_symbols": []}\n```'
)
_UNCONFIDENT = (
    '```json\n{"confident": false, "root_cause": "needs cross-file context", '
    '"target_symbol": "", "change_spec": "", "diagnosis_kind": "fix", '
    '"module_statement": "", "related_symbols": []}\n```'
)


def _failed_report(path: str) -> DirectiveReport:
    return DirectiveReport(
        flow="file_ops",
        status="failed",
        summary="gate failed",
        checks_failed=[f"syntax: {path}"],
        terminal_output="yaml.scanner.ScannerError: mapping values",
    )


def _mission(tmp_path: Path, files=("rooms.yaml", "items.yaml"), notes=None):
    goals = []
    for f in files:
        (tmp_path / f).write_text("broken: [\n")
        goals.append(
            GoalRecord(
                description=f"Create {f} with content: brief",
                type="structural",
                associated_files=[f],
                reports=[_failed_report(f)],
            )
        )
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path), structural_mode="batch"),
        architecture=ArchitectureState(
            run_command="python x.py",
            creation_order=list(files),
            modules=[ModuleSpec(file=f, responsibility="data") for f in files],
        ),
        goals=goals,
        notes=notes or [],
    )


def _sweep_si(mission, effects=None):
    return StepInput(
        context={"mission": mission},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="structural_sweep_next"),
        effects=effects or MockEffects(mission=mission),
    )


def _batch_si(mission, fx, tmp_path):
    return StepInput(
        context={"mission": mission},
        inputs={"working_directory": str(tmp_path)},
        params={},
        meta=FlowMeta(flow_name="diagnose_batch", step_id="fan_out_triage"),
        effects=fx,
    )


# ── candidate selection ───────────────────────────────────────────────


def test_candidates_select_gate_failed_goals(tmp_path):
    mission = _mission(tmp_path)
    cands = _diagnose_batch_candidates(mission, str(tmp_path))
    assert sorted(p for _, p, _ in cands) == ["items.yaml", "rooms.yaml"]


def test_candidates_exclude_diagnosed_and_triaged(tmp_path):
    mission = _mission(tmp_path)
    # first goal already carries a diagnosis report
    mission.goals[0].reports.append(
        DirectiveReport(flow="diagnose_issue", status="success", summary="d")
    )
    # second goal is in the triage ledger
    mission.notes.append(
        NoteRecord(
            content=mission.goals[1].id,
            category="codebase_observation",
            tags=["diagnose_batch"],
        )
    )
    assert _diagnose_batch_candidates(mission, str(tmp_path)) == []


# ── sweep gate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_dispatches_diagnose_batch_at_two_candidates(tmp_path):
    mission = _mission(tmp_path)
    out = await action_structural_sweep_next(_sweep_si(mission))
    assert out.result.get("needs_diagnose_batch") is True
    assert out.context_updates["dispatch_config"]["flow"] == "diagnose_batch"


@pytest.mark.asyncio
async def test_sweep_single_candidate_stays_serial(tmp_path):
    mission = _mission(tmp_path, files=("rooms.yaml",))
    out = await action_structural_sweep_next(_sweep_si(mission))
    assert out.result.get("needs_diagnose_batch") is None
    assert out.result.get("needs_fix") is True
    assert out.context_updates["dispatch_config"]["flow"] == "diagnose_issue"


# ── the burst action + downstream routing ─────────────────────────────


@pytest.mark.asyncio
async def test_confident_triage_books_contract_and_routes_to_patch(tmp_path):
    mission = _mission(tmp_path)
    fx = MockEffects(mission=mission, inference_responses=[_CONFIDENT, _CONFIDENT])
    out = await action_swarm_diagnose_batch(_batch_si(mission, fx, tmp_path))
    assert out.result["any_ok"] is True and out.result["n_targets"] == 2
    for goal in mission.goals:
        last = goal.reports[-1]
        assert last.flow == "diagnose_batch"
        assert last.recommended_flow == "file_ops"
        assert last.target_file in ("rooms.yaml", "items.yaml")
        assert "close the bracket" in last.change_spec
    # the next sweep maps the booked diagnosis straight to a file_ops patch
    nxt = await action_structural_sweep_next(_sweep_si(mission))
    assert nxt.result.get("needs_fix") is True
    cfg = nxt.context_updates["dispatch_config"]
    assert cfg["flow"] == "file_ops"
    assert "close the bracket" in cfg["change_spec"]


@pytest.mark.asyncio
async def test_unconfident_triage_defers_to_interactive_diagnose(tmp_path):
    mission = _mission(tmp_path)
    fx = MockEffects(mission=mission, inference_responses=[_UNCONFIDENT, _UNCONFIDENT])
    out = await action_swarm_diagnose_batch(_batch_si(mission, fx, tmp_path))
    assert out.result["any_ok"] is False
    for goal in mission.goals:
        assert goal.reports[-1].flow == "file_ops"  # nothing booked
    # ledger prevents re-triage; the sweep falls to serial diagnose
    assert _diagnose_batch_candidates(mission, str(tmp_path)) == []
    nxt = await action_structural_sweep_next(_sweep_si(mission))
    assert nxt.result.get("needs_diagnose_batch") is None
    assert nxt.result.get("needs_fix") is True
    assert nxt.context_updates["dispatch_config"]["flow"] == "diagnose_issue"


# ── wiring ────────────────────────────────────────────────────────────


def test_all_three_controllers_carry_diagnose_batch_dispatch():
    compiled = json.loads(
        (Path(__file__).parent.parent / "flows" / "compiled.json").read_text()
    )
    assert "diagnose_batch" in compiled
    for ctrl in (
        "mission_control",
        "mission_control_contracted",
        "mission_control_swarm",
    ):
        steps = compiled[ctrl]["steps"]
        assert steps["dispatch_diagnose_batch"]["tail_call"]["flow"] == (
            "diagnose_batch"
        )
        rules = steps["structural_sweep_next"]["resolver"]["rules"]
        assert any("needs_diagnose_batch" in r.get("condition", "") for r in rules)
