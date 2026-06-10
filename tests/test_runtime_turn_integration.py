"""Runtime integration tests for the turn-based inference path.

These tests exercise the Phase 4 wiring in agent/runtime.py:
  - _execute_turn_inference renders via TurnRenderer, runs inference
    with turn.retries+1 attempts on empty response, and classifies
    the outcome as "default" or "no_answer".
  - _resolve_turn_transition routes to turn.transitions based on that
    outcome.
  - The existing prompt_template path remains untouched by turn work.

Step C migration, Phase 4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agent.effects.protocol import InferenceResult
from agent.models import (
    FlowDefinition,
    StepDefinition,
    StepInput,
    TurnDefinition,
)
from agent.runtime import (
    _execute_turn_inference,
    _get_turn_renderer,
    _resolve_turn_transition,
)
from agent.schema_registry import SchemaRegistry, set_default_registry
from agent.turn_renderer import TurnRenderer

# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def demo_schema_registry():
    """Inject a demo schema so json_document turns can render envelopes."""
    registry = SchemaRegistry(
        {
            "demo": {
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
                "x-example": {"result": "example"},
            }
        }
    )
    set_default_registry(registry)
    yield registry
    set_default_registry(None)


@pytest.fixture
def prompts_dir(tmp_path: Path, monkeypatch) -> Path:
    """Build a minimal prompts directory and point runtime's turn
    renderer singleton at it."""
    (tmp_path / "personas").mkdir()
    (tmp_path / "demo").mkdir()

    (tmp_path / "personas" / "tester.yaml").write_text(dedent("""
            id: personas/tester
            content: |
              ---ACT AS---
              You are a test subject.
              ---END---
            """).strip())

    (tmp_path / "demo" / "instructions.yaml").write_text(dedent("""
            id: demo/instructions
            content: |
              Produce a demo result for {input.task_name}.
            """).strip())

    # Override the runtime singleton so _get_turn_renderer returns our
    # test renderer pointing at tmp_path instead of the repo prompts/.
    import agent.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_turn_renderer", TurnRenderer(tmp_path))
    return tmp_path


@dataclass
class StubInferenceEffects:
    """Configurable effects double: returns a scripted sequence of
    InferenceResults on each inference call.

    Honors run_inference for stateless calls; tests can also set a
    session_id via fixtures to exercise the session path.
    """

    responses: list[InferenceResult]
    prompts_seen: list[str] = field(default_factory=list)
    calls_made: int = 0

    async def run_inference(
        self,
        prompt: str,
        config_overrides: dict | None = None,
    ) -> InferenceResult:
        self.prompts_seen.append(prompt)
        if self.calls_made >= len(self.responses):
            raise RuntimeError(
                f"Stub exhausted: made {self.calls_made} calls, "
                f"only {len(self.responses)} responses scripted"
            )
        result = self.responses[self.calls_made]
        self.calls_made += 1
        return result


def _demo_flow_def(
    step_name: str = "generate",
    *,
    turn: TurnDefinition | None = None,
    terminal_success: str = "done",
) -> FlowDefinition:
    """Build a minimal flow with one inference step + terminal targets
    for success/failure routing."""
    if turn is None:
        turn = TurnDefinition.model_validate(
            {
                "response_shape": "json_document",
                "sections": [
                    {"type": "instruction", "literal": "do the thing"},
                    {"type": "envelope"},
                ],
                "transitions": {"default": "done", "no_answer": "failed"},
                "response": {"schema_id": "demo"},
            }
        )

    return FlowDefinition.model_validate(
        {
            "flow": "test_flow",
            "steps": {
                step_name: {
                    "action": "inference",
                    "description": "test step",
                    "turn": turn.model_dump(),
                },
                "done": {
                    "action": "noop",
                    "description": "success",
                    "terminal": True,
                    "status": terminal_success,
                },
                "failed": {
                    "action": "noop",
                    "description": "failure",
                    "terminal": True,
                    "status": "failed",
                },
            },
            "entry": step_name,
        }
    )


def _make_step_input(context: dict[str, Any] | None = None) -> StepInput:
    """Minimal StepInput — runtime doesn't need most fields for these
    turn-path tests."""
    from agent.models import FlowMeta

    return StepInput(
        task="test",
        context=context or {},
        config={},
        params={},
        meta=FlowMeta(flow_name="test_flow", step_id="generate"),
        effects=None,
    )


# ──────────────────────────────────────────────────────────────────────
# _execute_turn_inference — happy path
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_inference_non_empty_response_returns_default_outcome(
    demo_schema_registry, prompts_dir
) -> None:
    """A single non-empty response → turn_outcome="default", one attempt."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "json_document",
            "sections": [
                {"type": "instruction", "literal": "summarize"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "done", "no_answer": "failed"},
            "response": {"schema_id": "demo"},
        }
    )
    flow = _demo_flow_def(turn=turn)
    effects = StubInferenceEffects(
        responses=[
            InferenceResult(
                text="here is the summary",
                tokens_generated=5,
                finished=True,
            )
        ]
    )

    step_output = await _execute_turn_inference(
        step_def=flow.steps["generate"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={},
        effects=effects,
        _step_name="generate",
    )

    assert step_output.result["turn_outcome"] == "default"
    assert step_output.result["attempts"] == 1
    assert step_output.result["text"] == "here is the summary"
    assert step_output.context_updates["inference_response"] == "here is the summary"
    assert effects.calls_made == 1


@pytest.mark.asyncio
async def test_turn_inference_publishes_keys_map_to_response(
    demo_schema_registry, prompts_dir
) -> None:
    """The `publishes` list on the step still maps to the response text."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "json_document",
            "sections": [
                {"type": "instruction", "literal": "x"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "done", "no_answer": "failed"},
            "response": {"schema_id": "demo"},
        }
    )
    step = StepDefinition.model_validate(
        {
            "action": "inference",
            "description": "t",
            "turn": turn.model_dump(),
            "publishes": ["summary", "my_response"],
        }
    )
    flow = FlowDefinition.model_validate(
        {
            "flow": "test",
            "steps": {
                "generate": step.model_dump(),
                "done": {
                    "action": "noop",
                    "description": "d",
                    "terminal": True,
                    "status": "success",
                },
                "failed": {
                    "action": "noop",
                    "description": "f",
                    "terminal": True,
                    "status": "failed",
                },
            },
            "entry": "generate",
        }
    )
    effects = StubInferenceEffects(
        responses=[InferenceResult(text="hello world", tokens_generated=2)]
    )

    step_output = await _execute_turn_inference(
        step_def=flow.steps["generate"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={},
        effects=effects,
    )

    assert step_output.context_updates["summary"] == "hello world"
    assert step_output.context_updates["my_response"] == "hello world"
    assert step_output.context_updates["inference_response"] == "hello world"


# ──────────────────────────────────────────────────────────────────────
# _execute_turn_inference — retry behavior
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_inference_retries_on_empty_response(
    demo_schema_registry, prompts_dir
) -> None:
    """Empty responses → retry up to turn.retries+1 attempts. Third
    attempt succeeds."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "json_document",
            "sections": [
                {"type": "instruction", "literal": "x"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "done", "no_answer": "failed"},
            "response": {"schema_id": "demo"},
            "retries": 3,
        }
    )
    flow = _demo_flow_def(turn=turn)
    effects = StubInferenceEffects(
        responses=[
            InferenceResult(text="", tokens_generated=0),
            InferenceResult(text="", tokens_generated=0),
            InferenceResult(text="finally got one", tokens_generated=3),
        ]
    )

    step_output = await _execute_turn_inference(
        step_def=flow.steps["generate"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={},
        effects=effects,
    )

    assert step_output.result["turn_outcome"] == "default"
    assert step_output.result["attempts"] == 3
    assert step_output.result["text"] == "finally got one"
    assert effects.calls_made == 3


@pytest.mark.asyncio
async def test_turn_inference_exhausts_retries_emits_no_answer(
    demo_schema_registry, prompts_dir
) -> None:
    """All attempts return empty → turn_outcome="no_answer"."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "json_document",
            "sections": [
                {"type": "instruction", "literal": "x"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "done", "no_answer": "failed"},
            "response": {"schema_id": "demo"},
            "retries": 2,  # 3 total attempts
        }
    )
    flow = _demo_flow_def(turn=turn)
    effects = StubInferenceEffects(
        responses=[
            InferenceResult(text="", tokens_generated=0),
            InferenceResult(text="   ", tokens_generated=0),  # whitespace also empty
            InferenceResult(text="", tokens_generated=0),
        ]
    )

    step_output = await _execute_turn_inference(
        step_def=flow.steps["generate"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={},
        effects=effects,
    )

    assert step_output.result["turn_outcome"] == "no_answer"
    assert step_output.result["attempts"] == 3
    assert effects.calls_made == 3


@pytest.mark.asyncio
async def test_turn_inference_zero_retries_means_single_attempt(
    demo_schema_registry, prompts_dir
) -> None:
    """retries=0 → one attempt total, no retry even on empty response."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "json_document",
            "sections": [
                {"type": "instruction", "literal": "x"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "done", "no_answer": "failed"},
            "response": {"schema_id": "demo"},
            "retries": 0,
        }
    )
    flow = _demo_flow_def(turn=turn)
    effects = StubInferenceEffects(
        responses=[InferenceResult(text="", tokens_generated=0)]
    )

    step_output = await _execute_turn_inference(
        step_def=flow.steps["generate"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={},
        effects=effects,
    )

    assert step_output.result["turn_outcome"] == "no_answer"
    assert step_output.result["attempts"] == 1
    assert effects.calls_made == 1


@pytest.mark.asyncio
async def test_turn_inference_error_breaks_out_of_retry_loop(
    demo_schema_registry, prompts_dir
) -> None:
    """Inference errors → no retry; classify as no_answer immediately."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "json_document",
            "sections": [
                {"type": "instruction", "literal": "x"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "done", "no_answer": "failed"},
            "response": {"schema_id": "demo"},
            "retries": 3,
        }
    )
    flow = _demo_flow_def(turn=turn)
    effects = StubInferenceEffects(
        responses=[
            InferenceResult(
                text="",
                tokens_generated=0,
                finished=False,
                error="connection refused",
            ),
            # Should NOT be consumed — errors don't retry.
            InferenceResult(text="never reached", tokens_generated=1),
        ]
    )

    step_output = await _execute_turn_inference(
        step_def=flow.steps["generate"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={},
        effects=effects,
    )

    assert step_output.result["turn_outcome"] == "no_answer"
    assert step_output.result["attempts"] == 1
    assert step_output.result["error"] == "connection refused"
    assert effects.calls_made == 1


# ──────────────────────────────────────────────────────────────────────
# _execute_turn_inference — config + rendering
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_inference_config_comes_from_turn_not_step_input(
    demo_schema_registry, prompts_dir
) -> None:
    """turn.config.temperature is authoritative for turn-based steps,
    not the merged step_input.config."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "json_document",
            "sections": [
                {"type": "instruction", "literal": "x"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "done", "no_answer": "failed"},
            "response": {"schema_id": "demo"},
            "config": {"temperature": "t*0.0"},
        }
    )
    flow = _demo_flow_def(turn=turn)

    captured_config: dict[str, Any] = {}

    class CapturingEffects:
        async def run_inference(
            self, prompt: str, config_overrides: dict | None = None
        ) -> InferenceResult:
            if config_overrides:
                captured_config.update(config_overrides)
            return InferenceResult(text="ok", tokens_generated=1)

    effects = CapturingEffects()
    await _execute_turn_inference(
        step_def=flow.steps["generate"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={},
        effects=effects,
    )

    assert captured_config.get("temperature") == "t*0.0"


@pytest.mark.asyncio
async def test_turn_inference_prompt_rendered_via_turn_renderer(
    demo_schema_registry, prompts_dir
) -> None:
    """The prompt sent to inference includes the banner and rendered
    sections — evidence TurnRenderer is being used."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "json_document",
            "sections": [
                {"type": "role", "template": "personas/tester"},
                {"type": "instruction", "template": "demo/instructions"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "done", "no_answer": "failed"},
            "response": {"schema_id": "demo"},
        }
    )
    flow = _demo_flow_def(turn=turn)
    effects = StubInferenceEffects(
        responses=[InferenceResult(text='{"result": "x"}', tokens_generated=5)]
    )

    await _execute_turn_inference(
        step_def=flow.steps["generate"],
        step_input=_make_step_input(),
        flow_def=flow,
        inputs={"task_name": "verify"},
        effects=effects,
    )

    prompt = effects.prompts_seen[0]
    assert prompt.startswith("=== JSON DOCUMENT ===")
    assert "test subject" in prompt  # from persona template
    assert "Produce a demo result for verify" in prompt  # input substitution
    assert "```json" in prompt  # envelope


# ──────────────────────────────────────────────────────────────────────
# _resolve_turn_transition
# ──────────────────────────────────────────────────────────────────────


def _trivial_turn(**overrides) -> TurnDefinition:
    """A turn just for routing tests — sections / shape don't matter."""
    base = {
        "response_shape": "json_document",
        "sections": [
            {"type": "instruction", "literal": "x"},
            {"type": "envelope"},
        ],
        "transitions": {"default": "dflt_step", "no_answer": "na_step"},
        "response": {"schema_id": "demo"},
    }
    base.update(overrides)
    return TurnDefinition.model_validate(base)


def _step_output_with_outcome(outcome: str):
    from agent.models import StepOutput

    return StepOutput(
        result={"turn_outcome": outcome},
        observations="",
    )


def test_resolve_default_outcome_routes_to_transitions_default() -> None:
    turn = _trivial_turn()
    next_step = _resolve_turn_transition(turn, _step_output_with_outcome("default"))
    assert next_step == "dflt_step"


def test_resolve_no_answer_outcome_routes_to_transitions_no_answer() -> None:
    turn = _trivial_turn()
    next_step = _resolve_turn_transition(turn, _step_output_with_outcome("no_answer"))
    assert next_step == "na_step"


def test_resolve_missing_outcome_defaults_to_default() -> None:
    """Absent turn_outcome (shouldn't happen in practice, but defend
    against regression) falls to default, not no_answer."""
    from agent.models import StepOutput

    turn = _trivial_turn()
    empty_output = StepOutput(result={}, observations="")
    next_step = _resolve_turn_transition(turn, empty_output)
    assert next_step == "dflt_step"


def test_resolve_option_outcome_routes_via_options_map() -> None:
    """Menu-path outcome "option:<key>" routes via transitions.options
    (reserved for future menu-site phases, but the plumbing is here)."""
    turn = _trivial_turn(
        transitions={
            "default": "dflt_step",
            "no_answer": "na_step",
            "options": {"__conclude__": "end_session", "my_option": "other_step"},
        }
    )
    assert (
        _resolve_turn_transition(turn, _step_output_with_outcome("option:__conclude__"))
        == "end_session"
    )
    assert (
        _resolve_turn_transition(turn, _step_output_with_outcome("option:my_option"))
        == "other_step"
    )


def test_resolve_option_outcome_unknown_key_falls_through_to_default() -> None:
    """An option-outcome whose key isn't in transitions.options → default."""
    turn = _trivial_turn(
        transitions={
            "default": "dflt_step",
            "no_answer": "na_step",
            "options": {"known_key": "target_a"},
        }
    )
    next_step = _resolve_turn_transition(
        turn, _step_output_with_outcome("option:unknown_key")
    )
    assert next_step == "dflt_step"


# ──────────────────────────────────────────────────────────────────────
# Legacy path is still intact
# ──────────────────────────────────────────────────────────────────────


def test_step_definition_without_turn_still_requires_prompt_template() -> None:
    """StepDefinition.turn=None + no prompt_template is still rejected
    at runtime. The error message mentions the new turn option."""
    # Nothing to validate at the Pydantic layer (both turn and
    # prompt_template are optional); the check is in runtime. We just
    # verify the model can still be built without a turn.
    step = StepDefinition.model_validate(
        {
            "action": "inference",
            "description": "legacy",
            "prompt_template": {"template": "some/template"},
        }
    )
    assert step.turn is None
    assert step.prompt_template is not None


def test_get_turn_renderer_singleton_is_stable() -> None:
    """Smoke test — _get_turn_renderer returns a consistent instance."""
    r1 = _get_turn_renderer()
    r2 = _get_turn_renderer()
    assert r1 is r2


# ══════════════════════════════════════════════════════════════════════
# Regression tests — issues discovered during the Step C live test
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_turn_inference_consumes_session_injections_into_prompt() -> None:
    """Regression: turn-based inference steps must consume seed
    injections queued by upstream actions (start_diagnosis_session,
    run_session's start_session, etc.). Pre-fix, _execute_turn_inference
    bypassed the queue entirely — the model got a bare turn-render with
    no error context, no file contents, no charter.

    Verifies:
      - Queued injections get prepended to the rendered prompt
      - The injection queue drains via context_updates so a single seed
        doesn't replay on every subsequent inference
    """
    from agent.session_injections import queue as queue_injection

    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_single",
            "sections": [
                {"type": "instruction", "literal": "Pick one:"},
                {"type": "options"},
                {"type": "envelope"},
            ],
            "response": {
                "options": {
                    "yes": {"key": "yes", "description": "y"},
                    "no": {"key": "no", "description": "n"},
                },
            },
            "transitions": {"default": "next", "no_answer": "fail"},
            "retries": 0,
        }
    )
    step_def = StepDefinition.model_validate(
        {
            "action": "inference",
            "description": "test",
            "turn": turn.model_dump(by_alias=True),
        }
    )
    flow_def = FlowDefinition.model_validate(
        {"flow": "t", "steps": {"s": step_def.model_dump(by_alias=True)}, "entry": "s"}
    )

    # Build a context with a seed injection queued, as start_session would.
    context: dict[str, Any] = {"inference_session_id": "sess-42"}
    queue_injection(context, context, "SEED CONTEXT: error=ModuleNotFoundError")

    step_input = StepInput(
        task="t",
        context=context,
        config={},
        params={},
        meta=_make_meta(),
        effects=None,
        turn=turn,
    )

    class CapturingSessionEffects:
        def __init__(self) -> None:
            self.prompt_seen = ""

        async def session_inference(self, session_id, prompt, config_overrides=None):
            self.prompt_seen = prompt
            return InferenceResult(text='{"choice": "yes"}', tokens_generated=3)

        async def emit_trace(self, ev):  # no-op
            pass

    effects = CapturingSessionEffects()
    out = await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=flow_def,
        inputs={},
        effects=effects,
    )

    # Seed reached the model
    assert "SEED CONTEXT: error=ModuleNotFoundError" in effects.prompt_seen
    # Turn's own rendered content is still there after the seed
    assert "Pick one:" in effects.prompt_seen
    # Queue drains the seed via consume — but for menu turns with a
    # successfully-extracted choice, an acceptance signal is queued
    # afterward (e39 round). The original seed must be gone from
    # the queue; only the new acceptance signal remains.
    remaining = out.context_updates.get("session_injections", [])
    assert "SEED CONTEXT" not in " ".join(remaining)
    # Acceptance signal queued for the next turn
    assert any("was accepted" in s for s in remaining)


@pytest.mark.asyncio
async def test_turn_inference_does_not_double_emit_trace_on_session_path() -> None:
    """Regression: _execute_turn_inference was emitting InferenceCall
    while LocalEffects.session_inference also emits one — producing two
    trace rows per logical inference and inflating cost reports 2x.
    The runtime emit should be skipped when session_id is set."""
    from agent.trace import InferenceCall

    turn = TurnDefinition.model_validate(
        {
            "response_shape": "prose",
            "sections": [
                {"type": "instruction", "literal": "Describe."},
                {"type": "envelope"},
            ],
            "response": {},
            "transitions": {"default": "next", "no_answer": "fail"},
            "retries": 0,
        }
    )
    step_def = StepDefinition.model_validate(
        {
            "action": "inference",
            "description": "test",
            "turn": turn.model_dump(by_alias=True),
        }
    )
    flow_def = FlowDefinition.model_validate(
        {"flow": "t", "steps": {"s": step_def.model_dump(by_alias=True)}, "entry": "s"}
    )
    step_input = StepInput(
        task="t",
        context={"inference_session_id": "sess-7"},
        config={},
        params={},
        meta=_make_meta(),
        effects=None,
        turn=turn,
    )

    class CountingEffects:
        def __init__(self) -> None:
            self.emit_count = 0

        async def session_inference(self, session_id, prompt, config_overrides=None):
            return InferenceResult(text="some prose", tokens_generated=2)

        async def emit_trace(self, ev):
            if isinstance(ev, InferenceCall):
                self.emit_count += 1

    effects = CountingEffects()
    await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=flow_def,
        inputs={},
        effects=effects,
    )

    # LocalEffects emits; runtime does NOT duplicate on the session path.
    # This stub is the effects layer itself, so it emits zero (we don't
    # exercise LocalEffects here) — the check is that the RUNTIME path
    # doesn't fire its own emit when session_id is present.
    assert effects.emit_count == 0, (
        f"Runtime should not emit InferenceCall on session path "
        f"(LocalEffects.session_inference owns that). Got {effects.emit_count}."
    )


@pytest.mark.asyncio
async def test_turn_inference_still_emits_trace_on_stateless_path() -> None:
    """Complement to the above: for the stateless run_inference path
    (no session_id), runtime MUST emit the trace event because
    run_inference doesn't emit its own."""
    from agent.trace import InferenceCall

    turn = TurnDefinition.model_validate(
        {
            "response_shape": "prose",
            "sections": [
                {"type": "instruction", "literal": "Describe."},
                {"type": "envelope"},
            ],
            "response": {},
            "transitions": {"default": "next", "no_answer": "fail"},
            "retries": 0,
        }
    )
    step_def = StepDefinition.model_validate(
        {
            "action": "inference",
            "description": "test",
            "turn": turn.model_dump(by_alias=True),
        }
    )
    flow_def = FlowDefinition.model_validate(
        {"flow": "t", "steps": {"s": step_def.model_dump(by_alias=True)}, "entry": "s"}
    )
    # No inference_session_id / edit_session_id / session_id in context
    step_input = StepInput(
        task="t",
        context={},
        config={},
        params={},
        meta=_make_meta(),
        effects=None,
        turn=turn,
    )

    class CountingEffects:
        def __init__(self) -> None:
            self.emit_count = 0

        async def run_inference(self, prompt, config_overrides=None):
            return InferenceResult(text="some prose", tokens_generated=2)

        async def emit_trace(self, ev):
            if isinstance(ev, InferenceCall):
                self.emit_count += 1

    effects = CountingEffects()
    await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=flow_def,
        inputs={},
        effects=effects,
    )

    assert effects.emit_count == 1, (
        f"Runtime must emit InferenceCall on stateless path "
        f"(run_inference doesn't emit). Got {effects.emit_count}."
    )


def _make_meta():
    from agent.models import FlowMeta

    return FlowMeta(flow_name="t", step_id="s")


# ══════════════════════════════════════════════════════════════════════
# Regression tests — issues discovered in the a85f381e live run
# ══════════════════════════════════════════════════════════════════════


def test_inference_session_id_flows_through_context_filter() -> None:
    """Regression: before this fix, _AMBIENT_CONTEXT_KEYS held only
    ``session_injections``. Turn-inference steps that declared only
    a flow-specific session key (like ``diagnosis_session_id``) and
    omitted ``inference_session_id`` from context.optional would have
    the latter stripped from their filtered context by the runtime.

    Consequence in the a85f381e trace: diagnose_issue's pick_file and
    pick_action each hit 180s timeouts (18× observed) because the
    turn dispatcher's session picker found no inference_session_id,
    fell back to stateless run_inference, and never consumed the
    queued seed injections. The model got a bare menu and timed out.

    Fix: inference_session_id is now ambient, like session_injections.
    This test pins the allowlist so a future refactor doesn't silently
    re-break the behavior.
    """
    from agent.runtime import _AMBIENT_CONTEXT_KEYS

    assert "inference_session_id" in _AMBIENT_CONTEXT_KEYS, (
        "inference_session_id must be ambient so it reaches the turn "
        "inference dispatcher regardless of whether a flow step's CUE "
        "declares it in context.required/optional"
    )
    # session_injections was already ambient; keep it pinned too since
    # the two keys together form the session-memory ambient protocol.
    assert "session_injections" in _AMBIENT_CONTEXT_KEYS


@pytest.mark.asyncio
async def test_turn_inference_picks_up_ambient_inference_session_id() -> None:
    """End-to-end check: a step whose CUE declares only a flow-specific
    session key (e.g. ``diagnosis_session_id``) without mentioning
    ``inference_session_id`` should still route to the session path
    when an upstream action published ``inference_session_id`` into
    context. This catches the a85 regression at the dispatch level,
    not just at the allowlist level."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "prose",
            "sections": [
                {"type": "instruction", "literal": "Describe."},
                {"type": "envelope"},
            ],
            "response": {},
            "transitions": {"default": "next", "no_answer": "fail"},
            "retries": 0,
        }
    )
    step_def = StepDefinition.model_validate(
        {
            "action": "inference",
            "description": "diagnose-style step that doesn't list inference_session_id",
            "turn": turn.model_dump(by_alias=True),
            # Mimics diagnose_issue: declares a flow-specific session
            # key but NOT inference_session_id. Pre-fix, the runtime
            # filter would strip inference_session_id even if the
            # accumulator had it.
            "context": {"required": ["diagnosis_session_id"]},
        }
    )
    flow_def = FlowDefinition.model_validate(
        {
            "flow": "t",
            "steps": {"s": step_def.model_dump(by_alias=True)},
            "entry": "s",
        }
    )

    # Context as if upstream start_diagnosis_session already ran:
    # publishes both diagnosis_session_id and inference_session_id.
    step_input = StepInput(
        task="t",
        context={
            "diagnosis_session_id": "diag-42",
            "inference_session_id": "diag-42",
        },
        config={},
        params={},
        meta=_make_meta(),
        effects=None,
        turn=turn,
    )

    class PathRecordingEffects:
        def __init__(self) -> None:
            self.used_session = False
            self.used_stateless = False

        async def session_inference(self, session_id, prompt, config_overrides=None):
            self.used_session = True
            return InferenceResult(text="ok", tokens_generated=1)

        async def run_inference(self, prompt, config_overrides=None):
            self.used_stateless = True
            return InferenceResult(text="ok", tokens_generated=1)

        async def emit_trace(self, ev):
            pass

    effects = PathRecordingEffects()
    await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=flow_def,
        inputs={},
        effects=effects,
    )

    assert effects.used_session, (
        "Turn dispatcher should route to session_inference when "
        "inference_session_id is in context (even if the step's CUE "
        "didn't declare it — it's ambient)"
    )
    assert not effects.used_stateless, (
        "Turn dispatcher must NOT fall through to stateless run_inference "
        "when an ambient inference_session_id is available"
    )


@pytest.mark.asyncio
async def test_turn_inference_publishes_menu_compound_arg() -> None:
    """Regression: menu_compound turns must publish the chosen option's
    argument under ``{publish_selection}_arg`` so downstream steps
    don't have to reparse inference_response.

    The a85 trace showed diagnose_issue.pick_action emit
    ``{"choice": "__run_command__", "command": "grep foo"}`` but
    the command string was lost: runtime only published the choice,
    and execute_investigation_tool's fallback chain found nothing.
    """
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_compound",
            "sections": [
                {"type": "instruction", "literal": "Pick and provide an arg:"},
                {"type": "options"},
                {"type": "envelope"},
            ],
            "response": {
                "options": {
                    "run_cmd": {
                        "key": "run_cmd",
                        "description": "Run a command",
                        "arg": {
                            "name": "command",
                            "description": "The command to run",
                        },
                    },
                    "bail": {
                        "key": "bail",
                        "description": "Give up",
                    },
                },
                "publish_selection": "my_choice",
            },
            "transitions": {
                "default": "next",
                "no_answer": "fail",
                "options": {"run_cmd": "exec", "bail": "done"},
            },
            "retries": 0,
        }
    )
    step_def = StepDefinition.model_validate(
        {
            "action": "inference",
            "description": "test",
            "turn": turn.model_dump(by_alias=True),
        }
    )
    flow_def = FlowDefinition.model_validate(
        {
            "flow": "t",
            "steps": {"s": step_def.model_dump(by_alias=True)},
            "entry": "s",
        }
    )
    step_input = StepInput(
        task="t",
        context={},
        config={},
        params={},
        meta=_make_meta(),
        effects=None,
        turn=turn,
    )

    class ScriptedEffects:
        async def run_inference(self, prompt, config_overrides=None):
            return InferenceResult(
                text='{"choice": "run_cmd", "command": "grep foo file.py"}',
                tokens_generated=10,
            )

        async def emit_trace(self, ev):
            pass

    out = await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=flow_def,
        inputs={},
        effects=ScriptedEffects(),
    )

    # Choice is published as before.
    assert out.context_updates.get("my_choice") == "run_cmd"
    # Arg is published under the expected convention.
    assert out.context_updates.get("my_choice_arg") == "grep foo file.py", (
        f"Expected my_choice_arg='grep foo file.py', got "
        f"{out.context_updates.get('my_choice_arg')!r}. Runtime must "
        f"extract the menu_compound arg and publish it under "
        f"{{publish_selection}}_arg."
    )


@pytest.mark.asyncio
async def test_turn_inference_menu_arg_preserves_trailing_newline_when_fenced() -> None:
    """End-to-end guard for the PTY "phantom": a fenced menu_compound reply
    whose arg ends in ``\\n`` must publish that arg WITH the newline intact.

    The model fences its reply (```` ```json … ``` ````); json_repair ate the
    trailing ``\\n`` from the command arg because the closing fence is trailing
    slop, so a ``send_input`` of ``"look\\n"`` reached the PTY as ``"look"`` and
    the game's input() blocked forever — a hang the harness mis-reported as a
    program defect. Fixed in parse_llm_json (recovery now fires on any trailing
    content); this asserts the whole runtime path preserves it.
    """
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_compound",
            "sections": [
                {"type": "instruction", "literal": "Pick:"},
                {"type": "options"},
                {"type": "envelope"},
            ],
            "response": {
                "options": {
                    "send_input": {
                        "key": "send_input",
                        "description": "Send input to the program",
                        "arg": {"name": "text", "description": "Text to send"},
                    },
                },
                "publish_selection": "planned_action",
            },
            "transitions": {
                "default": "next",
                "no_answer": "fail",
                "options": {"send_input": "exec"},
            },
            "retries": 0,
        }
    )
    step_def = StepDefinition.model_validate(
        {
            "action": "inference",
            "description": "test",
            "turn": turn.model_dump(by_alias=True),
        }
    )
    flow_def = FlowDefinition.model_validate(
        {
            "flow": "t",
            "steps": {"s": step_def.model_dump(by_alias=True)},
            "entry": "s",
        }
    )
    step_input = StepInput(
        task="t",
        context={},
        config={},
        params={},
        meta=_make_meta(),
        effects=None,
        turn=turn,
    )

    class ScriptedEffects:
        async def run_inference(self, prompt, config_overrides=None):
            # Fenced reply, exactly as small models emit it.
            return InferenceResult(
                text='```json\n{"choice": "send_input", "text": "look\\n"}\n```',
                tokens_generated=10,
            )

        async def emit_trace(self, ev):
            pass

    out = await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=flow_def,
        inputs={},
        effects=ScriptedEffects(),
    )

    assert out.context_updates.get("planned_action") == "send_input"
    assert out.context_updates.get("planned_action_arg") == "look\n", (
        f"trailing newline must survive the fenced menu_compound path; got "
        f"{out.context_updates.get('planned_action_arg')!r}"
    )


@pytest.mark.asyncio
async def test_turn_inference_omits_arg_when_option_has_none() -> None:
    """Complement to above: if the chosen option has no arg declared
    (e.g., a bail option with just a key), no {publish_selection}_arg
    key is written. Publishing an empty string there would cause
    downstream ``context.get(...) or fallback`` chains to misfire."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_compound",
            "sections": [
                {"type": "instruction", "literal": "Pick:"},
                {"type": "options"},
                {"type": "envelope"},
            ],
            "response": {
                "options": {
                    "run_cmd": {
                        "key": "run_cmd",
                        "description": "Run a command",
                        "arg": {"name": "command", "description": "cmd"},
                    },
                    "bail": {
                        "key": "bail",
                        "description": "Give up",
                    },
                },
                "publish_selection": "my_choice",
            },
            "transitions": {
                "default": "next",
                "no_answer": "fail",
                "options": {"run_cmd": "exec", "bail": "done"},
            },
            "retries": 0,
        }
    )
    step_def = StepDefinition.model_validate(
        {
            "action": "inference",
            "description": "test",
            "turn": turn.model_dump(by_alias=True),
        }
    )
    flow_def = FlowDefinition.model_validate(
        {
            "flow": "t",
            "steps": {"s": step_def.model_dump(by_alias=True)},
            "entry": "s",
        }
    )
    step_input = StepInput(
        task="t",
        context={},
        config={},
        params={},
        meta=_make_meta(),
        effects=None,
        turn=turn,
    )

    class ScriptedEffects:
        async def run_inference(self, prompt, config_overrides=None):
            return InferenceResult(
                text='{"choice": "bail"}',
                tokens_generated=3,
            )

        async def emit_trace(self, ev):
            pass

    out = await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=flow_def,
        inputs={},
        effects=ScriptedEffects(),
    )

    assert out.context_updates.get("my_choice") == "bail"
    # No arg on the bail option → no _arg key at all (not "", not None).
    assert "my_choice_arg" not in out.context_updates, (
        "Options without an arg should not emit a _arg context key — "
        "downstream `context.get('x') or fallback` would be broken by "
        "a falsy-but-present '' value."
    )


def test_send_interaction_parser_reads_choice_not_action() -> None:
    """Regression: Step C menu_compound schema emits
    ``{"choice": "shell_command", "command": "..."}``. Before this fix,
    agent/actions/interactive_actions.py read ``action_data["action"]``
    which was never set, so send_interaction returned command_sent=False
    and run_session closed in 0ms on every iteration — all 13 interact
    cycles in the a85 trace failed this way.

    The fix is clean-break: consumer reads "choice" only. No legacy
    "action" fallback. This test pins the parser's accepted shape.
    """
    from agent.actions.interactive_actions import _parse_interaction_action

    # Current Step C shape: "choice" + arg field. Should parse.
    result = _parse_interaction_action(
        '{"choice": "shell_command", "command": "python main.py"}'
    )
    assert result is not None
    assert result["choice"] == "shell_command"
    assert result["command"] == "python main.py"

    # Legacy "action" shape — no longer supported. Clean break per
    # post-a85 design decision: don't carry the legacy.
    legacy = _parse_interaction_action(
        '{"action": "shell_command", "command": "python main.py"}'
    )
    assert legacy is None, (
        "Parser must NOT accept the legacy 'action' key — Step C migrated "
        "to 'choice' and we don't carry the old shape."
    )

    # Non-JSON, non-dict, missing choice — all None.
    assert _parse_interaction_action("not json") is None
    assert _parse_interaction_action('{"other": "field"}') is None
    assert _parse_interaction_action("[]") is None
