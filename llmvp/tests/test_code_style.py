"""llmvp's own formatter gate — the sibling of tests/test_code_style.py.

llmvp is a separate package with its own venv and its own suite, so it
polices itself here rather than being swept up by the agent-side check.
37 of its files were unformatted before 2026-08-05.

Its black floor was also raised from >=25.11.0 to >=26.3.1 to match the
agent package. The two trees are edited in the same sessions, and a 25.x
here against a 26.x there would have the two venvs fighting over the same
files.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

LLMVP = Path(__file__).resolve().parent.parent
TARGETS = ["api", "core", "inference", "formats", "tests"]


@pytest.mark.parametrize("target", TARGETS)
def test_target_is_black_clean(target):
    path = LLMVP / target
    if not path.exists():
        pytest.skip(f"{target} not present")
    proc = subprocess.run(
        [sys.executable, "-m", "black", "--check", "--quiet", str(path)],
        cwd=LLMVP,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"llmvp/{target} is not black-formatted. Run:\n"
        f"    cd llmvp && .venv/bin/python3 -m black {target}\n\n"
        f"{proc.stdout}{proc.stderr}"
    )
