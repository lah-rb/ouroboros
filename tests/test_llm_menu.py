"""Tests for the LLM menu resolver."""

import pytest

from agent.resolvers.llm_menu import (
    LLMMenuResolverError,
    _build_menu_prompt,
    _build_options_list,
    resolve_llm_menu,
)
from agent.effects.mock import MockEffects

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


# ── Menu Prompt Building (Letter-Key + GBNF) ─────────────────────────


class TestBuildMenuPrompt:
    """Tests for _build_menu_prompt() — returns (prompt, gbnf, letter_map)."""

    def test_returns_three_tuple(self):
        result = _build_menu_prompt(
            "What should we do?",
            {"fix": "Fix the bug", "skip": "Skip it"},
        )
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_prompt_includes_letter_keys(self):
        prompt, gbnf, letter_map = _build_menu_prompt(
            "What should we do?",
            {"fix": "Fix the bug", "skip": "Skip it"},
        )
        assert "[a]" in prompt
        assert "[b]" in prompt
        assert "Fix the bug" in prompt
        assert "Skip it" in prompt
        assert "What should we do?" in prompt
        assert "ONLY the letter" in prompt

    def test_letter_map_correct(self):
        _, _, letter_map = _build_menu_prompt(
            None,
            {"fix": "Fix the bug", "skip": "Skip it"},
        )
        assert letter_map == {"a": "fix", "b": "skip"}

    def test_gbnf_grammar_two_options(self):
        _, gbnf, _ = _build_menu_prompt(
            None,
            {"fix": "Fix", "skip": "Skip"},
        )
        assert gbnf == 'root ::= "a" | "b"'

    def test_gbnf_grammar_three_options(self):
        _, gbnf, _ = _build_menu_prompt(
            None,
            {"opt_a": "A", "opt_b": "B", "opt_c": "C"},
        )
        assert gbnf == 'root ::= "a" | "b" | "c"'

    def test_gbnf_grammar_four_options(self):
        _, gbnf, _ = _build_menu_prompt(
            None,
            {"w": "W", "x": "X", "y": "Y", "z": "Z"},
        )
        assert gbnf == 'root ::= "a" | "b" | "c" | "d"'

    def test_no_resolver_prompt(self):
        prompt, _, _ = _build_menu_prompt(None, {"a": "Option A"})
        assert "[a]" in prompt
        assert "Option A" in prompt

    def test_include_step_output_text(self):
        prompt, _, _ = _build_menu_prompt(
            "Which option?",
            {"opt_a": "Do A", "opt_b": "Do B"},
            step_output_text="The task completed successfully with 3 files created.",
        )
        assert "Here is what just happened:" in prompt
        assert "The task completed successfully" in prompt
        assert "Which option?" in prompt
        assert "[a]" in prompt

    def test_step_output_text_none_excluded(self):
        prompt, _, _ = _build_menu_prompt(
            "Which option?",
            {"opt_a": "Do A"},
            step_output_text=None,
        )
        assert "Here is what just happened:" not in prompt
        assert "Which option?" in prompt

    def test_letter_map_preserves_order(self):
        """Letter assignment follows insertion order of options dict."""
        from collections import OrderedDict

        options = OrderedDict(
            [("alpha", "First"), ("beta", "Second"), ("gamma", "Third")]
        )
        _, _, letter_map = _build_menu_prompt(None, options)
        assert letter_map == {"a": "alpha", "b": "beta", "c": "gamma"}


# ── Full LLM Menu Resolver Tests ─────────────────────────────────────


class TestResolveLLMMenu:
    """Tests for resolve_llm_menu() — letter-key + grammar approach."""

    @pytest.mark.asyncio
    async def test_letter_a_selects_first_option(self):
        """Model returns 'a' → selects first option."""
        effects = MockEffects(inference_responses=["a"])
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
    async def test_letter_b_selects_second_option(self):
        """Model returns 'b' → selects second option."""
        effects = MockEffects(inference_responses=["b"])
        resolver_def = {
            "type": "llm_menu",
            "prompt": "What next?",
            "options": {
                "fix": {"description": "Fix the issue"},
                "skip": {"description": "Skip it"},
            },
        }
        result = await resolve_llm_menu(resolver_def, {}, {}, {}, effects=effects)
        assert result == "skip"

    @pytest.mark.asyncio
    async def test_grammar_passed_in_config(self):
        """Grammar string is passed via config to effects.run_inference."""
        effects = MockEffects(inference_responses=["a"])
        resolver_def = {
            "type": "llm_menu",
            "prompt": "Choose",
            "options": {
                "fix": {"description": "Fix"},
                "skip": {"description": "Skip"},
            },
        }
        await resolve_llm_menu(resolver_def, {}, {}, {}, effects=effects)
        # Check that grammar was included in config
        inference_calls = effects.calls_to("run_inference")
        assert len(inference_calls) == 1
        call_config = inference_calls[0].args["config_overrides"]
        assert "grammar" in call_config
        assert '"a"' in call_config["grammar"]
        assert '"b"' in call_config["grammar"]

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_letter(self):
        """Invalid letter falls back to first option (safety net)."""
        effects = MockEffects(inference_responses=["z"])
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
        effects = MockEffects(inference_responses=["a"])
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
        effects = MockEffects(inference_responses=["b"])
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
    async def test_uppercase_letter_normalized(self):
        """Model responds with uppercase letter — still matches."""
        effects = MockEffects(inference_responses=["A"])
        resolver_def = {
            "type": "llm_menu",
            "options": {
                "fix": {"description": "Fix"},
                "skip": {"description": "Skip"},
            },
        }
        result = await resolve_llm_menu(resolver_def, {}, {}, {}, effects=effects)
        assert result == "fix"

    @pytest.mark.asyncio
    async def test_max_tokens_is_5(self):
        """Config uses max_tokens=5 for efficient grammar-constrained generation."""
        effects = MockEffects(inference_responses=["a"])
        resolver_def = {
            "type": "llm_menu",
            "options": {
                "opt_a": {"description": "A"},
                "opt_b": {"description": "B"},
            },
        }
        await resolve_llm_menu(resolver_def, {}, {}, {}, effects=effects)
        inference_calls = effects.calls_to("run_inference")
        call_config = inference_calls[0].args["config_overrides"]
        assert call_config["max_tokens"] == 5

    @pytest.mark.asyncio
    async def test_retry_on_invalid_response_succeeds(self):
        """Model returns junk twice, then a valid letter on third attempt."""
        effects = MockEffects(inference_responses=["b<|", "<|im_end|>", "b"])
        resolver_def = {
            "type": "llm_menu",
            "default_transition": "fallback_step",
            "options": {
                "first": {"description": "First option"},
                "second": {"description": "Second option"},
            },
        }
        result = await resolve_llm_menu(resolver_def, {}, {}, {}, effects=effects)
        assert result == "second"

    @pytest.mark.asyncio
    async def test_retry_exhausted_uses_default(self):
        """Model returns junk all 3 times — falls to default_transition."""
        effects = MockEffects(inference_responses=["b<|", "z<|", "<|im_end|>"])
        resolver_def = {
            "type": "llm_menu",
            "default_transition": "fallback_step",
            "options": {
                "fix": {"description": "Fix"},
                "skip": {"description": "Skip"},
            },
        }
        result = await resolve_llm_menu(resolver_def, {}, {}, {}, effects=effects)
        assert result == "fallback_step"

    @pytest.mark.asyncio
    async def test_retry_first_attempt_succeeds_no_extra_calls(self):
        """Valid response on first attempt — no retries needed."""
        effects = MockEffects(inference_responses=["a", "b"])
        resolver_def = {
            "type": "llm_menu",
            "options": {
                "fix": {"description": "Fix"},
                "skip": {"description": "Skip"},
            },
        }
        result = await resolve_llm_menu(resolver_def, {}, {}, {}, effects=effects)
        assert result == "fix"
        # Should have consumed only 1 response
        calls = effects.calls_to("run_inference")
        assert len(calls) == 1
