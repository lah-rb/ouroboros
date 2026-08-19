"""Exact token counts over the wire.

WHY THE SERVER OWNS THIS. Only the server knows how a model tokenizes —
muse counts against a 202,048-token vocabulary, paddle against 103,424 —
and a client sizing work from character counts cannot bridge that. The
tests below care most about the failure directions: a wrong-model count
is worse than no count, and a sizing hint that raises is worse than one
that returns nothing.
"""

from __future__ import annotations

import api.graphql_api as gql


class _Tok:
    def __init__(self, per_char=1, vocab=202048):
        self._per_char = per_char
        self._vocab = vocab

    def tokenize(self, raw, add_bos=False, special=False):
        return [1] * (len(raw) * self._per_char)

    def n_vocab(self):
        return self._vocab


def _patch(monkeypatch, tok=None, resident=None):
    monkeypatch.setattr(
        "inference.tokenizer.get_cached_tokenizer", lambda cfg=None: tok or _Tok()
    )
    if resident is not None:
        monkeypatch.setattr("core.resident_models.get_resident", resident)


def test_counts_each_text_and_totals_them(monkeypatch):
    _patch(monkeypatch)
    out = gql._token_count(["abc", "de"])
    assert out.counts == [3, 2] and out.total == 5
    assert out.n_vocab == 202048 and not out.error


def test_reports_which_tokenizer_answered(monkeypatch):
    """A count from the wrong model is worse than no count — the paddle
    warm-up defect fed muse's BOS through a 103,424-token vocab. n_vocab
    is how a caller can tell."""
    _patch(monkeypatch, tok=_Tok(vocab=103424))
    assert gql._token_count(["x"]).n_vocab == 103424


def test_unknown_resident_model_is_an_error_not_a_wrong_answer(monkeypatch):
    _patch(monkeypatch, resident=lambda name: None)
    out = gql._token_count(["x"], model="nope")
    assert out.error and "nope" in out.error
    assert out.counts == [], "no count at all beats a count from the wrong model"


def test_an_absurd_payload_is_refused_rather_than_tokenized(monkeypatch):
    _patch(monkeypatch)
    out = gql._token_count(["x" * (gql._MAX_TOKENIZE_CHARS + 1)])
    assert "too large" in out.error and out.counts == []


def test_a_broken_tokenizer_returns_an_error_not_an_exception(monkeypatch):
    def boom(cfg=None):
        raise RuntimeError("tokenizer gone")

    monkeypatch.setattr("inference.tokenizer.get_cached_tokenizer", boom)
    out = gql._token_count(["x"])
    assert out.error and out.counts == []


def test_empty_input_is_a_valid_question(monkeypatch):
    _patch(monkeypatch)
    out = gql._token_count([])
    assert out.counts == [] and out.total == 0 and not out.error


def test_schema_exposes_the_query():
    sdl = str(gql.schema)
    assert "type TokenCount" in sdl
    assert 'tokenCount(texts: [String!]!, model: String! = ""): TokenCount!' in sdl
