"""Regression: a file_ops/structural DirectiveReport must record the gate's
failure text so the downstream re-diagnose can see the live error.

Motivating incident (proven cache-independent via a controlled cacheON/cacheOFF
resume A/B): the gate (run_checks) captures each check's error on the check dict,
but the report step is threaded ONLY ``validation_results`` (the structured check
list) — NOT the formatted ``validation_output`` string, and not ``terminal_output``
(a key only the interact flow's program run sets). So a file_ops/structural
failure report landed with ``terminal_output=''``; the re-diagnose that reads
``last_report.terminal_output`` got an empty ``error_output``; the diagnose seed
silently dropped its ``## What crashed`` / ``## Transcript`` sections; and the
model fixated on a stale prior-attempt headline rather than the current failure.

These lock in the reconstruction: when no explicit ``terminal_output`` exists,
the report rebuilds the failure text from the failed checks' stderr/stdout in
``validation_results`` — and an explicit ``terminal_output`` (the interact
program run) is never clobbered by it.
"""

from __future__ import annotations

import pytest

from agent.actions.reporting_actions import action_compile_directive_report
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

# The exact shape run_checks publishes: a list of check dicts, each carrying the
# captured stderr/stdout for that check (pipeline_actions.run_checks).
IMPORT_ERR = (
    "Traceback (most recent call last):\n"
    "    from src import GameState\n"
    "ImportError: cannot import name 'GameState' from 'src'"
)
VALIDATION_RESULTS_FAILED = [
    {"name": "syntax: src/engine.py", "passed": True, "tier": "syntax",
     "stdout": "", "stderr": ""},
    {"name": "import: src/engine.py", "passed": False, "tier": "import",
     "stdout": "", "stderr": IMPORT_ERR},
]


def _step_input(effects: MockEffects, flow_name: str, status: str, **ctx) -> StepInput:
    return StepInput(
        context=dict(ctx),
        params={"flow_name": flow_name, "status": status},
        meta=FlowMeta(flow_name=flow_name, step_id="compile_report", attempt=1),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_gate_output_reaches_report_terminal_output():
    """file_ops failure report with empty terminal_output must reconstruct the
    live error from validation_results so the re-diagnose can render it."""
    effects = MockEffects()
    si = _step_input(
        effects, "file_ops", "failed",
        validation_results=VALIDATION_RESULTS_FAILED,
    )
    out = await action_compile_directive_report(si)
    report = out.context_updates["directive_report"]
    assert "ImportError" in report["terminal_output"]
    assert "GameState" in report["terminal_output"]
    # the passing check contributes no error block
    assert "[FAIL] import: src/engine.py" in report["terminal_output"]
    assert "[FAIL] syntax" not in report["terminal_output"]


@pytest.mark.asyncio
async def test_explicit_terminal_output_not_clobbered_by_validation():
    """An interact report that already captured the program's terminal output
    must keep it — the reconstruction only fills the empty case."""
    effects = MockEffects()
    program_output = "You are in a dark room.\nTypeError: real runtime crash"
    si = _step_input(
        effects, "interact", "failed",
        terminal_output=program_output,
        validation_results=VALIDATION_RESULTS_FAILED,
    )
    out = await action_compile_directive_report(si)
    report = out.context_updates["directive_report"]
    assert report["terminal_output"] == program_output
    assert "ImportError" not in report["terminal_output"]


@pytest.mark.asyncio
async def test_no_output_anywhere_stays_empty():
    """Neither key present → empty terminal_output (no spurious content)."""
    effects = MockEffects()
    si = _step_input(effects, "file_ops", "success")
    out = await action_compile_directive_report(si)
    report = out.context_updates["directive_report"]
    assert report["terminal_output"] == ""
