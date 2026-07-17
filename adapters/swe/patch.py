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


# Paths that are NEVER part of a solution and must be kept out of model_patch:
#   .agent          — OUR scaffolding (set_env writes .agent/env.json via a
#                     RELATIVE path, which ContainerEffects routes into the
#                     container repo — it leaked into all 12 pilot patches).
#   build/dist/*.egg-info/__pycache__/.pytest_cache — build+cache artifacts an
#                     install/test command drops (requests-1142: 66 build/ files
#                     dwarfed the 8 real ones and broke `git apply`).
#   doc-build outputs (_build, out_html, htmlcov, site, .tox, mypy/ruff caches)
#                   — a doc/coverage build the agent runs (e.g. to satisfy a
#                     "docs build" acceptance check) drops a whole output tree
#                     (sphinx-8548: 240 files / 8MB of out_html/** swept in).
#   *requirements*.txt — deps are pre-installed in the SWE testbed; a repair
#                     never fixes the bug by editing them (sympy-13798/17139
#                     env-phase leak). setup.py/pyproject stay IN (rare but a
#                     real fix can touch them).
# Excluded via git pathspec magic on the diff itself (no repo mutation).
_PATCH_EXCLUDES = (
    ":(exclude).agent",
    ":(exclude).agent/**",
    ":(exclude)build/**",
    ":(exclude)dist/**",
    ":(exclude)**/*.egg-info/**",
    ":(exclude)**/__pycache__/**",
    ":(exclude).pytest_cache/**",
    ":(exclude)_build/**",
    ":(exclude)**/_build/**",
    ":(exclude)out_html/**",
    ":(exclude)**/out_html/**",
    ":(exclude)htmlcov/**",
    ":(exclude).tox/**",
    ":(exclude).mypy_cache/**",
    ":(exclude).ruff_cache/**",
    ":(exclude)site/**",
    ":(exclude)node_modules/**",
    ":(exclude)**/*requirements*.txt",
)

# Generous catastrophe backstop for pollution the name-based excludes miss (a
# build into an ARBITRARY output dir — the names above can't be exhaustive).
# Set FAR above any legitimate fix: the largest gold patches touch ~21 files /
# ~17KB, real multi-file fixes rarely exceed ~40 files, so these only trip on a
# swept build tree (sphinx-8548 = 240 files / 8MB). On a trip we fall back to a
# TRACKED-ONLY diff (drops untracked build spew, keeps edits to existing files —
# where the real fix almost always lives).
_MAX_PATCH_FILES = 100
_MAX_PATCH_BYTES = 1_000_000


def _run_diff(container, repo_dir: str, *, staged: bool) -> str:
    """One diff pass. staged=True → `git add -A && git diff --cached` (captures
    NEW files too); staged=False → `git diff` (tracked modifications only, no
    untracked). Both apply the pathspec exclusions. "" on exec failure."""
    excludes = " ".join(f"'{e}'" for e in _PATCH_EXCLUDES)
    safe = (
        f"cd {repo_dir} && git config --global --add safe.directory {repo_dir} "
        f"2>/dev/null; rm -rf .agent; "
    )
    diff = (
        f"git add -A && git diff --cached -- . {excludes}"
        if staged
        else f"git diff -- . {excludes}"
    )
    try:
        res = container.exec_run(cmd=["bash", "-lc", safe + diff], demux=True)
    except Exception as e:  # noqa: BLE001 — a missing container must not crash the run
        logger.warning("patch extraction exec failed: %s", e)
        return ""
    output = getattr(res, "output", None)
    stdout, stderr = output if isinstance(output, tuple) else (output, b"")
    patch = (stdout or b"").decode("utf-8", "replace")
    if not patch.strip() and stderr:
        logger.info(
            "empty diff from %s (stderr: %s)",
            repo_dir,
            (stderr or b"").decode("utf-8", "replace")[:200],
        )
    return patch


def extract_model_patch(container, repo_dir: str = "/testbed") -> str:
    """Return the unified diff of source changes in `repo_dir`, or "" on failure.

    Captures new source files (`git add -A`) with the scaffolding/artifact
    exclusions above, then a GENEROUS patch-sanity backstop: if the result is
    still catastrophically large (a build into an unanticipated dir), fall back
    to a tracked-only diff so the real fix survives without the build spew.
    Always returns a string (never None) — an empty patch is a valid prediction.
    """
    patch = _run_diff(container, repo_dir, staged=True)
    n_files = patch.count("diff --git ")
    if n_files > _MAX_PATCH_FILES or len(patch) > _MAX_PATCH_BYTES:
        logger.warning(
            "patch pollution: %d files / %d bytes exceeds sanity cap "
            "(%d / %d) — retrying tracked-only (dropping untracked spew)",
            n_files,
            len(patch),
            _MAX_PATCH_FILES,
            _MAX_PATCH_BYTES,
        )
        tracked = _run_diff(container, repo_dir, staged=False)
        # Keep tracked-only if it recovered a real (bounded) change; else the
        # polluted patch is useless to the grader (and git apply chokes on 8MB) —
        # ship "" (honest "unsolved") rather than a build tree.
        if tracked.strip() and tracked.count("diff --git ") <= _MAX_PATCH_FILES:
            return tracked
        return ""
    return patch


def prediction_row(instance_id: str, model_name: str, model_patch: str) -> dict:
    """The official SWE-bench predictions schema — one object per JSONL line."""
    return {
        "instance_id": instance_id,
        "model_name_or_path": model_name,
        "model_patch": model_patch or "",
    }
