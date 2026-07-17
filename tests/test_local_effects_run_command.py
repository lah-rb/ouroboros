"""LocalEffects.run_command — the timeout/kill path, pinned at last.

This exact branch (local.py: process-group SIGKILL + bounded drain) hung a
mission for 72 minutes on 2026-07-16: an sh -c heredoc check forked, the
old proc.kill() killed only sh, and the un-bounded post-kill communicate()
waited forever on the orphan's pipes. The fix shipped without a test; the
suite-quality eval found the gap. These tests drive the REAL run_command.
"""

import asyncio
import time

import pytest

from agent.effects.local import LocalEffects


@pytest.fixture()
def effects(tmp_path):
    return LocalEffects(working_directory=str(tmp_path))


def test_run_command_success(effects):
    r = asyncio.run(effects.run_command(["echo", "hello"]))
    assert r.return_code == 0 and r.stdout.strip() == "hello"
    assert r.timed_out is False


def test_run_command_timeout_returns_bounded(effects):
    t0 = time.monotonic()
    r = asyncio.run(effects.run_command(["sleep", "30"], timeout=1))
    wall = time.monotonic() - t0
    assert r.timed_out is True and r.return_code == -1
    assert "timed out" in r.stderr
    assert wall < 10  # the whole point: the timeout path itself must be bounded


def test_run_command_timeout_kills_forked_grandchildren(effects, tmp_path):
    """The wedge shape: sh -c forks, the grandchild holds the pipes.

    The process-group kill must take the WHOLE tree down and return within
    the drain bound — not wait for the grandchild's pipe EOF.
    """
    marker = f"wedge-test-{tmp_path.name}"
    script = f"sleep 30 # {marker}\n"
    t0 = time.monotonic()
    r = asyncio.run(
        effects.run_command(
            ["/bin/sh", "-c", f"/bin/sh -c 'exec sleep 30 # {marker}' & sleep 30"],
            timeout=1,
        )
    )
    wall = time.monotonic() - t0
    assert r.timed_out is True
    assert wall < 10
    # no orphaned tree members survive the group kill
    import subprocess

    left = subprocess.run(
        ["pgrep", "-f", marker], capture_output=True, text=True
    ).stdout.strip()
    assert left == "", f"orphaned processes survived: {left}"
    _ = script  # (documentational)


def test_run_command_never_shell_interprets(effects):
    # create_subprocess_exec semantics: metacharacters are argv, not shell.
    r = asyncio.run(effects.run_command(["echo", "a && rm -rf /"]))
    assert r.return_code == 0
    assert "a && rm -rf /" in r.stdout
