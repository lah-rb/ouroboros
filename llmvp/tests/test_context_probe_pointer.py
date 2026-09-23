"""The context probe must restore the pointer it FOUND, not a constant.

2026-09-20: every rung overwrites ``active_config.txt`` to boot the model
under test, and the finally-block put back a hardcoded ``PRODUCTION``
("gpt-oss-120b-a5-swarm-524k") regardless of what had been there. Probing
while the seat was on qwen3-next therefore left the pointer on gpt-oss —
silently, since nothing reads the pointer again until the next boot, which
would then serve a model the operator never chose. That is the failure the
onboarding procedure already warns about from the other direction ("a
verification round was once burned probing gpt-oss while believing it was
muse"); this pins the probe's half of it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.context_probe as cp  # noqa: E402


class _Args:
    """The two attributes Probe.__init__ touches before the pointer read."""

    probe_resolution = 2048
    probe_gens = 3
    probe_no_write = True


def _probe_with_pointer(monkeypatch, tmp_path, pointer: str | None):
    """Build a Probe with ``active_config.txt`` set to ``pointer``.

    LLMVP is a module-level Path, so redirecting it is what isolates the
    test from the real checkout — the probe writes that file for real.
    """
    monkeypatch.setattr(cp, "LLMVP", tmp_path)
    monkeypatch.setattr(cp, "RUNS", tmp_path / "runs")
    if pointer is not None:
        (tmp_path / "active_config.txt").write_text(pointer)
    return cp.Probe(_Args())


def test_restores_the_pointer_it_found(monkeypatch, tmp_path):
    probe = _probe_with_pointer(monkeypatch, tmp_path, "qwen3-next-80b-a3")
    assert probe.prev_pointer == "qwen3-next-80b-a3"
    assert probe.prev_pointer != cp.PRODUCTION


def test_trailing_newline_is_not_part_of_the_name(monkeypatch, tmp_path):
    """The file is operator-edited, so it usually ends in a newline."""
    probe = _probe_with_pointer(monkeypatch, tmp_path, "gemma-4-31b\n")
    assert probe.prev_pointer == "gemma-4-31b"


def test_missing_pointer_file_falls_back_to_production(monkeypatch, tmp_path):
    """Fresh checkout: the shipped default is the only honest restore."""
    probe = _probe_with_pointer(monkeypatch, tmp_path, None)
    assert probe.prev_pointer == cp.PRODUCTION


def test_empty_pointer_file_falls_back_to_production(monkeypatch, tmp_path):
    probe = _probe_with_pointer(monkeypatch, tmp_path, "   \n")
    assert probe.prev_pointer == cp.PRODUCTION
