"""Tests for shared sub-flow YAML definitions and flow loading with templates.

Validates that all shared sub-flows and task flows load correctly,
that template expansion produces valid flow structures, and that
the directory-recursive loading works.
"""

import pytest

from agent.loader import (
    FlowLoadError,
    load_all_flows,
    load_flow,
    load_flow_with_templates,
    load_template_registry,
)

# ── Shared Sub-Flow Loading Tests ─────────────────────────────────────


class TestSharedFlowsLoad:
    """Verify each shared sub-flow YAML parses and validates."""

    def test_prepare_context_loads(self):
        flow = load_flow("flows/shared/prepare_context.yaml")
        assert flow.flow == "prepare_context"
        assert flow.entry == "scan_workspace"
        assert "complete" in flow.steps
        assert flow.steps["complete"].terminal is True

    def test_validate_output_loads(self):
        flow = load_flow("flows/shared/validate_output.yaml")
        assert flow.flow == "validate_output"
        assert flow.entry == "determine_strategy"
        assert "complete_pass" in flow.steps
        assert "complete_fail" in flow.steps

    def test_capture_learnings_loads(self):
        flow = load_flow("flows/shared/capture_learnings.yaml")
        assert flow.flow == "capture_learnings"
        assert flow.entry == "reflect"
        assert "save_note" in flow.steps
        assert "complete" in flow.steps

    def test_research_context_loads(self):
        flow = load_flow("flows/shared/research_context.yaml")
        assert flow.flow == "research_context"
        assert flow.entry == "formulate_query"
        assert "execute_search" in flow.steps
        assert "complete" in flow.steps

    def test_revise_plan_loads(self):
        flow = load_flow("flows/shared/revise_plan.yaml")
        assert flow.flow == "revise_plan"
        assert flow.entry == "load_current_plan"
        assert "apply_revision" in flow.steps
        assert "complete" in flow.steps


# ── Task Flow Loading Tests ───────────────────────────────────────────


class TestTaskFlowsLoad:
    """Verify task flows load with template expansion."""

    def _load_with_templates(self, path):
        reg = load_template_registry("flows")
        return load_flow_with_templates(path, reg)

    def test_create_file_loads(self):
        flow = self._load_with_templates("flows/tasks/create_file.yaml")
        assert flow.flow == "create_file"
        assert flow.entry == "gather_context"
        # Template-expanded steps should have actions
        assert flow.steps["gather_context"].action == "flow"
        assert flow.steps["write_file"].action == "execute_file_creation"
        assert flow.steps["validate"].action == "flow"
        assert flow.steps["capture_learnings"].action == "flow"
        # Should have tail_calls for completion
        assert flow.steps["complete"].tail_call is not None
        assert flow.steps["failed"].tail_call is not None

    def test_modify_file_loads(self):
        flow = self._load_with_templates("flows/tasks/modify_file.yaml")
        assert flow.flow == "modify_file"
        assert flow.entry == "gather_context"
        assert flow.steps["read_target"].action == "read_files"
        assert flow.steps["plan_change"].action == "inference"
        assert flow.steps["execute_change"].action == "inference"
        # LLM menu on plan_change
        assert flow.steps["plan_change"].resolver.type == "llm_menu"
        # Tail calls
        assert flow.steps["complete"].tail_call is not None
        assert flow.steps["abandon"].tail_call is not None

    def test_create_file_template_expansion_inherits_context(self):
        flow = self._load_with_templates("flows/tasks/create_file.yaml")
        # write_file template requires inference_response in context
        assert "inference_response" in flow.steps["write_file"].context.required

    def test_modify_file_template_expansion_inherits_publishes(self):
        flow = self._load_with_templates("flows/tasks/modify_file.yaml")
        # read_target_file template publishes target_file
        assert "target_file" in flow.steps["read_target"].publishes


# ── Recursive Directory Loading Tests ─────────────────────────────────


class TestRecursiveLoading:
    """Verify load_all_flows handles subdirectories."""

    def test_loads_all_flows_including_subdirs(self):
        flows = load_all_flows("flows")
        # Should have flows from root, shared/, and tasks/
        assert "mission_control" in flows  # root
        assert "prepare_context" in flows  # shared/
        assert "create_file" in flows  # tasks/
        assert "modify_file" in flows  # tasks/
        assert "capture_learnings" in flows  # shared/
        assert "validate_output" in flows  # shared/
        assert "research_context" in flows  # shared/
        assert "revise_plan" in flows  # shared/

    def test_no_duplicate_flow_names(self):
        flows = load_all_flows("flows")
        # This would raise FlowLoadError if duplicates exist
        assert len(flows) >= 11  # at least 11 flows total

    def test_test_flows_still_load(self):
        flows = load_all_flows("flows")
        assert "test_simple" in flows
        assert "test_branching" in flows
        assert "test_inference" in flows

    def test_step_templates_not_loaded_as_flow(self):
        flows = load_all_flows("flows")
        # step_templates.yaml should be skipped
        for name in flows:
            assert name != "step_templates"


# ── Flow Structure Validation Tests ───────────────────────────────────


class TestFlowStructure:
    """Verify structural properties of the new flows."""

    def test_all_shared_flows_have_terminal_states(self):
        shared_flows = [
            "flows/shared/prepare_context.yaml",
            "flows/shared/validate_output.yaml",
            "flows/shared/capture_learnings.yaml",
            "flows/shared/research_context.yaml",
            "flows/shared/revise_plan.yaml",
        ]
        for path in shared_flows:
            flow = load_flow(path)
            terminals = [name for name, step in flow.steps.items() if step.terminal]
            assert len(terminals) >= 1, f"{path} has no terminal steps"

    def test_task_flows_have_tail_calls(self):
        reg = load_template_registry("flows")
        task_flows = [
            "flows/tasks/create_file.yaml",
            "flows/tasks/modify_file.yaml",
        ]
        for path in task_flows:
            flow = load_flow_with_templates(path, reg)
            tail_calls = [
                name for name, step in flow.steps.items() if step.tail_call is not None
            ]
            assert len(tail_calls) >= 1, f"{path} has no tail_call steps"

    def test_prepare_context_has_research_branch(self):
        flow = load_flow("flows/shared/prepare_context.yaml")
        # check_research_needed should have path to research step
        check_step = flow.steps["check_research_needed"]
        transitions = [r.transition for r in check_step.resolver.rules]
        assert "research" in transitions
        assert "select_relevant" in transitions

    def test_validate_output_has_pass_and_fail_terminals(self):
        flow = load_flow("flows/shared/validate_output.yaml")
        assert flow.steps["complete_pass"].terminal is True
        assert flow.steps["complete_pass"].status == "success"
        assert flow.steps["complete_fail"].terminal is True
        assert flow.steps["complete_fail"].status == "failed"

    def test_capture_learnings_uses_push_note_action(self):
        flow = load_flow("flows/shared/capture_learnings.yaml")
        assert flow.steps["save_note"].action == "push_note"

    def test_research_context_uses_curl_search(self):
        flow = load_flow("flows/shared/research_context.yaml")
        assert flow.steps["execute_search"].action == "curl_search"
