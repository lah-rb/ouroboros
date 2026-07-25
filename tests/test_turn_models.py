"""Tests for the turn schema Pydantic models (agent/models.py).

These models mirror flows/shared/turn.cue. Tests here verify the Pydantic
layer accepts what CUE emits and rejects what would escape CUE's
closure rules — specifically the response-shape/response-contract
coupling and the default/no_answer distinctness constraint.

Step C migration, Phase 1. Table-driven (C5, 2026-07-25): 31 functions
became three tables plus two keepers, same 31 reported cases.

Two notes on how the tables assert:

* REJECTION rows pin ``errors()[0]["loc"]`` and ``["type"]``, not the
  rendered pydantic message. The originals asserted only that *some*
  ValidationError was raised, which would have passed if the model rejected
  for a completely unrelated reason (a typo in the payload's ``sections``,
  say) — the loc/type pair pins the contract that was actually meant, and it
  survives pydantic upgrades that reword messages.
* CONSTRUCTION rows compare attributes by dotted path, where a *type* means
  isinstance and ``PRESENT`` means "set, whatever the value". Both forms
  exist in the originals and neither collapses into the other.
"""

from __future__ import annotations

from typing import Any

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

# Sentinel for "this attribute must be set" where the value itself is not the
# point (the originals used `assert x is not None`).
PRESENT = object()


def _turn(shape: str, response: dict, **kw) -> dict:
    """A minimal valid TurnDefinition payload for `shape`."""
    payload = {
        "response_shape": shape,
        "sections": [{"type": "instruction", "literal": "x"}, {"type": "envelope"}],
        "transitions": {"default": "a", "no_answer": "b"},
        "response": response,
    }
    payload.update(kw)
    return payload


def _assert_attrs(obj: Any, expected: dict) -> None:
    """Assert dotted-path attributes: a type means isinstance, PRESENT means
    not-None, anything else means equality."""
    assert expected, "row must assert at least one attribute"
    for path, want in expected.items():
        got = obj
        for part in path.split("."):
            got = getattr(got, part)
        if want is PRESENT:
            assert got is not None, f"{path}: expected a value, got None"
        elif isinstance(want, type):
            assert isinstance(
                got, want
            ), f"{path}: expected {want.__name__}, got {type(got).__name__}"
        else:
            assert got == want, f"{path}: expected {want!r}, got {got!r}"


# ──────────────────────────────────────────────────────────────────────
# Construction — what the models accept and what they default
# ──────────────────────────────────────────────────────────────────────

_CONSTRUCTION = [
    # Ref: CUE emits `$ref` as the JSON key; Pydantic aliases it.
    pytest.param(
        Ref,
        {"$ref": "context.error_description"},
        {"ref": "context.error_description"},
        id="ref_dollar_alias",
    ),
    # Section — one variant per field the schema supports.
    pytest.param(
        Section,
        {"type": "problem", "ref": {"$ref": "input.flow_directive"}},
        {
            "type": "problem",
            "ref": PRESENT,
            "ref.ref": "input.flow_directive",
            "template": None,
            "literal": None,
            "required": False,
        },
        id="section_with_ref",
    ),
    pytest.param(
        Section,
        {"type": "role", "template": "personas/architect"},
        {"template": "personas/architect"},
        id="section_with_template",
    ),
    # Site #10 added dynamic template refs — template may itself be a Ref.
    pytest.param(
        Section,
        {"type": "instruction", "template": {"$ref": "derived.kind_template"}},
        {"template": Ref, "template.ref": "derived.kind_template"},
        id="section_template_as_ref",
    ),
    pytest.param(
        Section,
        {"type": "instruction", "literal": "Which file?"},
        {"literal": "Which file?"},
        id="section_with_literal",
    ),
    # Title override enables per-site header naming (Site #10).
    pytest.param(
        Section,
        {
            "type": "target_entity",
            "ref": {"$ref": "context.current_symbol"},
            "title": "Symbol to rewrite",
        },
        {"title": "Symbol to rewrite"},
        id="section_title_override",
    ),
    pytest.param(
        Section,
        {"type": "evidence", "literal": "x"},
        {"required": False},
        id="section_required_defaults_false",
    ),
    # MenuOption / OptionSource
    pytest.param(
        MenuOption,
        {"key": "shell_command", "description": "Run a shell command"},
        {"key": "shell_command", "arg": None, "terminal": False},
        id="menu_option_simple",
    ),
    pytest.param(
        MenuOption,
        {
            "key": "send_input",
            "description": "Send input",
            "arg": {"name": "text", "description": "The input to send"},
        },
        {"arg": OptionArg, "arg.name": "text"},
        id="menu_option_with_arg",
    ),
    pytest.param(
        MenuOption,
        {"key": "__bail__", "description": "Bail", "terminal": True, "status": "bail"},
        {"terminal": True, "status": "bail"},
        id="menu_option_terminal",
    ),
    pytest.param(
        OptionSource,
        {"source": "projection", "projection": "fix_target_menu"},
        {"source": "projection", "projection": "fix_target_menu"},
        id="option_source_projection",
    ),
    pytest.param(
        OptionSource,
        {"source": "context", "context_key": "symbol_menu_options"},
        {"context_key": "symbol_menu_options"},
        id="option_source_context_key",
    ),
    # Transitions
    pytest.param(
        TurnTransitions,
        {"default": "step_a", "no_answer": "step_b"},
        {"default": "step_a", "no_answer": "step_b"},
        id="transitions_distinct",
    ),
    # INCIDENT (kept as its own case id): same-target graceful degradation is
    # LEGITIMATE — Site #17's run_session.evaluate routes both default and
    # no_answer to close_session. The invariant is field PRESENCE (both must
    # be declared), not distinctness; an equality check here would have
    # broken a real flow.
    pytest.param(
        TurnTransitions,
        {"default": "close_session", "no_answer": "close_session"},
        {"default": "close_session", "no_answer": "close_session"},
        id="transitions_same_target_is_legal",
    ),
    # Retries bounds
    pytest.param(
        TurnDefinition, _turn("prose", {}), {"retries": 3}, id="retries_defaults_to_3"
    ),
    pytest.param(
        TurnDefinition,
        _turn("prose", {}, retries=0),
        {"retries": 0},
        id="retries_zero_allowed",
    ),
    # StepDefinition integration — with and without a turn.
    pytest.param(
        StepDefinition,
        {
            "action": "inference",
            "description": "test",
            "turn": _turn(
                "prose", {}, transitions={"default": "next", "no_answer": "bail"}
            ),
        },
        {"turn": PRESENT, "turn.response_shape": "prose"},
        id="step_accepts_turn",
    ),
    pytest.param(
        StepDefinition,
        {
            "action": "inference",
            "description": "old-style",
            "prompt_template": {"template": "some/template"},
        },
        {"turn": None, "prompt_template": PRESENT},
        id="step_without_turn_still_valid",
    ),
]


@pytest.mark.parametrize("model_cls,payload,expected", _CONSTRUCTION)
def test_model_construction(model_cls, payload, expected) -> None:
    _assert_attrs(model_cls.model_validate(payload), expected)


# ──────────────────────────────────────────────────────────────────────
# Rejection — shape/contract mismatch, closure, and bounds
# ──────────────────────────────────────────────────────────────────────

_REJECTIONS = [
    pytest.param(
        Ref,
        {"$ref": "context.x", "extra": "nope"},
        ("extra",),
        "extra_forbidden",
        id="ref_rejects_extra_field",
    ),
    pytest.param(
        TurnDefinition,
        _turn("json_document", {}),
        ("schema_id",),
        "missing",
        id="json_document_requires_schema_id",
    ),
    pytest.param(
        TurnDefinition,
        _turn("code", {}),
        ("language",),
        "missing",
        id="code_requires_language",
    ),
    # schema_id belongs to json_document only — a menu turn must reject it.
    pytest.param(
        TurnDefinition,
        {
            "response_shape": "menu_single",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {"default": "a", "no_answer": "b"},
            "response": {"schema_id": "wrong"},
        },
        ("schema_id",),
        "extra_forbidden",
        id="menu_rejects_schema_id",
    ),
    pytest.param(
        TurnDefinition,
        _turn("prose", {}, retries=10),
        ("retries",),
        "less_than_equal",
        id="retries_rejects_above_bound",
    ),
    # Belt-and-braces: extra=forbid on StepDefinition is preserved.
    pytest.param(
        StepDefinition,
        {"action": "inference", "this_field_does_not_exist": True},
        ("this_field_does_not_exist",),
        "extra_forbidden",
        id="step_rejects_unknown_field",
    ),
]


@pytest.mark.parametrize("model_cls,payload,loc,err_type", _REJECTIONS)
def test_model_rejection(model_cls, payload, loc, err_type) -> None:
    with pytest.raises(ValidationError) as exc:
        model_cls.model_validate(payload)
    errors = exc.value.errors()
    assert any(
        e["loc"] == loc and e["type"] == err_type for e in errors
    ), f"expected loc={loc} type={err_type!r}, got {[(e['loc'], e['type']) for e in errors]}"


# ──────────────────────────────────────────────────────────────────────
# Response-shape dispatch — shape picks the contract class
# ──────────────────────────────────────────────────────────────────────

_DISPATCH = [
    # Site #19 — validation_env_config schema.
    pytest.param(
        _turn("json_document", {"schema_id": "validation_env_config"}),
        JsonDocumentResponseContract,
        {"schema_id": "validation_env_config"},
        id="json_document",
    ),
    # Site #9 — resolve_fix_target with projection-sourced options.
    pytest.param(
        {
            "response_shape": "menu_single",
            "sections": [{"type": "options"}, {"type": "envelope"}],
            "transitions": {"default": "apply_fix_target", "no_answer": "check_phase"},
            "response": {
                "options_from": {
                    "source": "projection",
                    "projection": "fix_target_menu",
                },
                "publish_selection": "selected_fix_target",
            },
        },
        MenuResponseContract,
        {"options_from": PRESENT, "options_from.projection": "fix_target_menu"},
        id="menu_single",
    ),
    # Site #1 — code with a python language tag.
    pytest.param(
        _turn("code", {"language": "python"}),
        CodeResponseContract,
        {"language": "python"},
        id="code_with_language",
    ),
    # Site #12 — multi-language multi-fence uses an EMPTY language tag, which
    # is distinct from the tag being absent (that is a rejection, above).
    pytest.param(
        _turn("code", {"language": ""}),
        CodeResponseContract,
        {"language": ""},
        id="code_empty_language_for_multi_fence",
    ),
    # Site #8 — prose with an empty response contract.
    pytest.param(
        _turn("prose", {}), ProseResponseContract, {}, id="prose_empty_contract"
    ),
]


@pytest.mark.parametrize("payload,contract_cls,expected", _DISPATCH)
def test_response_shape_dispatch(payload, contract_cls, expected) -> None:
    turn = TurnDefinition.model_validate(payload)
    assert isinstance(
        turn.response, contract_cls
    ), f"expected {contract_cls.__name__}, got {type(turn.response).__name__}"
    if expected:
        _assert_attrs(turn.response, expected)


# ──────────────────────────────────────────────────────────────────────
# Keepers — assertion shapes a row cannot carry
# ──────────────────────────────────────────────────────────────────────


def test_menu_compound_turn_with_per_option_args() -> None:
    """Site #18 — three options, each with a DISTINCT arg name.

    Not a table row: the claim is about a mapping of three sibling options
    whose arg names must not collide, which is a statement about the whole
    dict rather than one attribute path.
    """
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
    names = {k: o.arg.name for k, o in turn.response.options.items()}
    assert names == {
        "shell_command": "command",
        "send_input": "text",
        "close": "reason",
    }


def test_transitions_with_options_map() -> None:
    """Per-option transition overrides live in a dict keyed by option key."""
    t = TurnTransitions.model_validate(
        {
            "default": "loop",
            "no_answer": "close",
            "options": {"__conclude__": "end_session", "__bail__": "capture_bail"},
        }
    )
    assert t.options is not None
    assert t.options == {"__conclude__": "end_session", "__bail__": "capture_bail"}
