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
from tests.conftest import ScriptedCommandEffects as _SeqEffects


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
        files={
            "tests/test_dirfs.py": "def test_open_async():\n    fs.open_async('x')\n"
        },
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
    assert (
        out2.result["tests_verified"] is True
    )  # all failing already in flight → finalize


@pytest.mark.asyncio
async def test_gate_certifies_on_clean_suite():
    from agent.actions.mission_actions import action_run_test_suite_gate

    goal = GoalRecord(
        description="fix it",
        type="functional",
        status="complete",
        repair_tests={
            "test_files": ["tests/test_x.py"],
            "collect_ok": True,
            "derived": True,
        },
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
async def test_gate_stands_down_only_when_collection_STILL_broken():
    # Stale-baseline refresh (b5d): the gate re-checks collection live. Still
    # broken -> stand down (unbuildable checkout); the fixed-mid-mission case
    # is pinned in test_gate_stale_baseline_recheck.
    from agent.actions.mission_actions import action_run_test_suite_gate

    goal = GoalRecord(
        description="fix it",
        type="functional",
        status="complete",
        repair_tests={
            "test_files": ["tests/test_x.py"],
            "collect_ok": False,
            "derived": True,
        },
    )
    m = _mission(goals=[goal])
    fx = MockEffects(
        mission=m,
        commands={
            "/bin/sh": CommandResult(
                return_code=2, stdout="", stderr="errors during collection", command="c"
            )
        },
    )
    out = await action_run_test_suite_gate(_gate_si(m, fx))
    assert m.tests_verified is True
    assert "still does not collect" in out.observations


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
    assert any(
        "BASELINE" in line
        for line in out.context_updates["validation_output"].splitlines()
    )


# ── Retest regressions: capability_absent + recursive search ──────────


@pytest.mark.asyncio
async def test_repair_loop_engages_for_capability_absent_goals():
    # The first B.5 retest regression: BOTH repo-scale goals were
    # capability_absent (replan frames "supports X" as a build), and the repair
    # branch excluded them — the whole loop silently skipped. On a repair
    # mission the failing test IS the build spec; the loop must engage.
    from agent.actions.mission_actions import action_functional_sweep_next

    goal = GoalRecord(
        description="DirFileSystem supports open_async",
        type="functional",
        capability_absent=True,
    )
    m = _mission(goals=[goal])
    fx = MockEffects(
        mission=m,
        files={
            "tests/test_dirfs.py": "def test_open_async():\n    fs.open_async('x', 'rb', 5)\n"
        },
        commands={
            "/bin/sh": CommandResult(
                return_code=1,
                stdout="FAILED tests/test_dirfs.py::test_open_async - AttributeError",
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
    assert dc["interaction_mode"] == "deterministic"
    assert goal.repair_tests.get("derived") is True


@pytest.mark.asyncio
async def test_derive_repair_tests_finds_nested_test_dirs_local_glob():
    # LocalEffects expands "*.py" non-recursively; the double-pattern query
    # ("**/*.py") must still find test files in nested dirs. MockEffects
    # fnmatch is permissive for both, so pin the CALL pattern by asserting a
    # nested path is selected.
    fx = MockEffects(
        files={
            "pkg/sub/tests/test_deep.py": "def test_thing():\n    DeepThing().frob()\n"
        },
        commands={
            "/bin/sh": CommandResult(
                return_code=1,
                stdout="FAILED pkg/sub/tests/test_deep.py::test_thing - AssertionError",
                stderr="",
                command="pytest",
            )
        },
    )
    rt = await derive_repair_tests(fx, "DeepThing.frob returns wrong value")
    assert rt.get("test_files") == ["pkg/sub/tests/test_deep.py"]


# ── The witness rule + tiered term search (corrected-retest regressions) ──


@pytest.mark.asyncio
async def test_witness_rule_rejects_green_suite_tries_next():
    # First-ranked pick is green at baseline (the fsspec trap: test_local/
    # test_cached pass while the real defect lives in test_dirfs) → rejected;
    # the next-ranked candidate fails at baseline → selected.
    files = {
        # Most term-hits → ranked as the first PAIR, but green at baseline
        "tests/test_big_green.py": "DirFileSystem\n" * 5,
        "tests/test_also_green.py": "DirFileSystem\n" * 4,
        # Fewer hits → next-ranked candidate, and it witnesses the defect
        "tests/test_dirfs.py": "def test_open_async():\n    DirFileSystem().open_async('x')\n",
    }
    fx = _SeqEffects(
        [
            CommandResult(return_code=0, stdout="12 passed", stderr="", command="p"),
            CommandResult(
                return_code=1,
                stdout="FAILED tests/test_dirfs.py::test_open_async - TypeError",
                stderr="",
                command="p",
            ),
        ],
        files=files,
    )
    rt = await derive_repair_tests(fx, "DirFileSystem supports open_async")
    assert rt.get("derived") is True
    # the green pair was rejected; the witnessing candidate got selected
    assert "tests/test_dirfs.py" in rt["test_files"]
    assert rt["failing_nodes"] == ["tests/test_dirfs.py::test_open_async"]


@pytest.mark.asyncio
async def test_witness_rule_all_green_falls_back_to_evaluator():
    fx = _SeqEffects(
        [CommandResult(return_code=0, stdout="12 passed", stderr="", command="p")] * 2,
        files={
            "tests/test_a.py": "DirFileSystem\n",
            "tests/test_b.py": "DirFileSystem\n",
        },
    )
    rt = await derive_repair_tests(fx, "DirFileSystem supports open_async")
    assert rt == {}  # no witness anywhere → LLM-evaluator fallback


@pytest.mark.asyncio
async def test_strong_terms_exclude_prose_only_hits():
    # The astropy trap: prose words ("reader", "lines", "case") out-hit the
    # identifier in big generic test files. With tiered search, a file hit
    # ONLY by prose words is invisible when a strong term (QDP) exists.
    files = {
        "tests/test_generic.py": "reader lines case reader lines case\n" * 20,
        "tests/test_qdp.py": "def test_lowercase():\n    QDP().read('read serr 1 2')\n",
    }
    fx = _SeqEffects(
        [
            CommandResult(
                return_code=1,
                stdout="FAILED tests/test_qdp.py::test_lowercase - ValueError",
                stderr="",
                command="p",
            )
        ],
        files=files,
    )
    rt = await derive_repair_tests(
        fx, "The QDP reader accepts command lines in any letter case"
    )
    assert rt["test_files"] == ["tests/test_qdp.py"]


# ── b5d regressions: pytest-error grading + stale-baseline refresh ─────


@pytest.mark.asyncio
async def test_deterministic_eval_fails_fixture_error_even_with_exit0():
    # The b5d false success: a pytest run that ERRORED at setup (missing
    # `mocker` fixture — pytest-mock not installed) was graded goal_met
    # because the exit-code capture reported success and the pattern net only
    # knew Python tracebacks. Pytest error idioms must fail the grade
    # regardless of the captured exit code.
    from agent.actions.pipeline_actions import action_evaluate_deterministic_result

    si = StepInput(
        context={
            "terminal_output": (
                "==== ERRORS ====\n"
                "ERROR at setup of test_open[sync]\n"
                "file test_dirfs.py, line 12\n"
                "  def make_fs(mocker):\n"
                "E       fixture 'mocker' not found\n"
            ),
            "all_passed": True,  # the mis-captured exit code
        },
        inputs={},
        params={},
        meta=FlowMeta(flow_name="interact", step_id="evaluate_deterministic"),
        effects=MockEffects(),
    )
    out = await action_evaluate_deterministic_result(si)
    assert out.result["goal_met"] is False


# ── Should-raise contract (GoalRecord.expected_error) ─────────────────────


def _eval_si(terminal_output: str, all_passed: bool, *, goal=None) -> StepInput:

    mission = _mission(goals=[goal]) if goal else None
    return StepInput(
        context={"terminal_output": terminal_output, "all_passed": all_passed},
        inputs={"goal_id": goal.id} if goal else {},
        params={},
        meta=FlowMeta(flow_name="interact", step_id="evaluate_deterministic"),
        effects=MockEffects(mission=mission),
    )


@pytest.mark.asyncio
async def test_deterministic_eval_should_raise_passes_on_expected_exception():
    """The flask false-fail: a fix whose success is that bad input now RAISES.
    The propagated exception yields a non-zero exit + traceback — but with the
    goal's expected_error set, the named exception IS the pass signal."""
    from agent.actions.pipeline_actions import action_evaluate_deterministic_result

    goal = GoalRecord(
        description="reject empty separator",
        type="functional",
        expected_error="ValueError",
    )
    out = await action_evaluate_deterministic_result(
        _eval_si(
            "Traceback (most recent call last):\n"
            '  File "app.py", line 3, in <module>\n'
            "ValueError: separator must not be empty\n",
            all_passed=False,  # the propagated exception exits non-zero
            goal=goal,
        )
    )
    assert out.result["goal_met"] is True
    assert "ValueError" in out.observations


@pytest.mark.asyncio
async def test_deterministic_eval_should_raise_fails_when_not_raised():
    """A should-raise test that FAILED because nothing raised must still fail —
    pytest's `DID NOT RAISE <class 'ValueError'>` contains 'ValueError' but not
    'ValueError:' (the colon-anchored match), so it is not mistaken for a raise."""
    from agent.actions.pipeline_actions import action_evaluate_deterministic_result

    goal = GoalRecord(
        description="reject empty separator",
        type="functional",
        expected_error="ValueError",
    )
    out = await action_evaluate_deterministic_result(
        _eval_si(
            "F\n"
            "def test_rejects():\n"
            ">       with pytest.raises(ValueError):\n"
            "E       Failed: DID NOT RAISE <class 'ValueError'>\n"
            "1 failed\n",
            all_passed=False,
            goal=goal,
        )
    )
    assert out.result["goal_met"] is False


@pytest.mark.asyncio
async def test_deterministic_eval_should_raise_fails_on_other_error():
    """expected_error relaxes ONLY the named exception — a different real error
    alongside it (an ImportError the edit introduced) still fails the goal."""
    from agent.actions.pipeline_actions import action_evaluate_deterministic_result

    goal = GoalRecord(
        description="reject empty separator",
        type="functional",
        expected_error="ValueError",
    )
    out = await action_evaluate_deterministic_result(
        _eval_si(
            "Traceback (most recent call last):\n"
            "ImportError: cannot import name 'foo'\n"
            "ValueError: separator must not be empty\n",
            all_passed=False,
            goal=goal,
        )
    )
    assert out.result["goal_met"] is False


@pytest.mark.asyncio
async def test_deterministic_eval_normal_goal_still_fails_on_exception():
    """Regression guard: a goal with NO expected_error keeps the blanket
    behavior — a ValueError in the output fails, exactly as before."""
    from agent.actions.pipeline_actions import action_evaluate_deterministic_result

    goal = GoalRecord(description="do a thing", type="functional")  # no expected_error
    out = await action_evaluate_deterministic_result(
        _eval_si(
            "Traceback (most recent call last):\nValueError: boom\n",
            all_passed=False,
            goal=goal,
        )
    )
    assert out.result["goal_met"] is False


@pytest.mark.asyncio
async def test_gate_stale_baseline_recheck():
    # Baseline said collection was broken, but the agent fixed the blocker
    # mid-mission — the gate must re-check NOW and run the suite (not stand
    # down on the stale flag and certify a still-failing repo).
    from agent.actions.mission_actions import action_run_test_suite_gate

    goal = GoalRecord(
        description="fix it",
        type="functional",
        status="complete",
        repair_tests={
            "test_files": ["tests/test_x.py"],
            "collect_ok": False,
            "derived": True,
        },
    )
    m = _mission(goals=[goal])

    # collect-only now CLEAN, then the suite run FAILS a node → harvest.
    fx = _SeqEffects(
        [
            CommandResult(
                return_code=0, stdout="3 tests collected", stderr="", command="c"
            ),
            CommandResult(
                return_code=1,
                stdout="FAILED tests/test_x.py::test_y - Boom",
                stderr="",
                command="p",
            ),
        ],
        mission=m,
    )
    si = StepInput(
        context={"mission": m},
        inputs={},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="dispatch_test_gate"),
        effects=fx,
    )
    out = await action_run_test_suite_gate(si)
    assert m.tests_verified is False  # gate ran and harvested, not stood down
    assert out.result["harvested"] == 1


# ── Test-extras provisioning: an LLM-set env category ─────────────────


def test_project_ops_wires_test_install_leg():
    import json as _json
    import os as _os

    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    with open(_os.path.join(root, "flows", "compiled.json")) as f:
        steps = _json.load(f)["project_ops"]["steps"]
    ci = {
        r["condition"]: r["transition"]
        for r in steps["collect_installs"]["resolver"]["rules"]
    }
    assert ci["result.commands_found == true"] == "run_installs"
    assert (
        ci["true"] == "collect_test_installs"
    )  # no main installs → still check test deps
    ri = {
        r["condition"]: r["transition"]
        for r in steps["run_installs"]["resolver"]["rules"]
    }
    assert ri["context.get('all_passed') == true"] == "collect_test_installs"
    cti = steps["collect_test_installs"]
    assert cti["params"]["field"] == "test_install_command"
    rti = steps["run_test_installs"]
    assert rti["input_map"]["stop_on_error"] is False  # best-effort
    assert rti["resolver"]["rules"][0]["transition"] == "build_report_success"


@pytest.mark.asyncio
async def test_collect_env_field_picks_up_test_install_command():
    from agent.actions.pipeline_actions import action_collect_env_field

    fx = MockEffects(
        files={
            ".agent/env.json": (
                '{"py": {"install_command": ["pip", "install", "-e", "."],'
                ' "test_install_command": ["pip", "install", "-e", ".[test]", "pytest-mock"]}}'
            )
        }
    )
    si = StepInput(
        context={},
        inputs={},
        params={"field": "test_install_command", "output_key": "test_install_commands"},
        meta=FlowMeta(flow_name="project_ops", step_id="collect_test_installs"),
        effects=fx,
    )
    out = await action_collect_env_field(si)
    assert out.result["commands_found"] is True
    cmds = out.context_updates["test_install_commands"]
    assert cmds == ["pip install -e .[test] pytest-mock"]  # verbatim — no uv rewrite
