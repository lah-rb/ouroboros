"""Tests for agent/loader.py — YAML parsing and semantic validation."""

import os
import tempfile

import pytest

from agent.loader import (
    FlowLoadError,
    FlowValidationError,
    load_all_flows,
    load_flow,
    load_flow_from_dict,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _minimal_flow_dict(**overrides):
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


def _write_yaml(directory, filename, content):
    """Write a YAML string to a file in the given directory."""
    path = os.path.join(directory, filename)
    with open(path, "w") as f:
        f.write(content)
    return path


# ── load_flow_from_dict Tests ─────────────────────────────────────────


class TestLoadFlowFromDict:
    def test_minimal_flow_loads(self):
        flow = load_flow_from_dict(_minimal_flow_dict())
        assert flow.flow == "test_flow"
        assert flow.entry == "start"

    def test_flow_with_multiple_steps(self):
        data = {
            "flow": "multi_step",
            "steps": {
                "begin": {
                    "action": "noop",
                    "resolver": {
                        "type": "rule",
                        "rules": [
                            {"condition": "true", "transition": "end"},
                        ],
                    },
                },
                "end": {
                    "action": "log_completion",
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "begin",
        }
        flow = load_flow_from_dict(data)
        assert len(flow.steps) == 2
        assert flow.steps["begin"].resolver.rules[0].transition == "end"

    def test_missing_flow_name_raises(self):
        data = _minimal_flow_dict()
        del data["flow"]
        with pytest.raises(FlowLoadError):
            load_flow_from_dict(data)

    def test_missing_steps_raises(self):
        data = _minimal_flow_dict()
        del data["steps"]
        with pytest.raises(FlowLoadError):
            load_flow_from_dict(data)

    def test_missing_entry_raises(self):
        data = _minimal_flow_dict()
        del data["entry"]
        with pytest.raises(FlowLoadError):
            load_flow_from_dict(data)

    def test_entry_references_nonexistent_step_raises(self):
        data = _minimal_flow_dict(entry="ghost_step")
        with pytest.raises(FlowLoadError, match="Entry step"):
            load_flow_from_dict(data)


# ── Semantic Validation Tests ─────────────────────────────────────────


class TestSemanticValidation:
    def test_transition_target_must_exist(self):
        data = {
            "flow": "bad_transition",
            "steps": {
                "start": {
                    "action": "noop",
                    "resolver": {
                        "type": "rule",
                        "rules": [
                            {
                                "condition": "true",
                                "transition": "nonexistent",
                            },
                        ],
                    },
                },
                "end": {
                    "action": "noop",
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "start",
        }
        with pytest.raises(FlowValidationError, match="nonexistent"):
            load_flow_from_dict(data)

    def test_no_terminal_steps_raises(self):
        data = {
            "flow": "no_terminal",
            "steps": {
                "start": {
                    "action": "noop",
                    "resolver": {
                        "type": "rule",
                        "rules": [
                            {"condition": "true", "transition": "start"},
                        ],
                    },
                },
            },
            "entry": "start",
        }
        with pytest.raises(FlowValidationError, match="no terminal"):
            load_flow_from_dict(data)

    def test_non_terminal_without_resolver_raises(self):
        data = {
            "flow": "no_resolver",
            "steps": {
                "start": {
                    "action": "noop",
                    # No resolver, not terminal
                },
                "end": {
                    "action": "noop",
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "start",
        }
        with pytest.raises(FlowValidationError, match="no resolver"):
            load_flow_from_dict(data)

    def test_unreachable_terminal_raises(self):
        data = {
            "flow": "unreachable",
            "steps": {
                "start": {
                    "action": "noop",
                    "resolver": {
                        "type": "rule",
                        "rules": [
                            {"condition": "true", "transition": "middle"},
                        ],
                    },
                },
                "middle": {
                    "action": "noop",
                    "resolver": {
                        "type": "rule",
                        "rules": [
                            {"condition": "true", "transition": "start"},
                        ],
                    },
                },
                "unreachable_end": {
                    "action": "noop",
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "start",
        }
        with pytest.raises(FlowValidationError, match="reachable"):
            load_flow_from_dict(data)

    def test_missing_context_key_publisher_raises(self):
        data = {
            "flow": "missing_publisher",
            "steps": {
                "start": {
                    "action": "noop",
                    "context": {"required": ["data_that_nobody_publishes"]},
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "start",
        }
        with pytest.raises(FlowValidationError, match="no step publishes"):
            load_flow_from_dict(data)

    def test_context_key_from_input_is_ok(self):
        """Required context keys that match flow inputs should not raise."""
        data = {
            "flow": "input_provides",
            "input": {"required": ["reason"], "optional": []},
            "steps": {
                "start": {
                    "action": "noop",
                    "context": {"required": ["reason"]},
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "start",
        }
        # Should not raise
        flow = load_flow_from_dict(data)
        assert flow.flow == "input_provides"

    def test_context_key_from_publisher_is_ok(self):
        """Required context keys that are published by another step should not raise."""
        data = {
            "flow": "published_provides",
            "steps": {
                "start": {
                    "action": "noop",
                    "publishes": ["data"],
                    "resolver": {
                        "type": "rule",
                        "rules": [
                            {"condition": "true", "transition": "end"},
                        ],
                    },
                },
                "end": {
                    "action": "noop",
                    "context": {"required": ["data"]},
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "start",
        }
        flow = load_flow_from_dict(data)
        assert flow.flow == "published_provides"

    def test_valid_multi_branch_flow(self):
        """A valid flow with multiple branches should load without errors."""
        data = {
            "flow": "branching",
            "input": {"required": ["mode"], "optional": []},
            "steps": {
                "check": {
                    "action": "noop",
                    "resolver": {
                        "type": "rule",
                        "rules": [
                            {
                                "condition": "context.mode == 'a'",
                                "transition": "path_a",
                            },
                            {"condition": "true", "transition": "path_b"},
                        ],
                    },
                },
                "path_a": {
                    "action": "noop",
                    "terminal": True,
                    "status": "success",
                },
                "path_b": {
                    "action": "noop",
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "check",
        }
        flow = load_flow_from_dict(data)
        assert len(flow.steps) == 3


# ── File Loading Tests ────────────────────────────────────────────────


class TestLoadFlowFromFile:
    def test_load_yaml_file(self):
        flow = load_flow("flows/test_simple.yaml")
        assert flow.flow == "test_simple"
        assert flow.entry == "read_file"
        assert "read_file" in flow.steps
        assert "complete" in flow.steps

    def test_load_branching_flow(self):
        flow = load_flow("flows/test_branching.yaml")
        assert flow.flow == "test_branching"
        assert "fast_path" in flow.steps
        assert "slow_path" in flow.steps

    def test_nonexistent_file_raises(self):
        with pytest.raises(FlowLoadError, match="not found"):
            load_flow("flows/nonexistent.yaml")

    def test_non_yaml_file_raises(self):
        with pytest.raises(FlowLoadError, match=".yaml"):
            load_flow("pyproject.toml")

    def test_invalid_yaml_raises(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write(": invalid: yaml: {{{\n")
            f.flush()
            try:
                with pytest.raises(FlowLoadError, match="YAML parse error"):
                    load_flow(f.name)
            finally:
                os.unlink(f.name)


class TestLoadAllFlows:
    def test_loads_all_flows_from_directory(self):
        flows = load_all_flows("flows")
        assert "test_simple" in flows
        assert "test_branching" in flows
        assert len(flows) >= 2

    def test_nonexistent_directory_raises(self):
        with pytest.raises(FlowLoadError, match="not found"):
            load_all_flows("nonexistent_dir")

    def test_skips_registry_yaml(self):
        flows = load_all_flows("flows")
        # registry.yaml should not be loaded as a flow
        assert "registry" not in flows
