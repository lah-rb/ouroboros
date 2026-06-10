"""Tests for the cross-cutting data-file structural check.

Structured data files (.yaml/.yml/.json/.toml) that the program loads at runtime
were previously skipped by the structural gate, so a malformed one passed
validation and only broke at runtime (often mis-diagnosed). These lock in the
parse-validity gate — the data analog of the Python syntax check:

  - valid data file → all_passing, not syntax_failed,
  - malformed data file → syntax_failed (blocks the structural goal like a
    syntax error), with the standard `syntax: <file>` check shape,
  - lookup_validation_env routes data extensions to the check (not skip), while
    code/markdown extensions behave as before.
"""

from __future__ import annotations

import pytest

from agent.actions.pipeline_actions import (
    action_check_data_file,
    action_collect_env_field,
    action_lookup_validation_env,
    action_persist_validation_env,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput


def _si(effects, target, **files) -> StepInput:
    return StepInput(
        context={},
        params={"target": target},
        meta=FlowMeta(flow_name="file_ops", step_id="run_data_check", attempt=1),
        effects=effects,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fname,content",
    [
        ("world.yaml", "rooms:\n  hall:\n    name: Hall\n    exits: {north: cave}\n"),
        ("save.json", '{"current_room": "hall", "inventory": ["key"]}'),
        ("pyproject.toml", 'name = "demo"\n[tool.x]\nval = 1\n'),
    ],
)
async def test_valid_data_files_pass(fname, content):
    effects = MockEffects(files={fname: content})
    out = await action_check_data_file(_si(effects, fname))
    assert out.result["all_passing"] is True
    assert out.result["syntax_failed"] is False
    assert out.context_updates["validation_results"][0]["passed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fname,content",
    [
        ("world.yaml", "rooms:\n  hall:\n    items: [a, b\n"),  # unclosed flow seq
        ("save.json", '{"current_room": "hall",'),  # unclosed object
        ("pyproject.toml", "name = \nbroken"),  # missing value
    ],
)
async def test_malformed_data_files_block(fname, content):
    effects = MockEffects(files={fname: content})
    out = await action_check_data_file(_si(effects, fname))
    assert out.result["syntax_failed"] is True, f"{fname} should fail parse"
    assert out.result["all_passing"] is False
    check = out.context_updates["validation_results"][0]
    assert check["name"] == f"syntax: {fname}"
    assert check["required"] is True and check["passed"] is False


@pytest.mark.asyncio
async def test_empty_data_file_passes():
    """An empty file parses as valid for all three formats — nothing to flag."""
    effects = MockEffects(files={"world.yaml": ""})
    out = await action_check_data_file(_si(effects, "world.yaml"))
    assert out.result["all_passing"] is True


@pytest.mark.asyncio
async def test_lookup_routes_data_files_to_check_not_skip():
    """Data extensions route to the parse check; code/markdown behave as before."""
    eff = MockEffects()

    def lk(target):
        return StepInput(
            context={},
            params={"target": target},
            meta=FlowMeta(flow_name="file_ops", step_id="lookup_env", attempt=1),
            effects=eff,
        )

    for data in ("world.yaml", "a.yml", "save.json", "pyproject.toml"):
        out = await action_lookup_validation_env(lk(data))
        assert out.result.get("is_data_file") is True, f"{data} should be data_file"
        assert not out.result.get("skip_validation")

    md = await action_lookup_validation_env(lk("README.md"))
    assert md.result.get("skip_validation") is True  # still skipped

    py = await action_lookup_validation_env(lk("engine.py"))
    assert not py.result.get("is_data_file")  # code → normal env path
    assert not py.result.get("skip_validation")


@pytest.mark.asyncio
async def test_env_config_round_trips_through_effects_working_dir_scoped():
    """Regression: env.json must round-trip through the effects layer so it lands
    in the mission working_directory — NOT via a bare relative Path that resolves
    against the agent process cwd (which leaked a stray .agent/ into the repo and
    shared one env.json across every mission). Locks persist→lookup→collect to the
    effects-scoped ``.agent/env.json``."""
    effects = MockEffects(files={})
    cfg = {
        "interactive_prompt": "> ",
        "py": {
            "install_command": ["pip", "install", "-e", "."],
            "syntax": ["python", "-c", "import py_compile"],
        },
    }

    def si(ctx, params, step):
        return StepInput(
            context=ctx,
            params=params,
            meta=FlowMeta(flow_name="set_env", step_id=step, attempt=1),
            effects=effects,
        )

    # persist → writes through effects to the working-dir-scoped key, not cwd.
    out = await action_persist_validation_env(
        si({"inference_response": cfg}, {}, "persist_env")
    )
    assert out.result.get("env_saved") is True
    assert ".agent/env.json" in effects.written_files  # effects-scoped, not raw cwd
    import json as _json

    assert _json.loads(effects.written_files[".agent/env.json"])["py"][
        "install_command"
    ] == ["pip", "install", "-e", "."]

    # lookup reads it back via effects (known ext → env_found).
    lout = await action_lookup_validation_env(
        si({}, {"target": "engine.py"}, "lookup_env")
    )
    assert lout.result.get("env_found") is True

    # collect_env_field gathers install_command via effects (list → shell string).
    cout = await action_collect_env_field(
        si(
            {},
            {"field": "install_command", "output_key": "install_commands"},
            "collect",
        )
    )
    assert cout.result.get("commands_found") is True
    # Python installs are routed through uv (clean per-project venv); editable
    # `-e .` becomes deps-only. See test_project_env_pinning for the full contract.
    assert cout.context_updates["install_commands"] == [
        "uv venv --allow-existing",
        "uv pip install -r pyproject.toml",
    ]
