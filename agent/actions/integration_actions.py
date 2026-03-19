"""Integration actions — apply_multi_file_changes, run_project_tests,
check_remaining_smells, restore_file_from_context, check_remaining_doc_tasks.

These actions power the integrate_modules, refactor, and document_project flows.
"""

from __future__ import annotations

import re
import logging
from typing import Any

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)


# ── Multi-file changes ────────────────────────────────────────────────


async def action_apply_multi_file_changes(step_input: StepInput) -> StepOutput:
    """Parse multi-file output and write each file through effects.

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
    if not raw_text:
        raw_text = step_input.context.get("docstring_changes", "")

    file_blocks = _parse_multi_file_output(raw_text)

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

    files_written = 0
    errors = []
    files_changed = []

    for file_path, content in file_blocks:
        try:
            wr = await effects.write_file(file_path, content)
            if wr.success:
                files_written += 1
                files_changed.append(file_path)
                logger.debug("Wrote %d bytes to %s", wr.bytes_written, file_path)
            else:
                errors.append(f"Write failed for {file_path}: {wr.error}")
        except Exception as e:
            errors.append(f"Error writing {file_path}: {e}")

    all_written = files_written == len(file_blocks) and len(errors) == 0

    return StepOutput(
        result={
            "all_written": all_written,
            "files_written": files_written,
            "total_files": len(file_blocks),
            "errors": errors,
        },
        observations=f"Wrote {files_written}/{len(file_blocks)} files"
        + (f", errors: {errors}" if errors else ""),
        context_updates={"files_changed": files_changed},
    )


def _parse_multi_file_output(text: str) -> list[tuple[str, str]]:
    """Parse text containing multiple file blocks.

    Supports formats:
        === FILE: path/to/file.py ===
        ```python
        content
        ```

    And also:
        === FILE: path/to/file.py ===
        content (until next === FILE or end)

    Returns list of (path, content) tuples.
    """
    blocks: list[tuple[str, str]] = []

    # Split on === FILE: ... === markers
    pattern = r"===\s*FILE:\s*(.+?)\s*===\s*\n"
    parts = re.split(pattern, text)

    # parts[0] is text before first marker (discard)
    # parts[1] = path, parts[2] = content, parts[3] = path, parts[4] = content, ...
    i = 1
    while i + 1 < len(parts):
        file_path = parts[i].strip()
        raw_content = parts[i + 1]

        # Extract code from fenced block if present
        code_match = re.search(r"```\w*\s*\n([\s\S]*?)```", raw_content)
        if code_match:
            content = code_match.group(1)
        else:
            # Use the raw content, stripping leading/trailing whitespace
            content = raw_content.strip()

        if file_path and content:
            blocks.append((file_path, content))
        i += 2

    return blocks


# ── Project test runner ───────────────────────────────────────────────


async def action_run_project_tests(step_input: StepInput) -> StepOutput:
    """Discover and run project tests.

    Checks for test directories/files, runs them via effects.run_command(),
    and reports whether tests pass, fail, or don't exist.

    Result: all_passing, no_tests, test_output
    Publishes: test_results
    """
    effects = step_input.effects
    if effects is None:
        return StepOutput(
            result={"all_passing": True, "no_tests": True, "test_output": ""},
            observations="No effects — assuming no tests",
            context_updates={"test_results": {"all_passing": True, "no_tests": True}},
        )

    working_dir = step_input.params.get(
        "working_directory",
        step_input.context.get("working_directory", "."),
    )

    # Check for test directories/files
    test_dirs = ["tests", "test", "tests/"]
    test_found = False

    for td in test_dirs:
        listing = await effects.list_directory(td)
        if listing.exists and len(listing.entries) > 0:
            test_found = True
            break

    if not test_found:
        # Check for individual test files
        listing = await effects.list_directory(".", recursive=True)
        for entry in listing.entries:
            if entry.is_file and (
                entry.name.startswith("test_") or entry.name.endswith("_test.py")
            ):
                test_found = True
                break

    if not test_found:
        return StepOutput(
            result={"all_passing": True, "no_tests": True, "test_output": ""},
            observations="No test files found",
            context_updates={
                "test_results": {"all_passing": True, "no_tests": True, "output": ""}
            },
        )

    # Run tests
    cmd = step_input.params.get(
        "test_command", ["uv", "run", "pytest", "tests/", "-v", "--tb=short"]
    )
    if isinstance(cmd, str):
        cmd = cmd.split()

    result = await effects.run_command(cmd, timeout=120)

    all_passing = result.return_code == 0
    output = result.stdout + "\n" + result.stderr

    return StepOutput(
        result={
            "all_passing": all_passing,
            "no_tests": False,
            "test_output": output[:2000],
            "return_code": result.return_code,
        },
        observations=f"Tests {'PASSED' if all_passing else 'FAILED'} (rc={result.return_code})",
        context_updates={
            "test_results": {
                "all_passing": all_passing,
                "no_tests": False,
                "output": output[:2000],
            }
        },
    )


# ── Refactoring helpers ──────────────────────────────────────────────


async def action_check_remaining_smells(step_input: StepInput) -> StepOutput:
    """Count remaining actionable items from smell analysis.

    Compares smell_analysis text against applied refactorings list.

    Result: remaining (int)
    Publishes: previous_refactorings (updated list)
    """
    smell_analysis = step_input.context.get("smell_analysis", "")
    previous = step_input.context.get("previous_refactorings", [])
    last_applied = step_input.context.get("refactoring_applied", "")

    # Track applied refactorings
    updated = list(previous)
    if last_applied and last_applied not in updated:
        updated.append(last_applied)

    # Estimate remaining by counting numbered items in smell analysis
    # that haven't been applied yet
    numbered = re.findall(r"(?:^|\n)\s*\d+\.\s+\*\*(.+?)\*\*", smell_analysis)
    if not numbered:
        # Fallback: count lines starting with numbers
        numbered = re.findall(r"(?:^|\n)\s*\d+\.\s+(.+)", smell_analysis)

    total_smells = len(numbered)
    remaining = max(0, total_smells - len(updated))

    return StepOutput(
        result={"remaining": remaining, "total": total_smells, "applied": len(updated)},
        observations=f"{remaining} remaining smells ({len(updated)}/{total_smells} applied)",
        context_updates={"previous_refactorings": updated},
    )


async def action_restore_file_from_context(step_input: StepInput) -> StepOutput:
    """Restore a file to its pre-refactoring state from context.

    Reads the original file content from context (target_file before refactoring)
    and writes it back via effects.

    Publishes: target_file (reset), failed_refactoring
    """
    effects = step_input.effects
    target_file = step_input.context.get("target_file", {})
    refactoring_applied = step_input.context.get("refactoring_applied", "unknown")

    if not target_file or not target_file.get("path"):
        return StepOutput(
            result={"restored": False},
            observations="No target_file in context to restore from",
            context_updates={"failed_refactoring": refactoring_applied},
        )

    if effects is None:
        return StepOutput(
            result={"restored": False},
            observations="No effects interface for restore",
            context_updates={"failed_refactoring": refactoring_applied},
        )

    path = target_file["path"]
    content = target_file.get("content", "")

    wr = await effects.write_file(path, content)

    return StepOutput(
        result={"restored": wr.success},
        observations=f"{'Restored' if wr.success else 'Failed to restore'} {path} after failed refactoring: {refactoring_applied}",
        context_updates={
            "target_file": target_file,
            "failed_refactoring": refactoring_applied,
        },
    )


# ── Documentation helpers ────────────────────────────────────────────


async def action_check_remaining_doc_tasks(step_input: StepInput) -> StepOutput:
    """Count remaining documentation tasks from assessment.

    Checks which doc tasks have been completed (readme_written,
    docstring_changes, architecture_written) against the assessment.

    Result: remaining (int)
    Publishes: docs_completed
    """
    doc_assessment = step_input.context.get("doc_assessment", "")
    readme_done = step_input.context.get("readme_written") is not None
    docstrings_done = step_input.context.get("docstring_changes") is not None
    architecture_done = step_input.context.get("architecture_written") is not None

    # Count doc tasks mentioned in assessment
    assessment_lower = doc_assessment.lower() if isinstance(doc_assessment, str) else ""
    total_tasks = 0
    completed = 0

    if "readme" in assessment_lower and (
        "missing" in assessment_lower or "inadequate" in assessment_lower
    ):
        total_tasks += 1
        if readme_done:
            completed += 1

    if "docstring" in assessment_lower and (
        "missing" in assessment_lower or "incomplete" in assessment_lower
    ):
        total_tasks += 1
        if docstrings_done:
            completed += 1

    if "architecture" in assessment_lower and (
        "missing" in assessment_lower or "no " in assessment_lower
    ):
        total_tasks += 1
        if architecture_done:
            completed += 1

    remaining = max(0, total_tasks - completed)

    docs_summary = []
    if readme_done:
        docs_summary.append("README")
    if docstrings_done:
        docs_summary.append("docstrings")
    if architecture_done:
        docs_summary.append("architecture")

    return StepOutput(
        result={"remaining": remaining, "total": total_tasks, "completed": completed},
        observations=f"{remaining} doc tasks remaining. Completed: {', '.join(docs_summary) or 'none'}",
        context_updates={"docs_completed": docs_summary},
    )
