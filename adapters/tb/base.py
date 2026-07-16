"""Harness-agnostic helpers shared by the two terminal-bench agents.

adapters.tb.agent (TB1, task.yaml, sync perform_task) and
adapters.tb.harbor_agent (TB2/Harbor, task.toml, async run) were built as
near-clones; everything here is the part that never depended on the
harness. Each agent keeps only its harness glue: task-config reading,
entry-point contract, and result shaping.

This module also restores probe_container_cwd/token_totals, which an
over-greedy cleanup regex deleted from BOTH agents alongside the dead
_select_flow_set methods (callers survived — a live AttributeError).
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path
from typing import Iterable, Optional

_DEP_PAT = re.compile(r"(?:uv\s+add|uv\s+pip\s+install|pip3?\s+install)\s+([^\n;&|]+)")


def _exec_kwargs(exec_user: Optional[str]) -> dict:
    """Harbor execs as a task-declared user; TB1 uses the image default."""
    return {"user": exec_user} if exec_user else {}


def probe_container_cwd(container, exec_user: Optional[str] = None) -> str:
    """The task's 'current directory' — the container's default WORKDIR."""
    try:
        res = container.exec_run(cmd=["pwd"], **_exec_kwargs(exec_user))
        out = (res.output or b"").decode("utf-8", "replace").strip()
        first = out.splitlines()[0].strip() if out else ""
        if first.startswith("/"):
            return first
    except Exception:
        pass
    return "/app"


def token_totals(host_tmp: str) -> tuple[int, int]:
    """Sum inference token usage from the flushed trace JSONL (reporting
    only — never affects pass/fail). Best-effort; zeros on any issue."""
    tin = tout = 0
    try:
        for path in glob.glob(os.path.join(host_tmp, ".agent", "traces", "*.jsonl")):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tin += int(row.get("tokens_in") or 0)
                    tout += int(row.get("tokens_out") or 0)
    except Exception:
        return 0, 0
    return tin, tout


def extract_deps(script_files: Iterable[Path]) -> list[str]:
    """Package names from ``uv add`` / ``pip install`` lines in the task's
    grading/test scripts. The caller supplies the file list (TB1:
    run-tests.sh + tests/*.sh; TB2: tests/*.sh)."""
    pkgs: set[str] = set()
    for p in script_files:
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        for m in _DEP_PAT.finditer(text):
            for tok in m.group(1).split():
                tok = tok.strip().strip("\"'")
                if not tok or tok.startswith("-"):
                    continue
                if any(c in tok for c in "$/.=") or tok in ("install", "add"):
                    continue
                pkgs.add(tok)
    return sorted(pkgs)


def mirror_test_env(
    container,
    pkgs: list[str],
    cwd: str,
    exec_user: Optional[str] = None,
) -> list[str]:
    """Install the deps the task's grading scripts install, into the
    container's system python, so the agent's ``python3 x.py`` (and our
    checks) see the same env the bench grades in. Best-effort.

    The t-bench ubuntu images ship NO pip and NO ensurepip (the graders use
    ``uv``), so a bare ``python3 -m pip install`` is a silent no-op — which
    is exactly why csv-class tasks thrashed forever on install. So: ensure
    pip first (``apt-get install python3-pip``, ~15s), THEN install with
    --break-system-packages (PEP-668). Returns the deps it attempted."""
    if not pkgs:
        return []
    eu = _exec_kwargs(exec_user)
    try:
        has_pip = (
            container.exec_run(
                cmd=["python3", "-m", "pip", "--version"], **eu
            ).exit_code
            == 0
        )
        if not has_pip:
            container.exec_run(cmd=["apt-get", "update", "-q"], workdir=cwd, **eu)
            container.exec_run(
                cmd=["apt-get", "install", "-y", "-q", "python3-pip"],
                workdir=cwd,
                **eu,
            )
        container.exec_run(
            cmd=[
                "python3", "-m", "pip", "install", "--quiet",
                "--break-system-packages", *pkgs,
            ],
            workdir=cwd,
            **eu,
        )
    except Exception:
        pass
    return pkgs


def per_task_cap(
    task_timeout_s: Optional[float],
    *,
    fraction: float,
    fallback: float,
    multiplier: float,
    global_override: Optional[str],
) -> float:
    """Self-cap at ~fraction× the harness's wait_for budget so we park
    before it fires (and don't self-handicap). Pure math — the caller reads
    its own task-config format (task.yaml / task.toml) and passes the
    configured timeout (None/0 → fallback)."""
    if global_override:
        try:
            return max(60.0, float(global_override) * fraction)
        except ValueError:
            pass
    if task_timeout_s and task_timeout_s > 0:
        return max(60.0, task_timeout_s * multiplier * fraction)
    return max(60.0, fallback * multiplier)
