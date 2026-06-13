"""Static token cache staleness detection (preprocessing/builder.py).

The per-model token cache is derived from on-disk inputs (SOUL.md, the
knowledge dir) and is rebuilt by the backend when it goes stale, so the
mtime comparison is the contract: edit the persona and the next startup
rebuilds rather than silently serving the previous one.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from preprocessing.builder import cache_is_stale, static_input_paths


def _config(tmp: Path) -> SimpleNamespace:
    return SimpleNamespace(
        prompt=SimpleNamespace(
            persona_file=str(tmp / "knowledge" / "SOUL.md"),
            tools_file=str(tmp / "knowledge" / "tools.txt"),
        ),
        knowledge=SimpleNamespace(
            tokens_bin=str(tmp / "data" / "model.tokens.bin"),
            token_limit=32768,
        ),
        model=SimpleNamespace(family="chatml", thinking_mode=None),
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    # cache_is_stale resolves the knowledge dir relative to cwd (matching
    # the --prep CLI), so run from the temp root.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "knowledge" / "SOUL.md").write_text("persona", encoding="utf-8")
    (tmp_path / "knowledge" / "tools.txt").write_text("tools", encoding="utf-8")
    # Isolate from the repo's real active config (its mtime would otherwise
    # dominate the comparison). The active-config input is covered by its
    # own behavior; here we pin the persona/knowledge contract.
    import core.config

    def _no_active_config():
        raise FileNotFoundError("no active config in test")

    monkeypatch.setattr(core.config, "_default_config_path", _no_active_config)
    return tmp_path, _config(tmp_path)


def _stamp(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"\x00\x00\x00\x00")
    import os

    os.utime(path, (mtime, mtime))


def test_missing_cache_is_stale(world):
    _tmp, config = world
    assert cache_is_stale(config) is True


def test_cache_newer_than_inputs_is_fresh(world):
    tmp, config = world
    for name in ("SOUL.md", "tools.txt"):
        _stamp(tmp / "knowledge" / name, 1000)
    _stamp(tmp / "data" / "model.tokens.bin", 2000)
    assert cache_is_stale(config) is False


def test_edited_persona_makes_cache_stale(world):
    tmp, config = world
    _stamp(tmp / "knowledge" / "tools.txt", 1000)
    _stamp(tmp / "data" / "model.tokens.bin", 2000)
    _stamp(tmp / "knowledge" / "SOUL.md", 3000)  # edited after the build
    assert cache_is_stale(config) is True


def test_edited_knowledge_file_makes_cache_stale(world):
    tmp, config = world
    _stamp(tmp / "knowledge" / "SOUL.md", 1000)
    _stamp(tmp / "data" / "model.tokens.bin", 2000)
    # A brand-new knowledge file (e.g. regenerated tools.txt) lands newer.
    _stamp(tmp / "knowledge" / "extra.md", 3000)
    assert cache_is_stale(config) is True


def test_equal_mtime_is_not_stale(world):
    # Strict '>' so inputs written in the same build as the cache (same
    # second) don't trigger an immediate rebuild.
    tmp, config = world
    for name in ("SOUL.md", "tools.txt"):
        _stamp(tmp / "knowledge" / name, 1500)
    _stamp(tmp / "data" / "model.tokens.bin", 1500)
    assert cache_is_stale(config) is False


def test_static_inputs_include_persona_and_knowledge(world):
    tmp, config = world
    _stamp(tmp / "data" / "model.tokens.bin", 2000)
    inputs = {p.name for p in static_input_paths(config)}
    assert "SOUL.md" in inputs
    assert "tools.txt" in inputs
