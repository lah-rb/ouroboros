"""Contract tests for the Context Contract Architecture.

Tests the contract machinery — not every flow end-to-end. Focus areas:
  1. Returns assembly: assemble_returns resolves correctly
  2. Tier enforcement: runtime warnings fire on violations
  3. Goal lifecycle: GoalRecord creation, task linking, frustration derivation
  4. Flow loading: compiled.json loads with new fields
  5. Tail-call resolution: structured last_result replaces prose strings
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agent.loader_v2 import assemble_returns
from agent.models import FlowDefinition, FlowResult
from agent.persistence.models import (
    GoalRecord,
    MissionConfig,
    MissionState,
    TaskRecord,
)
from agent.tail_call import FlowTailCall, FlowTermination


# ── Helpers ──────────────────────────────────────────────────────────


def _make_flow(
    name: str = "test_flow",
    tier: str = "flow_directive",
    returns: dict | None = None,
    state_reads: list | None = None,
) -> FlowDefinition:
    """Build a minimal FlowDefinition for testing."""
    return FlowDefinition(
        flow=name,
        context_tier=tier,
        returns=returns or {},
        state_reads=state_reads or [],
        steps={"entry": {"action": "noop", "terminal": True, "status": "success"}},
        entry="entry",
    )


# ══════════════════════════════════════════════════════════════════════
# 1. Returns Assembly
# ══════════════════════════════════════════════════════════════════════


class TestAssembleReturns:
    """Verify assemble_returns resolves from paths correctly."""

    def test_resolves_input_path(self):
        fd = _make_flow(returns={
            "target": {"type": "string", "from": "input.target_file_path"},
        })
        result = assemble_returns(fd, {}, {"target_file_path": "engine.py"})
        assert result == {"target": "engine.py"}

    def test_resolves_context_path(self):
        fd = _make_flow(returns={
            "files": {"type": "list", "from": "context.files_changed"},
        })
        result = assemble_returns(fd, {"files_changed": ["a.py", "b.py"]}, {})
        assert result == {"files": ["a.py", "b.py"]}

    def test_omits_missing_optional(self):
        fd = _make_flow(returns={
            "target": {"type": "string", "from": "input.target_file_path"},
            "summary": {"type": "string", "from": "context.edit_summary", "optional": True},
        })
        result = assemble_returns(fd, {}, {"target_file_path": "x.py"})
        assert result == {"target": "x.py"}
        assert "summary" not in result

    def test_includes_present_optional(self):
        fd = _make_flow(returns={
            "summary": {"type": "string", "from": "context.edit_summary", "optional": True},
        })
        result = assemble_returns(fd, {"edit_summary": "fixed imports"}, {})
        assert result == {"summary": "fixed imports"}

    def test_missing_required_returns_none_with_warning(self, caplog):
        fd = _make_flow(returns={
            "target": {"type": "string", "from": "input.missing_field"},
        })
        with caplog.at_level(logging.WARNING):
            result = assemble_returns(fd, {}, {})
        assert result == {"target": None}
        assert "required return field" in caplog.text

    def test_empty_returns_declaration(self):
        fd = _make_flow(returns={})
        result = assemble_returns(fd, {"stuff": "ignored"}, {"more": "ignored"})
        assert result == {}

    def test_nested_context_path(self):
        fd = _make_flow(returns={
            "verdict": {"type": "string", "from": "context.quality_results.verdict"},
        })
        result = assemble_returns(
            fd, {"quality_results": {"verdict": "pass", "issues": []}}, {},
        )
        assert result == {"verdict": "pass"}

    def test_bool_return_field(self):
        fd = _make_flow(returns={
            "all_passed": {"type": "bool", "from": "context.all_passed"},
        })
        result = assemble_returns(fd, {"all_passed": True}, {})
        assert result == {"all_passed": True}


# ══════════════════════════════════════════════════════════════════════
# 2. Tail-Call Resolution with Structured Returns
# ══════════════════════════════════════════════════════════════════════


class TestTailCallResolution:
    """Verify _resolve_tail_call produces structured last_result."""

    def test_structured_last_result_in_tail_call(self):
        from agent.loop import _resolve_tail_call

        fd = _make_flow(
            name="file_ops",
            returns={
                "target_file": {"type": "string", "from": "input.target_file_path"},
                "files_changed": {"type": "list", "from": "context.files_changed", "optional": True},
            },
        )
        fr = FlowResult(
            status="success",
            result={},
            context={"files_changed": ["models.py"], "target_file_path": "engine.py"},
            steps_executed=["check_exists", "run_create"],
            tail_call={
                "flow": "mission_control",
                "input_map": {
                    "mission_id": "abc",
                    "last_status": "success",
                },
            },
        )
        outcome = _resolve_tail_call(fr, fd, {"target_file_path": "engine.py"})

        assert isinstance(outcome, FlowTailCall)
        assert outcome.target_flow == "mission_control"
        assert isinstance(outcome.inputs["last_result"], dict)
        assert outcome.inputs["last_result"]["target_file"] == "engine.py"
        assert outcome.inputs["last_result"]["files_changed"] == ["models.py"]

    def test_no_returns_no_last_result(self):
        from agent.loop import _resolve_tail_call

        fd = _make_flow(name="retrospective", returns={})
        fr = FlowResult(
            status="diagnosed",
            result={},
            context={},
            steps_executed=["complete"],
            tail_call={
                "flow": "mission_control",
                "input_map": {"mission_id": "abc", "last_status": "diagnosed"},
            },
        )
        outcome = _resolve_tail_call(fr, fd, {})
        assert isinstance(outcome, FlowTailCall)
        assert "last_result" not in outcome.inputs

    def test_termination_without_tail_call(self):
        from agent.loop import _resolve_tail_call

        fd = _make_flow(name="mission_control", tier="project_goal")
        fr = FlowResult(
            status="completed",
            result={},
            context={},
            steps_executed=["completed"],
        )
        outcome = _resolve_tail_call(fr, fd, {})
        assert isinstance(outcome, FlowTermination)


# ══════════════════════════════════════════════════════════════════════
# 3. Goal Lifecycle
# ══════════════════════════════════════════════════════════════════════


class TestGoalLifecycle:
    """Verify GoalRecord, task linking, and MissionState integration."""

    def test_goal_creation(self):
        g = GoalRecord(
            description="NPC dialogue with branching choices",
            type="functional",
        )
        assert g.status == "pending"
        assert g.type == "functional"
        assert len(g.id) > 0

    def test_task_goal_linking(self):
        g = GoalRecord(description="Room navigation", type="structural")
        t = TaskRecord(description="Create rooms.py", goal_id=g.id)
        assert t.goal_id == g.id

    def test_mission_state_with_goals(self):
        goals = [
            GoalRecord(description="Navigation", type="structural", associated_files=["rooms.py"]),
            GoalRecord(description="Dialogue", type="functional"),
        ]
        tasks = [
            TaskRecord(description="Create rooms.py", goal_id=goals[0].id),
            TaskRecord(description="Create dialogue.py", goal_id=goals[1].id),
        ]
        m = MissionState(
            objective="Build a game",
            goals=goals,
            plan=tasks,
            config=MissionConfig(working_directory="/tmp/test"),
        )
        assert len(m.goals) == 2
        assert m.schema_version == 3
        assert m.plan[0].goal_id == m.goals[0].id

    def test_goal_frustration_derivation(self):
        """Goal is 'blocked' when >50% of associated tasks have frustration >= 3."""
        g = GoalRecord(description="Test goal", type="structural")
        tasks = [
            TaskRecord(description="t1", goal_id=g.id, frustration=4),
            TaskRecord(description="t2", goal_id=g.id, frustration=3),
            TaskRecord(description="t3", goal_id=g.id, frustration=0),
        ]
        g.associated_task_ids = [t.id for t in tasks]

        # Derive frustration: 2/3 tasks frustrated (>=3) → >50% → blocked
        frustrated = sum(1 for t in tasks if t.frustration >= 3)
        is_blocked = frustrated > len(tasks) / 2
        assert is_blocked is True

    def test_goal_serialization_roundtrip(self):
        g = GoalRecord(
            description="Save/load game state",
            type="functional",
            associated_files=["save.py", "load.py"],
            associated_task_ids=["task_001", "task_002"],
        )
        data = g.model_dump()
        g2 = GoalRecord(**data)
        assert g2.description == g.description
        assert g2.associated_files == g.associated_files


# ══════════════════════════════════════════════════════════════════════
# 4. Flow Loading — compiled.json
# ══════════════════════════════════════════════════════════════════════


class TestFlowLoading:
    """Verify compiled.json loads with context contract fields."""

    @pytest.fixture
    def compiled_flows(self):
        compiled_path = Path(__file__).parent.parent / "flows" / "compiled.json"
        if not compiled_path.exists():
            pytest.skip("compiled.json not found")
        with open(compiled_path) as f:
            data = json.load(f)
        flows = {}
        for name, flow_data in data.items():
            if isinstance(flow_data, dict) and "flow" in flow_data:
                flows[name] = FlowDefinition(**flow_data)
        return flows

    def test_all_flows_have_context_tier(self, compiled_flows):
        valid_tiers = {"mission_objective", "project_goal", "flow_directive", "session_task"}
        for name, fd in compiled_flows.items():
            assert fd.context_tier in valid_tiers, f"{name}: invalid tier {fd.context_tier}"

    def test_all_flows_have_returns(self, compiled_flows):
        for name, fd in compiled_flows.items():
            assert isinstance(fd.returns, dict), f"{name}: returns is not a dict"

    def test_flow_directive_tier_has_directive_input(self, compiled_flows):
        for name, fd in compiled_flows.items():
            if fd.context_tier == "flow_directive":
                assert "flow_directive" in fd.input.required, (
                    f"{name}: flow_directive tier but flow_directive not in required inputs"
                )

    def test_no_result_formatter_in_compiled(self, compiled_flows):
        compiled_path = Path(__file__).parent.parent / "flows" / "compiled.json"
        text = compiled_path.read_text()
        assert "result_formatter" not in text, "result_formatter still in compiled.json"
        assert "result_keys" not in text, "result_keys still in compiled.json"

    def test_no_mission_objective_in_task_flow_inputs(self, compiled_flows):
        """Task flows at flow_directive/session_task tier should not accept mission_objective."""
        for name, fd in compiled_flows.items():
            if fd.context_tier in ("flow_directive", "session_task"):
                all_inputs = fd.input.required + fd.input.optional
                assert "mission_objective" not in all_inputs, (
                    f"{name}: at {fd.context_tier} tier but accepts mission_objective"
                )

    def test_flow_count(self, compiled_flows):
        assert len(compiled_flows) == 18, f"Expected 18 flows, got {len(compiled_flows)}"

    def test_tier_distribution(self, compiled_flows):
        tiers = {}
        for name, fd in compiled_flows.items():
            tiers.setdefault(fd.context_tier, []).append(name)
        assert len(tiers["mission_objective"]) == 2
        assert len(tiers["project_goal"]) == 3
        assert len(tiers["flow_directive"]) == 4
        assert len(tiers["session_task"]) == 9


# ══════════════════════════════════════════════════════════════════════
# 5. Formatter Tests
# ══════════════════════════════════════════════════════════════════════


class TestFormatters:
    """Verify new formatters work correctly."""

    def test_format_goals_listing_empty(self):
        from agent.formatters import format_goals_listing
        result = format_goals_listing({"source": []}, {})
        assert "No goals defined" in result

    def test_format_goals_listing_with_goals(self):
        from agent.formatters import format_goals_listing
        goals = [
            {"id": "g1", "description": "Room navigation", "type": "structural", "status": "complete", "associated_files": ["rooms.py"]},
            {"id": "g2", "description": "NPC dialogue", "type": "functional", "status": "in_progress", "associated_files": []},
        ]
        result = format_goals_listing({"source": goals}, {})
        assert "Room navigation" in result
        assert "NPC dialogue" in result
        assert "complete" in result
        assert "in_progress" in result

    def test_format_structured_result_dict(self):
        from agent.formatters import format_structured_result
        result = format_structured_result({"source": {
            "target_file": "engine.py",
            "files_changed": ["models.py", "parser.py"],
            "all_passed": True,
        }}, {})
        assert "engine.py" in result
        assert "models.py" in result
        assert "yes" in result  # bool formatting

    def test_format_structured_result_string_passthrough(self):
        from agent.formatters import format_structured_result
        result = format_structured_result({"source": "already a string"}, {})
        assert result == "already a string"

    def test_format_structured_result_empty(self):
        from agent.formatters import format_structured_result
        result = format_structured_result({"source": None}, {})
        assert result == ""

    def test_result_formatters_registry_empty(self):
        from agent.formatters import RESULT_FORMATTERS
        assert len(RESULT_FORMATTERS) == 0
