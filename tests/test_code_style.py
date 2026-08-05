"""The formatter gate that did not exist.

black has been a declared dependency all along and `black --check` was run
by hand, so the tree drifted quietly: 136 files were unformatted before
2026-08-05, ten of them tests that had been that way across many commits.
Nothing failed, because nothing checked.

This is that check. It runs in the suite everyone already runs, which is
the only always-on gate this repo has — there is no CI and no pre-commit
hook.

SCOPE: the production surface. `dev/` is deliberately excluded — it is
experiment residue where one-shot scripts land and are deleted once their
conclusion is banked, and gating it would put a formatter step in front of
throwaway work. It was formatted in the same pass; it is simply not
policed. `llmvp/` polices itself in its own suite, under its own venv.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ["agent", "tests", "adapters", "tools", "ouroboros.py"]


@pytest.mark.parametrize("target", TARGETS)
def test_target_is_black_clean(target):
    path = ROOT / target
    if not path.exists():
        pytest.skip(f"{target} not present")
    proc = subprocess.run(
        [sys.executable, "-m", "black", "--check", "--quiet", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"{target} is not black-formatted. Run:\n"
        f"    .venv/bin/python3 -m black {target}\n\n"
        f"{proc.stdout}{proc.stderr}"
    )


def test_the_configured_line_length_is_what_black_actually_uses():
    """pyproject pins line-length so a future black default cannot silently
    reformat the tree. If the pin is dropped, this fails rather than the
    whole repo quietly restyling on the next dependency bump."""
    import tomllib

    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert cfg["tool"]["black"]["line-length"] == 88
