"""Turn inference publishes generation-health signals to step context.

A multi-file batch generation that hits the token ceiling (finish_reason
== length) is sliced for whatever completed; the slicer needs
``inference_truncated`` in context to report truncation so missing files
route to per-file creation instead of being misread as the model
omitting them. ``inference_tokens_generated`` rides along for the batch
economics note.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.effects.protocol import InferenceResult
from agent.models import FlowDefinition, FlowMeta, StepInput, TurnDefinition
from agent.runtime import _execute_turn_inference
from tests.conftest import ScriptedInferenceEffects
from agent.turn_renderer import TurnRenderer


def _code_turn_flow() -> FlowDefinition:
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "code",
            "sections": [
                {"type": "instruction", "literal": "Write the files."},
                {"type": "envelope"},
            ],
            "transitions": {"default": "done", "no_answer": "done"},
            "response": {"language": ""},
        }
    )
    return FlowDefinition.model_validate(
        {
            "flow": "t",
            "steps": {
                "gen": {
                    "action": "inference",
                    "description": "g",
                    "turn": turn.model_dump(),
                },
                "done": {
                    "action": "noop",
                    "description": "d",
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "gen",
        }
    )


def _make_step_input() -> StepInput:
    return StepInput(
        task="t",
        context={},
        config={},
        params={},
        meta=FlowMeta(flow_name="t", step_id="gen"),
        effects=None,
    )


@pytest.fixture
def renderer_fixture(tmp_path: Path, monkeypatch):
    import agent.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_turn_renderer", TurnRenderer(tmp_path))
    yield tmp_path


@pytest.mark.asyncio
async def test_truncated_generation_surfaces_in_context(renderer_fixture) -> None:
    flow = _code_turn_flow()
    effects = ScriptedInferenceEffects(
        [InferenceResult(text="partial output", tokens_generated=42, truncated=True)]
    )

    out = await _execute_turn_inference(
        step_def=flow.steps["gen"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={},
        effects=effects,
    )

    assert out.context_updates["inference_truncated"] is True
    assert out.context_updates["inference_tokens_generated"] == 42
    assert out.result["turn_outcome"] == "default"


@pytest.mark.asyncio
async def test_clean_generation_surfaces_falsy_flag(renderer_fixture) -> None:
    flow = _code_turn_flow()
    effects = ScriptedInferenceEffects(
        [InferenceResult(text="complete output", tokens_generated=7)]
    )

    out = await _execute_turn_inference(
        step_def=flow.steps["gen"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={},
        effects=effects,
    )

    assert out.context_updates["inference_truncated"] is False
    assert out.context_updates["inference_tokens_generated"] == 7
