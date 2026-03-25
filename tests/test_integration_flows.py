"""Tests for Wave 2 integration flows: integrate_modules, refactor, document_project.

Tests flow loading, structural validation, action unit tests, and
flow type inference for the new system-level capability flows.
"""

import pytest

from agent.loader import (
    load_flow_with_templates,
    load_template_registry,
    load_all_flows,
)
from agent.models import StepInput
from agent.effects.mock import MockEffects
from agent.actions.integration_actions import (
    action_apply_multi_file_changes,
    action_run_project_tests,
    action_check_remaining_smells,
    action_restore_file_from_context,
    action_check_remaining_doc_tasks,
    _parse_multi_file_output,
)
from agent.actions.mission_actions import _infer_flow_from_description

# ── Helper ────────────────────────────────────────────────────────────


def _load(path):
    reg = load_template_registry("flows")
    return load_flow_with_templates(path, reg)


# ═══════════════════════════════════════════════════════════════════════
# integrate_modules Flow Loading Tests
# ═══════════════════════════════════════════════════════════════════════


class TestIntegrateModulesLoads:
    """Verify integrate_modules YAML loads and validates."""

    def test_loads_successfully(self):
        flow = _load("flows/tasks/integrate_modules.yaml")
        assert flow.flow == "integrate_modules"
        assert flow.entry == "gather_context"

    def test_has_required_inputs(self):
        flow = _load("flows/tasks/integrate_modules.yaml")
        assert "mission_id" in flow.input.required
        assert "task_id" in flow.input.required

    def test_has_tail_call_exits(self):
        flow = _load("flows/tasks/integrate_modules.yaml")
        tail_call_steps = [
            name for name, step in flow.steps.items() if step.tail_call is not None
        ]
        assert len(tail_call_steps) >= 3  # complete, nothing_to_inspect, failed

    def test_uses_structural_check(self):
        flow = _load("flows/tasks/integrate_modules.yaml")
        step = flow.steps["structural_check"]
        assert step.action == "validate_cross_file_consistency"
        assert "cross_file_results" in step.publishes

    def test_composes_prepare_context(self):
        flow = _load("flows/tasks/integrate_modules.yaml")
        assert flow.steps["gather_context"].action == "flow"
        assert flow.steps["gather_context"].flow == "prepare_context"

    def test_uses_compile_integration_report(self):
        flow = _load("flows/tasks/integrate_modules.yaml")
        step = flow.steps["compile_report"]
        assert step.action == "compile_integration_report"
        assert "integration_report" in step.publishes

    def test_no_quality_gate_or_diagnose(self):
        """v2 inspector does not compose quality_gate or diagnose_issue."""
        flow = _load("flows/tasks/integrate_modules.yaml")
        for step in flow.steps.values():
            assert step.flow != "quality_gate"
            assert step.flow != "diagnose_issue"

    def test_composes_capture_learnings(self):
        flow = _load("flows/tasks/integrate_modules.yaml")
        assert flow.steps["capture_learnings"].action == "flow"
        assert flow.steps["capture_learnings"].flow == "capture_learnings"

    def test_inference_steps_have_prompts(self):
        flow = _load("flows/tasks/integrate_modules.yaml")
        step = flow.steps["analyze_cohesion"]
        assert step.action == "inference"
        assert step.prompt is not None
        assert len(step.prompt) > 50


# ═══════════════════════════════════════════════════════════════════════
# refactor Flow Loading Tests
# ═══════════════════════════════════════════════════════════════════════


class TestRefactorLoads:
    """Verify refactor YAML loads and validates."""

    def test_loads_successfully(self):
        flow = _load("flows/tasks/refactor.yaml")
        assert flow.flow == "refactor"
        assert flow.entry == "gather_context"

    def test_has_required_inputs(self):
        flow = _load("flows/tasks/refactor.yaml")
        assert "mission_id" in flow.input.required
        assert "task_id" in flow.input.required
        assert "target_file_path" in flow.input.required

    def test_has_tail_call_exits(self):
        flow = _load("flows/tasks/refactor.yaml")
        tail_call_steps = [
            name for name, step in flow.steps.items() if step.tail_call is not None
        ]
        assert (
            len(tail_call_steps) >= 5
        )  # complete, code_is_clean, too_risky, needs_tests_first, cannot_refactor, failed

    def test_has_llm_menu_on_identify_smells(self):
        flow = _load("flows/tasks/refactor.yaml")
        step = flow.steps["identify_smells"]
        assert step.resolver.type == "llm_menu"
        option_names = list(step.resolver.options.keys())
        assert "apply_refactoring" in option_names
        assert "code_is_clean" in option_names
        assert "too_risky" in option_names

    def test_has_llm_menu_on_identify_smells_no_tests(self):
        flow = _load("flows/tasks/refactor.yaml")
        step = flow.steps["identify_smells_no_tests"]
        assert step.resolver.type == "llm_menu"
        option_names = list(step.resolver.options.keys())
        assert "apply_refactoring" in option_names
        assert "code_is_clean" in option_names
        assert "needs_tests_first" in option_names

    def test_uses_run_project_tests(self):
        flow = _load("flows/tasks/refactor.yaml")
        assert flow.steps["baseline_tests"].action == "run_project_tests"
        assert flow.steps["verify_refactoring"].action == "run_project_tests"

    def test_uses_check_remaining_smells(self):
        flow = _load("flows/tasks/refactor.yaml")
        assert flow.steps["check_more_refactorings"].action == "check_remaining_smells"

    def test_uses_restore_file_from_context(self):
        flow = _load("flows/tasks/refactor.yaml")
        assert flow.steps["rollback_refactoring"].action == "restore_file_from_context"

    def test_has_two_smell_paths(self):
        """With-tests and no-tests paths based on baseline_tests result."""
        flow = _load("flows/tasks/refactor.yaml")
        bt_transitions = [
            r.transition for r in flow.steps["baseline_tests"].resolver.rules
        ]
        assert "identify_smells" in bt_transitions
        assert "identify_smells_no_tests" in bt_transitions
        assert "cannot_refactor" in bt_transitions

    def test_refactoring_loop_structure(self):
        """apply → write → verify → check_more → re_read → apply (loop)."""
        flow = _load("flows/tasks/refactor.yaml")
        # apply_refactoring → write_refactored
        ar_transitions = [
            r.transition for r in flow.steps["apply_refactoring"].resolver.rules
        ]
        assert "write_refactored" in ar_transitions
        # check_more_refactorings → re_read_target (loop) or capture_learnings (done)
        cm_transitions = [
            r.transition for r in flow.steps["check_more_refactorings"].resolver.rules
        ]
        assert "re_read_target" in cm_transitions
        assert "capture_learnings" in cm_transitions


# ═══════════════════════════════════════════════════════════════════════
# document_project Flow Loading Tests
# ═══════════════════════════════════════════════════════════════════════


class TestDocumentProjectLoads:
    """Verify document_project YAML loads and validates."""

    def test_loads_successfully(self):
        flow = _load("flows/tasks/document_project.yaml")
        assert flow.flow == "document_project"
        assert flow.entry == "gather_context"

    def test_has_required_inputs(self):
        flow = _load("flows/tasks/document_project.yaml")
        assert "mission_id" in flow.input.required
        assert "task_id" in flow.input.required

    def test_has_tail_call_exits(self):
        flow = _load("flows/tasks/document_project.yaml")
        tail_call_steps = [
            name for name, step in flow.steps.items() if step.tail_call is not None
        ]
        assert len(tail_call_steps) >= 3  # complete, documentation_adequate, failed

    def test_has_llm_menu_on_assess(self):
        flow = _load("flows/tasks/document_project.yaml")
        step = flow.steps["assess_documentation_state"]
        assert step.resolver.type == "llm_menu"
        option_names = list(step.resolver.options.keys())
        assert "write_readme" in option_names
        assert "update_docstrings" in option_names
        assert "write_architecture" in option_names
        assert "documentation_adequate" in option_names

    def test_uses_apply_multi_file_changes(self):
        flow = _load("flows/tasks/document_project.yaml")
        assert flow.steps["apply_docstrings"].action == "apply_multi_file_changes"

    def test_uses_check_remaining_doc_tasks(self):
        flow = _load("flows/tasks/document_project.yaml")
        assert flow.steps["check_more_docs"].action == "check_remaining_doc_tasks"

    def test_composes_validate_output(self):
        flow = _load("flows/tasks/document_project.yaml")
        assert flow.steps["verify_no_behavior_change"].action == "flow"
        assert flow.steps["verify_no_behavior_change"].flow == "validate_output"

    def test_has_readme_and_architecture_write_steps(self):
        flow = _load("flows/tasks/document_project.yaml")
        assert flow.steps["save_readme"].action == "write_file"
        assert flow.steps["save_architecture"].action == "write_file"

    def test_inference_steps_have_prompts(self):
        flow = _load("flows/tasks/document_project.yaml")
        for step_name in [
            "assess_documentation_state",
            "write_readme",
            "update_docstrings",
            "write_architecture",
        ]:
            step = flow.steps[step_name]
            assert step.action == "inference"
            assert step.prompt is not None
            assert len(step.prompt) > 50


# ═══════════════════════════════════════════════════════════════════════
# Action Unit Tests — apply_multi_file_changes
# ═══════════════════════════════════════════════════════════════════════


class TestApplyMultiFileChanges:
    """Test the apply_multi_file_changes action."""

    @pytest.mark.asyncio
    async def test_parses_and_writes_files(self):
        effects = MockEffects()
        step_input = StepInput(
            context={
                "integration_code": (
                    "=== FILE: src/main.py ===\n"
                    "```python\nprint('hello')\n```\n"
                    "=== FILE: src/utils.py ===\n"
                    "```python\ndef helper(): pass\n```\n"
                ),
            },
            params={},
            effects=effects,
        )
        result = await action_apply_multi_file_changes(step_input)
        assert result.result["all_written"] is True
        assert result.result["files_written"] == 2
        assert "src/main.py" in effects.written_files
        assert "src/utils.py" in effects.written_files

    @pytest.mark.asyncio
    async def test_handles_bare_content(self):
        """Content without code fences should still work."""
        effects = MockEffects()
        step_input = StepInput(
            context={
                "integration_code": (
                    "=== FILE: config.py ===\n" "DEBUG = True\nPORT = 8080\n"
                ),
            },
            params={},
            effects=effects,
        )
        result = await action_apply_multi_file_changes(step_input)
        assert result.result["files_written"] == 1
        assert "config.py" in effects.written_files

    @pytest.mark.asyncio
    async def test_no_effects_returns_failure(self):
        step_input = StepInput(
            context={"integration_code": "=== FILE: x.py ===\n```\ncontent\n```\n"},
            params={},
            effects=None,
        )
        result = await action_apply_multi_file_changes(step_input)
        assert result.result["all_written"] is False

    @pytest.mark.asyncio
    async def test_no_file_blocks_returns_failure(self):
        effects = MockEffects()
        step_input = StepInput(
            context={"integration_code": "No file markers here"},
            params={},
            effects=effects,
        )
        result = await action_apply_multi_file_changes(step_input)
        assert result.result["all_written"] is False
        assert result.result["files_written"] == 0

    @pytest.mark.asyncio
    async def test_custom_content_key(self):
        effects = MockEffects()
        step_input = StepInput(
            context={
                "docstring_changes": (
                    "=== FILE: module.py ===\n"
                    '```python\n"""Module docstring."""\n```\n'
                ),
            },
            params={"content_key": "docstring_changes"},
            effects=effects,
        )
        result = await action_apply_multi_file_changes(step_input)
        assert result.result["files_written"] == 1


class TestParseMultiFileOutput:
    """Test the _parse_multi_file_output parser."""

    def test_parses_fenced_blocks(self):
        text = (
            "=== FILE: a.py ===\n"
            "```python\ncode_a\n```\n"
            "=== FILE: b.py ===\n"
            "```python\ncode_b\n```\n"
        )
        blocks = _parse_multi_file_output(text)
        assert len(blocks) == 2
        assert blocks[0] == ("a.py", "code_a\n")
        assert blocks[1] == ("b.py", "code_b\n")

    def test_parses_bare_content(self):
        text = "=== FILE: config.txt ===\nkey=value\n"
        blocks = _parse_multi_file_output(text)
        assert len(blocks) == 1
        assert blocks[0][0] == "config.txt"
        assert "key=value" in blocks[0][1]

    def test_empty_text(self):
        assert _parse_multi_file_output("") == []

    def test_no_markers(self):
        assert _parse_multi_file_output("just some text") == []


# ═══════════════════════════════════════════════════════════════════════
# Action Unit Tests — run_project_tests
# ═══════════════════════════════════════════════════════════════════════


class TestRunProjectTests:
    """Test the run_project_tests action."""

    @pytest.mark.asyncio
    async def test_no_tests_found(self):
        effects = MockEffects(files={"src/main.py": "print('hello')"})
        step_input = StepInput(context={}, params={}, effects=effects)
        result = await action_run_project_tests(step_input)
        assert result.result["no_tests"] is True
        assert result.result["all_passing"] is True

    @pytest.mark.asyncio
    async def test_no_effects(self):
        step_input = StepInput(context={}, params={}, effects=None)
        result = await action_run_project_tests(step_input)
        assert result.result["no_tests"] is True
        assert result.result["all_passing"] is True

    @pytest.mark.asyncio
    async def test_discovers_test_files(self):
        from agent.effects.protocol import CommandResult

        effects = MockEffects(
            files={"tests/test_app.py": "def test_it(): pass"},
            commands={
                "uv run pytest tests/ -v --tb=short": CommandResult(
                    return_code=0, stdout="1 passed", stderr="", command="pytest"
                )
            },
        )
        step_input = StepInput(context={}, params={}, effects=effects)
        result = await action_run_project_tests(step_input)
        assert result.result["all_passing"] is True
        assert result.result["no_tests"] is False

    @pytest.mark.asyncio
    async def test_failing_tests(self):
        from agent.effects.protocol import CommandResult

        effects = MockEffects(
            files={"tests/test_app.py": "def test_fail(): assert False"},
            commands={
                "uv run pytest tests/ -v --tb=short": CommandResult(
                    return_code=1, stdout="1 failed", stderr="", command="pytest"
                )
            },
        )
        step_input = StepInput(context={}, params={}, effects=effects)
        result = await action_run_project_tests(step_input)
        assert result.result["all_passing"] is False
        assert result.result["no_tests"] is False


# ═══════════════════════════════════════════════════════════════════════
# Action Unit Tests — check_remaining_smells
# ═══════════════════════════════════════════════════════════════════════


class TestCheckRemainingSmells:
    """Test the check_remaining_smells action."""

    @pytest.mark.asyncio
    async def test_counts_correctly(self):
        step_input = StepInput(
            context={
                "smell_analysis": (
                    "1. **Long Method**: process_data is 80 lines\n"
                    "2. **Dead Code**: unused_helper\n"
                    "3. **Missing Type Hints**: all params untyped\n"
                ),
                "previous_refactorings": ["Long Method"],
                "refactoring_applied": "Dead Code",
            },
            params={},
        )
        result = await action_check_remaining_smells(step_input)
        assert result.result["total"] == 3
        assert result.result["applied"] == 2  # Long Method + Dead Code
        assert result.result["remaining"] == 1

    @pytest.mark.asyncio
    async def test_empty_analysis(self):
        step_input = StepInput(
            context={"smell_analysis": "", "refactoring_applied": ""},
            params={},
        )
        result = await action_check_remaining_smells(step_input)
        assert result.result["remaining"] == 0
        assert result.result["total"] == 0

    @pytest.mark.asyncio
    async def test_tracks_newly_applied(self):
        step_input = StepInput(
            context={
                "smell_analysis": "1. **Rename**: bad_var\n",
                "previous_refactorings": [],
                "refactoring_applied": "Rename: bad_var",
            },
            params={},
        )
        result = await action_check_remaining_smells(step_input)
        # One smell, one applied
        assert result.result["applied"] == 1
        assert "Rename: bad_var" in result.context_updates["previous_refactorings"]


# ═══════════════════════════════════════════════════════════════════════
# Action Unit Tests — restore_file_from_context
# ═══════════════════════════════════════════════════════════════════════


class TestRestoreFileFromContext:
    """Test the restore_file_from_context action."""

    @pytest.mark.asyncio
    async def test_restores_file(self):
        effects = MockEffects(files={"src/main.py": "modified version"})
        step_input = StepInput(
            context={
                "target_file": {"path": "src/main.py", "content": "original version"},
                "refactoring_applied": "Extract Method",
            },
            params={},
            effects=effects,
        )
        result = await action_restore_file_from_context(step_input)
        assert result.result["restored"] is True
        assert effects.written_files["src/main.py"] == "original version"

    @pytest.mark.asyncio
    async def test_no_target_file(self):
        effects = MockEffects()
        step_input = StepInput(
            context={"target_file": {}, "refactoring_applied": "Failed op"},
            params={},
            effects=effects,
        )
        result = await action_restore_file_from_context(step_input)
        assert result.result["restored"] is False

    @pytest.mark.asyncio
    async def test_no_effects(self):
        step_input = StepInput(
            context={
                "target_file": {"path": "x.py", "content": "code"},
                "refactoring_applied": "X",
            },
            params={},
            effects=None,
        )
        result = await action_restore_file_from_context(step_input)
        assert result.result["restored"] is False


# ═══════════════════════════════════════════════════════════════════════
# Action Unit Tests — check_remaining_doc_tasks
# ═══════════════════════════════════════════════════════════════════════


class TestCheckRemainingDocTasks:
    """Test the check_remaining_doc_tasks action."""

    @pytest.mark.asyncio
    async def test_counts_missing_docs(self):
        step_input = StepInput(
            context={
                "doc_assessment": "README: missing. Docstrings: incomplete. Architecture: missing.",
            },
            params={},
        )
        result = await action_check_remaining_doc_tasks(step_input)
        assert result.result["total"] == 3
        assert result.result["remaining"] == 3

    @pytest.mark.asyncio
    async def test_counts_completed(self):
        step_input = StepInput(
            context={
                "doc_assessment": "README: missing. Docstrings: incomplete.",
                "readme_written": True,
            },
            params={},
        )
        result = await action_check_remaining_doc_tasks(step_input)
        assert result.result["total"] == 2
        assert result.result["completed"] == 1
        assert result.result["remaining"] == 1
        assert "README" in result.context_updates["docs_completed"]

    @pytest.mark.asyncio
    async def test_adequate_docs(self):
        step_input = StepInput(
            context={"doc_assessment": "All documentation is adequate and good."},
            params={},
        )
        result = await action_check_remaining_doc_tasks(step_input)
        assert result.result["remaining"] == 0


# ═══════════════════════════════════════════════════════════════════════
# Flow Type Inference Tests
# ═══════════════════════════════════════════════════════════════════════


class TestInferFlowWave2:
    """Test that new keywords route to Wave 2 flows."""

    def test_integrate_keywords(self):
        assert (
            _infer_flow_from_description("Integrate the modules") == "integrate_modules"
        )
        assert (
            _infer_flow_from_description("Wire up the API layer") == "integrate_modules"
        )
        assert (
            _infer_flow_from_description("Fix missing imports across modules")
            == "integrate_modules"
        )

    def test_refactor_keywords(self):
        assert _infer_flow_from_description("Refactor the data layer") == "refactor"
        assert _infer_flow_from_description("Clean up code in utils.py") == "refactor"
        assert _infer_flow_from_description("Restructure the auth module") == "refactor"

    def test_document_keywords(self):
        assert (
            _infer_flow_from_description("Document the API endpoints")
            == "document_project"
        )
        assert (
            _infer_flow_from_description("Write README for the project")
            == "document_project"
        )
        assert (
            _infer_flow_from_description("Add docstrings to all modules")
            == "document_project"
        )

    def test_modify_still_works(self):
        """Modification keywords that aren't refactor should still go to modify_file."""
        assert (
            _infer_flow_from_description("Fix the broken import in app.py")
            == "modify_file"
        )
        assert (
            _infer_flow_from_description("Modify user.py to add validation")
            == "modify_file"
        )


# ═══════════════════════════════════════════════════════════════════════
# All Flows Discovery Test
# ═══════════════════════════════════════════════════════════════════════


class TestWave2FlowsInRegistry:
    """Verify Wave 2 flows are discoverable in load_all_flows."""

    def test_integrate_modules_in_all_flows(self):
        flows = load_all_flows("flows")
        assert "integrate_modules" in flows

    def test_refactor_in_all_flows(self):
        flows = load_all_flows("flows")
        assert "refactor" in flows

    def test_document_project_in_all_flows(self):
        flows = load_all_flows("flows")
        assert "document_project" in flows

    def test_total_flow_count(self):
        flows = load_all_flows("flows")
        assert len(flows) >= 20  # 15 original + 2 Wave 1 + 3 Wave 2


# ═══════════════════════════════════════════════════════════════════════
# Registry Action Registration Test
# ═══════════════════════════════════════════════════════════════════════


class TestWave2ActionsRegistered:
    """Verify all Wave 2 actions are registered in the action registry."""

    def test_all_actions_registered(self):
        from agent.actions.registry import build_action_registry

        registry = build_action_registry()
        for action_name in [
            "apply_multi_file_changes",
            "run_project_tests",
            "check_remaining_smells",
            "restore_file_from_context",
            "check_remaining_doc_tasks",
        ]:
            assert registry.has(action_name), f"{action_name} not registered"
