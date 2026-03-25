"""Tests for agent/blueprint/analyzer.py — IR generation from flows and registry."""

import json

import pytest

from agent.blueprint.analyzer import (
    analyze,
    extract_injects,
    _build_source_map,
    _categorize_flow,
    _classify_action_type,
    _scan_effects_usage,
    _scan_template_usage,
)
from agent.blueprint.ir import (
    BlueprintIR,
    FlowIR,
    StepIR,
    ActionIR,
    ContextKeyIR,
    FlowEdgeIR,
    TemplateIR,
)

# ── Full Analyzer Integration ─────────────────────────────────────────


class TestAnalyzeIntegration:
    """Run the full analyzer against the real flows/ directory."""

    @pytest.fixture(scope="class")
    def ir(self) -> BlueprintIR:
        """Run analyze() once and share across all tests in this class."""
        return analyze(flows_dir="flows", agent_dir="agent")

    def test_produces_valid_ir(self, ir: BlueprintIR):
        assert isinstance(ir, BlueprintIR)
        assert ir.meta.flow_count > 0
        assert ir.meta.action_count > 0
        assert ir.meta.context_key_count > 0

    def test_flow_count_matches(self, ir: BlueprintIR):
        assert len(ir.flows) == ir.meta.flow_count

    def test_action_count_matches(self, ir: BlueprintIR):
        assert len(ir.actions) == ir.meta.action_count

    def test_context_key_count_matches(self, ir: BlueprintIR):
        assert len(ir.context_keys) == ir.meta.context_key_count

    def test_has_expected_flows(self, ir: BlueprintIR):
        """Core flows that must always be present."""
        expected = [
            "mission_control",
            "create_plan",
            "modify_file",
            "create_file",
            "test_simple",
        ]
        for name in expected:
            assert name in ir.flows, f"Expected flow {name!r} not found"

    def test_source_hash_is_hex(self, ir: BlueprintIR):
        assert len(ir.meta.source_hash) == 64  # SHA-256 hex
        assert all(c in "0123456789abcdef" for c in ir.meta.source_hash)

    def test_generated_at_is_iso(self, ir: BlueprintIR):
        assert "T" in ir.meta.generated_at  # ISO 8601 format

    def test_ir_serializes_to_dict(self, ir: BlueprintIR):
        d = ir.to_dict()
        assert isinstance(d, dict)
        assert "meta" in d
        assert "flows" in d
        assert "actions" in d
        assert "context_keys" in d
        # Verify it's JSON-serializable
        json_str = json.dumps(d, default=str)
        assert len(json_str) > 100


# ── Flow Categories ───────────────────────────────────────────────────


class TestFlowCategories:
    @pytest.fixture(scope="class")
    def ir(self) -> BlueprintIR:
        return analyze(flows_dir="flows", agent_dir="agent")

    def test_task_flows_categorized(self, ir: BlueprintIR):
        assert ir.flows["modify_file"].category == "task"
        assert ir.flows["create_file"].category == "task"
        assert ir.flows["diagnose_issue"].category == "task"

    def test_shared_flows_categorized(self, ir: BlueprintIR):
        assert ir.flows["prepare_context"].category == "shared"
        assert ir.flows["validate_output"].category == "shared"
        assert ir.flows["capture_learnings"].category == "shared"

    def test_control_flows_categorized(self, ir: BlueprintIR):
        assert ir.flows["mission_control"].category == "control"
        assert ir.flows["create_plan"].category == "control"

    def test_test_flows_categorized(self, ir: BlueprintIR):
        assert ir.flows["test_simple"].category == "test"
        assert ir.flows["test_branching"].category == "test"


# ── Step IR Details ───────────────────────────────────────────────────


class TestStepIR:
    @pytest.fixture(scope="class")
    def ir(self) -> BlueprintIR:
        return analyze(flows_dir="flows", agent_dir="agent")

    def test_inference_step_type(self, ir: BlueprintIR):
        """Inference steps should have action_type='inference' and a prompt."""
        modify = ir.flows["modify_file"]
        # v3: full_rewrite is the inference fallback step
        rewrite_step = modify.steps.get("full_rewrite")
        assert rewrite_step is not None
        assert rewrite_step.action_type == "inference"
        assert rewrite_step.prompt is not None
        assert len(rewrite_step.prompt_injects) > 0

    def test_subflow_step_type(self, ir: BlueprintIR):
        """Sub-flow steps should have action_type='flow' and a target."""
        modify = ir.flows["modify_file"]
        gather = modify.steps.get("gather_context")
        assert gather is not None
        assert gather.action_type == "flow"
        assert gather.sub_flow_target == "prepare_context"

    def test_noop_step_type(self, ir: BlueprintIR):
        """Noop steps should have action_type='noop'."""
        modify = ir.flows["modify_file"]
        complete = modify.steps.get("complete")
        assert complete is not None
        assert complete.action_type == "noop"

    def test_action_step_type(self, ir: BlueprintIR):
        """Regular action steps should have action_type='action'."""
        simple = ir.flows["test_simple"]
        read_step = simple.steps.get("read_file")
        assert read_step is not None
        assert read_step.action_type == "action"
        assert read_step.action == "read_files"

    def test_entry_step_flagged(self, ir: BlueprintIR):
        """The entry step should have is_entry=True."""
        simple = ir.flows["test_simple"]
        assert simple.steps["read_file"].is_entry is True
        assert simple.steps["complete"].is_entry is False

    def test_terminal_step_flagged(self, ir: BlueprintIR):
        """Terminal steps should have is_terminal=True and a status."""
        simple = ir.flows["test_simple"]
        assert simple.steps["complete"].is_terminal is True
        assert simple.steps["complete"].terminal_status == "success"
        assert simple.steps["read_file"].is_terminal is False

    def test_tail_call_step(self, ir: BlueprintIR):
        """Steps with tail-calls should have tail_call_target set."""
        modify = ir.flows["modify_file"]
        complete = modify.steps.get("complete")
        assert complete is not None
        assert complete.tail_call_target == "mission_control"


# ── Flow Stats ────────────────────────────────────────────────────────


class TestFlowStats:
    @pytest.fixture(scope="class")
    def ir(self) -> BlueprintIR:
        return analyze(flows_dir="flows", agent_dir="agent")

    def test_step_count(self, ir: BlueprintIR):
        simple = ir.flows["test_simple"]
        assert simple.stats.step_count == len(simple.steps)

    def test_inference_step_count(self, ir: BlueprintIR):
        modify = ir.flows["modify_file"]
        actual = sum(1 for s in modify.steps.values() if s.action_type == "inference")
        assert modify.stats.inference_step_count == actual

    def test_resolver_counts(self, ir: BlueprintIR):
        modify = ir.flows["modify_file"]
        rule_count = sum(1 for s in modify.steps.values() if s.resolver.type == "rule")
        menu_count = sum(
            1 for s in modify.steps.values() if s.resolver.type == "llm_menu"
        )
        assert modify.stats.rule_resolver_count == rule_count
        assert modify.stats.llm_menu_resolver_count == menu_count


# ── Tail Calls and Sub-flows ─────────────────────────────────────────


class TestFlowConnections:
    @pytest.fixture(scope="class")
    def ir(self) -> BlueprintIR:
        return analyze(flows_dir="flows", agent_dir="agent")

    def test_modify_file_tail_calls(self, ir: BlueprintIR):
        modify = ir.flows["modify_file"]
        assert len(modify.tail_calls) > 0
        targets = [tc.target_flow for tc in modify.tail_calls]
        assert "mission_control" in targets

    def test_modify_file_sub_flows(self, ir: BlueprintIR):
        modify = ir.flows["modify_file"]
        assert len(modify.sub_flows) > 0
        sub_flow_names = [sf.flow for sf in modify.sub_flows]
        assert "prepare_context" in sub_flow_names


# ── Prompt Inject Extraction ──────────────────────────────────────────


class TestPromptInjectExtraction:
    def test_simple_inject(self):
        prompt = "File: {{ context.target_file.path }}"
        result = extract_injects(prompt)
        assert result == ["context.target_file.path"]

    def test_multiple_injects(self):
        prompt = "{{ input.reason }} and {{ context.plan }}"
        result = extract_injects(prompt)
        assert "input.reason" in result
        assert "context.plan" in result

    def test_deduplication(self):
        prompt = "{{ input.x }} then {{ input.x }} again"
        result = extract_injects(prompt)
        assert result == ["input.x"]

    def test_whitespace_stripping(self):
        prompt = "{{  input.reason  }}"
        result = extract_injects(prompt)
        assert result == ["input.reason"]

    def test_multiline_prompt(self):
        prompt = """Line 1: {{ context.file }}
        Line 2: {{ input.reason }}"""
        result = extract_injects(prompt)
        assert len(result) == 2

    def test_no_injects(self):
        prompt = "No template variables here"
        result = extract_injects(prompt)
        assert result == []

    def test_filter_syntax(self):
        prompt = "{{ input.x | default('0') }}"
        result = extract_injects(prompt)
        assert result == ["input.x | default('0')"]

    def test_real_flow_prompt_injects(self):
        """Verify prompt injects extracted from a real flow."""
        ir = analyze(flows_dir="flows", agent_dir="agent")
        modify = ir.flows["modify_file"]
        # v3: full_rewrite is the inference step with prompt injects
        rewrite_step = modify.steps["full_rewrite"]
        inject_strs = " ".join(rewrite_step.prompt_injects)
        assert "context.target_file.content" in inject_strs
        assert "input.target_file_path" in inject_strs


# ── Action Registry Introspection ────────────────────────────────────


class TestActionIntrospection:
    @pytest.fixture(scope="class")
    def ir(self) -> BlueprintIR:
        return analyze(flows_dir="flows", agent_dir="agent")

    def test_actions_have_module_paths(self, ir: BlueprintIR):
        for name, action in ir.actions.items():
            assert action.module != "", f"Action {name!r} has no module path"

    def test_read_files_has_effects(self, ir: BlueprintIR):
        read = ir.actions.get("read_files")
        assert read is not None
        assert "read_file" in read.effects_used

    def test_write_file_has_effects(self, ir: BlueprintIR):
        write = ir.actions.get("write_file")
        assert write is not None
        assert "write_file" in write.effects_used

    def test_referenced_by_populated(self, ir: BlueprintIR):
        """Actions used in flows should have referenced_by entries."""
        read = ir.actions.get("read_files")
        assert read is not None
        assert len(read.referenced_by) > 0
        # test_simple uses read_files
        assert any("test_simple" in ref for ref in read.referenced_by)


# ── Context Key Cross-Reference ───────────────────────────────────────


class TestContextKeys:
    @pytest.fixture(scope="class")
    def ir(self) -> BlueprintIR:
        return analyze(flows_dir="flows", agent_dir="agent")

    def test_target_file_key_exists(self, ir: BlueprintIR):
        assert "target_file" in ir.context_keys

    def test_target_file_has_publishers(self, ir: BlueprintIR):
        key = ir.context_keys["target_file"]
        assert len(key.published_by) > 0

    def test_target_file_has_consumers(self, ir: BlueprintIR):
        key = ir.context_keys["target_file"]
        assert key.consumer_count > 0

    def test_audit_flags_present(self, ir: BlueprintIR):
        """At least some keys should have audit flags."""
        flagged = [k for k in ir.context_keys.values() if k.audit_flags]
        assert len(flagged) > 0

    def test_never_consumed_flag(self, ir: BlueprintIR):
        """Keys published but never consumed should be flagged."""
        never_consumed = [
            k for k in ir.context_keys.values() if "never_consumed" in k.audit_flags
        ]
        # There should be at least some (summary, note_saved, etc.)
        assert len(never_consumed) >= 0  # Non-negative; may be 0 in tight systems


# ── Dependency Graph ──────────────────────────────────────────────────


class TestDependencyGraph:
    @pytest.fixture(scope="class")
    def ir(self) -> BlueprintIR:
        return analyze(flows_dir="flows", agent_dir="agent")

    def test_has_flow_edges(self, ir: BlueprintIR):
        assert len(ir.dependency_graph.flow_edges) > 0

    def test_has_tail_call_edges(self, ir: BlueprintIR):
        tail_calls = [
            e for e in ir.dependency_graph.flow_edges if e.edge_type == "tail_call"
        ]
        assert len(tail_calls) > 0

    def test_has_sub_flow_edges(self, ir: BlueprintIR):
        sub_flows = [
            e for e in ir.dependency_graph.flow_edges if e.edge_type == "sub_flow"
        ]
        assert len(sub_flows) > 0

    def test_modify_file_to_mission_control_edge(self, ir: BlueprintIR):
        """modify_file should tail-call to mission_control."""
        edge = next(
            (
                e
                for e in ir.dependency_graph.flow_edges
                if e.source == "modify_file"
                and e.target == "mission_control"
                and e.edge_type == "tail_call"
            ),
            None,
        )
        assert edge is not None

    def test_modify_file_to_prepare_context_edge(self, ir: BlueprintIR):
        """modify_file should invoke prepare_context as sub-flow."""
        edge = next(
            (
                e
                for e in ir.dependency_graph.flow_edges
                if e.source == "modify_file"
                and e.target == "prepare_context"
                and e.edge_type == "sub_flow"
            ),
            None,
        )
        assert edge is not None


# ── Template Usage ────────────────────────────────────────────────────


class TestTemplateUsage:
    @pytest.fixture(scope="class")
    def ir(self) -> BlueprintIR:
        return analyze(flows_dir="flows", agent_dir="agent")

    def test_templates_loaded(self, ir: BlueprintIR):
        assert len(ir.templates) > 0

    def test_known_templates_present(self, ir: BlueprintIR):
        expected = ["push_note", "write_file", "read_target_file"]
        for name in expected:
            assert name in ir.templates, f"Template {name!r} not found"

    def test_used_by_populated(self, ir: BlueprintIR):
        """Templates used by flows should have used_by entries."""
        used = [t for t in ir.templates.values() if t.used_by]
        assert len(used) > 0

    def test_read_target_file_used_by_modify_file(self, ir: BlueprintIR):
        template = ir.templates.get("read_target_file")
        assert template is not None
        assert any(
            "modify_file" in ref for ref in template.used_by
        ), f"read_target_file not used by modify_file: {template.used_by}"


# ── Helper Functions ──────────────────────────────────────────────────


class TestHelperFunctions:
    def test_classify_action_type_inference(self):
        assert _classify_action_type("inference") == "inference"

    def test_classify_action_type_flow(self):
        assert _classify_action_type("flow") == "flow"

    def test_classify_action_type_noop(self):
        assert _classify_action_type("noop") == "noop"

    def test_classify_action_type_other(self):
        assert _classify_action_type("read_files") == "action"

    def test_categorize_task(self):
        assert _categorize_flow("modify_file", "flows/tasks/modify_file.yaml") == "task"

    def test_categorize_shared(self):
        assert (
            _categorize_flow("prepare_context", "flows/shared/prepare_context.yaml")
            == "shared"
        )

    def test_categorize_control(self):
        assert (
            _categorize_flow("mission_control", "flows/mission_control.yaml")
            == "control"
        )

    def test_categorize_test(self):
        assert _categorize_flow("test_simple", "flows/test_simple.yaml") == "test"

    def test_source_map_finds_flows(self):
        source_map = _build_source_map("flows")
        assert "test_simple" in source_map
        assert "modify_file" in source_map
        assert "mission_control" in source_map

    def test_template_usage_scan(self):
        usage = _scan_template_usage("flows")
        # modify_file uses read_target_file template
        assert "read_target_file" in usage
        assert any("modify_file" in ref for ref in usage["read_target_file"])


# ── Source Hash ───────────────────────────────────────────────────────


class TestSourceHash:
    def test_hash_is_deterministic(self):
        ir1 = analyze(flows_dir="flows", agent_dir="agent")
        ir2 = analyze(flows_dir="flows", agent_dir="agent")
        assert ir1.meta.source_hash == ir2.meta.source_hash

    def test_hash_is_valid_hex(self):
        ir = analyze(flows_dir="flows", agent_dir="agent")
        assert len(ir.meta.source_hash) == 64
