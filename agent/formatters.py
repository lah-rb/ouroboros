"""Pre-compute formatter implementations.

Pre-compute formatters transform raw context data into prompt-ready strings.
Registered by name and invoked by loader.py before template rendering.
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
    # STUB: Task system removed. This formatter is kept as a no-op
    # until CUE flows that reference it are updated in Phase 6.
    return "Plan listing unavailable — task system removed, goals are the plan."


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
    return str(arch)


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


def format_feedback_block(params: dict, namespaces: dict) -> str:
    """Render an ops TaskState's last_feedback as a charter feedback block
    (empty on the first attempt) so the next run_session addresses it."""
    td = params.get("source")
    fb = str(getattr(td, "last_feedback", "") if td else "").strip()
    if not fb:
        return ""
    return f"## FEEDBACK FROM YOUR LAST ATTEMPT — address this\n{fb}"


def format_session_tail(params: dict, namespaces: dict) -> str:
    """Tail of a terminal session transcript, for the completion judge."""
    out = str(params.get("source") or "")
    n = int(params.get("max_chars", 2000))
    return out[-n:] if len(out) > n else out


def format_completion_criteria(params: dict, namespaces: dict) -> str:
    """Render an ops TaskState's completion_criteria as the {"checks": [...]}
    JSON that action_run_validation_checks consumes (the ops definition-of-done
    fed to the reused check-runner each work cycle).

    Each check's command is wrapped as ``["/bin/sh", "-c", cmd]`` so it runs
    through a shell: the criteria are shell one-liners (quotes, pipes, ``$(...)``,
    ``[ ... ]``, ``&&``), but the effects' run_command execs argv with NO shell
    and the check-runner naive-splits a bare string — which mangles every
    non-trivial check (``grep -qE 'x'`` keeps the quotes literal; ``a | b``
    becomes args to ``a``). Passing a list skips the split and the shell parses
    the full line. Matches the ``/bin/sh -c`` convention in pipeline_actions.
    The stored criteria stay readable strings; only the rendered strategy wraps.
    """
    import json

    criteria = params.get("source") or []
    checks = []
    for c in criteria:
        if not isinstance(c, dict):
            continue
        cmd = c.get("command")
        if isinstance(cmd, str) and cmd.strip():
            checks.append({**c, "command": ["/bin/sh", "-c", cmd]})
        else:
            checks.append(c)
    return json.dumps({"checks": checks})


def format_existing_goals(params: dict, namespaces: dict) -> str:
    """Render the mission's current goals (type/status/description) so a
    brownfield planner sees what already exists and won't re-emit it."""
    goals = params.get("source") or []
    if not goals:
        return "No existing goals."
    lines = []
    for g in goals:
        gtype = getattr(g, "type", None) or (
            g.get("type") if isinstance(g, dict) else "?"
        )
        status = getattr(g, "status", None) or (
            g.get("status") if isinstance(g, dict) else "?"
        )
        desc = getattr(g, "description", None) or (
            g.get("description") if isinstance(g, dict) else ""
        )
        # First line only — placement hints can make descriptions multi-line.
        desc = str(desc).splitlines()[0] if desc else ""
        lines.append(f"  - [{gtype}, {status}] {desc}")
    return "\n".join(lines)


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


def format_project_file_list(params: dict, namespaces: dict) -> str:
    manifest = params.get("source") or {}
    if not manifest:
        return ""
    if isinstance(manifest, dict):
        return "\n".join(f"- {p}" for p in manifest.keys())
    if isinstance(manifest, list):
        return "\n".join(f"- {item}" for item in manifest)
    return str(manifest)


def format_project_listing(params: dict, namespaces: dict) -> str:
    manifest = params.get("source") or {}
    if not manifest:
        return ""
    lines = []
    for filepath, sig in manifest.items():
        lines.append(f"- {filepath}")
        if sig:
            lines.append(f"  {str(sig)}")
    return "\n".join(lines)


def format_verified_behaviors(params: dict, namespaces: dict) -> str:
    """Render the verified-behaviors list from the quality_overview projection.

    Fed to the quality gate's summarize turn with a do-not-relitigate
    rule: behaviors with a verified passing play-test must not be
    reported as untested or broken just because the latest UX session
    didn't happen to re-tour them — without this, "untested: X" findings
    re-open completed goals on every gate pass (the harvester's
    fix-didn't-hold logic) and verified work ping-pongs forever.
    """
    overview = params.get("source") or {}
    behaviors = overview.get("verified_behaviors") if isinstance(overview, dict) else []
    if not behaviors:
        return ""
    lines = ["Behaviors with a VERIFIED passing play-test:"]
    for b in behaviors:
        lines.append(f"- {b}")
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
            # Surface the command so a judge can see WHY it failed — a boolean
            # check (e.g. [ "$(cmd)" = "3" ]) emits no output of its own.
            cmd = check.get("command", "")
            if isinstance(cmd, list):
                # Unwrap ["/bin/sh", "-c", X] → X for readability.
                cmd = (
                    cmd[-1]
                    if len(cmd) >= 3 and cmd[0] in ("/bin/sh", "sh", "bash")
                    else " ".join(str(p) for p in cmd)
                )
            if cmd:
                lines.append(f"  command: {cmd}")
            for key in ("stdout", "stderr"):
                val = check.get(key, "")
                if val:
                    lines.append(f"  {key}: {val}")
    return "\n".join(lines)


def format_session_history(params: dict, namespaces: dict) -> str:
    history = params.get("source") or []
    if not history:
        return "No commands have been run yet."
    lines = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        # Support both run_commands entries ('command') and interactive entries ('input')
        cmd = entry.get("command") or entry.get("input", "")
        lines.append(f"[Turn {entry.get('turn','?')}] $ {cmd}")
        if entry.get("output"):
            lines.append(entry["output"])
        if entry.get("return_code", 0) != 0:
            lines.append(f"(exit code: {entry['return_code']})")
        if entry.get("timed_out"):
            lines.append("⚠️ TIMED OUT")
        lines.append("")
    return "\n".join(lines)


def format_last_turn(params: dict, namespaces: dict) -> str:
    """Format the most recent turn with ---LAST TURN--- attention block.

    Creates a prominent block around the last interaction so the model
    can quickly orient to its current state in a multi-step session.
    """
    history = params.get("source") or []
    if not history:
        return ""
    last = history[-1] if isinstance(history, list) else history
    if not isinstance(last, dict):
        return ""

    action = last.get("action", "")
    cmd = last.get("command") or last.get("input", "")
    output = last.get("output", "")

    lines = ["---LAST TURN---"]
    if action == "shell_command":
        lines.append(f"[Turn {last.get('turn', '?')}] $ {cmd}")
    elif action == "send_input":
        lines.append(f"[Turn {last.get('turn', '?')}] > {cmd}")
    elif action == "read_output":
        lines.append(f"[Turn {last.get('turn', '?')}] (read_output)")
    else:
        lines.append(f"[Turn {last.get('turn', '?')}] ({action}) {cmd}")

    if output:
        lines.append(output)
    if last.get("return_code", 0) != 0:
        lines.append(f"(exit code: {last['return_code']})")
    lines.append("---END LAST TURN---")
    return "\n".join(lines)


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
            files = goal.get("associated_files", [])
        elif hasattr(goal, "status"):
            status, desc, gtype = goal.status, goal.description, goal.type
            files = getattr(goal, "associated_files", [])
        else:
            continue
        parts = [f"{i+1:2d}. [{status:11s}] ({gtype})"]
        parts.append(desc)
        if files:
            parts.append(f"  files: {', '.join(files)}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


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
            lines.append(f"Project files: {', '.join(sorted(file_list))}")

    return "\n".join(lines) if lines else ""


# ══════════════════════════════════════════════════════════════════════
# Persona formatters
# ══════════════════════════════════════════════════════════════════════


def format_aspect_definitions(params: dict, namespaces: dict) -> str:
    """Render the research plan's aspects for the tagging turn."""
    plan = params.get("source")
    aspects = getattr(plan, "aspects", None) or (
        plan.get("aspects") if isinstance(plan, dict) else None
    )
    if not aspects:
        return ""
    lines = ["Research aspects (tag papers against THESE names only):"]
    for a in aspects:
        name = getattr(a, "name", None) or (
            a.get("name") if isinstance(a, dict) else ""
        )
        desc = getattr(a, "description", None) or (
            a.get("description") if isinstance(a, dict) else ""
        )
        lines.append(f"- {name}: {desc or 'no description'}")
    return "\n".join(lines)


def format_catalog_batch(params: dict, namespaces: dict) -> str:
    """Render the catalog batch's papers (key, title, abstract) for tagging."""
    batch = params.get("source") or []
    if not batch:
        return ""
    blocks = []
    for rec in batch:
        if not isinstance(rec, dict):
            continue
        abstract = (rec.get("abstract") or "").strip() or "(no abstract — title only)"
        blocks.append(
            f"paper_key: {rec.get('paper_key', '?')}\n"
            f"title: {rec.get('title', '')}\n"
            f"abstract: {abstract[:1500]}"
        )
    return "\n\n".join(blocks)


def format_research_overview(params: dict, namespaces: dict) -> str:
    """Render the research_overview projection as a prompt block.

    Aspect coverage table + worklist/corpus counts for the scraper's
    planning re-entry and gate summary turns.
    """
    overview = params.get("source") or {}
    if not isinstance(overview, dict) or not overview.get("aspects"):
        return ""
    lines = ["Research corpus status:"]
    for a in overview["aspects"]:
        lines.append(
            f"- {a['name']}: {a['strong_tagged']} strongly-tagged "
            f"(target {a['target']}), {a['candidates']} candidate(s)"
        )
    wl = overview.get("worklist") or {}
    corpus = overview.get("corpus") or {}
    lines.append(
        f"Worklist: {wl.get('candidate', 0)} candidate, "
        f"{wl.get('cataloged', 0)} cataloged, {wl.get('needs_retag', 0)} retag"
    )
    lines.append(
        f"Corpus: {corpus.get('papers', 0)} papers, {corpus.get('pdfs', 0)} PDFs, "
        f"{corpus.get('closed', 0)} closed-access"
    )
    return "\n".join(lines)


# Lazy-loaded persona data from compiled.json
_persona_cache: dict[str, str] | None = None


PRE_COMPUTE_FORMATTERS: dict[str, Any] = {
    "format_plan_listing": format_plan_listing,
    "format_architecture_summary": format_architecture_summary,
    "format_architecture_listing": format_architecture_listing,
    "format_existing_architecture": format_existing_architecture,
    "format_completion_criteria": format_completion_criteria,
    "format_feedback_block": format_feedback_block,
    "format_session_tail": format_session_tail,
    "format_existing_goals": format_existing_goals,
    "format_mission_meta": format_mission_meta,
    "format_project_file_list": format_project_file_list,
    "format_project_listing": format_project_listing,
    "format_validation_results": format_validation_results,
    "format_verified_behaviors": format_verified_behaviors,
    "format_research_overview": format_research_overview,
    "format_aspect_definitions": format_aspect_definitions,
    "format_catalog_batch": format_catalog_batch,
    "format_session_history": format_session_history,
    "format_last_turn": format_last_turn,
    "format_run_context": format_run_context,
    "extract_field": extract_field,
    "format_goals_listing": format_goals_listing,
    # Structural context formatters (Phase B — patch redesign)
    "format_file_outline": lambda params, namespaces: _format_file_outline(
        params, namespaces
    ),
    "format_call_graph": lambda params, namespaces: _format_call_graph(
        params, namespaces
    ),
    # Multi-symbol patching (505 round): renders the other symbols
    # already rewritten in this batch so the current rewrite sees
    # what its co-dependents finalized to.
    "format_already_rewritten": lambda params, namespaces: _format_already_rewritten(
        params, namespaces
    ),
}


# ══════════════════════════════════════════════════════════════════════
# Structural context formatters (Phase B — patch redesign)
# ══════════════════════════════════════════════════════════════════════


def _extract_docstring(body: str) -> str:
    """Pull the first docstring from a function/method/class body.

    Looks for the first triple-quoted string literal that appears
    immediately after a ``def``/``class`` line. Returns only the
    first line of the docstring, trimmed. Empty if no docstring.
    """
    if not body:
        return ""
    lines = body.splitlines()
    # Skip past def/class signature lines (may continue across multiple
    # lines for long signatures). Find the first line after the ':'
    # terminator of the signature.
    sig_done = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not sig_done:
            if stripped.endswith(":") and (
                stripped.startswith("def ")
                or stripped.startswith("async def ")
                or stripped.startswith("class ")
                or (
                    i > 0
                    and (stripped == ":" or ") -> " in stripped or "):" in stripped)
                )
            ):
                sig_done = True
                continue
            if not (
                stripped.startswith(("def ", "async def ", "class ", "@"))
                or stripped == ""
                or stripped.startswith(("(", ")", ","))
                or ":" in stripped
            ):
                # First non-signature, non-decorator line without ':'
                # probably means the signature is simpler than expected.
                sig_done = True
            else:
                continue
        # After signature
        if not stripped:
            continue
        if stripped.startswith(('"""', "'''")):
            # Extract first line of docstring
            quote = stripped[:3]
            inner = stripped[3:]
            if quote in inner:  # Single-line docstring
                return inner.split(quote, 1)[0].strip()
            return inner.strip() or (lines[i + 1].strip() if i + 1 < len(lines) else "")
        # First statement is not a docstring
        return ""
    return ""


def _format_file_outline(params: dict, namespaces: dict) -> str:
    """Render a file's symbol outline with signatures and docstrings.

    Given a ``symbol_table`` as produced by ``extract_symbol_bodies``,
    returns a compact outline block suitable for prompt injection:

        class GameEngine:
          __init__(self, world: World)
            Initialize engine with world data
          process_command(self, command: Command) -> Optional[bool]
          _handle_move(self, command: Command) -> str
            Handle direction command; return room description

    Params:
      symbol_table: list of dicts with name, kind, signature, body, parent.
      target_file (optional): filename to title the outline block with.
      highlight_symbol (optional): qualified name of a symbol to mark
        with an arrow; used when the caller knows the rewrite target.

    Returns empty string if the symbol_table is empty or malformed —
    callers render this as an optional section.
    """
    symbol_table = params.get("symbol_table") or []
    target_file = params.get("target_file", "") or ""
    highlight = params.get("highlight_symbol", "") or ""

    if not isinstance(symbol_table, list) or not symbol_table:
        return ""

    # Group by parent — classes collect their methods; free functions
    # live under parent="" at the top level.
    by_parent: dict[str, list[dict]] = {}
    classes: dict[str, dict] = {}
    for sym in symbol_table:
        if not isinstance(sym, dict):
            continue
        parent = sym.get("parent", "") or ""
        if sym.get("kind") == "class":
            classes[sym.get("name", "")] = sym
            by_parent.setdefault(sym.get("name", ""), [])
        else:
            by_parent.setdefault(parent, []).append(sym)

    lines: list[str] = []
    if target_file:
        lines.append(f"## File outline — {target_file}")
    else:
        lines.append("## File outline")
    lines.append("")

    # Classes first, with their methods nested
    for cls_name in sorted(classes):
        cls_sig = (
            classes[cls_name].get("signature", f"class {cls_name}").strip().rstrip(":")
        )
        marker = "  ← target" if cls_name == highlight else ""
        lines.append(f"{cls_sig}:{marker}")
        ds = _extract_docstring(classes[cls_name].get("body", ""))
        if ds:
            lines.append(f"    {ds}")
        for method in by_parent.get(cls_name, []):
            name = method.get("name", "")
            sig = (method.get("signature", "") or "").strip().rstrip(":")
            mark = "  ← target" if name == highlight else ""
            lines.append(f"  {sig}{mark}")
            ds = _extract_docstring(method.get("body", ""))
            if ds:
                lines.append(f"      {ds}")
        lines.append("")

    # Module-level (non-class) functions
    top_level = by_parent.get("", [])
    if top_level:
        for fn in top_level:
            name = fn.get("name", "")
            sig = (fn.get("signature", "") or "").strip().rstrip(":")
            mark = "  ← target" if name == highlight else ""
            lines.append(f"{sig}{mark}")
            ds = _extract_docstring(fn.get("body", ""))
            if ds:
                lines.append(f"    {ds}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _format_call_graph(params: dict, namespaces: dict) -> str:
    """Render the direct call graph for a target symbol.

    Given project files + a target symbol name, returns:

        ## Call graph — GameEngine._handle_move

        Called from:
          engine.py:process_command
              if action == Action.MOVE:
                  return self._handle_move(command)

        Calls into:
          self.current_room.get_exit(direction)
          self.rooms.get(exit_obj.target_room_id)
          self._describe_current_room()

    Params:
      target_symbol: qualified name (e.g. "GameEngine._handle_move").
      target_file: file where the target lives (filters callees from
        references collected inside the target's own body).
      symbol_table: list of dicts with body fields — used to locate
        the target's body for callee extraction.
      project_files: dict of {file_path: content} for cross-file
        caller search. If absent, only the target file is searched.

    Returns empty string when target_symbol is absent or no call
    sites are found — callers render as optional section.
    """
    target_symbol = (params.get("target_symbol", "") or "").strip()
    target_file = params.get("target_file", "") or ""
    symbol_table = params.get("symbol_table") or []
    project_files = params.get("project_files") or {}

    if not target_symbol:
        return ""

    # Extract the bare method/function name from a qualified name —
    # cross-file references typically don't carry the class qualifier.
    bare_name = target_symbol.rsplit(".", 1)[-1]

    # ── Find the target's body for callee extraction ────────────
    target_body = ""
    target_line_range: tuple[int, int] | None = None
    for sym in symbol_table:
        if isinstance(sym, dict) and sym.get("name") == target_symbol:
            target_body = sym.get("body", "") or ""
            line = sym.get("line")
            end_line = sym.get("end_line")
            if isinstance(line, int) and isinstance(end_line, int):
                target_line_range = (line, end_line)
            break

    # ── Find callers across project files ───────────────────────
    callers: list[tuple[str, str, str]] = []  # (file, context_line, snippet)

    # Build a simple search across project files. We look for the bare
    # name as a word, prefer hits that look like function calls
    # (`<name>(` pattern). Skip the target file's own body to avoid
    # listing the definition itself as a caller.
    import re

    call_pattern = re.compile(r"\b" + re.escape(bare_name) + r"\s*\(")
    for fp, content in project_files.items():
        if not isinstance(content, str) or not content:
            continue
        for i, line in enumerate(content.splitlines(), start=1):
            if not call_pattern.search(line):
                continue
            # Skip the def line itself
            stripped = line.strip()
            if stripped.startswith(("def ", "async def ")):
                continue
            # Skip lines within the target's own body (self-reference)
            if fp == target_file and target_line_range is not None:
                if target_line_range[0] <= i <= target_line_range[1]:
                    continue
            # Look up which symbol this caller line falls in, if we have
            # a symbol_table for this file. Prefer the innermost (smallest
            # line range) match — methods sit inside classes, and we want
            # the method as the caller, not the enclosing class.
            context = f"{fp}:{i}"
            if fp == target_file and symbol_table:
                best_range = None
                for sym in symbol_table:
                    if not isinstance(sym, dict):
                        continue
                    ln = sym.get("line")
                    el = sym.get("end_line")
                    if isinstance(ln, int) and isinstance(el, int) and ln <= i <= el:
                        span = el - ln
                        if best_range is None or span < best_range[0]:
                            best_range = (span, sym.get("name", "?"))
                if best_range is not None:
                    context = f"{fp}:{best_range[1]}"
            callers.append((fp, context, stripped))

    # Deduplicate by (context, snippet) — same call line twice would be rare
    # but possible after refactors
    seen: set[tuple[str, str]] = set()
    uniq_callers: list[tuple[str, str, str]] = []
    for c in callers:
        key = (c[1], c[2])
        if key not in seen:
            seen.add(key)
            uniq_callers.append(c)
    # Limit: 10 call sites is plenty; more becomes noise
    uniq_callers = uniq_callers[:10]

    # ── Extract callees from target body ────────────────────────
    # Heuristic: look for `self.<name>(` and `<namespace>.<name>(` patterns.
    # Skip built-ins and dunders to keep signal-to-noise high.
    callees: list[str] = []
    seen_callees: set[str] = set()
    if target_body:
        # self.method(...) pattern
        self_method_pat = re.compile(r"self\.(\w+)\s*\(")
        # <name>.<method>(...) pattern, where <name> is not `self`
        attr_method_pat = re.compile(r"(\w+)\.(\w+)\s*\(")

        for line in target_body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(
                ("#", "def ", "async def ", "class ")
            ):
                continue
            for m in self_method_pat.finditer(line):
                snippet = f"self.{m.group(1)}(...)"
                if snippet not in seen_callees:
                    seen_callees.add(snippet)
                    callees.append(snippet)
            for m in attr_method_pat.finditer(line):
                ns, meth = m.group(1), m.group(2)
                if ns in ("self", "cls"):  # handled above
                    continue
                if meth.startswith("__") and meth.endswith("__"):  # dunders
                    continue
                snippet = f"{ns}.{meth}(...)"
                if snippet not in seen_callees:
                    seen_callees.add(snippet)
                    callees.append(snippet)

    # Keep the callee list focused — 15 is plenty
    callees = callees[:15]

    # ── Format the output block ─────────────────────────────────
    if not uniq_callers and not callees:
        return ""

    lines: list[str] = [f"## Call graph — {target_symbol}", ""]
    if uniq_callers:
        lines.append("Called from:")
        for _fp, context, snippet in uniq_callers:
            lines.append(f"  {context}")
            lines.append(f"    {snippet}")
        lines.append("")
    if callees:
        lines.append("Calls into:")
        for c in callees:
            lines.append(f"  {c}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ══════════════════════════════════════════════════════════════════════
# Multi-symbol patching (505 round) — already-rewritten context
# ══════════════════════════════════════════════════════════════════════


def _format_already_rewritten(params: dict, namespaces: dict) -> str:
    """Render the list of symbols already rewritten in this batch.

    When diagnose supplies ``related_symbols``, ``prepare_next_rewrite``
    seeds the queue with the primary target plus those related
    symbols. As ``rewrite_symbol_turn`` drains the queue, each
    successful rewrite appends its final body to
    ``context.already_rewritten`` under a file-qualified key
    ("path.py:Class.method" — rendered verbatim, so cross-file batches
    read naturally). This formatter reads that dict and renders it as a
    context block so subsequent rewrites in the same batch see what
    their co-dependents finalized to.

    Empty when no rewrites have landed yet (the first symbol in the
    queue sees nothing), or when diagnose didn't supply related
    symbols (single-symbol rewrites don't need this context).

    The turn template places this block BEFORE the current symbol so
    the model reads it as established context before authoring the
    next rewrite.
    """
    ctx = namespaces.get("context", {}) or {}
    already = ctx.get("already_rewritten") or {}
    if not already or not isinstance(already, dict):
        return ""

    lines = ["## Other symbols already changed in this batch", ""]
    lines.append(
        "These have been rewritten in this same edit session. "
        "Match the attribute names, method signatures, and return "
        "shapes they establish."
    )
    lines.append("")

    for qname, body in already.items():
        if not body:
            continue
        # Normalize: show just the signature plus the first few body
        # lines if the body is long. Full body is sometimes big (large
        # class), and the purpose here is contract visibility, not
        # redundant re-authoring.
        body_str = body.rstrip()
        body_lines = body_str.splitlines()
        if len(body_lines) <= 20:
            display = body_str
        else:
            # Show first 18 lines + tail marker
            display = "\n".join(body_lines[:18]) + "\n    # ... (truncated)"
        lines.append(f"### {qname}")
        lines.append("")
        lines.append("```python")
        lines.append(display)
        lines.append("```")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ══════════════════════════════════════════════════════════════════════
# Step C Batch E — patch flow formatters
# ══════════════════════════════════════════════════════════════════════


def select_rewrite_instruction_template(params: dict, namespaces: dict) -> str:
    """Site #10a: dynamic template selection by symbol kind.

    Reads the symbol's `kind` field and emits the ID of the
    appropriate instruction template. The turn's instruction section
    uses `template: {$ref: "context.kind_instruction_template"}`
    to resolve this at render time.

    Unknown kinds fall back to the function template — less wrong
    than a class template for anything else.
    """
    source = params.get("source")
    if isinstance(source, dict):
        kind = source.get("kind", "")
    else:
        kind = getattr(source, "kind", "")
    if kind == "class":
        return "patch/rewrite_class_instruction"
    return "patch/rewrite_function_instruction"


def format_selection_state(params: dict, namespaces: dict) -> str:
    """Site #11: running "Selected so far" list rendered as evidence.

    Reads `selected_symbols` from params and emits a short
    human-readable list. Returns empty string when nothing is
    selected yet — the evidence section's ref will then omit
    naturally.
    """
    selected = params.get("selected") or []
    if not selected:
        return ""
    names: list[str] = []
    for entry in selected:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("id") or ""
        else:
            name = str(entry)
        if name:
            names.append(name)
    if not names:
        return ""
    return ", ".join(names)


PRE_COMPUTE_FORMATTERS["select_rewrite_instruction_template"] = (
    select_rewrite_instruction_template
)
PRE_COMPUTE_FORMATTERS["format_selection_state"] = format_selection_state
