"""SWE-bench artifact contract: extract the model_patch from a container.

The predictions row the official harness applies is a unified git diff of
everything the agent changed in the repo. We take it directly off the
container (docker-py exec), NOT through ContainerEffects.run_command — the
latter rewrites `<shell> -c` into interactive bash (loading ~/.bashrc) and
scrubs job-control stderr, neither of which we want in a clean diff capture.

`git add -A` first so NEW/untracked files land in the diff (plain `git diff`
misses them). Test files are captured verbatim: the SWE-bench grader resets
test files and applies its own test_patch, so stripping them here is
unnecessary and would risk dropping a legitimate non-test change under a
tests/ path.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def extract_model_patch(container, repo_dir: str = "/testbed") -> str:
    """Return the unified diff of all changes in `repo_dir`, or "" on failure.

    Runs `git add -A && git diff --cached` via a direct container exec. Always
    returns a string (never None) so a caller can write a predictions row
    unconditionally — an empty patch is a valid (unsolved) prediction, not an
    error.
    """
    script = (
        f"cd {repo_dir} && git config --global --add safe.directory {repo_dir} "
        f"2>/dev/null; git add -A && git diff --cached"
    )
    try:
        res = container.exec_run(cmd=["bash", "-lc", script], demux=True)
    except Exception as e:  # noqa: BLE001 — a missing container must not crash the run
        logger.warning("patch extraction exec failed: %s", e)
        return ""

    output = getattr(res, "output", None)
    if isinstance(output, tuple):
        stdout, stderr = output
    else:  # non-demux fallback
        stdout, stderr = output, b""
    patch = (stdout or b"").decode("utf-8", "replace")
    if not patch.strip() and stderr:
        logger.info(
            "empty diff from %s (stderr: %s)",
            repo_dir,
            (stderr or b"").decode("utf-8", "replace")[:200],
        )
    return patch


def prediction_row(instance_id: str, model_name: str, model_patch: str) -> dict:
    """The official SWE-bench predictions schema — one object per JSONL line."""
    return {
        "instance_id": instance_id,
        "model_name_or_path": model_name,
        "model_patch": model_patch or "",
    }
