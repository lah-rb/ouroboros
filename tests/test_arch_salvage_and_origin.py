"""Arch-parse coercion/salvage + origin-stamped phase routing + the
no-architecture sweep guard — the three fixes from the OLMo zombie
mission (2026-07-23): a well-formed 6k blueprint was discarded over three
scalar-vs-list values, the brownfield inference then skipped re-design,
and an unaddressable structural goal spun check_phase↔sweep to the 51x
guard.
"""

from __future__ import annotations

import pytest

from agent.actions.mission_actions import (
    action_parse_and_store_architecture,
    action_structural_sweep_next,
)
from agent.effects.mock import MockEffects
from agent.flow_sets import FLOW_SETS, evaluate_phases
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
)


def _mission(tmp_path, origin="", goals=None):
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path), origin=origin),
        goals=goals or [],
    )


def _si(mission, extra=None):
    ctx = {"mission": mission}
    ctx.update(extra or {})
    return StepInput(
        context=ctx,
        effects=MockEffects(mission=mission),
        meta=FlowMeta(flow_name="mission_control", step_id="t"),
    )


# ── coercion: the exact OLMo shape miss ───────────────────────────────


def test_imports_from_scalar_values_coerce_to_lists():
    m = ModuleSpec(
        file="engine.py",
        imports_from={"game.items": "Item", "game.npcs": ["NPC"], "x": None},
    )
    assert m.imports_from == {"game.items": ["Item"], "game.npcs": ["NPC"], "x": []}


# ── salvage: one bad module must not nuke the blueprint ───────────────

ARCH_JSON = """```json
{"execution": {"run_command": "python main.py"},
 "modules": [
   {"file": "models.py", "responsibility": "data", "imports_from": {"yaml": "safe_load"}},
   {"file": {"nested": "garbage"}, "responsibility": 3, "defines": {"a": 1}, "imports_from": 7}
 ],
 "creation_order": ["models.py"]}
```"""


@pytest.mark.asyncio
async def test_parse_arch_salvages_valid_modules(tmp_path):
    mission = _mission(tmp_path, origin="greenfield")
    out = await action_parse_and_store_architecture(
        _si(mission, {"inference_response": ARCH_JSON})
    )
    assert out.result["architecture_parsed"] is True
    assert [m.file for m in mission.architecture.modules] == ["models.py"]
    # The scalar import value coerced instead of failing.
    assert mission.architecture.modules[0].imports_from == {"yaml": ["safe_load"]}


@pytest.mark.asyncio
async def test_parse_arch_all_modules_invalid_is_parse_failure(tmp_path):
    bad = '```json\n{"execution": {}, "modules": ["garbage", 42]}\n```'
    mission = _mission(tmp_path, origin="greenfield")
    out = await action_parse_and_store_architecture(
        _si(mission, {"inference_response": bad})
    )
    assert out.result["architecture_parsed"] is False


# ── origin stamp: greenfield never infers brownfield ──────────────────


def _code_core_phases():
    return FLOW_SETS["code_core"].phases


def test_greenfield_goals_without_architecture_replans(tmp_path):
    goals = [GoalRecord(description="g", type="functional", status="incomplete")]
    mission = _mission(tmp_path, origin="greenfield", goals=goals)
    phase, obs = evaluate_phases(mission, _code_core_phases())
    assert phase == "plan"
    assert "design incomplete" in obs


def test_legacy_goals_without_architecture_keep_brownfield_skip(tmp_path):
    goals = [GoalRecord(description="g", type="functional", status="incomplete")]
    mission = _mission(tmp_path, origin="", goals=goals)
    phase, _ = evaluate_phases(mission, _code_core_phases())
    assert phase != "plan"  # inference preserved for legacy/ingest missions


# ── sweep suspenders: unaddressable structural goals escalate ─────────


@pytest.mark.asyncio
async def test_sweep_without_arch_but_structural_goals_needs_replan(tmp_path):
    goals = [
        GoalRecord(
            description="s",
            type="structural",
            associated_files=["main.py"],
            status="incomplete",
        )
    ]
    mission = _mission(tmp_path, origin="greenfield", goals=goals)
    out = await action_structural_sweep_next(_si(mission))
    assert out.result.get("needs_replan") is True
    assert out.result.get("sweep_complete") is False


@pytest.mark.asyncio
async def test_sweep_without_arch_and_no_structural_goals_completes(tmp_path):
    mission = _mission(tmp_path, origin="greenfield", goals=[])
    out = await action_structural_sweep_next(_si(mission))
    assert out.result.get("sweep_complete") is True


# ── W5 (2026-08-02): contract-list salvage — the olmo-think arm18 crash ──
#
# The modules loop got per-element salvage in 2026-06; the contract lists
# never did, so `"interfaces": ["main calls loader.load"]` raised
# `'str' object has no attribute 'get'` inside the blanket try and the
# ENTIRE blueprint (modules included) was discarded.

CONTRACT_SALVAGE_JSON = """```json
{
  "execution": {"import_scheme": "flat", "run_command": "python main.py"},
  "modules": [
    {"file": "main.py", "responsibility": "entry"},
    {"file": "engine.py", "responsibility": "loop"}
  ],
  "interfaces": [
    "main calls loader.load",
    {"caller": "main.py", "callee": "engine.py", "symbol": "run"}
  ],
  "data_shapes": ["rooms are dicts"],
  "state_shapes": [
    {"name": "GameState.inventory", "owner": "state.py"},
    "save file is JSON"
  ],
  "creation_order": "engine.py",
  "transient_files": "save.json"
}
```"""


@pytest.mark.asyncio
async def test_str_elements_in_contract_lists_salvage_not_discard(tmp_path):
    mission = _mission(tmp_path)
    out = await action_parse_and_store_architecture(
        _si(mission, {"inference_response": CONTRACT_SALVAGE_JSON})
    )
    assert out.result.get("architecture_parsed") is True
    arch = mission.architecture
    # The valid elements survived; the str elements dropped alone.
    assert len(arch.modules) == 2
    assert len(arch.interfaces) == 1 and arch.interfaces[0].symbol == "run"
    assert len(arch.data_shapes) == 0
    assert len(arch.state_shapes) == 1
    assert arch.state_shapes[0].owner == "state.py"
    # Scalar containers coerced instead of iterated into characters.
    assert arch.creation_order == ["engine.py"]
    assert arch.transient_files == ["save.json"]


@pytest.mark.asyncio
async def test_non_list_contract_containers_treated_empty(tmp_path):
    bad = CONTRACT_SALVAGE_JSON.replace(
        '"interfaces": [\n    "main calls loader.load",\n    {"caller": "main.py", "callee": "engine.py", "symbol": "run"}\n  ]',
        '"interfaces": "main -> engine"',
    )
    mission = _mission(tmp_path)
    out = await action_parse_and_store_architecture(
        _si(mission, {"inference_response": bad})
    )
    assert out.result.get("architecture_parsed") is True
    assert mission.architecture.interfaces == []
    assert len(mission.architecture.modules) == 2
