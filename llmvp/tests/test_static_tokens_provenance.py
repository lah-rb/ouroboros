"""tokens.bin provenance: header write/read, validation, and config keying.

Model-free by construction. What is under test is the trust boundary around
the static token cache: before the header, a bin was trusted by filename
alone, and a head built by one model's tokenizer could be fed to another
model (the 2026-08-15 incident: muse's BOS 200000 — valid in its 202,048
vocab — evaluated into paddle's 103,424 vocab at warm-up, flagging paddle's
context on every boot and getting misread as cache contamination).
"""

from __future__ import annotations

import struct
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from preprocessing.builder import (  # noqa: E402
    TOKENS_BIN_MAGIC,
    TOKENS_BIN_HEADER_LEN,
    cache_is_stale,
    write_token_file,
)
from preprocessing.static_tokens import StaticTokensManager  # noqa: E402


def _cfg(tmp_path: Path, bin_name: str = "t.tokens.bin"):
    tokens_bin = tmp_path / bin_name
    persona = types.SimpleNamespace(persona_file=None, tokens_bin=str(tokens_bin))
    cfg = types.SimpleNamespace(
        resolve_persona=lambda name: persona,
        prompt=types.SimpleNamespace(persona_file=None),
        knowledge=types.SimpleNamespace(tokens_bin=str(tokens_bin)),
    )
    return cfg, tokens_bin


# ── writer ────────────────────────────────────────────────────────────


def test_write_token_file_stamps_header(tmp_path):
    out = tmp_path / "x.tokens.bin"
    write_token_file([1, 2, 300], out, n_vocab=1000)
    raw = out.read_bytes()
    assert raw[: len(TOKENS_BIN_MAGIC)] == TOKENS_BIN_MAGIC
    n_vocab, count = struct.unpack(
        "<II", raw[len(TOKENS_BIN_MAGIC) : TOKENS_BIN_HEADER_LEN]
    )
    assert (n_vocab, count) == (1000, 3)
    assert struct.unpack("<3I", raw[TOKENS_BIN_HEADER_LEN:]) == (1, 2, 300)


# ── loader validation ─────────────────────────────────────────────────


def test_read_valid_header_roundtrip(tmp_path):
    out = tmp_path / "x.tokens.bin"
    write_token_file([5, 6, 7], out, n_vocab=100)
    mm, view, ids = StaticTokensManager._read_token_file(str(out))
    try:
        assert ids == [5, 6, 7]
    finally:
        view.release()
        mm.close()


def test_read_rejects_legacy_headerless(tmp_path):
    out = tmp_path / "x.tokens.bin"
    out.write_bytes(struct.pack("<3I", 1, 2, 3))  # pre-header format
    with pytest.raises(ValueError, match="provenance"):
        StaticTokensManager._read_token_file(str(out))


def test_read_rejects_count_mismatch(tmp_path):
    out = tmp_path / "x.tokens.bin"
    write_token_file([1, 2, 3], out, n_vocab=10)
    with open(out, "ab") as f:  # extra id the header does not admit to
        f.write(struct.pack("<I", 4))
    with pytest.raises(ValueError, match="holds"):
        StaticTokensManager._read_token_file(str(out))


def test_read_rejects_id_outside_stamped_vocab(tmp_path):
    """The incident shape: an id valid for the writer, stamped, then the
    stamp itself catches a corrupt/foreign edit."""
    out = tmp_path / "x.tokens.bin"
    write_token_file([200000], out, n_vocab=103424)
    with pytest.raises(ValueError, match="outside the stamped vocab"):
        StaticTokensManager._read_token_file(str(out))


def test_read_accepts_unknown_vocab_stamp(tmp_path):
    """n_vocab=0 means the writer could not determine it — ids load."""
    out = tmp_path / "x.tokens.bin"
    write_token_file([200000], out, n_vocab=0)
    mm, view, ids = StaticTokensManager._read_token_file(str(out))
    try:
        assert ids == [200000]
    finally:
        view.release()
        mm.close()


# ── staleness ─────────────────────────────────────────────────────────


def test_legacy_headerless_bin_is_stale(tmp_path, monkeypatch):
    """Auto-migration: a pre-header bin rebuilds on first staleness check."""
    import preprocessing.builder as builder

    monkeypatch.setattr(builder, "static_input_paths", lambda c, p: [])
    cfg, tokens_bin = _cfg(tmp_path)
    tokens_bin.write_bytes(struct.pack("<2I", 1, 2))
    assert cache_is_stale(cfg) is True
    write_token_file([1, 2], tokens_bin, n_vocab=10)
    assert cache_is_stale(cfg) is False


# ── loader rebuild-on-mismatch ────────────────────────────────────────


def test_load_rebuilds_foreign_bin_once(tmp_path, monkeypatch):
    """A bin that fails validation is rebuilt via build_and_write and the
    rebuilt file is what loads."""
    import preprocessing.builder as builder

    cfg, tokens_bin = _cfg(tmp_path)
    monkeypatch.setattr(builder, "static_input_paths", lambda c, p: [])
    # Foreign bin: stamped vocab smaller than its own max id.
    write_token_file([200000], tokens_bin, n_vocab=103424)

    rebuilt = {}

    def fake_build(config, *, emit=None, persona="default"):
        rebuilt["hit"] = True
        write_token_file([7, 8], tokens_bin, n_vocab=100)
        return tokens_bin

    monkeypatch.setattr(builder, "build_and_write", fake_build)

    mgr = StaticTokensManager()
    mgr.load_static_buffer("default", config=cfg)
    assert rebuilt.get("hit") is True
    assert mgr.get_static_tokens("default", config=cfg) == [7, 8]
    mgr.cleanup()


# ── config keying (the globalism fix) ─────────────────────────────────


def test_manager_key_uses_given_config(tmp_path):
    cfg_a, bin_a = _cfg(tmp_path, "a.tokens.bin")
    cfg_b, bin_b = _cfg(tmp_path, "b.tokens.bin")
    assert StaticTokensManager._key("default", cfg_a) == str(bin_a)
    assert StaticTokensManager._key("default", cfg_b) == str(bin_b)


def test_tokenizer_cache_keys_non_active_config_separately(monkeypatch):
    """A non-active config must NOT receive the backend-delegating wrapper
    (which tokenizes with the ACTIVE model's vocab)."""
    import inference.tokenizer as tk

    active = types.SimpleNamespace(
        model=types.SimpleNamespace(path="/models/active.gguf")
    )
    secondary = types.SimpleNamespace(
        model=types.SimpleNamespace(path="/models/secondary.gguf")
    )
    monkeypatch.setattr(tk, "get_config", lambda: active)
    made = {}

    def fake_llama(cfg):
        made["path"] = str(cfg.model.path)
        return object()

    monkeypatch.setattr(tk, "_create_llama_tokenizer", fake_llama)
    monkeypatch.setattr(tk, "_is_mlc_model", lambda p: False)
    tk.reset_tokenizer_cache()
    try:
        tok = tk.get_cached_tokenizer(secondary)
        assert made["path"] == "/models/secondary.gguf"
        assert not isinstance(tok, tk._BackendTokenizerWrapper)
    finally:
        tk.reset_tokenizer_cache()
