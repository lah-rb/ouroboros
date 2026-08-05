"""Tests for the FSM labeller — verifying drop-in parity with CRF on
single-turn outputs AND correct behaviour on multi-turn rambling.

The FSM is an alternative to the CRF approach for delimiter extraction.
Where the CRF learned D/T/C/E labels from training data, the FSM encodes
each model family's grammar explicitly. These tests verify it handles
the cases that matter:

1. Well-formed single-turn output (the CRF's training distribution) —
   FSM should extract the same content the CRF does.
2. Multi-turn rambling (the CRF's failure mode observed in challenge
   a7ff) — FSM should extract only true final-channel content, no
   leaked analysis prose or structural markers.
3. Cross-family (Qwen <think>, Mistral) — FSM should handle both
   inline-tag thinking and no-thinking-marker families.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make llmvp importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.fsm_labeller import (
    fsm_extract_content,
    fsm_extract_phases,
    fsm_decode,
)

# ── Well-formed single-turn (Harmony) ────────────────────────────────


def test_harmony_thinking_then_final():
    raw = (
        "<|channel|>analysis<|message|>Let me think.<|end|>"
        "<|start|>assistant<|channel|>final<|message|>The answer is 4.<|end|>"
    )
    assert fsm_extract_content(raw) == "The answer is 4."


def test_harmony_with_constrain_preamble():
    raw = (
        "<|channel|>analysis<|message|>consider<|end|>"
        "<|start|>assistant<|channel|>final <|constrain|>json<|message|>"
        '{"k": 1}<|end|>'
    )
    assert fsm_extract_content(raw) == '{"k": 1}'


def test_harmony_commentary_channel_is_not_content():
    """Commentary channel is side-chatter, not user-facing content."""
    raw = (
        "<|channel|>analysis<|message|>think<|end|>"
        "<|start|>assistant<|channel|>commentary<|message|>aside<|end|>"
        "<|start|>assistant<|channel|>final<|message|>answer<|end|>"
    )
    assert fsm_extract_content(raw) == "answer"


def test_harmony_phases_split_correctly():
    raw = (
        "<|channel|>analysis<|message|>my reasoning<|end|>"
        "<|start|>assistant<|channel|>final<|message|>my reply<|end|>"
    )
    phases = fsm_extract_phases(raw)
    assert phases["T"] == "my reasoning"
    assert phases["C"] == "my reply"


# ── Multi-turn rambling (the a7ff failure mode) ──────────────────────


def test_rambling_model_keeps_first_final_only():
    """The a7ff pathology (interaction 182): a valid first final, then the
    model kept generating — a second fake assistant turn with more analysis
    and a second final.

    ORIGINAL doctrine (now overturned): extract BOTH finals as "real content
    per the grammar". The astropy-2 runaway capture (20260703T152322) proved
    the post-answer channels are SELF-PLAY — the model hallucinated the next
    observation and degenerated — and concatenating them yields unparseable
    output ('{"choice": "a"}{"choice": "b"}' is not JSON). The single_turn
    seal (default) keeps the first completed non-empty final; the verbatim
    multi-turn labelling survives under single_turn=False.
    """
    raw = (
        "<|channel|>analysis<|message|>consider<|end|>"
        '<|start|>assistant<|channel|>final<|message|>{"choice": "a"}<|end|>'
        "<|start|>assistant<|channel|>analysis<|message|>hmm more<|end|>"
        '<|start|>assistant<|channel|>final<|message|>{"choice": "b"}<|end|>'
    )
    result = fsm_extract_content(raw)
    assert result == '{"choice": "a"}'
    # Concretely: no leaked markers, no self-play
    assert "<|" not in result
    assert "analysis" not in result
    assert "hmm more" not in result
    # Multi-turn transcript labelling (training/analysis path) is unchanged.
    assert (
        fsm_extract_content(raw, single_turn=False) == '{"choice": "a"}{"choice": "b"}'
    )


def test_rambling_post_answer_analysis_is_D_not_T():
    """Post-answer "analysis" is self-play, not thinking — after the seal it
    labels D (discarded), so the thinking side-channel never carries the
    ramble either. Pre-seal analysis still lands in T."""
    raw = (
        "<|channel|>analysis<|message|>first think<|end|>"
        "<|start|>assistant<|channel|>final<|message|>answer one<|end|>"
        "<|start|>assistant<|channel|>analysis<|message|>second think<|end|>"
    )
    phases = fsm_extract_phases(raw)
    assert phases["C"] == "answer one"
    assert "first think" in phases["T"]
    assert "second think" not in phases["T"]  # sealed → D
    # Verbatim labelling keeps the old split for multi-turn transcripts.
    phases_mt = fsm_extract_phases(raw, single_turn=False)
    assert "second think" in phases_mt["T"]


def test_end_resets_phase_even_without_subsequent_channel():
    """An <|end|> without a following channel marker should close the
    current phase. Subsequent content atoms (if any) should be D, not
    bleed as C or T.
    """
    raw = "<|channel|>final<|message|>good<|end|>" "stray text without channel marker"
    # "stray text" has no governing channel — should not be content
    result = fsm_extract_content(raw)
    assert result == "good"


# ── Cross-family coverage ────────────────────────────────────────────


def test_qwen_think_then_content():
    raw = "<think>I will answer.</think>The answer is 42."
    assert fsm_extract_content(raw, family="chatml") == "The answer is 42."


def test_qwen_phases():
    raw = "<think>reasoning here</think>final response"
    phases = fsm_extract_phases(raw, family="chatml")
    assert phases["T"] == "reasoning here"
    assert phases["C"] == "final response"


def test_mistral_family_no_thinking_markers():
    """Mistral-family outputs contain no thinking markers in the
    generated stream. The whole output is content.
    """
    raw = "The response from Mistral."
    assert fsm_extract_content(raw, family="tekken") == "The response from Mistral."


# ── Robustness ───────────────────────────────────────────────────────


def test_empty_input():
    assert fsm_extract_content("") == ""


def test_only_structural_markers_yields_empty_content():
    raw = "<|channel|>final<|message|><|end|>"
    assert fsm_extract_content(raw) == ""


def test_eos_atom_labeled_E():
    labelled = fsm_decode("content")
    # Last atom is the synthetic EOS, should always be E
    assert labelled[-1][1] == "E"


def test_structural_atoms_labeled_D():
    """Every angle-pipe / channel / message atom should be D."""
    raw = "<|channel|>final<|message|>x<|end|>"
    labelled = fsm_decode(raw)
    for text, lbl in labelled:
        if text in ("<|", "|>", "channel", "final", "message", "end"):
            assert lbl == "D", f"{text!r} should be D, got {lbl}"


# ── Content-character preservation (regression for bracket-stripping bug) ──


def test_harmony_preserves_json_brackets_in_content():
    """Regression: Harmony content containing JSON arrays must preserve
    the [ and ] characters. The original FSM treated BRACKET_OPEN/CLOSE
    as structural for all families, which stripped array brackets from
    valid JSON output and broke downstream parsing (diagnosed in the
    architecture-flow break during live testing).
    """
    raw = (
        "<|channel|>final<|message|>"
        '{"items": ["one", "two", "three"], "count": 3}'
        "<|end|>"
    )
    result = fsm_extract_content(raw, family="harmony")
    assert result == '{"items": ["one", "two", "three"], "count": 3}'
    # Quick JSON round-trip sanity check
    import json

    assert json.loads(result) == {"items": ["one", "two", "three"], "count": 3}


def test_harmony_preserves_slashes_in_content():
    """Slashes appear in file paths, URLs, fractions — must survive
    Harmony content extraction.
    """
    raw = (
        "<|channel|>final<|message|>"
        "File at /path/to/src/main.py, URL https://example.com/api."
        "<|end|>"
    )
    result = fsm_extract_content(raw, family="harmony")
    assert "/path/to/src/main.py" in result
    assert "https://example.com/api" in result


def test_harmony_preserves_angle_brackets_in_content():
    """Angle brackets appear in HTML, XML, and generic Python types —
    must survive Harmony content extraction. Harmony uses <|...|> for
    its own markers, which are ANGLE_PIPE_OPEN atoms and are correctly
    structural. Bare < and > are content.
    """
    raw = (
        "<|channel|>final<|message|>"
        'Use <div class="foo">content</div> and List<int>.'
        "<|end|>"
    )
    result = fsm_extract_content(raw, family="harmony")
    assert '<div class="foo">content</div>' in result
    assert "List<int>" in result


def test_mistral_still_strips_inst_markers():
    """Mistral family must continue to strip [INST]/[/INST] — verify
    the family-specific structural set still works where it should.
    """
    # Mistral [/INST] ends the user turn; everything after is content.
    # Using the actual featurizer behavior — [/INST] maps to
    # BRACKET_OPEN + SLASH + MARKER_INST + BRACKET_CLOSE.
    raw = "[INST]user prompt[/INST]assistant response"
    result = fsm_extract_content(raw, family="mistral")
    # The content after [/INST] should be extracted cleanly
    assert "assistant response" in result
    # [INST], [/INST] brackets should not leak
    assert "[INST]" not in result
    assert "[/INST]" not in result


# ── Lowercase identifiers in brackets (regression for 17b pathology) ──
#
# The featurizer flips into marker_context on `[` to handle Mistral's
# [INST] / [/INST] / [END] / [/END] envelopes. Before the fix, ANY
# lowercase word matching _MARKER_WORDS or _CHANNEL_NAMES inside that
# bracket context was reclassified as a structural marker — and the
# FSM dropped it from content.
#
# This silently corrupted any code passing through LLMVP that used
# the names `start`, `end`, `return`, `message`, `call`, `think`,
# `analysis`, `final`, `tool`, `functions` (and others) as identifiers
# inside square brackets — common Python idioms like `tokens[start:]`,
# `arr[end]`, `state[message]`, `data[return_value]`.
#
# The 17b challenge run hit this on `tokens[start:]` in a parser
# helper. The model emitted the correct slice expression on every
# rewrite cycle (11 attempts captured); LLMVP's content extraction
# stripped `start` from each, leaving `tokens[:]` on disk. The fix
# couldn't land because the bug was downstream of the model.
#
# Fix: tighten bracket-marker recognition to case-sensitive uppercase
# Mistral forms only (INST, /INST, END, /END). Lowercase identifiers
# in brackets always pass through as content, regardless of family.


def test_harmony_preserves_lowercase_start_in_slice():
    """Regression: `tokens[start:]` must survive Harmony extraction.

    The 17b run failure mode — model emits the correct slice
    expression, LLMVP's FSM strips `start` because it's between
    brackets, and the agent receives `tokens[:]`.
    """
    raw = (
        "<|channel|>final<|message|>"
        "def _join(start: int) -> str:\n"
        '    return " ".join(tokens[start:])\n'
        "<|end|>"
    )
    result = fsm_extract_content(raw, family="harmony")
    assert "tokens[start:]" in result
    assert "tokens[:]" not in result


def test_harmony_preserves_lowercase_marker_words_in_brackets():
    """All lowercase marker words must pass through as identifiers
    when they appear between square brackets in Harmony content.
    """
    # Build a single content payload covering every marker word that
    # was vulnerable — _MARKER_WORDS keys plus _CHANNEL_NAMES keys.
    # If any of these gets stripped, the assertion fails for that key.
    payload = (
        "a[start] b[end] c[return] d[message] e[call] "
        "f[think] g[constrain] h[channel] "
        "i[analysis] j[final] k[tool] l[functions] "
        "m[im_start] n[im_end]"
    )
    raw = f"<|channel|>final<|message|>{payload}<|end|>"
    result = fsm_extract_content(raw, family="harmony")
    for token in (
        "[start]",
        "[end]",
        "[return]",
        "[message]",
        "[call]",
        "[think]",
        "[constrain]",
        "[channel]",
        "[analysis]",
        "[final]",
        "[tool]",
        "[functions]",
        "[im_start]",
        "[im_end]",
    ):
        assert token in result, f"{token} stripped from content"


def test_harmony_preserves_slice_with_end_index():
    """Slices using `end` as an index name must survive."""
    raw = (
        "<|channel|>final<|message|>"
        "result = items[end]\n"
        "subset = items[5:end]\n"
        "<|end|>"
    )
    result = fsm_extract_content(raw, family="harmony")
    assert "items[end]" in result
    assert "items[5:end]" in result


def test_harmony_preserves_dict_lookup_with_marker_word_key():
    """Common dict access patterns where the key is a string literal
    matching a marker word must not collapse the brackets."""
    raw = (
        "<|channel|>final<|message|>"
        'state[message] = "hello"\n'
        'config[channel] = "main"\n'
        "<|end|>"
    )
    result = fsm_extract_content(raw, family="harmony")
    assert "state[message]" in result
    assert "config[channel]" in result


def test_chatml_preserves_lowercase_marker_words_in_brackets():
    """Same protection for ChatML family — Qwen-style models must
    also let bracketed identifiers through."""
    raw = "<|im_start|>assistant\nx = tokens[start:]<|im_end|>"
    result = fsm_extract_content(raw, family="chatml")
    assert "tokens[start:]" in result


def test_mistral_preserves_lowercase_marker_words_inside_assistant_content():
    """Mistral assistant turn (after [/INST]) must preserve bracketed
    lowercase identifiers in code. This is the cross-family check —
    if Mistral output contains `arr[start:]` after [/INST], it must
    survive."""
    raw = "[INST]write code[/INST]def f():\n    return arr[start:]"
    result = fsm_extract_content(raw, family="mistral")
    assert "arr[start:]" in result
    # Mistral envelope still strips correctly
    assert "[INST]" not in result
    assert "[/INST]" not in result


def test_mistral_end_block_still_recognized():
    """Mistral [END] / [/END] uppercase markers must still be
    recognized as structural after the strict-uppercase fix.

    Note: this also surfaces a latent issue — before the strict-
    uppercase fix, [END] was being routed to MARKER_END (the
    Harmony close-message marker) via the case-insensitive dict
    lookup, NOT to MARKER_END_TAG (the Mistral user-turn-end
    marker the FSM actually expects). The strict-uppercase fix
    correctly routes [END] to MARKER_END_TAG.
    """
    raw = "[INST]q[/INST]a[END]"
    result = fsm_extract_content(raw, family="mistral")
    # The 'a' content survives, brackets and END marker are stripped
    assert "a" in result
    assert "[END]" not in result
    assert "[INST]" not in result
    assert "[/INST]" not in result


# ── Bare '<' before a marker word (regression for the B+tree range bug) ──
#
# The featurizer flips into marker_context="angle" on a bare '<' (or '</')
# to handle ChatML's <think>/</think> inline tag. Before the fix, that
# context ran the SAME lookup as the '<|' angle_pipe context — so ANY
# lowercase Harmony marker word (start/end/message/return/channel/call/
# constrain) appearing after a bare '<' was reclassified structural and the
# FSM stripped or truncated from there. The exact analog of the bracket-
# context "17b pathology" above, but triggered by '<' instead of '['.
#
# This silently corrupted generated code using those names in a comparison:
# `x < end`, `if k < start:`, `start <= key < end`. The B+tree mining run hit
# it on a range-method docstring (``Yield pairs with ``start <= key < end```):
# the model emitted valid code on every rewrite cycle (110+ captured), the
# FSM tagged `end` as MARKER_END and truncated each at that word, leaving an
# unterminated docstring on disk. The fix loop could never converge — the
# corruption was downstream of the model.
#
# Fix: the bare-'<' angle context recognizes ONLY the ChatML inline tag
# `think`. Harmony pipe-markers REQUIRE '<|'. Real <think>/</think> and
# <|...|> delimiters are unaffected.


def test_harmony_preserves_lt_before_marker_word():
    """Regression: `start <= key < end` must survive Harmony extraction.

    The B+tree run failure mode — model emits the correct range comparison,
    LLMVP's FSM tags `end` after the bare `<` as MARKER_END and truncates the
    rest, leaving an unterminated docstring on disk.
    """
    raw = (
        "<|channel|>final<|message|>"
        "def range(self, start, end):\n"
        '    """Yield (key, value) pairs with ``start <= key < end``."""\n'
        "    return\n"
        "<|end|>"
    )
    result = fsm_extract_content(raw, family="harmony")
    assert "start <= key < end" in result
    assert result.rstrip().endswith("return")  # not truncated at the '<'


def test_harmony_preserves_lt_comparison_mid_strip():
    """`if k < start:` must survive — the mid-line strip flavor where the
    marker word (start) is eaten but the line continues, yielding invalid
    `if k < :`."""
    raw = "<|channel|>final<|message|>if k < start:\n    pass\n<|end|>"
    result = fsm_extract_content(raw, family="harmony")
    assert "if k < start:" in result
    assert "if k < :" not in result


def test_harmony_preserves_all_marker_words_after_bare_lt():
    """Every Harmony pipe-marker word used as an identifier in a `<`
    comparison passes through as content (angle-context analog of the
    bracket-context regression above)."""
    payload = (
        "a < start and b < end and c < message and "
        "d < return and e < call and f < channel and g < constrain"
    )
    raw = f"<|channel|>final<|message|>{payload}<|end|>"
    assert fsm_extract_content(raw, family="harmony") == payload


def test_harmony_still_parses_think_and_pipe_delimiters():
    """The fix must not over-correct: the real ChatML <think>/</think> (bare
    '<') and Harmony <|...|> delimiters must still be recognized."""
    assert (
        fsm_extract_content("<think>reasoning</think>answer", family="chatml")
        == "answer"
    )
    raw = (
        "<|channel|>analysis<|message|>hidden<|end|>"
        "<|start|>assistant<|channel|>final<|message|>shown<|end|>"
    )
    assert fsm_extract_content(raw, family="harmony") == "shown"


# ── ChatML mode detection (regression for 440-run pathology) ──────


def test_chatml_non_thinking_preserves_content():
    """ChatML without any <think>/</think> markers (e.g. Qwen3 non-
    thinking, vanilla Qwen2.5, Llama-3 Instruct) must treat the
    entire stream as content.

    Regression: the 440 run used Qwen3 non-thinking and every
    response came back empty because the FSM started in DELIM and
    stayed there forever (the stream never contained a </think> to
    flip it out).
    """
    raw = "The capital of France is Paris.<|im_end|>"
    result = fsm_extract_content(raw, family="chatml")
    assert result == "The capital of France is Paris."


def test_chatml_non_thinking_preserves_multiline_content():
    """Multi-line non-thinking responses — lists, code, markdown —
    must flow through untouched except for the closing <|im_end|>."""
    raw = (
        "The three primary colors are:\n\n"
        "1. Red\n"
        "2. Blue\n"
        "3. Yellow\n\n"
        "These form the traditional color model."
        "<|im_end|>"
    )
    result = fsm_extract_content(raw, family="chatml")
    assert "The three primary colors are:" in result
    assert "1. Red" in result
    assert "2. Blue" in result
    assert "3. Yellow" in result
    assert "These form the traditional color model." in result
    assert "<|im_end|>" not in result


def test_chatml_prefilled_thinking_splits_at_close_tag():
    """ChatML with prefilled <think> (the chat template primes the
    opening tag into the prompt; only </think> appears in the output
    stream) — everything before </think> is thinking, everything
    after is content.

    This is the qwen3.5 family pattern observed in the knowledge
    fixtures: raw output starts mid-reasoning and closes with a
    bare </think> before the answer.
    """
    raw = (
        "Okay, the user is asking what 2+2 is. "
        "That's basic arithmetic.\n"
        "</think>\n\n"
        "2 + 2 equals **4**."
        "<|im_end|>"
    )
    phases = fsm_extract_phases(raw, family="chatml")
    # Thinking content before </think>
    assert "Okay, the user is asking" in phases["T"]
    assert "basic arithmetic" in phases["T"]
    # Final answer after </think>
    assert "2 + 2 equals **4**." in phases["C"]
    # </think> itself stripped from both
    assert "</think>" not in phases["T"]
    assert "</think>" not in phases["C"]


def test_chatml_explicit_thinking_brackets_are_stripped():
    """ChatML with the model emitting both <think> and </think> in
    its own output — the DeepSeek-R1 style. Reasoning between tags
    goes to T; content after goes to C."""
    raw = (
        "<think>Let me reason through this. The answer is obvious.</think>\n"
        "The final answer is 42."
        "<|im_end|>"
    )
    phases = fsm_extract_phases(raw, family="chatml")
    assert "Let me reason through this." in phases["T"]
    assert "The answer is obvious." in phases["T"]
    assert "The final answer is 42." in phases["C"]
    # Markers themselves must be stripped
    assert "<think>" not in phases["T"] + phases["C"]
    assert "</think>" not in phases["T"] + phases["C"]


def test_chatml_non_thinking_preserves_html_in_content():
    """A common non-thinking output contains HTML/XML. The angle
    brackets around tags must NOT cause phase transitions (they're
    content). Also tests our family-specific structural set works
    correctly — ChatML has ANGLE_OPEN/ANGLE_CLOSE in its structural
    set, but only for recognizing <think>/</think>, not arbitrary
    angle brackets outside a thinking context.
    """
    raw = (
        "Here is some HTML:\n"
        "<div class='foo'>Hello</div>\n"
        "<p>World</p>"
        "<|im_end|>"
    )
    result = fsm_extract_content(raw, family="chatml")
    # The tags themselves get stripped (angle-brackets in ChatML
    # structural set strip them as delimiters), but the text
    # between and around tags must survive.
    assert "Here is some HTML:" in result
    assert "Hello" in result
    assert "World" in result


# ── Tekken / Mistral thinking-mode tolerance ────────────────────────


def test_tekken_explicit_bracket_thinking():
    """Tekken/Mistral with inline [THINK]...[/THINK] — reasoning
    between the tags must be stripped, content after kept.

    This is the Magistral-style pattern: the model emits both the
    opener and closer in its own output stream.
    """
    raw = "[THINK]Let me reason about this carefully.[/THINK]The answer is 42."
    result = fsm_extract_content(raw, family="tekken")
    assert "The answer is 42." in result
    assert "Let me reason" not in result
    assert "[THINK]" not in result
    assert "[/THINK]" not in result


def test_mistral_prefilled_bracket_thinking():
    """Mistral with prefilled [THINK] — only [/THINK] appears in the
    output stream (opener was primed by the chat template). Prose
    before [/THINK] is thinking, after is content.
    """
    raw = "Analyzing the problem step by step.[/THINK]The final answer."
    phases = fsm_extract_phases(raw, family="mistral")
    assert "Analyzing the problem" in phases["T"]
    assert "The final answer." in phases["C"]
    assert "[/THINK]" not in phases["T"] + phases["C"]


def test_tekken_prefilled_bracket_thinking():
    """Same as the Mistral case but for the tekken family name —
    verifies both family strings trigger the same logic.
    """
    raw = "reasoning prose[/THINK]content answer"
    phases = fsm_extract_phases(raw, family="tekken")
    assert "reasoning prose" in phases["T"]
    assert "content answer" in phases["C"]


def test_mistral_pure_content_unchanged():
    """The historical Mistral Instruct default — no markers at all —
    must continue to pass through as pure content. Regression guard
    to ensure the new helper doesn't break the simple case.
    """
    raw = "A straightforward response with no thinking markers."
    result = fsm_extract_content(raw, family="mistral")
    assert result == "A straightforward response with no thinking markers."


def test_mistral_inst_block_still_strips_user_prose():
    """When the whole [INST]...[/INST]... sequence appears in the
    stream (i.e. we've been handed prompt+response together), the
    user prose between the brackets must be stripped — only the
    response after [/INST] is content. Regression against the
    CONTENT-by-default start policy masking the user prompt as
    assistant output.
    """
    raw = "[INST]ignore this — it is the user's prompt[/INST]this is the response"
    result = fsm_extract_content(raw, family="mistral")
    assert "this is the response" in result
    assert "ignore this" not in result
    assert "user's prompt" not in result


# ── Code-content preservation (regression for 543-run pathology) ────


def test_chatml_preserves_arrow_in_function_signature():
    """Python's ``->`` in function signatures uses a bare ``>`` that
    must NOT be stripped by the ChatML FSM. Regression from the 543
    run where qwen3-next-coder emitted ``def f(x: int) -> Command:``
    and the FSM stripped every ``>`` and ``<`` as structural, turning
    the output into ``def f(x: int) - Command:`` and breaking the
    downstream Python parse.
    """
    raw = "def parse_command(raw: str) -> Command:\n    return Command()<|im_end|>"
    result = fsm_extract_content(raw, family="chatml")
    assert "def parse_command(raw: str) -> Command:" in result
    assert "return Command()" in result
    assert "<|im_end|>" not in result


def test_chatml_preserves_comparison_operators_in_content():
    """Arithmetic comparison characters ``<`` and ``>`` must survive
    in content. Companion to the arrow test — the FSM must not treat
    bare angle brackets as structural outside the ``<think>`` compound.
    """
    raw = "if x > 0 and y < 10: print(x + y)<|im_end|>"
    result = fsm_extract_content(raw, family="chatml")
    assert "if x > 0 and y < 10:" in result
    assert "print(x + y)" in result


def test_mistral_preserves_brackets_and_slashes_in_code():
    """Tekken/Mistral content containing Python list literals, dict
    indexing, and division operators — the ``[``, ``]``, and ``/``
    chars must survive unless they form a recognized ``[INST]`` /
    ``[/INST]`` / ``[THINK]`` / ``[/THINK]`` compound.
    """
    raw = "x = [1, 2, 3]; y = x[0]; z = y / 2"
    result = fsm_extract_content(raw, family="mistral")
    # Every bracket, slash, and value must survive
    assert "[1, 2, 3]" in result
    assert "x[0]" in result
    assert "y / 2" in result


def test_chatml_json_with_angle_brackets_in_values():
    """JSON/YAML content may contain HTML or templating with angle
    brackets. These must be preserved when no ``<think>`` / ``</think>``
    compound is present.
    """
    raw = '{"template": "<div>{{ x }}</div>", "cmp": "a > b"}<|im_end|>'
    result = fsm_extract_content(raw, family="chatml")
    assert "<div>" in result
    assert "</div>" in result
    assert "a > b" in result
    assert "{{ x }}" in result


# ── Fixture-corpus regression: Nemotron-3-super ──────────────────────
#
# Nemotron-3-super uses ChatML with prefilled thinking — identical
# shape to qwen3.5. The chat template primes <think> into the prompt
# (so it lives in the KV cache, not the output stream). The model
# emits reasoning as plain prose, closes with </think>, then emits
# the answer. A 20-entry corpus captured from a live Apr-19 run is
# stored alongside the qwen3.5 / qwennext / gptoss / devstral fixtures.
#
# Failure mode this test guards against: if the ChatML prefilled
# path regresses, Nemotron's reasoning prose will leak into content.
# Historically this produced 14KB of reasoning written to disk
# alongside valid Python, which blew up downstream parsers.


def test_nemotron_corpus_extracts_nonempty_content():
    """Every capture in the Nemotron fixture must yield non-empty
    content under family='chatml'. Regression guard for the Apr-19
    run where Nemotron was misclassified as family='unknown' and
    the pipeline couldn't get past the structural-goal gate.
    """
    import json

    fixture = (
        Path(__file__).parent.parent
        / "knowledge"
        / "crf"
        / "captured_raw_nemotron.json"
    )
    if not fixture.exists():  # pragma: no cover — absent in minimal checkouts
        import pytest

        pytest.skip(f"Nemotron fixture not present at {fixture}")

    with open(fixture) as f:
        entries = json.load(f)

    assert entries, "Nemotron fixture is empty"

    for i, entry in enumerate(entries):
        raw = entry.get("raw", "")
        content = fsm_extract_content(raw, family="chatml")
        # The three session_turn entries that got cut off before
        # </think> (max_tokens=20 mid-reasoning) will have the
        # reasoning in CONTENT phase — that's expected given the
        # FSM can't see a thinking boundary. What we DON'T want
        # is silent empty output on any well-formed completion.
        stop = entry.get("stop", "")
        if stop == "completion" and "</think>" in raw:
            assert content.strip(), (
                f"Entry {i}: Nemotron completion with </think> marker "
                f"produced empty content after FSM extraction"
            )


def test_nemotron_corpus_strips_reasoning_prose():
    """For Nemotron entries that contain the full prefilled-thinking
    pattern (</think> present, ending with <|im_end|>), the extracted
    content must NOT contain the reasoning prose prefix ('We need to',
    'We are to'). This catches the case where </think> detection
    regresses and reasoning leaks as answer.
    """
    import json

    fixture = (
        Path(__file__).parent.parent
        / "knowledge"
        / "crf"
        / "captured_raw_nemotron.json"
    )
    if not fixture.exists():  # pragma: no cover
        import pytest

        pytest.skip(f"Nemotron fixture not present at {fixture}")

    with open(fixture) as f:
        entries = json.load(f)

    well_formed = [
        e
        for e in entries
        if "</think>" in e.get("raw", "") and "<|im_end|>" in e.get("raw", "")
    ]
    assert well_formed, (
        "Nemotron fixture has no well-formed completions "
        "(</think> + <|im_end|>) — regression test has no coverage"
    )

    for i, entry in enumerate(well_formed):
        raw = entry["raw"]
        content = fsm_extract_content(raw, family="chatml")
        # The reasoning phase starts with one of these phrases in
        # every Nemotron capture we have. If the FSM leaks reasoning
        # as content, these will appear at the start of extraction.
        first_200 = content[:200]
        assert not first_200.startswith("We need to"), (
            f"Nemotron entry {i}: reasoning prose leaked into content. "
            f"First 200 chars: {first_200!r}"
        )
        assert not first_200.startswith("We are to"), (
            f"Nemotron entry {i}: reasoning prose leaked into content. "
            f"First 200 chars: {first_200!r}"
        )
        assert not first_200.startswith("We are given"), (
            f"Nemotron entry {i}: reasoning prose leaked into content. "
            f"First 200 chars: {first_200!r}"
        )


# ── single_turn seal: post-answer rambling never pollutes content ──
#
# The a7ff pathology, proven live by the astropy-2 runaway capture
# (logs/runaway_captures/20260703T152322_106343.json): the model emitted a
# perfect final channel, then kept generating — chained fake assistant
# turns, hallucinated the observation it expected next, and degenerated
# into a backtick run the repetition guard aborted. The <|end|> → DELIM
# reset labelled the ramble's channels correctly, but extraction
# CONCATENATED every final channel, so a completed ramble returned
# answer + hallucination. The seal: the first NON-EMPTY content phase to
# close wins; everything after labels D. An EMPTY first final must not
# seal (the e75 lesson — analysis→final reopens the assistant role
# mid-turn, and a truncated final can precede the real one).

_RAMBLE = (
    "<|channel|>analysis<|message|>Now inspect _line_type.<|end|>"
    "<|start|>assistant<|channel|>final<|message|>"
    '{"choice": "trace", "symbol_ref": "astropy/io/ascii/qdp.py:_line_type"}'
    "<|end|>"
    "<|start|>assistant<|channel|>analysis<|message|><|end|>"
    "<|start|>assistant<|channel|>final<|message|>"
    "The observation for _line_type is not provided yet; we need to wait."
    "<|end|>"
)


def test_seal_first_final_wins_over_ramble():
    out = fsm_extract_content(_RAMBLE, family="harmony")
    assert out == (
        '{"choice": "trace", "symbol_ref": "astropy/io/ascii/qdp.py:_line_type"}'
    )
    assert "not provided yet" not in out


def test_seal_real_runaway_capture_extracts_clean_answer():
    """Extraction over the actual captured runaway (fixture-ified tail):
    the hallucinated observation with its RST double-backticks — the text
    that drove the token-26178 run — must not leak into content."""
    ramble = (
        "<|channel|>analysis<|message|>Now inspect _line_type to see command "
        "detection.<|end|><|start|>assistant<|channel|>final<|message|>"
        '{\n  "choice": "trace",\n  "symbol_ref": "astropy/io/ascii/qdp.py:_line_type"\n}'
        "<|end|><|start|>assistant<|channel|>analysis<|message|><|end|>"
        "<|start|>assistant<|channel|>final<|message|>Observation (from your "
        "trace of `astropy/io/ascii/qdp.py:_line_type`):\n\n"
        "def _line_type(line, delimiter=None):\n"
        '    """Determine the type of a line in a QDP file.\n'
        "    * If the line starts with ``!`` it is a comment.\n"
        "    ````````````````````````````````````````````````"
    )
    out = fsm_extract_content(ramble, family="harmony")
    assert out.startswith('{\n  "choice": "trace"')
    assert out.rstrip().endswith("}")
    assert "Observation" not in out and "````" not in out


def test_seal_empty_first_final_does_not_block_real_one():
    # A truncated/empty final followed by the real answer — the seal must
    # not fire on the empty phase (e75: never block the real content).
    raw = (
        "<|channel|>final<|message|><|end|>"
        "<|start|>assistant<|channel|>final<|message|>real answer<|end|>"
    )
    assert fsm_extract_content(raw, family="harmony") == "real answer"


def test_seal_analysis_close_does_not_seal():
    # The 91% pattern: analysis closes with <|end|>, then the final opens.
    raw = (
        "<|channel|>analysis<|message|>thinking here<|end|>"
        "<|start|>assistant<|channel|>final<|message|>answer<|end|>"
    )
    assert fsm_extract_content(raw, family="harmony") == "answer"


def test_seal_off_for_multi_turn_transcripts():
    # single_turn=False labels a transcript verbatim (both finals count) —
    # the training/analysis path over multi-turn text.
    out = fsm_extract_content(_RAMBLE, family="harmony", single_turn=False)
    assert "not provided yet" in out


def test_seal_chatml_im_end():
    raw = "<|im_start|>assistant\nanswer<|im_end|>\nfake ramble after close"
    out = fsm_extract_content(raw, family="chatml")
    assert "answer" in out
    assert "fake ramble" not in out


def test_olmo_family_aliases_chatml_think_handling():
    """OLMo is chatml-framed with inline <think> tags; before the alias, the
    unknown-family default mis-split its output — "think>" residue at the
    head of CONTENT (a SyntaxError as combat.py line 1, 2026-07-23) and no
    thinking captured. OLMo's real shape has NO opening tag (the opener is
    prompt-side), so both shapes are pinned."""
    from core.fsm_labeller import fsm_extract_phases

    # Real OLMo shape: reasoning starts immediately, closes with </think>.
    ph = fsm_extract_phases("We reason.\n</think>\ncontent here", family="olmo")
    assert ph.get("T") == "We reason."
    assert ph.get("C") == "content here"
    assert not (ph.get("C") or "").startswith("think>")
    # Fully-tagged shape.
    ph2 = fsm_extract_phases("<think>\nr\n</think>\nc", family="olmo")
    assert ph2.get("T") == "r" and ph2.get("C") == "c"


# ── family behaviour is DERIVED, not hardcoded (2026-07-26) ───────────────


def test_derived_family_behaviour_matches_the_old_hardcoded_table():
    """EQUIVALENCE GUARD for the derivation refactor.

    Family behaviour used to be two hardcoded lists — a structural-category
    table and a phase-dispatch tuple — that had to be edited together with
    nothing enforcing it. That is how OLMo broke: added to one, missed in the
    other, fell to the unknown-family default, and left a "think>" residue as
    line 1 of a generated file while capturing zero thinking.

    Both are now derived from formats/<family>.yaml. This pins the derivation
    against the EXACT values the old table held, so the refactor cannot have
    silently changed a working family. If a value here needs to change, that
    is a real behaviour change and must be argued for, not edited to green.
    """
    from core.featurizer import ObsCategory
    from core.fsm_labeller import _ALWAYS_STRUCTURAL_CATS, _structural_cats_for

    expected = {
        "harmony": set(),
        "chatml": {ObsCategory.MARKER_THINK},
        "olmo": {ObsCategory.MARKER_THINK},
        "laguna": {ObsCategory.MARKER_THINK},
        "mistral": {
            ObsCategory.MARKER_INST,
            ObsCategory.MARKER_END_TAG,
            ObsCategory.MARKER_THINK,
        },
        "tekken": {
            ObsCategory.MARKER_INST,
            ObsCategory.MARKER_END_TAG,
            ObsCategory.MARKER_THINK,
        },
        "gemma": set(),  # override: template pre-closes the thought block
    }
    for family, extra in expected.items():
        got = _structural_cats_for(family)
        assert got == (_ALWAYS_STRUCTURAL_CATS | extra), (
            f"{family}: derived {got - _ALWAYS_STRUCTURAL_CATS}, " f"table said {extra}"
        )


def test_a_new_chatml_fork_needs_no_fsm_edits():
    """THE POINT of the refactor. laguna was onboarded the same day this
    landed; before it, that meant remembering two separate registration sites.
    A family whose spec declares inline <think> tags must get chatml treatment
    with no entry in this file at all."""
    from core.featurizer import ObsCategory
    from core.fsm_labeller import _ThinkShape, _shape_for, _structural_cats_for

    assert _shape_for("laguna") is _ThinkShape.ANGLE
    assert ObsCategory.MARKER_THINK in _structural_cats_for("laguna")
    # and it is NOT special-cased
    from core.fsm_labeller import _SHAPE_OVERRIDES

    assert "laguna" not in _SHAPE_OVERRIDES


def test_unknown_family_falls_back_to_the_safe_default():
    """An unloadable/unknown family must start in DELIM, not CONTENT — a
    misrouted family that starts in CONTENT silently leaks structural markers
    into extracted content."""
    from core.fsm_labeller import _ThinkShape, _shape_for

    assert _shape_for("no-such-family-xyz") is _ThinkShape.CHANNEL


def test_inst_framing_is_read_from_framing_not_thinking_tags():
    """[INST] markers and [THINK] tags are INDEPENDENT properties that only
    coincide in the mistral family today. Deriving one from the other would
    mislabel a future family that uses bracket thinking without INST framing.
    """
    from core.fsm_labeller import _uses_inst_framing

    assert _uses_inst_framing("tekken") is True
    assert _uses_inst_framing("chatml") is False
    assert _uses_inst_framing("laguna") is False


class TestSuffixedThinkTags:
    """A family whose think tag carries a suffix — hunyuan3's
    `<think:opensource>` — must not leak the suffix into CONTENT.

    THE FIRST LIVE GENERATION ON THAT FAMILY (2026-07-28) returned
    ``:opensource>def reverse_string(s): ...``. The labeller arms its closer
    lookahead for the atom IMMEDIATELY after the marker word; `:opensource` is
    not that closer, so it fell through as content. Written to disk that is a
    SyntaxError on line 1 — which is precisely the OLMo `think>` residue
    incident of 2026-07-23, in a new family.

    The tail is DERIVED from the family spec, so the guarantee that matters is
    twofold: suffixed families get stripped, and unsuffixed families are
    untouched by the machinery that strips them.
    """

    def _content(self, raw: str, family: str) -> str:
        from core.featurizer import featurize
        from core.fsm_labeller import label_atoms

        return "".join(
            t for t, lab in label_atoms(featurize(raw), family=family) if lab == "C"
        )

    def test_the_suffix_never_reaches_content(self):
        got = self._content(
            "</think:opensource>def reverse_string(s):\n    return s[::-1]", "hunyuan3"
        )
        assert got.startswith("def reverse_string"), got[:40]
        assert "opensource" not in got

    def test_the_opening_tag_is_stripped_too(self):
        got = self._content(
            "<think:opensource>weighing it up</think:opensource>ANSWER", "hunyuan3"
        )
        assert got == "ANSWER", repr(got)

    def test_thinking_between_suffixed_tags_is_captured_as_T(self):
        """Not merely discarded — the reasoning has to land in T, or
        thinking_content is 0 on every call (the other half of the OLMo bug)."""
        from core.featurizer import featurize
        from core.fsm_labeller import label_atoms

        out = label_atoms(
            featurize("<think:opensource>deliberating</think:opensource>done"),
            family="hunyuan3",
        )
        assert "deliberating" in "".join(t for t, lab in out if lab == "T")

    def test_an_unsuffixed_family_is_unaffected(self):
        """The regression guard. Deriving a tail must not change chatml,
        laguna, olmo or anything else whose tag is bare."""
        for fam in ("chatml", "laguna", "olmo"):
            got = self._content("</think>def f(x):\n    return x", fam)
            assert got == "def f(x):\n    return x", f"{fam}: {got!r}"

    def test_the_tail_is_read_from_the_spec_not_hardcoded(self):
        from core.fsm_labeller import _tag_tail_for

        assert _tag_tail_for("hunyuan3") == ":opensource"
        assert _tag_tail_for("chatml") == ""
        assert _tag_tail_for("laguna") == ""

    def test_content_that_merely_resembles_the_suffix_survives(self):
        """The tail is matched by TEXT and only right after the marker word, so
        prose containing the same characters elsewhere is still content."""
        got = self._content("</think:opensource>see the :opensource notes", "hunyuan3")
        assert got == "see the :opensource notes", repr(got)
