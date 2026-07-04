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
    steps = json.load(open(os.path.join(root, "flows", "compiled.json")))["replan"]["steps"]
    cd = {r["condition"]: r["transition"] for r in steps["choose_decompose"]["resolver"]["rules"]}
    assert cd["context.mission.config.task_profile == 'repair'"] == "decompose_repair"
    assert cd["true"] == "decompose_directive"
    assert steps["decompose_repair"]["prompt_template"]["template"] == "replan/decompose_directive_repair"
    # both variants feed the same derive step
    assert steps["decompose_repair"]["resolver"]["rules"][0]["transition"] == "derive_directive_goals"


# ── §2: repair write-guard ────────────────────────────────────────────


def test_repair_write_reason_flags_tests_and_scaffolding():
    from agent.actions.file_ops_actions import repair_write_reason

    assert repair_write_reason("django/tests/queryset_union_ordering.py")
    assert repair_write_reason("pkg/test_thing.py")
    assert repair_write_reason("pkg/thing_test.py")
    assert repair_write_reason("pyproject.toml")
    assert repair_write_reason("requirements-dev.txt")
    assert repair_write_reason("README.md")
    # real source is fine
    assert repair_write_reason("sympy/geometry/point.py") is None
    assert repair_write_reason("django/db/models/sql/compiler.py") is None


@pytest.mark.asyncio
async def test_guarded_write_blocks_new_test_on_repair_allows_source():
    from agent.actions.file_ops_actions import guarded_write_file

    fx = MockEffects(files={"pkg/mod.py": "def f():\n    return 1\n"})
    # new test file → rejected in repair mode
    ok, err = await guarded_write_file(fx, "pkg/tests/test_new.py", "def test_x(): pass\n", repair_mode=True)
    assert ok is False and "do not author tests" in err
    assert "pkg/tests/test_new.py" not in fx._files
    # editing existing source → allowed
    ok2, err2 = await guarded_write_file(fx, "pkg/mod.py", "def f():\n    return 2\n", repair_mode=True)
    assert ok2 is True and err2 is None


@pytest.mark.asyncio
async def test_guarded_write_test_file_allowed_when_not_repair():
    from agent.actions.file_ops_actions import guarded_write_file

    fx = MockEffects(files={})
    ok, err = await guarded_write_file(fx, "pkg/tests/test_new.py", "def test_x(): pass\n", repair_mode=False)
    assert ok is True and err is None


@pytest.mark.asyncio
async def test_guarded_write_allows_editing_existing_scaffolding_on_repair():
    # The guard blocks CREATION only — an existing pyproject can still be edited
    # (the scaffold parse floor governs its content separately).
    from agent.actions.file_ops_actions import guarded_write_file

    valid = '[project]\nname = "x"\nversion = "1"\n'
    fx = MockEffects(files={"pyproject.toml": valid + '[tool.ruff]\nline-length = 88\n'})
    ok, err = await guarded_write_file(fx, "pyproject.toml", valid + '[tool.mypy]\nstrict = true\n', repair_mode=True)
    assert ok is True and err is None


# ── §3: test-selection module-name match ──────────────────────────────


@pytest.mark.asyncio
async def test_selection_prefers_module_matching_test():
    from agent.actions.pipeline_actions import derive_repair_tests

    # test_args has MORE term hits, but test_point matches the module name →
    # must rank first (the sympy false-done fix).
    fx = _SeqEffects(
        [CommandResult(return_code=1, stdout="FAILED tests/test_point.py::test_distance - AssertionError", stderr="", command="p")],
        files={
            "tests/test_args.py": "Point\nPoint\nPoint\ndistance\n",  # 4 hits
            "tests/test_point.py": "def test_distance():\n    Point(2,0).distance(Point(1,0,2))\n",  # fewer
        },
    )
    rt = await derive_repair_tests(fx, "`Point.distance` in sympy/geometry/point.py drops a dimension")
    assert rt["test_files"][0] == "tests/test_point.py"


class _SeqEffects(MockEffects):
    def __init__(self, results, **kw):
        super().__init__(**kw)
        self._seq = list(results)

    async def run_command(self, command, **kw):
        if command and command[0] == "/bin/sh" and self._seq:
            return self._seq.pop(0)
        return await super().run_command(command, **kw)
