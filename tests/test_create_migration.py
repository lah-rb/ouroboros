"""End-to-end integration tests for the create turn migration (Batch A).

Exercises the real compiled create flow and real templates. Covers:

  - Both generate_content and generate_tests are turn-based code shape
  - Shared-base DRY idiom preserved the correct per-step instruction
  - Code envelope renders with fence-with-path-comment protocol
  - End-to-end execution routes by turn outcome
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
def compiled_create_flow() -> FlowDefinition:
    with open(REPO_ROOT / "flows" / "compiled.json") as f:
        return FlowDefinition.model_validate(json.load(f)["create"])


@pytest.fixture
def real_turn_renderer() -> TurnRenderer:
    return TurnRenderer(REPO_ROOT / "prompts")


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


# ──────────────────────────────────────────────────────────────────────
# Both steps are turn-based with shared shape
# ──────────────────────────────────────────────────────────────────────


def test_generate_content_is_turn_based(compiled_create_flow) -> None:
    step = compiled_create_flow.steps["generate_content"]
    assert step.turn is not None
    assert step.prompt_template is None
    assert step.turn.response_shape == "code"
    assert step.turn.response.language == "python"


def test_generate_tests_is_turn_based(compiled_create_flow) -> None:
    step = compiled_create_flow.steps["generate_tests"]
    assert step.turn is not None
    assert step.prompt_template is None
    assert step.turn.response_shape == "code"
    assert step.turn.response.language == "python"


def test_both_steps_share_section_structure_except_instruction(
    compiled_create_flow,
) -> None:
    """DRY idiom: only the instruction template differs between the two."""
    content_turn = compiled_create_flow.steps["generate_content"].turn
    tests_turn = compiled_create_flow.steps["generate_tests"].turn

    content_types = [s.type for s in content_turn.sections]
    tests_types = [s.type for s in tests_turn.sections]
    assert (
        content_types
        == tests_types
        == [
            "role",
            "problem",
            "target_entity",
            "dependencies",
            "context_files",
            "instruction",
            "envelope",
        ]
    )

    # Same templates for role/problem/dependencies, same ref for target_entity
    for idx in [0, 1, 3]:  # role, problem, dependencies
        assert content_turn.sections[idx].template == tests_turn.sections[idx].template

    # Instruction templates differ
    content_instr = content_turn.sections[5].template
    tests_instr = tests_turn.sections[5].template
    assert content_instr == "create_file/generate_content_instruction"
    assert tests_instr == "create_file/generate_tests_instruction"
    assert content_instr != tests_instr


def test_both_steps_transitions_and_config(compiled_create_flow) -> None:
    for step_name in ["generate_content", "generate_tests"]:
        turn = compiled_create_flow.steps[step_name].turn
        assert turn.transitions.default == "write_files"
        assert turn.transitions.no_answer == "failed"
        assert turn.config["temperature"] == "t*0.4"


# ──────────────────────────────────────────────────────────────────────
# Rendering with real templates
# ──────────────────────────────────────────────────────────────────────


def test_generate_content_renders_all_sections(
    compiled_create_flow, real_turn_renderer
) -> None:
    turn = compiled_create_flow.steps["generate_content"].turn
    prompt = real_turn_renderer.render(
        turn,
        namespaces={
            "input": {
                "flow_directive": "Build a TodoStore",
                "target_file_path": "engine.py",
            },
            "context": {
                "data_contract_block": "Todo: {id: int, title: str}",
                "file_excerpts": "### test_engine.py\n...",
                "architecture_spec": "## Architecture\nRepo pattern.",
            },
            "meta": {},
        },
    )

    assert prompt.startswith("=== CODE EDITOR ===")
    assert "---ACT AS---" in prompt
    assert "code authoring module" in prompt
    assert "## Task" in prompt
    assert "Build a TodoStore" in prompt
    assert "## Target file\nengine.py" in prompt
    assert "Todo: {id: int, title: str}" in prompt
    assert "Repo pattern" in prompt
    assert "Generate the complete content" in prompt
    # Code envelope present with fence-with-path-comment
    assert "```python" in prompt
    assert "# === FILE:" in prompt


def test_generate_tests_uses_tests_instruction(
    compiled_create_flow, real_turn_renderer
) -> None:
    turn = compiled_create_flow.steps["generate_tests"].turn
    prompt = real_turn_renderer.render(
        turn,
        namespaces={
            "input": {
                "flow_directive": "Test the TodoStore",
                "target_file_path": "test_engine.py",
            },
            "context": {
                "data_contract_block": "TodoStore: add/remove",
                "file_excerpts": "### engine.py\nclass TodoStore: ...",
                "architecture_spec": "## Architecture",
            },
            "meta": {},
        },
    )
    # Wording unique to the tests-variant instruction
    assert "pytest conventions" in prompt
    assert "Every assertion is concrete" in prompt
    # Content-variant wording not present
    assert "production-quality code" not in prompt


# ──────────────────────────────────────────────────────────────────────
# End-to-end runtime execution
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_content_end_to_end(compiled_create_flow) -> None:
    from agent.models import FlowMeta, StepInput

    step_def = compiled_create_flow.steps["generate_content"]
    step_input = StepInput(
        task="gen",
        context={},
        config={},
        params={},
        meta=FlowMeta(flow_name="create", step_id="generate_content"),
        effects=None,
    )
    effects = ScriptedInferenceEffects(
        [
            InferenceResult(
                text=(
                    "```python\n"
                    "# === FILE: engine.py ===\n"
                    "class TodoStore:\n"
                    "    pass\n"
                    "```\n"
                ),
                tokens_generated=25,
            )
        ]
    )

    # file_context is the formatter input; we stub a minimal one.
    out = await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=compiled_create_flow,
        inputs={
            "flow_directive": "build",
            "target_file_path": "engine.py",
            "file_context": {},  # pre_compute formatters tolerate empty
        },
        effects=effects,
    )

    assert out.result["turn_outcome"] == "default"
    assert _resolve_turn_transition(step_def.turn, out) == "write_files"


@pytest.mark.asyncio
async def test_generate_content_empty_routes_to_failed(
    compiled_create_flow,
) -> None:
    from agent.models import FlowMeta, StepInput

    step_def = compiled_create_flow.steps["generate_content"]
    step_input = StepInput(
        task="gen",
        context={},
        config={},
        params={},
        meta=FlowMeta(flow_name="create", step_id="generate_content"),
        effects=None,
    )
    effects = ScriptedInferenceEffects(
        [InferenceResult(text="", tokens_generated=0)] * 4  # retries=3 + initial
    )

    out = await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=compiled_create_flow,
        inputs={
            "flow_directive": "build",
            "target_file_path": "engine.py",
            "file_context": {},
        },
        effects=effects,
    )

    assert out.result["turn_outcome"] == "no_answer"
    assert _resolve_turn_transition(step_def.turn, out) == "failed"
