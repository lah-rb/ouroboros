"""Tests for diagnose_issue v12 systemic_scan (``action_systemic_scan``).

systemic_scan is the optional post-conclude pass that widens a diagnosis by
PATTERN: it asks whether sibling symbols share the same defect class, then
appends existence-checked siblings to ``related_symbols`` so the multi-symbol
patch fixes the whole class at once. These lock in:

  - confirmed (real, file-qualified) siblings are appended + change_spec
    generalized,
  - a "local" verdict is a clean no-op,
  - hallucinated siblings (qualified name absent from the file's AST) are
    dropped — even when another class defines a method of the same bare name,
  - local ops (no target symbol / new-file) skip the inference entirely.
"""

from __future__ import annotations

import pytest

from agent.actions.diagnosis_session_actions import action_systemic_scan
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

COMMANDS_SRC = """
class TakeCommand:
    def execute(self, engine):
        return engine.take_item(self.item_id)


class TalkCommand:
    def execute(self, engine):
        engine.talk_to(self.npc_id)


class HelpCommand:
    def execute(self, engine):
        engine.show_help()
"""


def _step_input(effects: MockEffects, **context) -> StepInput:
    return StepInput(
        context=dict(context),
        params={},
        meta=FlowMeta(
            flow_name="diagnose_issue",
            step_id="systemic_scan",
            attempt=1,
        ),
        effects=effects,
    )


def _fence(body: str) -> str:
    return f"```json\n{body}\n```"


@pytest.mark.asyncio
async def test_scan_appends_confirmed_siblings_and_generalizes_spec():
    """Systemic verdict + real siblings → appended to related_symbols, and
    change_spec generalized to the pattern."""
    scan = _fence(
        '{"systemic": true, '
        '"siblings": ["commands.py:TalkCommand.execute", '
        '"commands.py:HelpCommand.execute"], '
        '"pattern_change_spec": "Each Command.execute must call the engine '
        'method that actually exists."}'
    )
    effects = MockEffects(
        files={"commands.py": COMMANDS_SRC}, inference_responses=[scan]
    )
    step_input = _step_input(
        effects,
        diagnosis_session_id="s",
        target_file="commands.py",
        target_symbol="TakeCommand.execute",
        related_symbols=[],
        change_spec="Make TakeCommand.execute call engine.take.",
        diagnosis_kind="fix",
    )

    out = await action_systemic_scan(step_input)
    cu = out.context_updates
    assert cu["related_symbols"] == [
        "commands.py:TalkCommand.execute",
        "commands.py:HelpCommand.execute",
    ]
    assert "must call the engine method" in cu["change_spec"]
    assert "Primary target" in cu["change_spec"]  # original spec preserved
    assert out.result["siblings_added"] == 2


@pytest.mark.asyncio
async def test_scan_local_verdict_is_noop():
    """systemic:false → related_symbols and change_spec unchanged."""
    scan = _fence('{"systemic": false, "siblings": [], "pattern_change_spec": ""}')
    effects = MockEffects(
        files={"commands.py": COMMANDS_SRC}, inference_responses=[scan]
    )
    step_input = _step_input(
        effects,
        diagnosis_session_id="s",
        target_file="commands.py",
        target_symbol="TakeCommand.execute",
        related_symbols=["commands.py:Existing.sym"],
        change_spec="local fix",
        diagnosis_kind="fix",
    )

    out = await action_systemic_scan(step_input)
    assert out.context_updates["related_symbols"] == ["commands.py:Existing.sym"]
    assert out.context_updates["change_spec"] == "local fix"
    assert out.result["siblings_added"] == 0


@pytest.mark.asyncio
async def test_scan_drops_hallucinated_sibling():
    """A qualified sibling whose class isn't in the file is dropped — even
    though another class defines a method of the same bare name (`execute`)."""
    scan = _fence(
        '{"systemic": true, '
        '"siblings": ["commands.py:GhostCommand.execute"], '
        '"pattern_change_spec": "fix the class"}'
    )
    effects = MockEffects(
        files={"commands.py": COMMANDS_SRC}, inference_responses=[scan]
    )
    step_input = _step_input(
        effects,
        diagnosis_session_id="s",
        target_file="commands.py",
        target_symbol="TakeCommand.execute",
        related_symbols=[],
        change_spec="local fix",
        diagnosis_kind="fix",
    )

    out = await action_systemic_scan(step_input)
    assert out.context_updates["related_symbols"] == []  # hallucination dropped
    assert out.context_updates["change_spec"] == "local fix"  # not generalized
    assert out.result["siblings_added"] == 0


@pytest.mark.asyncio
async def test_scan_skips_inference_for_local_ops():
    """new-file / no-target ops have no symbol-level siblings — skip the
    inference entirely (cheap pass-through)."""
    effects = MockEffects(files={}, inference_responses=["SHOULD_NOT_BE_USED"])
    step_input = _step_input(
        effects,
        diagnosis_session_id="s",
        target_file="newmod.py",
        target_symbol="",  # whole-file / new file
        related_symbols=[],
        change_spec="create newmod.py",
        diagnosis_kind="new_file",
    )

    out = await action_systemic_scan(step_input)
    assert out.result["scanned"] is False
    assert effects.call_count("session_inference") == 0  # no inference burned
    assert out.context_updates["change_spec"] == "create newmod.py"
