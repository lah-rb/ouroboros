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
            flow, target = task.get("flow", ""), task.get("inputs", {}).get("target_file_path", "")
            frust = task.get("frustration", 0)
        elif hasattr(task, "status"):
            status, desc, flow = task.status, task.description, task.flow or ""
            target = (task.inputs or {}).get("target_file_path", "")
            frust = getattr(task, "frustration", 0)
        else:
            continue
        parts = [f"{i+1:2d}. [{status:11s}]"]
        if flow: parts.append(f"{flow:15s}")
        if target: parts.append(f"→ {target}")
        parts.append(desc[:70])
        if frust > 0: parts.append(f"[frustration: {frust}]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def format_frustration_landscape(params: dict, namespaces: dict) -> str:
    frust = params.get("source") or {}
    if not frust:
        return "No frustration tracked."
    if isinstance(frust, dict):
        lines = [f"  {k}: {v}" for k, v in frust.items() if isinstance(v, (int, float)) and v > 0]
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
            lines.append(f"  {entry.get('flow', '?')}: {entry.get('task_description', '')[:50]}")
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
            lines.append(f"  [{note.get('category', '')}] {note.get('content', '')[:150]}")
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
        return (f"Import scheme: {arch.get('import_scheme', '?')}. "
                f"Run command: {arch.get('run_command', '?')}. "
                f"Modules: {', '.join(arch.get('creation_order', []))}.")
    elif hasattr(arch, "import_scheme"):
        modules = arch.canonical_files() if hasattr(arch, "canonical_files") else []
        return (f"Import scheme: {arch.import_scheme}. Run command: {arch.run_command}. "
                f"Modules: {', '.join(modules)}.")
    return str(arch)[:300]


def format_architecture_listing(params: dict, namespaces: dict) -> str:
    arch = params.get("source")
    if not arch:
        return "No architecture available."
    lines = []
    if isinstance(arch, dict):
        ex = arch.get("execution", {})
        lines.extend([f"Import scheme: {ex.get('import_scheme', '?')}",
                       f"Run command: {ex.get('run_command', '?')}", "",
                       "Modules (in creation order):"])
        for mod in arch.get("modules", []):
            lines.append(f"  - {mod.get('file', '?')}: {mod.get('responsibility', '')}")
            if mod.get("defines"):
                lines.append(f"    Defines: {', '.join(mod['defines'])}")
            if mod.get("imports_from"):
                lines.append(f"    Imports from: {mod['imports_from']}")
        for iface in arch.get("interfaces", []):
            lines.append(f"  - {iface.get('caller','?')} → {iface.get('callee','?')}: "
                         f"{iface.get('symbol','?')}({iface.get('signature','')})")
    elif hasattr(arch, "import_scheme"):
        lines.extend([f"Import scheme: {arch.import_scheme}",
                       f"Run command: {arch.run_command}"])
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
        content = f.get("content", "") if isinstance(f, dict) else getattr(f, "content", "")
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
        lines = [f"[Turn {last.get('turn','?')}] $ {last.get('command','')}", last.get("output", "")]
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


# ══════════════════════════════════════════════════════════════════════
# Result formatters
# ══════════════════════════════════════════════════════════════════════


def _r_file_operation(p: dict, ns: dict) -> str:
    s = p.get("context.edit_summary", "")
    if s: return str(s)
    f = p.get("context.files_changed", [])
    if f: return f"Files changed: {', '.join(str(x) for x in f) if isinstance(f, list) else f}"
    return f"Modified {p.get('input.target_file_path', '')}" if p.get("input.target_file_path") else "File operation completed"

def _r_modify_operation(p: dict, ns: dict) -> str:
    s = p.get("context.edit_summary", "")
    return str(s) if s else f"Modified {p.get('input.target_file_path', '')}"

def _r_file_operation_failed(p: dict, ns: dict) -> str:
    t = p.get("input.target_file_path", "")
    r = p.get("context.validation_results", "")
    msg = f"Failed to write {t}" if t else "File operation failed"
    return f"{msg}. Validation: {str(r)[:200]}" if r else msg

def _r_file_operation_with_issues(p: dict, ns: dict) -> str:
    return f"Created {p.get('input.target_file_path', '')} with validation issues"

def _r_modify_operation_with_issues(p: dict, ns: dict) -> str:
    return f"Modified {p.get('input.target_file_path', '')} with validation issues"

def _r_bail_operation(p: dict, ns: dict) -> str:
    return (f"BAIL on {p.get('input.target_file_path', '')}: "
            f"{p.get('context.bail_reason', 'No changes needed')}. "
            f"Task: {p.get('input.task_description', '')}")

def _r_file_not_found(p: dict, ns: dict) -> str:
    return f"File not found: {p.get('input.target_file_path', '')}"

def _r_diagnosis_complete(p: dict, ns: dict) -> str:
    return f"Diagnosed issue in {p.get('input.target_file_path', '')} — fix task created"

def _r_quality_gate_failed(p: dict, ns: dict) -> str:
    r = p.get("context.quality_results", "")
    return f"Quality gate FAILED. Results: {str(r)[:300] if r else 'no details'}"

def _r_interaction_result(p: dict, ns: dict) -> str:
    return f"Interaction complete: {p.get('input.task_description', '')}. Output: {str(p.get('context.terminal_output', ''))[:200]}"

def _r_interaction_issues(p: dict, ns: dict) -> str:
    return f"Interaction issues: {p.get('input.task_description', '')}. Output: {str(p.get('context.terminal_output', ''))[:200]}"

def _r_setup_complete(p: dict, ns: dict) -> str:
    f = p.get("context.files_changed", [])
    return f"Project setup complete. Files: {', '.join(str(x) for x in f)}" if f else "Project setup complete"

def _r_plan_revised(p: dict, ns: dict) -> str: return "Plan revised with new tasks"
def _r_reject_existing(p: dict, ns: dict) -> str: return "Target file already exists"
def _r_task_failed(p: dict, ns: dict) -> str: return "Task failed"
def _r_static_message(p: dict, ns: dict) -> str: return "Completed"


# ══════════════════════════════════════════════════════════════════════
# Registry exports
# ══════════════════════════════════════════════════════════════════════

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
    "extract_field": extract_field,
}

RESULT_FORMATTERS: dict[str, Any] = {
    "file_operation": _r_file_operation,
    "modify_operation": _r_modify_operation,
    "file_operation_failed": _r_file_operation_failed,
    "file_operation_with_issues": _r_file_operation_with_issues,
    "modify_operation_with_issues": _r_modify_operation_with_issues,
    "bail_operation": _r_bail_operation,
    "file_not_found": _r_file_not_found,
    "diagnosis_complete": _r_diagnosis_complete,
    "quality_gate_failed": _r_quality_gate_failed,
    "interaction_result": _r_interaction_result,
    "interaction_issues": _r_interaction_issues,
    "setup_complete": _r_setup_complete,
    "plan_revised": _r_plan_revised,
    "reject_existing": _r_reject_existing,
    "task_failed": _r_task_failed,
    "static_message": _r_static_message,
}
