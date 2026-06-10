"""Integration actions — apply_multi_file_changes, run_project_tests,
check_remaining_smells, restore_file_from_context, check_remaining_doc_tasks.

These actions power the integrate_modules, refactor, and document_project flows.
"""

from __future__ import annotations

import logging

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

    # ── Anti-gut guard ────────────────────────────────────────────
    # Reject rewrites that reduce file size below a threshold ratio
    # of the original. This catches LLM responses that produce a
    # minimal stub instead of a complete file rewrite.
    #
    # The ratio could potentially be set by an inference session that
    # evaluates the expected scope of changes — for now it's a fixed
    # threshold that catches catastrophic reductions while allowing
    # legitimate simplifications.
    min_retention_ratio = float(step_input.params.get("min_retention_ratio", 0.20))

    files_written = 0
    errors = []
    files_changed = []

    for file_path, content in file_blocks:
        try:
            # Check existing file size before overwriting
            if min_retention_ratio > 0:
                existing = await effects.read_file(file_path)
                if existing.exists and len(existing.content) > 0:
                    ratio = len(content) / len(existing.content)
                    if ratio < min_retention_ratio:
                        errors.append(
                            f"Anti-gut guard: {file_path} would shrink from "
                            f"{len(existing.content)} to {len(content)} chars "
                            f"({ratio:.0%} retention, minimum is "
                            f"{min_retention_ratio:.0%}). Rejecting to prevent "
                            f"content loss."
                        )
                        logger.warning(
                            "Anti-gut guard rejected write to %s: "
                            "%d→%d chars (%.0f%% retention)",
                            file_path,
                            len(existing.content),
                            len(content),
                            ratio * 100,
                        )
                        continue

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


# ── Project test runner ───────────────────────────────────────────────


# ── Refactoring helpers ──────────────────────────────────────────────


# ── Integration report ────────────────────────────────────────────────


# ── Documentation helpers ────────────────────────────────────────────
