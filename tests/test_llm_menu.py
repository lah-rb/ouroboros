"""Tests for the LLM menu resolver."""

import pytest

from agent.resolvers.llm_menu import (
    LLMMenuResolverError,
    _build_menu_prompt,
    _build_options_list,
    _parse_choice,
    resolve_llm_menu,
)
from agent.effects.mock import MockEffects

# ── Option Parsing Tests ──────────────────────────────────────────────


class TestParseChoice:
    """Tests for _parse_choice()."""

    def test_exact_match(self):
        assert (
            _parse_choice("execute_change", {"execute_change", "abandon"})
            == "execute_change"
        )

    def test_case_insensitive(self):
        assert (
            _parse_choice("Execute_Change", {"execute_change", "abandon"})
            == "execute_change"
        )

    def test_first_word_match(self):
        assert (
            _parse_choice(
                "execute_change — proceed with confidence",
                {"execute_change", "abandon"},
            )
            == "execute_change"
        )

    def test_substring_match(self):
        assert (
            _parse_choice(
                "I think we should execute_change the file",
                {"execute_change", "abandon"},
            )
            == "execute_change"
        )

    def test_no_match_returns_none(self):
        assert (
            _parse_choice(
                "something completely different", {"execute_change", "abandon"}
            )
            is None
        )

    def test_empty_text(self):
        assert _parse_choice("", {"execute_change", "abandon"}) is None

    def test_first_word_with_punctuation(self):
        assert _parse_choice("abandon.", {"execute_change", "abandon"}) == "abandon"


# ── Options Building Tests ────────────────────────────────────────────


class TestBuildOptionsList:
    """Tests for _build_options_list()."""

    def test_static_options_dict(self):
        resolver_def = {
            "options": {
                "fix": {"description": "Fix the issue"},
                "skip": {"description": "Skip it"},
            }
        }
        result = _build_options_list(resolver_def, {})
        assert result == {"fix": "Fix the issue", "skip": "Skip it"}

    def test_static_options_string(self):
        resolver_def = {
            "options": {
                "fix": "Fix it",
                "skip": "Skip it",
            }
        }
        result = _build_options_list(resolver_def, {})
        assert result == {"fix": "Fix it", "skip": "Skip it"}

    def test_no_options_raises(self):
        with pytest.raises(LLMMenuResolverError, match="no options"):
            _build_options_list({}, {})

    def test_dynamic_options_from_context(self):
        resolver_def = {"options_from": "context.ready_tasks"}
        context = {
            "ready_tasks": [
                {"id": "task_1", "description": "Fix bug"},
                {"id": "task_2", "description": "Add feature"},
            ]
        }
        result = _build_options_list(resolver_def, context)
        assert result == {"task_1": "Fix bug", "task_2": "Add feature"}

    def test_dynamic_options_from_string_list(self):
        resolver_def = {"options_from": "context.choices"}
        context = {"choices": ["alpha", "beta", "gamma"]}
        result = _build_options_list(resolver_def, context)
        assert result == {"alpha": "alpha", "beta": "beta", "gamma": "gamma"}

    def test_dynamic_options_missing_path_raises(self):
        resolver_def = {"options_from": "context.nonexistent"}
        with pytest.raises(LLMMenuResolverError, match="resolved to None"):
            _build_options_list(resolver_def, {})


# ── Menu Prompt Building ──────────────────────────────────────────────


class TestBuildMenuPrompt:
    """Tests for _build_menu_prompt()."""

    def test_includes_options(self):
        prompt = _build_menu_prompt(
            "What should we do?",
            {"fix": "Fix the bug", "skip": "Skip it"},
        )
        assert "fix: Fix the bug" in prompt
        assert "skip: Skip it" in prompt
        assert "What should we do?" in prompt
        assert "ONLY the option name" in prompt

    def test_no_resolver_prompt(self):
        prompt = _build_menu_prompt(None, {"a": "Option A"})
        assert "a: Option A" in prompt

    def test_include_step_output_text(self):
        prompt = _build_menu_prompt(
            "Which option?",
            {"opt_a": "Do A", "opt_b": "Do B"},
            step_output_text="The task completed successfully with 3 files created.",
        )
        assert "Here is what just happened:" in prompt
        assert "The task completed successfully" in prompt
        assert "Which option?" in prompt
        assert "opt_a" in prompt

    def test_step_output_text_none_excluded(self):
        prompt = _build_menu_prompt(
            "Which option?",
            {"opt_a": "Do A"},
            step_output_text=None,
        )
        assert "Here is what just happened:" not in prompt
        assert "Which option?" in prompt


# ── Full LLM Menu Resolver Tests ─────────────────────────────────────


class TestResolveLLMMenu:
    """Tests for resolve_llm_menu()."""

    @pytest.mark.asyncio
    async def test_first_attempt_success(self):
        """Model picks a valid option on first try."""
        effects = MockEffects(inference_responses=["fix"])
        resolver_def = {
            "type": "llm_menu",
            "prompt": "What next?",
            "options": {
                "fix": {"description": "Fix the issue"},
                "skip": {"description": "Skip it"},
            },
        }
        result = await resolve_llm_menu(resolver_def, {}, {}, {}, effects=effects)
        assert result == "fix"

    @pytest.mark.asyncio
    async def test_retry_on_invalid_response(self):
        """Model gives garbage first, valid option on retry."""
        effects = MockEffects(
            inference_responses=["I'm not sure what to do here", "skip"]
        )
        resolver_def = {
            "type": "llm_menu",
            "prompt": "Choose",
            "options": {
                "fix": {"description": "Fix"},
                "skip": {"description": "Skip"},
            },
        }
        result = await resolve_llm_menu(resolver_def, {}, {}, {}, effects=effects)
        assert result == "skip"

    @pytest.mark.asyncio
    async def test_fallback_on_double_failure(self):
        """Model fails twice — falls back to first option."""
        effects = MockEffects(inference_responses=["completely wrong", "still wrong"])
        resolver_def = {
            "type": "llm_menu",
            "options": {
                "first_option": {"description": "First"},
                "second_option": {"description": "Second"},
            },
        }
        result = await resolve_llm_menu(resolver_def, {}, {}, {}, effects=effects)
        assert result == "first_option"  # Falls back to first

    @pytest.mark.asyncio
    async def test_no_effects_raises(self):
        with pytest.raises(LLMMenuResolverError, match="requires effects"):
            await resolve_llm_menu(
                {"type": "llm_menu", "options": {"a": "A"}},
                {},
                {},
                {},
                effects=None,
            )

    @pytest.mark.asyncio
    async def test_option_with_explicit_target(self):
        """Options can have explicit target step names."""
        effects = MockEffects(inference_responses=["gather_more"])
        resolver_def = {
            "type": "llm_menu",
            "options": {
                "gather_more": {
                    "description": "Need more context",
                    "target": "gather_context",
                },
                "done": {"description": "All done"},
            },
        }
        result = await resolve_llm_menu(resolver_def, {}, {}, {}, effects=effects)
        assert result == "gather_context"  # Uses explicit target

    @pytest.mark.asyncio
    async def test_dynamic_options_from_context(self):
        """Resolver with options_from reads options from context."""
        effects = MockEffects(inference_responses=["task_2"])
        resolver_def = {
            "type": "llm_menu",
            "options_from": "context.tasks",
        }
        context = {
            "tasks": [
                {"id": "task_1", "description": "First task"},
                {"id": "task_2", "description": "Second task"},
            ]
        }
        result = await resolve_llm_menu(resolver_def, {}, context, {}, effects=effects)
        assert result == "task_2"

    @pytest.mark.asyncio
    async def test_case_insensitive_model_response(self):
        """Model responds with different casing — still matches."""
        effects = MockEffects(inference_responses=["FIX"])
        resolver_def = {
            "type": "llm_menu",
            "options": {
                "fix": {"description": "Fix"},
                "skip": {"description": "Skip"},
            },
        }
        result = await resolve_llm_menu(resolver_def, {}, {}, {}, effects=effects)
        assert result == "fix"
