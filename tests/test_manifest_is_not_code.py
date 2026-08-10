"""A declarative manifest has no module frame, and parsing is not the bar.

TWICE, SAME SHAPE, DIFFERENT FILES — a diagnosis that correctly said
"environment/config, not code" was routed into the module-frame editor,
whose whole vocabulary is "add a module-level line":

  2026-08-03  "ensure the environment provides python and ruff"
              → `assert shutil.which('python')` spliced into parser.py
  2026-08-10  a Poetry dependency
              → `pytest = "^7.4"` appended after [project.optional-dependencies]
                in a PEP 621 pyproject.toml

The second parses as TOML, so the scaffold floor passed it, and it declares
nothing. Meanwhile project_ops HAD produced a correct PEP 621 manifest with
pytest in `dependencies` — that output never landed. The quality gate then
failed on the same missing dependency round after round.

Routing was taught to prefer project_ops (structural 08-03, functional b75,
quality 08-10). Routing is a preference; these two are the floor.
"""

from __future__ import annotations

import pytest

from agent.actions.file_ops_actions import scaffold_parse_error
from agent.actions.frame_actions import action_check_module_fix
from agent.models import FlowMeta, StepInput

# Mirrors the real artifact: [project.optional-dependencies] is LAST, which
# is where TOML folds a trailing appended key.
_PEP621 = """[project]
name = "text-adventure"
dependencies = ["PyYAML>=6.0"]

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project.optional-dependencies]
dev = ["ruff>=0.4.0"]
"""


def _si(target: str, stmt: str) -> StepInput:
    return StepInput(
        context={
            "diagnosis_kind": "module_fix",
            "target_file_path": target,
            "module_statement": stmt,
            "file_content": _PEP621,
        },
        params={},
        meta=FlowMeta(flow_name="file_ops", step_id="check_module_fix"),
        effects=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "pyproject.toml",
        "config.json",
        "world.yaml",
        "settings.yml",
        "setup.cfg",
        "tox.ini",
        "uv.lock",
        "README.md",
    ],
)
async def test_a_declarative_config_never_reaches_the_frame_editor(target):
    out = await action_check_module_fix(_si(target, 'pytest = "^7.4"'))
    assert out.result["is_module_fix"] is False
    assert "declarative config" in out.observations


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["main.py", "run.sh", "Dockerfile"])
async def test_code_and_shell_still_take_the_frame_path(target):
    """Shell/Dockerfile are deliberately NOT config: a shebang, `source` or
    `set -e` IS a frame line, which is what module_fix was extended for."""
    out = await action_check_module_fix(_si(target, "import os"))
    assert "declarative config" not in out.observations


def test_a_stray_top_level_key_is_rejected_even_though_it_parses():
    """The exact bytes that shipped: valid TOML, meaningless as a manifest."""
    broken = _PEP621 + '\npytest = "^7.4"\n'
    err = scaffold_parse_error("pyproject.toml", broken, _PEP621)
    assert err is not None
    # TOML folds a trailing key into the LAST OPEN TABLE, so this does NOT
    # become a top-level key — it becomes an extras group "pytest" whose
    # value is a string. My first version of this check looked at the root
    # and missed it; the bytes below are the ones that actually shipped.
    assert "optional-dependencies" in err and "pytest" in err


def test_the_same_stray_key_is_caught_when_build_system_is_last():
    """The landing table depends on file order, so both placements must be
    covered — the first version of this check caught neither."""
    reordered = """[project]
name = "x"

[project.optional-dependencies]
dev = ["ruff"]

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
"""
    err = scaffold_parse_error(
        "pyproject.toml", reordered + '\npytest = "^7.4"\n', reordered
    )
    assert err is not None
    assert "build-system" in err and "pytest" in err


def test_a_poetry_pep621_hybrid_is_rejected():
    hybrid = _PEP621 + '\n[tool.poetry.dependencies]\npytest = "^7.4"\n'
    err = scaffold_parse_error("pyproject.toml", hybrid, _PEP621)
    assert err is not None
    assert "poetry" in err.lower()


def test_a_correct_manifest_still_writes():
    good = _PEP621.replace(
        'dependencies = ["PyYAML>=6.0"]',
        'dependencies = ["PyYAML>=6.0", "pytest>=7.4"]',
    )
    assert scaffold_parse_error("pyproject.toml", good, _PEP621) is None


def test_an_already_incoherent_manifest_does_not_block_a_repair():
    """Same stand-down the parse floor uses: if the file on disk is already
    broken, a write that is merely no worse must not be blocked."""
    broken = _PEP621 + '\npytest = "^7.4"\n'
    assert scaffold_parse_error("pyproject.toml", broken, broken) is None


def test_other_toml_files_are_untouched_by_the_coherence_check():
    assert scaffold_parse_error("ruff.toml", "line-length = 100\n", None) is None
