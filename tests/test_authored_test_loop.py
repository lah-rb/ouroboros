"""Authored regression tests — TDD at the point of repair.

THE INCIDENT THESE PIN. A goal's acceptance checks are derived AFTER a passing
session, from that session's transcript. Replay cannot own its preconditions,
and on the hy3 run (2026-08-05..09) that produced `test -f save.json` — an
assertion on a file the pre-session transient flush DELETES. Five goals
reopened per sweep wave; the `quit` goal reached retest_count 51, fifty-one
guided re-tests of a behaviour that never broke.

The repair: author a real test at the one moment a NEGATIVE CONTROL exists —
inside the diagnosis session, while the code is still broken — and keep it only
if four mechanical controls pass. Prompt rules alone are the lever that already
failed (rule 8 only partially fixed the fingerprint class), so every control
below is deterministic and every one of these tests is the control's alarm.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.authored_test_actions import (
    _AUTHORED_MAX_SECONDS,
    _classify,
    _parse_candidate,
    _path_error,
    _safe_command,
    action_author_regression_test,
)
from agent.actions.diagnosis_session_actions import action_gate_author_test
from agent.actions.pipeline_actions import (
    _AUTHORED_QUARANTINE_K,
    action_gate_goal_acceptance,
    action_reconcile_acceptance,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    GoalRecord,
    MissionConfig,
    MissionState,
)

ROOT = Path(__file__).resolve().parents[1]

PYTEST_PROBE = "python -m pytest --version"


@pytest.fixture(autouse=True)
def _reset_pytest_probe_cache():
    """The gate caches the pytest-availability answer per process. Tests must
    not inherit each other's answer."""
    import agent.actions.diagnosis_session_actions as dsa

    dsa._PYTEST_AVAILABLE = None
    yield
    dsa._PYTEST_AVAILABLE = None


TEST_PATH = "tests/test_quit_saves.py"
TEST_CMD = f"python -m pytest -q --no-header {TEST_PATH}"
COMPILE_CMD = f"python -m py_compile {TEST_PATH}"

RED_OUT = (
    "FAILED tests/test_quit_saves.py::test_quit_writes_save - AssertionError\n1 failed"
)
GREEN_OUT = "1 passed"


def _cmd(rc: int, out: str = "") -> CommandResult:
    return CommandResult(return_code=rc, stdout=out, stderr="", command="")


def _candidate_response(
    path: str = TEST_PATH, body: str = "def test_x():\n    assert False\n"
) -> str:
    return (
        "```json\n"
        + json.dumps(
            {
                "path": path,
                "language": "python",
                "command": f"python -m pytest -q --no-header {path}",
            }
        )
        + "\n```\n\n```python\n"
        + body
        + "```\n"
    )


def _goal(**kw) -> GoalRecord:
    base = dict(
        id="g1",
        description="quit saves the game and exits cleanly",
        type="functional",
        status="incomplete",
        interaction_mode="exploratory",
    )
    base.update(kw)
    return GoalRecord(**base)


def _mission(goal: GoalRecord | None = None, **cfg) -> MissionState:
    conf = dict(working_directory="/tmp/x")
    conf.update(cfg)
    m = MissionState(
        objective="build a game",
        status="active",
        config=MissionConfig(**conf),
        goals=[goal] if goal is not None else [],
    )
    m.architecture = ArchitectureState(transient_files=["save.json"])
    return m


def _gate_effects(mission, *, pytest_rc: int = 0) -> MockEffects:
    return MockEffects(
        commands={PYTEST_PROBE: _cmd(pytest_rc, "pytest 8.0.0")}, mission=mission
    )


def _si(effects, context=None, goal_id="g1") -> StepInput:
    return StepInput(
        context=dict(context or {}),
        inputs={"goal_id": goal_id},
        params={},
        meta=FlowMeta(flow_name="diagnose_issue", step_id="author_test"),
        effects=effects,
    )


class _FS(MockEffects):
    """MockEffects whose `rm -f` actually removes the file, so the transient
    flush and the leftover diff are measured against a real filesystem model."""

    def __init__(self, *a, creates: dict[str, str] | None = None, **kw):
        super().__init__(*a, **kw)
        # {command -> {path: content}} written as a side effect of the command.
        self._creates = dict(creates or {})

    async def run_command(self, command, working_dir=None, timeout=30):
        res = await super().run_command(
            command, working_dir=working_dir, timeout=timeout
        )
        if len(command) >= 3 and command[0] == "rm" and command[1] == "-f":
            self._files.pop(command[2], None)
            return CommandResult(
                return_code=0, stdout="", stderr="", command=" ".join(command)
            )
        for path, content in self._creates.get(" ".join(command), {}).items():
            self._files[path] = content
        return res


# ══════════════════════════════════════════════════════════════════════
# The gate — who may author, and who may not
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_gate_authors_on_a_plain_repair_round():
    m = _mission(_goal())
    fx = _gate_effects(m)
    out = await action_gate_author_test(
        _si(
            fx,
            {
                "recommended_flow": "file_ops",
                "target_file": "engine.py",
                "target_symbol": "Engine.quit",
                "root_cause": "quit never saves",
                "change_spec": "call save() before exit",
            },
        )
    )
    assert out.result["should_author"] is True
    brief = out.context_updates["author_test_brief"]
    assert brief["target_file"] == "engine.py"
    assert brief["suggested_path"].startswith("tests/test_")
    # LOAD-BEARING: the transient set travels to the prompt. Without it the
    # author has no way to know save.json is not there at test time.
    assert "save.json" in brief["transient_files"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "why,goal_kw,ctx,cfg",
    [
        ("kill switch", {}, {}, {"authored_tests": "off"}),
        (
            "repair profile — the grader owns the test",
            {},
            {},
            {"task_profile": "repair"},
        ),
        ("structural goal", {"type": "structural"}, {}, {}),
        ("already has one", {"authored_test": {"path": "tests/t.py"}}, {}, {}),
        ("attempts spent", {"authored_test_attempts": 2}, {}, {}),
        (
            "deterministic goal has no in-session rung",
            {"interaction_mode": "deterministic"},
            {},
            {},
        ),
        (
            "retest verdict — no code change coming",
            {},
            {"recommended_flow": "retest"},
            {},
        ),
        (
            "project_ops verdict — no code change coming",
            {},
            {"recommended_flow": "project_ops"},
            {},
        ),
        ("junk target", {}, {"target_file": "unknown"}, {}),
        ("no target", {}, {"target_file": ""}, {}),
    ],
)
async def test_gate_declines(why, goal_kw, ctx, cfg):
    m = _mission(_goal(**goal_kw), **cfg)
    fx = _gate_effects(m)
    base = {"recommended_flow": "file_ops", "target_file": "engine.py"}
    base.update(ctx)
    out = await action_gate_author_test(_si(fx, base))
    assert out.result["should_author"] is False, why


@pytest.mark.asyncio
async def test_gate_declines_a_warning_channel_diagnosis():
    """Warning-channel diagnoses dispatch with goal_id: "" — there is no
    acceptance rung to hang a test on."""
    m = _mission(_goal())
    fx = _gate_effects(m)
    out = await action_gate_author_test(
        _si(
            fx, {"recommended_flow": "file_ops", "target_file": "engine.py"}, goal_id=""
        )
    )
    assert out.result["should_author"] is False


@pytest.mark.asyncio
async def test_gate_declines_when_the_workspace_has_no_pytest():
    """FOUND LIVE on hy3 (2026-08-09): the artifact's own venv had no pytest,
    so every candidate would classify as BROKEN (control #1 reads pytest's
    output) and the arm would burn two in-session inferences per goal to
    guarantee nothing. We decline rather than install into a deliverable."""
    m = _mission(_goal())
    fx = _gate_effects(m, pytest_rc=1)
    out = await action_gate_author_test(
        _si(fx, {"recommended_flow": "file_ops", "target_file": "engine.py"})
    )
    assert out.result["should_author"] is False
    assert "pytest" in out.observations


@pytest.mark.asyncio
async def test_a_positive_pytest_probe_is_paid_once_per_process():
    m = _mission(_goal())
    fx = _gate_effects(m)
    for _ in range(3):
        await action_gate_author_test(
            _si(fx, {"recommended_flow": "file_ops", "target_file": "engine.py"})
        )
    probes = [
        c
        for c in fx.calls_to("run_command")
        if " ".join(c.args["command"]) == PYTEST_PROBE
    ]
    assert len(probes) == 1


@pytest.mark.asyncio
async def test_a_negative_probe_is_NOT_cached():
    """FOUND LIVE on the first gpt-oss-medium run: the agent built a real
    venv (python resolves, no pytest), the first probe failed, and the
    cached False kept the arm off for the whole mission — even after pytest
    became installable. A negative is about the workspace RIGHT NOW."""
    m = _mission(_goal())
    fx = _gate_effects(m, pytest_rc=1)
    base = {"recommended_flow": "file_ops", "target_file": "engine.py"}
    out1 = await action_gate_author_test(_si(fx, base))
    assert out1.result["should_author"] is False

    # pytest gets installed mid-run; the next gate pass must see it.
    fx._commands[PYTEST_PROBE] = _cmd(0, "pytest 8.0.0")
    out2 = await action_gate_author_test(_si(fx, base))
    assert out2.result["should_author"] is True


@pytest.mark.asyncio
async def test_the_cheap_checks_run_before_the_subprocess():
    """A goal that is ineligible anyway must not pay for a probe."""
    m = _mission(_goal(type="structural"))
    fx = _gate_effects(m)
    await action_gate_author_test(
        _si(fx, {"recommended_flow": "file_ops", "target_file": "engine.py"})
    )
    assert not fx.calls_to("run_command")


# ══════════════════════════════════════════════════════════════════════
# Control #1 — CLASSIFIED red, not merely non-zero
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "rc,out,expected,why",
    [
        (1, RED_OUT, "red", "a named assertion failure is the negative control"),
        (0, GREEN_OUT, "green", "passing against broken code is vacuous"),
        # THE KEY PIN: rc 1 with an import error is red today AND red after the
        # fix — arming it would make the goal impossible to complete, ever.
        (
            1,
            "ImportError while importing test module 'tests/t.py'",
            "broken",
            "import error",
        ),
        (1, "ERROR collecting tests/t.py\n1 error", "broken", "collection error"),
        (1, "no tests ran", "broken", "nonzero with no named failure is noise"),
        (2, "INTERNALERROR> ...", "broken", "pytest internal error"),
        (4, "ERROR: file or directory not found", "broken", "usage error"),
        (5, "no tests ran", "broken", "nothing collected"),
    ],
)
def test_red_is_classified_not_assumed(rc, out, expected, why):
    verdict, _nodes, _detail = _classify(rc, out, timed_out=False)
    assert verdict == expected, why


SELF_INFLICTED_OUT = """FF                             [100%]
=================================== FAILURES ===================================
    def _fresh_engine():
        world = load_world("world.json")
>       engine = GameEngine(world=world)
E       TypeError: GameEngine.__init__() got an unexpected keyword argument 'world'

tests/test_quit_saves.py:14: TypeError
FAILED tests/test_quit_saves.py::test_save_then_load - TypeError
2 failed"""

PRODUCT_SIDE_OUT = """F                              [100%]
=================================== FAILURES ===================================
>       return self._rooms[room_id]
E       KeyError: 'hall'

engine.py:88: KeyError
FAILED tests/test_quit_saves.py::test_save_then_load - KeyError
1 failed"""


def test_a_test_that_mis_calls_the_code_is_broken_not_red():
    """FOUND LIVE, first firing (2026-08-09). The arm authored a well-shaped
    save/load round-trip test that went red on
    `GameEngine.__init__() got an unexpected keyword argument 'world'` —
    rc 1, two named FAILED nodes, clean collection. It passed control #1 as
    originally written and would have stayed red after the fix, blocking the
    goal until the quarantine wore it down three rounds later.

    A negative control must fail on an ASSERTION, or fail in the PRODUCT."""
    verdict, _nodes, detail = _classify(
        1, SELF_INFLICTED_OUT, timed_out=False, test_path="tests/test_quit_saves.py"
    )
    assert verdict == "broken"
    assert "TypeError" in detail


def test_a_failure_raised_in_product_code_is_still_red():
    """The fix CAN turn this one green — it is a real negative control."""
    verdict, _nodes, _d = _classify(
        1, PRODUCT_SIDE_OUT, timed_out=False, test_path="tests/test_quit_saves.py"
    )
    assert verdict == "red"


def test_an_assertion_failure_in_the_test_is_red():
    out = (
        "FAILED tests/test_quit_saves.py::test_x - AssertionError\n"
        "tests/test_quit_saves.py:40: AssertionError\n1 failed"
    )
    verdict, _nodes, _d = _classify(
        1, out, timed_out=False, test_path="tests/test_quit_saves.py"
    )
    assert verdict == "red"


def test_a_timeout_is_broken_not_red():
    assert _classify(1, RED_OUT, timed_out=True)[0] == "broken"


# ══════════════════════════════════════════════════════════════════════
# Path + command hygiene — a stored check re-runs unattended forever
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "path",
    [
        "/etc/test_x.py",
        "../test_x.py",
        "tests/../../test_x.py",
        "engine.py",
        "tests/helpers.py",
        "tests/test_x.txt",
        "",
    ],
)
def test_bad_paths_are_refused(path):
    assert _path_error(path) is not None


def test_a_fresh_pytest_file_under_tests_is_accepted():
    assert _path_error("tests/test_quit.py") is None


@pytest.mark.parametrize(
    "cmd",
    [
        "python -m pytest -q tests/test_x.py; rm -rf /",
        "python -m pytest tests/test_x.py || true",
        "python -m pytest tests/test_x.py > /dev/null",
        "python tests/test_x.py",  # not pytest — red is unclassifiable
        "python -m pytest tests/other.py",  # not this file
        "",
    ],
)
def test_unsafe_or_wrong_commands_fall_back_to_the_canonical_one(cmd):
    assert _safe_command(cmd, "tests/test_x.py") == (
        "python -m pytest -q --no-header tests/test_x.py"
    )


def test_a_clean_model_command_survives():
    cmd = "python -m pytest -q tests/test_x.py"
    assert _safe_command(cmd, "tests/test_x.py") == cmd


def test_the_body_may_arrive_inline_or_fenced():
    inline = _parse_candidate(
        json.dumps({"path": "tests/test_x.py", "content": "assert 1"})
    )
    assert inline["content"] == "assert 1"
    fenced = _parse_candidate(_candidate_response(body="def test_y():\n    assert 0\n"))
    assert "def test_y" in fenced["content"]
    assert fenced["path"] == TEST_PATH


# ══════════════════════════════════════════════════════════════════════
# The probe end to end — arm, or drop and leave the fix untouched
# ══════════════════════════════════════════════════════════════════════


def _probe_effects(run_results, mission, *, creates=None, compile_rc=0):
    """Effects whose pytest command returns `run_results` in order."""
    seq = list(run_results)

    class _P(_FS):
        async def run_command(self, command, working_dir=None, timeout=30):
            # super() first so the call is RECORDED (the flush-ordering test
            # reads the call log), then substitute the sequenced verdict.
            res = await super().run_command(
                command, working_dir=working_dir, timeout=timeout
            )
            if " ".join(command) == "/bin/sh -c " + TEST_CMD and seq:
                rc, out = seq.pop(0)
                for path, content in (creates or {}).items():
                    self._files[path] = content
                return CommandResult(
                    return_code=rc, stdout=out, stderr="", command=TEST_CMD
                )
            return res

    return _P(
        files={"save.json": "{}"},
        commands={COMPILE_CMD: _cmd(compile_rc)},
        inference_responses=[_candidate_response()],
        mission=mission,
    )


@pytest.mark.asyncio
async def test_a_red_candidate_is_armed_and_the_derived_checks_are_demoted():
    """THE CURE for the save.json reopen class: `required: False` stops the
    replay check vetoing completion AND drops it out of the regression sweep,
    which filters on `required` in both directions. Nothing is deleted."""
    goal = _goal(
        acceptance_checks=[
            {"command": "test -f save.json", "name": "save exists", "required": True}
        ],
        acceptance_grounded=True,
    )
    m = _mission(goal)
    fx = _probe_effects([(1, RED_OUT), (1, RED_OUT)], m)

    out = await action_author_regression_test(
        _si(
            fx,
            {
                "diagnosis_session_id": "s1",
                "author_test_brief": {
                    "goal_description": goal.description,
                    "target_file": "engine.py",
                    "suggested_path": TEST_PATH,
                    "transient_files": ["save.json"],
                },
            },
        )
    )

    assert out.result["authored"] is True
    assert goal.authored_test["path"] == TEST_PATH
    assert goal.acceptance_checks[0]["source"] == "authored"
    assert goal.acceptance_checks[0]["required"] is True
    stale = goal.acceptance_checks[1]
    assert stale["command"] == "test -f save.json"
    assert stale["required"] is False, "the replay check must stop vetoing"
    assert (
        len(goal.acceptance_checks) == 2
    ), "nothing is deleted — history stays readable"


@pytest.mark.asyncio
async def test_the_workspace_is_flushed_cold_before_the_probe():
    """Control #2: the red must be measured from the floor the acceptance rung
    will later use, not from whatever the last session left behind."""
    goal = _goal()
    m = _mission(goal)
    fx = _probe_effects([(1, RED_OUT), (1, RED_OUT)], m)

    await action_author_regression_test(
        _si(
            fx,
            {
                "diagnosis_session_id": "s1",
                "author_test_brief": {
                    "suggested_path": TEST_PATH,
                    "transient_files": ["save.json"],
                },
            },
        )
    )

    cmds = [" ".join(c.args["command"]) for c in fx.calls_to("run_command")]
    assert "rm -f save.json" in cmds
    assert cmds.index("rm -f save.json") < cmds.index("/bin/sh -c " + TEST_CMD)


@pytest.mark.asyncio
async def test_an_order_dependent_candidate_is_dropped():
    """Control #3: run 1 is cold, run 2 inherits run 1's leftovers. A verdict
    that changes between them is leftover-state noise, not a defect signal."""
    goal = _goal()
    m = _mission(goal)
    fx = _probe_effects([(1, RED_OUT), (0, GREEN_OUT)], m)

    out = await action_author_regression_test(
        _si(
            fx,
            {
                "diagnosis_session_id": "s1",
                "author_test_brief": {"suggested_path": TEST_PATH},
            },
        )
    )

    assert out.result["authored"] is False
    assert "order-dependent" in out.result["reason"]
    assert not goal.authored_test
    assert goal.authored_test_attempts == 1


@pytest.mark.asyncio
async def test_a_test_that_leaves_files_behind_is_dropped_and_deleted():
    """Control #4: it would poison every later session's cold floor."""
    goal = _goal()
    m = _mission(goal)
    fx = _probe_effects([(1, RED_OUT), (1, RED_OUT)], m, creates={"scratch.db": "x"})

    out = await action_author_regression_test(
        _si(
            fx,
            {
                "diagnosis_session_id": "s1",
                "author_test_brief": {"suggested_path": TEST_PATH},
            },
        )
    )

    assert out.result["authored"] is False
    assert "leaves files behind" in out.result["reason"]
    assert (
        TEST_PATH not in fx._files
    ), "the candidate must be removed from the workspace"


@pytest.mark.asyncio
async def test_an_uncompilable_candidate_is_dropped_before_it_ever_runs():
    """An erroring test is red before the fix AND after it — arming one would
    veto correct code forever."""
    goal = _goal()
    m = _mission(goal)
    fx = _probe_effects([(1, RED_OUT), (1, RED_OUT)], m, compile_rc=1)

    out = await action_author_regression_test(
        _si(
            fx,
            {
                "diagnosis_session_id": "s1",
                "author_test_brief": {"suggested_path": TEST_PATH},
            },
        )
    )

    assert out.result["authored"] is False
    assert "compile" in out.result["reason"]
    assert TEST_PATH not in fx._files


@pytest.mark.asyncio
async def test_a_vacuous_candidate_gets_one_repair_turn_then_is_dropped():
    goal = _goal()
    m = _mission(goal)
    fx = _probe_effects(
        [(0, GREEN_OUT), (0, GREEN_OUT), (0, GREEN_OUT), (0, GREEN_OUT)], m
    )
    fx._inference_responses = [_candidate_response(), _candidate_response()]

    out = await action_author_regression_test(
        _si(
            fx,
            {
                "diagnosis_session_id": "s1",
                "author_test_brief": {"suggested_path": TEST_PATH},
            },
        )
    )

    assert out.result["authored"] is False
    assert "vacuous" in out.result["reason"]
    assert fx.call_count("session_inference") == 2, "exactly one bounded repair turn"


@pytest.mark.asyncio
async def test_a_mis_calling_candidate_gets_one_repair_turn_and_can_recover():
    """The live miss was a wrong constructor keyword — the traceback names it
    and the true signature is already in the session's KV, so throwing the
    whole session away over it is the expensive answer to a cheap mistake."""
    goal = _goal()
    m = _mission(goal)
    fx = _probe_effects(
        [
            (1, SELF_INFLICTED_OUT),
            (1, SELF_INFLICTED_OUT),
            (1, RED_OUT),
            (1, RED_OUT),
        ],
        m,
    )
    fx._inference_responses = [_candidate_response(), _candidate_response()]

    out = await action_author_regression_test(
        _si(
            fx,
            {
                "diagnosis_session_id": "s1",
                "author_test_brief": {"suggested_path": TEST_PATH},
            },
        )
    )

    assert out.result["authored"] is True, out.result["reason"]
    assert fx.call_count("session_inference") == 2, "exactly one bounded repair turn"
    # The FIX prompt was used, not the vacuous-test one (the mock records only
    # the first 100 chars, which is enough to tell them apart).
    second = fx.calls_to("session_inference")[1].args["prompt"]
    assert second.startswith("Your test failed, but for the WRONG REASON")


def test_the_repair_turn_carries_the_real_traceback():
    """It is the only thing that can correct a wrong signature — a bare
    "you got it wrong" turn would just re-guess."""
    from agent.actions.authored_test_actions import _render_fix_prompt

    text = _render_fix_prompt("TypeError raised inside the test", SELF_INFLICTED_OUT)
    assert "unexpected keyword argument" in text
    assert "GameEngine(world=world)" in text
    assert "{failure_output}" not in text and "{failure_reason}" not in text


@pytest.mark.asyncio
async def test_a_candidate_that_mis_calls_twice_is_dropped():
    goal = _goal()
    m = _mission(goal)
    fx = _probe_effects([(1, SELF_INFLICTED_OUT)] * 4, m)
    fx._inference_responses = [_candidate_response(), _candidate_response()]

    out = await action_author_regression_test(
        _si(
            fx,
            {
                "diagnosis_session_id": "s1",
                "author_test_brief": {"suggested_path": TEST_PATH},
            },
        )
    )

    assert out.result["authored"] is False
    assert "TypeError" in out.result["reason"]
    assert TEST_PATH not in fx._files


@pytest.mark.asyncio
async def test_a_slow_candidate_is_dropped(monkeypatch):
    goal = _goal()
    m = _mission(goal)
    fx = _probe_effects([(1, RED_OUT), (1, RED_OUT)], m)
    # Each _run_once reads the clock twice; the first run is over budget.
    ticks = iter([0.0, _AUTHORED_MAX_SECONDS + 5.0, 0.0, 1.0])
    last = [0.0]

    def _clock():
        last[0] = next(ticks, last[0])
        return last[0]

    monkeypatch.setattr("agent.actions.authored_test_actions.time.monotonic", _clock)

    out = await action_author_regression_test(
        _si(
            fx,
            {
                "diagnosis_session_id": "s1",
                "author_test_brief": {"suggested_path": TEST_PATH},
            },
        )
    )

    assert out.result["authored"] is False
    assert "too slow" in out.result["reason"]


@pytest.mark.asyncio
async def test_every_drop_path_leaves_the_fix_dispatchable():
    """THE INVARIANT. The arm has one exit; nothing it publishes may perturb
    the diagnosis the dispatcher is about to act on."""
    goal = _goal()
    m = _mission(goal)
    fx = _probe_effects([(1, RED_OUT), (0, GREEN_OUT)], m)

    out = await action_author_regression_test(
        _si(
            fx,
            {
                "diagnosis_session_id": "s1",
                "author_test_brief": {"suggested_path": TEST_PATH},
            },
        )
    )

    assert out.context_updates in (None, {}), "the arm must publish nothing"


@pytest.mark.asyncio
async def test_an_unusable_brief_is_a_pass_through():
    fx = MockEffects()
    out = await action_author_regression_test(_si(fx, {"diagnosis_session_id": ""}))
    assert out.result["authored"] is False


# ══════════════════════════════════════════════════════════════════════
# Precedence + the disarm exemption
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_an_authored_test_suppresses_derivation_even_when_ungrounded():
    """Gated on authored_test, NOT acceptance_grounded: reconcile RESETS
    grounded on any disarm, so an unrelated check wearing out would otherwise
    re-arm derivation on a goal that already has a real test."""
    goal = _goal(authored_test={"path": TEST_PATH}, acceptance_grounded=False)
    m = _mission(goal)
    fx = MockEffects(mission=m)
    out = await action_gate_goal_acceptance(_si(fx))
    assert out.result["needs_derive"] is False


@pytest.mark.asyncio
async def test_a_goal_without_an_authored_test_still_derives():
    m = _mission(_goal(acceptance_grounded=False))
    fx = MockEffects(mission=m)
    out = await action_gate_goal_acceptance(_si(fx))
    assert out.result["needs_derive"] is True


def _reconcile_si(mission, effects, command):
    from agent.actions.check_result import check_result

    return StepInput(
        context={
            "mission": mission,
            "validation_results": [
                check_result(
                    "authored regression test",
                    ["/bin/sh", "-c", command],
                    False,
                    required=True,
                    return_code=1,
                )
            ],
        },
        inputs={"goal_id": "g1"},
        params={},
        meta=FlowMeta(flow_name="interact", step_id="reconcile_acceptance"),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_an_authored_test_is_never_disarmed_only_quarantined():
    """Disarm exists to wear out mis-grounded REPLAY checks. Deleting the one
    check with a verified negative control — after two contradicting LLM
    verdicts — would silently destroy the goal's only ground truth."""
    check = {
        "command": TEST_CMD,
        "name": "authored regression test",
        "required": True,
        "source": "authored",
    }
    goal = _goal(
        acceptance_checks=[check],
        authored_test={"path": TEST_PATH, "command": TEST_CMD},
    )
    m = _mission(goal)
    fx = MockEffects(mission=m)

    for round_no in range(1, _AUTHORED_QUARANTINE_K + 1):
        out = await action_reconcile_acceptance(_reconcile_si(m, fx, TEST_CMD))
        assert any(
            c.get("source") == "authored" for c in goal.acceptance_checks
        ), f"round {round_no}: the authored test must never be removed"

    quarantined = goal.acceptance_checks[0]
    assert quarantined["required"] is False, "demoted to advisory, not deleted"
    assert out.result["now_ok"] is True, "an advisory check may not hold the veto"
    warn = [w for w in m.pending_warnings if w.kind == "authored_test_contradicted"]
    assert warn, "the dispute must reach a reader through the warning channel"


@pytest.mark.asyncio
async def test_a_derived_check_is_still_disarmed_as_before():
    """The exemption must not weaken the wear-out path it sits beside."""
    cmd = "test -f save.json"
    goal = _goal(
        acceptance_checks=[{"command": cmd, "name": "save", "required": True}],
        acceptance_grounded=True,
    )
    m = _mission(goal)
    fx = MockEffects(mission=m)

    from agent.actions.pipeline_actions import _ACCEPTANCE_DISARM_K

    for _ in range(_ACCEPTANCE_DISARM_K):
        await action_reconcile_acceptance(_reconcile_si(m, fx, cmd))
    assert goal.acceptance_checks == []


@pytest.mark.asyncio
async def test_a_disarm_does_not_re_arm_derivation_on_a_tested_goal():
    cmd = "test -f save.json"
    goal = _goal(
        acceptance_checks=[
            {
                "command": TEST_CMD,
                "name": "authored",
                "required": True,
                "source": "authored",
            },
            {"command": cmd, "name": "save", "required": False},
        ],
        authored_test={"path": TEST_PATH},
        acceptance_grounded=True,
    )
    m = _mission(goal)
    fx = MockEffects(mission=m)

    from agent.actions.pipeline_actions import _ACCEPTANCE_DISARM_K

    for _ in range(_ACCEPTANCE_DISARM_K):
        out = await action_reconcile_acceptance(_reconcile_si(m, fx, cmd))
    assert "acceptance_needs_derive" not in (out.context_updates or {})


# ══════════════════════════════════════════════════════════════════════
# The regression sweep honours a stored timeout
# ══════════════════════════════════════════════════════════════════════


def test_the_sweep_honours_a_stored_timeout_within_the_cap():
    from agent.actions.mission_actions import (
        _REGRESSION_CHECK_TIMEOUT,
        _REGRESSION_CHECK_TIMEOUT_CAP,
        _check_timeout,
    )

    assert _check_timeout({}) == _REGRESSION_CHECK_TIMEOUT
    assert _check_timeout({"timeout": 60}) == 60
    assert _check_timeout({"timeout": 5}) == _REGRESSION_CHECK_TIMEOUT, "never shorter"
    assert _check_timeout({"timeout": 9999}) == _REGRESSION_CHECK_TIMEOUT_CAP
    assert _check_timeout({"timeout": "nonsense"}) == _REGRESSION_CHECK_TIMEOUT


# ══════════════════════════════════════════════════════════════════════
# Compiled-graph pins — the safety argument is graph structure
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def diagnose():
    return json.loads((ROOT / "flows" / "compiled.json").read_text())["diagnose_issue"]


class TestTheArmCanNeverBlockTheFix:
    def test_systemic_scan_routes_into_the_gate(self, diagnose):
        rules = diagnose["steps"]["systemic_scan"]["resolver"]["rules"]
        assert [r["transition"] for r in rules] == ["author_test_gate"]

    def test_the_gate_falls_through_to_end_session(self, diagnose):
        rules = diagnose["steps"]["author_test_gate"]["resolver"]["rules"]
        assert rules[-1]["condition"] == "true"
        assert rules[-1]["transition"] == "end_session"

    def test_author_test_has_exactly_one_exit(self, diagnose):
        """THE INVARIANT: no path through the author arm can delay, redirect
        or block the fix dispatch."""
        rules = diagnose["steps"]["author_test"]["resolver"]["rules"]
        assert len(rules) == 1
        assert rules[0]["condition"] == "true"
        assert rules[0]["transition"] == "end_session"

    def test_the_arm_publishes_nothing(self, diagnose):
        """In particular not files_changed — a test file is not a product edit
        and must not arm regression_dirty."""
        assert not (diagnose["steps"]["author_test"].get("publishes") or [])

    def test_both_actions_are_registered(self):
        from agent.actions.registry import build_action_registry

        registry = build_action_registry()
        assert registry.has("gate_author_test")
        assert registry.has("author_regression_test")


class TestThePromptCarriesTheLessons:
    def test_it_loads_and_names_the_output_contract(self):
        from agent.loader import load_prompt_text

        text = load_prompt_text("diagnose/author_test")
        for key in ("path", "language", "command", "python"):
            assert key in text
        assert "{transient_files}" in text, "the transient list must be substitutable"
        assert "{root_cause}" in text

    def test_the_repair_turn_prompt_exists(self):
        from agent.loader import load_prompt_text

        assert "PASSED" in load_prompt_text("diagnose/author_test_repair")

    def test_the_derive_prompt_forbids_runtime_file_assertions(self):
        """Rule 9 — the companion fix for goals that never get an authored
        test. The example that taught `test -f save.json` is gone."""
        text = (
            ROOT / "prompts" / "interact" / "derive_goal_acceptance.yaml"
        ).read_text()
        assert "NEVER ASSERT ON A FILE THE PROGRAM WRITES AT RUNTIME" in text
        assert "saves/slot1.json" not in text, "the ✅ example taught the trap"


@pytest.mark.asyncio
async def test_a_menu_shape_answer_gets_the_format_correction_turn():
    """THE 779 CLASS, THIRD SIGHTING (first gpt-oss-medium authoring turn):
    the session's every prior turn was menu JSON, and the model answered the
    authoring turn with {"choice": "trace", ...}. A missed contract, not a
    bad test — the bounded repair turn corrects the shape and keeps the
    attempt instead of burning it on 'no path'."""
    goal = _goal()
    m = _mission(goal)
    fx = _probe_effects([(1, RED_OUT), (1, RED_OUT)], m)
    fx._inference_responses = [
        '{"choice": "trace", "symbol_ref": "game.py:GameEngine.examine"}',
        _candidate_response(),
    ]

    out = await action_author_regression_test(
        _si(
            fx,
            {
                "diagnosis_session_id": "s1",
                "author_test_brief": {"suggested_path": TEST_PATH},
            },
        )
    )

    assert out.result["authored"] is True, out.result["reason"]
    second = fx.calls_to("session_inference")[1].args["prompt"]
    assert second.startswith("That was a menu answer")


@pytest.mark.asyncio
async def test_a_menu_shape_answer_twice_is_dropped():
    goal = _goal()
    m = _mission(goal)
    fx = _probe_effects([], m)
    fx._inference_responses = [
        '{"choice": "trace", "symbol_ref": "a.py:f"}',
        '{"choice": "conclude"}',
    ]

    out = await action_author_regression_test(
        _si(
            fx,
            {
                "diagnosis_session_id": "s1",
                "author_test_brief": {"suggested_path": TEST_PATH},
            },
        )
    )

    assert out.result["authored"] is False
    assert goal.authored_test_attempts == 1


def test_the_parser_accepts_a_tagless_fence_and_a_bare_json_header():
    """SECOND LIVE gpt-oss ATTEMPT: header as bare (unfenced) JSON, body in
    a ``` fence with no language tag — a well-shaped test dropped as
    'empty test body'. The parser must take what models actually emit."""
    raw = (
        '{\n  "path": "tests/test_heal.py",\n  "language": "python",\n'
        '  "command": "python -m pytest -q --no-header tests/test_heal.py"\n}\n'
        "```\n"
        "import pytest\n\ndef test_heal():\n    assert False\n"
        "```\n"
    )
    cand = _parse_candidate(raw)
    assert cand["path"] == "tests/test_heal.py"
    assert "def test_heal" in cand["content"]


def test_the_parser_never_mistakes_the_json_header_for_the_test_body():
    raw = (
        "```json\n"
        '{"path": "tests/test_x.py", "command": "python -m pytest tests/test_x.py"}\n'
        "```\n"
    )
    cand = _parse_candidate(raw)
    assert cand["path"] == "tests/test_x.py"
    assert cand["content"] == ""
