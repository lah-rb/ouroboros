"""Projection renderers — convert structured projection dicts to prompt-ready text.

Registered as pre_compute formatters. Each renderer takes a projection dict
(from a materializer) and produces a formatted string for prompt template
injection.

Signature: (params: dict, namespaces: dict) -> str
(Same as existing pre_compute formatter signature — renderers are just
formatters that consume projections instead of raw mission state.)

These replace the old formatters that reached into raw mission state
(format_architecture_summary, format_notes, etc.). They receive
pre-computed structured dicts and format them for human-readable
prompt consumption.
"""

from __future__ import annotations

import os
from typing import Any

# ══════════════════════════════════════════════════════════════════════
# Renderers
# ══════════════════════════════════════════════════════════════════════


def render_file_context(params: dict, namespaces: dict) -> str:
    """Render the file_context projection for code generation prompts.

    Produces a structured architecture specification that the code gen
    model can follow. Includes dependency content (full or symbol-level),
    data shape contracts, and interface contracts.

    Input: file_context projection dict (from project_file_context materializer).
    Output key: architecture_spec
    """
    ctx = params.get("source")
    if not ctx or not isinstance(ctx, dict):
        return ""

    target_file = ctx.get("target_file", "")
    if not target_file:
        return ""

    lines = [f"=== Architecture Specification for {target_file} ==="]

    # Module responsibility
    responsibility = ctx.get("responsibility", "")
    if responsibility:
        lines.append(f"\nResponsibility: {responsibility}")

    # Required exports
    defines = ctx.get("defines", [])
    if defines:
        lines.append(f"\nMust define (top-level exports): {', '.join(defines)}")

    # Import requirements
    imports_from = ctx.get("imports_from", {})
    if imports_from:
        lines.append("\nRequired imports:")
        for module, symbols in imports_from.items():
            # Strip file extensions — import statements use module names,
            # not file paths (e.g., "from models import X", not "from models.py import X")
            module_name = os.path.splitext(module)[0] if "." in module else module
            syms = ", ".join(symbols) if isinstance(symbols, list) else str(symbols)
            lines.append(f"  from {module_name} import {syms}")

    # Import dependencies — expanded specs of modules we depend on
    import_deps = ctx.get("import_deps", [])
    if import_deps:
        lines.append("\nDependency modules (available for import):")
        for dep in import_deps:
            dep_defines = ", ".join(dep.get("defines", []))
            lines.append(
                f"  {dep['file']}: {dep.get('responsibility', '')}"
                f"{f' — exports: {dep_defines}' if dep_defines else ''}"
            )
            # Field signatures — show actual constructor parameters
            field_sigs = dep.get("field_signatures", {})
            if field_sigs:
                for cls_name, sig in field_sigs.items():
                    lines.append(f"    {cls_name}({sig})")

            # Symbol bodies — included when we selectively extracted
            # the symbols our target imports (large file optimization)
            symbol_bodies = dep.get("symbol_bodies", {})
            if symbol_bodies:
                lines.append(f"\n  --- Imported symbols from {dep['file']} ---")
                for sym_name, body in symbol_bodies.items():
                    lines.append(f"  {body}")
                lines.append(f"  --- end {dep['file']} ---")
            elif dep.get("content"):
                # Full file content for small dependencies
                lines.append(f"\n  --- {dep['file']} (full content) ---")
                lines.append(f"  {dep['content']}")
                lines.append(f"  --- end {dep['file']} ---")

    # Reverse dependencies — who imports from us
    reverse_deps = ctx.get("reverse_deps", [])
    if reverse_deps:
        lines.append("\nModules that import from this file:")
        for rd in reverse_deps:
            needs = ", ".join(rd.get("needs", []))
            resp = rd.get("responsibility", "")
            resp_suffix = f" ({resp})" if resp else ""
            lines.append(f"  {rd['file']} needs: {needs}{resp_suffix}")

    # Interface contracts
    interfaces = ctx.get("interfaces", [])
    if interfaces:
        lines.append("\nInterface contracts:")
        for iface in interfaces:
            sig = iface.get("signature", "")
            symbol = iface.get("symbol", "")
            if sig and symbol and sig.startswith(symbol):
                sig = sig[len(symbol) :]
            if sig and not sig.startswith("("):
                sig = f"({sig})"
            lines.append(f"  {iface['caller']} → {iface['callee']}: " f"{symbol}{sig}")

    # Data shape contracts
    data_shapes = ctx.get("data_shapes", [])
    if data_shapes:
        lines.append("\nData format contracts:")
        for ds in data_shapes:
            lines.append(
                f"  {ds['file']} consumed by {ds['consumed_by']}: " f"{ds['structure']}"
            )

    # State contracts — canonical runtime/persisted representations
    state_shapes = ctx.get("state_shapes", [])
    if state_shapes:
        lines.append(
            "\nState contracts (canonical representations — all files conform):"
        )
        for ss in state_shapes:
            lines.append(
                f"  {ss['name']} (owner {ss['owner']}; consumers "
                f"{ss['consumed_by']}): {ss['structure']}"
            )

    # Data file contents — full content of related data files
    data_file_contents = ctx.get("data_file_contents", {})
    if data_file_contents:
        lines.append("\nData file contents:")
        for data_path, data_content in data_file_contents.items():
            lines.append(f"\n  --- {data_path} ---")
            lines.append(f"  {data_content}")
            lines.append(f"  --- end {data_path} ---")

    # Data flows — attribute access patterns for fix context
    data_flows = ctx.get("data_flows", [])
    if data_flows:
        lines.append("")
        lines.append(_render_data_flows_block(data_flows))

    # Project-level info
    import_scheme = ctx.get("import_scheme", "")
    run_command = ctx.get("run_command", "")
    if import_scheme:
        lines.append(f"\nImport scheme: {import_scheme}")
    if run_command:
        lines.append(f"Run command: {run_command}")

    # Relevant notes
    relevant_notes = ctx.get("relevant_notes", [])
    if relevant_notes:
        lines.append("\nRelevant notes:")
        for note in relevant_notes:
            lines.append(f"  {note}")

    return "\n".join(lines)


def render_director_overview(params: dict, namespaces: dict) -> str:
    """Render the director_overview projection for mission_control reasoning.

    Replaces the 8 separate pre_compute formatters that mission_control
    previously used. Produces a single comprehensive brief.

    Input: director_overview projection dict.
    Output key: director_brief
    """
    ctx = params.get("source")
    if not ctx or not isinstance(ctx, dict):
        return ""

    lines = []

    # Objective
    objective = ctx.get("objective", "")
    if objective:
        lines.append(f"Mission: {objective}")

    # Goals
    goals = ctx.get("goals", [])
    if goals:
        lines.append("\nGoals:")
        for i, g in enumerate(goals, 1):
            lines.append(
                f"  {i}. [{g['status']:11s}] ({g['type']}) "
                f"{g['description']}"
                f" — {g['file_count']} files, {g['report_count']} reports"
            )
            # Show recent reports for incomplete goals.
            # Each report has the LLM summary on the first line,
            # followed by raw evidence snippets so the director
            # can verify the summary against ground truth.
            for r in g.get("recent_reports", []):
                lines.append(f"       └ {r['flow']} ({r['status']}): {r['summary']}")
                # Raw evidence — indented under the summary
                if r.get("files_affected"):
                    lines.append(f"         files: {', '.join(r['files_affected'])}")
                if r.get("checks_failed"):
                    lines.append(f"         FAILED: {', '.join(r['checks_failed'])}")
                if r.get("checks_passed"):
                    lines.append(f"         passed: {', '.join(r['checks_passed'])}")
                if r.get("terminal_output"):
                    lines.append(f"         output: {r['terminal_output']}")
    else:
        lines.append("\nNo goals defined yet. Run design_and_plan to derive goals.")

    # Architecture brief
    arch_brief = ctx.get("architecture_brief", "")
    if arch_brief:
        lines.append(f"\nArchitecture: {arch_brief}")

    # Architecture modules
    arch_modules = ctx.get("architecture_modules", [])
    if arch_modules:
        lines.append("Modules:")
        for mod in arch_modules:
            lines.append(f"  {mod['file']}: {mod['responsibility']}")

    # Dispatch history
    dispatch_history = ctx.get("dispatch_history", [])
    if dispatch_history:
        lines.append("\nRecent dispatches:")
        for d in dispatch_history:
            lines.append(
                f"  {d['flow']}: {d.get('target', '')} — {d.get('status', '')}"
                f" (goal {d.get('goal_id', '')})"
            )

    # Recent notes
    recent_notes = ctx.get("recent_notes", [])
    if recent_notes:
        lines.append("\nRecent notes:")
        for note in recent_notes:
            lines.append(f"  [{note['category']}] {note['content']}")

    return "\n".join(lines)


def render_quality_overview(params: dict, namespaces: dict) -> str:
    """Render the quality_overview projection for quality gate prompts.

    Input: quality_overview projection dict.
    Output key: quality_brief
    """
    ctx = params.get("source")
    if not ctx or not isinstance(ctx, dict):
        return ""

    lines = []

    objective = ctx.get("objective", "")
    if objective:
        lines.append(f"Objective: {objective}")

    run_command = ctx.get("run_command", "")
    if run_command:
        lines.append(f"Run command: {run_command}")

    import_scheme = ctx.get("import_scheme", "")
    if import_scheme:
        lines.append(f"Import scheme: {import_scheme}")

    modules = ctx.get("modules", [])
    if modules:
        lines.append("\nArchitecture modules:")
        for mod in modules:
            defines = ", ".join(mod.get("defines", []))
            lines.append(f"  {mod['file']}: defines {defines}")

    interfaces = ctx.get("interfaces", [])
    if interfaces:
        lines.append("\nInterface contracts:")
        for iface in interfaces:
            sig = f"({iface['signature']})" if iface.get("signature") else ""
            lines.append(
                f"  {iface['caller']} → {iface['callee']}: " f"{iface['symbol']}{sig}"
            )

    data_shapes = ctx.get("data_shapes", [])
    if data_shapes:
        lines.append("\nData format contracts:")
        for ds in data_shapes:
            lines.append(
                f"  {ds['file']} consumed by {ds['consumed_by']}: " f"{ds['structure']}"
            )

    state_shapes = ctx.get("state_shapes", [])
    if state_shapes:
        lines.append("\nState contracts:")
        for ss in state_shapes:
            lines.append(
                f"  {ss['name']} (owner {ss['owner']}; consumers "
                f"{ss['consumed_by']}): {ss['structure']}"
            )

    creation_order = ctx.get("creation_order", [])
    if creation_order:
        lines.append(f"\nCreation order: {', '.join(creation_order)}")

    return "\n".join(lines)


def render_interaction_context(params: dict, namespaces: dict) -> str:
    """Render the interaction_context projection for interact prompts.

    Produces a structured brief that gives the persona planner everything
    needed to build a concrete test charter: launch command, command
    vocabulary, world layout (from data files), and recent issues.

    Input: interaction_context projection dict.
    Output key: interaction_brief
    """
    ctx = params.get("source")
    if not ctx or not isinstance(ctx, dict):
        return ""

    lines = []

    objective = ctx.get("objective", "")
    if objective:
        lines.append(f"Project: {objective}")

    working_dir = ctx.get("working_directory", "")
    if working_dir:
        lines.append(f"Working directory: {working_dir}")

    # Derive the interactive launch command from run_command.
    # run_command is the deterministic test command (e.g., "echo quit | python main.py").
    # Strip the pipe prefix to get the interactive launch command ("python main.py").
    run_command = ctx.get("run_command", "")
    if run_command:
        launch = run_command
        if "|" in launch:
            launch = launch.split("|", 1)[1].strip()
        lines.append(f"Launch command: {launch}")

    module_summary = ctx.get("module_summary", [])
    if module_summary:
        lines.append("\nModules:")
        for mod in module_summary:
            lines.append(f"  {mod['file']}: {mod['responsibility']}")

    # Command vocabulary — extracted from parser/command modules
    command_vocabulary = ctx.get("command_vocabulary", [])
    if command_vocabulary:
        lines.append(f"\nAvailable commands: {', '.join(command_vocabulary)}")

    # Data file contents — the "map of the territory"
    data_file_contents = ctx.get("data_file_contents", {})
    if data_file_contents:
        lines.append("\nWorld/data files:")
        for path, content in data_file_contents.items():
            lines.append(f"\n--- {path} ---")
            lines.append(content.rstrip())

    recent_issues = ctx.get("recent_issues", [])
    if recent_issues:
        lines.append("\nRecent issues:")
        for issue in recent_issues:
            lines.append(f"  - {issue}")

    return "\n".join(lines)


def render_project_setup_context(params: dict, namespaces: dict) -> str:
    """Render the project_setup_context projection for project_ops prompts.

    Input: project_setup_context projection dict.
    Output key: setup_brief
    """
    ctx = params.get("source")
    if not ctx or not isinstance(ctx, dict):
        return ""

    lines = []

    objective = ctx.get("objective", "")
    if objective:
        lines.append(f"Project: {objective}")

    run_command = ctx.get("run_command", "")
    if run_command:
        lines.append(f"Run command: {run_command}")

    import_scheme = ctx.get("import_scheme", "")
    if import_scheme:
        lines.append(f"Import scheme: {import_scheme}")

    init_files = ctx.get("init_files", False)
    if init_files:
        lines.append("Requires __init__.py files: yes")

    working_dir = ctx.get("working_directory", "")
    if working_dir:
        lines.append(f"Working directory: {working_dir}")

    module_files = ctx.get("module_files", [])
    if module_files:
        lines.append(f"\nModule files: {', '.join(module_files)}")

    data_files = ctx.get("data_files", [])
    if data_files:
        lines.append("\nData files:")
        for df in data_files:
            lines.append(f"  {df['file']}: {df['structure']}")

    # Durable-effects memory (workspace_ledger): what prior cycles already
    # installed/provisioned — plan on top of it, don't repeat it.
    ledger_block = ctx.get("workspace_ledger_block", "")
    if ledger_block:
        lines.append("\n" + ledger_block)

    return "\n".join(lines)


def render_dependency_excerpts(params: dict, namespaces: dict) -> str:
    """Render dependency file content from the file_context projection.

    Replaces format_file_excerpts which read from context_bundle.files
    (the old prepare_context pipeline). This renderer extracts content
    from the projection's import_deps, which contains architecture-guided
    dependency content — either full file content (small files) or
    imported symbol bodies (large files).

    Input: file_context projection dict.
    Output key: file_excerpts
    """
    ctx = params.get("source")
    if not ctx or not isinstance(ctx, dict):
        return ""

    lines = []
    import_deps = ctx.get("import_deps", [])
    for dep in import_deps:
        dep_file = dep.get("file", "")
        if not dep_file:
            continue

        # Prefer symbol_bodies (selective), fall back to content (full)
        symbol_bodies = dep.get("symbol_bodies", {})
        content = dep.get("content", "")

        if symbol_bodies:
            lines.append(f"──── {dep_file} (imported symbols) ────")
            for sym_name, body in symbol_bodies.items():
                lines.append(body)
            # The FULL signature listing for everything NOT included above —
            # `_build_import_deps` always attaches `symbols`, and dropping it
            # here left large deps showing imported bodies while every other
            # def in the file was invisible (a rewrite deliberated ~150k
            # tokens over whether UI.prompt took an argument; the signature
            # answers in one line — OPEN_TASKS §21).
            included = set(symbol_bodies)
            other_sigs = [
                f"  {sym.get('kind', '?')} {sym.get('signature', sym.get('name', '?'))}"
                for sym in dep.get("symbols", [])
                if sym.get("name") not in included
                # methods of an included class ride inside its body already
                and not any(
                    str(sym.get("name", "")).startswith(f"{b}.") for b in included
                )
            ]
            if other_sigs:
                lines.append(f"(all other definitions in {dep_file}:)")
                lines.extend(other_sigs)
            lines.append("")
        elif content:
            lines.append(f"──── {dep_file} ────")
            lines.append(content)
            lines.append("")

    # Also include data file contents if present
    data_file_contents = ctx.get("data_file_contents", {})
    for data_path, data_content in data_file_contents.items():
        lines.append(f"──── {data_path} ────")
        lines.append(data_content)
        lines.append("")

    return "\n".join(lines)


def render_data_contracts(params: dict, namespaces: dict) -> str:
    """Render data + state contracts as a mandatory conformance block.

    Produces a ---DATA CONTRACTS (MANDATORY)--- block that makes the
    architecture's data_shapes AND state_shapes contracts visually
    prominent and frames them as hard requirements. Pretty-prints JSON
    structures so key names are unmistakable.

    Used in create and rewrite prompts. The model MUST use the exact key
    names from the file contracts, and the exact canonical
    representations from the state contracts (e.g. if inventory is
    declared a list of item_id strings, never store Item objects).

    Input: file_context projection dict.
    Output key: data_contract_block
    """
    import json

    ctx = params.get("source")
    if not ctx or not isinstance(ctx, dict):
        return ""

    data_shapes = ctx.get("data_shapes", [])
    state_shapes = ctx.get("state_shapes", [])
    if not data_shapes and not state_shapes:
        return ""

    lines = [
        "---DATA CONTRACTS (MANDATORY)---",
        "Your code MUST use these exact key names and structure when reading",
        "or writing data files. Do not rename keys, restructure nesting, or",
        "add fields not listed here.",
        "",
    ]

    for ds in data_shapes:
        file_name = ds.get("file", "")
        consumed_by = ds.get("consumed_by", "")
        structure_raw = ds.get("structure", "")

        lines.append(f"File: {file_name}")
        lines.append(f"Consumer: {consumed_by}")

        # Pretty-print the structure JSON so key names are readable
        try:
            parsed = (
                json.loads(structure_raw)
                if isinstance(structure_raw, str)
                else structure_raw
            )
            pretty = json.dumps(parsed, indent=2)
            lines.append("Required structure:")
            lines.append(pretty)
        except (json.JSONDecodeError, TypeError):
            lines.append(f"Required structure: {structure_raw}")

        # The exemplar is the authoritative nested contract — the same
        # text the gate's shape checker diffs the real file against.
        # Without it here, the structure prose was the file authors'
        # ONLY view of the shape, and it's vague exactly where bugs
        # live (nested keys, list-vs-dict, optional fields). Code must
        # not require keys the exemplar doesn't declare; data files
        # must extend it without reshaping.
        example = (ds.get("example") or "").strip()
        if example:
            try:
                example = json.dumps(json.loads(example), indent=2)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            lines.append(
                "Exemplar (minimal valid instance — the file extends this "
                "shape; code requires ONLY what it declares):"
            )
            lines.append(example)

        lines.append("")

    if state_shapes:
        lines.append("State contracts — the canonical representation of shared")
        lines.append("and persisted state. Every module MUST conform; never use")
        lines.append("a different representation even if locally convenient:")
        lines.append("")
        for ss in state_shapes:
            lines.append(f"State: {ss.get('name', '')}")
            lines.append(
                f"Owner: {ss.get('owner', '')}  "
                f"Consumers: {ss.get('consumed_by', '')}"
            )
            lines.append(f"Canonical form: {ss.get('structure', '')}")
            lines.append("")

    lines.append("Use EXACTLY these key names in your code. If the contract says")
    lines.append('"options" with "text" inside, do not use "responses" or "choice".')
    lines.append("---END DATA CONTRACTS---")

    return "\n".join(lines)


# ── Data Flow Renderer ───────────────────────────────────────────────


def _render_data_flows_block(data_flows: list[dict]) -> str:
    """Render data flow traces as a readable block.

    Shared by render_diagnosis_context and render_file_context.
    Shows per-function attribute access patterns grouped by root object,
    cross-referenced against known class field types and parameter sources.
    """
    lines = ["Data value flows through functions:"]

    for flow in data_flows:
        qualified = flow.get("qualified_name", flow.get("function", "?"))
        params = flow.get("parameters", [])
        param_str = f"({', '.join(params)})" if params else "()"
        lines.append(f"\n  {qualified}{param_str}:")

        # Parameter sources — where do arguments come from?
        param_sources = flow.get("parameter_sources", [])
        if param_sources:
            for ps in param_sources:
                lines.append(
                    f"    param '{ps['param']}' ← {ps['from_caller']} via {ps['via']}"
                )

        # Attribute accesses grouped by root
        accesses_by_root = flow.get("accesses_by_root", {})
        for root, accesses in accesses_by_root.items():
            lines.append(f"    {root}:")
            for acc in accesses:
                lines.append(
                    f"      .{acc['attribute']}  (L{acc['line']}: {acc['chain']})"
                )

        # Known field types from dependencies
        known_fields = flow.get("known_field_types", {})
        if known_fields:
            lines.append("    known types:")
            for cls_name, sig in known_fields.items():
                lines.append(f"      {cls_name}({sig})")

    return "\n".join(lines)


# ── Batch Blueprint Renderer ─────────────────────────────────────────


def render_batch_blueprint(params: dict, namespaces: dict) -> str:
    """Render the COMPLETE architecture as a one-shot build brief.

    Input: mission.architecture (ArchitectureState or dict).
    Output key: batch_blueprint

    Unlike render_file_context (one file's neighborhood), this renders
    the whole blueprint — every module, every interface, every data and
    state contract WITH its exemplar — because the batch generation
    writes all files in shared context and must honor every contract at
    once. Exemplars are included verbatim: the shape checker later diffs
    the written data files against them.
    """
    import json

    arch = params.get("source")
    if not arch:
        return ""

    def _get(obj: Any, key: str, default: Any = "") -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    lines = ["---PROJECT BLUEPRINT---"]
    lines.append(f"Import scheme: {_get(arch, 'import_scheme', 'flat')}")
    lines.append(f"Run command: {_get(arch, 'run_command')}")
    smoke = _get(arch, "smoke_command")
    if smoke:
        lines.append(f"Smoke command: {smoke}")

    order = list(_get(arch, "creation_order", []) or [])
    modules = list(_get(arch, "modules", []) or [])
    by_file = {_get(m, "file"): m for m in modules}
    listed = [f for f in order if f] + [
        _get(m, "file") for m in modules if _get(m, "file") not in set(order)
    ]
    lines.append("")
    lines.append("Files to produce (every one, in this order):")
    for i, f in enumerate(listed, 1):
        mod = by_file.get(f)
        resp = _get(mod, "responsibility") if mod else ""
        lines.append(f"  {i}. {f}" + (f" — {resp}" if resp else ""))
        if mod:
            defines = _get(mod, "defines", []) or []
            if defines:
                lines.append(f"     Defines: {', '.join(defines)}")
            imports_from = _get(mod, "imports_from", {}) or {}
            if imports_from:
                pairs = ", ".join(
                    f"{src} ({', '.join(syms)})" if syms else src
                    for src, syms in imports_from.items()
                )
                lines.append(f"     Imports from: {pairs}")

    interfaces = list(_get(arch, "interfaces", []) or [])
    if interfaces:
        lines.append("")
        lines.append("Cross-module interfaces (honor these signatures exactly):")
        for iface in interfaces:
            sig = _get(iface, "signature")
            lines.append(
                f"  - {_get(iface, 'caller')} → {_get(iface, 'callee')}: "
                f"{_get(iface, 'symbol')}" + (f"({sig})" if sig else "")
            )

    data_shapes = list(_get(arch, "data_shapes", []) or [])
    if data_shapes:
        lines.append("")
        lines.append("---DATA CONTRACTS (MANDATORY)---")
        lines.append("Data files and their consumers MUST agree on these exact")
        lines.append("key names and nesting. The written data file is checked")
        lines.append("against the exemplar structurally.")
        lines.append("LOCATION IS PART OF THE CONTRACT: each file lives at")
        lines.append("EXACTLY the path shown, relative to the working directory")
        lines.append("the program runs from. Code opens it by exactly that")
        lines.append("path. NEVER prefix a directory the design didn't declare")
        lines.append("(no invented data/ or assets/) — code path references")
        lines.append("are checked against these declared paths.")
        for ds in data_shapes:
            lines.append("")
            lines.append(f"File: {_get(ds, 'file')}")
            consumer = _get(ds, "consumed_by")
            if consumer:
                lines.append(f"Consumer: {consumer}")
            structure = _get(ds, "structure")
            if structure:
                lines.append(f"Structure: {structure}")
            # The design phase's creative canon. Without this line the
            # batch model sees only a generic objective + nameless schema
            # and invents its own names — which the design-derived goals
            # then spend the whole run testing against (hy3: "Boss Nyx"
            # goals vs a "Shadow Lord" artifact).
            content_brief = (_get(ds, "content_brief") or "").strip()
            if content_brief:
                lines.append(
                    "Content (the design's canon — use these names, places, "
                    "and entities verbatim; invent freely only where the "
                    "brief is silent):"
                )
                lines.append(content_brief)
            example = (_get(ds, "example") or "").strip()
            if example:
                try:
                    example = json.dumps(json.loads(example), indent=2)
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
                lines.append(
                    "Exemplar (minimal valid instance — extend, don't deviate):"
                )
                lines.append(example)

    state_shapes = list(_get(arch, "state_shapes", []) or [])
    if state_shapes:
        lines.append("")
        lines.append("State contracts — canonical representations every module")
        lines.append("must use (never a locally convenient variant):")
        for ss in state_shapes:
            lines.append(
                f"  - {_get(ss, 'name')}: {_get(ss, 'structure')} "
                f"(owner: {_get(ss, 'owner')})"
            )

    notes = _get(arch, "notes")
    if notes:
        lines.append("")
        lines.append(f"Architecture notes: {notes}")
    lines.append("---END BLUEPRINT---")
    return "\n".join(lines)


def render_data_registry_brief(params: dict, namespaces: dict) -> str:
    """Render the input for the entity-id registry turn (round 5).

    Input: mission (params.source) — needs both architecture.data_shapes
    AND the goals (for each data file's content brief).
    Output key: data_registry_brief

    For every data file: its shape + exemplar (so ids fit the structure)
    AND the file's independently-written content brief (so the author can
    RECONCILE those briefs into one shared id namespace rather than let
    each file invent its own — the round-4 residual). Data-file goals only.
    """
    import json

    mission = params.get("source")
    if not mission:
        return ""

    def _get(obj: Any, key: str, default: Any = "") -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    arch = _get(mission, "architecture", None)
    data_shapes = list(_get(arch, "data_shapes", []) or []) if arch else []
    if not data_shapes:
        return ""

    # file -> content brief, from the matching goal's description (strip the
    # "Create <file> with content: " prefix and any appended marker blocks).
    briefs: dict[str, str] = {}
    for g in _get(mission, "goals", []) or []:
        files = _get(g, "associated_files", []) or []
        desc = str(_get(g, "description", "") or "")
        for f in files:
            brief = desc
            marker = brief.find("\n\n## ")
            if marker != -1:
                brief = brief[:marker]
            prefix = f"Create {f} with content:"
            if brief.startswith(prefix):
                brief = brief[len(prefix) :]
            briefs[f] = brief.strip()

    lines = ["---DATA FILES TO GIVE A SHARED ID NAMESPACE---"]
    for ds in data_shapes:
        f = _get(ds, "file")
        lines.append("")
        lines.append(f"File: {f}")
        consumer = _get(ds, "consumed_by")
        if consumer:
            lines.append(f"Consumer: {consumer}")
        structure = _get(ds, "structure")
        if structure:
            lines.append(f"Structure: {structure}")
        example = (_get(ds, "example") or "").strip()
        if example:
            try:
                example = json.dumps(json.loads(example), indent=2)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            lines.append("Exemplar (shape only — ids here are throwaway):")
            lines.append(example)
        brief = briefs.get(f, "")
        if brief:
            lines.append(f"Content brief: {brief}")
    lines.append("---END DATA FILES---")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# Registry
# ══════════════════════════════════════════════════════════════════════


def render_contract_digest(params: dict, namespaces: dict) -> str:
    """Render a contract_set back to reviewable stub text.

    Input: the parsed contract_set (params.source).
    Output key: contract_digest

    The reviewer sees exactly what the workers will implement — every
    module's stub text in order — plus any parse issues the deterministic
    gate let through on the exhausted-revisions path.
    """
    cs = params.get("source") or {}
    files = cs.get("files") or {}
    parts = []
    for path, entry in files.items():
        parts.append(f"# === FILE: {path} ===\n{entry.get('stub_text', '')}".rstrip())
    issues = cs.get("issues") or []
    if issues:
        parts.append(
            "## Known unresolved contract issues\n"
            + "\n".join(f"- {i.get('file')}: {i.get('problem')}" for i in issues)
        )
    return "\n\n".join(parts)


def render_assembled_package(params: dict, namespaces: dict) -> str:
    """Render the on-disk assembled package as one FILE-marker blob.

    Input: params.source = files_changed (list of relative/absolute paths);
    params.working_directory = mission working dir. Output key: assembled_package.

    The integrator (reconcile_integration) sees the WHOLE assembled artifact
    at once — every worker-produced file's real source, framed in the same
    `# === FILE: path ===` envelope it re-emits, so the reconciled response
    parses cleanly through slice_batch_files. Missing/unreadable files are
    skipped (a worker/splice failure leaves a file unwritten — serial
    fallback territory, not the integrator's concern).
    """
    files = params.get("source") or []
    if not isinstance(files, (list, tuple)) or not files:
        return ""
    workdir = params.get("working_directory") or ""

    parts = []
    for path in files:
        if not path or not isinstance(path, str):
            continue
        full = path if os.path.isabs(path) else os.path.join(workdir, path)
        try:
            with open(full, "r", encoding="utf-8") as fh:
                content = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        parts.append(f"# === FILE: {path} ===\n{content}".rstrip())
    return "\n\n".join(parts)


def render_drilldown_bodies(params: dict, namespaces: dict) -> str:
    """Render the drill-down picks (§21 scope-don't-truncate menu) as a
    titled block for the rewrite prompt. Empty when nothing was pulled."""
    bodies = params.get("source") or []
    if not isinstance(bodies, list) or not bodies:
        return ""
    lines = []
    for entry in bodies:
        if not isinstance(entry, dict) or not entry.get("body"):
            continue
        lines.append(f"──── {entry.get('ref', '?')} (requested) ────")
        lines.append(str(entry["body"]).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip()


RENDERER_REGISTRY: dict[str, Any] = {
    "render_file_context": render_file_context,
    "render_dependency_excerpts": render_dependency_excerpts,
    "render_drilldown_bodies": render_drilldown_bodies,
    "render_data_contracts": render_data_contracts,
    "render_director_overview": render_director_overview,
    "render_quality_overview": render_quality_overview,
    "render_interaction_context": render_interaction_context,
    "render_project_setup_context": render_project_setup_context,
    "render_batch_blueprint": render_batch_blueprint,
    "render_data_registry_brief": render_data_registry_brief,
    "render_contract_digest": render_contract_digest,
    "render_assembled_package": render_assembled_package,
}
