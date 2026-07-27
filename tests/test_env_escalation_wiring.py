"""project_ops escalates instead of dead-ending when dependency installs fail.

`escalate` is a bounded read/run/write REACT loop — the only fix path in
code_core that can RUN a command. It shipped with exactly one caller
(file_ops.self_correct) while OPEN_TASKS §2 recorded wiring it to the stalled
fix-loop as the outstanding fix.

Without it, a failed install went straight to build_report_failure → the
functional sweep → diagnose_issue, whose entire action space is "trace a
symbol", and back to project_ops, whose planner emits config FILES. An
environment defect (a package declared but not installed) had no expressible
remedy anywhere on that route: one run re-derived the same CORRECT root cause 26
times and wrote a fix script 22 times that nothing ever executed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

COMPILED = Path(__file__).resolve().parents[1] / "flows" / "compiled.json"


@pytest.fixture(scope="module")
def flows():
    return json.loads(COMPILED.read_text())


def test_failed_install_routes_to_escalate_not_a_dead_end(flows):
    rules = flows["project_ops"]["steps"]["run_installs"]["resolver"]["rules"]
    transitions = [r["transition"] for r in rules]
    assert transitions[-1] == "escalate_env", (
        "a failed install must escalate, not report failure directly — "
        f"got {transitions}"
    )


def test_escalate_env_invokes_the_escalate_flow(flows):
    step = flows["project_ops"]["steps"]["escalate_env"]
    assert step["action"] == "flow", "escalate must be a SUB-flow call"
    assert step["flow"] == "escalate"


def test_escalate_env_supplies_both_required_inputs(flows):
    im = flows["project_ops"]["steps"]["escalate_env"]["input_map"]
    assert "failure_evidence" in im
    assert "expected_outcome" in im
    # The evidence must be the actual install output, not a fixed string.
    assert im["failure_evidence"].get("$ref") == "context.terminal_output"


def test_escalate_env_branches_on_resolved_vs_deferred(flows):
    rules = flows["project_ops"]["steps"]["escalate_env"]["resolver"]["rules"]
    assert rules[0]["condition"] == "result.status == 'resolved'"
    assert rules[0]["transition"] == "collect_test_installs", "resolved → continue"
    assert rules[-1]["transition"] == "build_report_failure", "deferred → honest failure"


def test_run_installs_publishes_the_evidence_escalate_consumes(flows):
    # escalate_env reads context.terminal_output; run_installs must publish it,
    # or the escalation would open with an empty brief.
    assert "terminal_output" in flows["project_ops"]["steps"]["run_installs"]["publishes"]


def test_escalate_is_a_subflow_not_tail_callable(flows):
    """Why escalate is invoked as a sub-flow rather than dispatched.

    Its terminals are `terminal: true` with a status and it does NOT tail-call
    back to mission_control. Tail-calling it would run the loop and then halt
    the mission.
    """
    esc = flows["escalate"]["steps"]
    terminals = [s for s in esc.values() if s.get("terminal")]
    assert terminals, "escalate must have terminal steps"
    assert any(s.get("status") == "resolved" for s in terminals)
    assert any(s.get("status") == "deferred" for s in terminals)
    assert not any("tail_call" in s for s in terminals), (
        "escalate terminals must not tail-call — it returns to its invoker"
    )
