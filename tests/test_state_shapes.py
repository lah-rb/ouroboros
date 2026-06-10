"""State contracts: architecture-level canonical representations.

Data shapes cover designed INPUT files; state shapes cover the emergent
contracts that previously existed only implicitly across symbols — the
in-memory state model and persisted-state schemas. These tests pin the
full pipeline: design output parsing → ArchitectureState → file_context /
quality_overview projections → renderers → the diagnose-session seed.
The motivating failure is the save/load oscillation class (Room.items
flipped objects↔ids in opposite fixes because no canonical answer was
written down anywhere).
"""

from __future__ import annotations

import json

import pytest

from agent.actions.diagnosis_session_actions import action_start_diagnosis_session
from agent.actions.mission_actions import action_parse_and_store_architecture
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    MissionConfig,
    MissionState,
    StateShapeContract,
)
from agent.projections import project_file_context, project_quality_overview
from agent.renderers import render_data_contracts, render_file_context

_DESIGN_JSON = json.dumps(
    {
        "execution": {
            "run_command": "echo quit | python main.py",
            "import_scheme": "flat",
            "init_files": False,
            "working_directory": "project root",
        },
        "modules": [
            {
                "file": "models.py",
                "responsibility": "Data classes",
                "defines": ["Room"],
                "imports_from": {},
            },
            {
                "file": "engine.py",
                "responsibility": "Game loop",
                "defines": ["GameEngine"],
                "imports_from": {"models": ["Room"]},
            },
        ],
        "interfaces": [],
        "data_shapes": [
            {
                "file": "world.yaml",
                "consumed_by": "loader.py",
                "structure": "rooms: list of {id, name}",
            }
        ],
        "state_shapes": [
            {
                "name": "Room.items",
                "owner": "models.py",
                "consumed_by": "engine.py, loader.py",
                "structure": "list of item_id strings — never Item objects",
            },
            {
                "name": "save file JSON",
                "owner": "engine.py",
                "consumed_by": "engine.py",
                "structure": "{current_room_id: str, inventory: [item_id]}",
            },
        ],
        "creation_order": ["models.py", "engine.py"],
        "notes": "n",
    }
)


def _mission(architecture=None) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/nonexistent-ws"),
        architecture=architecture,
    )


def _arch_with_state_shapes() -> ArchitectureState:
    return ArchitectureState(
        modules=[],
        state_shapes=[
            StateShapeContract(
                name="Room.items",
                owner="models.py",
                consumed_by="engine.py, loader.py",
                structure="list of item_id strings — never Item objects",
            )
        ],
    )


# ── model coercion / alias handling ───────────────────────────────────


def test_from_llm_dict_handles_field_name_drift():
    ss = StateShapeContract.from_llm_dict(
        {
            "state": "GameState.inventory",
            "defined_by": "models.py",
            "consumers": ["engine.py", "saver.py"],
            "shape": {"inventory": "list of item_id"},
        }
    )
    assert ss.name == "GameState.inventory"
    assert ss.owner == "models.py"
    assert ss.consumed_by == "engine.py, saver.py"
    assert "item_id" in ss.structure  # dict coerced to JSON string


# ── design output parsing ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_architecture_stores_state_shapes_and_notes_them():
    m = _mission()
    si = StepInput(
        context={"mission": m, "inference_response": f"```json\n{_DESIGN_JSON}\n```"},
        params={},
        meta=FlowMeta(flow_name="design_and_plan", step_id="parse"),
        effects=MockEffects(),
    )
    out = await action_parse_and_store_architecture(si)
    assert out.result["architecture_parsed"] is True
    assert len(m.architecture.state_shapes) == 2
    assert m.architecture.state_shapes[0].name == "Room.items"
    assert "never Item objects" in m.architecture.state_shapes[0].structure
    # The architecture summary note carries the contracts forward.
    assert any("State contracts" in n.content for n in m.notes)


# ── projections ───────────────────────────────────────────────────────


def test_file_context_projects_all_state_shapes():
    m = _mission(_arch_with_state_shapes())
    ctx = project_file_context(m, {"target_file": "engine.py"})
    assert ctx["state_shapes"] == [
        {
            "name": "Room.items",
            "owner": "models.py",
            "consumed_by": "engine.py, loader.py",
            "structure": "list of item_id strings — never Item objects",
        }
    ]
    # Global contract: present even for files not listed as consumers.
    ctx2 = project_file_context(m, {"target_file": "parser.py"})
    assert len(ctx2["state_shapes"]) == 1


def test_quality_overview_projects_state_shapes():
    m = _mission(_arch_with_state_shapes())
    overview = project_quality_overview(m, {})
    assert overview["state_shapes"][0]["name"] == "Room.items"


def test_empty_architecture_yields_empty_state_shapes():
    m = _mission(None)
    ctx = project_file_context(m, {"target_file": "engine.py"})
    assert ctx["state_shapes"] == []


# ── renderers ─────────────────────────────────────────────────────────


def _projected_shape() -> dict:
    return {
        "name": "Room.items",
        "owner": "models.py",
        "consumed_by": "engine.py, loader.py",
        "structure": "list of item_id strings — never Item objects",
    }


def test_data_contracts_block_includes_state_contracts():
    block = render_data_contracts(
        {"source": {"data_shapes": [], "state_shapes": [_projected_shape()]}},
        namespaces={},
    )
    # Fires even with no data-file shapes — state contracts alone justify it.
    assert "DATA CONTRACTS (MANDATORY)" in block
    assert "Room.items" in block
    assert "never Item objects" in block
    assert "Canonical form" in block


def test_data_contracts_block_empty_without_any_shapes():
    block = render_data_contracts(
        {"source": {"data_shapes": [], "state_shapes": []}}, namespaces={}
    )
    assert block == ""


def test_render_file_context_lists_state_contracts():
    text = render_file_context(
        {"source": {"target_file": "engine.py", "state_shapes": [_projected_shape()]}},
        namespaces={},
    )
    assert "State contracts" in text
    assert "Room.items" in text


# ── diagnose-session seed ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_diagnosis_seed_includes_state_contracts():
    effects = MockEffects()
    si = StepInput(
        context={
            "flow_directive": "Diagnose why loading a save crashes",
            "error_output": "TypeError: string indices must be integers",
            "file_context": {
                "state_shapes": [_projected_shape()],
                "data_shapes": [],
            },
        },
        params={},
        meta=FlowMeta(flow_name="diagnose_issue", step_id="start_session"),
        effects=effects,
    )
    out = await action_start_diagnosis_session(si)
    assert out.result.get("session_started") is True
    seed = out.context_updates.get("session_injections", [""])[0]
    assert "## State contracts" in seed
    assert "Room.items" in seed
    assert "deviating" in seed  # the diagnose-against-the-contract framing
