"""End-to-end integration tests for the research turn migration (Batch A).

Exercises the real compiled research flow and the real templates in
`prompts/research/` and `prompts/personas/research_*.yaml`. Covers:

  - plan_queries step: json_document shape, research_queries schema
  - summarize step: prose shape, no envelope
  - Both steps' conditional evidence (research_context optional)
  - Prose envelope renders nothing (omitted cleanly)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.effects.protocol import InferenceResult
from agent.models import FlowDefinition
from agent.runtime import _execute_turn_inference, _resolve_turn_transition
from agent.schema_registry import set_default_registry
from agent.turn_renderer import TurnRenderer

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def real_schema_registry():
    set_default_registry(None)
    yield
    set_default_registry(None)


@pytest.fixture
def compiled_research_flow() -> FlowDefinition:
    with open(REPO_ROOT / "flows" / "compiled.json") as f:
        data = json.load(f)
    return FlowDefinition.model_validate(data["research"])


@pytest.fixture
def real_turn_renderer() -> TurnRenderer:
    return TurnRenderer(REPO_ROOT / "prompts")


class ScriptedInferenceEffects:
    def __init__(self, responses: list[InferenceResult]):
        self.responses = responses
        self.calls_made = 0
        self.prompts_seen: list[str] = []

    async def run_inference(self, prompt: str, config_overrides=None):
        self.prompts_seen.append(prompt)
        r = self.responses[self.calls_made]
        self.calls_made += 1
        return r


# ──────────────────────────────────────────────────────────────────────
# plan_queries — json_document shape
# ──────────────────────────────────────────────────────────────────────


def test_plan_queries_is_turn_based(compiled_research_flow) -> None:
    step = compiled_research_flow.steps["plan_queries"]
    assert step.turn is not None
    assert step.prompt_template is None
    assert step.turn.response_shape == "json_document"
    assert step.turn.response.schema_id == "research_queries"


def test_plan_queries_sections_match_record(compiled_research_flow) -> None:
    """Site #13's section plan: role, problem, evidence, instruction, envelope."""
    turn = compiled_research_flow.steps["plan_queries"].turn
    assert [s.type for s in turn.sections] == [
        "role",
        "problem",
        "evidence",
        "instruction",
        "envelope",
    ]
    # problem is ref-sourced from input.research_query
    assert turn.sections[1].ref.ref == "input.research_query"
    # evidence is ref-sourced from input.research_context (optional)
    assert turn.sections[2].ref.ref == "input.research_context"


def test_plan_queries_transitions(compiled_research_flow) -> None:
    turn = compiled_research_flow.steps["plan_queries"].turn
    assert turn.transitions.default == "extract_queries"
    assert turn.transitions.no_answer == "search"


def test_plan_queries_temperature_at_point_six(compiled_research_flow) -> None:
    """Natural-synthesis cluster: t*0.6."""
    turn = compiled_research_flow.steps["plan_queries"].turn
    assert turn.config["temperature"] == "t*0.6"


def test_plan_queries_renders_with_all_inputs(
    compiled_research_flow, real_turn_renderer
) -> None:
    turn = compiled_research_flow.steps["plan_queries"].turn
    prompt = real_turn_renderer.render(
        turn,
        namespaces={
            "input": {
                "research_query": "dialogue tree structures",
                "research_context": "Python 3.12, YAML content",
            },
            "context": {},
            "meta": {},
        },
    )

    # Structure
    assert prompt.startswith("=== JSON DOCUMENT ===")
    assert "---ACT AS---" in prompt
    assert "research planner" in prompt

    # Problem and evidence both render with titles
    assert "## Research question" in prompt
    assert "dialogue tree structures" in prompt
    assert "## Background context" in prompt
    assert "Python 3.12, YAML content" in prompt

    # Instruction
    assert "Good query categories" in prompt
    # Coaching content survived but legacy noise didn't
    assert "User experience insights" in prompt
    assert "Common pitfalls" in prompt
    assert "❌" not in prompt
    assert "✅" not in prompt
    assert "Return ONLY" not in prompt

    # Envelope with example from research_queries schema
    assert "```json" in prompt
    assert "npc dialogue" in prompt.lower()  # from x-example


def test_plan_queries_research_context_omitted_when_absent(
    compiled_research_flow, real_turn_renderer
) -> None:
    """When research_context isn't provided, evidence section vanishes."""
    turn = compiled_research_flow.steps["plan_queries"].turn
    prompt = real_turn_renderer.render(
        turn,
        namespaces={
            "input": {"research_query": "something"},
            "context": {},
            "meta": {},
        },
    )
    assert "## Research question" in prompt
    assert "## Background context" not in prompt


# ──────────────────────────────────────────────────────────────────────
# summarize — prose shape (new territory!)
# ──────────────────────────────────────────────────────────────────────


def test_summarize_is_turn_based_prose(compiled_research_flow) -> None:
    step = compiled_research_flow.steps["summarize"]
    assert step.turn is not None
    assert step.prompt_template is None
    assert step.turn.response_shape == "prose"


def test_summarize_sections_match_record(compiled_research_flow) -> None:
    """Site #14: role, problem, evidence×2 (context + results), instruction, envelope."""
    turn = compiled_research_flow.steps["summarize"].turn
    assert [s.type for s in turn.sections] == [
        "role",
        "problem",
        "evidence",
        "evidence",
        "instruction",
        "envelope",
    ]
    # First evidence is research_context
    assert turn.sections[2].ref.ref == "input.research_context"
    # Second evidence is raw_search_results
    assert turn.sections[3].ref.ref == "context.raw_search_results"


def test_summarize_transitions(compiled_research_flow) -> None:
    turn = compiled_research_flow.steps["summarize"].turn
    assert turn.transitions.default == "done"
    assert turn.transitions.no_answer == "no_results"


def test_summarize_renders_prose_without_envelope(
    compiled_research_flow, real_turn_renderer
) -> None:
    """Prose shape: banner + persona + problem + evidences + instruction.
    No fenced code, no JSON example."""
    turn = compiled_research_flow.steps["summarize"].turn
    prompt = real_turn_renderer.render(
        turn,
        namespaces={
            "input": {"research_query": "structuring trees"},
            "context": {
                "raw_search_results": "[Article 1] YAML works well...",
            },
            "meta": {},
        },
    )

    assert prompt.startswith("=== WRITING ===")
    assert "research synthesis module" in prompt
    assert "## Research question" in prompt
    assert "structuring trees" in prompt
    assert "## Search results" in prompt
    assert "YAML works well" in prompt
    assert "Synthesize the search results" in prompt

    # Crucial: no envelope fragment on prose
    assert "```" not in prompt
    assert "Respond with" not in prompt
    # And no trailing whitespace from an omitted envelope section
    assert not prompt.endswith("\n\n")


def test_summarize_omits_background_when_absent(
    compiled_research_flow, real_turn_renderer
) -> None:
    """research_context is optional; its evidence section omits cleanly
    while raw_search_results (required) still renders."""
    turn = compiled_research_flow.steps["summarize"].turn
    prompt = real_turn_renderer.render(
        turn,
        namespaces={
            "input": {"research_query": "x"},
            "context": {"raw_search_results": "some results"},
            "meta": {},
        },
    )
    assert "## Background context" not in prompt
    assert "## Search results" in prompt


# ──────────────────────────────────────────────────────────────────────
# End-to-end execution through runtime
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_queries_end_to_end(compiled_research_flow) -> None:
    from agent.models import FlowMeta, StepInput

    step_def = compiled_research_flow.steps["plan_queries"]
    step_input = StepInput(
        task="plan",
        context={},
        config={},
        params={},
        meta=FlowMeta(flow_name="research", step_id="plan_queries"),
        effects=None,
    )
    effects = ScriptedInferenceEffects(
        [
            InferenceResult(
                text='```json\n["dialogue tree python", "yaml game content"]\n```',
                tokens_generated=15,
            )
        ]
    )

    out = await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=compiled_research_flow,
        inputs={"research_query": "dialogue trees", "research_context": ""},
        effects=effects,
    )

    assert out.result["turn_outcome"] == "default"
    assert _resolve_turn_transition(step_def.turn, out) == "extract_queries"


@pytest.mark.asyncio
async def test_summarize_end_to_end_prose_response(
    compiled_research_flow,
) -> None:
    from agent.models import FlowMeta, StepInput

    step_def = compiled_research_flow.steps["summarize"]
    step_input = StepInput(
        task="summarize",
        context={"raw_search_results": "[A1] YAML works..."},
        config={},
        params={},
        meta=FlowMeta(flow_name="research", step_id="summarize"),
        effects=None,
    )
    effects = ScriptedInferenceEffects(
        [
            InferenceResult(
                text="YAML works well for dialogue trees because it handles nesting clearly. "
                "State machines are the common pattern for branching logic.",
                tokens_generated=30,
            )
        ]
    )

    out = await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=compiled_research_flow,
        inputs={"research_query": "dialogue trees"},
        effects=effects,
    )

    assert out.result["turn_outcome"] == "default"
    # The prompt sent should NOT contain an envelope
    assert "```" not in effects.prompts_seen[0]
    assert _resolve_turn_transition(step_def.turn, out) == "done"


@pytest.mark.asyncio
async def test_summarize_empty_prose_routes_to_no_results(
    compiled_research_flow,
) -> None:
    from agent.models import FlowMeta, StepInput

    step_def = compiled_research_flow.steps["summarize"]
    step_input = StepInput(
        task="summarize",
        context={"raw_search_results": "[A1] ..."},
        config={},
        params={},
        meta=FlowMeta(flow_name="research", step_id="summarize"),
        effects=None,
    )
    effects = ScriptedInferenceEffects(
        [InferenceResult(text="", tokens_generated=0)] * 4  # retries=3 + initial
    )

    out = await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=compiled_research_flow,
        inputs={"research_query": "x"},
        effects=effects,
    )

    assert out.result["turn_outcome"] == "no_answer"
    assert out.result["attempts"] == 4
    assert _resolve_turn_transition(step_def.turn, out) == "no_results"
