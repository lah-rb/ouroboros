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
    # "resolved" is the escalation's own claim about its own work — the exact
    # class of claim that produced this trap — so it is re-verified rather than
    # taken on trust. See test_project_env_verification.py for the loop's
    # termination guarantee.
    assert rules[0]["transition"] == "verify_env_after_escalation"
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


# ── The dead setup-commands step ──────────────────────────────────────


def test_project_ops_no_longer_has_the_dead_setup_step(flows):
    """`run_setup_commands` ran `execute_project_setup`, which JSON-parses
    `inference_response` for `setup_actions` — while `plan_setup` declares
    response_shape "code" and demands `# === FILE: path ===` fences. The
    contracts never met, so it logged "Could not parse setup plan" and ran ZERO
    commands, and its resolver discarded even that."""
    assert "run_setup_commands" not in flows["project_ops"]["steps"]


def test_write_files_routes_straight_to_env_detection(flows):
    rules = flows["project_ops"]["steps"]["write_files"]["resolver"]["rules"]
    assert [r["transition"] for r in rules] == ["detect_env"]


def test_no_dangling_transitions_in_project_ops(flows):
    steps = flows["project_ops"]["steps"]
    targets = {
        r["transition"]
        for s in steps.values()
        for r in s.get("resolver", {}).get("rules", [])
    }
    assert not (targets - set(steps)), "every transition must name a real step"


def test_execute_project_setup_still_used_correctly_by_ops(flows):
    """The ACTION is fine — only the code_core wiring was wrong. ops feeds it
    prompts/ops/plan_provision.yaml, which does emit {"setup_actions": [...]}."""
    ops = flows["ops_task"]["steps"]
    assert any(s.get("action") == "execute_project_setup" for s in ops.values())
