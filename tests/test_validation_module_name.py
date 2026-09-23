"""The import check's {module} must be the workspace-relative dotted name.

A diagnosis can name its fix target by ABSOLUTE path. The validator derived
the module from the raw path, so /private/tmp/tier/<arm>/engine.py became
`private.tmp.tier.<arm>.engine` and every import check failed — on
2026-09-22 a correct `take` handler was booked as a failed fix while the
follow-up diagnosis correctly blamed the validator.
"""

from __future__ import annotations

import os

import pytest

from agent.actions.pipeline_actions import _module_name_for


def test_relative_paths_keep_their_dotted_name():
    assert _module_name_for("engine.py", "/w") == "engine"
    assert _module_name_for("src/pkg/mod.py", "/w") == "src.pkg.mod"


def test_an_absolute_target_inside_the_workspace_is_made_relative(tmp_path):
    ws = tmp_path / "arm"
    (ws / "pkg").mkdir(parents=True)
    assert _module_name_for(str(ws / "engine.py"), str(ws)) == "engine"
    assert _module_name_for(str(ws / "pkg" / "a.py"), str(ws)) == "pkg.a"


def test_symlinked_workspace_roots_compare_by_real_path(tmp_path):
    # macOS: /tmp is a symlink to /private/tmp — the diagnosis saw one
    # spelling, the effects the other.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    assert _module_name_for(str(real / "engine.py"), str(link)) == "engine"


def test_non_python_files_have_no_module():
    assert _module_name_for("README.md", "/w") == ""
    # Only the suffix is stripped — the old .replace(".py", "") also ate
    # ".py" out of the middle of a name.
    assert _module_name_for("my.pyramid.py", "/w") == "my.pyramid"


@pytest.mark.asyncio
async def test_the_import_check_receives_the_relative_module(tmp_path):
    from types import SimpleNamespace

    from agent.actions.pipeline_actions import action_run_validation_checks_from_env
    from agent.effects.protocol import CommandResult
    from agent.models import FlowMeta, StepInput

    ran: list[list[str]] = []

    async def run_command(command, working_dir=None, timeout=30):
        ran.append(list(command))
        return CommandResult(return_code=0, stdout="", stderr="", command=command[0])

    ws = tmp_path / "qwen3.8-arm"
    ws.mkdir()
    effects = SimpleNamespace(working_directory=str(ws), run_command=run_command)
    si = StepInput(
        context={
            "validation_commands": {"import": ["python", "-c", "import {module}"]}
        },
        params={"target": str(ws / "engine.py")},
        meta=FlowMeta(flow_name="patch", step_id="x"),
        effects=effects,
    )
    out = await action_run_validation_checks_from_env(si)
    assert ran == [["python", "-c", "import engine"]]
    assert out.result["all_passing"] is True
