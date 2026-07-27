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
    """A USABLE ``<wd>/.venv`` → VIRTUAL_ENV + PATH-prepend, so bare ``python``
    resolves to the per-project interpreter.

    The fixture now installs a distribution as well as the binary: activation
    requires the venv to hold at least one, because a bare `bin/python` was
    passing this check while the venv was empty and shadowing a working system
    interpreter. The assertions below are unchanged — only the precondition
    tightened."""
    bindir = tmp_path / ".venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").write_text("#!/bin/sh\n")
    (tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
     / "PyYAML-6.0.3.dist-info").mkdir(parents=True)

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


# ── The empty-venv trap ───────────────────────────────────────────────
#
# `syntax` is a required field for every detected extension, so EVERY Python
# project has a `py` section. Gating venv creation on that section meant a model
# omitting `install_command` (its own prompt calls it "optional") still got
# `["uv venv --allow-existing --python 3.x"]` — venv creation with nothing after
# it. The caller computes `commands_found` AFTER this rewrite, so that lone line
# reported success while creating an EMPTY venv, which then shadowed a working
# system interpreter for the rest of the mission. One 2h run lost ~50 minutes to
# the resulting blind diagnose loop (dev/POOLSIDE_TRAP_ROOTCAUSE.md).


def test_no_install_command_creates_NO_venv():
    """THE core regression. An empty venv is strictly worse than no venv."""
    out = _uvize_install_commands([], {"py": {"syntax": ["python", "-m", "py_compile"]}})
    assert out == [], f"a py section alone must not create a venv, got {out!r}"


def test_python_section_with_only_a_non_python_install_creates_no_venv():
    # A `py` section plus an npm-only install is the same trap in subtler form:
    # nothing would ever be installed into the venv it used to create.
    out = _uvize_install_commands(["npm install"], {"py": {}, "js": {}})
    assert out == ["npm install"]


def test_venv_still_created_when_a_python_install_is_present():
    # The fix must not disable per-project pinning where it was doing its job.
    out = _uvize_install_commands(["uv pip install -r pyproject.toml"], {"py": {}})
    assert out == [_PINNED_VENV, "uv pip install -r pyproject.toml"]


def test_python_dash_m_pip_is_recognised_as_a_python_install():
    # The third form models reach for; previously it was neither normalised nor
    # counted, so it installed into whatever ambient python PATH offered.
    out = _uvize_install_commands(["python -m pip install pyyaml"], {"py": {}})
    assert out == [_PINNED_VENV, "uv pip install pyyaml"]
    out3 = _uvize_install_commands(["python3 -m pip install pyyaml"], {"py": {}})
    assert out3 == [_PINNED_VENV, "uv pip install pyyaml"]


def test_missing_install_command_is_logged_loudly(caplog):
    # The step used to report success silently; the failure then surfaced an
    # hour later as a ModuleNotFoundError with no trace back to this decision.
    import logging

    with caplog.at_level(logging.WARNING):
        _uvize_install_commands([], {"py": {"syntax": ["python", "-m", "py_compile"]}})
    assert any(
        "NO Python install command" in r.message for r in caplog.records
    ), "a Python project with no install command must warn"


# ── An empty venv must not shadow a working interpreter ───────────────


def _make_venv(root, *, dists=(), py="python"):
    """Build a venv skeleton: bin/<py> plus a site-packages with `dists`."""
    bindir = os.path.join(root, ".venv", "bin")
    os.makedirs(bindir, exist_ok=True)
    open(os.path.join(bindir, py), "w").close()
    site = os.path.join(root, ".venv", "lib", "python3.12", "site-packages")
    os.makedirs(site, exist_ok=True)
    for d in dists:
        os.makedirs(os.path.join(site, d), exist_ok=True)
    return root


def test_empty_venv_is_NOT_activated(tmp_path, caplog):
    """THE second regression. `bin/python` existing was the whole test, and an
    empty `uv venv` passes it — so the venv hid a system Python that HAD the
    project's dependencies."""
    import logging

    wd = _make_venv(str(tmp_path))
    with caplog.at_level(logging.WARNING):
        overrides = LocalEffects(working_directory=wd).venv_env_overrides()
    assert overrides == {}, "an empty venv must not be activated"
    assert any("no installed distributions" in r.message for r in caplog.records)


def test_populated_venv_IS_activated(tmp_path):
    # The fix must not disable per-project pinning where it was working.
    wd = _make_venv(str(tmp_path), dists=["yaml", "PyYAML-6.0.3.dist-info"])
    overrides = LocalEffects(working_directory=wd).venv_env_overrides()
    assert overrides.get("VIRTUAL_ENV", "").endswith(".venv")
    assert overrides["PATH"].split(os.pathsep)[0].endswith(os.path.join(".venv", "bin"))


def test_package_dir_without_dist_info_does_not_count(tmp_path):
    # A bare directory is not an installed distribution — uv/pip always write a
    # .dist-info alongside. Requiring the marker avoids treating stray files
    # (e.g. _virtualenv.py) as proof the venv is usable.
    wd = _make_venv(str(tmp_path), dists=["not_a_real_package"])
    assert LocalEffects(working_directory=wd).venv_env_overrides() == {}


def test_no_venv_at_all_uses_ambient_interpreter(tmp_path):
    assert LocalEffects(working_directory=str(tmp_path)).venv_env_overrides() == {}


def test_venv_becomes_usable_once_a_distribution_lands(tmp_path):
    # Not cached: an install arrives mid-mission and the answer must change.
    wd = _make_venv(str(tmp_path))
    eff = LocalEffects(working_directory=wd)
    assert eff.venv_env_overrides() == {}
    os.makedirs(
        os.path.join(wd, ".venv", "lib", "python3.12", "site-packages",
                     "PyYAML-6.0.3.dist-info"),
        exist_ok=True,
    )
    assert eff.venv_env_overrides() != {}, "must re-evaluate after an install"
