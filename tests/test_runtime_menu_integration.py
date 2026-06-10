"""Runtime integration tests for menu-shape turn handling.

Exercises the menu-specific branches in `_execute_turn_inference`:
  - JSON {"choice": "..."} parsing via _extract_menu_choice
  - Retry on unparseable menu response (same protocol as empty-text
    retry for non-menu shapes)
  - turn_outcome="option:<key>" on successful match
  - turn_outcome="no_answer" when all retries fail to yield a choice
  - publish_selection deposits the option key under the declared name
  - _resolve_turn_transition routes option:<key> to per-option target
    when declared, else to default

Step C Batch B (mission_control Site #9).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.effects.protocol import InferenceResult
from agent.models import FlowDefinition, FlowMeta, StepInput, TurnDefinition
from agent.runtime import _execute_turn_inference, _resolve_turn_transition
from agent.schema_registry import set_default_registry
from agent.turn_renderer import TurnRenderer


@pytest.fixture(autouse=True)
def reset_registry():
    set_default_registry(None)
    yield
    set_default_registry(None)


class ScriptedInferenceEffects:
    def __init__(self, responses):
        self.responses = responses
        self.calls_made = 0
        self.prompts_seen: list[str] = []

    async def run_inference(self, prompt, config_overrides=None):
        self.prompts_seen.append(prompt)
        r = self.responses[self.calls_made]
        self.calls_made += 1
        return r


def _menu_turn_with_projection(publish_selection: str | None = "selected"):
    """Build a menu_single turn that sources options from a projection."""
    turn_dict = {
        "response_shape": "menu_single",
        "sections": [
            {"type": "instruction", "literal": "Pick one."},
            {"type": "options"},
            {"type": "envelope"},
        ],
        "transitions": {"default": "act", "no_answer": "recheck"},
        "response": {
            "options_from": {
                "source": "projection",
                "projection": "menu_items",
            },
        },
    }
    if publish_selection is not None:
        turn_dict["response"]["publish_selection"] = publish_selection
    return TurnDefinition.model_validate(turn_dict)


def _wrap_in_flow(turn: TurnDefinition) -> FlowDefinition:
    return FlowDefinition.model_validate(
        {
            "flow": "t",
            "steps": {
                "pick": {
                    "action": "inference",
                    "description": "pick",
                    "turn": turn.model_dump(),
                },
                "act": {
                    "action": "noop",
                    "description": "a",
                    "terminal": True,
                    "status": "success",
                },
                "recheck": {
                    "action": "noop",
                    "description": "r",
                    "terminal": True,
                    "status": "retry",
                },
            },
            "entry": "pick",
        }
    )


def _make_step_input(context: dict | None = None) -> StepInput:
    return StepInput(
        task="t",
        context=context or {},
        config={},
        params={},
        meta=FlowMeta(flow_name="t", step_id="pick"),
        effects=None,
    )


@pytest.fixture
def menu_renderer_fixture(tmp_path: Path, monkeypatch):
    """Point the runtime's turn-renderer singleton at an empty prompts
    dir — menu turns in these tests don't use templates."""
    import agent.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_turn_renderer", TurnRenderer(tmp_path))
    yield tmp_path


# ──────────────────────────────────────────────────────────────────────
# Successful choice path
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_menu_successful_choice_sets_option_outcome(
    menu_renderer_fixture,
) -> None:
    """Valid JSON choice → turn_outcome='option:<key>', publishes the
    selection under publish_selection."""
    turn = _menu_turn_with_projection()
    flow = _wrap_in_flow(turn)
    effects = ScriptedInferenceEffects(
        [
            InferenceResult(
                text='```json\n{"choice": "file_a.py"}\n```',
                tokens_generated=6,
            )
        ]
    )

    out = await _execute_turn_inference(
        step_def=flow.steps["pick"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={
            "menu_items": [
                {"id": "file_a.py", "description": "A — something"},
                {"id": "file_b.py", "description": "B — something else"},
            ],
        },
        effects=effects,
    )

    assert out.result["turn_outcome"] == "option:file_a.py"
    assert out.result["attempts"] == 1
    # publish_selection deposits the key
    assert out.context_updates["selected"] == "file_a.py"
    # Transition routes to default (no per-option override declared)
    assert _resolve_turn_transition(flow.steps["pick"].turn, out) == "act"


@pytest.mark.asyncio
async def test_menu_choice_normalized_case_and_separators(
    menu_renderer_fixture,
) -> None:
    """Model may uppercase or transpose underscores/hyphens — match_option
    normalizes. Exercises the full path: extractor → runtime outcome."""
    turn = _menu_turn_with_projection()
    flow = _wrap_in_flow(turn)
    effects = ScriptedInferenceEffects(
        [
            # Hyphen instead of underscore, different case
            InferenceResult(
                text='{"choice": "FILE-A.py"}',
                tokens_generated=4,
            )
        ]
    )

    out = await _execute_turn_inference(
        step_def=flow.steps["pick"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={
            "menu_items": [
                {"id": "file_a.py", "description": "A"},
                {"id": "file_b.py", "description": "B"},
            ],
        },
        effects=effects,
    )

    # FILE-A.py normalizes to file_a.py — paths contain dots, so the
    # raw-match path handles the case-insensitive compare.
    assert out.result["turn_outcome"] == "option:file_a.py"


# ──────────────────────────────────────────────────────────────────────
# Retry on unparseable menu response
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_menu_unparseable_response_retries(menu_renderer_fixture) -> None:
    """Non-empty but non-JSON response → retry. Third attempt succeeds."""
    turn_dict = _menu_turn_with_projection().model_dump()
    turn_dict["retries"] = 3
    turn = TurnDefinition.model_validate(turn_dict)
    flow = _wrap_in_flow(turn)

    effects = ScriptedInferenceEffects(
        [
            InferenceResult(text="I would pick file_a.", tokens_generated=5),
            InferenceResult(text="```\nfile_a.py looks good\n```", tokens_generated=6),
            InferenceResult(text='{"choice": "file_a.py"}', tokens_generated=4),
        ]
    )

    out = await _execute_turn_inference(
        step_def=flow.steps["pick"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={
            "menu_items": [
                {"id": "file_a.py", "description": "A"},
                {"id": "file_b.py", "description": "B"},
            ],
        },
        effects=effects,
    )

    assert out.result["turn_outcome"] == "option:file_a.py"
    assert out.result["attempts"] == 3
    assert effects.calls_made == 3


@pytest.mark.asyncio
async def test_menu_exhausted_retries_emits_no_answer(
    menu_renderer_fixture,
) -> None:
    """All attempts fail to parse → no_answer outcome."""
    turn_dict = _menu_turn_with_projection().model_dump()
    turn_dict["retries"] = 2
    turn = TurnDefinition.model_validate(turn_dict)
    flow = _wrap_in_flow(turn)

    effects = ScriptedInferenceEffects(
        [
            InferenceResult(text="not valid", tokens_generated=2),
            InferenceResult(text="also not valid", tokens_generated=3),
            InferenceResult(text="still not", tokens_generated=2),
        ]
    )

    out = await _execute_turn_inference(
        step_def=flow.steps["pick"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={
            "menu_items": [{"id": "file_a.py", "description": "A"}],
        },
        effects=effects,
    )

    assert out.result["turn_outcome"] == "no_answer"
    assert out.result["attempts"] == 3
    # publish_selection NOT populated since no valid choice
    assert "selected" not in out.context_updates
    assert _resolve_turn_transition(flow.steps["pick"].turn, out) == "recheck"


@pytest.mark.asyncio
async def test_menu_invalid_choice_key_treated_as_unparseable(
    menu_renderer_fixture,
) -> None:
    """Model returns JSON with a 'choice' value that isn't a valid
    option key → same as unparseable: retry, then no_answer."""
    turn_dict = _menu_turn_with_projection().model_dump()
    turn_dict["retries"] = 1
    turn = TurnDefinition.model_validate(turn_dict)
    flow = _wrap_in_flow(turn)

    effects = ScriptedInferenceEffects(
        [
            InferenceResult(text='{"choice": "nonexistent.py"}', tokens_generated=5),
            InferenceResult(text='{"choice": "another_fake.py"}', tokens_generated=5),
        ]
    )

    out = await _execute_turn_inference(
        step_def=flow.steps["pick"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={
            "menu_items": [{"id": "file_a.py", "description": "A"}],
        },
        effects=effects,
    )

    assert out.result["turn_outcome"] == "no_answer"
    assert effects.calls_made == 2


# ──────────────────────────────────────────────────────────────────────
# Per-option transition routing
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_option_outcome_routes_via_transitions_options_map(
    menu_renderer_fixture,
) -> None:
    """If transitions.options maps a chosen key → a specific step,
    routing uses that instead of default."""
    turn_dict = _menu_turn_with_projection().model_dump()
    turn_dict["transitions"] = {
        "default": "act",
        "no_answer": "recheck",
        "options": {"__conclude__": "finish_up"},
    }
    turn = TurnDefinition.model_validate(turn_dict)
    flow_dict = _wrap_in_flow(turn).model_dump()
    flow_dict["steps"]["finish_up"] = {
        "action": "noop",
        "description": "f",
        "terminal": True,
        "status": "complete",
    }
    flow = FlowDefinition.model_validate(flow_dict)

    effects = ScriptedInferenceEffects(
        [InferenceResult(text='{"choice": "__conclude__"}', tokens_generated=5)]
    )

    out = await _execute_turn_inference(
        step_def=flow.steps["pick"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={
            "menu_items": [
                {"id": "file_a.py", "description": "A"},
                {"id": "__conclude__", "description": "Stop here"},
            ],
        },
        effects=effects,
    )

    assert out.result["turn_outcome"] == "option:__conclude__"
    assert _resolve_turn_transition(flow.steps["pick"].turn, out) == "finish_up"


# ──────────────────────────────────────────────────────────────────────
# publish_selection
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_selection_absent_no_key_published(
    menu_renderer_fixture,
) -> None:
    """When turn doesn't declare publish_selection, the selected key is
    NOT published — only the raw response goes to inference_response."""
    turn = _menu_turn_with_projection(publish_selection=None)
    flow = _wrap_in_flow(turn)
    effects = ScriptedInferenceEffects(
        [InferenceResult(text='{"choice": "file_a.py"}', tokens_generated=4)]
    )

    out = await _execute_turn_inference(
        step_def=flow.steps["pick"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={"menu_items": [{"id": "file_a.py", "description": "A"}]},
        effects=effects,
    )

    assert out.result["turn_outcome"] == "option:file_a.py"
    # No extra context key
    assert "selected" not in out.context_updates
    # inference_response still holds the full text
    assert out.context_updates["inference_response"] == '{"choice": "file_a.py"}'
