"""Guard G2 — workspace scan as a bounded, navigable skeleton.

Pins the contract: the scan excludes vendor/cache/build dirs, skips large/binary
files before reading, caps the file count and per-file signature, and the
rendered listing has a total budget — so a dataset/cache tree (the 29M-token
eval-mteb overflow) can't balloon the prompt.
"""

from __future__ import annotations

import pytest

from agent.actions.refinement_actions import (
    _MAX_FILE_SIZE,
    _MAX_SCAN_FILES,
    _SIGNATURE_MAX_CHARS,
    _excluded,
    action_scan_project,
)
from agent.effects.mock import MockEffects
from agent.formatters import _LISTING_MAX_CHARS, format_project_listing
from agent.models import FlowMeta, StepInput


def _si(files, patterns=("*.py", "*.js", "*.json")):
    return StepInput(
        context={},
        params={"root": ".", "include_patterns": list(patterns)},
        meta=FlowMeta(flow_name="x", step_id="y"),
        effects=MockEffects(files=files),
    )


def test_excluded_dirs_and_egg_info():
    assert _excluded("node_modules/lib/x.js")
    assert _excluded("a/__pycache__/x.pyc")
    assert _excluded(".venv/lib/site.py")
    assert _excluded("foo.egg-info/PKG-INFO")
    assert not _excluded("src/main.py")


@pytest.mark.asyncio
async def test_scan_excludes_vendor_and_skips_large_files():
    files = {
        "src/main.py": "def f(): pass",
        "node_modules/dep.js": "x" * 10,
        "data.json": "y" * (_MAX_FILE_SIZE + 100),  # too big — data, not source
    }
    out = await action_scan_project(_si(files))
    m = out.context_updates["project_manifest"]
    assert list(m.keys()) == ["src/main.py"]


@pytest.mark.asyncio
async def test_scan_caps_file_count():
    files = {f"f{i}.py": "x = 1" for i in range(_MAX_SCAN_FILES + 50)}
    out = await action_scan_project(_si(files))
    assert len(out.context_updates["project_manifest"]) == _MAX_SCAN_FILES
    assert out.result["scan_omitted"] == 50


@pytest.mark.asyncio
async def test_scan_byte_caps_minified_signature():
    # A minified file UNDER the size limit but one giant line — the 50-line cap
    # alone wouldn't bound it; the byte-cap must.
    files = {"bundle.js": "var x=" + "9," * 50_000}  # ~100KB, under _MAX_FILE_SIZE
    out = await action_scan_project(_si(files))
    sig = out.context_updates["project_manifest"]["bundle.js"]
    assert len(sig) <= _SIGNATURE_MAX_CHARS + 80


def test_listing_total_budget_and_omit_note():
    big = {f"f{i}.py": "x" * 200 for i in range(1000)}
    out = format_project_listing({"source": big}, {})
    assert len(out) < _LISTING_MAX_CHARS + 500
    assert "more files omitted" in out


def test_listing_small_manifest_unchanged():
    out = format_project_listing({"source": {"main.py": "def f(): ..."}}, {})
    assert "main.py" in out and "omitted" not in out
