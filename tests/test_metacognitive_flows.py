"""Tests for Wave 3 meta-cognitive flows: retrospective, request_review.

Tests flow loading, structural validation, action unit tests,
flow type inference, and registry for the new meta-cognitive flows.
"""

import pytest

from agent.loader import (
    load_flow_with_templates,
    load_template_registry,
    load_all_flows,
)
from agent.models import StepInput
from agent.effects.mock import MockEffects
from agent.actions.retrospective_actions import (
    action_load_retrospective_data,
    action_apply_retrospective_recommendations,
    action_compose_director_report,
    action_submit_review_to_api,
)
from agent.actions.mission_actions import _infer_flow_from_description
from agent.actions.registry import build_action_registry

# ── Helper ────────────────────────────────────────────────────────────


def _load(path):
    reg = load_template_registry("flows")
    return load_flow_with_templates(path, reg)


def _make_step_input(context=None, params=None, effects=None):
    return StepInput(
        step_name="test_step",
        flow_name="test_flow",
        params=params or {},
        context=context or {},
        input={},
        meta={"attempt": 0},
        effects=effects,
    )


# ═══════════════════════════════════════════════════════════════════════
# retrospective Flow Loading Tests
# ═══════════════════════════════════════════════════════════════════════


class TestRetrospectiveLoads:
    """Verify retrospective YAML loads and validates."""

    def test_loads_successfully(self):
        flow = _load("flows/tasks/retrospective.yaml")
        assert flow.flow == "retrospective"
        assert flow.entry == "gather_history"

    def test_has_required_inputs(self):
        flow = _load("flows/tasks/retrospective.yaml")
        assert "mission_id" in flow.input.required

    def test_has_tail_call_exits(self):
        flow = _load("flows/tasks/retrospective.yaml")
        tail_call_steps = [
            name for name, step in flow.steps.items() if step.tail_call is not None
        ]
        assert len(tail_call_steps) >= 2  # complete, too_early

    def test_has_llm_menu_on_recommendations(self):
        flow = _load("flows/tasks/retrospective.yaml")
        step = flow.steps["generate_recommendations"]
        assert step.resolver.type == "llm_menu"
        option_names = list(step.resolver.options.keys())
        assert "apply_recommendations" in option_names
        assert "flag_for_director" in option_names
        assert "no_changes_needed" in option_names

    def test_uses_load_retrospective_data(self):
        flow = _load("flows/tasks/retrospective.yaml")
        assert flow.steps["gather_history"].action == "load_retrospective_data"

    def test_has_snapshot_analysis_step(self):
        """inference_response must be snapshotted as performance_analysis
        before the next inference step overwrites it."""
        flow = _load("flows/tasks/retrospective.yaml")
        step = flow.steps["snapshot_analysis"]
        assert step.action == "transform"

    def test_uses_apply_retrospective_recommendations(self):
        flow = _load("flows/tasks/retrospective.yaml")
        assert (
            flow.steps["apply_recommendations"].action
            == "apply_retrospective_recommendations"
        )

    def test_uses_compose_director_report(self):
        flow = _load("flows/tasks/retrospective.yaml")
        assert flow.steps["flag_for_director"].action == "compose_director_report"

    def test_composes_capture_learnings(self):
        flow = _load("flows/tasks/retrospective.yaml")
        step = flow.steps["capture_learnings"]
        assert step.action == "flow"
        assert step.flow == "capture_learnings"

    def test_has_too_early_path(self):
        flow = _load("flows/tasks/retrospective.yaml")
        step = flow.steps["too_early"]
        assert step.tail_call is not None

    def test_inference_steps_have_prompts(self):
        flow = _load("flows/tasks/retrospective.yaml")
        for name in ["analyze_patterns", "generate_recommendations"]:
            assert flow.steps[name].prompt, f"{name} missing prompt"

    def test_all_steps_reachable(self):
        flow = _load("flows/tasks/retrospective.yaml")
        reachable = set()

        def walk(step_name):
            if step_name in reachable:
                return
            reachable.add(step_name)
            step = flow.steps.get(step_name)
            if not step or not step.resolver:
                return
            for rule in getattr(step.resolver, "rules", []):
                walk(rule.transition)
            for opt_name in (getattr(step.resolver, "options", None) or {}).keys():
                if opt_name in flow.steps:
                    walk(opt_name)

        walk(flow.entry)
        assert reachable == set(
            flow.steps.keys()
        ), f"Unreachable steps: {set(flow.steps.keys()) - reachable}"


# ═══════════════════════════════════════════════════════════════════════
# request_review Flow Loading Tests
# ═══════════════════════════════════════════════════════════════════════


class TestRequestReviewLoads:
    """Verify request_review YAML loads and validates."""

    def test_loads_successfully(self):
        flow = _load("flows/tasks/request_review.yaml")
        assert flow.flow == "request_review"
        assert flow.entry == "gather_review_context"

    def test_has_required_inputs(self):
        flow = _load("flows/tasks/request_review.yaml")
        assert "mission_id" in flow.input.required
        assert "task_id" in flow.input.required

    def test_has_stub_submission_step(self):
        flow = _load("flows/tasks/request_review.yaml")
        assert flow.steps["submit_review"].action == "submit_review_to_api"

    def test_has_llm_menu_on_feedback_processing(self):
        flow = _load("flows/tasks/request_review.yaml")
        step = flow.steps["process_review_feedback"]
        assert step.resolver.type == "llm_menu"
        option_names = list(step.resolver.options.keys())
        assert "approved" in option_names
        assert "changes_needed" in option_names
        assert "major_rework" in option_names

    def test_review_unavailable_is_success(self):
        """Review unavailability should be success, not failure."""
        flow = _load("flows/tasks/request_review.yaml")
        step = flow.steps["review_unavailable"]
        assert step.status == "success"

    def test_has_tail_call_exits(self):
        flow = _load("flows/tasks/request_review.yaml")
        tail_call_steps = [
            name for name, step in flow.steps.items() if step.tail_call is not None
        ]
        assert (
            len(tail_call_steps) >= 3
        )  # changes_needed, major_rework, review_unavailable

    def test_composes_capture_learnings(self):
        flow = _load("flows/tasks/request_review.yaml")
        step = flow.steps["approved"]
        assert step.action == "flow"
        assert step.flow == "capture_learnings"


# ═══════════════════════════════════════════════════════════════════════
# action_load_retrospective_data Tests
# ═══════════════════════════════════════════════════════════════════════


class TestLoadRetrospectiveData:
    """Test the load_retrospective_data action."""

    @pytest.mark.asyncio
    async def test_no_effects(self):
        si = _make_step_input(effects=None)
        result = await action_load_retrospective_data(si)
        assert result.result["completed_tasks"] == 0

    @pytest.mark.asyncio
    async def test_no_mission(self):
        effects = MockEffects()
        si = _make_step_input(effects=effects)
        result = await action_load_retrospective_data(si)
        assert result.result["completed_tasks"] == 0

    @pytest.mark.asyncio
    async def test_with_mission_data(self):
        from agent.persistence.models import (
            MissionState,
            MissionConfig,
            TaskRecord,
            NoteRecord,
        )

        mission = MissionState(
            objective="Build a REST API",
            config=MissionConfig(working_directory="/tmp/test"),
            plan=[
                TaskRecord(
                    description="Create models",
                    flow="create_file",
                    status="complete",
                    summary="Created models.py",
                ),
                TaskRecord(
                    description="Create routes",
                    flow="create_file",
                    status="complete",
                    summary="Created routes.py",
                ),
                TaskRecord(
                    description="Create tests",
                    flow="create_tests",
                    status="pending",
                ),
                TaskRecord(
                    description="Fix bug",
                    flow="modify_file",
                    status="failed",
                    frustration=3,
                    summary="Import error persists",
                ),
            ],
            notes=[
                NoteRecord(
                    content="models.py uses SQLAlchemy ORM",
                    category="codebase_observation",
                    source_flow="create_file",
                ),
            ],
        )

        effects = MockEffects()
        await effects.save_mission(mission)

        si = _make_step_input(effects=effects)
        result = await action_load_retrospective_data(si)

        assert result.result["completed_tasks"] == 2
        assert len(result.context_updates["task_outcomes"]) == 4
        assert len(result.context_updates["learnings_archive"]) == 1
        assert (
            result.context_updates["mission_history"]["objective"] == "Build a REST API"
        )

    @pytest.mark.asyncio
    async def test_task_outcome_structure(self):
        from agent.persistence.models import (
            MissionState,
            MissionConfig,
            TaskRecord,
        )

        mission = MissionState(
            objective="Test",
            config=MissionConfig(working_directory="/tmp"),
            plan=[
                TaskRecord(
                    description="A task",
                    flow="create_file",
                    status="complete",
                    frustration=1,
                ),
            ],
        )

        effects = MockEffects()
        await effects.save_mission(mission)

        si = _make_step_input(effects=effects)
        result = await action_load_retrospective_data(si)

        outcome = result.context_updates["task_outcomes"][0]
        assert "id" in outcome
        assert "description" in outcome
        assert "flow" in outcome
        assert "status" in outcome
        assert "frustration" in outcome
        assert "attempts" in outcome

    @pytest.mark.asyncio
    async def test_failed_task_has_failure_reason(self):
        from agent.persistence.models import (
            MissionState,
            MissionConfig,
            TaskRecord,
        )

        mission = MissionState(
            objective="Test",
            config=MissionConfig(working_directory="/tmp"),
            plan=[
                TaskRecord(
                    description="Broken task",
                    flow="modify_file",
                    status="failed",
                    summary="Could not resolve import",
                ),
            ],
        )

        effects = MockEffects()
        await effects.save_mission(mission)

        si = _make_step_input(effects=effects)
        result = await action_load_retrospective_data(si)

        outcome = result.context_updates["task_outcomes"][0]
        assert outcome["failure_reason"] == "Could not resolve import"


# ═══════════════════════════════════════════════════════════════════════
# action_apply_retrospective_recommendations Tests
# ═══════════════════════════════════════════════════════════════════════


class TestApplyRetrospectiveRecommendations:
    """Test the apply_retrospective_recommendations action."""

    @pytest.mark.asyncio
    async def test_no_effects(self):
        si = _make_step_input(
            context={"recommendations": "add_task: Write tests for validation"},
            effects=None,
        )
        result = await action_apply_retrospective_recommendations(si)
        assert result.result["changes_applied"] is False

    @pytest.mark.asyncio
    async def test_add_task_recommendation(self):
        from agent.persistence.models import MissionState, MissionConfig, TaskRecord

        mission = MissionState(
            objective="Build API",
            config=MissionConfig(working_directory="/tmp"),
            plan=[
                TaskRecord(
                    description="Create models",
                    flow="create_file",
                    status="complete",
                ),
            ],
        )

        effects = MockEffects()
        await effects.save_mission(mission)

        si = _make_step_input(
            context={
                "recommendations": "add_task: Write comprehensive tests for the validation module in validator.py",
                "mission_history": {"objective": "Build API"},
            },
            effects=effects,
        )
        result = await action_apply_retrospective_recommendations(si)

        assert result.result["changes_applied"] is True
        assert result.result["change_count"] >= 1

        # Verify mission was saved with new task
        saved = await effects.load_mission()
        assert len(saved.plan) == 2
        assert (
            "test" in saved.plan[1].description.lower()
            or "validation" in saved.plan[1].description.lower()
        )

    @pytest.mark.asyncio
    async def test_note_for_knowledge_base(self):
        from agent.persistence.models import MissionState, MissionConfig

        mission = MissionState(
            objective="Build API",
            config=MissionConfig(working_directory="/tmp"),
        )

        effects = MockEffects()
        await effects.save_mission(mission)

        si = _make_step_input(
            context={
                "recommendations": "note_for_knowledge_base: Always validate input schemas before processing",
                "mission_history": {"objective": "Build API"},
            },
            effects=effects,
        )
        result = await action_apply_retrospective_recommendations(si)

        assert result.result["changes_applied"] is True
        saved = await effects.load_mission()
        assert len(saved.notes) >= 1
        assert "validate" in saved.notes[0].content.lower()

    @pytest.mark.asyncio
    async def test_adjust_approach(self):
        from agent.persistence.models import MissionState, MissionConfig

        mission = MissionState(
            objective="Build API",
            config=MissionConfig(working_directory="/tmp"),
        )

        effects = MockEffects()
        await effects.save_mission(mission)

        si = _make_step_input(
            context={
                "recommendations": "adjust_approach: Run tests after every file creation, not just at the end",
                "mission_history": {"objective": "Build API"},
            },
            effects=effects,
        )
        result = await action_apply_retrospective_recommendations(si)

        assert result.result["changes_applied"] is True
        saved = await effects.load_mission()
        assert any("approach" in n.content.lower() for n in saved.notes)

    @pytest.mark.asyncio
    async def test_no_actionable_recommendations(self):
        from agent.persistence.models import MissionState, MissionConfig

        mission = MissionState(
            objective="Build API",
            config=MissionConfig(working_directory="/tmp"),
        )

        effects = MockEffects()
        await effects.save_mission(mission)

        si = _make_step_input(
            context={
                "recommendations": "Everything looks good, keep going.",
                "mission_history": {"objective": "Build API"},
            },
            effects=effects,
        )
        result = await action_apply_retrospective_recommendations(si)

        assert result.result["changes_applied"] is False
        assert result.result["change_count"] == 0


# ═══════════════════════════════════════════════════════════════════════
# action_compose_director_report Tests
# ═══════════════════════════════════════════════════════════════════════


class TestComposeDirectorReport:
    """Test the compose_director_report action."""

    @pytest.mark.asyncio
    async def test_generates_report(self):
        effects = MockEffects()
        si = _make_step_input(
            context={
                "performance_analysis": "Tasks complete quickly but tests are fragile.",
                "recommendations": "Add more integration tests.",
                "mission_health": "at_risk",
                "mission_history": {
                    "objective": "Build REST API",
                    "total_tasks": 8,
                    "completed": 5,
                },
            },
            effects=effects,
        )
        result = await action_compose_director_report(si)

        assert result.result["report_generated"] is True
        report = result.context_updates["director_report"]
        assert "Build REST API" in report
        assert "5/8" in report
        assert "at_risk" in report

    @pytest.mark.asyncio
    async def test_pushes_event(self):
        effects = MockEffects()
        si = _make_step_input(
            context={
                "performance_analysis": "Analysis text",
                "recommendations": "Recommendations text",
                "mission_health": "on_track",
                "mission_history": {
                    "objective": "Test",
                    "total_tasks": 3,
                    "completed": 2,
                },
            },
            effects=effects,
        )
        await action_compose_director_report(si)

        assert effects.call_count("push_event") == 1

    @pytest.mark.asyncio
    async def test_no_effects_still_generates(self):
        si = _make_step_input(
            context={
                "performance_analysis": "Analysis",
                "recommendations": "Recs",
                "mission_health": "on_track",
                "mission_history": {
                    "objective": "Test",
                    "total_tasks": 1,
                    "completed": 1,
                },
            },
            effects=None,
        )
        result = await action_compose_director_report(si)
        assert result.result["report_generated"] is True


# ═══════════════════════════════════════════════════════════════════════
# action_submit_review_to_api Tests (Stub)
# ═══════════════════════════════════════════════════════════════════════


class TestSubmitReviewToApi:
    """Test the submit_review_to_api stub action."""

    @pytest.mark.asyncio
    async def test_returns_unavailable(self):
        si = _make_step_input(
            context={
                "review_request": "Please review this code",
                "review_files": {"main.py": "print('hello')"},
            }
        )
        result = await action_submit_review_to_api(si)
        assert result.result["response_received"] is False
        assert "Phase 6" in result.result["reason"]

    @pytest.mark.asyncio
    async def test_context_updates_review_response_none(self):
        si = _make_step_input(context={"review_request": "Review", "review_files": {}})
        result = await action_submit_review_to_api(si)
        assert result.context_updates["review_response"] is None


# ═══════════════════════════════════════════════════════════════════════
# Flow Type Inference Tests
# ═══════════════════════════════════════════════════════════════════════


class TestFlowTypeInference:
    """Test _infer_flow_from_description for retrospective/review keywords."""

    def test_retrospective_keyword(self):
        assert (
            _infer_flow_from_description("Run a retrospective on progress")
            == "retrospective"
        )

    def test_self_assessment_keyword(self):
        assert (
            _infer_flow_from_description("Perform self-assessment of work")
            == "retrospective"
        )

    def test_review_progress_keyword(self):
        assert (
            _infer_flow_from_description("Review progress and adjust plan")
            == "retrospective"
        )

    def test_analyze_performance_keyword(self):
        assert (
            _infer_flow_from_description("Analyze performance patterns")
            == "retrospective"
        )

    def test_mission_health_keyword(self):
        assert (
            _infer_flow_from_description("Check mission health status")
            == "retrospective"
        )

    def test_request_review_keyword(self):
        assert (
            _infer_flow_from_description("Request review of completed work")
            == "request_review"
        )

    def test_code_review_keyword(self):
        assert (
            _infer_flow_from_description("Submit code review for auth module")
            == "request_review"
        )

    def test_submit_for_review_keyword(self):
        assert (
            _infer_flow_from_description("Submit for review before merging")
            == "request_review"
        )

    def test_seek_feedback_keyword(self):
        assert (
            _infer_flow_from_description("Seek feedback on the implementation")
            == "request_review"
        )


# ═══════════════════════════════════════════════════════════════════════
# Registry Tests
# ═══════════════════════════════════════════════════════════════════════


class TestRegistryIncludesWave3Actions:
    """Verify all Wave 3 actions are registered."""

    def test_load_retrospective_data_registered(self):
        reg = build_action_registry()
        assert reg.has("load_retrospective_data")

    def test_apply_retrospective_recommendations_registered(self):
        reg = build_action_registry()
        assert reg.has("apply_retrospective_recommendations")

    def test_compose_director_report_registered(self):
        reg = build_action_registry()
        assert reg.has("compose_director_report")

    def test_submit_review_to_api_registered(self):
        reg = build_action_registry()
        assert reg.has("submit_review_to_api")


# ═══════════════════════════════════════════════════════════════════════
# Flow Registry Tests
# ═══════════════════════════════════════════════════════════════════════


class TestFlowRegistryIncludes:
    """Verify flows are loadable through the registry."""

    def test_retrospective_in_registry(self):
        flows = load_all_flows("flows")
        assert "retrospective" in flows

    def test_request_review_in_registry(self):
        flows = load_all_flows("flows")
        assert "request_review" in flows

    def test_total_flow_count(self):
        flows = load_all_flows("flows")
        assert (
            len(flows) == 29
        ), f"Expected 29 flows, got {len(flows)}: {list(flows.keys())}"


# ═══════════════════════════════════════════════════════════════════════
# mission_control Wiring Tests
# ═══════════════════════════════════════════════════════════════════════


class TestMissionControlRetrospectiveWiring:
    """Verify mission_control has retrospective dispatch wired."""

    def test_has_check_retrospective_step(self):
        flow = _load("flows/mission_control.yaml")
        assert "check_retrospective" in flow.steps

    def test_has_dispatch_retrospective_step(self):
        flow = _load("flows/mission_control.yaml")
        assert "dispatch_retrospective" in flow.steps

    def test_dispatch_retrospective_tail_calls_retrospective(self):
        flow = _load("flows/mission_control.yaml")
        step = flow.steps["dispatch_retrospective"]
        assert step.tail_call is not None
        assert step.tail_call["flow"] == "retrospective"

    def test_check_retrospective_can_reach_check_extension(self):
        """When retrospective isn't due, flow continues to check_extension."""
        flow = _load("flows/mission_control.yaml")
        step = flow.steps["check_retrospective"]
        transitions = [r.transition for r in step.resolver.rules]
        assert "check_extension" in transitions

    def test_apply_last_result_routes_to_check_retrospective(self):
        """Task completion now routes through retrospective check."""
        flow = _load("flows/mission_control.yaml")
        step = flow.steps["apply_last_result"]
        transitions = [r.transition for r in step.resolver.rules]
        assert "check_retrospective" in transitions
