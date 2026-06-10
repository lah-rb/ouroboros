"""Tests for the turn schema Pydantic models (agent/models.py).

These models mirror flows/shared/turn.cue. Tests here verify the Pydantic
layer accepts what CUE emits and rejects what would escape CUE's
closure rules — specifically the response-shape/response-contract
coupling and the default/no_answer distinctness constraint.

Step C migration, Phase 1.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.models import (
    CodeResponseContract,
    JsonDocumentResponseContract,
    MenuOption,
    MenuResponseContract,
    OptionArg,
    OptionSource,
    ProseResponseContract,
    Ref,
    Section,
    StepDefinition,
    TurnDefinition,
    TurnTransitions,
)

# ──────────────────────────────────────────────────────────────────────
# Ref and Section
# ──────────────────────────────────────────────────────────────────────


def test_ref_parses_dollar_ref_alias() -> None:
    """CUE emits `$ref` as the JSON key; Pydantic aliases it."""
    r = Ref.model_validate({"$ref": "context.error_description"})
    assert r.ref == "context.error_description"


def test_ref_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Ref.model_validate({"$ref": "context.x", "extra": "nope"})


def test_section_with_ref() -> None:
    s = Section.model_validate(
        {"type": "problem", "ref": {"$ref": "input.flow_directive"}}
    )
    assert s.type == "problem"
    assert s.ref is not None
    assert s.ref.ref == "input.flow_directive"
    assert s.template is None
    assert s.literal is None
    assert s.required is False


def test_section_with_template() -> None:
    s = Section.model_validate({"type": "role", "template": "personas/architect"})
    assert s.template == "personas/architect"


def test_section_with_template_as_ref() -> None:
    """Site #10 added dynamic template refs."""
    s = Section.model_validate(
        {"type": "instruction", "template": {"$ref": "derived.kind_template"}}
    )
    assert isinstance(s.template, Ref)
    assert s.template.ref == "derived.kind_template"


def test_section_with_literal() -> None:
    s = Section.model_validate({"type": "instruction", "literal": "Which file?"})
    assert s.literal == "Which file?"


def test_section_with_title_override() -> None:
    """Title override enables per-site header naming (Site #10)."""
    s = Section.model_validate(
        {
            "type": "target_entity",
            "ref": {"$ref": "context.current_symbol"},
            "title": "Symbol to rewrite",
        }
    )
    assert s.title == "Symbol to rewrite"


def test_section_required_defaults_false() -> None:
    s = Section.model_validate({"type": "evidence", "literal": "x"})
    assert s.required is False


# ──────────────────────────────────────────────────────────────────────
# Menu options and sourcing
# ──────────────────────────────────────────────────────────────────────


def test_menu_option_simple() -> None:
    m = MenuOption.model_validate(
        {"key": "shell_command", "description": "Run a shell command"}
    )
    assert m.key == "shell_command"
    assert m.arg is None
    assert m.terminal is False


def test_menu_option_with_arg() -> None:
    m = MenuOption.model_validate(
        {
            "key": "send_input",
            "description": "Send input",
            "arg": {"name": "text", "description": "The input to send"},
        }
    )
    assert isinstance(m.arg, OptionArg)
    assert m.arg.name == "text"


def test_menu_option_terminal() -> None:
    m = MenuOption.model_validate(
        {
            "key": "__bail__",
            "description": "Bail",
            "terminal": True,
            "status": "bail",
        }
    )
    assert m.terminal is True
    assert m.status == "bail"


def test_option_source_projection() -> None:
    s = OptionSource.model_validate(
        {"source": "projection", "projection": "fix_target_menu"}
    )
    assert s.source == "projection"
    assert s.projection == "fix_target_menu"


def test_option_source_context_key() -> None:
    s = OptionSource.model_validate(
        {"source": "context", "context_key": "symbol_menu_options"}
    )
    assert s.context_key == "symbol_menu_options"


# ──────────────────────────────────────────────────────────────────────
# Response contracts — shape dispatch
# ──────────────────────────────────────────────────────────────────────


def test_json_document_turn_dispatches_to_json_contract() -> None:
    """Site #19 — validation_env_config schema."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "json_document",
            "sections": [
                {"type": "instruction", "literal": "detect"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "persist_env", "no_answer": "failed"},
            "response": {"schema_id": "validation_env_config"},
        }
    )
    assert isinstance(turn.response, JsonDocumentResponseContract)
    assert turn.response.schema_id == "validation_env_config"


def test_menu_single_turn_dispatches_to_menu_contract() -> None:
    """Site #9 — resolve_fix_target with projection-sourced options."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_single",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {
                "default": "apply_fix_target",
                "no_answer": "check_phase",
            },
            "response": {
                "options_from": {
                    "source": "projection",
                    "projection": "fix_target_menu",
                },
                "publish_selection": "selected_fix_target",
            },
        }
    )
    assert isinstance(turn.response, MenuResponseContract)
    assert turn.response.options_from is not None
    assert turn.response.options_from.projection == "fix_target_menu"


def test_menu_compound_turn_with_per_option_args() -> None:
    """Site #18 — three options, each with a distinct arg name."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "menu_compound",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {
                "default": "execute_interaction",
                "no_answer": "close_session",
            },
            "response": {
                "options": {
                    "shell_command": {
                        "key": "shell_command",
                        "description": "Run command",
                        "arg": {"name": "command", "description": "bash command"},
                    },
                    "send_input": {
                        "key": "send_input",
                        "description": "Send input",
                        "arg": {"name": "text", "description": "input text"},
                    },
                    "close": {
                        "key": "close",
                        "description": "End session",
                        "arg": {"name": "reason", "description": "why"},
                    },
                },
                "publish_selection": "planned_action",
            },
        }
    )
    assert isinstance(turn.response, MenuResponseContract)
    assert turn.response.options is not None
    assert turn.response.options["shell_command"].arg is not None
    assert turn.response.options["shell_command"].arg.name == "command"
    assert turn.response.options["send_input"].arg.name == "text"
    assert turn.response.options["close"].arg.name == "reason"


def test_code_turn_dispatches_to_code_contract() -> None:
    """Site #1 — code with python language tag."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "code",
            "sections": [
                {"type": "instruction", "literal": "write"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "write_file", "no_answer": "failed"},
            "response": {"language": "python"},
        }
    )
    assert isinstance(turn.response, CodeResponseContract)
    assert turn.response.language == "python"


def test_code_turn_allows_empty_language_for_multi_fence() -> None:
    """Site #12 — multi-language multi-fence uses empty language tag."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "code",
            "sections": [
                {"type": "instruction", "literal": "write"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "write_files", "no_answer": "report_failure"},
            "response": {"language": ""},
        }
    )
    assert isinstance(turn.response, CodeResponseContract)
    assert turn.response.language == ""


def test_prose_turn_dispatches_to_prose_contract() -> None:
    """Site #8 — prose with empty response contract."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "prose",
            "sections": [
                {"type": "instruction", "literal": "summarize"},
                {"type": "envelope"},
            ],
            "transitions": {"default": "done", "no_answer": "no_results"},
            "response": {},
        }
    )
    assert isinstance(turn.response, ProseResponseContract)


# ──────────────────────────────────────────────────────────────────────
# Shape/contract mismatch rejection
# ──────────────────────────────────────────────────────────────────────


def test_json_document_without_schema_id_rejected() -> None:
    with pytest.raises(ValidationError):
        TurnDefinition.model_validate(
            {
                "response_shape": "json_document",
                "sections": [
                    {"type": "instruction", "literal": "x"},
                    {"type": "envelope"},
                ],
                "transitions": {"default": "a", "no_answer": "b"},
                "response": {},  # schema_id required
            }
        )


def test_code_without_language_rejected() -> None:
    with pytest.raises(ValidationError):
        TurnDefinition.model_validate(
            {
                "response_shape": "code",
                "sections": [
                    {"type": "instruction", "literal": "x"},
                    {"type": "envelope"},
                ],
                "transitions": {"default": "a", "no_answer": "b"},
                "response": {},  # language required (empty string is fine)
            }
        )


def test_menu_turn_with_schema_id_rejected() -> None:
    """schema_id belongs to json_document only — menu turn rejects it."""
    with pytest.raises(ValidationError):
        TurnDefinition.model_validate(
            {
                "response_shape": "menu_single",
                "sections": [{"type": "options"}, {"type": "envelope"}],
                "transitions": {"default": "a", "no_answer": "b"},
                "response": {"schema_id": "wrong"},
            }
        )


# ──────────────────────────────────────────────────────────────────────
# Transitions
# ──────────────────────────────────────────────────────────────────────


def test_transitions_default_and_no_answer_distinct() -> None:
    t = TurnTransitions.model_validate({"default": "step_a", "no_answer": "step_b"})
    assert t.default == "step_a"
    assert t.no_answer == "step_b"


def test_transitions_allow_same_default_and_no_answer() -> None:
    """Same-target graceful-degradation pattern is legitimate —
    e.g. Site #17's run_session.evaluate routes both default and
    no_answer to close_session. The field-presence invariant (both
    must be declared) is what matters."""
    t = TurnTransitions.model_validate(
        {"default": "close_session", "no_answer": "close_session"}
    )
    assert t.default == "close_session"
    assert t.no_answer == "close_session"


def test_transitions_with_options_map() -> None:
    t = TurnTransitions.model_validate(
        {
            "default": "loop",
            "no_answer": "close",
            "options": {
                "__conclude__": "end_session",
                "__bail__": "capture_bail",
            },
        }
    )
    assert t.options is not None
    assert t.options["__conclude__"] == "end_session"


# ──────────────────────────────────────────────────────────────────────
# Retries bounds
# ──────────────────────────────────────────────────────────────────────


def test_retries_defaults_to_3() -> None:
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "prose",
            "sections": [{"type": "instruction", "literal": "x"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {},
        }
    )
    assert turn.retries == 3


def test_retries_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        TurnDefinition.model_validate(
            {
                "response_shape": "prose",
                "sections": [
                    {"type": "instruction", "literal": "x"},
                    {"type": "envelope"},
                ],
                "transitions": {"default": "a", "no_answer": "b"},
                "response": {},
                "retries": 10,  # > 5
            }
        )


def test_retries_zero_allowed() -> None:
    """Retries can be explicitly disabled."""
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "prose",
            "sections": [{"type": "instruction", "literal": "x"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {},
            "retries": 0,
        }
    )
    assert turn.retries == 0


# ──────────────────────────────────────────────────────────────────────
# Integration with StepDefinition
# ──────────────────────────────────────────────────────────────────────


def test_step_definition_accepts_turn() -> None:
    """Verify the turn field integrates into StepDefinition."""
    step = StepDefinition.model_validate(
        {
            "action": "inference",
            "description": "test",
            "turn": {
                "response_shape": "prose",
                "sections": [
                    {"type": "instruction", "literal": "do the thing"},
                    {"type": "envelope"},
                ],
                "transitions": {"default": "next", "no_answer": "bail"},
                "response": {},
            },
        }
    )
    assert step.turn is not None
    assert step.turn.response_shape == "prose"


def test_step_definition_without_turn_still_valid() -> None:
    """Existing non-turn step definitions still work."""
    step = StepDefinition.model_validate(
        {
            "action": "inference",
            "description": "old-style",
            "prompt_template": {"template": "some/template"},
        }
    )
    assert step.turn is None
    assert step.prompt_template is not None


def test_step_definition_rejects_unknown_field() -> None:
    """Belt-and-braces: extra=forbid on StepDefinition is preserved."""
    with pytest.raises(ValidationError):
        StepDefinition.model_validate(
            {
                "action": "inference",
                "this_field_does_not_exist": True,
            }
        )
