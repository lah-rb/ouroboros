"""Pre-compute and result formatter implementations.

Pre-compute formatters transform raw context data into prompt-ready strings.
Result formatters build human-readable tail_call messages.

Both are registered by name and invoked by loader_v2.py.
Signature: (params: dict, namespaces: dict) -> str
"""

from __future__ import annotations

from typing import Any

# ══════════════════════════════════════════════════════════════════════
# Pre-compute formatters
# ══════════════════════════════════════════════════════════════════════


def format_plan_listing(params: dict, namespaces: dict) -> str:
    plan = params.get("source") or []
    if not plan:
        return "No plan exists yet."
    lines = []
    for i, task in enumerate(plan):
        if isinstance(task, dict):
            status, desc = task.get("status", "pending"), task.get("description", "")
            flow, target = task.get("flow", ""), task.get("inputs", {}).get(
                "target_file_path", ""
            )
            frust = task.get("frustration", 0)
        elif hasattr(task, "status"):
            status, desc, flow = task.status, task.description, task.flow or ""
            target = (task.inputs or {}).get("target_file_path", "")
            frust = getattr(task, "frustration", 0)
        else:
            continue
        parts = [f"{i+1:2d}. [{status:11s}]"]
        if flow:
            parts.append(f"{flow:15s}")
        if target:
            parts.append(f"→ {target}")
        parts.append(desc[:70])
        if frust > 0:
            parts.append(f"[frustration: {frust}]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def format_frustration_landscape(params: dict, namespaces: dict) -> str:
    frust = params.get("source") or {}
    if not frust:
        return "No frustration tracked."
    if isinstance(frust, dict):
        lines = [
            f"  {k}: {v}"
            for k, v in frust.items()
            if isinstance(v, (int, float)) and v > 0
        ]
        return "\n".join(lines) if lines else "All frustration levels at 0."
    return str(frust)[:500]


def format_dispatch_history(params: dict, namespaces: dict) -> str:
    history = params.get("source") or []
    limit = params.get("limit", 5)
    if not history:
        return ""
    recent = history[-limit:] if isinstance(history, list) else []
    lines = []
    for entry in recent:
        if isinstance(entry, dict):
            lines.append(
                f"  {entry.get('flow', '?')}: {entry.get('task_description', '')[:50]}"
            )
        else:
            lines.append(f"  {str(entry)[:60]}")
    return "\n".join(lines)


def format_notes(params: dict, namespaces: dict) -> str:
    notes = params.get("source") or []
    limit = params.get("limit", 5)
    if not notes:
        return ""
    recent = notes[-limit:] if isinstance(notes, list) else []
    lines = []
    for note in recent:
        if isinstance(note, dict):
            lines.append(
                f"  [{note.get('category', '')}] {note.get('content', '')[:150]}"
            )
        elif hasattr(note, "category"):
            lines.append(f"  [{note.category}] {note.content[:150]}")
        else:
            lines.append(f"  {str(note)[:150]}")
    return "\n".join(lines)


def format_architecture_summary(params: dict, namespaces: dict) -> str:
    arch = params.get("source")
    if not arch:
        return ""
    if isinstance(arch, dict):
        return (
            f"Import scheme: {arch.get('import_scheme', '?')}. "
            f"Run command: {arch.get('run_command', '?')}. "
            f"Modules: {', '.join(arch.get('creation_order', []))}."
        )
    elif hasattr(arch, "import_scheme"):
        modules = arch.canonical_files() if hasattr(arch, "canonical_files") else []
        return (
            f"Import scheme: {arch.import_scheme}. Run command: {arch.run_command}. "
            f"Modules: {', '.join(modules)}."
        )
    return str(arch)[:300]


def format_architecture_listing(params: dict, namespaces: dict) -> str:
    arch = params.get("source")
    if not arch:
        return "No architecture available."
    lines = []
    if isinstance(arch, dict):
        ex = arch.get("execution", {})
        lines.extend(
            [
                f"Import scheme: {ex.get('import_scheme', '?')}",
                f"Run command: {ex.get('run_command', '?')}",
                "",
                "Modules (in creation order):",
            ]
        )
        for mod in arch.get("modules", []):
            lines.append(f"  - {mod.get('file', '?')}: {mod.get('responsibility', '')}")
            if mod.get("defines"):
                lines.append(f"    Defines: {', '.join(mod['defines'])}")
            if mod.get("imports_from"):
                lines.append(f"    Imports from: {mod['imports_from']}")
        for iface in arch.get("interfaces", []):
            lines.append(
                f"  - {iface.get('caller','?')} → {iface.get('callee','?')}: "
                f"{iface.get('symbol','?')}({iface.get('signature','')})"
            )
    elif hasattr(arch, "import_scheme"):
        lines.extend(
            [f"Import scheme: {arch.import_scheme}", f"Run command: {arch.run_command}"]
        )
        if hasattr(arch, "modules"):
            for mod in arch.modules:
                lines.append(f"  - {mod.file}: {mod.responsibility}")
    return "\n".join(lines)


def format_existing_architecture(params: dict, namespaces: dict) -> str:
    return format_architecture_listing(params, namespaces)


def format_mission_meta(params: dict, namespaces: dict) -> str:
    mission = params.get("mission")
    field = params.get("field", "")
    if not mission or not field:
        return ""
    current = mission
    for part in field.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            return ""
        if current is None:
            return ""
    return str(current)


def format_cycle_status(params: dict, namespaces: dict) -> str:
    return "true" if not params.get("last_status") else ""


def format_file_excerpts(params: dict, namespaces: dict) -> str:
    files = params.get("source") or []
    exclude = params.get("exclude", "")
    max_chars = params.get("max_chars", 1500)
    if not files:
        return ""
    lines = []
    for f in files:
        path = f.get("path", "") if isinstance(f, dict) else getattr(f, "path", "")
        content = (
            f.get("content", "") if isinstance(f, dict) else getattr(f, "content", "")
        )
        if path == exclude:
            continue
        lines.extend([f"──── {path} ────", content[:max_chars], ""])
    return "\n".join(lines)


def format_project_file_list(params: dict, namespaces: dict) -> str:
    manifest = params.get("source") or {}
    if not manifest:
        return ""
    if isinstance(manifest, dict):
        return "\n".join(f"- {p}" for p in manifest.keys())
    if isinstance(manifest, list):
        return "\n".join(f"- {item}" for item in manifest)
    return str(manifest)[:500]


def format_project_listing(params: dict, namespaces: dict) -> str:
    manifest = params.get("source") or {}
    if not manifest:
        return ""
    lines = []
    for filepath, sig in manifest.items():
        lines.append(f"- {filepath}")
        if sig:
            lines.append(f"  {str(sig)[:120]}")
    return "\n".join(lines)


def format_validation_results(params: dict, namespaces: dict) -> str:
    results = params.get("source") or []
    if not results:
        return "No validation results."
    lines = []
    for check in results:
        if not isinstance(check, dict):
            continue
        status = "PASS" if check.get("passed", False) else "FAIL"
        lines.append(f"- {check.get('name', '?')}: {status}")
        if not check.get("passed"):
            for key in ("stdout", "stderr"):
                val = check.get(key, "")
                if val:
                    lines.append(f"  {key}: {val[:200]}")
    return "\n".join(lines)


def format_session_history(params: dict, namespaces: dict) -> str:
    history = params.get("source") or []
    if not history:
        return "No commands have been run yet."
    lines = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        lines.append(f"[Turn {entry.get('turn','?')}] $ {entry.get('command','')}")
        if entry.get("output"):
            lines.append(entry["output"])
        if entry.get("return_code", 0) != 0:
            lines.append(f"(exit code: {entry['return_code']})")
        if entry.get("timed_out"):
            lines.append("⚠️ TIMED OUT")
        lines.append("")
    return "\n".join(lines)


def format_last_command(params: dict, namespaces: dict) -> str:
    history = params.get("source") or []
    if not history:
        return ""
    last = history[-1] if isinstance(history, list) else history
    if isinstance(last, dict):
        lines = [
            f"[Turn {last.get('turn','?')}] $ {last.get('command','')}",
            last.get("output", ""),
        ]
        if last.get("return_code", 0) != 0:
            lines.append(f"(exit code: {last['return_code']})")
        return "\n".join(lines)
    return str(last)[:500]


def format_turn_count(params: dict, namespaces: dict) -> str:
    return str(len(params.get("source") or []))


def format_file_listing(params: dict, namespaces: dict) -> str:
    return format_file_excerpts(params, namespaces)


def extract_field(params: dict, namespaces: dict) -> str:
    source = params.get("source")
    field = params.get("field", "")
    if source is None or not field:
        return ""
    if isinstance(source, dict):
        return str(source.get(field, ""))
    if hasattr(source, field):
        return str(getattr(source, field, ""))
    return ""


def format_architecture_for_quality(params: dict, namespaces: dict) -> str:
    return format_architecture_summary(params, namespaces)


def format_repo_map(params: dict, namespaces: dict) -> str:
    """Pass through the pre-formatted repo map string.

    The repo map (tree-sitter symbol signatures + structure) provides
    complete interface contracts for all project files without dumping
    full file contents. Used by full_rewrite to give the LLM visibility
    into cross-file interfaces.
    """
    source = params.get("source", "")
    if isinstance(source, str):
        return source
    return str(source) if source else ""


# ══════════════════════════════════════════════════════════════════════
# Result formatters — REMOVED
# ══════════════════════════════════════════════════════════════════════
#
# Result formatters (the _r_* functions and RESULT_FORMATTERS registry)
# have been removed as part of the Context Contract Architecture.
# Flows now declare structured `returns` in their CUE definitions,
# and the runtime assembles them via assemble_returns() in loader_v2.py.
# The director's prompt template formats the structured dict for display.
#
# NOTE: The persistence system for relevant_notes uses Option A
# (declared inputs with explicit semantics). This should be re-evaluated
# alongside a comprehensive persistence system audit.


# ══════════════════════════════════════════════════════════════════════
# New formatters for Context Contract Architecture
# ══════════════════════════════════════════════════════════════════════


def format_goals_listing(params: dict, namespaces: dict) -> str:
    """Format GoalRecords for director reasoning prompt."""
    goals = params.get("source") or []
    if not goals:
        return "No goals defined yet. Run design_and_plan to derive goals."
    lines = []
    for i, goal in enumerate(goals):
        if isinstance(goal, dict):
            status = goal.get("status", "pending")
            desc = goal.get("description", "")
            gtype = goal.get("type", "structural")
            gid = goal.get("id", "?")
            files = goal.get("associated_files", [])
        elif hasattr(goal, "status"):
            status, desc, gtype = goal.status, goal.description, goal.type
            gid = goal.id
            files = getattr(goal, "associated_files", [])
        else:
            continue
        parts = [f"{i+1:2d}. [{status:11s}] ({gtype[:5]})"]
        parts.append(desc[:80])
        if files:
            parts.append(f"  files: {', '.join(files[:5])}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def format_structured_result(params: dict, namespaces: dict) -> str:
    """Format a structured returns dict for the director reasoning prompt.

    Converts the structured last_result dict into a readable summary.
    Unlike the old result formatters, this is a single generic formatter
    that works for any flow's returns.
    """
    source = params.get("source")
    if not source:
        return ""
    if isinstance(source, str):
        return source  # Already a string (legacy compatibility)
    if not isinstance(source, dict):
        return str(source)[:500]

    lines = []
    for key, value in source.items():
        if value is None:
            continue
        if isinstance(value, list):
            if value:
                lines.append(f"  {key}: {', '.join(str(v) for v in value[:10])}")
        elif isinstance(value, dict):
            # Compact dict representation
            lines.append(f"  {key}: {str(value)[:200]}")
        elif isinstance(value, bool):
            lines.append(f"  {key}: {'yes' if value else 'no'}")
        else:
            lines.append(f"  {key}: {str(value)[:200]}")
    return "\n".join(lines) if lines else ""


# ══════════════════════════════════════════════════════════════════════
# Registry exports
# ══════════════════════════════════════════════════════════════════════


def format_run_context(params: dict, namespaces: dict) -> str:
    """Build project context for terminal sessions.

    Assembles run command, project tooling hints (uv/pip/poetry from
    manifest file detection), and file listing so the terminal model
    knows how to run the project without guessing.
    """
    lines = []

    run_command = params.get("run_command", "")
    if run_command:
        lines.append(f"Run command: {run_command}")

    manifest = params.get("manifest", {})

    if isinstance(manifest, dict):
        filenames = set(manifest.keys())
        if "pyproject.toml" in filenames:
            if "uv.lock" in filenames:
                lines.append("Package manager: uv (uv.lock present)")
                if run_command:
                    lines.append(f"Use: uv run {run_command}")
            elif "poetry.lock" in filenames:
                lines.append("Package manager: poetry")
                if run_command:
                    lines.append(f"Use: poetry run {run_command}")
            elif "Pipfile.lock" in filenames:
                lines.append("Package manager: pipenv")
            else:
                lines.append("Build system: pyproject.toml")
                if run_command:
                    lines.append(f"Try: uv run {run_command}")

        file_list = [f for f in filenames if not f.startswith(".")]
        if file_list:
            lines.append(f"Project files: {', '.join(sorted(file_list)[:15])}")

    return "\n".join(lines) if lines else ""


# ══════════════════════════════════════════════════════════════════════
# Persona formatters
# ══════════════════════════════════════════════════════════════════════

# Lazy-loaded persona data from compiled.json
_persona_cache: dict[str, str] | None = None


def _load_personas() -> dict[str, str]:
    """Load persona definitions from compiled.json, cached after first call."""
    global _persona_cache
    if _persona_cache is not None:
        return _persona_cache

    import json
    from pathlib import Path

    _persona_cache = {}
    for candidate in [Path("flows/compiled.json"), Path("ouroboros/flows/compiled.json")]:
        if candidate.exists():
            with open(candidate) as f:
                data = json.load(f)
            for name, flow_data in data.items():
                if isinstance(flow_data, dict) and "flow_persona" in flow_data:
                    _persona_cache[name] = flow_data["flow_persona"].strip()
            break
    return _persona_cache


def format_flow_persona(params: dict, namespaces: dict) -> str:
    """Format the ---ACT AS--- block for the current flow's persona.

    Params:
        source: The flow_persona string (from $ref to the flow definition).

    Returns:
        Formatted ---ACT AS--- block, or empty string if no persona.
    """
    persona = params.get("source", "")
    if not persona:
        return ""
    return f"---ACT AS---\n{persona.strip()}"


def format_known_personas(params: dict, namespaces: dict) -> str:
    """Format the ---PEERS--- block with persona descriptions of peer flows.

    Params:
        source: List of flow names whose personas to include.

    Returns:
        Formatted ---PEERS--- block, or empty string if no peers.
    """
    flow_names = params.get("source") or []
    if not flow_names:
        return ""

    personas = _load_personas()
    blocks = []
    for name in flow_names:
        if isinstance(name, str) and name in personas:
            blocks.append(personas[name])

    if not blocks:
        return ""
    return "---PEERS---\n" + "\n\n".join(blocks)


def format_dep_coverage_issues(params: dict, namespaces: dict) -> str:
    """Format dependency coverage issues for the quality gate summarizer."""
    issues = params.get("source") or []
    if not issues:
        return ""
    if isinstance(issues, list):
        return "\n".join(str(i) for i in issues)
    return str(issues)[:2000]


PRE_COMPUTE_FORMATTERS: dict[str, Any] = {
    "format_plan_listing": format_plan_listing,
    "format_frustration_landscape": format_frustration_landscape,
    "format_dispatch_history": format_dispatch_history,
    "format_notes": format_notes,
    "format_architecture_summary": format_architecture_summary,
    "format_architecture_listing": format_architecture_listing,
    "format_existing_architecture": format_existing_architecture,
    "format_mission_meta": format_mission_meta,
    "format_cycle_status": format_cycle_status,
    "format_file_excerpts": format_file_excerpts,
    "format_project_file_list": format_project_file_list,
    "format_project_listing": format_project_listing,
    "format_validation_results": format_validation_results,
    "format_session_history": format_session_history,
    "format_last_command": format_last_command,
    "format_turn_count": format_turn_count,
    "format_file_listing": format_file_listing,
    "format_architecture_for_quality": format_architecture_for_quality,
    "format_repo_map": format_repo_map,
    "format_run_context": format_run_context,
    "extract_field": extract_field,
    # New formatters for Context Contract Architecture
    "format_goals_listing": format_goals_listing,
    "format_structured_result": format_structured_result,
    # Persona formatters
    "format_flow_persona": format_flow_persona,
    "format_known_personas": format_known_personas,
    # A1: Dependency coverage
    "format_dep_coverage_issues": format_dep_coverage_issues,
}

# RESULT_FORMATTERS removed — replaced by structured returns declarations.
# See assemble_returns() in loader_v2.py.
RESULT_FORMATTERS: dict[str, Any] = {}
