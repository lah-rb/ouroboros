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
nothing. It broke every `uv` invocation for the rest of the run.

Routing was taught to prefer project_ops (structural 08-03, functional b75,
quality 08-10) — and the full 08-10 trace shows why a preference was not
enough. project_ops CANNOT EDIT AN EXISTING FILE: its only writer runs with
protect_existing, and verify_env probes only the DECLARED dependencies, so the
one dependency missing from the declaration is exactly the one it cannot see.
It reported success, having changed nothing, twice — and then the model
itself rerouted to file_ops, which is when the frame editor got its chance:

  08:49:24  uv sync: "Resolved 1 package, Checked 1 package"   (manifest CLEAN)
  08:50:39  diagnose: kind=module_fix, statement=`pytest = "^7.4"`, flow=file_ops
  08:50:55  patch_module spliced it in                          (manifest BROKEN)
  08:53:02  uv: "TOML parse error at line 21"                   — and every run after

So there are three floors here, not one: never frame-edit a manifest; never
route a file edit to a flow that cannot write files; and never let a flow that
changed nothing count as a fix.
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


# ══════════════════════════════════════════════════════════════════════
# Floor 2: a file edit goes to the flow that can edit files
# ══════════════════════════════════════════════════════════════════════


def _diag(**over) -> dict:
    d = {
        "recommended_flow": "project_ops",
        "summary": "tests/ import pytest but pyproject.toml does not declare it",
        "target_file": "pyproject.toml",
        "change_spec": "Add pytest to the project's declared dependencies.",
        "diagnosis_kind": "module_fix",
        "module_statement": 'pytest = "^7.4"',
    }
    d.update(over)
    return d


@pytest.mark.parametrize(
    "target", ["pyproject.toml", "package.json", "world.yaml", "setup.cfg", "Pipfile"]
)
def test_a_named_manifest_routes_to_the_flow_that_can_write_it(target):
    from agent.actions.mission_actions import _fileops_dispatch_from_quality_diagnosis

    cfg = _fileops_dispatch_from_quality_diagnosis(
        _diag(target_file=target), goal_id="g1", goal_description="undeclared pytest"
    )
    assert cfg["flow"] == "file_ops", "project_ops cannot edit an existing file"
    assert cfg["target_file_path"] == target
    assert cfg["change_spec"]


@pytest.mark.parametrize("recommended", ["project_ops", "file_ops"])
def test_the_manifest_route_withholds_the_frame_editors_vocabulary(recommended):
    """diagnosis_kind/module_statement are what produced `pytest = "^7.4"`.
    The floor above stops the splice; this stops the suggestion reaching a
    flow that has no use for it — whichever flow the model named. On the live
    run it named both within four minutes for the same file."""
    from agent.actions.mission_actions import _fileops_dispatch_from_quality_diagnosis

    cfg = _fileops_dispatch_from_quality_diagnosis(
        _diag(recommended_flow=recommended), goal_id="g1"
    )
    assert cfg["flow"] == "file_ops"
    assert "module_statement" not in cfg
    assert "diagnosis_kind" not in cfg
    assert "schema" in cfg["flow_directive"] or "format" in cfg["flow_directive"]


def test_a_fileless_environment_finding_still_goes_to_project_ops():
    """Install/tooling/detect is project_ops' actual job — unchanged."""
    from agent.actions.mission_actions import _fileops_dispatch_from_quality_diagnosis

    cfg = _fileops_dispatch_from_quality_diagnosis(
        _diag(target_file="", summary="ruff is not installed"), goal_id="g1"
    )
    assert cfg["flow"] == "project_ops"
    assert cfg["target_file_path"] == ""


def test_a_code_target_is_unaffected_by_the_manifest_route():
    from agent.actions.mission_actions import _fileops_dispatch_from_quality_diagnosis

    cfg = _fileops_dispatch_from_quality_diagnosis(
        _diag(target_file="game.py", recommended_flow="file_ops"), goal_id="g1"
    )
    assert cfg["flow"] == "file_ops"
    assert cfg["module_statement"] == 'pytest = "^7.4"'  # code keeps the frame path


# ══════════════════════════════════════════════════════════════════════
# Floor 3: success without effect is not success
# ══════════════════════════════════════════════════════════════════════
#
# The live-lock. action_quality_sweep_next branched on file_ops and
# diagnose_issue only; a project_ops report matched neither, fell through to
# the "fresh goal" default, and re-diagnosed — forever, with failed_attempts
# empty the whole time, so the reopen ceiling, the attempt ceiling and the
# harvester were all blind. 28 cycles, 6 no-op successes, 0 recorded attempts.


def _quality_goal(reports):
    from agent.persistence.models import GoalRecord

    return GoalRecord(
        description="Quality gate failed: undeclared dependencies — pytest",
        type="quality",
        status="incomplete",
        origin="quality_gate",
        reports=reports,
    )


async def _sweep(goal):
    from agent.actions.mission_actions import action_quality_sweep_next
    from agent.effects.mock import MockEffects
    from agent.persistence.models import MissionConfig, MissionState

    m = MissionState(
        objective="t",
        status="active",
        goals=[goal],
        config=MissionConfig(working_directory="/tmp/x"),
    )
    return await action_quality_sweep_next(
        StepInput(
            context={"mission": m},
            params={},
            meta=FlowMeta(flow_name="mission_control", step_id="quality_sweep_next"),
            effects=MockEffects(),
        )
    )


@pytest.mark.asyncio
async def test_project_ops_changing_nothing_records_a_failed_attempt():
    from agent.persistence.models import DirectiveReport

    goal = _quality_goal(
        [
            DirectiveReport(
                flow="project_ops",
                status="success",
                summary="project_ops completed with status: success",
                files_affected=[],
            )
        ]
    )
    await _sweep(goal)
    assert len(goal.failed_attempts) == 1, "no-op success used to record nothing"
    assert "changed no files" in goal.failed_attempts[0].reason
    assert goal.status == "complete", "the gate re-reports if it is not really fixed"


@pytest.mark.asyncio
async def test_project_ops_that_did_change_something_also_records_the_attempt():
    """Same as the file_ops route: 'the commands exited 0' is not 'the goal is
    fixed'. The record is discarded with the goal when the fix holds."""
    from agent.persistence.models import DirectiveReport

    goal = _quality_goal(
        [
            DirectiveReport(
                flow="project_ops",
                status="success",
                summary="wrote setup.sh",
                files_affected=["setup.sh"],
            )
        ]
    )
    out = await _sweep(goal)
    assert len(goal.failed_attempts) == 1
    assert "changed no files" not in goal.failed_attempts[0].reason
    assert goal.status == "complete"
    assert not out.result.get("needs_fix")


@pytest.mark.asyncio
async def test_a_project_ops_report_never_falls_through_to_re_diagnosis():
    """The live-lock itself: the old default re-diagnosed, so the pair
    alternated with nothing accumulating anywhere."""
    from agent.persistence.models import DirectiveReport

    goal = _quality_goal(
        [DirectiveReport(flow="project_ops", status="success", summary="s")]
    )
    out = await _sweep(goal)
    assert not out.result.get("needs_fix"), "re-dispatching here is the spin"
