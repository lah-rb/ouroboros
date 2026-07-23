"""Traceback-eval localization rung (pre-rewrite): symbol-less file_ops
dispatches get one cheap eval of the error evidence instead of falling
straight into whole-file rewrite. LLM eval by design (a wrong
deterministic line→symbol pick would repeat identically forever);
fail-safe to the rewrite floor on no-evidence / no-parse / invented
symbol.
"""

from __future__ import annotations

import pytest

from agent.actions.frame_actions import action_localize_fix_target
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

SYMBOLS = [
    {
        "name": "GameEngine.process_command",
        "kind": "method",
        "signature": "def process_command(self, raw_input)",
    },
    {"name": "GameEngine.tick", "kind": "method", "signature": "def tick(self)"},
    {"name": "main", "kind": "function", "signature": "def main()"},
]

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "main.py", line 98, in main\n'
    "    result = engine.process_command(command)\n"
    "AttributeError: 'str' object has no attribute 'command_type'\n"
)


def _si(responses, error_output=TRACEBACK, symbols=SYMBOLS):
    return StepInput(
        context={"symbol_table": symbols},
        params={
            "target_file_path": "main.py",
            "error_output": error_output,
            "flow_directive": "Fix the startup crash",
        },
        effects=MockEffects(inference_responses=responses),
        meta=FlowMeta(flow_name="file_ops", step_id="run_localize"),
    )


@pytest.mark.asyncio
async def test_localizes_named_symbol_present_in_ast():
    out = await action_localize_fix_target(
        _si(
            [
                '```json\n{"target_symbol": "main", "scope": "symbol",'
                ' "module_statement": "", "change_spec": "parse before dispatch"}\n```'
            ]
        )
    )
    assert out.result["localized_symbol_in_ast"] is True
    assert out.context_updates["localized_symbol"] == "main"
    assert "parse" in out.context_updates["localized_change_spec"]


@pytest.mark.asyncio
async def test_invented_symbol_falls_to_rewrite_floor():
    out = await action_localize_fix_target(
        _si(['```json\n{"target_symbol": "Ghost.method", "scope": "symbol"}\n```'])
    )
    assert out.result["localized_symbol_in_ast"] is False
    assert out.result["localized_module_fix"] is False


@pytest.mark.asyncio
async def test_module_scope_routes_to_frame_edit():
    out = await action_localize_fix_target(
        _si(
            [
                '```json\n{"target_symbol": "", "scope": "module",'
                ' "module_statement": "from parser import parse_command",'
                ' "change_spec": "import the parser"}\n```'
            ]
        )
    )
    assert out.result["localized_module_fix"] is True
    assert out.context_updates["module_statement"] == "from parser import parse_command"


@pytest.mark.asyncio
async def test_no_error_evidence_skips_inference_entirely():
    si = _si(["should-never-be-consumed"], error_output="")
    out = await action_localize_fix_target(si)
    assert out.result["localized_symbol_in_ast"] is False
    assert out.result["localized_module_fix"] is False
    # No inference call was made — the free fall-through.
    assert not [c for c in si.effects._calls if c.method == "run_inference"]


@pytest.mark.asyncio
async def test_unparseable_responses_fall_to_rewrite_floor():
    out = await action_localize_fix_target(_si(["garbage", "more garbage"]))
    assert out.result["localized_symbol_in_ast"] is False
    assert out.result["localized_module_fix"] is False
