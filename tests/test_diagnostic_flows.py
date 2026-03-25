"""Tests for Wave 1 diagnostic flows: diagnose_issue and explore_spike.

Tests flow loading, structural validation, action unit tests, and
flow execution integration with MockEffects.
"""

import pytest

from agent.loader import (
    load_flow_with_templates,
    load_template_registry,
    load_all_flows,
)
from agent.models import StepInput, FlowMeta
from agent.effects.mock import MockEffects
from agent.actions.diagnostic_actions import (
    action_compile_diagnosis,
    action_read_investigation_targets,
)
from agent.actions.mission_actions import _infer_flow_from_description

# ── Flow Loading Tests ────────────────────────────────────────────────


class TestDiagnoseIssueLoads:
    """Verify diagnose_issue YAML loads and validates."""

    def _load(self):
        reg = load_template_registry("flows")
        return load_flow_with_templates("flows/tasks/diagnose_issue.yaml", reg)

    def test_loads_successfully(self):
        flow = self._load()
        assert flow.flow == "diagnose_issue"
        assert flow.entry == "gather_context"

    def test_has_required_inputs(self):
        flow = self._load()
        assert "error_description" in flow.input.optional
        assert "target_file_path" in flow.input.required

    def test_has_terminal_and_tail_call_exits(self):
        flow = self._load()
        # complete and diagnosis_failed are tail-call exits
        assert flow.steps["complete"].tail_call is not None
        assert flow.steps["diagnosis_failed"].tail_call is not None

    def test_has_llm_menu_on_evaluate_hypotheses(self):
        flow = self._load()
        step = flow.steps["evaluate_hypotheses"]
        assert step.resolver.type == "llm_menu"
        assert step.resolver.options is not None
        option_names = list(step.resolver.options.keys())
        assert "compile_complete" in option_names
        assert "gather_additional_context" in option_names
        assert "compile_intractable" in option_names

    def test_composes_prepare_context(self):
        flow = self._load()
        # gather_context uses the gather_project_context template
        assert flow.steps["gather_context"].action == "flow"
        assert flow.steps["gather_context"].flow == "prepare_context"

    def test_composes_capture_learnings(self):
        flow = self._load()
        assert flow.steps["capture_diagnosis_learnings"].action == "flow"
        assert flow.steps["capture_diagnosis_learnings"].flow == "capture_learnings"

    def test_has_compile_diagnosis_steps(self):
        flow = self._load()
        assert flow.steps["compile_complete"].action == "compile_diagnosis"
        assert flow.steps["compile_intractable"].action == "compile_diagnosis"

    def test_read_target_uses_template(self):
        flow = self._load()
        assert flow.steps["read_target"].action == "read_files"
        assert "target_file" in flow.steps["read_target"].publishes

    def test_inference_steps_have_prompts(self):
        flow = self._load()
        for step_name in [
            "reproduce_mentally",
            "form_hypotheses",
            "evaluate_hypotheses",
        ]:
            step = flow.steps[step_name]
            assert step.action == "inference"
            assert step.prompt is not None
            assert len(step.prompt) > 100


class TestExploreSpikeLoads:
    """Verify explore_spike YAML loads and validates."""

    def _load(self):
        reg = load_template_registry("flows")
        return load_flow_with_templates("flows/tasks/explore_spike.yaml", reg)

    def test_loads_successfully(self):
        flow = self._load()
        assert flow.flow == "explore_spike"
        assert flow.entry == "plan_investigation"

    def test_has_required_inputs(self):
        flow = self._load()
        assert "investigation_goal" in flow.input.required

    def test_has_tail_call_exit(self):
        flow = self._load()
        assert flow.steps["complete"].tail_call is not None

    def test_has_llm_menu_on_analyze(self):
        flow = self._load()
        step = flow.steps["analyze"]
        assert step.resolver.type == "llm_menu"
        option_names = list(step.resolver.options.keys())
        assert "synthesize" in option_names
        assert "deeper_look" in option_names
        assert "external_research" in option_names

    def test_composes_prepare_context(self):
        flow = self._load()
        assert flow.steps["scan_structure"].action == "flow"
        assert flow.steps["scan_structure"].flow == "prepare_context"

    def test_uses_read_investigation_targets(self):
        flow = self._load()
        assert flow.steps["deep_read"].action == "read_investigation_targets"
        assert "deep_context" in flow.steps["deep_read"].publishes

    def test_composes_research_context(self):
        flow = self._load()
        assert flow.steps["external_research"].action == "flow"
        assert flow.steps["external_research"].flow == "research_context"

    def test_composes_capture_learnings(self):
        flow = self._load()
        assert flow.steps["capture_findings"].action == "flow"
        assert flow.steps["capture_findings"].flow == "capture_learnings"

    def test_inference_steps_have_prompts(self):
        flow = self._load()
        for step_name in ["plan_investigation", "analyze", "synthesize", "deeper_look"]:
            step = flow.steps[step_name]
            assert step.action == "inference"
            assert step.prompt is not None
            assert len(step.prompt) > 100


# ── Action Unit Tests ─────────────────────────────────────────────────


class TestCompileDiagnosis:
    """Test the compile_diagnosis action."""

    @pytest.mark.asyncio
    async def test_assembles_diagnosis_fields(self):
        step_input = StepInput(
            context={
                "error_analysis": "The function fails because X is None",
                "hypotheses": "1. Add null check\n2. Fix upstream",
                "evaluation": "Selected approach 1: add null check",
                "error_description": "TypeError: NoneType has no attribute 'foo'",
            },
            params={"include_rejected_hypotheses": True},
        )
        result = await action_compile_diagnosis(step_input)
        assert result.result["diagnosis_complete"] is True
        assert result.result["is_intractable"] is False
        diagnosis = result.context_updates["diagnosis"]
        assert diagnosis["root_cause"] == "The function fails because X is None"
        assert diagnosis["confidence"] == "medium"
        assert diagnosis["is_intractable"] is False

    @pytest.mark.asyncio
    async def test_marks_intractable(self):
        step_input = StepInput(
            context={
                "error_analysis": "Complex threading issue",
                "error_description": "Race condition",
            },
            params={"mark_as_intractable": True, "include_rejected_hypotheses": True},
        )
        result = await action_compile_diagnosis(step_input)
        assert result.result["is_intractable"] is True
        diagnosis = result.context_updates["diagnosis"]
        assert diagnosis["confidence"] == "low"
        assert diagnosis["is_intractable"] is True

    @pytest.mark.asyncio
    async def test_handles_missing_optional_context(self):
        step_input = StepInput(
            context={
                "error_analysis": "Something went wrong",
                "error_description": "Error",
            },
            params={},
        )
        result = await action_compile_diagnosis(step_input)
        assert result.result["diagnosis_complete"] is True
        diagnosis = result.context_updates["diagnosis"]
        assert diagnosis["hypotheses"] == ""  # Missing, defaults to empty
        assert diagnosis["selected_fix"] == ""


class TestReadInvestigationTargets:
    """Test the read_investigation_targets action."""

    @pytest.mark.asyncio
    async def test_reads_files_from_plan(self):
        effects = MockEffects(
            files={
                "src/models.py": "class User: pass",
                "src/views.py": "def index(): pass",
                "src/utils.py": "def helper(): pass",
            }
        )
        step_input = StepInput(
            context={
                "investigation_plan": (
                    "1. Examine src/models.py for data classes\n"
                    "2. Check src/views.py for route handlers"
                ),
            },
            params={"max_files": 5},
            effects=effects,
        )
        result = await action_read_investigation_targets(step_input)
        assert result.result["files_read"] == 2
        deep_context = result.context_updates["deep_context"]
        assert "src/models.py" in deep_context
        assert "src/views.py" in deep_context
        assert "src/utils.py" not in deep_context

    @pytest.mark.asyncio
    async def test_respects_max_files(self):
        effects = MockEffects(
            files={
                "a.py": "a",
                "b.py": "b",
                "c.py": "c",
            }
        )
        step_input = StepInput(
            context={
                "investigation_plan": "Read a.py, b.py, and c.py",
            },
            params={"max_files": 2},
            effects=effects,
        )
        result = await action_read_investigation_targets(step_input)
        assert result.result["files_read"] == 2

    @pytest.mark.asyncio
    async def test_handles_missing_files(self):
        effects = MockEffects(files={"exists.py": "content"})
        step_input = StepInput(
            context={
                "investigation_plan": "Read exists.py and missing.py",
            },
            params={},
            effects=effects,
        )
        result = await action_read_investigation_targets(step_input)
        assert result.result["files_read"] == 1
        assert "exists.py" in result.context_updates["deep_context"]

    @pytest.mark.asyncio
    async def test_handles_no_effects(self):
        step_input = StepInput(
            context={"investigation_plan": "Read something.py"},
            params={},
            effects=None,
        )
        result = await action_read_investigation_targets(step_input)
        assert result.result["files_read"] == 0

    @pytest.mark.asyncio
    async def test_uses_manifest_paths(self):
        effects = MockEffects(files={"src/engine/core.py": "engine code"})
        step_input = StepInput(
            context={
                "investigation_plan": "Look at the core engine module",
                "project_manifest": {
                    "src/engine/core.py": "class Engine: ...",
                    "src/engine/utils.py": "def util(): ...",
                },
            },
            params={},
            effects=effects,
        )
        result = await action_read_investigation_targets(step_input)
        # "core" appears in plan text, so core.py should be found via manifest matching
        assert result.result["files_read"] >= 1


# ── Flow Type Inference Tests ─────────────────────────────────────────


class TestInferFlowFromDescription:
    """Test the _infer_flow_from_description helper."""

    def test_investigation_keywords(self):
        assert (
            _infer_flow_from_description("Investigate the auth module")
            == "explore_spike"
        )
        assert (
            _infer_flow_from_description("Explore the codebase structure")
            == "explore_spike"
        )
        assert (
            _infer_flow_from_description("Research best practices for caching")
            == "explore_spike"
        )

    def test_diagnosis_keywords(self):
        assert (
            _infer_flow_from_description("Diagnose the login failure")
            == "diagnose_issue"
        )
        assert _infer_flow_from_description("Debug the memory leak") == "diagnose_issue"

    def test_modification_keywords(self):
        assert (
            _infer_flow_from_description("Modify user.py to add validation")
            == "modify_file"
        )
        assert (
            _infer_flow_from_description("Fix the broken import in app.py")
            == "modify_file"
        )
        assert _infer_flow_from_description("Update config handling") == "modify_file"

    def test_test_keywords(self):
        assert (
            _infer_flow_from_description("Write tests for the User model")
            == "create_tests"
        )
        assert (
            _infer_flow_from_description("Add tests for auth module") == "create_tests"
        )

    def test_setup_keywords(self):
        assert (
            _infer_flow_from_description("Setup the project tooling") == "setup_project"
        )
        assert (
            _infer_flow_from_description("Initialize the database config")
            == "setup_project"
        )

    def test_defaults_to_create_file(self):
        assert (
            _infer_flow_from_description("Build the user registration page")
            == "create_file"
        )
        assert (
            _infer_flow_from_description("Implement the payment module")
            == "create_file"
        )


# ── Wiring Tests ──────────────────────────────────────────────────────


class TestModifyFileDiagnosisBranch:
    """Test that modify_file v3 has AST-aware editing and full-rewrite fallback."""

    def _load_modify_file(self):
        reg = load_template_registry("flows")
        return load_flow_with_templates("flows/tasks/modify_file.yaml", reg)

    def test_has_extract_symbols_step(self):
        flow = self._load_modify_file()
        assert "extract_symbols" in flow.steps
        step = flow.steps["extract_symbols"]
        assert step.action == "extract_symbol_bodies"

    def test_has_ast_edit_session(self):
        flow = self._load_modify_file()
        step = flow.steps["ast_edit"]
        assert step.action == "flow"
        assert step.flow == "ast_edit_session"

    def test_validate_routes_to_full_rewrite_on_failure(self):
        flow = self._load_modify_file()
        validate_transitions = [
            r.transition for r in flow.steps["validate"].resolver.rules
        ]
        assert "full_rewrite" in validate_transitions

    def test_full_rewrite_is_inference_fallback(self):
        flow = self._load_modify_file()
        assert flow.steps["full_rewrite"].action == "inference"


# ── All Flows Load Test ───────────────────────────────────────────────


class TestNewFlowsInRegistry:
    """Verify the new flows are discoverable in load_all_flows."""

    def test_diagnose_issue_in_all_flows(self):
        flows = load_all_flows("flows")
        assert "diagnose_issue" in flows

    def test_explore_spike_in_all_flows(self):
        flows = load_all_flows("flows")
        assert "explore_spike" in flows

    def test_total_flow_count_increased(self):
        flows = load_all_flows("flows")
        # Was 15, now 20 with Wave 1 (2) and Wave 2 (3) flows
        assert len(flows) >= 20
