"""SWE Phase B.5 — repair verification loop, test gate, and budget floors.

Pins the pivot from localization to verification (dev/SWE_PHASE_A_FINDINGS.md):
the repo's own failing tests become a repair goal's ground truth (M1), their
source seeds the diagnose prompt so the fix matches the call signature (M2), an
edit that breaks collection re-diagnoses instead of proceeding (M2), a smoke
check that failed at baseline can't indict an edit (M4), and a config-togglable
test-suite gate harvests fix goals from failures between functional and quality
(M6).
"""

from __future__ import annotations

import pytest

from agent.actions.pipeline_actions import (
    _parse_pytest_output,
    derive_repair_tests,
    extract_repair_terms,
    is_repair_profile,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import GoalRecord, MissionConfig, MissionState


def _mission(profile="repair", goals=None) -> MissionState:
    return MissionState(
        objective="DirFileSystem missing open_async method",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", task_profile=profile),
        goals=goals or [],
    )


# ── M1: term extraction + test selection ──────────────────────────────


def test_extract_repair_terms_prioritizes_identifiers():
    terms = extract_repair_terms(
        "DirFileSystem is missing `open_async()` — add it to dirfs.py"
    )
    assert "DirFileSystem" in terms
    assert "open_async" in terms
    # prose stopwords dropped
    assert "missing" not in terms and "add" not in terms


def test_parse_pytest_output_nodes_and_collection():
    ok = "FAILED tests/test_dirfs.py::test_open_async - TypeError\n1 failed"
    nodes, collect_ok = _parse_pytest_output(ok)
    assert nodes == ["tests/test_dirfs.py::test_open_async"] and collect_ok is True
    broken = "ImportError while importing test module\nERRORS during collection"
    _n, collect_ok2 = _parse_pytest_output(broken)
    assert collect_ok2 is False


@pytest.mark.asyncio
async def test_derive_repair_tests_selects_and_baselines():
    fx = MockEffects(
        files={
            "fsspec/implementations/tests/test_dirfs.py": "def test_open_async():\n    fs.open_async('x')\n",
            "fsspec/tests/test_unrelated.py": "def test_other():\n    pass\n",
        },
        commands={
            "/bin/sh": CommandResult(
                return_code=1,
                stdout="FAILED fsspec/implementations/tests/test_dirfs.py::test_open_async - TypeError\n1 failed",
                stderr="",
                command="pytest",
            )
        },
    )
    rt = await derive_repair_tests(fx, "DirFileSystem missing open_async")
    assert rt["derived"] is True
    assert "fsspec/implementations/tests/test_dirfs.py" in rt["test_files"]
    assert "test_unrelated" not in " ".join(rt["test_files"])  # no term hits
    assert rt["command"].startswith("python -m pytest")
    assert rt["failing_nodes"] == [
        "fsspec/implementations/tests/test_dirfs.py::test_open_async"
    ]
    assert rt["collect_ok"] is True


@pytest.mark.asyncio
async def test_derive_repair_tests_empty_when_no_matching_suite():
    fx = MockEffects(files={"src/mod.py": "def f(): pass\n"})
    rt = await derive_repair_tests(fx, "TotallyUnrelatedThing broken")
    assert rt == {}


def test_is_repair_profile():
    assert is_repair_profile(_mission("repair")) is True
    assert is_repair_profile(_mission("plain")) is False


# ── M1: deterministic-first dispatch on a repair goal ─────────────────


@pytest.mark.asyncio
async def test_functional_sweep_dispatches_repair_suite_deterministically():
    from agent.actions.mission_actions import action_functional_sweep_next

    goal = GoalRecord(description="DirFileSystem missing open_async", type="functional")
    m = _mission(goals=[goal])
    fx = MockEffects(
        mission=m,
        files={"tests/test_dirfs.py": "def test_open_async():\n    fs.open_async('x')\n"},
        commands={
            "/bin/sh": CommandResult(
                return_code=1,
                stdout="FAILED tests/test_dirfs.py::test_open_async - TypeError\n1 failed",
                stderr="",
                command="pytest",
            )
        },
    )
    si = StepInput(
        context={"mission": m},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="functional_sweep_next"),
        effects=fx,
    )
    out = await action_functional_sweep_next(si)
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "interact"
    assert dc["interaction_mode"] == "deterministic"
    assert dc["run_command"].startswith("python -m pytest")
    # persisted on the goal, and mirrored as a tighten-only acceptance check
    assert goal.repair_tests.get("derived") is True
    assert any("pytest" in c["command"] for c in goal.acceptance_checks)


# ── M6: the test-suite gate ───────────────────────────────────────────


def _gate_si(mission, effects):
    return StepInput(
        context={"mission": mission},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="dispatch_test_gate"),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_gate_off_certifies_immediately():
    from agent.actions.mission_actions import action_run_test_suite_gate

    m = _mission()
    m.config.test_gate = "off"
    out = await action_run_test_suite_gate(_gate_si(m, MockEffects(mission=m)))
    assert out.result["tests_verified"] is True and m.tests_verified is True


@pytest.mark.asyncio
async def test_gate_auto_no_suite_stands_down():
    from agent.actions.mission_actions import action_run_test_suite_gate

    m = _mission()  # no repair_tests on goals, objective yields no test files
    fx = MockEffects(mission=m, files={"src/mod.py": "def f(): pass\n"})
    out = await action_run_test_suite_gate(_gate_si(m, fx))
    assert m.tests_verified is True
    assert "no test suite" in out.observations


@pytest.mark.asyncio
async def test_gate_harvests_fix_goals_on_failure():
    from agent.actions.mission_actions import action_run_test_suite_gate

    goal = GoalRecord(
        description="fix it",
        type="functional",
        status="complete",
        repair_tests={
            "test_files": ["tests/test_dirfs.py"],
            "collect_ok": True,
            "derived": True,
        },
    )
    m = _mission(goals=[goal])
    fx = MockEffects(
        mission=m,
        commands={
            "/bin/sh": CommandResult(
                return_code=1,
                stdout="FAILED tests/test_dirfs.py::test_open_async - TypeError\n1 failed",
                stderr="",
                command="pytest",
            )
        },
    )
    out = await action_run_test_suite_gate(_gate_si(m, fx))
    assert out.result["harvested"] == 1
    assert m.tests_verified is False  # NOT certified — fix loop runs first
    harvested = [g for g in m.goals if g.origin == "test_gate"]
    assert len(harvested) == 1
    assert harvested[0].status == "incomplete"
    assert "test_open_async" in harvested[0].finding_signature
    # idempotent: a second run reopens (already complete? no — still incomplete) → skip, no dup
    out2 = await action_run_test_suite_gate(_gate_si(m, fx))
    assert len([g for g in m.goals if g.origin == "test_gate"]) == 1
    assert out2.result["tests_verified"] is True  # all failing already in flight → finalize


@pytest.mark.asyncio
async def test_gate_certifies_on_clean_suite():
    from agent.actions.mission_actions import action_run_test_suite_gate

    goal = GoalRecord(
        description="fix it",
        type="functional",
        status="complete",
        repair_tests={"test_files": ["tests/test_x.py"], "collect_ok": True, "derived": True},
    )
    m = _mission(goals=[goal])
    fx = MockEffects(
        mission=m,
        commands={
            "/bin/sh": CommandResult(
                return_code=0, stdout="3 passed", stderr="", command="pytest"
            )
        },
    )
    out = await action_run_test_suite_gate(_gate_si(m, fx))
    assert out.result["tests_verified"] is True and m.tests_verified is True


@pytest.mark.asyncio
async def test_gate_stands_down_on_baseline_collection_failure():
    from agent.actions.mission_actions import action_run_test_suite_gate

    goal = GoalRecord(
        description="fix it",
        type="functional",
        status="complete",
        repair_tests={"test_files": ["tests/test_x.py"], "collect_ok": False, "derived": True},
    )
    m = _mission(goals=[goal])
    out = await action_run_test_suite_gate(_gate_si(m, MockEffects(mission=m)))
    assert m.tests_verified is True
    assert "baseline" in out.observations


# ── M2: failing-test source seeds the diagnose prompt ─────────────────


@pytest.mark.asyncio
async def test_diagnose_seed_includes_failing_test_body():
    from agent.actions.diagnosis_session_actions import _failing_test_block

    fx = MockEffects(
        files={
            "tests/test_dirfs.py": (
                "import pytest\n\n"
                "def test_open_async():\n"
                "    fs = DirFileSystem('/tmp')\n"
                "    f = await fs.open_async('x', 'rb')\n"
                "    assert f\n\n"
                "def test_other():\n    pass\n"
            )
        }
    )
    err = "FAILED tests/test_dirfs.py::test_open_async - TypeError: too many args"
    block = await _failing_test_block(fx, err)
    assert "def test_open_async" in block
    assert "open_async('x', 'rb')" in block  # the exact call signature
    assert "def test_other" not in block  # only the failing node


@pytest.mark.asyncio
async def test_failing_test_block_empty_when_no_nodes():
    from agent.actions.diagnosis_session_actions import _failing_test_block

    assert await _failing_test_block(MockEffects(), "no failures here") == ""


# ── M4: smoke baseline stand-down ─────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_check_stands_down_when_baseline_failing():
    from agent.actions.pipeline_actions import action_run_validation_checks_from_env

    m = _mission()
    m.environment_verified = True
    m.smoke_baseline_ok = False  # unbuilt checkout — smoke fails regardless
    from agent.persistence.models import ArchitectureState

    m.architecture = ArchitectureState(run_command="python -c 'import astropy'")
    fx = MockEffects(
        mission=m,
        files={"astropy/io/ascii/qdp.py": "def f():\n    return 1\n"},
        commands={
            # syntax passes; the smoke import would fail — but must be SKIPPED
            "python": CommandResult(return_code=0, stdout="", stderr="", command="py"),
            "/bin/sh": CommandResult(
                return_code=1, stdout="", stderr="ModuleNotFoundError", command="smoke"
            ),
        },
    )
    si = StepInput(
        context={
            "validation_commands": {
                "syntax": ["python", "-c", "import py_compile"],
            }
        },
        params={"target": "astropy/io/ascii/qdp.py"},
        meta=FlowMeta(flow_name="file_ops", step_id="run_checks"),
        effects=fx,
    )
    out = await action_run_validation_checks_from_env(si)
    # smoke stood down → not counted as a failure → no whole-file self-correct
    assert out.result["smoke_failed"] is False
    assert any("BASELINE" in line for line in out.context_updates["validation_output"].splitlines())
