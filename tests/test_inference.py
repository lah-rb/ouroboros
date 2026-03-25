"""Tests for Phase 3 inference integration.

Tests the inference effect, relative temperature resolution,
and the inference action type in the runtime.
"""

import pytest

from agent.effects.inference import (
    InferenceEffect,
    InferenceError,
    resolve_temperature,
)
from agent.effects.protocol import InferenceResult
from agent.effects.mock import MockEffects
from agent.actions.registry import build_action_registry
from agent.loader import load_flow
from agent.runtime import execute_flow, FlowRuntimeError

# ── Temperature Resolution Tests ──────────────────────────────────────


class TestResolveTemperature:
    """Tests for resolve_temperature()."""

    def test_absolute_float(self):
        assert resolve_temperature(0.1) == 0.1

    def test_absolute_int(self):
        assert resolve_temperature(0) == 0.0

    def test_absolute_string_float(self):
        assert resolve_temperature("0.5") == 0.5

    def test_relative_half(self):
        result = resolve_temperature("t*0.5", model_default=0.7)
        assert abs(result - 0.35) < 0.001

    def test_relative_double(self):
        result = resolve_temperature("t*2.0", model_default=0.4)
        assert abs(result - 0.8) < 0.001

    def test_relative_1_2(self):
        result = resolve_temperature("t*1.2", model_default=0.7)
        assert abs(result - 0.84) < 0.001

    def test_none_returns_none(self):
        assert resolve_temperature(None) is None

    def test_invalid_string_raises(self):
        with pytest.raises(InferenceError, match="Invalid temperature"):
            resolve_temperature("not_a_temp")

    def test_invalid_type_raises(self):
        with pytest.raises(InferenceError, match="Invalid temperature type"):
            resolve_temperature([1, 2, 3])


# ── MockEffects Inference Tests ───────────────────────────────────────


class TestMockInference:
    """Tests for MockEffects.run_inference()."""

    @pytest.mark.asyncio
    async def test_mock_returns_canned_responses_in_order(self):
        effects = MockEffects(inference_responses=["First response", "Second response"])
        r1 = await effects.run_inference("prompt 1")
        assert r1.text == "First response"
        assert r1.finished is True

        r2 = await effects.run_inference("prompt 2")
        assert r2.text == "Second response"

    @pytest.mark.asyncio
    async def test_mock_returns_default_when_exhausted(self):
        effects = MockEffects(inference_responses=["Only one"])
        await effects.run_inference("prompt 1")
        r2 = await effects.run_inference("prompt 2")
        assert r2.text == "Mock inference response"

    @pytest.mark.asyncio
    async def test_mock_records_inference_calls(self):
        effects = MockEffects(inference_responses=["response"])
        await effects.run_inference("test prompt", {"temperature": 0.5})
        calls = effects.calls_to("run_inference")
        assert len(calls) == 1
        assert "test prompt" in calls[0].args["prompt"]

    @pytest.mark.asyncio
    async def test_mock_with_no_responses_uses_default(self):
        effects = MockEffects()
        result = await effects.run_inference("hello")
        assert result.text == "Mock inference response"
        assert result.tokens_generated > 0


# ── Inference Action in Runtime ───────────────────────────────────────


class TestInferenceActionInRuntime:
    """Tests for the special 'inference' action type in the flow runtime."""

    @pytest.mark.asyncio
    async def test_inference_action_renders_prompt_and_calls_inference(self):
        """Flow with action: inference renders prompt template and calls effects."""
        effects = MockEffects(
            files={"test.txt": "Hello world content"},
            inference_responses=["This is a summary of the file."],
        )
        registry = build_action_registry()
        flow = load_flow("flows/test_inference.yaml")

        # The LLM menu resolver will also consume an inference response,
        # so provide enough. The menu will get "complete" as a response.
        effects._inference_responses = [
            "This is a summary of the file.",
            "complete",
        ]
        effects._inference_index = 0

        result = await execute_flow(
            flow,
            {"target_file_path": "test.txt"},
            registry,
            effects=effects,
        )

        assert result.status == "success"
        assert "read_file" in result.steps_executed
        assert "summarize" in result.steps_executed
        assert "complete" in result.steps_executed

        # Verify inference was called
        inf_calls = effects.calls_to("run_inference")
        assert len(inf_calls) >= 1  # At least the summarize call

    @pytest.mark.asyncio
    async def test_inference_action_file_not_found_path(self):
        """Flow should take the file_not_found branch for missing files."""
        effects = MockEffects()
        registry = build_action_registry()
        flow = load_flow("flows/test_inference.yaml")

        result = await execute_flow(
            flow,
            {"target_file_path": "nonexistent.xyz"},
            registry,
            effects=effects,
        )

        assert result.status == "failed"
        assert "file_not_found" in result.steps_executed

    @pytest.mark.asyncio
    async def test_inference_action_without_effects_raises(self):
        """Inference action requires effects with run_inference.

        Note: The first step (read_files) has a direct I/O fallback when
        effects=None, so we need to ensure the file exists on disk to reach
        the inference step. Instead, we test with an effects object that
        lacks run_inference.
        """

        class NoInferenceEffects:
            """Effects-like object that has read_file but not run_inference."""

            async def read_file(self, path):
                from agent.effects.protocol import FileContent

                return FileContent(path=path, content="hello", size=5, exists=True)

            def get_log(self):
                return []

            def clear_log(self):
                pass

        registry = build_action_registry()
        flow = load_flow("flows/test_inference.yaml")

        with pytest.raises(FlowRuntimeError, match="requires effects"):
            await execute_flow(
                flow,
                {"target_file_path": "test.txt"},
                registry,
                effects=NoInferenceEffects(),
            )

    @pytest.mark.asyncio
    async def test_inference_action_deeper_analysis_path(self):
        """Flow should go through analyze_deeper when LLM picks that option."""
        effects = MockEffects(
            files={"test.txt": "Some code content"},
            inference_responses=[
                "Initial summary of the code.",
                "b",  # LLM menu picks deeper analysis (b = analyze_deeper)
                "Detailed analysis of patterns and structure.",
            ],
        )
        registry = build_action_registry()
        flow = load_flow("flows/test_inference.yaml")

        result = await execute_flow(
            flow,
            {"target_file_path": "test.txt"},
            registry,
            effects=effects,
        )

        assert result.status == "success"
        assert "analyze_deeper" in result.steps_executed
        assert "complete_deep" in result.steps_executed
