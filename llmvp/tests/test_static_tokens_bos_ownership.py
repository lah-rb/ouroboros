"""The static-token builder applies BOS ownership: one <bos>, never two (2026-10-01).

The renderer emits a spec-declared BOS as its first framing segment; the
builder then tokenized with add_bos from the GGUF alone, so Google's
gemma-4-12b QAT (spec <bos>, GGUF add_bos=true) served every request on
[2, 2, 105, ...]. core/lifecycle.py's bare path had the rule since 2026-08-03.
"""

from __future__ import annotations

import os

import pytest

from preprocessing.builder import tokenizer_adds_bos


@pytest.mark.parametrize(
    "add_bos, spec_bos, template_bos, expected",
    [
        (True, "<bos>", None, False),  # google gemma QAT: the renderer owns it
        (False, "<bos>", None, False),  # unsloth gemma: the renderer still owns it
        (True, "", None, True),  # no spec BOS: the GGUF governs
        (False, "", None, False),
        (True, "<s>", False, True),  # template_bos off: the renderer emitted none
        (True, "<s>", True, False),
    ],
)
def test_ownership(add_bos, spec_bos, template_bos, expected):
    assert tokenizer_adds_bos(add_bos, spec_bos, template_bos) is expected


GEMMA = "/home/lah-rb/models/gemma-4-12b-it-qat/gemma-4-12b-it-qat-q4_0.gguf"


@pytest.mark.skipif(not os.path.exists(GEMMA), reason="gemma-4-12b weights absent")
def test_the_gemma_static_prefix_starts_with_one_bos():
    from core.config import load_named_config
    from preprocessing.builder import build_static_tokens

    ids = build_static_tokens(load_named_config("gemma-4-12b-3060"))
    assert ids[0] == 2 and ids[1] != 2
