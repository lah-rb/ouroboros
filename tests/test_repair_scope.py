"""Brownfield repair scope discipline (SWE pilot-1 corrections).

Pins the three levers that stop a repair mission over-scoping a bug fix into a
multi-goal build: (1) repair decompose yields fix goals — capability_absent
FALSE, so they route diagnose-first not explore-and-build; (2) a repair
write-guard refuses to CREATE new test/scaffolding files (django wrote its own
test across 7 cycles) while leaving source edits alone; (3) test selection
prefers the module-name-matching test (sympy verified green against the wrong
suite).
"""

from __future__ import annotations

import json
import os

import pytest

from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import MissionConfig, MissionState
from tests.conftest import ScriptedCommandEffects as _SeqEffects


def _mission(profile="repair", **cfg) -> MissionState:
    return MissionState(
        objective="fix the bug",
        status="active",
        config=MissionConfig(working_directory="/testbed", task_profile=profile, **cfg),
        pending_directive="Point.distance drops the extra dimension",
    )


def _si(mission, effects=None, **ctx) -> StepInput:
    return StepInput(
        context={"mission": mission, **ctx},
        inputs={},
        params={},
        meta=FlowMeta(flow_name="replan", step_id="derive_directive_goals"),
        effects=effects if effects is not None else MockEffects(),
    )


# ── §1: repair decompose → fix goals (capability_absent False) ────────


_DECOMP = (
    '```json\n{"new_files": [], "capabilities": ['
    '{"description": "distance accounts for all dimensions",'
    ' "placement": "sympy/geometry/point.py, Point.distance"}]}\n```'
)


@pytest.mark.asyncio
async def test_repair_goals_are_fixes_not_absent_capabilities():
    from agent.actions.mission_actions import action_derive_directive_goals

    m = _mission("repair")
    fx = MockEffects(mission=m)
    await action_derive_directive_goals(_si(m, fx, inference_response=_DECOMP))
    fns = [g for g in m.goals if g.type == "functional"]
    assert len(fns) == 1
    assert fns[0].capability_absent is False  # diagnose-first, not explore-build


@pytest.mark.asyncio
async def test_nonrepair_goals_stay_capability_absent():
    from agent.actions.mission_actions import action_derive_directive_goals

    m = _mission("plain")
    fx = MockEffects(mission=m)
    await action_derive_directive_goals(_si(m, fx, inference_response=_DECOMP))
    fns = [g for g in m.goals if g.type == "functional"]
    assert len(fns) == 1
    assert fns[0].capability_absent is True  # greenfield build path unchanged


def test_replan_selects_repair_prompt_by_profile():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    steps = json.load(open(os.path.join(root, "flows", "compiled.json")))["replan"][
        "steps"
    ]
    cd = {
        r["condition"]: r["transition"]
        for r in steps["choose_decompose"]["resolver"]["rules"]
    }
    assert cd["context.mission.config.task_profile == 'repair'"] == "decompose_repair"
    assert cd["true"] == "decompose_directive"
    assert (
        steps["decompose_repair"]["prompt_template"]["template"]
        == "replan/decompose_directive_repair"
    )
    # both variants feed the same derive step
    assert (
        steps["decompose_repair"]["resolver"]["rules"][0]["transition"]
        == "derive_directive_goals"
    )


# ── §2: repair write-guard ────────────────────────────────────────────


def test_repair_write_reason_classifies_tests_vs_config():
    from agent.actions.file_ops_actions import repair_write_reason

    # test files → reason set, block_edits=False (block CREATE only)
    for p in (
        "django/tests/queryset_union_ordering.py",
        "pkg/test_thing.py",
        "pkg/thing_test.py",
    ):
        reason, block_edits = repair_write_reason(p)
        assert reason and block_edits is False, p
    # config/CI/docs → reason set, block_edits=True (block CREATE and EDIT)
    for p in (
        "pyproject.toml",
        "setup.cfg",
        "requirements-dev.txt",
        "README.md",
        "CONTRIBUTING.rst",
        ".github/workflows/ci.yml",
        "tox.ini",
        ".pre-commit-config.yaml",
        "docs/.github/FUNDING.yml",
    ):
        reason, block_edits = repair_write_reason(p)
        assert reason and block_edits is True, p
    # real source is fine (no reason)
    for p in (
        "sympy/geometry/point.py",
        "django/db/models/sql/compiler.py",
        "src/_pytest/unittest.py",
    ):
        reason, _ = repair_write_reason(p)
        assert reason is None, p


@pytest.mark.asyncio
async def test_guarded_write_blocks_new_test_on_repair_allows_source():
    from agent.actions.file_ops_actions import guarded_write_file

    fx = MockEffects(files={"pkg/mod.py": "def f():\n    return 1\n"})
    # new test file → rejected in repair mode
    ok, err = await guarded_write_file(
        fx, "pkg/tests/test_new.py", "def test_x(): pass\n", repair_mode=True
    )
    assert ok is False and "do not author tests" in err
    assert "pkg/tests/test_new.py" not in fx._files
    # editing existing source → allowed
    ok2, err2 = await guarded_write_file(
        fx, "pkg/mod.py", "def f():\n    return 2\n", repair_mode=True
    )
    assert ok2 is True and err2 is None


@pytest.mark.asyncio
async def test_guarded_write_test_file_allowed_when_not_repair():
    from agent.actions.file_ops_actions import guarded_write_file

    fx = MockEffects(files={})
    ok, err = await guarded_write_file(
        fx, "pkg/tests/test_new.py", "def test_x(): pass\n", repair_mode=False
    )
    assert ok is True and err is None


@pytest.mark.asyncio
async def test_guarded_write_blocks_editing_config_on_repair():
    # Config/CI/docs are blocked create-OR-edit on repair (pytest-10081 leaked
    # ci.yml/CONTRIBUTING edits from the env phase). Editing an EXISTING config
    # is now refused, unlike a source edit.
    from agent.actions.file_ops_actions import guarded_write_file

    valid = '[project]\nname = "x"\nversion = "1"\n'
    fx = MockEffects(
        files={
            "pyproject.toml": valid + "[tool.ruff]\nline-length = 88\n",
            ".github/workflows/ci.yml": "on: [push]\n",
        }
    )
    ok, err = await guarded_write_file(
        fx, "pyproject.toml", valid + "[tool.mypy]\nstrict = true\n", repair_mode=True
    )
    assert ok is False and "config/CI/docs" in err  # existing config edit blocked
    ok2, err2 = await guarded_write_file(
        fx, ".github/workflows/ci.yml", "on: [pull_request]\n", repair_mode=True
    )
    assert ok2 is False and "config/CI/docs" in err2


# ── §1b: repair fix-goal routes diagnose-first, not verify-interact ───
# The capability_absent=False flip alone sent repair goals to the default
# "verify it works" interact, which no-ops on SWE-bench's held-out test →
# goal completes with zero edits (pilot-2: 5 empty patches). A repair fix
# goal with no witnessed baseline test must dispatch diagnose_issue.


@pytest.mark.asyncio
async def test_repair_fix_goal_dispatches_diagnose_not_verify():
    from agent.actions.mission_actions import action_functional_sweep_next
    from agent.persistence.models import GoalRecord

    m = _mission("repair")
    m.pending_directive = ""
    m.goals = [
        GoalRecord(
            description="Point.distance includes all coordinate dimensions",
            type="functional",
            origin="directive",
            capability_absent=False,  # a fix, not a build
        )
    ]
    # MockEffects: no test file greps a baseline-failing witness (held-out test)
    out = await action_functional_sweep_next(_si(m, MockEffects(mission=m)))
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "diagnose_issue"  # NOT interact/verify
    assert out.result.get("needs_fix") is True


@pytest.mark.asyncio
async def test_nonrepair_fix_goal_still_verifies_via_interact():
    # The diagnose-first branch is repair-only; a plain-profile design goal
    # keeps the reproduce/verify interact path.
    from agent.actions.mission_actions import action_functional_sweep_next
    from agent.persistence.models import GoalRecord

    m = _mission("plain")
    m.pending_directive = ""
    m.goals = [
        GoalRecord(description="the widget renders", type="functional", origin="design")
    ]
    out = await action_functional_sweep_next(_si(m, MockEffects(mission=m)))
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "interact"


# ── §4: held_out_tests (SWE-bench) disables in-repo-test ground truth ──
# The regression test is held out, so every baseline-failing test is a
# pre-existing red-herring. The test gate must not harvest them (pilot-3:
# astropy 1 goal → 9 phantom "fix failing test" goals) and the repair-test
# loop must not witness them — repair goals drive off the problem statement.


@pytest.mark.asyncio
async def test_held_out_test_gate_passes_without_harvest():
    from agent.actions.mission_actions import action_run_test_suite_gate
    from agent.persistence.models import GoalRecord

    m = _mission("repair")
    m.config.held_out_tests = True
    m.config.test_gate = "auto"
    m.goals = [
        GoalRecord(
            description="fix the bug",
            type="functional",
            origin="directive",
            # The goal must already carry repair_tests, or the gate never
            # reaches the suite: with none, derive_repair_tests finds nothing
            # and returns the SAME {tests_verified: True, harvested: 0} as the
            # held-out pass. That is why the original version of this test
            # stayed green with the guard deleted outright (verified
            # 2026-07-25) — and why a bare MockEffects was not enough either.
            repair_tests={"test_files": ["tests/test_models.py"], "collect_ok": True},
        )
    ]
    # A RED suite, so only the guard can explain a clean pass.
    fx = _SeqEffects(
        [
            CommandResult(
                return_code=1,
                stdout="FAILED tests/test_models.py::test_separability - E",
                stderr="",
                command="p",
            )
        ],
        files={"tests/test_models.py": "def test_separability(): assert False\n"},
        mission=m,
    )
    out = await action_run_test_suite_gate(_si(m, fx))
    assert out.result["tests_verified"] is True
    assert out.result["harvested"] == 0  # no default: an absent key is a bug
    assert not [g for g in m.goals if g.origin == "test_gate"]  # no phantom goals
    # The whole point: the repo suite is never even run.
    assert fx._seq, "held-out gate must stand down WITHOUT running the suite"


@pytest.mark.asyncio
async def test_held_out_repair_goal_skips_witness_goes_diagnose():
    # Even if a baseline-failing test EXISTS, held_out skips the repair-test
    # loop → the goal routes diagnose-first (not deterministic-verify).
    from agent.actions.mission_actions import action_functional_sweep_next
    from agent.persistence.models import GoalRecord

    m = _mission("repair")
    m.config.held_out_tests = True
    m.pending_directive = ""
    m.goals = [
        GoalRecord(
            description="Point.distance drops a dim",
            type="functional",
            origin="directive",
            capability_absent=False,
        )
    ]
    fx = _SeqEffects(
        [
            CommandResult(
                return_code=1,
                stdout="FAILED tests/test_point.py::test_x - E",
                stderr="",
                command="p",
            )
        ],
        files={"tests/test_point.py": "def test_x(): Point()\n"},
    )
    out = await action_functional_sweep_next(_si(m, fx))
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "diagnose_issue"  # NOT the deterministic repair-test verify
    # repair_tests must not have been derived (loop skipped)
    assert not (m.goals[0].repair_tests or {}).get("command")


def test_swe_adapter_sets_held_out_tests():
    from adapters.swe.runner import build_mission
    import tempfile
    from adapters.swe.instance import SweInstance

    inst = SweInstance(
        instance_id="a__b-1",
        repo="a/b",
        base_commit="c",
        problem_statement="bug",
        patch="P",
        test_patch="T",
    )
    with tempfile.TemporaryDirectory() as d:
        m, _ = build_mission(inst, d)
    assert m.config.held_out_tests is True


# ── §3: test-selection module-name match ──────────────────────────────


# ── §3b: nonzero-rc-with-no-failing-nodes is NOT a witness ────────────
# In SWE-bench the regression test is held out, so a suite exits nonzero on
# noise (warnings-as-errors, teardown) without any FAILED/ERROR node. That is
# not a witness — derive must return {} so the goal routes diagnose-first
# (pilot-3: spurious witness → deterministic-verify against a green baseline →
# 4 empty patches).


@pytest.mark.asyncio
async def test_nonzero_rc_without_failing_nodes_is_not_witnessed():
    from agent.actions.pipeline_actions import derive_repair_tests

    # baseline: rc=1 but no FAILED/ERROR lines and collection fine → noise
    noisy = CommandResult(
        return_code=1,
        stdout="1 passed, 3 warnings in 0.4s\n",
        stderr="warnings summary: -W error triggered",
        command="p",
    )
    fx = _SeqEffects(
        [noisy, noisy],  # both candidate pairs return the same noise
        files={"tests/test_point.py": "def test_x():\n    Point()\n"},
    )
    rt = await derive_repair_tests(fx, "`Point.distance` in point.py drops a dimension")
    assert rt == {}  # no witness → caller falls to diagnose-first


@pytest.mark.asyncio
async def test_real_failing_node_is_still_witnessed():
    from agent.actions.pipeline_actions import derive_repair_tests

    fx = _SeqEffects(
        [
            CommandResult(
                return_code=1,
                stdout="FAILED tests/test_point.py::test_d - E",
                stderr="",
                command="p",
            )
        ],
        files={"tests/test_point.py": "def test_d():\n    Point()\n"},
    )
    rt = await derive_repair_tests(fx, "`Point.distance` in point.py drops a dimension")
    assert rt.get("derived") is True
    assert rt["failing_nodes"] == ["tests/test_point.py::test_d"]


@pytest.mark.asyncio
async def test_selection_prefers_module_matching_test():
    from agent.actions.pipeline_actions import derive_repair_tests

    # test_args has MORE term hits, but test_point matches the module name →
    # must rank first (the sympy false-done fix).
    fx = _SeqEffects(
        [
            CommandResult(
                return_code=1,
                stdout="FAILED tests/test_point.py::test_distance - AssertionError",
                stderr="",
                command="p",
            )
        ],
        files={
            "tests/test_args.py": "Point\nPoint\nPoint\ndistance\n",  # 4 hits
            "tests/test_point.py": "def test_distance():\n    Point(2,0).distance(Point(1,0,2))\n",  # fewer
        },
    )
    rt = await derive_repair_tests(
        fx, "`Point.distance` in sympy/geometry/point.py drops a dimension"
    )
    assert rt["test_files"][0] == "tests/test_point.py"
