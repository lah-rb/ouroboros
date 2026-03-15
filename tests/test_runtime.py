"""Tests for agent/runtime.py — flow executor end-to-end tests."""

import os
import tempfile

import pytest

from agent.actions.registry import build_action_registry
from agent.loader import load_flow, load_flow_from_dict
from agent.runtime import (
    FlowRuntimeError,
    MaxStepsExceeded,
    MissingContextError,
    MissingInputError,
    execute_flow,
)

# ── Helpers ───────────────────────────────────────────────────────────


@pytest.fixture
def action_registry():
    """Build a fresh action registry with built-in test actions."""
    return build_action_registry()


def _simple_two_step_flow():
    """A minimal two-step flow: noop → terminal."""
    return {
        "flow": "two_step",
        "steps": {
            "start": {
                "action": "noop",
                "resolver": {
                    "type": "rule",
                    "rules": [
                        {"condition": "true", "transition": "done"},
                    ],
                },
            },
            "done": {
                "action": "log_completion",
                "params": {"message": "All done"},
                "terminal": True,
                "status": "success",
                "publishes": ["summary"],
            },
        },
        "entry": "start",
    }


# ── Basic Execution Tests ─────────────────────────────────────────────


class TestBasicExecution:
    @pytest.mark.asyncio
    async def test_single_terminal_step(self, action_registry):
        """A flow with just one terminal step should execute and return."""
        flow = load_flow_from_dict(
            {
                "flow": "single_step",
                "steps": {
                    "only": {
                        "action": "log_completion",
                        "params": {"message": "done"},
                        "terminal": True,
                        "status": "success",
                        "publishes": ["summary"],
                    },
                },
                "entry": "only",
            }
        )
        result = await execute_flow(flow, {}, action_registry)
        assert result.status == "success"
        assert result.result["completed"] is True
        assert result.steps_executed == ["only"]

    @pytest.mark.asyncio
    async def test_two_step_flow(self, action_registry):
        """A two-step flow should transition and complete."""
        flow = load_flow_from_dict(_simple_two_step_flow())
        result = await execute_flow(flow, {}, action_registry)
        assert result.status == "success"
        assert result.steps_executed == ["start", "done"]

    @pytest.mark.asyncio
    async def test_observations_collected(self, action_registry):
        """Observations from each step should be collected in the result."""
        flow = load_flow_from_dict(_simple_two_step_flow())
        result = await execute_flow(flow, {}, action_registry)
        assert len(result.observations) >= 1


# ── Context Accumulation Tests ────────────────────────────────────────


class TestContextAccumulation:
    @pytest.mark.asyncio
    async def test_context_flows_between_steps(self, action_registry):
        """Context published by step A should be available to step B."""
        flow = load_flow_from_dict(
            {
                "flow": "context_flow",
                "steps": {
                    "produce": {
                        "action": "transform",
                        "params": {
                            "set_values": {"data": "hello from produce"},
                        },
                        "publishes": ["data"],
                        "resolver": {
                            "type": "rule",
                            "rules": [
                                {"condition": "true", "transition": "consume"},
                            ],
                        },
                    },
                    "consume": {
                        "action": "transform",
                        "context": {"required": ["data"]},
                        "params": {
                            "pass_through": ["data"],
                            "set_values": {"consumed": True},
                        },
                        "publishes": ["consumed"],
                        "resolver": {
                            "type": "rule",
                            "rules": [
                                {"condition": "true", "transition": "done"},
                            ],
                        },
                    },
                    "done": {
                        "action": "log_completion",
                        "context": {"required": ["data", "consumed"]},
                        "terminal": True,
                        "status": "success",
                        "publishes": ["summary"],
                    },
                },
                "entry": "produce",
            }
        )
        result = await execute_flow(flow, {}, action_registry)
        assert result.status == "success"
        assert result.context["data"] == "hello from produce"
        assert result.context["consumed"] is True

    @pytest.mark.asyncio
    async def test_inputs_available_in_accumulator(self, action_registry):
        """Flow inputs should be available in the context accumulator."""
        flow = load_flow_from_dict(
            {
                "flow": "input_access",
                "input": {"required": ["name"], "optional": []},
                "steps": {
                    "check": {
                        "action": "noop",
                        "resolver": {
                            "type": "rule",
                            "rules": [
                                {
                                    "condition": "context.name == 'test'",
                                    "transition": "done",
                                },
                                {"condition": "true", "transition": "fail"},
                            ],
                        },
                    },
                    "done": {
                        "action": "log_completion",
                        "terminal": True,
                        "status": "success",
                        "publishes": ["summary"],
                    },
                    "fail": {
                        "action": "log_completion",
                        "terminal": True,
                        "status": "failed",
                        "publishes": ["summary"],
                    },
                },
                "entry": "check",
            }
        )
        result = await execute_flow(flow, {"name": "test"}, action_registry)
        assert result.status == "success"

    @pytest.mark.asyncio
    async def test_context_filtering(self, action_registry):
        """Steps should only see context keys they've declared interest in."""
        flow = load_flow_from_dict(
            {
                "flow": "context_filter",
                "steps": {
                    "produce": {
                        "action": "transform",
                        "params": {
                            "set_values": {
                                "visible": "yes",
                                "invisible": "no",
                            },
                        },
                        "publishes": ["visible", "invisible"],
                        "resolver": {
                            "type": "rule",
                            "rules": [
                                {"condition": "true", "transition": "consume"},
                            ],
                        },
                    },
                    "consume": {
                        "action": "check_condition",
                        # Only declares 'visible', not 'invisible'
                        "context": {
                            "required": ["visible"],
                            "optional": [],
                        },
                        "params": {
                            "field": "visible",
                            "expected": "yes",
                        },
                        "resolver": {
                            "type": "rule",
                            "rules": [
                                {"condition": "true", "transition": "done"},
                            ],
                        },
                    },
                    "done": {
                        "action": "log_completion",
                        "terminal": True,
                        "status": "success",
                        "publishes": ["summary"],
                    },
                },
                "entry": "produce",
            }
        )
        result = await execute_flow(flow, {}, action_registry)
        assert result.status == "success"


# ── Branching Tests ───────────────────────────────────────────────────


class TestBranching:
    @pytest.mark.asyncio
    async def test_fast_branch(self, action_registry):
        """The branching flow should take the fast path when mode='fast'."""
        flow = load_flow("flows/test_branching.yaml")
        result = await execute_flow(flow, {"mode": "fast"}, action_registry)
        assert result.status == "success"
        assert result.context.get("route_taken") == "fast"
        assert "fast_path" in result.steps_executed

    @pytest.mark.asyncio
    async def test_slow_branch(self, action_registry):
        """The branching flow should take the slow path (2 steps) when mode='slow'."""
        flow = load_flow("flows/test_branching.yaml")
        result = await execute_flow(flow, {"mode": "slow"}, action_registry)
        assert result.status == "success"
        assert result.context.get("route_taken") == "slow"
        assert "slow_path" in result.steps_executed
        assert "slow_path_2" in result.steps_executed

    @pytest.mark.asyncio
    async def test_default_branch(self, action_registry):
        """Unknown modes should take the default path."""
        flow = load_flow("flows/test_branching.yaml")
        result = await execute_flow(flow, {"mode": "unknown"}, action_registry)
        assert result.status == "success"
        assert result.context.get("route_taken") == "default"
        assert "default_path" in result.steps_executed


# ── File I/O Flow Tests ──────────────────────────────────────────────


class TestFileIOFlow:
    @pytest.mark.asyncio
    async def test_read_existing_file(self, action_registry):
        """The simple flow should successfully read an existing file."""
        flow = load_flow("flows/test_simple.yaml")
        result = await execute_flow(
            flow, {"target_file_path": "pyproject.toml"}, action_registry
        )
        assert result.status == "success"
        assert "read_file" in result.steps_executed
        assert "process_content" in result.steps_executed
        assert "complete" in result.steps_executed

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, action_registry):
        """The simple flow should branch to file_not_found for missing files."""
        flow = load_flow("flows/test_simple.yaml")
        result = await execute_flow(
            flow,
            {"target_file_path": "definitely_not_a_file.xyz"},
            action_registry,
        )
        assert result.status == "failed"
        assert "file_not_found" in result.steps_executed

    @pytest.mark.asyncio
    async def test_file_content_in_context(self, action_registry):
        """After reading a file, its content should be in the context."""
        flow = load_flow("flows/test_simple.yaml")
        result = await execute_flow(
            flow, {"target_file_path": "pyproject.toml"}, action_registry
        )
        target_file = result.context.get("target_file", {})
        assert target_file.get("path") == "pyproject.toml"
        assert "ouroboros" in target_file.get("content", "")


# ── Template Rendering in Params ──────────────────────────────────────


class TestParamRendering:
    @pytest.mark.asyncio
    async def test_input_template_in_params(self, action_registry):
        """Params with {{ input.x }} should be rendered correctly."""
        flow = load_flow("flows/test_simple.yaml")
        # The test_simple flow uses {{ input.target_file_path }} in params
        result = await execute_flow(
            flow, {"target_file_path": "pyproject.toml"}, action_registry
        )
        # If template rendering failed, the action would get the raw template
        # string and file read would fail
        assert result.status == "success"


# ── Error Handling Tests ──────────────────────────────────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_missing_required_input_raises(self, action_registry):
        """Missing required inputs should raise MissingInputError."""
        flow = load_flow_from_dict(
            {
                "flow": "requires_input",
                "input": {"required": ["name"], "optional": []},
                "steps": {
                    "start": {
                        "action": "noop",
                        "terminal": True,
                        "status": "success",
                    },
                },
                "entry": "start",
            }
        )
        with pytest.raises(MissingInputError, match="name"):
            await execute_flow(flow, {}, action_registry)

    @pytest.mark.asyncio
    async def test_missing_context_key_raises(self, action_registry):
        """A step requiring a context key that's not in the accumulator should raise."""
        from agent.models import (
            FlowDefinition,
            StepDefinition,
            ContextRequirements,
            ResolverDefinition,
            RuleCondition,
        )

        # Build directly to bypass semantic validation.
        # Step 'start' runs noop (publishes nothing), then step 'need_data'
        # requires 'data' which was never published → MissingContextError.
        flow = FlowDefinition(
            flow="missing_context",
            steps={
                "start": StepDefinition(
                    action="noop",
                    resolver=ResolverDefinition(
                        type="rule",
                        rules=[RuleCondition(condition="true", transition="need_data")],
                    ),
                ),
                "need_data": StepDefinition(
                    action="noop",
                    context=ContextRequirements(required=["nonexistent_key"]),
                    terminal=True,
                    status="success",
                ),
            },
            entry="start",
        )
        with pytest.raises(MissingContextError, match="nonexistent_key"):
            await execute_flow(flow, {}, action_registry)

    @pytest.mark.asyncio
    async def test_max_steps_guard(self, action_registry):
        """A flow that loops forever should be stopped by the max steps guard."""
        from agent.models import (
            FlowDefinition,
            StepDefinition,
            ResolverDefinition,
            RuleCondition,
        )

        # Build directly — semantic validation would reject this (no reachable terminal)
        flow = FlowDefinition(
            flow="infinite_loop",
            steps={
                "loop": StepDefinition(
                    action="noop",
                    resolver=ResolverDefinition(
                        type="rule",
                        rules=[RuleCondition(condition="true", transition="loop")],
                    ),
                ),
                "unreachable": StepDefinition(
                    action="noop", terminal=True, status="success"
                ),
            },
            entry="loop",
        )
        with pytest.raises(MaxStepsExceeded):
            await execute_flow(flow, {}, action_registry, max_steps=10)

    @pytest.mark.asyncio
    async def test_unknown_action_raises(self, action_registry):
        """Referencing an unregistered action should raise."""
        flow = load_flow_from_dict(
            {
                "flow": "bad_action",
                "steps": {
                    "start": {
                        "action": "completely_unknown_action",
                        "terminal": True,
                        "status": "success",
                    },
                },
                "entry": "start",
            }
        )
        with pytest.raises(FlowRuntimeError, match="completely_unknown_action"):
            await execute_flow(flow, {}, action_registry)


# ── Config Merging Tests ──────────────────────────────────────────────


class TestConfigMerging:
    @pytest.mark.asyncio
    async def test_flow_defaults_passed_to_step(self, action_registry):
        """Flow-level config defaults should be available in step input."""
        # We need a custom action that inspects its config
        from agent.models import StepInput, StepOutput

        received_config = {}

        async def inspect_config(step_input: StepInput) -> StepOutput:
            received_config.update(step_input.config)
            return StepOutput(result={"inspected": True})

        action_registry.register("inspect_config", inspect_config)

        flow = load_flow_from_dict(
            {
                "flow": "config_test",
                "defaults": {"config": {"temperature": 0.5, "max_tokens": 100}},
                "steps": {
                    "start": {
                        "action": "inspect_config",
                        "terminal": True,
                        "status": "success",
                    },
                },
                "entry": "start",
            }
        )
        await execute_flow(flow, {}, action_registry)
        assert received_config["temperature"] == 0.5
        assert received_config["max_tokens"] == 100

    @pytest.mark.asyncio
    async def test_step_config_overrides_defaults(self, action_registry):
        """Step-level config should override flow-level defaults."""
        from agent.models import StepInput, StepOutput

        received_config = {}

        async def inspect_config(step_input: StepInput) -> StepOutput:
            received_config.update(step_input.config)
            return StepOutput(result={"inspected": True})

        action_registry.register("inspect_config", inspect_config)

        flow = load_flow_from_dict(
            {
                "flow": "config_override",
                "defaults": {"config": {"temperature": 0.5, "max_tokens": 100}},
                "steps": {
                    "start": {
                        "action": "inspect_config",
                        "config": {"temperature": 0.1},
                        "terminal": True,
                        "status": "success",
                    },
                },
                "entry": "start",
            }
        )
        await execute_flow(flow, {}, action_registry)
        assert received_config["temperature"] == 0.1  # overridden
        assert received_config["max_tokens"] == 100  # inherited
