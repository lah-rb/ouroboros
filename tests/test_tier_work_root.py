"""Tier arms do not work in /tmp.

2026-09-22: `TIER_WORK_ROOT` was `/tmp/tier`, the one location the
never-host-missions-in-/tmp rule exists to prevent. macOS tmp_cleaner runs at
Hour 0 and deletes regular files judged stale by access time — precisely a
walk file written early and not touched again. It has already gutted a live
campaign's .venv and two test files (82 -> 66 goals). A tier arm run with no
wall routinely passes midnight, so the exposure was no longer theoretical.

The resolver is deliberately asymmetric: a CREATE always lands in the durable
root, because the run path `rm -rf`s what it resolves and must never be able
to aim that at a legacy workspace belonging to a still-running arm. Reads fall
back, so an arm started before the move stays visible to status/extend/resume.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.tier.runner as runner  # noqa: E402


def test_root_is_not_under_tmp():
    assert not str(runner.TIER_WORK_ROOT).startswith("/tmp")
    assert "ouroboros-runs" in str(runner.TIER_WORK_ROOT)


def test_create_always_uses_the_durable_root(monkeypatch, tmp_path):
    """Even when a legacy workspace exists — the caller rm -rf's this path."""
    new, legacy = tmp_path / "new", tmp_path / "legacy"
    (legacy / "arm" / ".agent").mkdir(parents=True)
    monkeypatch.setattr(runner, "TIER_WORK_ROOT", new)
    monkeypatch.setattr(runner, "_LEGACY_TIER_WORK_ROOT", legacy)
    assert runner.tier_work_dir("arm", for_create=True) == new / "arm"


def test_read_finds_an_in_flight_legacy_workspace(monkeypatch, tmp_path):
    new, legacy = tmp_path / "new", tmp_path / "legacy"
    (legacy / "arm" / ".agent").mkdir(parents=True)
    monkeypatch.setattr(runner, "TIER_WORK_ROOT", new)
    monkeypatch.setattr(runner, "_LEGACY_TIER_WORK_ROOT", legacy)
    assert runner.tier_work_dir("arm") == legacy / "arm"


def test_read_prefers_the_new_root_when_both_exist(monkeypatch, tmp_path):
    new, legacy = tmp_path / "new", tmp_path / "legacy"
    (legacy / "arm" / ".agent").mkdir(parents=True)
    (new / "arm" / ".agent").mkdir(parents=True)
    monkeypatch.setattr(runner, "TIER_WORK_ROOT", new)
    monkeypatch.setattr(runner, "_LEGACY_TIER_WORK_ROOT", legacy)
    assert runner.tier_work_dir("arm") == new / "arm"


def test_unknown_arm_resolves_to_the_new_root(monkeypatch, tmp_path):
    new, legacy = tmp_path / "new", tmp_path / "legacy"
    monkeypatch.setattr(runner, "TIER_WORK_ROOT", new)
    monkeypatch.setattr(runner, "_LEGACY_TIER_WORK_ROOT", legacy)
    assert runner.tier_work_dir("never-run") == new / "never-run"
