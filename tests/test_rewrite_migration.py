"""End-to-end integration tests for the rewrite turn migration (Batch A).

Exercises the real compiled rewrite flow and real templates. Covers:

  - generate_rewrite is turn-based code shape
  - Dynamic title "Current File: {input.target_file_path}" substitutes
  - Preservation rules survive into the rendered instruction
  - Temperature bumped from t*0.3 to t*0.4 per Site #15
  - End-to-end execution routes by turn outcome
"""

from __future__ import annotations

import pytest

from agent.effects.protocol import InferenceResult
from agent.runtime import _execute_turn_inference, _resolve_turn_transition
from tests.conftest import ScriptedInferenceEffects

pytestmark = pytest.mark.usefixtures("real_schema_registry")


def test_generate_rewrite_is_turn_based(compiled_rewrite_flow) -> None:
    step = compiled_rewrite_flow.steps["generate_rewrite"]
    assert step.turn is not None
    assert step.prompt_template is None
    assert step.turn.response_shape == "code"
    assert step.turn.response.language == "python"


def test_sections_match_record(compiled_rewrite_flow) -> None:
    """Site #15's section plan."""
    turn = compiled_rewrite_flow.steps["generate_rewrite"].turn
    assert [s.type for s in turn.sections] == [
        "role",
        "problem",
        "target_entity",
        "dependencies",
        "instruction",
        "envelope",
    ]
    # target_entity carries the dynamic title template
    target = turn.sections[2]
    assert target.title == "Current File: {input.target_file_path}"
    assert target.ref.ref == "context.target_file_content"


def test_temperature_bumped_to_point_four(compiled_rewrite_flow) -> None:
    """Site #15 correction: t*0.3 → t*0.4 (code-gen regime)."""
    turn = compiled_rewrite_flow.steps["generate_rewrite"].turn
    assert turn.config["temperature"] == "t*0.4"


def test_transitions(compiled_rewrite_flow) -> None:
    turn = compiled_rewrite_flow.steps["generate_rewrite"].turn
    assert turn.transitions.default == "write_file"
    assert turn.transitions.no_answer == "failed"


def test_dynamic_title_substitutes_in_rendered_prompt(
    compiled_rewrite_flow, real_turn_renderer
) -> None:
    """The title's {input.target_file_path} substitutes into the ##
    heading. This is the feature exercised here for the first time."""
    turn = compiled_rewrite_flow.steps["generate_rewrite"].turn
    prompt = real_turn_renderer.render(
        turn,
        namespaces={
            "input": {
                "flow_directive": "Fix the thing",
                "target_file_path": "app/models/user.py",
                "validation_errors": "MyPy: line 42 type mismatch",
            },
            "context": {
                "target_file_content": "class User: pass\n",
                "file_excerpts": "### related.py\n...",
                "architecture_spec": "## Architecture\nPattern.",
            },
            "meta": {},
        },
    )
    # Title substituted with actual path
    assert "## Current File: app/models/user.py" in prompt
    # Content below the title
    assert "class User: pass" in prompt
    # Validation errors shown
    assert "MyPy: line 42 type mismatch" in prompt
    # Preservation rules visible in the instruction
    assert "Preserve all functionality" in prompt
    assert "Scope discipline" in prompt
    # Legacy noise stripped
    assert "❌" not in prompt
    assert "✅" not in prompt


def test_preservation_rules_survive_in_instruction(
    compiled_rewrite_flow, real_turn_renderer
) -> None:
    turn = compiled_rewrite_flow.steps["generate_rewrite"].turn
    prompt = real_turn_renderer.render(
        turn,
        namespaces={
            "input": {
                "flow_directive": "x",
                "target_file_path": "a.py",
                "validation_errors": "",
            },
            "context": {
                "target_file_content": "x\n",
                "file_excerpts": "",
                "architecture_spec": "",
            },
            "meta": {},
        },
    )
    assert "comparable in size" in prompt
    assert "Do not remove functions" in prompt
    assert "Do not rename symbols" in prompt


@pytest.mark.asyncio
async def test_end_to_end_routing_on_success(compiled_rewrite_flow) -> None:
    from agent.models import FlowMeta, StepInput

    step_def = compiled_rewrite_flow.steps["generate_rewrite"]
    step_input = StepInput(
        task="rewrite",
        # v3: the file body is read by the read_target step into
        # context.target_file (effects-routed, container-safe) and the
        # target_file_content pre_compute sources from it — not from the
        # file_context projection (which host-reads, empty in a container).
        context={"target_file": {"content": "def add_todo(item): _store.append(item)"}},
        config={},
        params={},
        meta=FlowMeta(flow_name="rewrite", step_id="generate_rewrite"),
        effects=None,
    )
    effects = ScriptedInferenceEffects(
        [
            InferenceResult(
                text=(
                    "```python\n"
                    "# === FILE: app/engine.py ===\n"
                    "def add_todo(item):\n"
                    "    if item.id in _store:\n"
                    "        raise ValueError('duplicate')\n"
                    "    _store.append(item)\n"
                    "```\n"
                ),
                tokens_generated=40,
            )
        ]
    )

    out = await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=compiled_rewrite_flow,
        inputs={
            "flow_directive": "add dupe-check",
            "target_file_path": "app/engine.py",
            "validation_errors": "duplicates allowed",
            "file_context": {
                "target_content": "def add_todo(item): _store.append(item)"
            },
        },
        effects=effects,
    )

    assert out.result["turn_outcome"] == "default"
    assert _resolve_turn_transition(step_def.turn, out) == "write_file"
    # The rendered prompt should have carried the dynamic title
    assert "Current File: app/engine.py" in effects.prompts_seen[0]


# ══════════════════════════════════════════════════════════════════════
# Regression: rewrite prompts must carry the data-contract block.
# b6c live-test showed the loader rewritten with a required
# `player_location` field that world.yaml doesn't produce, silently
# drifting the data contract. Root cause: render_data_contracts ran
# in pre_compute producing `data_contract_block`, but no rewrite
# template referenced it, so the block was computed and discarded.
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rewrite_prompt_includes_data_contract_block(
    compiled_rewrite_flow,
) -> None:
    """When file_context carries data_shapes, the rewrite prompt must
    include the DATA CONTRACTS block so the model doesn't drift the
    contract. Without this the model is free to invent required keys
    that aren't in the data file (b6c: loader added required
    `player_location` to a YAML that doesn't have one)."""
    from agent.models import FlowMeta, StepInput

    step_def = compiled_rewrite_flow.steps["generate_rewrite"]
    step_input = StepInput(
        task="rewrite",
        context={},
        config={},
        params={},
        meta=FlowMeta(flow_name="rewrite", step_id="generate_rewrite"),
        effects=None,
    )

    effects = ScriptedInferenceEffects(
        [
            InferenceResult(
                text=(
                    "```python\n"
                    "# === FILE: loader.py ===\n"
                    "def load(path): return yaml.safe_load(open(path))\n"
                    "```\n"
                ),
                tokens_generated=40,
            )
        ]
    )

    await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=compiled_rewrite_flow,
        inputs={
            "flow_directive": "fix loader",
            "target_file_path": "loader.py",
            "validation_errors": "",
            "file_context": {
                "target_content": "def load(path): pass",
                "data_shapes": [
                    {
                        "file": "world.yaml",
                        "consumed_by": "loader.py",
                        "structure": (
                            '{"rooms": [{"id": "str", "name": "str"}], '
                            '"start_room": "str"}'
                        ),
                    }
                ],
            },
        },
        effects=effects,
    )

    prompt = effects.prompts_seen[0]
    # MANDATORY banner appears — this is the renderer's signature.
    assert "DATA CONTRACTS (MANDATORY)" in prompt, (
        "rewrite prompts must surface data contracts. Pre-fix, the "
        "pre_compute render_data_contracts output landed in a context "
        "slot no template read, so the MANDATORY block never reached "
        "the model (b6c regression)."
    )
    # Contract content must actually be there, not just the banner.
    assert "world.yaml" in prompt
    assert "loader.py" in prompt  # the consumer declaration
    assert "start_room" in prompt  # the declared key from the structure
    # The drift-prevention guidance rides along.
    assert "binding" in prompt.lower() or "exact" in prompt.lower()


@pytest.mark.asyncio
async def test_rewrite_prompt_omits_data_contract_block_when_none(
    compiled_rewrite_flow,
) -> None:
    """When file_context has no data_shapes, the MANDATORY banner must
    NOT appear — we don't want empty scaffolding leaking through."""
    from agent.models import FlowMeta, StepInput

    step_def = compiled_rewrite_flow.steps["generate_rewrite"]
    step_input = StepInput(
        task="rewrite",
        context={},
        config={},
        params={},
        meta=FlowMeta(flow_name="rewrite", step_id="generate_rewrite"),
        effects=None,
    )

    effects = ScriptedInferenceEffects(
        [
            InferenceResult(
                text=(
                    "```python\n"
                    "# === FILE: pure_code.py ===\n"
                    "def hello(): return 'hi'\n"
                    "```\n"
                ),
                tokens_generated=20,
            )
        ]
    )

    await _execute_turn_inference(
        step_def=step_def,
        step_input=step_input,
        flow_def=compiled_rewrite_flow,
        inputs={
            "flow_directive": "simplify",
            "target_file_path": "pure_code.py",
            "validation_errors": "",
            "file_context": {
                "target_content": "def hello(): return 'hi'",
                # no data_shapes — this file doesn't touch data files.
            },
        },
        effects=effects,
    )

    prompt = effects.prompts_seen[0]
    assert "DATA CONTRACTS (MANDATORY)" not in prompt, (
        "When there are no data contracts, the banner must not render — "
        "otherwise every file rewrite would carry irrelevant empty "
        "scaffolding."
    )
