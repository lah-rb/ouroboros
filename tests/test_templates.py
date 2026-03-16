"""Tests for the step template system — models, merge semantics, schema validation."""

import os
import tempfile

import pytest
import yaml

from agent.models import ParamSchemaEntry, StepTemplate, StepTemplateRegistry
from agent.loader import (
    FlowValidationError,
    _merge_step_with_template,
    load_template_registry,
    load_flow_with_templates,
    validate_params_against_schema,
)

# ── ParamSchemaEntry Model Tests ──────────────────────────────────────


class TestParamSchemaEntry:
    def test_basic_string_entry(self):
        entry = ParamSchemaEntry(type="string", required=True, description="A name")
        assert entry.type == "string"
        assert entry.required is True

    def test_enum_on_string_type(self):
        entry = ParamSchemaEntry(
            type="string", enum=["a", "b", "c"], description="Choice"
        )
        assert entry.enum == ["a", "b", "c"]

    def test_enum_on_non_string_raises(self):
        with pytest.raises(ValueError, match="enum is only valid"):
            ParamSchemaEntry(type="integer", enum=["a", "b"])

    def test_pattern_on_non_string_raises(self):
        with pytest.raises(ValueError, match="pattern is only valid"):
            ParamSchemaEntry(type="integer", pattern=r"\d+")

    def test_min_max_on_numeric_types(self):
        entry = ParamSchemaEntry(type="integer", min=0, max=100)
        assert entry.min == 0
        assert entry.max == 100

        entry_f = ParamSchemaEntry(type="float", min=0.0, max=1.0)
        assert entry_f.min == 0.0

    def test_min_max_on_non_numeric_raises(self):
        with pytest.raises(ValueError, match="min/max are only valid"):
            ParamSchemaEntry(type="string", min=0)

    def test_items_on_list_type(self):
        entry = ParamSchemaEntry(
            type="list", items={"type": "string"}, min_items=1, max_items=10
        )
        assert entry.items == {"type": "string"}
        assert entry.min_items == 1

    def test_items_on_non_list_raises(self):
        with pytest.raises(ValueError, match="items/min_items/max_items"):
            ParamSchemaEntry(type="string", items={"type": "string"})

    def test_default_matches_type(self):
        entry = ParamSchemaEntry(type="string", default="hello")
        assert entry.default == "hello"

    def test_default_type_mismatch_raises(self):
        with pytest.raises(ValueError, match="doesn't match type"):
            ParamSchemaEntry(type="integer", default="not_a_number")

    def test_jinja2_default_skips_type_check(self):
        entry = ParamSchemaEntry(type="string", default="{{ input.x }}")
        assert "{{" in entry.default

    def test_integer_default(self):
        entry = ParamSchemaEntry(type="integer", default=42)
        assert entry.default == 42

    def test_boolean_default(self):
        entry = ParamSchemaEntry(type="boolean", default=False)
        assert entry.default is False

    def test_list_default(self):
        entry = ParamSchemaEntry(type="list", default=[])
        assert entry.default == []

    def test_dict_default(self):
        entry = ParamSchemaEntry(type="dict", default={})
        assert entry.default == {}


# ── StepTemplate Model Tests ─────────────────────────────────────────


class TestStepTemplate:
    def test_minimal_template(self):
        t = StepTemplate(action="noop")
        assert t.action == "noop"
        assert t.params is None
        assert t.param_schema is None

    def test_full_template(self):
        t = StepTemplate(
            action="push_note",
            description="Save a note",
            context={"required": [], "optional": ["reflection"]},
            params={"category": "general", "content_key": "reflection"},
            publishes=["note_saved"],
            param_schema={
                "category": ParamSchemaEntry(
                    type="string",
                    enum=["general", "task_learning"],
                ),
            },
        )
        assert t.action == "push_note"
        assert "category" in t.param_schema


class TestStepTemplateRegistry:
    def test_empty_registry(self):
        reg = StepTemplateRegistry(templates={})
        assert len(reg.templates) == 0
        assert reg.version == 1

    def test_registry_with_templates(self):
        reg = StepTemplateRegistry(
            templates={
                "my_noop": StepTemplate(action="noop", description="Do nothing"),
                "my_write": StepTemplate(
                    action="write_file",
                    params={"path": "out.txt"},
                ),
            }
        )
        assert len(reg.templates) == 2
        assert reg.templates["my_noop"].action == "noop"


# ── Merge Semantics Tests ────────────────────────────────────────────


class TestMergeSemantics:
    def _make_template(self, **kwargs):
        defaults = {"action": "noop", "description": "template desc"}
        defaults.update(kwargs)
        return StepTemplate(**defaults)

    def test_action_from_template(self):
        template = self._make_template(action="push_note")
        result = _merge_step_with_template(template, {})
        assert result["action"] == "push_note"

    def test_action_overridden_by_step(self):
        template = self._make_template(action="push_note")
        result = _merge_step_with_template(template, {"action": "noop"})
        assert result["action"] == "noop"

    def test_description_from_template(self):
        template = self._make_template(description="Template says")
        result = _merge_step_with_template(template, {})
        assert result["description"] == "Template says"

    def test_description_overridden_by_step(self):
        template = self._make_template(description="Template says")
        result = _merge_step_with_template(template, {"description": "Step says"})
        assert result["description"] == "Step says"

    def test_context_union_merge(self):
        template = self._make_template(
            context={"required": ["a", "b"], "optional": ["c"]}
        )
        result = _merge_step_with_template(
            template, {"context": {"required": ["b", "d"], "optional": ["e"]}}
        )
        assert set(result["context"]["required"]) == {"a", "b", "d"}
        assert set(result["context"]["optional"]) == {"c", "e"}

    def test_context_from_template_only(self):
        template = self._make_template(context={"required": ["x"], "optional": []})
        result = _merge_step_with_template(template, {})
        assert "x" in result["context"]["required"]

    def test_params_deep_merge(self):
        template = self._make_template(params={"a": 1, "b": 2, "c": 3})
        result = _merge_step_with_template(template, {"params": {"b": 99, "d": 4}})
        assert result["params"]["a"] == 1  # from template
        assert result["params"]["b"] == 99  # overridden by step
        assert result["params"]["c"] == 3  # from template
        assert result["params"]["d"] == 4  # new from step

    def test_config_deep_merge(self):
        template = self._make_template(config={"temperature": 0.5, "max_tokens": 100})
        result = _merge_step_with_template(template, {"config": {"temperature": 0.1}})
        assert result["config"]["temperature"] == 0.1
        assert result["config"]["max_tokens"] == 100

    def test_resolver_always_from_step(self):
        template = self._make_template()
        resolver = {
            "type": "rule",
            "rules": [{"condition": "true", "transition": "done"}],
        }
        result = _merge_step_with_template(template, {"resolver": resolver})
        assert result["resolver"] == resolver

    def test_template_never_provides_resolver(self):
        template = self._make_template()
        result = _merge_step_with_template(template, {})
        assert "resolver" not in result

    def test_terminal_and_status_from_step(self):
        template = self._make_template()
        result = _merge_step_with_template(
            template, {"terminal": True, "status": "success"}
        )
        assert result["terminal"] is True
        assert result["status"] == "success"

    def test_tail_call_from_step(self):
        template = self._make_template()
        tc = {"flow": "mission_control", "input_map": {"id": "123"}}
        result = _merge_step_with_template(template, {"tail_call": tc})
        assert result["tail_call"] == tc

    def test_publishes_from_template(self):
        template = self._make_template(publishes=["note_saved"])
        result = _merge_step_with_template(template, {})
        assert result["publishes"] == ["note_saved"]

    def test_publishes_overridden_by_step(self):
        template = self._make_template(publishes=["note_saved"])
        result = _merge_step_with_template(template, {"publishes": ["custom_key"]})
        assert result["publishes"] == ["custom_key"]

    def test_flow_and_input_map_from_template(self):
        template = self._make_template(
            action="flow",
            flow="prepare_context",
            input_map={"working_directory": "{{ input.wd }}"},
        )
        result = _merge_step_with_template(template, {})
        assert result["flow"] == "prepare_context"
        assert result["input_map"]["working_directory"] == "{{ input.wd }}"

    def test_prompt_from_step_only(self):
        template = self._make_template()
        result = _merge_step_with_template(template, {"prompt": "Generate something"})
        assert result["prompt"] == "Generate something"

    def test_no_prompt_if_not_in_step(self):
        template = self._make_template()
        result = _merge_step_with_template(template, {})
        assert "prompt" not in result


# ── Param Schema Validation Tests ─────────────────────────────────────


class TestParamSchemaValidation:
    def test_no_schema_passes(self):
        warnings = validate_params_against_schema({"a": 1}, None, "step", "template")
        assert warnings == []

    def test_required_param_missing_raises(self):
        schema = {
            "name": ParamSchemaEntry(type="string", required=True),
        }
        with pytest.raises(FlowValidationError, match="required param 'name'"):
            validate_params_against_schema({}, schema, "step", "template")

    def test_required_param_present_passes(self):
        schema = {
            "name": ParamSchemaEntry(type="string", required=True),
        }
        validate_params_against_schema({"name": "hello"}, schema, "step", "template")

    def test_default_applied_when_missing(self):
        schema = {
            "count": ParamSchemaEntry(type="integer", default=5),
        }
        params = {}
        validate_params_against_schema(params, schema, "step", "template")
        assert params["count"] == 5

    def test_jinja2_string_skips_type_check(self):
        schema = {
            "path": ParamSchemaEntry(type="string", required=True),
        }
        # Jinja2 template string should pass even though it can't be validated
        validate_params_against_schema(
            {"path": "{{ input.file }}"}, schema, "step", "template"
        )

    def test_type_mismatch_raises(self):
        schema = {
            "count": ParamSchemaEntry(type="integer"),
        }
        with pytest.raises(FlowValidationError, match="expected integer"):
            validate_params_against_schema(
                {"count": "not_a_number"}, schema, "step", "template"
            )

    def test_enum_valid_value(self):
        schema = {
            "mode": ParamSchemaEntry(type="string", enum=["fast", "slow"]),
        }
        validate_params_against_schema({"mode": "fast"}, schema, "step", "template")

    def test_enum_invalid_value_raises(self):
        schema = {
            "mode": ParamSchemaEntry(type="string", enum=["fast", "slow"]),
        }
        with pytest.raises(FlowValidationError, match="not in allowed values"):
            validate_params_against_schema(
                {"mode": "turbo"}, schema, "step", "template"
            )

    def test_min_violation_raises(self):
        schema = {
            "count": ParamSchemaEntry(type="integer", min=1),
        }
        with pytest.raises(FlowValidationError, match="below minimum"):
            validate_params_against_schema({"count": 0}, schema, "step", "template")

    def test_max_violation_raises(self):
        schema = {
            "count": ParamSchemaEntry(type="integer", max=10),
        }
        with pytest.raises(FlowValidationError, match="above maximum"):
            validate_params_against_schema({"count": 11}, schema, "step", "template")

    def test_list_min_items_violation(self):
        schema = {
            "tags": ParamSchemaEntry(type="list", min_items=1),
        }
        with pytest.raises(FlowValidationError, match="minimum is 1"):
            validate_params_against_schema({"tags": []}, schema, "step", "template")

    def test_list_max_items_violation(self):
        schema = {
            "tags": ParamSchemaEntry(type="list", max_items=2),
        }
        with pytest.raises(FlowValidationError, match="maximum is 2"):
            validate_params_against_schema(
                {"tags": ["a", "b", "c"]}, schema, "step", "template"
            )

    def test_none_value_with_no_default_skipped(self):
        schema = {
            "optional_field": ParamSchemaEntry(type="string"),
        }
        # None value with no default and not required — just skip
        validate_params_against_schema({}, schema, "step", "template")

    def test_range_within_bounds(self):
        schema = {
            "budget": ParamSchemaEntry(type="integer", min=1, max=20),
        }
        validate_params_against_schema({"budget": 10}, schema, "step", "template")


# ── Template Registry Loading Tests ───────────────────────────────────


class TestLoadTemplateRegistry:
    def test_load_from_existing_file(self):
        reg = load_template_registry("flows")
        assert len(reg.templates) > 0
        assert "push_note" in reg.templates
        assert "write_file" in reg.templates

    def test_load_from_nonexistent_dir(self):
        reg = load_template_registry("/tmp/nonexistent_dir_xyz")
        assert len(reg.templates) == 0

    def test_load_returns_empty_when_no_shared_dir(self, tmp_path):
        reg = load_template_registry(str(tmp_path))
        assert len(reg.templates) == 0

    def test_loaded_templates_have_correct_structure(self):
        reg = load_template_registry("flows")
        push_note = reg.templates["push_note"]
        assert push_note.action == "push_note"
        assert push_note.publishes == ["note_saved"]
        assert push_note.param_schema is not None
        assert "category" in push_note.param_schema


# ── Flow Loading with Templates Tests ─────────────────────────────────


class TestLoadFlowWithTemplates:
    def _write_flow_yaml(self, tmp_dir, filename, content):
        path = os.path.join(tmp_dir, filename)
        with open(path, "w") as f:
            yaml.dump(content, f)
        return path

    def test_flow_without_templates_loads_normally(self):
        """A flow with no 'use' directives should load like normal."""
        reg = StepTemplateRegistry(templates={})
        flow = load_flow_with_templates("flows/test_simple.yaml", reg)
        assert flow.flow == "test_simple"

    def test_unknown_template_raises(self, tmp_path):
        reg = StepTemplateRegistry(templates={})
        flow_data = {
            "flow": "test_unknown",
            "steps": {
                "start": {
                    "use": "nonexistent_template",
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "start",
        }
        flow_path = self._write_flow_yaml(str(tmp_path), "test.yaml", flow_data)
        with pytest.raises(FlowValidationError, match="unknown template"):
            load_flow_with_templates(flow_path, reg)

    def test_template_expansion_produces_valid_flow(self, tmp_path):
        """A flow using a template should expand and validate."""
        reg = StepTemplateRegistry(
            templates={
                "my_noop": StepTemplate(
                    action="noop",
                    description="A noop from template",
                    publishes=["result_key"],
                ),
            }
        )
        flow_data = {
            "flow": "test_expansion",
            "steps": {
                "start": {
                    "use": "my_noop",
                    "resolver": {
                        "type": "rule",
                        "rules": [{"condition": "true", "transition": "done"}],
                    },
                },
                "done": {
                    "action": "noop",
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "start",
        }
        flow_path = self._write_flow_yaml(str(tmp_path), "test.yaml", flow_data)
        flow = load_flow_with_templates(flow_path, reg)
        assert flow.flow == "test_expansion"
        assert flow.steps["start"].action == "noop"
        assert flow.steps["start"].description == "A noop from template"
        assert flow.steps["start"].publishes == ["result_key"]

    def test_step_overrides_template_params(self, tmp_path):
        reg = StepTemplateRegistry(
            templates={
                "my_action": StepTemplate(
                    action="transform",
                    params={"a": 1, "b": 2},
                ),
            }
        )
        flow_data = {
            "flow": "test_override",
            "steps": {
                "start": {
                    "use": "my_action",
                    "params": {"b": 99},
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "start",
        }
        flow_path = self._write_flow_yaml(str(tmp_path), "test.yaml", flow_data)
        flow = load_flow_with_templates(flow_path, reg)
        assert flow.steps["start"].params["a"] == 1
        assert flow.steps["start"].params["b"] == 99

    def test_schema_validation_applied_during_load(self, tmp_path):
        reg = StepTemplateRegistry(
            templates={
                "strict": StepTemplate(
                    action="noop",
                    param_schema={
                        "required_field": ParamSchemaEntry(
                            type="string", required=True
                        ),
                    },
                ),
            }
        )
        flow_data = {
            "flow": "test_schema_fail",
            "steps": {
                "start": {
                    "use": "strict",
                    "terminal": True,
                    "status": "success",
                },
            },
            "entry": "start",
        }
        flow_path = self._write_flow_yaml(str(tmp_path), "test.yaml", flow_data)
        with pytest.raises(FlowValidationError, match="required param"):
            load_flow_with_templates(flow_path, reg)
