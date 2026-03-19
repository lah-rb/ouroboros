"""Tests for agent/visualize.py — flow visualization."""

import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from agent.loader import load_flow_from_dict
from agent.visualize import (
    flow_to_mermaid,
    flow_to_dot,
    all_flows_to_mermaid,
    all_flows_to_dot,
    render_to_svg,
    _step_type,
    _step_icon,
    _sanitize_mermaid_text,
    _mermaid_edge,
)

# ── Test fixtures ─────────────────────────────────────────────────────


def _simple_flow_dict():
    """A minimal flow with action, inference, and terminal steps."""
    return {
        "flow": "test_viz",
        "version": 1,
        "description": "Test flow for visualization",
        "input": {"required": ["target"], "optional": []},
        "steps": {
            "start": {
                "action": "read_files",
                "description": "Read input files",
                "params": {"target": "{{ input.target }}"},
                "resolver": {
                    "type": "rule",
                    "rules": [
                        {
                            "condition": "result.file_found == true",
                            "transition": "analyze",
                        },
                        {
                            "condition": "result.file_found == false",
                            "transition": "fail",
                        },
                    ],
                },
                "publishes": ["file_content"],
            },
            "analyze": {
                "action": "inference",
                "description": "Analyze the file content",
                "context": {"required": ["file_content"]},
                "prompt": "Analyze this file: {{ context.file_content }}",
                "resolver": {
                    "type": "llm_menu",
                    "prompt": "What next?",
                    "options": {
                        "done": {"description": "Analysis complete"},
                        "start": {
                            "description": "Need more context",
                            "target": "start",
                        },
                    },
                },
                "publishes": ["analysis"],
            },
            "done": {
                "action": "log_completion",
                "description": "Complete successfully",
                "terminal": True,
                "status": "success",
                "publishes": ["summary"],
            },
            "fail": {
                "action": "log_completion",
                "description": "File not found",
                "terminal": True,
                "status": "abandoned",
                "publishes": ["summary"],
            },
        },
        "entry": "start",
    }


def _tail_call_flow_dict():
    """A flow with a tail-call step."""
    return {
        "flow": "task_flow",
        "version": 1,
        "description": "Flow that tail-calls back to mission_control",
        "input": {"required": ["task_id"], "optional": []},
        "steps": {
            "work": {
                "action": "noop",
                "description": "Do work",
                "resolver": {
                    "type": "rule",
                    "rules": [{"condition": "true", "transition": "complete"}],
                },
                "publishes": ["result"],
            },
            "complete": {
                "action": "log_completion",
                "description": "Done, return to mission_control",
                "terminal": True,
                "status": "success",
                "tail_call": {
                    "flow": "mission_control",
                    "input_map": {"mission_id": "{{ meta.mission_id }}"},
                },
                "publishes": ["summary"],
            },
        },
        "entry": "work",
    }


def _subflow_flow_dict():
    """A flow that invokes a sub-flow."""
    return {
        "flow": "parent_flow",
        "version": 1,
        "description": "Flow that invokes a child",
        "input": {"required": ["task"], "optional": []},
        "steps": {
            "prepare": {
                "action": "noop",
                "description": "Prepare inputs",
                "resolver": {
                    "type": "rule",
                    "rules": [{"condition": "true", "transition": "child"}],
                },
                "publishes": ["prep"],
            },
            "child": {
                "action": "flow",
                "description": "Run child flow",
                "flow": "prepare_context",
                "input_map": {"working_directory": "."},
                "resolver": {
                    "type": "rule",
                    "rules": [{"condition": "true", "transition": "done"}],
                },
                "publishes": ["child_result"],
            },
            "done": {
                "action": "noop",
                "description": "Finished",
                "terminal": True,
                "status": "success",
                "publishes": ["summary"],
            },
        },
        "entry": "prepare",
    }


# ── Step classification tests ─────────────────────────────────────────


class TestStepClassification:
    def test_terminal_step(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        assert _step_type("done", flow.steps["done"]) == "terminal"

    def test_inference_step(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        assert _step_type("analyze", flow.steps["analyze"]) == "inference"

    def test_action_step(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        assert _step_type("start", flow.steps["start"]) == "action"

    def test_tail_call_step(self):
        flow = load_flow_from_dict(_tail_call_flow_dict())
        # Terminal + tail_call → terminal takes precedence
        assert _step_type("complete", flow.steps["complete"]) == "terminal"

    def test_subflow_step(self):
        flow = load_flow_from_dict(_subflow_flow_dict())
        assert _step_type("child", flow.steps["child"]) == "subflow"

    def test_icons_exist_for_all_types(self):
        for stype in ("action", "inference", "subflow", "terminal", "tail_call"):
            icon = _step_icon(stype)
            assert icon, f"No icon for type {stype}"
            assert len(icon) > 0


# ── Mermaid output tests ──────────────────────────────────────────────


class TestMermaidSingleFlow:
    def test_contains_flowchart_header(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        output = flow_to_mermaid(flow)
        assert output.startswith("flowchart TD")

    def test_contains_flow_name_comment(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        output = flow_to_mermaid(flow)
        assert "test_viz" in output

    def test_contains_all_step_nodes(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        output = flow_to_mermaid(flow)
        for step_name in ("start", "analyze", "done", "fail"):
            assert step_name in output, f"Missing step node: {step_name}"

    def test_contains_transition_edges(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        output = flow_to_mermaid(flow)
        # Rule-based transitions from start
        assert "start" in output and "analyze" in output
        assert "fail" in output

    def test_entry_step_styled(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        output = flow_to_mermaid(flow)
        assert "style start stroke-width:3px" in output

    def test_terminal_steps_colored(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        output = flow_to_mermaid(flow)
        # Success terminal → green
        assert "style done fill:#9f9" in output
        # Abandoned terminal → red
        assert "style fail fill:#f99" in output

    def test_llm_menu_options_as_dashed_edges(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        output = flow_to_mermaid(flow)
        # Dashed arrows for LLM menu options
        assert "-.->" in output

    def test_tail_call_creates_external_node(self):
        flow = load_flow_from_dict(_tail_call_flow_dict())
        output = flow_to_mermaid(flow)
        assert "mission_control" in output
        assert "tail-call" in output

    def test_inference_node_uses_hexagon(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        output = flow_to_mermaid(flow)
        # Mermaid hexagon syntax
        assert "{{" in output  # Hexagon delimiters

    def test_subflow_node_uses_double_border(self):
        flow = load_flow_from_dict(_subflow_flow_dict())
        output = flow_to_mermaid(flow)
        # Double-bordered rectangle: [[...]]
        assert "[[" in output


class TestMermaidSystemView:
    def test_all_flows_produces_output(self):
        flows = {
            "test_viz": load_flow_from_dict(_simple_flow_dict()),
            "task_flow": load_flow_from_dict(_tail_call_flow_dict()),
        }
        output = all_flows_to_mermaid(flows)
        assert "flowchart TD" in output
        assert "test_viz" in output
        assert "task_flow" in output

    def test_tail_call_edges_between_flows(self):
        flows = {
            "task_flow": load_flow_from_dict(_tail_call_flow_dict()),
            "mission_control": load_flow_from_dict(_simple_flow_dict()),
        }
        # Rename mission_control flow
        mc_dict = _simple_flow_dict()
        mc_dict["flow"] = "mission_control"
        flows["mission_control"] = load_flow_from_dict(mc_dict)
        output = all_flows_to_mermaid(flows)
        assert "task_flow" in output
        assert "mission_control" in output
        assert "-.->" in output  # Tail-call edge

    def test_subflow_edges_between_flows(self):
        parent_dict = _subflow_flow_dict()
        child_dict = _simple_flow_dict()
        child_dict["flow"] = "prepare_context"
        flows = {
            "parent_flow": load_flow_from_dict(parent_dict),
            "prepare_context": load_flow_from_dict(child_dict),
        }
        output = all_flows_to_mermaid(flows)
        assert "==>" in output  # Sub-flow edge

    def test_detailed_mode_shows_steps(self):
        flows = {"test_viz": load_flow_from_dict(_simple_flow_dict())}
        output = all_flows_to_mermaid(flows, show_internal_steps=True)
        assert "subgraph" in output
        assert "test_viz__start" in output


# ── DOT output tests ──────────────────────────────────────────────────


class TestDotSingleFlow:
    def test_valid_dot_structure(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        output = flow_to_dot(flow)
        assert output.startswith('digraph "test_viz"')
        assert output.strip().endswith("}")

    def test_contains_all_nodes(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        output = flow_to_dot(flow)
        for step in ("start", "analyze", "done", "fail"):
            assert step in output

    def test_entry_node_bold(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        output = flow_to_dot(flow)
        assert "penwidth=3" in output

    def test_terminal_colored(self):
        flow = load_flow_from_dict(_simple_flow_dict())
        output = flow_to_dot(flow)
        assert "#ccffcc" in output  # Green for success
        assert "#ffcccc" in output  # Red for abandoned


class TestDotSystemView:
    def test_system_dot_structure(self):
        flows = {"test_viz": load_flow_from_dict(_simple_flow_dict())}
        output = all_flows_to_dot(flows)
        assert output.startswith('digraph "ouroboros_system"')
        assert output.strip().endswith("}")

    def test_tail_call_edges(self):
        mc_dict = _simple_flow_dict()
        mc_dict["flow"] = "mission_control"
        flows = {
            "task_flow": load_flow_from_dict(_tail_call_flow_dict()),
            "mission_control": load_flow_from_dict(mc_dict),
        }
        output = all_flows_to_dot(flows)
        assert "task_flow" in output
        assert "mission_control" in output
        assert "dashed" in output


# ── Mermaid sanitization tests ────────────────────────────────────────


class TestSanitizeMermaidText:
    def test_brackets_replaced(self):
        result = _sanitize_mermaid_text("len([x for x in items])")
        assert "[" not in result
        assert "]" not in result

    def test_braces_replaced(self):
        result = _sanitize_mermaid_text("context.get('mission', {})")
        assert "{" not in result
        assert "}" not in result

    def test_parens_replaced(self):
        result = _sanitize_mermaid_text("len(items)")
        assert "(" not in result
        assert ")" not in result

    def test_pipes_replaced(self):
        result = _sanitize_mermaid_text("a | b")
        assert "|" not in result

    def test_angle_brackets_replaced(self):
        result = _sanitize_mermaid_text("a > b and c < d")
        assert ">" not in result
        assert "<" not in result

    def test_hash_replaced(self):
        result = _sanitize_mermaid_text("item #5")
        assert "#" not in result

    def test_whitespace_normalized(self):
        result = _sanitize_mermaid_text("line one\n  line two\n  line three")
        assert "\n" not in result
        assert "  " not in result
        assert "line one line two line three" in result

    def test_plain_text_unchanged(self):
        result = _sanitize_mermaid_text("result.status == 'active'")
        assert result == "result.status == 'active'"

    def test_complex_condition_sanitized(self):
        """The exact condition that caused the original parse error."""
        cond = (
            "len([t for t in context.get('mission', {}).plan "
            "if t.status == 'complete']) % 5 == 0"
        )
        result = _sanitize_mermaid_text(cond)
        # No Mermaid-special characters remain
        for char in "[]{}()|<>#":
            assert char not in result, f"Character {char!r} still present in: {result}"


class TestMermaidEdgeSanitization:
    def test_edge_label_with_brackets_is_safe(self):
        edge = _mermaid_edge("a", "b", label="len([x]) > 0")
        # The output should NOT contain raw [ or ]
        # (The edge format is: "    a -->|label| b")
        label_part = edge.split("|")[1]
        assert "[" not in label_part
        assert "]" not in label_part

    def test_edge_with_multiline_condition_collapsed(self):
        cond = "result.x == true\nand result.y == false"
        edge = _mermaid_edge("step_a", "step_b", label=cond)
        assert "\n" not in edge

    def test_edge_truncation_still_works(self):
        long_label = "a" * 100
        edge = _mermaid_edge("x", "y", label=long_label)
        label_part = edge.split("|")[1]
        assert len(label_part) <= 50
        assert label_part.endswith("...")


# ── SVG export tests ─────────────────────────────────────────────────


class TestRenderToSvg:
    def test_invalid_format_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported source_format"):
            render_to_svg(
                "graph TD; A-->B", source_format="png", output_path="/tmp/out.svg"
            )

    @patch("agent.visualize.shutil.which", return_value=None)
    def test_mermaid_missing_mmdc_raises(self, mock_which):
        with pytest.raises(RuntimeError, match="mmdc"):
            render_to_svg(
                "flowchart TD\n    A-->B",
                source_format="mermaid",
                output_path="/tmp/out.svg",
            )

    @patch("agent.visualize.shutil.which", return_value=None)
    def test_dot_missing_graphviz_raises(self, mock_which):
        with pytest.raises(RuntimeError, match="dot"):
            render_to_svg(
                "digraph { A -> B }",
                source_format="dot",
                output_path="/tmp/out.svg",
            )

    @patch("agent.visualize.subprocess.run")
    @patch("agent.visualize.shutil.which", return_value="/usr/local/bin/mmdc")
    def test_mermaid_svg_calls_mmdc(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "test.svg")
            render_to_svg(
                "flowchart TD\n    A-->B",
                source_format="mermaid",
                output_path=out_path,
            )
        # mmdc should have been called
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0][0] == "/usr/local/bin/mmdc"

    @patch("agent.visualize.subprocess.run")
    @patch("agent.visualize.shutil.which", return_value="/usr/local/bin/dot")
    def test_dot_svg_calls_dot(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "test.svg")
            render_to_svg(
                "digraph { A -> B }",
                source_format="dot",
                output_path=out_path,
            )
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0][0] == "/usr/local/bin/dot"

    @patch("agent.visualize.subprocess.run")
    @patch("agent.visualize.shutil.which", return_value="/usr/local/bin/dot")
    def test_dot_nonzero_exit_raises(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stderr="syntax error", stdout=""
        )
        with pytest.raises(RuntimeError, match="dot exited with code 1"):
            render_to_svg(
                "bad dot source",
                source_format="dot",
                output_path="/tmp/out.svg",
            )
