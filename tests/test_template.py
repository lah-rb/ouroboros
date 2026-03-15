"""Tests for agent/template.py — Jinja2 template rendering."""

import pytest

from agent.template import TemplateError, render_params, render_template


class TestRenderTemplate:
    def test_simple_variable(self):
        result = render_template("{{ name }}", {"name": "hello"})
        assert result == "hello"

    def test_nested_variable(self):
        result = render_template(
            "{{ input.target_file_path }}",
            {"input": {"target_file_path": "/foo/bar.py"}},
        )
        assert result == "/foo/bar.py"

    def test_multiple_variables(self):
        result = render_template(
            "File: {{ input.path }}, Reason: {{ input.reason }}",
            {"input": {"path": "test.py", "reason": "fix bug"}},
        )
        assert result == "File: test.py, Reason: fix bug"

    def test_context_variable(self):
        result = render_template(
            "Content: {{ context.target_file.content }}",
            {"context": {"target_file": {"content": "hello world"}}},
        )
        assert result == "Content: hello world"

    def test_undefined_variable_raises(self):
        with pytest.raises(TemplateError, match="undefined"):
            render_template("{{ missing_var }}", {})

    def test_undefined_nested_raises(self):
        with pytest.raises(TemplateError, match="undefined"):
            render_template("{{ input.missing }}", {"input": {}})

    def test_plain_string_no_template(self):
        result = render_template("just a plain string", {})
        assert result == "just a plain string"

    def test_boolean_rendering(self):
        result = render_template("{{ flag }}", {"flag": True})
        assert result == "True"

    def test_integer_rendering(self):
        result = render_template("{{ count }}", {"count": 42})
        assert result == "42"


class TestRenderParams:
    def test_simple_params(self):
        params = {"target": "{{ input.path }}"}
        result = render_params(params, {"input": {"path": "/foo/bar.py"}})
        assert result == {"target": "/foo/bar.py"}

    def test_non_template_strings_pass_through(self):
        params = {"mode": "fast", "count": 5}
        result = render_params(params, {})
        assert result == {"mode": "fast", "count": 5}

    def test_mixed_params(self):
        params = {
            "target": "{{ input.path }}",
            "discover_imports": True,
            "label": "static_value",
        }
        result = render_params(params, {"input": {"path": "test.py"}})
        assert result == {
            "target": "test.py",
            "discover_imports": True,
            "label": "static_value",
        }

    def test_nested_dict_params(self):
        params = {
            "outer": {
                "inner": "{{ input.value }}",
                "static": "unchanged",
            }
        }
        result = render_params(params, {"input": {"value": "rendered"}})
        assert result == {"outer": {"inner": "rendered", "static": "unchanged"}}

    def test_list_params(self):
        params = {
            "items": ["{{ input.a }}", "static", "{{ input.b }}"],
        }
        result = render_params(params, {"input": {"a": "X", "b": "Y"}})
        assert result == {"items": ["X", "static", "Y"]}

    def test_none_and_numbers_pass_through(self):
        params = {"a": None, "b": 42, "c": 3.14}
        result = render_params(params, {})
        assert result == {"a": None, "b": 42, "c": 3.14}
