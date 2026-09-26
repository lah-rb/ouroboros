"""Phase 4 step 1 of the whole-if-it-fits rule (operator-approved
2026-09-26): list caps that silently changed what the agent did are gone or
report themselves.

- the test gate runs every goal's own test files (was the first three of the
  union — a fourth goal's files were certified unrun);
- the already-rewritten block falls back to a skeleton (every symbol named
  with its signatures, newest bodies whole), never a tail cut;
- a validation strategy's checks past the limit are reported, not dropped;
- the identifier tier of repair terms is whole.
"""

from __future__ import annotations

import pytest

from agent import context_fit as cf
from agent.actions.mission_actions import action_run_test_suite_gate
from agent.actions.pipeline_actions import extract_repair_terms_tiered
from agent.actions.refinement_actions import action_run_validation_checks
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.formatters import _format_already_rewritten
from agent.models import FlowMeta, StepInput
from agent.persistence.models import GoalRecord, MissionConfig, MissionState

# ── the test gate runs every goal's files ───────────────────────────────


class _PerCommand(MockEffects):
    """Answers each pytest run by the files it names, and records them."""

    def __init__(self, failing: dict[str, str], **kw):
        super().__init__(**kw)
        self._failing = failing  # file -> the node that fails in it
        self.runs: list[str] = []

    async def run_command(self, command, **kw):
        if command and command[0] == "/bin/sh":
            cmd = command[-1]
            self.runs.append(cmd)
            bad = [node for f, node in self._failing.items() if f in cmd.split()]
            out = "".join(f"FAILED {n} - AssertionError\n" for n in bad)
            return CommandResult(
                return_code=1 if bad else 0,
                stdout=out or "4 passed",
                stderr="",
                command=cmd,
            )
        return await super().run_command(command, **kw)


def _goal(files: list[str]) -> GoalRecord:
    return GoalRecord(
        description="fix it",
        type="functional",
        status="complete",
        repair_tests={"test_files": files, "collect_ok": True, "derived": True},
    )


def _gate_input(mission, effects) -> StepInput:
    return StepInput(
        context={"mission": mission},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="dispatch_test_gate"),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_the_gate_runs_every_goals_files_in_its_own_batch():
    goals = [
        _goal(["tests/test_a.py", "tests/test_b.py"]),
        _goal(["tests/test_c.py", "tests/test_d.py"]),
        _goal(["tests/test_b.py", "tests/test_e.py"]),  # b already ran
    ]
    m = MissionState(
        objective="o",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", task_profile="repair"),
        goals=goals,
    )
    # Only the FOURTH file fails — the old `test_files[:3]` never ran it and
    # certified the suite.
    fx = _PerCommand({"tests/test_d.py": "tests/test_d.py::test_x"}, mission=m)
    out = await action_run_test_suite_gate(_gate_input(m, fx))
    assert fx.runs == [
        "python -m pytest -q --no-header tests/test_a.py tests/test_b.py",
        "python -m pytest -q --no-header tests/test_c.py tests/test_d.py",
        "python -m pytest -q --no-header tests/test_e.py",
    ]
    assert m.tests_verified is False
    assert out.result["harvested"] == 1
    assert any(
        g.finding_signature == "test-gate:tests/test_d.py::test_x" for g in m.goals
    )


@pytest.mark.asyncio
async def test_a_clean_suite_across_batches_certifies():
    m = MissionState(
        objective="o",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", task_profile="repair"),
        goals=[_goal(["tests/test_a.py"]), _goal(["tests/test_b.py"])],
    )
    fx = _PerCommand({}, mission=m)
    out = await action_run_test_suite_gate(_gate_input(m, fx))
    assert len(fx.runs) == 2
    assert m.tests_verified is True and "suite passed (2 file(s))" in out.observations


# ── the already-rewritten block: skeleton, not a tail cut ───────────────


def _method(name: str, body_lines: int) -> str:
    pad = "\n".join(f"        step_{i} = {i}" for i in range(body_lines))
    return (
        f"    def {name}(self, target: str, quiet: bool = False) -> bool:\n"
        f'        """{name} docstring."""\n{pad}\n        return True\n'
    )


def test_every_rewritten_symbol_is_named_with_its_signature(monkeypatch):
    monkeypatch.setattr(cf, "_last_window", 4000)  # share ≈ 3,000 chars
    already = {
        "engine.py:Engine.first": _method("first", 60),  # oldest, ~1.3k chars
        "engine.py:Engine.second": _method("second", 60),
        "engine.py:Engine.third": _method("third", 60),  # newest
    }
    out = _format_already_rewritten({}, {"context": {"already_rewritten": already}})
    # Every symbol appears, in order, with its signature.
    for name in ("first", "second", "third"):
        assert f"### engine.py:Engine.{name}" in out
        assert f"def {name}(self, target: str, quiet: bool = False)" in out
    # The newest bodies are whole; the oldest is its outline — never cut away.
    assert "step_59 = 59" in out.split("### engine.py:Engine.third")[1]
    first = out.split("### engine.py:Engine.first")[1].split("###")[0]
    assert "outline only" in first and "step_1 = 1" not in first


def test_small_batches_render_whole(monkeypatch):
    monkeypatch.setattr(cf, "_last_window", 262144)
    already = {"a.py:f": "def f(x):\n    return x + 1\n"}
    out = _format_already_rewritten({}, {"context": {"already_rewritten": already}})
    assert "return x + 1" in out and "outline only" not in out


# ── dropped validation checks are reported ──────────────────────────────


@pytest.mark.asyncio
async def test_checks_past_the_limit_are_reported():
    strategy = (
        '{"checks": ['
        + ", ".join(f'{{"name": "c{i}", "command": "true {i}"}}' for i in range(7))
        + "]}"
    )
    fx = MockEffects(
        commands={
            "true": CommandResult(return_code=0, stdout="", stderr="", command="true")
        }
    )
    out = await action_run_validation_checks(
        StepInput(
            context={"validation_strategy": strategy},
            params={"max_checks": 5},
            meta=FlowMeta(flow_name="ops_task", step_id="run_checks"),
            effects=fx,
        )
    )
    assert out.result["checks_run"] == 5
    assert out.result["checks_not_run"] == 2
    assert "2 proposed check(s) past the limit of 5 not run: c5; c6" in out.observations


# ── repair terms ────────────────────────────────────────────────────────


def test_every_identifier_is_a_repair_term():
    idents = [f"Widget{i}Handler" for i in range(20)]
    strong, _weak = extract_repair_terms_tiered(" ".join(idents))
    assert strong == idents  # not the first 12
