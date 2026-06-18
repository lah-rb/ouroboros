"""Tests for diagnose_issue v11 conclude (``action_conclude_diagnosis``).

v11 removed the (α) gate and the hallucination filter (both false-dropped real,
traced symbols). These lock in the replacement behaviour:

  - the conclude step publishes the model's structured diagnosis verbatim —
    ``related_symbols`` are NOT silently dropped, even for a cross-file symbol
    that wouldn't resolve in any project symbol map (the old filter's
    false-drop failure mode), and
  - the traced-symbol list is surfaced in the conclude prompt as a positive
    aid so the model names a concrete target + co-dependent symbols.
"""

from __future__ import annotations

import pytest

from agent.actions.diagnosis_session_actions import action_conclude_diagnosis
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput


def _step_input(effects: MockEffects, **context) -> StepInput:
    return StepInput(
        context=dict(context),
        params={},
        meta=FlowMeta(
            flow_name="diagnose_issue",
            step_id="conclude",
            attempt=1,
        ),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_conclude_preserves_related_symbols():
    """A multi-symbol diagnosis must publish related_symbols verbatim — no
    silent drop, even for cross-file symbols the old filter couldn't resolve."""
    fenced = (
        "```json\n"
        "{\n"
        '  "target_file": "loader.py",\n'
        '  "target_symbol": "WorldLoader.load",\n'
        '  "related_symbols": ["models.py:Room.add_item", "models.py:Room"],\n'
        '  "root_cause": "load does not populate Room items/npcs",\n'
        '  "change_spec": "make load call Room.add_item",\n'
        '  "kind": "fix",\n'
        '  "confidence": "HIGH",\n'
        '  "recommended_flow": "file_ops"\n'
        "}\n"
        "```"
    )
    effects = MockEffects(inference_responses=[fenced])
    step_input = _step_input(
        effects,
        diagnosis_session_id="sess1",
        investigation_turn=3,
        traced_symbols=[
            "loader.py:WorldLoader.load",
            "models.py:Room.add_item",
            "models.py:Room",
        ],
    )

    out = await action_conclude_diagnosis(step_input)
    cu = out.context_updates
    assert cu["target_file"] == "loader.py"
    assert cu["target_symbol"] == "WorldLoader.load"
    # Both co-dependent symbols survive — the old hallucination filter would
    # have silently dropped these if they didn't resolve in project_symbols.
    assert cu["related_symbols"] == ["models.py:Room.add_item", "models.py:Room"]
    assert cu["recommended_flow"] == "file_ops"
    # No gate machinery leaks into the published context.
    assert "gate_warning" not in cu


@pytest.mark.asyncio
async def test_conclude_drops_primary_and_dedupes_related():
    """Light hygiene survives the gate removal: the primary target is dropped
    from related_symbols and duplicates are collapsed (cap at 6)."""
    fenced = (
        "```json\n"
        "{\n"
        '  "target_file": "engine.py",\n'
        '  "target_symbol": "GameEngine.handle",\n'
        '  "related_symbols": ["GameEngine.handle", "a.py:X", "a.py:X", "b.py:Y"]\n'
        "}\n"
        "```"
    )
    effects = MockEffects(inference_responses=[fenced])
    step_input = _step_input(
        effects, diagnosis_session_id="s", investigation_turn=1, traced_symbols=[]
    )

    out = await action_conclude_diagnosis(step_input)
    related = out.context_updates["related_symbols"]
    assert "GameEngine.handle" not in related, "primary must be dropped"
    assert related == ["a.py:X", "b.py:Y"], f"dedupe failed: {related!r}"


@pytest.mark.asyncio
async def test_conclude_publishes_module_fix_declaration_verbatim():
    """kind == module_fix + literal module_statement publish unchanged —
    file_ops routes on these structured fields, no prose extraction."""
    fenced = (
        "```json\n"
        "{\n"
        '  "target_file": "parser.py",\n'
        '  "root_cause": "parse_command instantiates InventoryCommand but parser.py never imports it",\n'
        '  "change_spec": "Import InventoryCommand so parse_command can instantiate it.",\n'
        '  "kind": "module_fix",\n'
        '  "module_statement": "from commands import InventoryCommand",\n'
        '  "confidence": "HIGH",\n'
        '  "recommended_flow": "file_ops"\n'
        "}\n"
        "```"
    )
    effects = MockEffects(inference_responses=[fenced])
    step_input = _step_input(
        effects, diagnosis_session_id="s", investigation_turn=2, traced_symbols=[]
    )

    out = await action_conclude_diagnosis(step_input)
    cu = out.context_updates
    assert cu["diagnosis_kind"] == "module_fix"
    assert cu["module_statement"] == "from commands import InventoryCommand"


@pytest.mark.asyncio
async def test_conclude_module_statement_defaults_empty():
    fenced = '```json\n{"target_file": "x.py", "kind": "fix"}\n```'
    effects = MockEffects(inference_responses=[fenced])
    step_input = _step_input(
        effects, diagnosis_session_id="s", investigation_turn=1, traced_symbols=[]
    )

    out = await action_conclude_diagnosis(step_input)
    assert out.context_updates["module_statement"] == ""


@pytest.mark.asyncio
async def test_conclude_surfaces_traced_symbols_in_prompt():
    """The traced-symbol list is injected into the conclude prompt (positive
    aid) so the model names a concrete target + co-dependent symbols."""
    fenced = '```json\n{"target_file": "x.py", "target_symbol": "f"}\n```'
    effects = MockEffects(inference_responses=[fenced])
    step_input = _step_input(
        effects,
        diagnosis_session_id="sess1",
        investigation_turn=0,
        traced_symbols=["loader.py:WorldLoader.load", "models.py:Room"],
    )

    await action_conclude_diagnosis(step_input)

    calls = effects.calls_to("session_inference")
    assert calls, "conclude must run a session inference"
    prompt = calls[0].args["prompt"]
    assert "You inspected these symbols" in prompt
