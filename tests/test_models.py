"""Tests for agent/models.py — Pydantic model validation."""

import pytest
from pydantic import ValidationError

from agent.models import (
    ContextRequirements,
    FlowDefinition,
    FlowExecution,
    FlowInput,
    FlowMeta,
    FlowResult,
    OverflowConfig,
    ResolverDefinition,
    RuleCondition,
    StepDefinition,
    StepInput,
    StepOutput,
)

# ── FlowDefinition Tests ─────────────────────────────────────────────


class TestFlowDefinition:
    def _minimal_flow(self, **overrides):
        """Build a minimal valid flow definition dict."""
        base = {
            "flow": "test_flow",
            "steps": {
                "start": {
                    "action": "noop",
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "start",
        }
        base.update(overrides)
        return base

    def test_minimal_flow_is_valid(self):
        flow = FlowDefinition(**self._minimal_flow())
        assert flow.flow == "test_flow"
        assert flow.entry == "start"
        assert flow.version == 1

    def test_entry_step_must_exist(self):
        with pytest.raises(ValidationError, match="Entry step"):
            FlowDefinition(**self._minimal_flow(entry="nonexistent"))

    def test_defaults_are_populated(self):
        flow = FlowDefinition(**self._minimal_flow())
        assert flow.input.required == []
        assert flow.input.optional == []
        assert flow.defaults.config == {}
        assert flow.overflow.strategy == "split"
        assert flow.overflow.fallback == "reorganize"

    def test_flow_with_full_input(self):
        data = self._minimal_flow()
        data["input"] = {
            "required": ["target_file_path"],
            "optional": ["reason"],
        }
        flow = FlowDefinition(**data)
        assert flow.input.required == ["target_file_path"]
        assert flow.input.optional == ["reason"]

    def test_flow_with_defaults_config(self):
        data = self._minimal_flow()
        data["defaults"] = {"config": {"temperature": 0.5}}
        flow = FlowDefinition(**data)
        assert flow.defaults.config["temperature"] == 0.5


# ── StepDefinition Tests ─────────────────────────────────────────────


class TestStepDefinition:
    def test_terminal_requires_status(self):
        with pytest.raises(ValidationError, match="status"):
            StepDefinition(action="noop", terminal=True)

    def test_terminal_with_status_is_valid(self):
        step = StepDefinition(action="noop", terminal=True, status="success")
        assert step.terminal is True
        assert step.status == "success"

    def test_non_terminal_step_no_status_needed(self):
        step = StepDefinition(
            action="noop",
            resolver=ResolverDefinition(
                type="rule",
                rules=[RuleCondition(condition="true", transition="next")],
            ),
        )
        assert step.terminal is False
        assert step.status is None

    def test_step_with_context_requirements(self):
        step = StepDefinition(
            action="transform",
            context=ContextRequirements(
                required=["target_file"],
                optional=["related_files"],
            ),
            terminal=True,
            status="success",
        )
        assert step.context.required == ["target_file"]
        assert step.context.optional == ["related_files"]

    def test_step_with_params(self):
        step = StepDefinition(
            action="read_files",
            params={"target": "/some/path", "discover_imports": True},
            terminal=True,
            status="success",
        )
        assert step.params["target"] == "/some/path"
        assert step.params["discover_imports"] is True

    def test_step_with_publishes(self):
        step = StepDefinition(
            action="read_files",
            publishes=["target_file", "related_files"],
            terminal=True,
            status="success",
        )
        assert step.publishes == ["target_file", "related_files"]


# ── ResolverDefinition Tests ─────────────────────────────────────────


class TestResolverDefinition:
    def test_rule_resolver(self):
        resolver = ResolverDefinition(
            type="rule",
            rules=[
                RuleCondition(condition="result.ok == true", transition="next"),
                RuleCondition(condition="true", transition="fallback"),
            ],
        )
        assert resolver.type == "rule"
        assert len(resolver.rules) == 2
        assert resolver.rules[0].condition == "result.ok == true"
        assert resolver.rules[0].transition == "next"

    def test_empty_rules_list(self):
        resolver = ResolverDefinition(type="rule")
        assert resolver.rules == []


# ── StepInput / StepOutput Tests ──────────────────────────────────────


class TestStepIO:
    def test_step_input_defaults(self):
        si = StepInput()
        assert si.task == ""
        assert si.context == {}
        assert si.config == {}
        assert si.params == {}
        assert si.meta.flow_name == ""

    def test_step_input_with_data(self):
        si = StepInput(
            task="Read the file",
            context={"target_file": {"path": "foo.py", "content": "hello"}},
            config={"temperature": 0.1},
            params={"target": "foo.py"},
            meta=FlowMeta(flow_name="test", step_id="read"),
        )
        assert si.task == "Read the file"
        assert si.context["target_file"]["path"] == "foo.py"
        assert si.meta.flow_name == "test"

    def test_step_output_defaults(self):
        so = StepOutput()
        assert so.result == {}
        assert so.observations == ""
        assert so.context_updates == {}
        assert so.transition_hint is None

    def test_step_output_with_data(self):
        so = StepOutput(
            result={"file_found": True},
            observations="Read 100 chars",
            context_updates={"target_file": {"path": "x", "content": "y"}},
            transition_hint="next_step",
        )
        assert so.result["file_found"] is True
        assert so.context_updates["target_file"]["path"] == "x"


# ── FlowResult / FlowExecution Tests ─────────────────────────────────


class TestFlowResult:
    def test_flow_result(self):
        fr = FlowResult(
            status="success",
            result={"completed": True},
            steps_executed=["step1", "step2"],
        )
        assert fr.status == "success"
        assert len(fr.steps_executed) == 2

    def test_flow_execution_defaults(self):
        fe = FlowExecution(flow_name="test", current_step="start")
        assert fe.step_count == 0
        assert fe.max_steps == 100
        assert fe.accumulator == {}
