"""Shared seams for the benchmark adapters.

The adapters were built sequentially (tb → swe → gaia → tau) and each
re-implemented these; one copy each now. Domain logic stays per-adapter.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

DEFAULT_LLMVP_ENDPOINT = "http://localhost:8008/graphql"


def llmvp_endpoint() -> str:
    """The LLMVP GraphQL endpoint: OURO_LLMVP env override, else the default.

    Previously three adapters hardcoded the literal with no env fallback, so
    their runs could not be repointed at a non-default server.
    """
    return os.environ.get("OURO_LLMVP", DEFAULT_LLMVP_ENDPOINT)


def preserve_agent_dir(host_dir: str, dst: str) -> bool:
    """Copy the mission's .agent dir (mission.json + traces) to dst, best-effort.

    Returns True when something was preserved. Never raises — preservation
    must not mask the run's own outcome.
    """
    src = os.path.join(host_dir, ".agent")
    try:
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
            return True
    except Exception:
        pass
    return False


def seed_workspace_venv(workspace: str, timeout: int = 120) -> None:
    """Create an isolated .venv inside the workspace.

    Keeps mission pip installs OUT of the repo venv (the httpx 0.28→0.13
    downgrade poisoning). Raises on failure — an unisolated workspace is
    worse than a failed setup.
    """
    subprocess.run(
        [sys.executable, "-m", "venv", os.path.join(workspace, ".venv")],
        check=True,
        capture_output=True,
        timeout=timeout,
    )


def prune_mode(env_var: str, default: str = "run_end") -> str:
    """Parse an image-prune policy env var: off | run_end | per_instance.

    One parser for the SWE and TB prune policies (same three modes, different
    container-lifecycle owners — see each module's docstring).
    """
    mode = os.environ.get(env_var, default).strip().lower()
    return mode if mode in ("off", "run_end", "per_instance") else default


def remove_image(client, image: str, label: str = "") -> None:
    """Best-effort Docker image removal (frees the layer cache the VM holds
    resident). A missing/in-use image must never break the run."""
    if not client or not image:
        return
    import logging

    log = logging.getLogger(__name__)
    prefix = f"{label}: " if label else ""
    try:
        client.images.remove(image, force=True)
        log.info("%spruned image %s", prefix, image)
    except Exception as e:  # noqa: BLE001
        log.warning("%simage prune failed for %s: %s", prefix, image, e)
