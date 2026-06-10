"""Regression tests for ``parse_llm_json``.

Coverage focus:
  - Single JSON parsing (sanity)
  - Markdown code fences and stop tokens are handled by json_repair
    directly (no preprocessing pass needed)
  - Multi-JSON / harmony-format multi-final-block input: the first
    dict's trailing-whitespace string content is preserved (the
    071-cycle-14 cascade trigger). json_repair alone strips that
    content; ``_isolate_first_json`` restores it.
"""

from __future__ import annotations

from agent.llm_json import parse_llm_json, _isolate_first_json

# ── Sanity / preprocessing ─────────────────────────────────────────────


def test_plain_json_object() -> None:
    raw = '{"choice": "send_input", "text": "go north\\n"}'
    assert parse_llm_json(raw) == {"choice": "send_input", "text": "go north\n"}


def test_fenced_json_object() -> None:
    raw = '```json\n{"goal_met": true, "summary": "done"}\n```'
    result = parse_llm_json(raw)
    assert result == {"goal_met": True, "summary": "done"}


def test_fenced_json_preserves_trailing_newline_in_value() -> None:
    """Fenced reply must NOT lose a string value's trailing newline.

    The PTY "phantom" root cause: the model fences its menu reply, and
    json_repair eats trailing whitespace from the first object's last string
    value whenever trailing slop follows it — here the closing ``` fence. That
    silently turned ``"look\\n"`` into ``"look"``, so the game's input() sat
    blocked on an unterminated line and the harness reported a hang. The
    multi-JSON recovery only fired when the slop was itself JSON; the fence is
    not, so it slipped through. Recovery now triggers on any trailing content.
    """
    raw = '```json\n{"choice": "send_input", "text": "look\\n"}\n```'
    assert parse_llm_json(raw) == {"choice": "send_input", "text": "look\n"}


def test_fenced_json_with_trailing_prose_preserves_newline() -> None:
    """Same recovery for a fenced object followed by explanation prose."""
    raw = '```json\n{"text": "use key\\n"}\n```\nThat opens the door.'
    assert parse_llm_json(raw) == {"text": "use key\n"}


def test_stop_token_leakage_handled() -> None:
    """Small models leak chat-template stop tokens; json_repair
    handles these directly without our former preprocessing pass."""
    raw = '{"choice": "trace", "symbol_ref": "engine.py:foo"}<|im_end|>'
    result = parse_llm_json(raw)
    assert result == {"choice": "trace", "symbol_ref": "engine.py:foo"}


# ── Multi-JSON / harmony multi-final-block recovery ────────────────────


def test_multi_json_first_dict_unwrapped() -> None:
    """When the model emits multiple JSONs, the first dict wins."""
    raw = (
        '{"choice": "trace", "symbol_ref": "first.py:foo"}'
        '{"choice": "trace", "symbol_ref": "second.py:bar"}'
    )
    result = parse_llm_json(raw)
    assert result == {"choice": "trace", "symbol_ref": "first.py:foo"}


def test_multi_json_preserves_first_dict_trailing_newline() -> None:
    """The 071-cycle-14 cascade trigger.

    The model emitted two ``<|channel|>final`` blocks: the first
    a ``send_input`` with ``"\\n"`` (to trigger room re-description
    after drop), the second a ``close``. json_repair on the full
    text returns a list of two dicts, but it strips the trailing
    newline from the first dict's ``text`` value (treats inter-JSON
    content as separator slop and eats whitespace from the
    preceding string). Without the recovery, ``text`` becomes
    empty, send_interaction's empty-input guard fires, and the
    session terminates via close_failure before the look-after-drop
    step runs.

    With ``_isolate_first_json`` restoring the first JSON's source
    span and re-parsing it standalone, the newline survives and the
    framework executes the model's actual intent for at least the
    first action.
    """
    raw = (
        "<|channel|>analysis<|message|>Now step 4: press Enter to "
        "get room description again.<|end|><|start|>assistant"
        "<|channel|>final <|constrain|>json<|message|>"
        '{"choice": "send_input", "text": "\\n"}'
        "<|end|><|start|>assistant<|channel|>analysis<|message|>"
        "We need to see output.<|end|><|start|>assistant"
        "<|channel|>final <|constrain|>json<|message|>"
        '{"choice": "close", "reason": "Test completed."}'
    )
    result = parse_llm_json(raw)
    assert result == {
        "choice": "send_input",
        "text": "\n",
    }, f"trailing newline must survive multi-JSON parsing; got {result!r}"


def test_multi_json_with_trailing_text_in_string_preserved() -> None:
    """Generalization of the cycle-14 case: any trailing whitespace
    inside the first JSON's last string value should survive when
    a second JSON follows."""
    # "look\n" followed by a close
    raw = (
        '{"choice": "send_input", "text": "look\\n"}'
        '{"choice": "close", "reason": "wrap"}'
    )
    result = parse_llm_json(raw)
    assert result == {
        "choice": "send_input",
        "text": "look\n",
    }, f"non-trailing-only whitespace must also survive; got {result!r}"


# ── Brace scanner unit ────────────────────────────────────────────────


def test_isolate_first_json_returns_first_balanced_span() -> None:
    text = '{"a": 1}{"b": 2}'
    assert _isolate_first_json(text) == '{"a": 1}'


def test_isolate_first_json_handles_string_braces() -> None:
    """Braces inside a quoted string don't fool the balance counter."""
    text = '{"key": "val with } inside"}{"second": 2}'
    assert _isolate_first_json(text) == '{"key": "val with } inside"}'


def test_isolate_first_json_handles_escaped_quote() -> None:
    """Escaped quotes inside a string don't end the string."""
    text = '{"key": "she said \\"hi\\""}{"second": 2}'
    span = _isolate_first_json(text)
    assert span == '{"key": "she said \\"hi\\""}'


def test_isolate_first_json_no_brace_returns_none() -> None:
    assert _isolate_first_json("no brace here") is None


def test_isolate_first_json_unbalanced_returns_none() -> None:
    """Half-open object means we never reach depth 0 — correctly None."""
    assert _isolate_first_json('{"unclosed":') is None


# ── Pydantic validation path still works ──────────────────────────────


def test_pydantic_validation_passes_with_isolated_recovery() -> None:
    """When the multi-JSON recovery path runs, the recovered dict
    still passes through to the optional pydantic validation."""
    from pydantic import BaseModel

    class Action(BaseModel):
        choice: str
        text: str = ""

    raw = '{"choice": "send_input", "text": "\\n"}{"choice": "close"}'
    result = parse_llm_json(raw, model=Action)
    assert result is not None
    assert result.choice == "send_input"
    assert result.text == "\n"
