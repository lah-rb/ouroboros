"""Regression tests for action_push_note — tag handling and content filtering.

Motivating context: ``flows/code_core/file_ops.cue``'s ``report_bail`` step was
extended to include ``{$ref: "input.target_file_path", default: ""}`` in
its tags list so that bail notes become findable by per-file note
projections (``_filter_notes_for_file`` in ``agent/projections.py``).
When a bail runs on a flow that wasn't given a target file, the ``$ref``
resolves to the default empty string — which would otherwise leak into
``note.tags`` as ``""`` and quietly match every substring query.

``action_push_note`` drops empty-string tags at the boundary. These
tests lock that contract in.
"""

from __future__ import annotations

import pytest

from agent.actions.refinement_actions import action_push_note
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput


def _build_step_input(
    effects: MockEffects,
    *,
    context: dict | None = None,
    params: dict | None = None,
) -> StepInput:
    return StepInput(
        context=dict(context or {}),
        params=dict(params or {}),
        meta=FlowMeta(
            flow_name="file_ops",
            step_id="report_bail",
            attempt=1,
        ),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_push_note_drops_empty_string_tags():
    """Empty-string tags (from a $ref with default: '' that resolved to
    nothing) must not be persisted. They would match substring queries
    against note.tags accidentally and pollute the note journal."""
    effects = MockEffects()
    step_input = _build_step_input(
        effects,
        context={"bail_reason": "This file has no relevant business logic"},
        params={
            "content_key": "bail_reason",
            "category": "approach_rejected",
            # Simulates CUE $ref resolving to "" when target_file_path is absent
            "tags": ["bail", "wrong_target", "unchanged", ""],
            "source_flow": "file_ops",
        },
    )

    output = await action_push_note(step_input)

    assert output.result.get("note_saved") is True
    # One push_note call, with the empty tag already stripped
    notes = effects._state.get("notes", [])
    assert len(notes) == 1
    saved_tags = notes[0]["tags"]
    assert "" not in saved_tags
    assert saved_tags == ["bail", "wrong_target", "unchanged"]


@pytest.mark.asyncio
async def test_push_note_preserves_non_empty_tags_including_file_path():
    """The common case: a real target_file_path resolves to a path
    string (e.g. "main.py") — it must flow through as a real tag so
    _filter_notes_for_file can match on it."""
    effects = MockEffects()
    step_input = _build_step_input(
        effects,
        context={"bail_reason": "Editor declined to modify"},
        params={
            "content_key": "bail_reason",
            "category": "approach_rejected",
            "tags": ["bail", "wrong_target", "unchanged", "engine.py"],
            "source_flow": "file_ops",
        },
    )

    output = await action_push_note(step_input)

    assert output.result.get("note_saved") is True
    notes = effects._state.get("notes", [])
    assert notes[0]["tags"] == ["bail", "wrong_target", "unchanged", "engine.py"]


@pytest.mark.asyncio
async def test_push_note_coerces_non_string_tags_to_strings():
    """Tags list may receive non-string values if a $ref resolves
    to a non-string type (number, bool). The filter coerces to str
    so downstream code that treats tags as str[] doesn't crash."""
    effects = MockEffects()
    step_input = _build_step_input(
        effects,
        context={"bail_reason": "reason"},
        params={
            "content_key": "bail_reason",
            "category": "general",
            # Mix of types — 0 and False are falsy and should drop;
            # 42 and "real" should survive.
            "tags": ["real", 0, False, 42, "", None],
            "source_flow": "test",
        },
    )

    output = await action_push_note(step_input)

    assert output.result.get("note_saved") is True
    saved_tags = effects._state["notes"][0]["tags"]
    # Falsy values dropped; truthy values coerced to strings
    assert saved_tags == ["real", "42"]


@pytest.mark.asyncio
async def test_push_note_empty_content_is_skipped():
    """If the content_key resolves to an empty/whitespace string,
    no note is saved and no effect is called. Locks in existing
    behavior so the new tag filter doesn't accidentally alter it."""
    effects = MockEffects()
    step_input = _build_step_input(
        effects,
        context={"bail_reason": "   "},
        params={
            "content_key": "bail_reason",
            "category": "approach_rejected",
            "tags": ["bail", "engine.py"],
            "source_flow": "file_ops",
        },
    )

    output = await action_push_note(step_input)

    assert output.result.get("note_saved") is False
    assert effects.call_count("push_note") == 0
