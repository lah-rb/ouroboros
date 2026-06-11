"""Write-time smoke-boot guard in file_ops validation.

A syntactically valid edit can still break program startup; before this
guard the breakage surfaced N cycles later as gate findings (observed
live: one bad edit cascaded into 9 reopened goals). Once the program is
KNOWN bootable (environment_verified), every file_ops write re-runs the
architecture smoke command, and a failure routes into the same-dispatch
self-correct loop. During the structural phase (env not yet verified)
the check is silent — the program legitimately can't boot mid-build.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.pipeline_actions import action_run_validation_checks_from_env
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import ArchitectureState, MissionConfig, MissionState

_SMOKE = "echo quit | python main.py"


def _mission(env_verified=True, smoke=_SMOKE):
    return MissionState(
        objective="t",
        status="active",
        environment_verified=env_verified,
        config=MissionConfig(working_directory="/tmp/x"),
        architecture=ArchitectureState(
            run_command="python main.py", smoke_command=smoke
        ),
    )


def _si(effects) -> StepInput:
    return StepInput(
        context={
            "validation_commands": {"syntax": ["python", "-m", "py_compile", "{file}"]}
        },
        params={"target": "main.py"},
        meta=FlowMeta(flow_name="file_ops", step_id="run_checks"),
        effects=effects,
    )


_OK = CommandResult(return_code=0, stdout="", stderr="", command="x")
_BOOM = CommandResult(
    return_code=1, stdout="", stderr="Traceback: AttributeError", command="x"
)


@pytest.mark.asyncio
async def test_smoke_failure_flags_and_blocks_all_passing():
    fx = MockEffects(
        mission=_mission(),
        commands={"python -m py_compile main.py": _OK, "/bin/sh": _BOOM},
    )
    out = await action_run_validation_checks_from_env(_si(fx))
    assert out.result["smoke_failed"] is True
    assert out.result["all_passing"] is False
    assert any(
        r["tier"] == "smoke" and not r["passed"]
        for r in out.context_updates["validation_results"]
    )
    assert "no longer starts" in out.context_updates["validation_output"]


@pytest.mark.asyncio
async def test_smoke_pass_keeps_all_passing():
    fx = MockEffects(
        mission=_mission(),
        commands={"python -m py_compile main.py": _OK, "/bin/sh": _OK},
    )
    out = await action_run_validation_checks_from_env(_si(fx))
    assert out.result["smoke_failed"] is False
    assert out.result["all_passing"] is True


@pytest.mark.asyncio
async def test_smoke_skipped_during_structural_phase():
    fx = MockEffects(
        mission=_mission(env_verified=False),
        commands={"python -m py_compile main.py": _OK, "/bin/sh": _BOOM},
    )
    out = await action_run_validation_checks_from_env(_si(fx))
    assert out.result["smoke_failed"] is False
    # The smoke command was never run.
    sh_calls = [
        c for c in fx.calls_to("run_command") if c.args["command"][0] == "/bin/sh"
    ]
    assert sh_calls == []


@pytest.mark.asyncio
async def test_smoke_skipped_when_syntax_already_failed():
    fx = MockEffects(
        mission=_mission(),
        commands={"python -m py_compile main.py": _BOOM, "/bin/sh": _OK},
    )
    out = await action_run_validation_checks_from_env(_si(fx))
    assert out.result["syntax_failed"] is True
    assert out.result["smoke_failed"] is False


def test_compiled_routing_smoke_to_retry():
    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    rules = compiled["file_ops"]["steps"]["run_checks"]["resolver"]["rules"]
    transitions = {r["condition"]: r["transition"] for r in rules}
    assert transitions["result.smoke_failed == true"] == "check_retry"
