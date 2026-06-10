"""Transient-file flush: behavioral test isolation.

The gemma poison class: the program under test persisted game_over=true
to state.json on quit, auto-loaded it at the next launch, and every later
test session saw "game has already ended" — 25 fix rounds against a
symptom no code change could clear. The architecture now declares the
files the program creates at runtime (transient_files, names or globs),
and interact / quality-gate UX sessions deterministically flush them.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.interactive_actions import action_flush_transient_files
from agent.actions.mission_actions import action_parse_and_store_architecture
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    DataShapeContract,
    MissionConfig,
    MissionState,
    ModuleSpec,
)

_RM_OK = CommandResult(return_code=0, stdout="", stderr="", command="rm")


def _mission(transient=None) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        architecture=ArchitectureState(
            modules=[ModuleSpec(file="engine.py")],
            data_shapes=[
                DataShapeContract(
                    file="world.yaml", consumed_by="loader.py", structure="rooms"
                )
            ],
            transient_files=transient if transient is not None else [],
        ),
    )


def _si(effects) -> StepInput:
    return StepInput(
        context={},
        params={},
        meta=FlowMeta(flow_name="interact", step_id="flush_transient_success"),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_flush_deletes_matching_files_only():
    effects = MockEffects(
        files={
            "state.json": '{"game_over": true}',
            "save1.save.json": "{}",
            "world.yaml": "rooms: []",  # declared input data — protected
            "engine.py": "code",  # architecture module — protected
            "notes.txt": "n",  # unmatched — untouched
        },
        commands={"rm": _RM_OK},
        mission=_mission(transient=["state.json", "*.save.json"]),
    )
    out = await action_flush_transient_files(_si(effects))
    assert out.result["flushed"] == 2
    rm_targets = [c.args["command"][2] for c in effects.calls_to("run_command")]
    assert sorted(rm_targets) == ["save1.save.json", "state.json"]


@pytest.mark.asyncio
async def test_flush_protects_declared_files_even_when_glob_matches():
    effects = MockEffects(
        files={"world.yaml": "rooms: []", "cache.yaml": "x"},
        commands={"rm": _RM_OK},
        mission=_mission(transient=["*.yaml"]),  # over-broad glob
    )
    out = await action_flush_transient_files(_si(effects))
    rm_targets = [c.args["command"][2] for c in effects.calls_to("run_command")]
    assert rm_targets == ["cache.yaml"]  # world.yaml is data_shapes input
    assert out.result["flushed"] == 1


@pytest.mark.asyncio
async def test_flush_rejects_unsafe_patterns():
    effects = MockEffects(
        files={"state.json": "{}"},
        commands={"rm": _RM_OK},
        mission=_mission(transient=["/etc/passwd", "../outside", "~/x"]),
    )
    out = await action_flush_transient_files(_si(effects))
    assert out.result["flushed"] == 0
    assert effects.call_count("run_command") == 0


@pytest.mark.asyncio
async def test_flush_noop_without_declaration():
    effects = MockEffects(
        files={"state.json": "{}"},
        commands={"rm": _RM_OK},
        mission=_mission(transient=[]),
    )
    out = await action_flush_transient_files(_si(effects))
    assert out.result["flushed"] == 0
    assert "nothing to flush" in out.observations


@pytest.mark.asyncio
async def test_parse_architecture_stores_transient_files():
    design = json.dumps(
        {
            "execution": {"run_command": "x", "import_scheme": "flat"},
            "modules": [{"file": "main.py", "defines": [], "imports_from": {}}],
            "interfaces": [],
            "data_shapes": [],
            "state_shapes": [],
            "transient_files": ["savegame.json", "*.save.json"],
            "creation_order": ["main.py"],
            "notes": "",
        }
    )
    m = MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
    )
    si = StepInput(
        context={"mission": m, "inference_response": f"```json\n{design}\n```"},
        params={},
        meta=FlowMeta(flow_name="design_and_plan", step_id="parse"),
        effects=MockEffects(),
    )
    out = await action_parse_and_store_architecture(si)
    assert out.result["architecture_parsed"] is True
    assert m.architecture.transient_files == ["savegame.json", "*.save.json"]
