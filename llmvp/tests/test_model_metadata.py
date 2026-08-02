"""GGUF metadata reading — where `add_bos` comes from.

`read_metadata` derives BOS/EOS behavior from GGUF header values that are
STRINGS ("true"/"false"), defaulting to "false" when absent, and infers
`has_thinking` by substring-matching the chat template. It had zero tests
while reporting 34% coverage — almost all of which is the frozen dataclass
declaration executing at import (real function coverage ~10%).

Why it matters: `add_bos` decides whether the template owns the BOS token or
the tokenizer prepends it. Get it wrong and EVERY prompt for the process
lifetime carries a missing or doubled BOS. That is not a crash — it is a model
that quietly answers worse, which is the expensive kind of wrong. The
Mistral/tekken family already produced character salad from exactly this class
of framing error (see formats/schema.py's TokenSpec.bos note).

The reader needs no model: metadata lives in the GGUF header, and the
instance is duck-typed (`.metadata`, `.token_bos()`, `.token_eos()`,
`._model.token_get_text()`).
"""

from __future__ import annotations

import pytest

from inference.metadata import read_metadata


class _FakeModel:
    def __init__(self, texts=None, raises=False):
        self._texts = texts or {}
        self._raises = raises

    def token_get_text(self, tid):
        if self._raises:
            raise RuntimeError("token text unavailable in this build")
        return self._texts.get(tid, f"<{tid}>")


class _FakeLlama:
    """Duck-typed stand-in for llama_cpp.Llama (full or vocab_only)."""

    def __init__(self, metadata=None, bos=1, eos=2, texts=None, text_raises=False):
        self.metadata = metadata or {}
        self._bos = bos
        self._eos = eos
        self._model = _FakeModel(texts, raises=text_raises)

    def token_bos(self):
        return self._bos

    def token_eos(self):
        return self._eos


# ── add_bos / add_eos: string parsing with a "false" default ──────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        pytest.param("true", True, id="lowercase_true"),
        pytest.param("True", True, id="capitalized_true"),
        pytest.param("TRUE", True, id="uppercase_true"),
        pytest.param("false", False, id="lowercase_false"),
        pytest.param("False", False, id="capitalized_false"),
        # Anything that is not "true" reads as False — including junk. The
        # default direction matters: a spurious True would DOUBLE the BOS on
        # every prompt, so unknown input must fall to False.
        pytest.param("yes", False, id="yes_is_not_true"),
        pytest.param("1", False, id="one_is_not_true"),
        pytest.param("", False, id="empty_string"),
    ],
)
def test_add_bos_parses_header_strings(raw, expected):
    md = read_metadata(_FakeLlama(metadata={"tokenizer.ggml.add_bos_token": raw}))
    assert md.add_bos is expected


def test_add_bos_and_add_eos_default_to_false_when_absent():
    """An older GGUF with no BOS keys must not be read as 'prepend BOS'."""
    md = read_metadata(_FakeLlama(metadata={}))
    assert md.add_bos is False
    assert md.add_eos is False


def test_add_eos_is_read_independently_of_add_bos():
    md = read_metadata(
        _FakeLlama(
            metadata={
                "tokenizer.ggml.add_bos_token": "false",
                "tokenizer.ggml.add_eos_token": "true",
            }
        )
    )
    assert md.add_bos is False
    assert md.add_eos is True


# ── token ids and text ────────────────────────────────────────────────────


def test_token_text_is_resolved_from_ids():
    md = read_metadata(
        _FakeLlama(bos=11, eos=22, texts={11: "<s>", 22: "</s>"}),
    )
    assert (md.bos_token_id, md.bos_token_text) == (11, "<s>")
    assert (md.eos_token_id, md.eos_token_text) == (22, "</s>")


def test_absent_tokens_are_not_looked_up():
    """-1 means the model has no such token; asking for its text would be an
    out-of-range call into the C API."""
    md = read_metadata(_FakeLlama(bos=-1, eos=-1))
    assert md.bos_token_text == ""
    assert md.eos_token_text == ""


def test_unresolvable_token_text_degrades_instead_of_raising():
    """token_get_text is an internal API; a build that lacks it must not take
    the whole metadata read down with it."""
    md = read_metadata(_FakeLlama(bos=11, eos=22, text_raises=True))
    assert md.bos_token_id == 11  # ids still recorded
    assert md.bos_token_text == ""
    assert md.eos_token_text == ""


# ── thinking inference ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "template,expected",
    [
        pytest.param("{% if x %}<think>{% endif %}", True, id="think_tag"),
        pytest.param("prefix [THINK] suffix", True, id="bracket_think_uppercase"),
        # Gemma-4's <|think|> defeats a bare "<think>" substring — the miss
        # produced a false "GGUF lacks thinking tags" warning on a
        # thinking-capable 26B-A4B (2026-08-03).
        pytest.param("{{- '<|think|>\\n' -}}", True, id="gemma_pipe_think"),
        pytest.param("{%- if enable_thinking -%}x{%- endif -%}", True, id="enable_thinking_var"),
        pytest.param("{{ messages }}", False, id="no_think_marker"),
        pytest.param("", None, id="empty_template_is_unknown"),
        pytest.param(None, None, id="absent_template_is_unknown"),
    ],
)
def test_has_thinking_inferred_from_chat_template(template, expected):
    """Note the three-way result: None means UNKNOWN (no template to judge),
    which is distinct from False (a template that shows no thinking). Collapsing
    them would let a template-less GGUF claim it definitely cannot think."""
    meta = {} if template is None else {"tokenizer.chat_template": template}
    md = read_metadata(_FakeLlama(metadata=meta))
    assert md.has_thinking is expected


def test_name_and_architecture_fall_back_to_unknown():
    md = read_metadata(_FakeLlama(metadata={}))
    assert md.name == "unknown"
    assert md.architecture == "unknown"
