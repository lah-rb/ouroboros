"""Research actions — repomap querying and git history investigation.

These actions power Wave 4 research sub-flows: research_repomap,
research_codebase_history, and research_technical.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
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

    repo_map = build_repo_map(file_contents, root)

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

    schema_context = build_schema_context(file_contents, max_chars=1500)
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


async def action_run_git_investigation(step_input: StepInput) -> StepOutput:
    """Execute planned git commands and collect formatted output.

    Reads git_commands from context (LLM-planned list of git commands),
    executes each via effects.run_command(), and collects output.

    Context:
        required: git_commands (inference response containing git commands)

    Params:
        working_directory: Directory to run git commands in.
        max_output_lines: Max lines per command output (default 100).

    Publishes:
        git_output: Formatted output from all git commands.
    """
    effects = step_input.effects
    params = step_input.params

    raw_commands = step_input.context.get("git_commands", "")
    max_lines = int(params.get("max_output_lines", 100))
    working_dir = params.get("working_directory")

    if not effects:
        return StepOutput(
            result={"any_output": False, "commands_run": 0},
            observations="No effects interface available",
            context_updates={"git_output": ""},
        )

    # Parse git commands from LLM response
    commands = _parse_git_commands(str(raw_commands))

    if not commands:
        return StepOutput(
            result={"any_output": False, "commands_run": 0},
            observations="No git commands parsed from inference response",
            context_updates={"git_output": ""},
        )

    # Execute each command and collect output
    outputs: list[str] = []
    any_output = False

    for cmd in commands:
        # Split into args list for run_command
        cmd_parts = cmd.split()
        if not cmd_parts:
            continue

        # Safety: only allow git commands
        if cmd_parts[0] != "git":
            outputs.append(f"$ {cmd}\n(skipped: only git commands allowed)\n")
            continue

        result = await effects.run_command(
            cmd_parts,
            working_dir=working_dir,
            timeout=30,
        )

        # Format the output
        section = f"$ {cmd}\n"
        if result.return_code == 0 and result.stdout.strip():
            # Truncate to max_lines
            lines = result.stdout.splitlines()
            if len(lines) > max_lines:
                section += "\n".join(lines[:max_lines])
                section += f"\n... ({len(lines) - max_lines} more lines truncated)"
            else:
                section += result.stdout.strip()
            any_output = True
        elif result.stderr.strip():
            section += f"(error: {result.stderr.strip()[:500]})"
        else:
            section += "(no output)"

        outputs.append(section)

    combined = "\n\n".join(outputs)

    return StepOutput(
        result={
            "any_output": any_output,
            "commands_run": len(outputs),
        },
        observations=f"Ran {len(outputs)} git commands, "
        f"{'got output' if any_output else 'no useful output'}",
        context_updates={"git_output": combined},
    )


def _parse_git_commands(raw: str) -> list[str]:
    """Extract git commands from LLM response.

    Handles multiple formats:
    - Lines starting with `git `
    - Lines starting with `$ git `
    - Backtick-wrapped commands: `git log --oneline`
    - Numbered lists: 1. git log --oneline
    """
    commands: list[str] = []
    seen: set[str] = set()

    # Strategy 1: Find backtick-wrapped git commands
    backtick_matches = re.findall(r"`(git\s[^`]+)`", raw)
    for match in backtick_matches:
        cmd = match.strip()
        if cmd not in seen:
            seen.add(cmd)
            commands.append(cmd)

    # Strategy 2: Find lines that start with git or $ git
    for line in raw.splitlines():
        stripped = line.strip()
        # Remove common prefixes
        for prefix in ("$ ", "- ", "* ", "1. ", "2. ", "3. ", "4. ", "5. "):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :]
                break

        stripped = stripped.strip()
        if stripped.startswith("git ") and stripped not in seen:
            seen.add(stripped)
            commands.append(stripped)

    return commands[:5]  # Cap at 5 commands


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

    repo_map = build_repo_map(file_contents, root)

    # Step 3: Analyze for issues
    issues: list[dict[str, Any]] = []

    # Build global definition index: name → list of (file, kind)
    global_defs: dict[str, list[tuple[str, str]]] = {}
    for fp, info in repo_map.files.items():
        for defn in info.definitions:
            if defn.kind == "import":
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


async def action_select_relevant_files(step_input: StepInput) -> StepOutput:
    """Deterministically select relevant files using AST graph + heuristics.

    Selection strategy:
    1. Direct dependencies: files that import or are imported by the target file.
       Determined by tree-sitter AST graph traversal.
    2. PageRank top-N: highest-ranked files from the repo map that aren't already
       included. These are files with many incoming references (structurally
       important regardless of direct connection to the target).
    3. Non-code files: .yaml, .json, .md, .toml files in the project root or
       config directories, filtered by keyword match against the task description.
    4. Cold-start fallback: if the project has fewer than 3 Python files, include
       all files (project is small enough that context bloat isn't a concern).

    No inference calls. Deterministic, instant, bounded output.
    """
    import os

    task_description = step_input.context.get(
        "task_description", step_input.params.get("task_description", "")
    )
    target_file = step_input.params.get("target_file_path", "")
    repo_map_formatted = step_input.context.get("repo_map_formatted", "")
    related_files = step_input.context.get("related_files", [])
    project_manifest = step_input.context.get("project_manifest", {})

    file_list = (
        list(project_manifest.keys()) if isinstance(project_manifest, dict) else []
    )

    # Cold-start: small project → include everything
    code_extensions = {".py", ".js", ".ts", ".rs", ".go", ".java", ".rb"}
    code_files = [
        f for f in file_list if any(f.endswith(ext) for ext in code_extensions)
    ]
    if len(code_files) < 3:
        selected = file_list[:]
        return StepOutput(
            result={"files_selected": len(selected), "strategy": "cold_start"},
            observations=f"Cold-start: small project ({len(code_files)} code files), including all {len(selected)} files",
            context_updates={"selected_files": selected},
        )

    selected: set[str] = set()

    # 1. Always include target file
    if target_file:
        selected.add(target_file)

    # 2. Direct dependencies from repomap related_files
    if related_files:
        for rf in related_files[:8]:
            selected.add(rf)

    # 3. Extract dependencies from repo_map_formatted text
    if target_file and repo_map_formatted:
        deps = _extract_dependencies_from_repomap(repo_map_formatted, target_file)
        selected.update(deps)

    # 4. PageRank top-N from the map (files ranked by structural importance)
    if repo_map_formatted:
        top_n = _extract_pagerank_top_n(repo_map_formatted, 5)
        selected.update(top_n)

    # 5. Non-code files matching task keywords
    if task_description:
        task_words = set(task_description.lower().split())
        # Remove very common words
        task_words -= {
            "the",
            "a",
            "an",
            "to",
            "in",
            "for",
            "of",
            "and",
            "or",
            "is",
            "with",
            "that",
            "this",
            "on",
        }
        non_code_extensions = {".yaml", ".yml", ".json", ".toml", ".md", ".cfg", ".ini"}
        for f in file_list:
            if any(f.endswith(ext) for ext in non_code_extensions):
                filename_base = os.path.basename(f).lower()
                # Strip extension and split on separators
                name_parts = set(
                    re.split(r"[_\-./]", os.path.splitext(filename_base)[0])
                )
                if task_words & name_parts:
                    selected.add(f)

    # Cap at context budget
    budget = int(step_input.params.get("context_budget", 10))
    selected_list = list(selected)[:budget]

    return StepOutput(
        result={"files_selected": len(selected_list), "strategy": "ast_graph"},
        observations=f"Deterministic selection: {len(selected_list)} files via AST graph + heuristics",
        context_updates={"selected_files": selected_list},
    )


def _extract_dependencies_from_repomap(
    repo_map_text: str, target_file: str
) -> list[str]:
    """Extract file paths that appear near the target file in the repo map.

    The repo map format lists files with their definitions and references.
    We look for file paths that appear in import/reference lines near our target.
    """
    files_found: list[str] = []
    # Find all file paths mentioned in the repo map
    file_pattern = re.compile(r"([a-zA-Z0-9_/.-]+\.(?:py|js|ts|rs|go|java|rb))")
    all_files_in_map = file_pattern.findall(repo_map_text)

    # Look for files mentioned near the target
    target_base = os.path.basename(target_file) if target_file else ""
    if target_base:
        # Find the section for our target file and extract nearby files
        lines = repo_map_text.split("\n")
        in_target_section = False
        for line in lines:
            if target_base in line or target_file in line:
                in_target_section = True
            elif in_target_section and line.strip() and not line.startswith(" "):
                in_target_section = False

            if in_target_section:
                found = file_pattern.findall(line)
                for f in found:
                    if f != target_file and f != target_base:
                        files_found.append(f)

    return files_found[:10]


def _extract_pagerank_top_n(repo_map_text: str, n: int) -> list[str]:
    """Extract the top-N files from the repo map (they appear first due to PageRank).

    The repo map is formatted with highest-ranked files first.
    We extract the first N file paths that look like section headers.
    """
    files: list[str] = []
    file_pattern = re.compile(
        r"^([a-zA-Z0-9_/.-]+\.(?:py|js|ts|rs|go|java|rb))",
        re.MULTILINE,
    )
    matches = file_pattern.findall(repo_map_text)
    seen: set[str] = set()
    for m in matches:
        if m not in seen:
            seen.add(m)
            files.append(m)
        if len(files) >= n:
            break
    return files


# ── format_technical_query ────────────────────────────────────────────


async def action_format_technical_query(step_input: StepInput) -> StepOutput:
    """Format a research query with technical source site filters.

    Prepends site-specific search operators to focus results on
    authoritative technical sources.

    Context:
        required: research_query

    Publishes:
        search_queries: List with the formatted technical query.
    """
    query = step_input.context.get("research_query", "")
    if not query:
        query = step_input.context.get("inference_response", "")

    if not query or not str(query).strip():
        return StepOutput(
            result={"query_count": 0},
            observations="No research query provided",
            context_updates={"search_queries": []},
        )

    query = str(query).strip()

    # Build site-filtered queries — one broad technical, one official docs
    site_filter = (
        "site:docs.python.org OR site:developer.mozilla.org OR "
        "site:realpython.com OR site:en.wikipedia.org OR site:arxiv.org"
    )
    technical_query = f"{query} {site_filter}"

    # Also keep a clean version as fallback
    queries = [technical_query, query]

    return StepOutput(
        result={"query_count": len(queries)},
        observations=f"Formatted {len(queries)} technical search queries",
        context_updates={"search_queries": queries},
    )
