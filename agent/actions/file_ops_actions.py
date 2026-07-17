"""file_ops actions — the canonical, guarded file-write path.

file_ops OWNS how a file gets written safely. Every generated-file write routes
through ``guarded_write_file`` here, so no path writes around the anti-gut guard:
- the rewrite / integrate_modules / refactor flows, via
  ``action_apply_multi_file_changes`` (the multi-file front door — parse a
  ``=== FILE: ===`` blob, write each block);
- the parallel structural batch slicer, which reuses the same
  ``guarded_write_file`` primitive directly after its own manifest slicing.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
import tomllib

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)


# ── Scaffolding parse floor ───────────────────────────────────────────
# Repo scaffolding (pyproject/tox/package.json/…) is load-bearing for
# graders and toolchains: an edit that leaves it syntactically invalid
# kills every downstream `pip install` / build (the swe-bench-fsspec
# parse_error — an invalid pyproject.toml at line 56 broke the grader's
# install). Deterministic floor: content written to a parseable config
# format must parse. Fail-safe: if the EXISTING file already doesn't
# parse (a templated yaml, a jinja config), the floor stands down — we
# only refuse to make a parseable file unparseable (or to create a new
# unparseable one).


def _parse_config(path: str, content: str) -> str | None:
    """Parse `content` per the file's format. Returns an error string, or
    None when it parses / the format isn't one we validate."""
    name = os.path.basename(path).lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    try:
        if ext == "toml":
            tomllib.loads(content)
        elif ext == "json":
            json.loads(content)
        elif ext in ("yaml", "yml"):
            try:
                import yaml
            except ImportError:
                return None
            yaml.safe_load(content)
        elif ext == "ini" or name == "setup.cfg":
            configparser.ConfigParser().read_string(content)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def scaffold_parse_error(
    path: str, content: str, existing_content: str | None
) -> str | None:
    """The floor: an error string when `content` fails to parse for a
    validated config format AND the existing file (if any) parses — else
    None (write allowed)."""
    err = _parse_config(path, content)
    if err is None:
        return None
    if existing_content and _parse_config(path, existing_content) is not None:
        return None  # file was already unparseable (template) — stand down
    return err


# ── Repair-mission write guard ────────────────────────────────────────
# A repair mission FIXES existing code against a failing test the grader
# supplies. Two classes of write are not part of a code fix:
#   TEST files       — the grader supplies the failing test; block CREATION
#                      (pilot 1: django wrote its own test_*.py across 7 cycles).
#   CONFIG/CI/DOCS   — build/CI/meta/docs files; block CREATE *and* EDIT (a code
#                      repair never touches them). pytest-10081 leaked
#                      .github/workflows/ci.yml + CONTRIBUTING.md into its patch
#                      from the ENV-setup phase — the create-only guard missed
#                      the edits. Editing project config is never a fix.
# Editing an EXISTING SOURCE file is always allowed.
_TEST_PATH_RE = re.compile(r"(^|/)(tests?)(/|$)|(^|/)(test_[^/]*|[^/]*_test)\.py$")
# config/CI/docs by path fragment (case-insensitive) — always blocked on repair.
_CONFIG_CI_DOCS_RE = re.compile(
    r"(^|/)\.github/"  # workflows, ISSUE_TEMPLATE, FUNDING, …
    r"|(^|/)(contributing|changelog|readme|authors|history)\b"
    r"|(^|/)\.pre-commit-config\.ya?ml$"
    r"|(^|/)\.?codecov\.ya?ml$"
    r"|(^|/)\.readthedocs\.ya?ml$"
    r"|(^|/)\.travis\.ya?ml$"
    r"|(^|/)azure-pipelines\.ya?ml$"
    r"|(^|/)tox\.ini$|(^|/)\.gitignore$|(^|/)\.flake8$|(^|/)ruff\.toml$"
    r"|(^|/)\.python-version$|(^|/)makefile$|(^|/)gruntfile\.js$",
    re.IGNORECASE,
)
_CONFIG_NAMES = {  # exact basenames — build/package config
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
}


async def _is_repair_mission(effects) -> bool:
    """True when the current mission is repair-profile (best-effort)."""
    if effects is None:
        return False
    try:
        from agent.actions.pipeline_actions import is_repair_profile

        return is_repair_profile(await effects.load_mission())
    except Exception:
        return False


def repair_write_reason(file_path: str) -> tuple[str | None, bool]:
    """Classify a write on a repair mission → ``(reason, block_edits)``.

    ``reason`` is set when the path is a test/config/CI/docs file a repair
    should not author (None → an ordinary source file, always allowed).
    ``block_edits`` is True for config/CI/docs (never part of a code fix →
    block CREATE and EDIT) and False for test files (block CREATE only)."""
    name = os.path.basename(file_path).lower()
    if (
        _CONFIG_CI_DOCS_RE.search(file_path)
        or name in _CONFIG_NAMES
        or (name.startswith("requirements") and name.endswith(".txt"))
    ):
        return (
            f"{file_path} is project config/CI/docs — a code repair never edits "
            "it. Fix the SOURCE that makes the failing test pass instead.",
            True,
        )
    if _TEST_PATH_RE.search(file_path):
        return (
            f"{file_path} is a test file — repair missions do not author tests "
            "(the grader supplies the failing test). Edit the SOURCE that the "
            "existing test exercises instead.",
            False,
        )
    return (None, False)


# ── The guarded write (the one safe write path) ───────────────────────


async def guarded_write_file(
    effects,
    file_path: str,
    content: str,
    min_retention_ratio: float = 0.20,
    repair_mode: bool = False,
) -> tuple[bool, str | None]:
    """Write a generated file through the anti-gut guard — the ONE write path.

    Anti-gut guard: reject a rewrite that would shrink an existing non-empty file
    below ``min_retention_ratio`` of its current size — the stub-clobbers-a-real-
    file hazard (a regeneration overwriting a brownfield file, or a catastrophic
    gut). Scaffolding parse floor: content for a parseable config format
    (toml/json/yaml/ini) must parse — never hand the grader/toolchain a broken
    pyproject. Returns ``(written, error)``: ``error`` is set on a guard
    rejection or a failed write, ``None`` on success.
    """
    existing_content: str | None = None
    # Repair guard: config/CI/docs writes are blocked outright (create OR edit —
    # never part of a code fix); a new test file is blocked (grader supplies the
    # test). Editing existing source is untouched.
    if repair_mode:
        reason, block_edits = repair_write_reason(file_path)
        if reason is not None:
            if block_edits:
                logger.warning(
                    "Repair write guard rejected config/CI/docs write %s", file_path
                )
                return False, f"Repair write guard: {reason}"
            existing = await effects.read_file(file_path)
            if not (getattr(existing, "exists", False) and (existing.content or "")):
                logger.warning(
                    "Repair write guard rejected NEW test file %s", file_path
                )
                return False, f"Repair write guard: {reason}"
    if min_retention_ratio > 0:
        existing = await effects.read_file(file_path)
        if existing.exists and len(existing.content) > 0:
            existing_content = existing.content
            ratio = len(content) / len(existing.content)
            if ratio < min_retention_ratio:
                logger.warning(
                    "Anti-gut guard rejected write to %s: %d→%d chars (%.0f%% retention)",
                    file_path,
                    len(existing.content),
                    len(content),
                    ratio * 100,
                )
                return False, (
                    f"Anti-gut guard: {file_path} would shrink from "
                    f"{len(existing.content)} to {len(content)} chars "
                    f"({ratio:.0%} retention, minimum is {min_retention_ratio:.0%})."
                )
    parse_err = scaffold_parse_error(file_path, content, existing_content)
    if parse_err:
        logger.warning(
            "Scaffold parse floor rejected write to %s: %s", file_path, parse_err
        )
        return False, (
            f"Scaffold parse floor: the new content for {file_path} does not "
            f"parse ({parse_err}). Fix the syntax — a broken config breaks "
            f"every downstream install/build."
        )
    wr = await effects.write_file(file_path, content)
    if wr.success:
        return True, None
    return False, f"Write failed for {file_path}: {wr.error}"


# ── Multi-file front door ─────────────────────────────────────────────


async def action_apply_multi_file_changes(step_input: StepInput) -> StepOutput:
    """Parse multi-file output and write each file through the guarded write.

    Expects context key containing text in the format:
        === FILE: path/to/file.py ===
        ```python
        content here
        ```

        === FILE: path/to/other.py ===
        ```python
        content here
        ```

    Also handles bare content (no code fences) after the FILE marker.

    Params:
        content_key: Context key containing the multi-file text (default: "integration_code")
        min_retention_ratio: Anti-gut threshold (default 0.20)

    Result: all_written, files_written, errors
    Publishes: files_changed
    """
    effects = step_input.effects
    if effects is None:
        return StepOutput(
            result={"all_written": False, "files_written": 0, "errors": ["No effects"]},
            observations="No effects interface — cannot write files",
            context_updates={"files_changed": []},
        )

    content_key = step_input.params.get("content_key", "integration_code")
    raw_text = step_input.context.get(content_key, "")

    # Also check inference_response if the content_key is empty
    if not raw_text:
        raw_text = step_input.context.get("inference_response", "")

    fallback_path = step_input.params.get("fallback_path", "")
    file_blocks = _parse_multi_file_output(raw_text, fallback_path=fallback_path)

    if not file_blocks:
        return StepOutput(
            result={
                "all_written": False,
                "files_written": 0,
                "errors": ["No file blocks found"],
            },
            observations="Could not parse any file blocks from the output",
            context_updates={"files_changed": []},
        )

    min_retention_ratio = float(step_input.params.get("min_retention_ratio", 0.20))
    # protect_existing (project_ops/env phase): CREATE missing files, never
    # REPLACE an existing non-empty one. On a brownfield repo the setup
    # planner regenerates scaffolding it deems "typical" (swe-bench-fsspec:
    # a Poetry pyproject.toml over the real hatch one — broke the grader's
    # install); the env phase's job is filling gaps, and targeted edits to
    # existing configs belong to the diagnosis-driven flows.
    protect_existing = bool(step_input.params.get("protect_existing", False))
    # Repair missions must not CREATE new test/scaffolding files (see
    # repair_write_reason). Resolve the profile once via the mission.
    repair_mode = await _is_repair_mission(effects)

    files_written = 0
    errors = []
    files_changed = []
    skipped_existing = []

    for file_path, content in file_blocks:
        try:
            if protect_existing:
                existing = await effects.read_file(file_path)
                if (
                    getattr(existing, "exists", False)
                    and (existing.content or "").strip()
                ):
                    skipped_existing.append(file_path)
                    logger.info(
                        "protect_existing: %s already present — not replaced", file_path
                    )
                    continue
            written_ok, err = await guarded_write_file(
                effects,
                file_path,
                content,
                min_retention_ratio,
                repair_mode=repair_mode,
            )
            if written_ok:
                files_written += 1
                files_changed.append(file_path)
                logger.debug("Wrote %s", file_path)
            elif err:
                errors.append(err)
        except Exception as e:
            errors.append(f"Error writing {file_path}: {e}")

    attempted = len(file_blocks) - len(skipped_existing)
    all_written = files_written == attempted and len(errors) == 0

    return StepOutput(
        result={
            "all_written": all_written,
            "files_written": files_written,
            "total_files": len(file_blocks),
            "skipped_existing": len(skipped_existing),
            "errors": errors,
        },
        observations=f"Wrote {files_written}/{len(file_blocks)} files"
        + (f" ({len(skipped_existing)} existing protected)" if skipped_existing else "")
        + (f", errors: {errors}" if errors else ""),
        context_updates={"files_changed": files_changed},
    )


def _parse_multi_file_output(
    text: str, fallback_path: str = ""
) -> list[tuple[str, str]]:
    """Parse text containing multiple file blocks.

    Delegates to agent.markdown_fence.parse_file_blocks which uses
    markdown-it-py for CommonMark-compliant fence extraction.

    Returns list of (path, content) tuples.
    """
    from agent.markdown_fence import parse_file_blocks

    return parse_file_blocks(text, fallback_path=fallback_path)
