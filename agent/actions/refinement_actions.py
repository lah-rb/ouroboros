"""Refinement phase actions — push_note, scan_project, curl_search,
run_validation_checks, load_file_contents, apply_plan_revision.

These actions power the shared sub-flows introduced in the intermediate
refinement phase: prepare_context, validate_output, capture_learnings,
research_context, and revise_plan.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import urllib.parse
from typing import Any

from agent.models import StepInput, StepOutput

# ── push_note ─────────────────────────────────────────────────────────


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

    # Filter to matching patterns — adapt to DirListing.entries protocol
    matched_files = []
    for entry in listing.entries:
        if not entry.is_file:
            continue
        filepath = entry.path
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
    # Try JSON array extraction
    json_match = re.search(r"\[[\s\S]*?\]", str(raw))
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
            result={"all_required_passing": True, "checks_run": 0},
            observations="No effects — skipping validation",
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
    json_match = re.search(r"\{[\s\S]*\}", str(raw))
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

    Reads file_selection from context (JSON array of {file, reason, priority}),
    loads each file via effects.read_file(), returns context_bundle.
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

    if not effects:
        return StepOutput(
            result={"files_loaded": 0},
            context_updates={"context_bundle": {"files": [], "manifest_summary": {}}},
        )

    files_to_load: list[str] = []

    if strategy == "target_plus_neighbors":
        if target and target in manifest:
            files_to_load.append(target)
        target_dir = os.path.dirname(target) if target else ""
        for fp in manifest:
            if fp != target and os.path.dirname(fp) == target_dir:
                files_to_load.append(fp)
                if len(files_to_load) >= budget:
                    break
    else:
        selected = _parse_file_selection(selection_raw, budget)
        files_to_load = [s["file"] for s in selected]

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

    context_bundle: dict[str, Any] = {
        "files": loaded,
        "manifest_summary": {fp: sig[:100] for fp, sig in list(manifest.items())[:20]},
    }

    if research_findings:
        context_bundle["research_findings"] = research_findings

    return StepOutput(
        result={"files_loaded": len(loaded)},
        observations=f"Loaded {len(loaded)} files: "
        + ", ".join(f["path"] for f in loaded),
        context_updates={"context_bundle": context_bundle},
    )


def _parse_file_selection(raw: str, budget: int) -> list[dict]:
    """Parse LLM file selection response into structured list."""
    json_match = re.search(r"\[[\s\S]*?\]", str(raw))
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
    revision_raw = step_input.context.get("revision_plan", "")

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

    # Add new tasks
    for new_task in revision.get("add_tasks", []):
        if not isinstance(new_task, dict) or "description" not in new_task:
            continue
        task = TaskRecord(
            description=new_task["description"],
            flow=new_task.get("flow", "create_file"),
            priority=new_task.get("priority", len(mission.plan)),
            inputs=new_task.get("inputs", {}),
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
    json_match = re.search(r"\{[\s\S]*\}", str(raw))
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return {}
