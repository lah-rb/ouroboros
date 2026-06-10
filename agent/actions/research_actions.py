"""Research actions — repomap querying and git history investigation.

These actions power Wave 4 research sub-flows: research_repomap,
research_codebase_history, and research_technical.
"""

from __future__ import annotations

import fnmatch
import json
from typing import Any

from agent.models import StepInput, StepOutput

# ── build_and_query_repomap ──────────────────────────────────────────


async def action_build_and_query_repomap(step_input: StepInput) -> StepOutput:
    """Build a repo map from workspace files and query it for relevant context.

    Uses tree-sitter AST parsing + PageRank ranking from agent/repomap.py.
    Returns a formatted repo map and list of related files for a given
    focus file or research query.

    Params:
        root: Directory to scan (default ".")
        include_patterns: File glob patterns to include.
        max_chars: Max characters for the formatted map output.
        focus_files: List of files to boost in ranking.

    Context:
        optional: research_query, target_file_path

    Publishes:
        repo_map_formatted: Token-budgeted map string for LLM consumption.
        related_files: List of files most related to focus files.
        raw_results: Combined output for synthesis by research dispatcher.
    """
    effects = step_input.effects
    params = step_input.params

    root = params.get("root", ".")
    include_patterns = params.get(
        "include_patterns",
        ["*.py", "*.yaml", "*.yml", "*.js", "*.ts", "*.rs"],
    )
    max_chars = int(params.get("max_chars", 4000))

    # Determine focus files from context or params
    focus_files = params.get("focus_files", [])
    if isinstance(focus_files, str):
        try:
            focus_files = json.loads(focus_files)
        except (json.JSONDecodeError, TypeError):
            focus_files = [focus_files] if focus_files else []

    target = step_input.context.get("target_file_path", "")
    if target and target not in focus_files:
        focus_files.append(target)

    if not effects:
        return StepOutput(
            result={"files_mapped": 0, "definitions_found": 0},
            observations="No effects interface available",
            context_updates={
                "repo_map_formatted": "",
                "related_files": [],
                "raw_results": "No effects interface — repo map unavailable.",
            },
        )

    # Step 1: List files recursively
    listing = await effects.list_directory(root, recursive=True)

    matched_paths = []
    for entry in listing.entries:
        if not entry.is_file:
            continue
        if any(fnmatch.fnmatch(entry.path, pat) for pat in include_patterns):
            matched_paths.append(entry.path)

    if not matched_paths:
        return StepOutput(
            result={"files_mapped": 0, "definitions_found": 0},
            observations="No matching files found for repo map",
            context_updates={
                "repo_map_formatted": "(empty project)",
                "related_files": [],
                "raw_results": "No source files found in project.",
            },
        )

    # Step 2: Read file contents
    file_contents: dict[str, str] = {}
    for path in matched_paths:
        try:
            fc = await effects.read_file(path)
            if fc.exists:
                file_contents[path] = fc.content
        except Exception:
            continue

    if not file_contents:
        return StepOutput(
            result={"files_mapped": 0, "definitions_found": 0},
            observations="Could not read any files for repo map",
            context_updates={
                "repo_map_formatted": "(no readable files)",
                "related_files": [],
                "raw_results": "Could not read any source files.",
            },
        )

    # Step 3: Build the repo map
    from agent.repomap import build_repo_map

    repo_map = build_repo_map(file_contents)

    # Step 4: Format for prompt consumption
    formatted = repo_map.format_for_prompt(
        max_chars=max_chars,
        focus_files=focus_files if focus_files else None,
    )

    # Step 5: Get related files for focus files
    related: list[str] = []
    for fp in focus_files:
        related.extend(repo_map.get_related_files(fp, max_files=5))
    # Deduplicate while preserving order
    seen = set()
    unique_related = []
    for fp in related:
        if fp not in seen:
            seen.add(fp)
            unique_related.append(fp)

    # Step 6: Build schema context (Level 1 + 2 — always on)
    from agent.schema_extract import build_schema_context

    schema_context = build_schema_context(file_contents)
    if schema_context:
        formatted += f"\n\n## Data Schemas\n{schema_context}"

    # Count total definitions
    total_defs = sum(
        len(info.definitions) for info in repo_map.files.values() if info.definitions
    )

    # Build raw_results for the research dispatcher synthesis
    raw_text = f"Repository structure map ({len(file_contents)} files, {total_defs} definitions):\n\n"
    raw_text += formatted
    if unique_related:
        raw_text += (
            f"\n\nFiles most related to {focus_files}: {', '.join(unique_related)}"
        )

    return StepOutput(
        result={
            "files_mapped": len(file_contents),
            "definitions_found": total_defs,
        },
        observations=f"Built repo map: {len(file_contents)} files, "
        f"{total_defs} definitions, {len(unique_related)} related files",
        context_updates={
            "repo_map_formatted": formatted,
            "related_files": unique_related,
            "raw_results": raw_text,
        },
    )


# ── run_git_investigation ─────────────────────────────────────────────


# ── validate_cross_file_consistency ───────────────────────────────────


async def action_validate_cross_file_consistency(step_input: StepInput) -> StepOutput:
    """Deterministic cross-file consistency check using tree-sitter repomap.

    Builds the AST-based repo map and checks for:
    1. Unresolved references — file A references symbol X but no file defines it
    2. Duplicate definitions — same symbol name defined in multiple files
    3. Orphan files — files that nothing imports and that import nothing

    This is a structural check, not a runtime check. It catches interface
    mismatches and disconnected modules before the LLM-planned quality gate.

    Params:
        root: Directory to scan (default ".")
        include_patterns: File glob patterns.

    Publishes:
        cross_file_results: Structured validation results.
        cross_file_summary: Human-readable summary for prompts.
    """
    effects = step_input.effects
    params = step_input.params

    root = params.get("root", ".")
    include_patterns = params.get("include_patterns", ["*.py", "*.js", "*.ts", "*.rs"])

    if not effects:
        return StepOutput(
            result={"issues_found": 0, "files_checked": 0},
            observations="No effects interface available",
            context_updates={
                "cross_file_results": {"issues": [], "summary": "No effects"},
                "cross_file_summary": "",
            },
        )

    # Step 1: List and read files
    listing = await effects.list_directory(root, recursive=True)
    matched_paths = []
    for entry in listing.entries:
        if not entry.is_file:
            continue
        if any(fnmatch.fnmatch(entry.path, pat) for pat in include_patterns):
            matched_paths.append(entry.path)

    if not matched_paths:
        return StepOutput(
            result={"issues_found": 0, "files_checked": 0},
            observations="No source files found",
            context_updates={
                "cross_file_results": {"issues": [], "summary": "No files"},
                "cross_file_summary": "",
            },
        )

    file_contents: dict[str, str] = {}
    for path in matched_paths:
        try:
            fc = await effects.read_file(path)
            if fc.exists:
                file_contents[path] = fc.content
        except Exception:
            continue

    if not file_contents:
        return StepOutput(
            result={"issues_found": 0, "files_checked": 0},
            observations="Could not read any files",
            context_updates={
                "cross_file_results": {"issues": [], "summary": "No readable files"},
                "cross_file_summary": "",
            },
        )

    # Step 2: Build repo map
    from agent.repomap import build_repo_map

    repo_map = build_repo_map(file_contents)

    # Step 3: Analyze for issues
    issues: list[dict[str, Any]] = []

    # Build global definition index: name → list of (file, kind)
    global_defs: dict[str, list[tuple[str, str]]] = {}
    for fp, info in repo_map.files.items():
        for defn in info.definitions:
            if defn.kind == "import":
                continue
            # Methods belong to their class's namespace — two classes in
            # different files each defining __init__/execute/etc. is not
            # a cross-file collision. Only module-level names share a
            # namespace under flat imports.
            if getattr(defn, "parent", None):
                continue
            # Dunder-pattern names are per-module conventions by design
            # (__all__, __post_init__, __version__, …) — expected to
            # repeat in every file, in any language with the convention.
            # Structural filter — no curated per-language list to maintain.
            if defn.name.startswith("__") and defn.name.endswith("__"):
                continue
            if defn.name not in global_defs:
                global_defs[defn.name] = []
            global_defs[defn.name].append((fp, defn.kind))

    # Check 1: Duplicate definitions (same name in multiple files)
    for name, locations in global_defs.items():
        if len(locations) > 1:
            # Filter: only flag if same kind (two classes named X, etc.)
            kinds = set(k for _, k in locations)
            if len(kinds) == 1 or "class" in kinds:
                files_list = [fp for fp, _ in locations]
                issues.append(
                    {
                        "type": "duplicate_definition",
                        "symbol": name,
                        "files": files_list,
                        "severity": "warning",
                        "message": f"Symbol '{name}' defined in multiple files: {', '.join(files_list)}",
                    }
                )

    # Check 2: Unresolved references
    defined_names = set(global_defs.keys())
    for fp, info in repo_map.files.items():
        for ref in info.references:
            if ref.name not in defined_names:
                # Only flag if it looks like a project symbol (not stdlib)
                # Skip short names and common patterns
                if len(ref.name) > 2 and not ref.name[0].isupper():
                    continue  # Skip — likely a variable, not a cross-file ref
                if len(ref.name) > 2:
                    issues.append(
                        {
                            "type": "unresolved_reference",
                            "symbol": ref.name,
                            "file": fp,
                            "line": ref.line,
                            "severity": "info",
                            "message": f"'{ref.name}' referenced in {fp}:{ref.line} but not defined in any project file",
                        }
                    )

    # Check 3: Orphan files (no references to/from other files)
    for fp, info in repo_map.files.items():
        if not info.definitions and not info.references:
            continue  # Empty file, skip
        related = repo_map.get_related_files(fp, max_files=1)
        has_own_defs = any(
            d.kind in ("class", "function", "method") for d in info.definitions
        )
        if not related and has_own_defs and len(file_contents) > 1:
            issues.append(
                {
                    "type": "orphan_file",
                    "file": fp,
                    "severity": "warning",
                    "message": f"'{fp}' defines symbols but has no cross-file connections — may be disconnected from the project",
                }
            )

    # Build summary
    summary_lines = []
    warnings = [i for i in issues if i["severity"] == "warning"]
    infos = [i for i in issues if i["severity"] == "info"]
    if warnings:
        summary_lines.append(f"{len(warnings)} structural warnings:")
        for w in warnings[:10]:
            summary_lines.append(f"  - {w['message']}")
    if infos:
        summary_lines.append(f"{len(infos)} unresolved references (informational)")
    if not issues:
        summary_lines.append(
            "No cross-file consistency issues found — project structure is clean."
        )

    summary_text = "\n".join(summary_lines)

    return StepOutput(
        result={
            "issues_found": len(issues),
            "warnings": len(warnings),
            "files_checked": len(file_contents),
        },
        observations=f"Cross-file validation: {len(file_contents)} files, "
        f"{len(issues)} issues ({len(warnings)} warnings)",
        context_updates={
            "cross_file_results": {
                "issues": issues,
                "summary": summary_text,
                "files_checked": len(file_contents),
            },
            "cross_file_summary": summary_text,
        },
    )


# ── select_relevant_files (deterministic) ─────────────────────────────


# ── format_technical_query ────────────────────────────────────────────
