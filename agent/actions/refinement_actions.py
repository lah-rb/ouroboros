"""Refinement phase actions — push_note, scan_project, curl_search,
run_validation_checks, load_file_contents, apply_plan_revision.

These actions power the shared sub-flows introduced in the intermediate
refinement phase: prepare_context, validate_output, capture_learnings,
research_context, and revise_plan.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import urllib.parse
from typing import Any

logger = logging.getLogger(__name__)

from agent.models import StepInput, StepOutput

# ── shared utilities ──────────────────────────────────────────────────


def strip_markdown_wrapper(text: str) -> str:
    """Strip markdown code block wrappers from LLM responses.

    Handles ```json ... ```, ```python ... ```, and plain ``` ... ```
    wrappers that models add despite instructions not to.
    Returns the inner content, or the original text if no wrapper found.
    """
    text = str(text).strip()

    # Strip model ending tokens that leak into output
    for token in (
        "<|im_end|>",
        "<|im_end|",
        "<|im_end",
        "<|endoftext|>",
        "<|endoftext|",
        "<|endoftext",
        "<|end|>",
        "<|end|",
        "<|end",
        "<|eot_id|>",
        "<|eot_id|",
    ):
        text = text.replace(token, "").strip()

    # Match ```lang\n...\n``` or ```\n...\n```
    match = re.match(r"^```(?:\w+)?\s*\n([\s\S]*?)```\s*$", text)
    if match:
        return match.group(1).strip()
    return text


def extract_code_from_response(text: str) -> str:
    """Extract code from an LLM response, handling multiple wrapper formats.

    Tries multiple extraction strategies in priority order:
    1. Strip markdown wrapper (single fenced block)
    2. Find the largest fenced code block if multiple exist
    3. Remove obvious non-code lines (explanations, commentary)
    4. Fall back to raw response

    Returns the best-effort extracted code.
    """
    text = str(text).strip()

    # Strip model ending tokens
    for token in (
        "<|im_end|>",
        "<|im_end|",
        "<|im_end",
        "<|endoftext|>",
        "<|endoftext|",
        "<|endoftext",
        "<|end|>",
        "<|end|",
        "<|end",
        "<|eot_id|>",
        "<|eot_id|",
    ):
        text = text.replace(token, "").strip()

    # Strategy 1: Single clean fenced block
    single_match = re.match(r"^```(?:\w+)?\s*\n([\s\S]*?)```\s*$", text)
    if single_match:
        return single_match.group(1).strip()

    # Strategy 2: Find the largest fenced code block
    blocks = re.findall(r"```(?:\w+)?\s*\n([\s\S]*?)```", text)
    if blocks:
        largest = max(blocks, key=len)
        return largest.strip()

    # Strategy 3: Remove obvious non-code lines
    lines = text.splitlines()
    code_lines = []
    skip_prefixes = (
        "here is",
        "here's",
        "i've ",
        "i have ",
        "the following",
        "this code",
        "this implementation",
        "below is",
        "note:",
        "explanation:",
        "i changed",
        "i fixed",
        "i modified",
        "i added",
        "i updated",
        "i structured",
    )
    for line in lines:
        stripped_lower = line.strip().lower()
        if stripped_lower and any(stripped_lower.startswith(p) for p in skip_prefixes):
            continue
        code_lines.append(line)

    # If we stripped lines, return the cleaned version
    if len(code_lines) < len(lines):
        return "\n".join(code_lines).strip()

    # Strategy 4: Return as-is
    return text


# ── push_note ─────────────────────────────────────────────────────────


async def action_accumulate_correction_history(step_input: StepInput) -> StepOutput:
    """Accumulate correction attempt history for retry loops.

    Tracks what errors occurred and what fixes were attempted so the
    correction step can avoid repeating the same failed approach.

    Publishes: correction_history (list of dicts with error + fix_summary)
    """
    existing = step_input.context.get("correction_history", [])
    validation = step_input.context.get("validation_results", {})

    errors = []
    if isinstance(validation, list):
        for check in validation:
            if isinstance(check, dict) and not check.get("passed"):
                errors.append(
                    f"{check.get('name', '?')}: "
                    f"{check.get('stderr', check.get('stdout', ''))[:200]}"
                )
    elif isinstance(validation, dict):
        for check in validation.get("checks", []):
            if not check.get("passed"):
                errors.append(
                    f"{check.get('name', '?')}: "
                    f"{check.get('stderr', check.get('stdout', ''))[:200]}"
                )

    last_response = step_input.context.get("inference_response", "")
    fix_summary = (
        last_response[:200]
        if isinstance(last_response, str)
        else str(last_response)[:200]
    )

    updated = list(existing) + [
        {"error": "; ".join(errors), "fix_summary": fix_summary}
    ]

    return StepOutput(
        result={"history_length": len(updated)},
        observations=f"Correction history: {len(updated)} attempts recorded",
        context_updates={"correction_history": updated},
    )


async def action_push_note(step_input: StepInput) -> StepOutput:
    """Persist an observation to mission state notes.

    Reads note content from a configurable context key.
    Categorizes and tags for retrieval by prepare_context and create_plan.
    """
    effects = step_input.effects
    params = step_input.params

    content_key = params.get("content_key", "reflection")
    content = step_input.context.get(content_key, "")

    if isinstance(content, dict):
        content = content.get("text", content.get("response", str(content)))

    if not content or not str(content).strip():
        return StepOutput(
            result={"note_saved": False},
            observations="No content to save as note",
            context_updates={"note_saved": False},
        )

    from agent.persistence.models import NoteRecord

    note = NoteRecord(
        content=str(content).strip(),
        category=params.get("category", "general"),
        tags=params.get("tags", []),
        source_flow=params.get("source_flow", "unknown"),
        source_task=params.get("source_task", "unknown"),
    )

    if effects:
        mission = await effects.load_mission()
        if mission:
            mission.notes.append(note)
            await effects.save_mission(mission)

    return StepOutput(
        result={"note_saved": True},
        observations=f"Saved note: category={note.category}, "
        f"tags={note.tags}, length={len(note.content)}",
        context_updates={"note_saved": True},
    )


# ── scan_project ──────────────────────────────────────────────────────


async def action_scan_project(step_input: StepInput) -> StepOutput:
    """Scan workspace and extract file signatures.

    Walks the directory tree via effects.list_directory(),
    reads file signatures via effects.read_file(),
    and produces a {filepath: signature_string} manifest.
    """
    effects = step_input.effects
    params = step_input.params

    root = params.get("root", ".")
    include_patterns = params.get(
        "include_patterns",
        ["*.py", "*.yaml", "*.yml", "*.md", "*.toml", "*.json", "*.js", "*.ts", "*.rs"],
    )
    signature_depth = params.get("signature_depth", "imports_and_exports")

    if not effects:
        return StepOutput(
            result={"file_count": 0},
            observations="No effects interface",
            context_updates={"project_manifest": {}},
        )

    # Get recursive directory listing
    listing = await effects.list_directory(root, recursive=True)

    # Directories that are agent infrastructure — not project code
    infrastructure_prefixes = (".agent/", ".agent\\")

    # Filter to matching patterns — adapt to DirListing.entries protocol
    matched_files = []
    for entry in listing.entries:
        if not entry.is_file:
            continue
        filepath = entry.path
        # Skip agent infrastructure files (e.g. .agent/mission.json)
        if any(filepath.startswith(prefix) for prefix in infrastructure_prefixes):
            continue
        if any(fnmatch.fnmatch(filepath, pat) for pat in include_patterns):
            matched_files.append(filepath)

    # Extract signatures
    manifest: dict[str, str] = {}
    for filepath in matched_files:
        try:
            content = await effects.read_file(filepath)
            if content.exists:
                signature = _extract_signature(
                    filepath, content.content, signature_depth
                )
                manifest[filepath] = signature
            else:
                manifest[filepath] = "(file not readable)"
        except Exception as e:
            manifest[filepath] = f"(error reading: {e})"

    return StepOutput(
        result={"file_count": len(manifest)},
        observations=f"Scanned {len(manifest)} files in {root}",
        context_updates={"project_manifest": manifest},
    )


def _extract_signature(filepath: str, content: str, depth: str) -> str:
    """Extract a concise signature from file content."""
    lines = content.splitlines()

    if filepath.endswith(".py"):
        return _extract_python_signature(lines, depth)
    elif filepath.endswith((".yaml", ".yml")):
        return _extract_yaml_signature(lines)
    elif filepath.endswith(".md"):
        return _extract_markdown_signature(lines)
    else:
        return "\n".join(lines[:10])


def _extract_python_signature(lines: list[str], depth: str) -> str:
    """Extract Python file signature: docstring + imports + definitions."""
    parts = []

    # Module docstring
    in_docstring = False
    docstring_lines = []
    for line in lines[:30]:
        stripped = line.strip()
        if not in_docstring and stripped.startswith('"""'):
            in_docstring = True
            docstring_lines.append(stripped)
            if stripped.endswith('"""') and len(stripped) > 3:
                break
        elif in_docstring:
            docstring_lines.append(stripped)
            if '"""' in stripped:
                break
    if docstring_lines:
        parts.append("\n".join(docstring_lines))

    # Imports
    imports = [l.strip() for l in lines if l.strip().startswith(("import ", "from "))]
    if imports:
        parts.append("\n".join(imports[:15]))

    # Class and function definitions
    if depth in ("imports_and_exports", "full"):
        defs = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("class ") or stripped.startswith("def "):
                defs.append(stripped.split(":", 1)[0] + ":")
        if defs:
            parts.append("\n".join(defs[:20]))

    return "\n\n".join(parts) if parts else "(empty file)"


def _extract_yaml_signature(lines: list[str]) -> str:
    """Extract YAML file signature: top-level keys."""
    top_keys = []
    for line in lines:
        if line and not line.startswith((" ", "\t", "#", "-")):
            key = line.split(":")[0].strip()
            if key:
                top_keys.append(key)
    return "Top-level keys: " + ", ".join(top_keys[:10]) if top_keys else "(empty)"


def _extract_markdown_signature(lines: list[str]) -> str:
    """Extract Markdown file signature: headings."""
    headings = []
    for line in lines:
        if line.startswith("#"):
            headings.append(line.strip())
    return "\n".join(headings[:10]) if headings else "(empty)"


# ── extract_search_queries ────────────────────────────────────────────


async def action_extract_search_queries(step_input: StepInput) -> StepOutput:
    """Parse search queries from inference response into structured list.

    Reformatting step: reads inference_response (raw LLM text containing
    a JSON array of query strings), parses it, and publishes search_queries.
    """
    raw = step_input.context.get("inference_response", "")
    max_queries = int(step_input.params.get("max_queries", 3))

    parsed = _parse_search_queries(str(raw), max_queries)

    if not parsed:
        return StepOutput(
            result={"query_count": 0},
            observations="Could not parse search queries from inference response",
            context_updates={"search_queries": []},
        )

    return StepOutput(
        result={"query_count": len(parsed)},
        observations=f"Extracted {len(parsed)} search queries: {parsed}",
        context_updates={"search_queries": parsed},
    )


# ── curl_search ───────────────────────────────────────────────────────


async def action_curl_search(step_input: StepInput) -> StepOutput:
    """Execute web searches via curl and return raw results.

    Parses search queries from inference response (JSON array),
    fetches results via DuckDuckGo lite, extracts text.
    """
    effects = step_input.effects
    queries_raw = step_input.context.get("search_queries", "")
    max_queries = int(step_input.params.get("max_queries", 2))
    timeout = int(step_input.params.get("timeout", 15))

    if not effects:
        return StepOutput(
            result={"results_found": 0},
            observations="No effects interface",
            context_updates={"raw_search_results": []},
        )

    # Parse queries from inference response
    parsed_queries = _parse_search_queries(queries_raw, max_queries)

    if not parsed_queries:
        return StepOutput(
            result={"results_found": 0},
            observations="Could not parse search queries from inference response",
            context_updates={"raw_search_results": []},
        )

    results = []
    for query in parsed_queries:
        encoded = urllib.parse.quote_plus(query)
        cmd = [
            "curl",
            "-s",
            "-L",
            "--max-time",
            str(timeout),
            "-A",
            "Mozilla/5.0",
            f"https://lite.duckduckgo.com/lite/?q={encoded}",
        ]
        cmd_result = await effects.run_command(cmd, timeout=timeout + 5)
        if cmd_result.return_code == 0 and cmd_result.stdout:
            text = _extract_text_from_html(cmd_result.stdout)
            if text.strip():
                results.append(
                    {
                        "query": query,
                        "url": f"duckduckgo: {query}",
                        "content": text[:3000],
                    }
                )

    return StepOutput(
        result={"results_found": len(results)},
        observations=f"Searched {len(parsed_queries)} queries, "
        f"got {len(results)} results",
        context_updates={"raw_search_results": results},
    )


def _parse_search_queries(raw: str, max_queries: int) -> list[str]:
    """Extract search query strings from inference response."""
    raw = strip_markdown_wrapper(str(raw))
    # Try JSON array extraction — greedy to find outermost brackets
    json_match = re.search(r"\[[\s\S]*\]", str(raw))
    if json_match:
        try:
            items = json.loads(json_match.group())
            queries = [str(item).strip() for item in items if str(item).strip()]
            return queries[:max_queries]
        except json.JSONDecodeError:
            pass

    # Fallback: treat each non-empty line as a query
    lines = str(raw).strip().splitlines()
    queries = []
    for line in lines:
        line = line.strip().strip("-•*").strip()
        if line and len(line) > 3 and len(line) < 200:
            queries.append(line)
    return queries[:max_queries]


def _extract_text_from_html(html: str) -> str:
    """Extract readable text from HTML, removing tags."""
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    return text


# ── run_validation_checks ─────────────────────────────────────────────


async def action_run_validation_checks(step_input: StepInput) -> StepOutput:
    """Execute a sequence of validation commands from LLM strategy.

    Parses the validation_strategy from context (JSON with checks array),
    runs each command via effects.run_command(), aggregates pass/fail.
    """
    effects = step_input.effects
    strategy_raw = step_input.context.get(
        "validation_strategy",
        step_input.context.get("inference_response", ""),
    )
    max_checks = int(step_input.params.get("max_checks", 5))

    if not effects:
        return StepOutput(
            result={
                "all_required_passing": False,
                "checks_run": 0,
                "status": "skipped",
            },
            observations="No effects — validation skipped (NOT assumed pass)",
            context_updates={"validation_results": []},
        )

    checks = _parse_validation_strategy(strategy_raw, max_checks)

    if not checks:
        return StepOutput(
            result={"all_required_passing": True, "checks_run": 0},
            observations="No validation checks parsed from strategy",
            context_updates={"validation_results": []},
        )

    results = []
    all_required_passing = True

    for check in checks:
        cmd = check.get("command", [])
        if isinstance(cmd, str):
            cmd = cmd.split()
        check_timeout = check.get("timeout", 30)

        cmd_result = await effects.run_command(cmd, timeout=check_timeout)
        passed = cmd_result.return_code == 0

        results.append(
            {
                "name": check.get("name", "unnamed check"),
                "passed": passed,
                "required": check.get("required", True),
                "stdout": cmd_result.stdout[:500],
                "stderr": cmd_result.stderr[:500],
                "return_code": cmd_result.return_code,
            }
        )

        if not passed and check.get("required", True):
            all_required_passing = False
            break  # Stop on first required failure

    return StepOutput(
        result={
            "all_required_passing": all_required_passing,
            "checks_run": len(results),
            "checks_passed": sum(1 for r in results if r["passed"]),
        },
        observations="Ran {} checks: {}".format(
            len(results),
            ", ".join(
                f"{r['name']}={'PASS' if r['passed'] else 'FAIL'}" for r in results
            ),
        ),
        context_updates={"validation_results": results},
    )


def _parse_validation_strategy(raw: str, max_checks: int) -> list[dict]:
    """Extract validation checks from LLM response (JSON object)."""
    raw = strip_markdown_wrapper(str(raw))
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if json_match:
        try:
            strategy = json.loads(json_match.group())
            checks = strategy.get("checks", [])
            return [c for c in checks if isinstance(c, dict) and "command" in c][
                :max_checks
            ]
        except json.JSONDecodeError:
            pass
    return []


# ── load_file_contents ────────────────────────────────────────────────


async def action_load_file_contents(step_input: StepInput) -> StepOutput:
    """Load full file contents for selected files.

    Uses hybrid selection: deterministic repomap-related files first,
    then LLM-selected files to fill remaining budget.

    Reads file_selection from context (JSON array of {file, reason, priority}),
    loads each file via effects.read_file(), returns context_bundle.

    The context_bundle always includes the mission_objective (when available)
    so downstream steps retain awareness of the overall goal.
    """
    effects = step_input.effects
    selection_raw = step_input.context.get(
        "file_selection",
        step_input.context.get("inference_response", ""),
    )
    manifest = step_input.context.get("project_manifest", {})
    budget = int(step_input.params.get("budget", 8))
    strategy = step_input.params.get("strategy")
    target = step_input.params.get("target")
    mission_objective = step_input.params.get("mission_objective", "")

    # Get repomap-determined related files for hybrid selection
    related_files = step_input.context.get("related_files", [])

    if not effects:
        return StepOutput(
            result={"files_loaded": 0},
            context_updates={
                "context_bundle": {
                    "files": [],
                    "manifest_summary": {},
                    "mission_objective": mission_objective,
                }
            },
        )

    files_to_load: list[str] = []
    seen: set[str] = set()

    if strategy == "target_plus_neighbors":
        if target and target in manifest:
            files_to_load.append(target)
            seen.add(target)
        target_dir = os.path.dirname(target) if target else ""
        for fp in manifest:
            if fp not in seen and os.path.dirname(fp) == target_dir:
                files_to_load.append(fp)
                seen.add(fp)
                if len(files_to_load) >= budget:
                    break
    else:
        # ── Hybrid selection: deterministic repomap files first ───
        # Phase 1: Include repomap-related files (actual dependency graph)
        if related_files and isinstance(related_files, list):
            for fp in related_files:
                if fp in manifest and fp not in seen:
                    files_to_load.append(fp)
                    seen.add(fp)
                    if len(files_to_load) >= budget:
                        break

        # Phase 2: Fill remaining budget with LLM-selected files
        selected = _parse_file_selection(selection_raw, budget)
        for s in selected:
            fp = s["file"]
            if fp not in seen:
                files_to_load.append(fp)
                seen.add(fp)
                if len(files_to_load) >= budget:
                    break

    loaded = []
    research_findings = step_input.context.get("research_findings")

    for filepath in files_to_load[:budget]:
        try:
            content = await effects.read_file(filepath)
            if content.exists:
                loaded.append(
                    {
                        "path": filepath,
                        "content": content.content,
                        "size": len(content.content),
                    }
                )
            else:
                loaded.append({"path": filepath, "content": "(not found)", "size": 0})
        except Exception as e:
            loaded.append({"path": filepath, "content": f"(error: {e})", "size": 0})

    # Build import graph from loaded files for cross-module awareness
    import_graph = _extract_import_graph(loaded)

    context_bundle: dict[str, Any] = {
        "files": loaded,
        "manifest_summary": {fp: sig[:100] for fp, sig in list(manifest.items())[:20]},
    }

    # Always include mission_objective so downstream steps retain the goal
    if mission_objective:
        context_bundle["mission_objective"] = mission_objective

    # Include import graph so models understand cross-module dependencies
    if import_graph:
        context_bundle["import_graph"] = import_graph

    if research_findings:
        context_bundle["research_findings"] = research_findings

    return StepOutput(
        result={"files_loaded": len(loaded)},
        observations=f"Loaded {len(loaded)} files: "
        + ", ".join(f["path"] for f in loaded),
        context_updates={"context_bundle": context_bundle},
    )


def _extract_import_graph(loaded_files: list[dict]) -> str:
    """Extract import relationships from loaded Python files.

    Produces a compact summary like:
        game/engine.py imports → game.models, game.parser, yaml, json
        game/parser.py imports → game.models, re
        game/models.py imports → dataclasses

    This helps models understand cross-module dependencies at a glance
    without reading full file contents.
    """
    lines = []
    for file_info in loaded_files:
        path = file_info.get("path", "")
        content = file_info.get("content", "")
        if not path.endswith(".py") or not content or content.startswith("("):
            continue
        imports = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("import "):
                # import foo, bar → [foo, bar]
                modules = stripped[7:].split(",")
                for m in modules:
                    m = m.strip().split(" as ")[0].strip()
                    if m:
                        imports.append(m)
            elif stripped.startswith("from ") and " import " in stripped:
                # from foo.bar import baz → foo.bar
                module = stripped[5:].split(" import ")[0].strip()
                if module and not module.startswith("."):
                    imports.append(module)
                elif module.startswith("."):
                    # Relative import — resolve to approximate module path
                    imports.append(f"(relative) {module}")
        if imports:
            # Deduplicate while preserving order
            seen = set()
            unique = []
            for imp in imports:
                if imp not in seen:
                    seen.add(imp)
                    unique.append(imp)
            lines.append(f"{path} imports → {', '.join(unique)}")
    return "\n".join(lines)


def _parse_file_selection(raw: str, budget: int) -> list[dict]:
    """Parse LLM file selection response into structured list."""
    raw = strip_markdown_wrapper(str(raw))
    # CRITICAL: Use greedy match to find outermost brackets
    json_match = re.search(r"\[[\s\S]*\]", raw)
    if json_match:
        try:
            items = json.loads(json_match.group())
            valid = [
                item for item in items if isinstance(item, dict) and "file" in item
            ]
            valid.sort(key=lambda x: x.get("priority", 99))
            return valid[:budget]
        except json.JSONDecodeError:
            pass
    return []


# ── apply_plan_revision ───────────────────────────────────────────────


async def action_apply_plan_revision(step_input: StepInput) -> StepOutput:
    """Apply plan revision from LLM analysis.

    Handles: adding new tasks, reprioritizing, marking obsolete.
    Preserves completed and in_progress task states.
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    revision_raw = step_input.context.get("inference_response", "")

    if not mission:
        return StepOutput(
            result={"revision_applied": False},
            observations="No mission in context",
        )

    revision = _parse_revision(revision_raw)

    if not revision or not revision.get("revision_needed", False):
        return StepOutput(
            result={"revision_applied": False},
            observations="No revision needed",
        )

    from agent.persistence.models import TaskRecord

    changes: list[str] = []

    # Add new tasks (with deduplication)
    from agent.actions.mission_actions import _is_duplicate_task

    for new_task in revision.get("add_tasks", []):
        if not isinstance(new_task, dict) or "description" not in new_task:
            continue
        desc = new_task["description"]
        flow = new_task.get("flow", "create_file")
        inputs = new_task.get("inputs", {})
        target_file = inputs.get("target_file_path", "")

        if _is_duplicate_task(mission, desc, flow, target_file):
            changes.append(f"Skipped duplicate task: {desc[:60]}")
            continue

        task = TaskRecord(
            description=desc,
            flow=flow,
            priority=new_task.get("priority", len(mission.plan)),
            inputs=inputs,
            depends_on=new_task.get("depends_on", []),
        )
        mission.plan.append(task)
        changes.append(f"Added task: {task.description}")

    # Reprioritize
    for repri in revision.get("reprioritize", []):
        task_id = repri.get("task_id")
        new_priority = repri.get("new_priority")
        if task_id is None or new_priority is None:
            continue
        for task in mission.plan:
            if task.id == task_id and task.status in ("pending", "failed"):
                task.priority = new_priority
                changes.append(f"Reprioritized {task_id} to {new_priority}")

    # Mark obsolete
    for task_id in revision.get("obsolete", []):
        for task in mission.plan:
            if task.id == task_id and task.status in ("pending", "failed"):
                task.status = "complete"
                task.summary = "Obsoleted by plan revision"
                changes.append(f"Obsoleted {task_id}")

    if effects and changes:
        await effects.save_mission(mission)

    return StepOutput(
        result={"revision_applied": len(changes) > 0},
        observations=(
            f"Applied {len(changes)} plan changes: " + "; ".join(changes)
            if changes
            else "No changes applied"
        ),
        context_updates={"mission": mission},
    )


def _parse_revision(raw: str) -> dict:
    """Parse revision plan from LLM response."""
    raw = strip_markdown_wrapper(str(raw))
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return {}


# ── log_validation_notes ──────────────────────────────────────────────


async def action_log_validation_notes(step_input: StepInput) -> StepOutput:
    """Capture lint warnings and non-blocking check failures as mission notes.

    Reads validation_results from context, filters for non-blocking failures
    (required: false checks that didn't pass), and persists them as notes
    with category 'lint_warning' so the agent can fix them in future tasks.
    """
    effects = step_input.effects
    validation_results = step_input.context.get("validation_results", [])

    if not isinstance(validation_results, list):
        return StepOutput(
            result={"notes_logged": 0},
            observations="No validation results to log",
            context_updates={"lint_notes_saved": False},
        )

    # Collect non-blocking failures (lint warnings, optional test failures)
    warnings = []
    for check in validation_results:
        if not isinstance(check, dict):
            continue
        if not check.get("passed", True) and not check.get("required", True):
            warning_text = (
                f"[{check.get('name', 'unnamed')}] "
                f"rc={check.get('return_code', '?')}\n"
            )
            stdout = check.get("stdout", "").strip()
            stderr = check.get("stderr", "").strip()
            if stdout:
                warning_text += f"stdout: {stdout[:300]}\n"
            if stderr:
                warning_text += f"stderr: {stderr[:300]}\n"
            warnings.append(warning_text)

    if not warnings or not effects:
        return StepOutput(
            result={"notes_logged": 0},
            observations=(
                "No lint warnings to log" if not warnings else "No effects interface"
            ),
            context_updates={"lint_notes_saved": len(warnings) == 0},
        )

    from agent.persistence.models import NoteRecord

    note_content = "Lint/quality warnings to fix:\n" + "\n".join(warnings)
    note = NoteRecord(
        content=note_content,
        category="lint_warning",
        tags=["lint", "quality", "auto-captured"],
        source_flow="validate_output",
        source_task="unknown",
    )

    mission = await effects.load_mission()
    if mission:
        mission.notes.append(note)
        await effects.save_mission(mission)

    return StepOutput(
        result={"notes_logged": len(warnings)},
        observations=f"Logged {len(warnings)} lint warnings as mission notes",
        context_updates={"lint_notes_saved": True},
    )


# ── run_fallback_validation ───────────────────────────────────────────


async def action_run_fallback_validation(step_input: StepInput) -> StepOutput:
    """Fallback validation when LLM strategy fails to parse.

    Runs language-appropriate syntax and import checks based on file extension.
    No LLM involved — purely heuristic.
    """
    effects = step_input.effects
    file_path = step_input.params.get("file_path", "")

    if not effects or not file_path:
        return StepOutput(
            result={"all_required_passing": True, "checks_run": 0},
            observations="No effects or file_path — skipping fallback validation",
            context_updates={"validation_results": []},
        )

    results = []
    all_required_passing = True

    if file_path.endswith(".py"):
        # Tier 1: Syntax check
        syntax_result = await effects.run_command(
            [
                "python",
                "-c",
                f"import py_compile; py_compile.compile('{file_path}', doraise=True)",
            ],
            timeout=30,
        )
        syntax_passed = syntax_result.return_code == 0
        results.append(
            {
                "name": "syntax check (fallback)",
                "passed": syntax_passed,
                "required": True,
                "tier": "syntax",
                "stdout": syntax_result.stdout[:500],
                "stderr": syntax_result.stderr[:500],
                "return_code": syntax_result.return_code,
            }
        )
        if not syntax_passed:
            all_required_passing = False

        # Tier 2: Import check (only if syntax passed)
        if syntax_passed:
            module_name = _filepath_to_module(file_path)
            if module_name:
                import_result = await effects.run_command(
                    ["python", "-c", f"import {module_name}"],
                    timeout=30,
                )
                import_passed = import_result.return_code == 0
                results.append(
                    {
                        "name": "import check (fallback)",
                        "passed": import_passed,
                        "required": True,
                        "tier": "execution",
                        "stdout": import_result.stdout[:500],
                        "stderr": import_result.stderr[:500],
                        "return_code": import_result.return_code,
                    }
                )
                if not import_passed:
                    all_required_passing = False

    elif file_path.endswith((".js", ".jsx", ".ts", ".tsx")):
        # Basic syntax check for JS/TS
        check_result = await effects.run_command(
            ["node", "--check", file_path],
            timeout=30,
        )
        passed = check_result.return_code == 0
        results.append(
            {
                "name": "syntax check (fallback)",
                "passed": passed,
                "required": True,
                "tier": "syntax",
                "stdout": check_result.stdout[:500],
                "stderr": check_result.stderr[:500],
                "return_code": check_result.return_code,
            }
        )
        if not passed:
            all_required_passing = False

    elif file_path.endswith((".yaml", ".yml")):
        # YAML syntax check
        yaml_result = await effects.run_command(
            [
                "python",
                "-c",
                f"import yaml; yaml.safe_load(open('{file_path}'))",
            ],
            timeout=30,
        )
        passed = yaml_result.return_code == 0
        results.append(
            {
                "name": "yaml syntax check (fallback)",
                "passed": passed,
                "required": True,
                "tier": "syntax",
                "stdout": yaml_result.stdout[:500],
                "stderr": yaml_result.stderr[:500],
                "return_code": yaml_result.return_code,
            }
        )
        if not passed:
            all_required_passing = False

    else:
        # Unknown file type — just check it exists
        exists = await effects.file_exists(file_path)
        results.append(
            {
                "name": "file exists check (fallback)",
                "passed": exists,
                "required": True,
                "tier": "syntax",
                "stdout": "",
                "stderr": "" if exists else f"File not found: {file_path}",
                "return_code": 0 if exists else 1,
            }
        )
        if not exists:
            all_required_passing = False

    return StepOutput(
        result={
            "all_required_passing": all_required_passing,
            "checks_run": len(results),
            "checks_passed": sum(1 for r in results if r["passed"]),
        },
        observations="Fallback validation: {}".format(
            ", ".join(
                f"{r['name']}={'PASS' if r['passed'] else 'FAIL'}" for r in results
            ),
        ),
        context_updates={"validation_results": results},
    )


# ── execute_project_setup ─────────────────────────────────────────────


async def action_execute_project_setup(step_input: StepInput) -> StepOutput:
    """Execute project setup actions from LLM analysis.

    Parses setup_actions from inference response, runs commands,
    creates directories, and reports results. Language agnostic —
    the LLM decides what to set up.
    """
    effects = step_input.effects
    raw = step_input.context.get("inference_response", "")

    if not effects:
        return StepOutput(
            result={"setup_complete": False, "actions_run": 0},
            observations="No effects interface",
            context_updates={"setup_results": []},
        )

    # Parse the setup plan
    setup_plan = _parse_setup_plan(str(raw))
    if not setup_plan:
        return StepOutput(
            result={"setup_complete": False, "actions_run": 0},
            observations="Could not parse setup plan from inference response",
            context_updates={"setup_results": []},
        )

    actions = setup_plan.get("setup_actions", setup_plan.get("scaffold", []))
    results = []
    all_required_ok = True

    for action in actions:
        if not isinstance(action, dict):
            continue

        action_type = action.get("type", "command")
        name = action.get("name", action_type)
        required = action.get("required", True)

        # Check skip_if_exists
        skip_path = action.get("skip_if_exists")
        if skip_path:
            exists = await effects.file_exists(skip_path)
            if exists:
                results.append(
                    {"name": name, "skipped": True, "reason": f"{skip_path} exists"}
                )
                continue

        if action_type == "command":
            cmd = action.get("command", [])
            if isinstance(cmd, str):
                cmd = cmd.split()
            if not cmd:
                continue
            timeout = action.get("timeout", 60)
            cmd_result = await effects.run_command(cmd, timeout=timeout)
            passed = cmd_result.return_code == 0
            results.append(
                {
                    "name": name,
                    "passed": passed,
                    "required": required,
                    "stdout": cmd_result.stdout[:300],
                    "stderr": cmd_result.stderr[:300],
                }
            )
            if not passed and required:
                all_required_ok = False

        elif action_type == "directory":
            path = action.get("path", "")
            if path:
                # Create directory by writing a .gitkeep file
                await effects.write_file(f"{path}/.gitkeep", "")
                results.append({"name": name, "passed": True, "path": path})

        elif action_type in ("file", "create_file"):
            path = action.get("file_path", action.get("path", ""))
            desc = action.get("description", "")
            if path:
                # For now, create a placeholder — the content will be
                # generated by a separate create_file task if needed
                results.append(
                    {
                        "name": name,
                        "passed": True,
                        "note": f"File {path} flagged for creation: {desc}",
                    }
                )

    return StepOutput(
        result={
            "setup_complete": all_required_ok,
            "actions_run": len(results),
            "language": setup_plan.get("language", "unknown"),
        },
        observations="Setup: {}".format(
            ", ".join(
                r.get("name", "?")
                + (
                    "=SKIP"
                    if r.get("skipped")
                    else "=OK" if r.get("passed", False) else "=FAIL"
                )
                for r in results
            ),
        ),
        context_updates={"setup_results": results},
    )


def _parse_setup_plan(raw: str) -> dict:
    """Parse setup plan from LLM response."""
    raw = strip_markdown_wrapper(raw)
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return {}


# ── apply_quality_gate_results ────────────────────────────────────────


async def action_apply_quality_gate_results(step_input: StepInput) -> StepOutput:
    """Parse quality gate summary and create fix tasks if needed.

    Reads the LLM summary of project-wide validation, extracts fix_tasks,
    adds them to the mission plan, and returns pass/fail status.
    """
    effects = step_input.effects
    raw = step_input.context.get("inference_response", "")
    validation_results = step_input.context.get("validation_results", [])

    # Parse the quality summary
    summary = _parse_quality_summary(str(raw))

    if not summary:
        # Fallback: check validation_results directly
        failed_checks = [r for r in validation_results if not r.get("passed", True)]
        all_passing = len(failed_checks) == 0
        return StepOutput(
            result={
                "all_passing": all_passing,
                "issues_found": len(failed_checks),
            },
            observations=f"Quality gate: {'PASS' if all_passing else 'FAIL'} "
            f"({len(failed_checks)} failures, could not parse LLM summary)",
            context_updates={
                "quality_results": {
                    "all_passing": all_passing,
                    "summary": f"{len(failed_checks)} check failures",
                    "fix_tasks": [],
                },
            },
        )

    all_passing = summary.get("all_passing", True)
    fix_tasks = summary.get("fix_tasks", [])

    # If quality gate failed, increment attempts counter and add fix tasks
    if not all_passing and effects:
        from agent.persistence.models import TaskRecord

        mission = await effects.load_mission()
        # Build set of known files from project_manifest for existence check
        known_files = set()
        manifest = step_input.context.get("project_manifest", {})
        if isinstance(manifest, dict):
            known_files = set(manifest.keys())

        if mission:
            mission.quality_gate_attempts += 1
            existing_descriptions = {t.description for t in mission.plan}
            # Also check semantic duplicates: same flow + same target file
            existing_targets = {
                (t.flow, t.inputs.get("target_file_path", ""))
                for t in mission.plan
                if t.status != "complete"
            }
            added = 0
            skipped = 0
            for ft in fix_tasks or []:
                if not isinstance(ft, dict) or "description" not in ft:
                    continue
                # Skip if an identical task already exists
                if ft["description"] in existing_descriptions:
                    skipped += 1
                    continue
                # Skip semantic duplicates (same flow + same file)
                target_file = ft.get("file", "")
                flow = ft.get("flow", "modify_file")
                if (flow, target_file) in existing_targets:
                    skipped += 1
                    continue
                # Skip fix tasks targeting non-existent files
                if target_file and known_files and target_file not in known_files:
                    logger.warning(
                        "Quality gate: skipping fix task for non-existent file %s",
                        target_file,
                    )
                    skipped += 1
                    continue
                # Cap at 5 new tasks per gate run
                if added >= 5:
                    break
                task = TaskRecord(
                    description=ft["description"],
                    flow=flow,
                    priority=len(mission.plan),
                    inputs={
                        "target_file_path": target_file,
                        "reason": ft.get("issue", ft["description"]),
                    },
                )
                mission.plan.append(task)
                existing_targets.add((flow, target_file))
                added += 1

            # Always save — at minimum the quality_gate_attempts counter changed
            await effects.save_mission(mission)

    return StepOutput(
        result={
            "all_passing": all_passing,
            "issues_found": summary.get("failed", 0),
            "fix_tasks_added": len(fix_tasks) if not all_passing else 0,
        },
        observations=f"Quality gate: {'PASS' if all_passing else 'FAIL'} — "
        f"{summary.get('summary', 'no summary')}",
        context_updates={
            "quality_results": {
                "all_passing": all_passing,
                "summary": summary.get("summary", ""),
                "fix_tasks": fix_tasks,
            },
        },
    )


def _parse_quality_summary(raw: str) -> dict:
    """Parse quality gate summary from LLM response."""
    raw = strip_markdown_wrapper(raw)
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return {}


# ── validate_created_files ────────────────────────────────────────────


async def action_validate_created_files(step_input: StepInput) -> StepOutput:
    """Validate all files in context.files_changed.

    For each file:
    - Skip non-code files (.md, .yaml, .json, .toml, .txt, .csv, .cfg, .ini, .env)
    - Run syntax check (required)
    - Run import check (non-blocking)

    Aggregates into a single status:
    - 'success': all files pass all checks
    - 'issues': all files pass syntax but some have import issues
    - 'failed': any file fails syntax check

    Publishes: validation_results (list of per-check results across all files)
    """
    effects = step_input.effects
    files_changed = step_input.context.get("files_changed", [])

    if not files_changed:
        return StepOutput(
            result={"status": "success"},
            observations="No files to validate",
            context_updates={"validation_results": []},
        )

    if not effects:
        return StepOutput(
            result={"status": "skipped"},
            observations="No effects interface — validation skipped (NOT assumed pass)",
            context_updates={"validation_results": []},
        )

    all_results = []
    any_syntax_failed = False
    any_issues = False

    skip_extensions = {
        "md",
        "yaml",
        "yml",
        "json",
        "toml",
        "txt",
        "csv",
        "cfg",
        "ini",
        "env",
    }

    for file_path in files_changed:
        # Skip non-code files
        ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""
        if ext in skip_extensions:
            continue

        # Syntax check
        if ext == "py":
            result = await effects.run_command(
                [
                    "python",
                    "-c",
                    f"import py_compile; py_compile.compile('{file_path}', doraise=True)",
                ],
                timeout=30,
            )
            check = {
                "name": f"syntax: {file_path}",
                "passed": result.return_code == 0,
                "stdout": result.stdout[:500],
                "stderr": result.stderr[:500],
                "tier": "syntax",
                "required": True,
            }
            all_results.append(check)
            if not check["passed"]:
                any_syntax_failed = True

            # Import check (non-blocking)
            module_name = _filepath_to_module(file_path)
            if module_name:
                result = await effects.run_command(
                    ["python", "-c", f"import {module_name}"],
                    timeout=30,
                )
                check = {
                    "name": f"import: {file_path}",
                    "passed": result.return_code == 0,
                    "stdout": result.stdout[:500],
                    "stderr": result.stderr[:500],
                    "tier": "execution",
                    "required": False,
                }
                all_results.append(check)
                if not check["passed"]:
                    any_issues = True

    # Determine aggregate status
    if any_syntax_failed:
        status = "failed"
    elif any_issues:
        status = "issues"
    else:
        status = "success"

    return StepOutput(
        result={
            "status": status,
            "all_required_passing": not any_syntax_failed,
            "total_checks": len(all_results),
            "passed": sum(1 for r in all_results if r["passed"]),
            "failed": sum(1 for r in all_results if not r["passed"]),
        },
        observations=f"Validated {len(files_changed)} files: {status}",
        context_updates={"validation_results": all_results},
    )


# ── filepath_to_module helper ─────────────────────────────────────────


def _filepath_to_module(filepath: str) -> str | None:
    """Convert a file path to a Python module name.

    app/main.py → app.main
    src/utils/helpers.py → src.utils.helpers
    script.py → script
    __init__.py → (None — can't import directly)
    """
    if not filepath.endswith(".py"):
        return None
    # Strip .py extension
    module = filepath[:-3]
    # Skip __init__ files
    if module.endswith("__init__"):
        return None
    # Convert path separators to dots
    module = module.replace("/", ".").replace("\\", ".")
    # Strip leading dots
    module = module.lstrip(".")
    return module if module else None
