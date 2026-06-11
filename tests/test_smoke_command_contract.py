"""run_command / smoke_command contract split.

The architecture's run_command was overloaded: design_and_plan emitted
the piped startup-check form (`echo quit | python main.py`) because the
startup check needed it — but every consumer needing launch-and-wait
semantics (verification probes, testers) then drove a program that had
already quit, with repro lines answering to the bare shell. The contract
now declares both: run_command is the plain interactive launch,
smoke_command the self-terminating check. effective_smoke_command falls
back to run_command for pre-split architectures (where run_command WAS
the smoke form).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.mission_actions import action_parse_and_store_architecture
from agent.actions.verification_actions import action_prepare_finding_verification
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import ArchitectureState, MissionConfig, MissionState

_COMPILED = json.loads(
    (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
)


def _mission() -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
    )


def _design_json(execution: dict) -> str:
    return json.dumps(
        {
            "execution": execution,
            "modules": [
                {
                    "file": "main.py",
                    "responsibility": "entry",
                    "defines": [],
                    "imports_from": {},
                }
            ],
            "interfaces": [],
            "data_shapes": [],
            "creation_order": ["main.py"],
        }
    )


@pytest.mark.asyncio
async def test_parse_stores_both_commands():
    mission = _mission()
    si = StepInput(
        context={
            "mission": mission,
            "inference_response": _design_json(
                {
                    "run_command": "python main.py",
                    "smoke_command": "echo quit | python main.py",
                    "import_scheme": "flat",
                }
            ),
        },
        params={},
        meta=FlowMeta(flow_name="design_and_plan", step_id="parse"),
        effects=MockEffects(),
    )
    await action_parse_and_store_architecture(si)
    arch = mission.architecture
    assert arch.run_command == "python main.py"
    assert arch.smoke_command == "echo quit | python main.py"
    assert arch.effective_smoke_command == "echo quit | python main.py"


def test_effective_smoke_falls_back_for_pre_split_architectures():
    # Pre-split mission.json: run_command holds the piped form, no smoke key.
    arch = ArchitectureState(run_command='printf "quit\\n" | python main.py')
    assert arch.effective_smoke_command == 'printf "quit\\n" | python main.py'


# ── probe launch preference ───────────────────────────────────────────


def _prepare_si(run_command, smoke_command="", ux_launch=""):
    return StepInput(
        context={
            "quality_results": {
                "all_passing": False,
                "fix_tasks": [
                    {
                        "issue": "x",
                        "description": "x",
                        "class": "functional",
                        "repro": ["go north"],
                    }
                ],
            },
            "terminal_output": "ux",
        },
        params={
            "run_command": run_command,
            "smoke_command": smoke_command,
            "ux_launch_command": ux_launch,
            "no_repro_policy": "permissive",
        },
        meta=FlowMeta(flow_name="quality_gate", step_id="prepare"),
        effects=MockEffects(),
    )


@pytest.mark.asyncio
async def test_probes_use_run_command_directly_under_contract_split():
    # smoke != run -> run_command is the declared interactive launch.
    out = await action_prepare_finding_verification(
        _prepare_si(
            run_command="python main.py",
            smoke_command="echo quit | python main.py",
            ux_launch="python main.py --debug",  # ignored — contract wins
        )
    )
    assert out.context_updates["probe_launch"] == "python main.py"


@pytest.mark.asyncio
async def test_pre_split_architecture_prefers_captured_ux_launch():
    # smoke == run (effective fallback) -> run may be the piped form.
    piped = 'printf "quit\\n" | python main.py'
    out = await action_prepare_finding_verification(
        _prepare_si(run_command=piped, smoke_command=piped, ux_launch="python main.py")
    )
    assert out.context_updates["probe_launch"] == "python main.py"


# ── compiled wiring ───────────────────────────────────────────────────


def test_startup_check_uses_smoke_command():
    step = _COMPILED["quality_gate"]["steps"]["run_startup_check"]
    assert (
        step["input_map"]["commands"][0]["$ref"] == "input.architecture_smoke_command"
    )


def test_mission_control_passes_effective_smoke_command():
    step = _COMPILED["mission_control"]["steps"]["dispatch_quality_gate"]
    assert (
        step["input_map"]["architecture_smoke_command"]["$ref"]
        == "context.mission.architecture.effective_smoke_command"
    )


def test_verification_steps_receive_smoke_command():
    steps = _COMPILED["quality_gate"]["steps"]
    for name in (
        "prepare_finding_verification",
        "record_and_advance",
        "record_probe_error",
    ):
        assert (
            steps[name]["params"]["smoke_command"]["$ref"]
            == "input.architecture_smoke_command"
        ), name
