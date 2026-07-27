"""End-to-end integration test for the set_env turn migration (Phase 5).

Exercises the real compiled flow definition and the real templates
shipped in `prompts/set_env/` and `prompts/personas/env_detector.yaml`,
rather than fixture stubs. Confirms:

  - The migrated set_env.cue compiles to a TurnDefinition Pydantic
    instance cleanly.
  - TurnRenderer can load every template the turn references.
  - The rendered prompt contains the banner, persona, evidence, target
    file, instruction, and a concrete envelope example.
  - MockEffects-style execution routes through _execute_turn_inference
    and picks transitions.default on a non-empty inference response.
"""

from __future__ import annotations

import pytest

from agent.effects.protocol import InferenceResult
from agent.runtime import _execute_turn_inference, _resolve_turn_transition
from tests.conftest import ScriptedInferenceEffects

pytestmark = pytest.mark.usefixtures("real_schema_registry")


def test_set_env_detect_tooling_is_turn_based(compiled_set_env_flow) -> None:
    """Migration landed: detect_tooling has `turn`, not `prompt_template`."""
    step = compiled_set_env_flow.steps["detect_tooling"]
    assert step.turn is not None
    assert step.prompt_template is None
    assert step.turn.response_shape == "json_document"


def test_set_env_turn_has_expected_sections(compiled_set_env_flow) -> None:
    """Section list matches Site #19's record."""
    turn = compiled_set_env_flow.steps["detect_tooling"].turn
    section_types = [s.type for s in turn.sections]
    assert section_types == [
        "role",
        "evidence",
        "problem",
        "instruction",
        "envelope",
    ]

    # Target file problem uses ref + title override
    problem = turn.sections[2]
    assert problem.ref is not None
    assert problem.ref.ref == "input.target_file_path"
    assert problem.title == "Target file"


def test_set_env_turn_transitions(compiled_set_env_flow) -> None:
    """Non-empty response routes to persist_env; no_answer routes to failed."""
    turn = compiled_set_env_flow.steps["detect_tooling"].turn
    assert turn.transitions.default == "persist_env"
    assert turn.transitions.no_answer == "failed"


def test_set_env_turn_temperature_at_zero(compiled_set_env_flow) -> None:
    """Site #19's deliberate-determinism choice survived migration."""
    turn = compiled_set_env_flow.steps["detect_tooling"].turn
    assert turn.config["temperature"] == "t*0.0"


def test_set_env_turn_schema_id(compiled_set_env_flow) -> None:
    """The response contract references the registered schema."""
    turn = compiled_set_env_flow.steps["detect_tooling"].turn
    # json_document response contract carries schema_id
    assert turn.response.schema_id == "validation_env_config"


def test_set_env_prompt_renders_from_real_templates(
    compiled_set_env_flow, real_turn_renderer
) -> None:
    """TurnRenderer can resolve every template the turn references and
    produce a complete prompt — no missing files, no unresolved refs."""
    turn = compiled_set_env_flow.steps["detect_tooling"].turn

    prompt = real_turn_renderer.render(
        turn,
        namespaces={
            "input": {
                "working_directory": "/home/dev/todo-app",
                "target_file_path": "main.py",
            },
            "context": {
                "project_file_list": "- main.py\n- pyproject.toml",
            },
            "meta": {"flow_name": "set_env", "step_id": "detect_tooling"},
        },
    )

    # Banner at top
    assert prompt.startswith("=== JSON DOCUMENT ===")

    # Persona block from real personas/env_detector.yaml
    assert "---ACT AS---" in prompt
    assert "project environment detection module" in prompt

    # Evidence from real set_env/project_scan.yaml
    assert "Working directory: /home/dev/todo-app" in prompt
    assert "- main.py" in prompt
    assert "pyproject.toml" in prompt

    # Target file section with title override
    assert "## Target file" in prompt
    assert "main.py" in prompt  # the file path itself

    # Instruction from real set_env/detect_tooling_rules.yaml
    assert "install_command" in prompt
    assert "## Rules" in prompt
    # Legacy negation NOISE should not appear — the new template drops it:
    assert "❌" not in prompt
    assert "✅" not in prompt
    # "Return ONLY" is no longer legacy template noise: the json_document
    # ENVELOPE now supplies a closing output-directive after the schema example.
    # Ending on a 40-line JSON blob left the last instruction as "here is a
    # shape" and never "emit only this", and a model inclined to preamble
    # opened with "I'll examine the project files…" — which a JSON extractor
    # reads as nothing. Measured on the exact prompt that failed in production
    # (laguna-S-2.1, 6 reps/arm): 1/6 JSON as-shipped, 6/6 with the directive,
    # identical at t=0.2 and t=1.0. The instruction TEMPLATE still carries none
    # of this; it comes from the envelope.
    assert "Return ONLY the fenced JSON object above" in prompt

    # Envelope renders the x-example from validation_env_config schema
    assert "```json" in prompt
    assert "interactive_prompt" in prompt


def test_set_env_problem_section_omitted_when_no_target_file(
    compiled_set_env_flow, real_turn_renderer
) -> None:
    """When target_file_path is absent, the problem section disappears
    entirely — no empty '## Target file\\n' header left behind."""
    turn = compiled_set_env_flow.steps["detect_tooling"].turn

    prompt = real_turn_renderer.render(
        turn,
        namespaces={
            "input": {
                "working_directory": "/home/dev/project",
                # no target_file_path
            },
            "context": {"project_file_list": "- a.py"},
            "meta": {},
        },
    )

    assert "## Target file" not in prompt
    # But the rest still renders
    assert prompt.startswith("=== JSON DOCUMENT ===")
    assert "---ACT AS---" in prompt
    assert "Working directory: /home/dev/project" in prompt


@pytest.mark.asyncio
async def test_set_env_runs_through_turn_execution(
    compiled_set_env_flow,
) -> None:
    """End-to-end: invoke _execute_turn_inference with the real step
    definition and confirm it renders, calls inference, and classifies
    outcome as 'default' for a non-empty JSON response."""
    from agent.models import FlowMeta, StepInput

    step_def = compiled_set_env_flow.steps["detect_tooling"]
    step_input = StepInput(
        task="detect",
        context={"project_manifest": {"files": ["main.py", "pyproject.toml"]}},
        config={},
        params={},
        meta=FlowMeta(flow_name="set_env", step_id="detect_tooling"),
        effects=None,
    )

    effects = ScriptedInferenceEffects(
        responses=[
            InferenceResult(
                text='```json\n{"py": {"syntax": ["python", "-c", "import py_compile"]}}\n```',
                tokens_generated=20,
                finished=True,
            )
        ]
    )

    step_output = await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=compiled_set_env_flow,
        inputs={
            "mission_id": "m1",
            "working_directory": "/tmp",
            "target_file_path": "main.py",
        },
        effects=effects,
    )

    assert step_output.result["turn_outcome"] == "default"
    assert step_output.result["attempts"] == 1
    assert "inference_response" in step_output.context_updates

    # Transition resolution points to persist_env
    next_step = _resolve_turn_transition(step_def.turn, step_output)
    assert next_step == "persist_env"


@pytest.mark.asyncio
async def test_set_env_empty_response_routes_to_failed(
    compiled_set_env_flow,
) -> None:
    """Empty response after retries → turn_outcome='no_answer' → failed."""
    from agent.models import FlowMeta, StepInput

    step_def = compiled_set_env_flow.steps["detect_tooling"]
    step_input = StepInput(
        task="detect",
        context={"project_manifest": {"files": []}},
        config={},
        params={},
        meta=FlowMeta(flow_name="set_env", step_id="detect_tooling"),
        effects=None,
    )

    # turn.retries=3, so 4 total attempts expected
    effects = ScriptedInferenceEffects(
        responses=[
            InferenceResult(text="", tokens_generated=0, finished=True),
            InferenceResult(text="", tokens_generated=0, finished=True),
            InferenceResult(text="", tokens_generated=0, finished=True),
            InferenceResult(text="", tokens_generated=0, finished=True),
        ]
    )

    step_output = await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=compiled_set_env_flow,
        inputs={
            "mission_id": "m1",
            "working_directory": "/tmp",
            "target_file_path": "main.py",
        },
        effects=effects,
    )

    assert step_output.result["turn_outcome"] == "no_answer"
    assert step_output.result["attempts"] == 4  # retries=3 + initial

    next_step = _resolve_turn_transition(step_def.turn, step_output)
    assert next_step == "failed"
