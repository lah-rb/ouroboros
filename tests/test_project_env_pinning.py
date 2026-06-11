"""Per-project interpreter pinning via a uv venv.

The framework runs a project's commands (validation, install, and the interactive
program) under whatever bare ``python``/``pip`` happen to sit first on the ambient
PATH. Those resolved to DIFFERENT interpreters across the two execution paths —
``run_command`` inherited the agent's env while the interactive PTY spawned a
fresh shell — so dependency installs landed in one interpreter while the program
ran under another (and a stray rustpython shim shadowed ``pip`` entirely). Every
interactive goal then failed regardless of code quality.

These lock in the fix: a per-project ``.venv`` (created + installed via uv) plus
activation injected into BOTH execution paths, so install and run share one
interpreter.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from agent.actions.pipeline_actions import (
    _uvize_install_commands,
    action_collect_env_field,
)
from agent.effects.local import LocalEffects
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

# The venv must pin the framework's own interpreter — unpinned uv venv
# discovered the macOS system Python 3.9.6 live, where 3.10+ annotation
# syntax passes every parse gate and explodes at import.
_PINNED_VENV = (
    f"uv venv --allow-existing --python "
    f"{sys.version_info.major}.{sys.version_info.minor}"
)

# ── venv activation (LocalEffects) ─────────────────────────────────────


def test_venv_overrides_empty_without_venv(tmp_path):
    """No project venv → no overrides; the inherited environment is unchanged."""
    eff = LocalEffects(working_directory=str(tmp_path))
    assert eff.venv_env_overrides() == {}


def test_venv_overrides_activate_when_venv_present(tmp_path):
    """A ``<wd>/.venv`` with a python binary → VIRTUAL_ENV + PATH-prepend, so bare
    ``python`` resolves to the per-project interpreter."""
    bindir = tmp_path / ".venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").write_text("#!/bin/sh\n")

    eff = LocalEffects(working_directory=str(tmp_path))
    ov = eff.venv_env_overrides()

    expected_venv = os.path.join(os.path.realpath(str(tmp_path)), ".venv")
    assert ov["VIRTUAL_ENV"] == expected_venv
    assert ov["PATH"].split(os.pathsep)[0] == os.path.join(expected_venv, "bin")
    # The command env reflects activation and is authoritative over PYTHONHOME.
    cenv = eff._command_env()
    assert cenv["VIRTUAL_ENV"] == expected_venv
    assert "PYTHONHOME" not in cenv


def test_command_env_unchanged_without_venv(tmp_path):
    eff = LocalEffects(working_directory=str(tmp_path))
    cenv = eff._command_env()
    assert "VIRTUAL_ENV" not in cenv or cenv.get("VIRTUAL_ENV") != str(
        tmp_path / ".venv"
    )
    assert cenv["PATH"] == os.environ["PATH"]


# ── uv-ized install commands ───────────────────────────────────────────


def test_uvize_rewrites_editable_install_to_deps_only():
    # `pip install -e .` builds the (flat-layout) package and fails; we install
    # the declared deps from pyproject instead, which is what the program needs.
    out = _uvize_install_commands(["pip install -e ."], {"py": {}})
    assert out == [_PINNED_VENV, "uv pip install -r pyproject.toml"]


def test_uvize_rewrites_uv_pip_editable_too():
    # Regression: the LLM emits `uv pip install -e .` (already uv-prefixed). The
    # editable build still fails on flat-layout, so it must ALSO be rewritten —
    # the prefix-only guard let this through and broke a live run.
    out = _uvize_install_commands(["uv pip install -e ."], {"py": {}})
    assert out == [_PINNED_VENV, "uv pip install -r pyproject.toml"]
    # --editable long form too.
    out2 = _uvize_install_commands(["uv pip install --editable ."], {"py": {}})
    assert out2 == [_PINNED_VENV, "uv pip install -r pyproject.toml"]


def test_uvize_keeps_uv_pip_requirements():
    out = _uvize_install_commands(["uv pip install -r pyproject.toml"], {"py": {}})
    assert out == [_PINNED_VENV, "uv pip install -r pyproject.toml"]


def test_uvize_handles_pip3_and_python_detected_by_section():
    # No leading-pip command, but the project IS Python → still create the venv.
    out = _uvize_install_commands(["pip3 install -r requirements.txt"], {"py": {}})
    assert out == [_PINNED_VENV, "uv pip install -r requirements.txt"]


def test_uvize_passes_through_non_python():
    out = _uvize_install_commands(["npm install"], {"js": {}})
    assert out == ["npm install"]  # no venv, no rewrite


@pytest.mark.asyncio
async def test_collect_env_field_uvizes_python_install():
    """End-to-end through collect_env_field: a detected bare-pip install becomes
    a uv venv bootstrap + uv pip install."""
    env = {"py": {"install_command": ["pip", "install", "-e", "."]}}
    eff = MockEffects(files={".agent/env.json": json.dumps(env)})
    si = StepInput(
        context={},
        params={"field": "install_command", "output_key": "install_commands"},
        meta=FlowMeta(flow_name="project_ops", step_id="collect_installs", attempt=1),
        effects=eff,
    )

    out = await action_collect_env_field(si)

    assert out.result["commands_found"] is True
    assert out.context_updates["install_commands"] == [
        _PINNED_VENV,
        "uv pip install -r pyproject.toml",
    ]
