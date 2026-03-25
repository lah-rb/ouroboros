"""Tests for agent/resolvers/ — transition resolver dispatch and rule resolver."""

import pytest

from agent.models import StepOutput
from agent.resolvers import ResolverError, resolve
from agent.resolvers.rule import RuleResolverError, resolve_rule, _DotDict

# ── Rule Resolver Tests ───────────────────────────────────────────────


class TestRuleResolver:
    def test_first_matching_rule_wins(self):
        resolver_def = {
            "type": "rule",
            "rules": [
                {"condition": "result.value == 1", "transition": "first"},
                {"condition": "result.value == 1", "transition": "second"},
            ],
        }
        output = StepOutput(result={"value": 1})
        result = resolve_rule(resolver_def, output, {}, {})
        assert result == "first"

    def test_second_rule_matches(self):
        resolver_def = {
            "type": "rule",
            "rules": [
                {"condition": "result.value == 1", "transition": "first"},
                {"condition": "result.value == 2", "transition": "second"},
            ],
        }
        output = StepOutput(result={"value": 2})
        result = resolve_rule(resolver_def, output, {}, {})
        assert result == "second"

    def test_boolean_true_condition(self):
        resolver_def = {
            "type": "rule",
            "rules": [
                {"condition": "result.file_found == true", "transition": "next"},
                {"condition": "result.file_found == false", "transition": "error"},
            ],
        }
        output = StepOutput(result={"file_found": True})
        result = resolve_rule(resolver_def, output, {}, {})
        assert result == "next"

    def test_boolean_false_condition(self):
        resolver_def = {
            "type": "rule",
            "rules": [
                {"condition": "result.file_found == true", "transition": "next"},
                {"condition": "result.file_found == false", "transition": "error"},
            ],
        }
        output = StepOutput(result={"file_found": False})
        result = resolve_rule(resolver_def, output, {}, {})
        assert result == "error"

    def test_catchall_true_rule(self):
        resolver_def = {
            "type": "rule",
            "rules": [
                {"condition": "result.special == true", "transition": "special"},
                {"condition": "true", "transition": "default"},
            ],
        }
        output = StepOutput(result={"special": False})
        result = resolve_rule(resolver_def, output, {}, {})
        assert result == "default"

    def test_context_in_condition(self):
        resolver_def = {
            "type": "rule",
            "rules": [
                {"condition": "context.mode == 'fast'", "transition": "fast"},
                {"condition": "context.mode == 'slow'", "transition": "slow"},
            ],
        }
        output = StepOutput()
        result = resolve_rule(resolver_def, output, {"mode": "fast"}, {})
        assert result == "fast"

    def test_null_comparison(self):
        resolver_def = {
            "type": "rule",
            "rules": [
                {
                    "condition": "result.obvious_next_task != null",
                    "transition": "dispatch",
                },
                {"condition": "true", "transition": "prioritize"},
            ],
        }
        output = StepOutput(result={"obvious_next_task": None})
        result = resolve_rule(resolver_def, output, {}, {})
        # None == null is True, so != null is False → falls to catchall
        assert result == "prioritize"

    def test_null_comparison_with_value(self):
        resolver_def = {
            "type": "rule",
            "rules": [
                {
                    "condition": "result.obvious_next_task != null",
                    "transition": "dispatch",
                },
                {"condition": "true", "transition": "prioritize"},
            ],
        }
        output = StepOutput(result={"obvious_next_task": "task_1"})
        result = resolve_rule(resolver_def, output, {}, {})
        assert result == "dispatch"

    def test_len_in_condition(self):
        resolver_def = {
            "type": "rule",
            "rules": [
                {
                    "condition": "context.items and len(context.items) > 0",
                    "transition": "process",
                },
                {"condition": "true", "transition": "empty"},
            ],
        }
        output = StepOutput()
        result = resolve_rule(resolver_def, output, {"items": [1, 2, 3]}, {})
        assert result == "process"

    def test_no_rules_raises(self):
        resolver_def = {"type": "rule", "rules": []}
        output = StepOutput()
        with pytest.raises(RuleResolverError, match="no rules"):
            resolve_rule(resolver_def, output, {}, {})

    def test_no_match_raises(self):
        resolver_def = {
            "type": "rule",
            "rules": [
                {"condition": "result.x == 1", "transition": "a"},
                {"condition": "result.x == 2", "transition": "b"},
            ],
        }
        output = StepOutput(result={"x": 99})
        with pytest.raises(RuleResolverError, match="No rule matched"):
            resolve_rule(resolver_def, output, {}, {})

    def test_invalid_condition_raises(self):
        resolver_def = {
            "type": "rule",
            "rules": [
                {"condition": "import os", "transition": "hack"},
            ],
        }
        output = StepOutput()
        with pytest.raises(RuleResolverError, match="Error evaluating"):
            resolve_rule(resolver_def, output, {}, {})

    def test_missing_result_key_returns_none(self):
        """Missing keys in DotDict return None, not KeyError."""
        resolver_def = {
            "type": "rule",
            "rules": [
                {"condition": "result.missing_key == null", "transition": "yes"},
            ],
        }
        output = StepOutput(result={})
        result = resolve_rule(resolver_def, output, {}, {})
        assert result == "yes"

    def test_str_builtin_in_condition(self):
        """str() is available for type conversion in conditions."""
        resolver_def = {
            "type": "rule",
            "rules": [
                {
                    "condition": "'hello' in str(input.get('msg', '')).lower()",
                    "transition": "found",
                },
                {"condition": "true", "transition": "not_found"},
            ],
        }
        output = StepOutput()
        result = resolve_rule(resolver_def, output, {"msg": "Hello World"}, {})
        assert result == "found"

    def test_str_builtin_with_none_value(self):
        """str() works on None values (the prepare_context use case)."""
        resolver_def = {
            "type": "rule",
            "rules": [
                {
                    "condition": "'error' in str(input.get('history', '')).lower()",
                    "transition": "has_error",
                },
                {"condition": "true", "transition": "no_error"},
            ],
        }
        output = StepOutput()
        result = resolve_rule(resolver_def, output, {}, {})
        assert result == "no_error"

    def test_int_builtin_in_condition(self):
        """int() is available for numeric conversion."""
        resolver_def = {
            "type": "rule",
            "rules": [
                {"condition": "int(input.get('level', '0')) > 3", "transition": "high"},
                {"condition": "true", "transition": "low"},
            ],
        }
        output = StepOutput()
        result = resolve_rule(resolver_def, output, {"level": "5"}, {})
        assert result == "high"

    def test_isinstance_builtin_in_condition(self):
        """isinstance() is available for type checking."""
        resolver_def = {
            "type": "rule",
            "rules": [
                {
                    "condition": "isinstance(input.get('items', []), list)",
                    "transition": "is_list",
                },
                {"condition": "true", "transition": "not_list"},
            ],
        }
        output = StepOutput()
        result = resolve_rule(resolver_def, output, {"items": [1, 2]}, {})
        assert result == "is_list"


# ── DotDict Tests ─────────────────────────────────────────────────────


class TestDotDict:
    def test_attribute_access(self):
        d = _DotDict({"foo": "bar"})
        assert d.foo == "bar"

    def test_nested_dict_becomes_dotdict(self):
        d = _DotDict({"outer": {"inner": "value"}})
        assert d.outer.inner == "value"

    def test_missing_key_returns_none(self):
        d = _DotDict({})
        assert d.nonexistent is None

    def test_dict_access_still_works(self):
        d = _DotDict({"key": "val"})
        assert d["key"] == "val"


# ── Resolver Dispatch Tests ───────────────────────────────────────────


class TestResolverDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_to_rule(self):
        resolver_def = {
            "type": "rule",
            "rules": [{"condition": "true", "transition": "next"}],
        }
        result = await resolve(resolver_def, StepOutput(), {}, {})
        assert result == "next"

    @pytest.mark.asyncio
    async def test_unknown_type_raises(self):
        resolver_def = {"type": "unknown_resolver"}
        with pytest.raises(ResolverError, match="Unknown resolver type"):
            await resolve(resolver_def, StepOutput(), {}, {})
