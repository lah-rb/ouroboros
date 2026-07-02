"""Projection materializers — compute typed views from MissionState.

Each materializer is registered by name (matching CUE's materializer field).
Signature: (mission: MissionState, params: dict) -> dict

The runtime calls materializers in loop.py before flow execution begins,
injecting results as flow inputs. Materializers are read-only operations —
they never mutate MissionState, never call inference, and never write files.
They may read files from disk (loading file content, extracting symbol
tables via AST) to enrich the projection with current-on-disk state.

This is the read side of CQRS: the write path (actions mutating mission
via effects.save_mission) and the read path (materializers computing
projections) are completely separate code.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from agent.persistence.models import MissionState

logger = logging.getLogger(__name__)

# ── Registry ─────────────────────────────────────────────────────────

# Projections return a dict for most shapes. Menu-option projections
# (Pattern C, see flows/shared/state.cue) return a list of option dicts
# because that's the natural shape the turn renderer consumes. Either
# shape is valid; consumers check at the call site.
ProjectionValue = dict | list
MaterializerFn = Callable[[MissionState, dict], ProjectionValue]
_registry: dict[str, MaterializerFn] = {}


def register(name: str):
    """Decorator to register a materializer by name."""

    def wrapper(fn: MaterializerFn) -> MaterializerFn:
        _registry[name] = fn
        return fn

    return wrapper


def materialize(name: str, mission: MissionState, params: dict) -> ProjectionValue:
    """Materialize a projection by name.

    Args:
        name: Materializer name (from CUE projection schema).
        mission: Current mission state (read-only).
        params: Resolved parameters from the projection declaration.

    Returns:
        A dict or list conforming to the projection schema documented
        in state.cue. Most projections are dicts; menu-option projections
        are lists.

    Raises:
        ValueError: If the materializer name is not registered.
    """
    if name not in _registry:
        raise ValueError(
            f"Unknown projection materializer: {name!r}. "
            f"Registered: {list(_registry.keys())}"
        )
    return _registry[name](mission, params)


def registered_materializers() -> list[str]:
    """Return list of registered materializer names."""
    return list(_registry.keys())


MATERIALIZER_REGISTRY = _registry  # for loader discovery


# ══════════════════════════════════════════════════════════════════════
# Materializer Implementations
# ══════════════════════════════════════════════════════════════════════


@register("project_file_context")
def project_file_context(mission: MissionState, params: dict) -> dict:
    """Per-file architecture context for code generation and diagnosis flows.

    Centered on one target file with architecture-guided context:
      - Target file: full content + symbol table
      - Import dependencies: responsibility + imported symbol bodies
      - Data files: full content when data_shapes link to target
      - Reverse deps: responsibility + what they need from us
      - Contracts: interfaces and data_shapes

    When target_file is empty, returns project-wide architecture context
    suitable for diagnosis without a specific file target.

    Schema shape: see state.cue file_context documentation.
    """
    target_file = params.get("target_file", "")
    arch = mission.architecture
    working_dir = mission.config.working_directory

    # ── No architecture: return empty shell ──────────────────────
    if not arch:
        return _empty_file_context(target_file)

    # ── No target file: return project-wide context ─────────────
    if not target_file:
        return _project_wide_context(mission, arch, working_dir)

    # ── Target file specified: build focused context ────────────
    target_mod = None
    for mod in arch.modules:
        if mod.file == target_file:
            target_mod = mod
            break

    # Also check if target is a data file
    target_data_shape = None
    for ds in arch.data_shapes:
        if ds.file == target_file:
            target_data_shape = ds
            break

    # Load target file content — always full, never truncated
    target_content = _load_file_content(working_dir, target_file, max_chars=0)

    # Extract symbol table for target file (Python files only)
    target_symbols = _extract_symbol_table(target_file, target_content)

    if not target_mod and not target_data_shape:
        # File not in architecture at all — return what we can
        return {
            "target_file": target_file,
            "target_content": target_content,
            "target_symbols": target_symbols,
            "responsibility": "",
            "defines": [],
            "imports_from": {},
            "import_deps": [],
            "reverse_deps": [],
            "interfaces": [
                _interface_to_dict(iface)
                for iface in arch.interfaces
                if iface.caller == target_file or iface.callee == target_file
            ],
            "data_shapes": [
                _data_shape_to_dict(ds)
                for ds in arch.data_shapes
                if _data_shape_relevant(ds, target_file, arch.modules)
            ],
            "state_shapes": _state_shapes_to_dicts(arch),
            "data_file_contents": _load_related_data_files(
                arch, working_dir, target_file
            ),
            "data_flows": _trace_data_flows(
                target_file,
                target_content,
                [],
                [],
            ),
            "import_scheme": arch.import_scheme,
            "run_command": arch.run_command,
            "relevant_notes": _filter_notes_for_file(mission, target_file),
        }

    # ── Build context for a known module ────────────────────────
    responsibility = ""
    defines: list[str] = []
    imports_from: dict[str, list[str]] = {}

    if target_mod:
        responsibility = target_mod.responsibility
        defines = list(target_mod.defines)
        imports_from = {k: list(v) for k, v in target_mod.imports_from.items()}

    if target_data_shape and not responsibility:
        # Data file — synthesize responsibility from the contract
        responsibility = f"Data file consumed by {target_data_shape.consumed_by}"

    # Import dependencies — smart content selection
    import_deps = _build_import_deps(arch, working_dir, target_mod)

    # Reverse dependencies — responsibility and needs only
    reverse_deps = _build_reverse_deps(arch, target_file, working_dir)

    # Interface and data shape contracts
    interfaces = [
        _interface_to_dict(iface)
        for iface in arch.interfaces
        if iface.caller == target_file or iface.callee == target_file
    ]

    data_shapes = [
        _data_shape_to_dict(ds)
        for ds in arch.data_shapes
        if _data_shape_relevant(ds, target_file, arch.modules)
    ]

    # Load related data file contents
    data_file_contents = _load_related_data_files(arch, working_dir, target_file)

    # Trace data flows through functions in the target file
    data_flows = _trace_data_flows(
        target_file,
        target_content,
        import_deps,
        interfaces,
    )

    # 32f round — project-wide module symbol tables for the (α)
    # gate's accuracy. Pre-fix, project_symbols at gate time only
    # covered the focal file (target_symbols) and import_deps
    # (imported_symbols + the round-prior dep symbols). When the
    # model's diagnosis named a symbol in a file that's neither
    # focal nor a direct import dep — frequently happens when
    # diagnose ranges across the project — the gate had no way to
    # see it and bounced. With the gate softened to advisory the
    # cost of false positives is gone, but the warning's signal
    # value depends on the project_symbols map being complete.
    # This widens it to cover every module in the architecture.
    project_modules = _build_project_modules(arch, working_dir)

    return {
        "target_file": target_file,
        "target_content": target_content,
        "target_symbols": target_symbols,
        "responsibility": responsibility,
        "defines": defines,
        "imports_from": imports_from,
        "import_deps": import_deps,
        "reverse_deps": reverse_deps,
        "interfaces": interfaces,
        "data_shapes": data_shapes,
        "state_shapes": _state_shapes_to_dicts(arch),
        "data_file_contents": data_file_contents,
        "data_flows": data_flows,
        "project_modules": project_modules,
        "import_scheme": arch.import_scheme,
        "run_command": arch.run_command,
        "relevant_notes": _filter_notes_for_file(mission, target_file),
    }


@register("project_director_overview")
def project_director_overview(mission: MissionState, params: dict) -> dict:
    """Goal-level mission overview for director reasoning.

    Replaces the 8-formatter pre_compute chain in mission_control's
    reason step. Everything the director needs to reason about
    which capability to advance next.

    Schema shape: see state.cue director_overview documentation.
    """
    arch = mission.architecture

    # Goals with recent reports
    goals = []
    for goal in mission.goals:
        file_count = len(goal.associated_files)
        report_count = len(goal.reports)

        # Include last 3 reports for incomplete goals, none for complete.
        # Each report carries the LLM summary alongside raw evidence
        # snippets so the director can reason from grounded facts
        # rather than relying solely on the summarizer's interpretation.
        recent_reports = []
        if goal.status == "incomplete" and goal.reports:
            for r in goal.reports[-3:]:
                entry = {
                    "flow": r.flow,
                    "status": r.status,
                    "summary": r.summary,
                }
                # Raw evidence fields — included when non-empty
                if r.files_affected:
                    entry["files_affected"] = r.files_affected
                if r.checks_failed:
                    entry["checks_failed"] = r.checks_failed
                if r.checks_passed:
                    entry["checks_passed"] = r.checks_passed
                if r.terminal_output:
                    # Truncate to keep prompt budget reasonable.
                    # Last 600 chars captures the traceback/error
                    # that matters most for diagnosis.
                    snippet = r.terminal_output.strip()
                    if len(snippet) > 600:
                        snippet = "…" + snippet[-600:]
                    entry["terminal_output"] = snippet
                recent_reports.append(entry)

        goals.append(
            {
                "id": goal.id,
                "description": goal.description,
                "type": goal.type,
                "status": goal.status,
                "file_count": file_count,
                "report_count": report_count,
                "recent_reports": recent_reports,
            }
        )

    # Architecture brief — one-line summary for director
    architecture_brief = ""
    architecture_modules = []
    if arch:
        module_files = arch.canonical_files()
        architecture_brief = (
            f"Import scheme: {arch.import_scheme}. "
            f"Run command: {arch.run_command}. "
            f"Modules: {', '.join(module_files)}."
        )
        architecture_modules = [
            {"file": mod.file, "responsibility": mod.responsibility}
            for mod in arch.modules
        ]

    # Recent dispatch history (last 5)
    dispatch_history = []
    for entry in mission.dispatch_history[-5:]:
        dispatch_history.append(
            {
                "flow": entry.flow,
                "target": entry.target_file_path,
                "goal_id": entry.goal_id,
                "status": entry.result_status,
            }
        )

    # Recent notes (last 8). The slice is load-bearing: without it every
    # note ever pushed enters the director prompt every cycle, forever
    # (memory audit: unbounded per-cycle prompt bloat on 24/7 missions).
    recent_notes = []
    sorted_notes = sorted(mission.notes, key=lambda n: n.timestamp, reverse=True)
    for note in sorted_notes[:8]:
        recent_notes.append(
            {
                "category": note.category,
                "content": note.content,
            }
        )

    return {
        "objective": mission.objective,
        "goals": goals,
        "architecture_brief": architecture_brief,
        "architecture_modules": architecture_modules,
        "dispatch_history": dispatch_history,
        "recent_notes": recent_notes,
    }


@register("project_quality_overview")
def project_quality_overview(mission: MissionState, params: dict) -> dict:
    """Cross-file structural data for quality gate validation.

    Focused on architectural invariants — not task status.
    The quality gate uses this to verify structural integrity
    of the project as a whole.

    Schema shape: see state.cue quality_overview documentation.
    """
    arch = mission.architecture

    # Behaviors with a verified passing play-test. The gate's summarize
    # turn receives these with a do-not-relitigate rule: without it, a
    # UX session that happens not to re-tour a verified feature causes
    # an "untested: X" finding whose signature RE-OPENS the completed
    # goal (the harvester's fix-didn't-hold logic) — verified work would
    # ping-pong on every gate pass that doesn't re-exercise everything.
    verified_behaviors = [
        g.description
        for g in mission.goals
        if g.type == "functional" and g.status == "complete"
    ]

    if not arch:
        return {
            "modules": [],
            "interfaces": [],
            "data_shapes": [],
            "state_shapes": [],
            "verified_behaviors": verified_behaviors,
            "run_command": "",
            "objective": mission.objective,
            "creation_order": [],
            "import_scheme": "",
        }

    return {
        "modules": [
            {"file": mod.file, "defines": list(mod.defines)} for mod in arch.modules
        ],
        "interfaces": [_interface_to_dict(iface) for iface in arch.interfaces],
        "data_shapes": [_data_shape_to_dict(ds) for ds in arch.data_shapes],
        "state_shapes": _state_shapes_to_dicts(arch),
        "verified_behaviors": verified_behaviors,
        "run_command": arch.run_command,
        "objective": mission.objective,
        "creation_order": list(arch.creation_order),
        "import_scheme": arch.import_scheme,
    }


@register("project_interaction_context")
def project_interaction_context(mission: MissionState, params: dict) -> dict:
    """Context for the interact flow.

    Provides everything a beta tester needs to systematically verify
    the project: how to launch, what commands exist, what the world
    looks like, and what recent issues have been found.

    Loads data file contents (YAML, JSON) from disk so the persona
    planner can build concrete test steps with real entity names,
    locations, and expected values.

    Schema shape: see state.cue interaction_context documentation.
    """
    arch = mission.architecture

    module_summary = []
    run_command = ""
    data_file_contents: dict[str, str] = {}
    command_vocabulary: list[str] = []
    if arch:
        run_command = arch.run_command
        module_summary = [
            {"file": mod.file, "responsibility": mod.responsibility}
            for mod in arch.modules
        ]

        working_dir = mission.config.working_directory

        # Load data file contents — these are typically small (YAML, JSON)
        # and give the tester the "map of the territory": rooms, items, NPCs,
        # config values, save format, etc.
        for ds in arch.data_shapes:
            if ds.file:
                content = _load_file_content(working_dir, ds.file, max_chars=4000)
                if content:
                    data_file_contents[ds.file] = content

        # Extract command vocabulary from parser/command modules.
        # Look for modules whose responsibility mentions "parse" or "command"
        # and extract their top-level function/class names as hints.
        for mod in arch.modules:
            resp_lower = (mod.responsibility or "").lower()
            if any(kw in resp_lower for kw in ["parse", "command", "dispatch"]):
                content = _load_file_content(working_dir, mod.file, max_chars=3000)
                if content:
                    # Extract string literals that look like commands
                    import re

                    # Match dict keys and string comparisons — common patterns:
                    #   "go": handler,  /  verb == "take"  /  elif cmd == "look"
                    found = set(
                        re.findall(
                            r"""['\"]([a-z][a-z_ ]{1,20})['\"]""",
                            content,
                        )
                    )
                    # Filter to likely command words
                    commands = sorted(
                        w
                        for w in found
                        if len(w) < 15
                        and w
                        not in {
                            "true",
                            "false",
                            "none",
                            "self",
                            "args",
                            "str",
                            "int",
                            "list",
                            "dict",
                            "type",
                            "return",
                            "class",
                            "import",
                            "from",
                        }
                    )
                    command_vocabulary = commands

    # Recent issues — from failed goal reports and failure_analysis notes
    recent_issues = []
    for goal in mission.goals:
        for report in goal.reports:
            if report.status == "failed" and report.summary:
                recent_issues.append(report.summary)
    for note in mission.notes:
        if note.category == "failure_analysis":
            recent_issues.append(note.content)

    # Cap at 5 most recent
    recent_issues = recent_issues[-5:]

    return {
        "objective": mission.objective,
        "run_command": run_command,
        "module_summary": module_summary,
        "recent_issues": recent_issues,
        "working_directory": mission.config.working_directory,
        "data_file_contents": data_file_contents,
        "command_vocabulary": command_vocabulary,
    }


@register("project_setup_context")
def project_setup_context(mission: MissionState, params: dict) -> dict:
    """Context for project_ops — infrastructure setup.

    What infrastructure exists, what the architecture expects.

    Schema shape: see state.cue project_setup_context documentation.
    """
    arch = mission.architecture

    if not arch:
        return {
            "objective": mission.objective,
            "run_command": "",
            "import_scheme": "",
            "init_files": False,
            "module_files": [],
            "data_files": [],
            "working_directory": mission.config.working_directory,
        }

    data_files = [
        {"file": ds.file, "structure": ds.structure}
        for ds in arch.data_shapes
        if ds.file
    ]

    return {
        "objective": mission.objective,
        "run_command": arch.run_command,
        "import_scheme": arch.import_scheme,
        "init_files": arch.init_files,
        "module_files": arch.canonical_files(),
        "data_files": data_files,
        "working_directory": mission.config.working_directory,
    }


# ══════════════════════════════════════════════════════════════════════
# Helpers (private)
# ══════════════════════════════════════════════════════════════════════


def _empty_file_context(target_file: str) -> dict:
    """Return an empty file_context shell when no architecture exists."""
    return {
        "target_file": target_file,
        "target_content": "",
        "target_symbols": [],
        "responsibility": "",
        "defines": [],
        "imports_from": {},
        "import_deps": [],
        "reverse_deps": [],
        "interfaces": [],
        "data_shapes": [],
        "state_shapes": [],
        "data_file_contents": {},
        "data_flows": [],
        "import_scheme": "",
        "run_command": "",
        "relevant_notes": [],
    }


def _project_wide_context(
    mission: MissionState,
    arch: Any,
    working_dir: str,
) -> dict:
    """Build project-wide architecture context when no target file is specified.

    Used by diagnose_issue when dispatched from the functional sweep with
    no traceback — the model needs a bird's-eye view to reason about where
    the problem might originate.

    Includes data flow traces for ALL code modules so the model can see
    attribute access patterns across the entire project.  This is critical
    for diagnosing cross-module data lifecycle issues (e.g., an engine
    comparing user input against internal IDs rather than display names).
    """
    # All module responsibilities
    all_modules = []
    for mod in arch.modules:
        all_modules.append(
            {
                "file": mod.file,
                "responsibility": mod.responsibility,
                "defines": list(mod.defines),
            }
        )

    # All data shapes with full content
    data_file_contents: dict[str, str] = {}
    all_data_shapes = []
    for ds in arch.data_shapes:
        all_data_shapes.append(_data_shape_to_dict(ds))
        if ds.file:
            content = _load_file_content(working_dir, ds.file, max_chars=0)
            if content:
                data_file_contents[ds.file] = content

    # All interfaces
    all_interfaces = [_interface_to_dict(iface) for iface in arch.interfaces]

    # Data flow traces for all code modules — gives the model
    # structural visibility into how each module accesses data
    all_data_flows: list[dict] = []
    for mod in arch.modules:
        mod_content = _load_file_content(working_dir, mod.file, max_chars=0)
        if mod_content:
            # Build import deps for this module for cross-referencing
            mod_import_deps = _build_import_deps(arch, working_dir, mod)
            mod_interfaces = [
                _interface_to_dict(iface)
                for iface in arch.interfaces
                if iface.caller == mod.file or iface.callee == mod.file
            ]
            mod_flows = _trace_data_flows(
                mod.file,
                mod_content,
                mod_import_deps,
                mod_interfaces,
            )
            all_data_flows.extend(mod_flows)

    # Recent failure notes
    relevant_notes = []
    failure_categories = {
        "failure_analysis",
        "codebase_observation",
        "approach_rejected",
    }
    for note in sorted(mission.notes, key=lambda n: n.timestamp, reverse=True):
        if note.category in failure_categories:
            relevant_notes.append(f"[{note.category}] {note.content}")
        if len(relevant_notes) >= 8:
            break

    return {
        "target_file": "",
        "target_content": "",
        "target_symbols": [],
        "responsibility": "",
        "defines": [],
        "imports_from": {},
        "import_deps": all_modules,  # reuse shape: list of module dicts
        "reverse_deps": [],
        "interfaces": all_interfaces,
        "data_shapes": all_data_shapes,
        "state_shapes": _state_shapes_to_dicts(arch),
        "data_file_contents": data_file_contents,
        "data_flows": all_data_flows,
        "import_scheme": arch.import_scheme,
        "run_command": arch.run_command,
        "relevant_notes": relevant_notes,
    }


def _build_project_modules(
    arch: Any,
    working_dir: str,
) -> list[dict]:
    """Build a list of {file, symbols} for every module in the architecture.

    Used by the (α) gate to widen project_symbols beyond the focal
    file + import_deps. Each module's symbols come from the same
    AST-based ``_extract_symbol_table`` that target_symbols uses,
    so private methods and architecture-untracked exports all show
    up. Files that fail to load or parse are skipped silently
    (caller treats absence as "no info" rather than failure).

    The result is intentionally minimal — file path and symbol
    table only. Other module metadata (responsibility, defines,
    interfaces) is already surfaced through the focal/import_deps
    paths or arch-wide projections; this helper exists specifically
    to give the gate a complete name-resolution map.
    """
    modules: list[dict] = []
    for mod in arch.modules:
        content = _load_file_content(working_dir, mod.file, max_chars=0)
        if not content:
            continue
        syms = _extract_symbol_table(mod.file, content)
        if syms:
            modules.append({"file": mod.file, "symbols": syms})
    return modules


def _build_import_deps(
    arch: Any,
    working_dir: str,
    target_mod: Any | None,
) -> list[dict]:
    """Build import dependency list with smart content selection.

    Strategy: include the specific symbols we import, not the whole file.
    For small files (< 200 lines), include full content.
    Falls back to full content if tree-sitter is unavailable.
    """
    if not target_mod:
        return []

    import_deps = []
    for dep_module_name, imported_symbols in target_mod.imports_from.items():
        dep_mod = _find_module_by_name(arch.modules, dep_module_name)
        if not dep_mod:
            continue

        dep_entry: dict[str, Any] = {
            "file": dep_mod.file,
            "responsibility": dep_mod.responsibility,
            "defines": list(dep_mod.defines),
        }

        # Extract field signatures for constructor awareness
        signatures = _extract_field_signatures(working_dir, dep_mod.file)
        if signatures:
            dep_entry["field_signatures"] = signatures

        # Smart content: load full content, then decide what to include
        dep_content = _load_file_content(working_dir, dep_mod.file, max_chars=0)
        if dep_content:
            # Symbol table is always included when content loads,
            # regardless of which content-trimming branch we take
            # below. It surfaces private methods of dep files (e.g.
            # `_describe_current_room`, `_build_rooms`) to the
            # diagnosis evidence — architecture interfaces and
            # imports_from only enumerate the public/imported
            # surface. Lightweight (names + signatures, no bodies),
            # so it's fine to attach uniformly.
            dep_symbols = _extract_symbol_table(dep_mod.file, dep_content)
            if dep_symbols:
                dep_entry["symbols"] = dep_symbols

            line_count = dep_content.count("\n") + 1
            if line_count <= 200:
                # Small file — include fully
                dep_entry["content"] = dep_content
            else:
                # Large file — extract only the imported symbols' bodies
                symbol_bodies = _extract_imported_symbol_bodies(
                    dep_mod.file, dep_content, list(imported_symbols)
                )
                if symbol_bodies:
                    dep_entry["symbol_bodies"] = symbol_bodies
                else:
                    # Fallback: include full content if extraction failed
                    dep_entry["content"] = dep_content

        import_deps.append(dep_entry)

    return import_deps


def _build_reverse_deps(
    arch: Any, target_file: str, working_dir: str = ""
) -> list[dict]:
    """Build reverse dependency list — who imports from us.

    Includes responsibility so the model understands why they depend on us.

    When ``working_dir`` is supplied, also scans each reverse-dep's
    file content for call sites mentioning the imported symbols and
    attaches a ``call_sites`` list of ``{line, snippet}`` dicts — up
    to 5 per dep. This gives diagnose visibility into where each
    importer actually invokes the target, which is the signal
    consumers need when a data-flow contract mismatch is suspected
    (the call site is the boundary where shape expectations meet).
    Gracefully handles missing/unreadable files.
    """
    target_module_name = _file_to_module_name(target_file)
    reverse_deps = []
    for mod in arch.modules:
        if mod.file == target_file:
            continue
        for imp_module_name, imp_symbols in mod.imports_from.items():
            if imp_module_name == target_module_name or imp_module_name == target_file:
                dep = {
                    "file": mod.file,
                    "responsibility": mod.responsibility,
                    "needs": list(imp_symbols),
                }
                # f3d round — attach call sites so diagnose can see
                # WHERE each dep invokes us, not just THAT it does.
                # The QA tester's CoT in f3d correctly identified
                # the loader→models contract mismatch but diagnose
                # didn't target the producer side because it had no
                # upstream trace. This gives diagnose enough to do
                # the one-hop upward walk the system prompt now
                # asks for.
                if working_dir:
                    dep_content = _load_file_content(working_dir, mod.file, max_chars=0)
                    if dep_content:
                        dep["call_sites"] = _extract_call_sites(
                            dep_content, list(imp_symbols)
                        )
                reverse_deps.append(dep)
    return reverse_deps


def _extract_call_sites(
    content: str, needed_symbols: list[str], max_per_symbol: int = 3
) -> list[dict]:
    """Find call sites for each needed symbol in the content.

    Returns a list of ``{symbol, line, snippet}`` dicts capped at
    ``max_per_symbol`` hits per symbol. Matches ``symbol(`` patterns
    only (not bare name references), so imports themselves aren't
    flagged. Line numbers are 1-based.
    """
    import re as _re

    results: list[dict] = []
    if not content or not needed_symbols:
        return results

    # Track hits per symbol to cap output
    hits: dict[str, int] = {s: 0 for s in needed_symbols}
    for i, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        # Skip import lines themselves
        if stripped.startswith(("import ", "from ")):
            continue
        for sym in needed_symbols:
            if hits.get(sym, 0) >= max_per_symbol:
                continue
            # Match `symbol(` or `.symbol(` as a call
            pat = r"(?<![a-zA-Z_])" + _re.escape(sym) + r"\s*\("
            if _re.search(pat, line):
                results.append(
                    {
                        "symbol": sym,
                        "line": i,
                        "snippet": stripped[:200],
                    }
                )
                hits[sym] = hits.get(sym, 0) + 1
    return results


def _load_related_data_files(
    arch: Any,
    working_dir: str,
    target_file: str,
) -> dict[str, str]:
    """Load full content of data files related to the target.

    Two modes:
      1. Target is a code file → load data files it consumes (via data_shapes).
      2. Target is a data file → load ALL other data files from data_shapes.
         Data files share ID namespaces (e.g., saved_state.json references
         room IDs defined in world_data.yaml). Including all data files lets
         the model cross-reference actual values rather than guessing IDs.

    Data files are typically small (YAML configs, JSON templates) — always
    included fully, never truncated.
    """
    data_file_contents: dict[str, str] = {}

    # Collect all known data file paths from data_shapes
    all_data_files = {ds.file for ds in arch.data_shapes if ds.file}

    # Is the target itself a data file?
    target_is_data = target_file in all_data_files

    for ds in arch.data_shapes:
        data_path = ds.file
        if not data_path or data_path == target_file:
            continue  # Skip the target itself (it's in target_content)

        if target_is_data:
            # Target is a data file — include ALL sibling data files
            # for cross-referencing shared ID namespaces
            content = _load_file_content(working_dir, data_path, max_chars=0)
            if content:
                data_file_contents[data_path] = content
        elif _data_shape_relevant(ds, target_file):
            # Target is a code file — include data files it consumes
            content = _load_file_content(working_dir, data_path, max_chars=0)
            if content:
                data_file_contents[data_path] = content

    return data_file_contents


def _extract_symbol_table(
    file_path: str,
    content: str,
) -> list[dict[str, str]]:
    """Extract a symbol table from file content.

    Returns a lightweight list of symbol names and signatures — no bodies.
    Used to give the model a structural map of the file. Works for ANY
    tree-sitter-supported language (Python, bash, js, ts, go, ruby, rust, java)
    via repomap — so diagnose can trace a bash function as readily as a Python
    one; the stdlib-ast path is kept only as a Python-only fallback for when
    tree-sitter isn't available. Empty for symbol-less or unsupported-grammar
    files (the caller's full-file fallback then surfaces the content).
    """
    if not content:
        return []

    try:
        from agent.repomap import extract_file_symbols, is_tree_sitter_available

        if is_tree_sitter_available():
            defs, _refs = extract_file_symbols(file_path, content)
            symbols = []
            for sym in defs:
                if sym.kind in ("function", "method", "class"):
                    qualified = f"{sym.parent}.{sym.name}" if sym.parent else sym.name
                    symbols.append(
                        {
                            "name": qualified,
                            "kind": sym.kind,
                            "signature": sym.signature,
                        }
                    )
            if symbols:
                return symbols
        # No tree-sitter, or it surfaced nothing: stdlib ast is Python-only.
        if file_path.endswith(".py"):
            return _symbol_table_from_ast(content)
        return []
    except Exception as e:
        logger.debug("Symbol extraction failed for %s: %s", file_path, e)
        return _symbol_table_from_ast(content) if file_path.endswith(".py") else []


def _symbol_table_from_ast(content: str) -> list[dict[str, str]]:
    """Fallback symbol table extraction using stdlib ast module."""
    import ast

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    symbols = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            symbols.append(
                {
                    "name": node.name,
                    "kind": "class",
                    "signature": f"class {node.name}",
                }
            )
        elif isinstance(node, ast.FunctionDef) or isinstance(
            node, ast.AsyncFunctionDef
        ):
            # Build a simple signature
            args = []
            for arg in node.args.args:
                if arg.arg == "self":
                    continue
                if arg.annotation:
                    try:
                        args.append(f"{arg.arg}: {ast.unparse(arg.annotation)}")
                    except Exception:
                        args.append(arg.arg)
                else:
                    args.append(arg.arg)
            ret = ""
            if node.returns:
                try:
                    ret = f" -> {ast.unparse(node.returns)}"
                except Exception:
                    pass
            sig = f"def {node.name}({', '.join(args)}){ret}"
            symbols.append(
                {
                    "name": node.name,
                    "kind": "function",
                    "signature": sig,
                }
            )
    return symbols


def _extract_imported_symbol_bodies(
    file_path: str,
    content: str,
    symbol_names: list[str],
) -> dict[str, str]:
    """Extract full bodies of specific symbols from a file.

    Used for smart dependency inclusion: instead of including the entire
    dependency file, include only the symbols we actually import.

    Returns {symbol_name: body_text} for each found symbol. Language-agnostic:
    bodies are sliced by repomap's tree-sitter byte offsets, which it produces
    for every supported grammar (no longer Python-only).
    """
    if not content:
        return {}

    try:
        from agent.repomap import extract_file_symbols, is_tree_sitter_available

        if not is_tree_sitter_available():
            return {}  # Can't extract bodies without tree-sitter

        defs, _ = extract_file_symbols(file_path, content)
        content_bytes = content.encode("utf-8")
        bodies: dict[str, str] = {}

        # Normalize requested names for matching
        requested = set(symbol_names)

        for sym in defs:
            if sym.kind not in ("function", "method", "class"):
                continue
            # Match against both bare name and qualified name
            qualified = f"{sym.parent}.{sym.name}" if sym.parent else sym.name
            if sym.name in requested or qualified in requested:
                if sym.start_byte >= 0 and sym.end_byte > sym.start_byte:
                    body = content_bytes[sym.start_byte : sym.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                    bodies[sym.name] = body

        return bodies
    except Exception as e:
        logger.debug("Symbol body extraction failed for %s: %s", file_path, e)
        return {}


def _interface_to_dict(iface: Any) -> dict:
    """Convert an InterfaceContract to a plain dict."""
    return {
        "caller": iface.caller,
        "callee": iface.callee,
        "symbol": iface.symbol,
        "signature": iface.signature,
    }


def _data_shape_to_dict(ds: Any) -> dict:
    """Convert a DataShapeContract to a plain dict."""
    return {
        "file": ds.file,
        "consumed_by": ds.consumed_by,
        "structure": ds.structure,
        # The exemplar is the authoritative nested contract. Dropping it
        # here meant creation prompts only ever saw the vague structure
        # prose — so code files invented fields (live: loader.py required
        # 'initial_dialogue_node' that no contract declared) while data
        # files followed the exemplar, and the two authorities diverged.
        "example": getattr(ds, "example", "") or "",
    }


def _state_shapes_to_dicts(arch: Any) -> list[dict]:
    """Convert ALL StateShapeContracts to plain dicts.

    State shapes are global canonical-representation contracts (in-memory
    state + persisted-state schemas). Unlike data_shapes they are never
    filtered by file relevance: the whole point is that EVERY author and
    diagnosis sees the same canonical answer, and the entries are terse
    one-liners.
    """
    return [
        {
            "name": ss.name,
            "owner": ss.owner,
            "consumed_by": ss.consumed_by,
            "structure": ss.structure,
        }
        for ss in getattr(arch, "state_shapes", []) or []
    ]


def _consumed_by_matches(consumed_by: str, target_file: str) -> bool:
    """Check if a data_shape's consumed_by field refers to a target file.

    consumed_by may be a qualified symbol path like "loader.load_world"
    or "engine.GameEngine.load_state". We match by checking if the first
    dotted component corresponds to the target file's module name.

    Examples:
        _consumed_by_matches("loader.load_world", "loader.py") → True
        _consumed_by_matches("engine.GameEngine", "engine.py") → True
        _consumed_by_matches("loader.load_world", "engine.py") → False
    """
    if consumed_by == target_file:
        return True
    consumer_module = consumed_by.split(".")[0] if "." in consumed_by else consumed_by
    target_module = _file_to_module_name(target_file)
    return consumer_module == target_module


def _data_shape_relevant(ds: Any, target_file: str, modules: Any = None) -> bool:
    """Check if a data_shape contract is relevant to a target file.

    Relevant when the target file is:
      - The data file itself (ds.file == target_file)
      - The code that consumes it (ds.consumed_by matches target_file)
      - A module the consumer imports from. The consumer maps the data
        into classes defined there, so that module co-owns the shape —
        live failure: models.py (imported by loader.py) invented an NPC
        field the exemplar never declared, loader validated it strictly,
        and the conforming data file failed to boot.
    """
    if ds.file == target_file:
        return True
    if _consumed_by_matches(ds.consumed_by, target_file):
        return True
    # imports_from keys arrive as either file names ("models.py") or bare
    # module names ("models") depending on how the design turn wrote
    # them — normalize both sides (live: bare keys silently excluded
    # models.py from the contract on the redo run).
    target_key = target_file.rsplit("/", 1)[-1].removesuffix(".py")
    for mod in modules or []:
        if _consumed_by_matches(ds.consumed_by, getattr(mod, "file", "")):
            keys = getattr(mod, "imports_from", None) or {}
            return target_key in {
                k.rsplit("/", 1)[-1].removesuffix(".py") for k in keys
            }
    return False


def _file_to_module_name(file_path: str) -> str:
    """Convert a file path to a Python module name.

    "models.py" → "models"
    "src/models.py" → "models"
    "parser.py" → "parser"
    """
    import os

    basename = os.path.basename(file_path)
    if basename.endswith(".py"):
        return basename[:-3]
    return basename


def _find_module_by_name(
    modules: list,
    module_name: str,
) -> Any | None:
    """Find a ModuleSpec by module name or file path.

    Matches "models" against "models.py", "src/models.py", etc.
    Also matches exact file path.
    """
    for mod in modules:
        if mod.file == module_name:
            return mod
        if _file_to_module_name(mod.file) == module_name:
            return mod
    return None


def _filter_notes_for_file(
    mission: MissionState,
    target_file: str,
) -> list[str]:
    """Filter notes relevant to a specific file.

    Returns notes that mention the file path, are tagged with the file,
    or belong to relevant categories (architecture, codebase_observation).
    Sorted by recency, capped at 5.
    """
    relevant = []
    for note in sorted(mission.notes, key=lambda n: n.timestamp, reverse=True):
        # Direct file mention in content or tags
        if target_file in note.content or target_file in note.tags:
            relevant.append(f"[{note.category}] {note.content}")
            continue
        # Architecture and codebase notes are broadly relevant
        if note.category in ("architecture_blueprint", "codebase_observation"):
            relevant.append(f"[{note.category}] {note.content}")

        if len(relevant) >= 5:
            break

    return relevant


def _trace_data_flows(
    target_file: str,
    target_content: str,
    import_deps: list[dict],
    interfaces: list[dict],
) -> list[dict]:
    """Trace data value paths through functions in the target file.

    Uses tree-sitter (via repomap.extract_attribute_accesses) to find
    every attribute access within each function body, then enriches the
    raw accesses with cross-references from:

      - **Class field signatures** from import dependencies — when the
        function accesses `item.name`, and the dep defines
        ``Item(name: str, id: str)``, we annotate the access with the
        known type.
      - **Interface contracts** from the architecture graph — when the
        function receives a parameter that a caller passes, we annotate
        the parameter's origin.

    This is structural evidence, not type inference.  The LLM does the
    semantic reasoning; we just make the evidence visible.

    Returns a list of per-function flow dicts:
    [
        {
            "function": "_handle_take",
            "qualified_name": "GameEngine._handle_take",
            "kind": "method",
            "parameters": ["cmd"],
            "accesses_by_root": {
                "self": [
                    {"chain": "self.items", "attribute": "items", "line": 148},
                    {"chain": "self.inventory", "attribute": "inventory", "line": 150},
                ],
                "cmd": [
                    {"chain": "cmd.args", "attribute": "args", "line": 144},
                ],
            },
            "known_field_types": {
                "Item": "item_id: str, name: str, description: str, portable: bool",
            },
            "parameter_sources": [
                {"param": "cmd", "from_caller": "engine.py", "via": "handle_command(cmd)"},
            ],
        }
    ]
    """
    if not target_content or not target_file:
        return []

    try:
        from agent.repomap import extract_attribute_accesses
    except ImportError:
        logger.debug("repomap not available for data flow tracing")
        return []

    try:
        raw_accesses = extract_attribute_accesses(target_file, target_content)
    except Exception as e:
        logger.debug("Attribute access extraction failed for %s: %s", target_file, e)
        return []

    if not raw_accesses:
        return []

    # Build a map of known class field signatures from import deps
    known_fields: dict[str, str] = {}
    for dep in import_deps:
        if isinstance(dep, dict):
            for cls_name, sig in dep.get("field_signatures", {}).items():
                known_fields[cls_name] = sig

    # Build parameter source map from interfaces
    # interfaces: [{caller, callee, symbol, signature}]
    target_module = _file_to_module_name(target_file)
    param_sources: dict[str, list[dict]] = {}
    for iface in interfaces:
        if not isinstance(iface, dict):
            continue
        callee = iface.get("callee", "")
        callee_mod = _file_to_module_name(callee) if callee else ""
        if callee == target_file or callee_mod == target_module:
            symbol = iface.get("symbol", "")
            caller = iface.get("caller", "")
            signature = iface.get("signature", "")
            if symbol not in param_sources:
                param_sources[symbol] = []
            param_sources[symbol].append(
                {
                    "from_caller": caller,
                    "via": f"{symbol}{signature}" if signature else symbol,
                }
            )

    # Convert raw FunctionAccesses to serializable dicts
    flows: list[dict] = []
    for fa in raw_accesses:
        qualified = (
            f"{fa.parent_class}.{fa.function_name}"
            if fa.parent_class
            else fa.function_name
        )

        # Group accesses by root object, deduplicate chains
        accesses_by_root: dict[str, list[dict]] = {}
        seen_chains: set[str] = set()
        for acc in fa.accesses:
            if acc.chain in seen_chains:
                continue
            seen_chains.add(acc.chain)
            if acc.root not in accesses_by_root:
                accesses_by_root[acc.root] = []
            accesses_by_root[acc.root].append(
                {
                    "chain": acc.chain,
                    "attribute": acc.attribute,
                    "line": acc.line,
                }
            )

        # Find parameter sources for this function
        func_param_sources = []
        lookup_name = fa.function_name
        if lookup_name in param_sources:
            for src in param_sources[lookup_name]:
                for param in fa.parameters:
                    func_param_sources.append(
                        {
                            "param": param,
                            "from_caller": src["from_caller"],
                            "via": src["via"],
                        }
                    )

        flow_entry = {
            "function": fa.function_name,
            "qualified_name": qualified,
            "kind": fa.kind,
            "parameters": fa.parameters,
            "accesses_by_root": accesses_by_root,
        }

        # Only include enrichments when they exist
        if known_fields:
            flow_entry["known_field_types"] = known_fields
        if func_param_sources:
            flow_entry["parameter_sources"] = func_param_sources

        flows.append(flow_entry)

    return flows


def _extract_field_signatures(
    working_dir: str,
    file_path: str,
) -> dict[str, str]:
    """Extract class/dataclass field signatures from a Python file on disk.

    Reads the file and uses AST or regex to find dataclass fields and
    __init__ parameters for each class. Returns a dict mapping class name
    to its field signature string.

    Example return: {"Item": "item_id: str, name: str, description: str, portable: bool"}

    Returns empty dict if the file doesn't exist or can't be parsed.
    This is expected when creation_order is violated — the caller
    degrades gracefully (no field signatures in the prompt, completion
    gate catches mismatches later).
    """
    import os

    full_path = os.path.join(working_dir, file_path)
    if not os.path.isfile(full_path):
        return {}

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return {}

    return _parse_field_signatures(content)


def _load_file_content(
    working_dir: str,
    file_path: str,
    max_chars: int = 0,
) -> str:
    """Load a file's content from disk for inclusion in projections.

    Args:
        working_dir: Project working directory.
        file_path: Relative path to the file.
        max_chars: Maximum characters to include. 0 means no limit (include
            the full file). Non-zero values truncate with a marker — use
            only for display contexts, never for content that will be parsed.

    Returns:
        File content as string, or empty string if not found.
    """
    import os

    full_path = os.path.join(working_dir, file_path)
    if not os.path.isfile(full_path):
        return ""

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return ""

    if max_chars > 0 and len(content) > max_chars:
        return content[:max_chars] + "\n# ... truncated ...\n"
    return content


def _parse_field_signatures(content: str) -> dict[str, str]:
    """Parse Python source to extract class field signatures.

    Handles:
    - @dataclass classes with field annotations
    - Regular classes with __init__ type hints
    - NamedTuple definitions

    Returns {class_name: "field1: type1, field2: type2, ..."}
    """
    import ast

    signatures: dict[str, str] = {}

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        class_name = node.name
        fields: list[str] = []

        # Check for dataclass-style annotated fields
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                field_name = item.target.id
                # Get the type annotation as source text
                try:
                    type_str = ast.unparse(item.annotation)
                except Exception:
                    type_str = "Any"

                # Check for default value
                if item.value is not None:
                    try:
                        default_str = ast.unparse(item.value)
                        fields.append(f"{field_name}: {type_str} = {default_str}")
                    except Exception:
                        fields.append(f"{field_name}: {type_str}")
                else:
                    fields.append(f"{field_name}: {type_str}")

        # If no annotated fields, try __init__ parameters
        if not fields:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    for arg in item.args.args:
                        if arg.arg == "self":
                            continue
                        if arg.annotation:
                            try:
                                type_str = ast.unparse(arg.annotation)
                                fields.append(f"{arg.arg}: {type_str}")
                            except Exception:
                                fields.append(arg.arg)
                        else:
                            fields.append(arg.arg)
                    break

        if fields:
            signatures[class_name] = ", ".join(fields)

    return signatures


@register("project_fix_target_menu")
def project_fix_target_menu(mission: MissionState, params: dict) -> list[dict]:
    """File menu for LLM-based fix target selection (Site #9).

    Pattern C projection: produces a list of pre-composed display
    strings for a menu_single turn. The turn renderer pipes this list
    directly into the options section; no template composition needed.

    Reads `mission.architecture.modules` and `.data_shapes`, emits
    one option per unique file path with `{id, description}` where:
      - id: the file path (what the model selects)
      - description: `{file} — {responsibility} [defines: sym1, sym2, ... (+N more)]`
        or `{file} — data file consumed by {consumer}` for data files

    Modules come first (in architecture order), then data files not
    already covered. The `defines` list is truncated at 6 symbols
    with `(+N more)` suffix to keep the menu readable.

    Returns an empty list when architecture is missing or has no
    modules — the turn renderer raises TurnRenderError on an empty
    options list, which then routes the step to `no_answer`.
    """
    arch = getattr(mission, "architecture", None)
    if not arch:
        return []

    options: list[dict] = []
    seen_paths: set[str] = set()

    # Modules first — each gets its responsibility + a defines summary.
    for mod in arch.modules:
        defines = list(mod.defines) if mod.defines else []
        if defines:
            symbols_str = ", ".join(defines[:6])
            if len(defines) > 6:
                symbols_str += f" (+{len(defines) - 6} more)"
            description = f"{mod.file} — {mod.responsibility} [defines: {symbols_str}]"
        else:
            description = f"{mod.file} — {mod.responsibility}"
        options.append({"id": mod.file, "description": description})
        seen_paths.add(mod.file)

    # Data files (e.g., world.yaml, dialogues.yaml) — valid fix targets
    # when the LLM needs to edit data content rather than code.
    for ds in getattr(arch, "data_shapes", []) or []:
        file_path = ds.file if hasattr(ds, "file") else ds.get("file", "")
        consumer = (
            ds.consumed_by if hasattr(ds, "consumed_by") else ds.get("consumed_by", "")
        )
        if not file_path or file_path in seen_paths:
            continue
        options.append(
            {
                "id": file_path,
                "description": f"{file_path} — data file consumed by {consumer}",
            }
        )
        seen_paths.add(file_path)

    return options


@register("project_research_overview")
def project_research_overview(mission: MissionState, params: dict) -> dict:
    """Aspect coverage + databank stats for the scraper flow set.

    Projections are sync and effect-less, so the databank is read
    straight from the mission working directory (same pattern as the
    sweeps' os.path file checks).
    """
    import json as _json
    import os as _os

    plan = getattr(mission, "research_plan", None)
    databank: dict[str, dict] = {}
    path = _os.path.join(mission.config.working_directory, "databank", "papers.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if rec.get("paper_key"):
                    databank[rec["paper_key"]] = rec
    except OSError:
        pass

    statuses = [r.get("status") for r in databank.values()]
    aspects = []
    for aspect in plan.aspects if plan else []:
        strong = sum(
            1
            for r in databank.values()
            if any(
                t.get("aspect") == aspect.name
                and t.get("relevance") in ("exact", "close")
                for t in (r.get("tags") or [])
            )
        )
        candidates = sum(
            1
            for r in databank.values()
            if aspect.name in (r.get("source_aspects") or [])
        )
        aspects.append(
            {
                "name": aspect.name,
                "target": aspect.coverage_target,
                "candidates": candidates,
                "strong_tagged": strong,
            }
        )

    return {
        "abstract": (plan.abstract if plan else mission.objective)[:500],
        "aspects": aspects,
        "worklist": {
            "candidate": statuses.count("candidate"),
            "acquired": statuses.count("acquired"),
            "cataloged": statuses.count("cataloged"),
            "needs_retag": statuses.count("needs_retag"),
            "failed": statuses.count("failed"),
        },
        "corpus": {
            "papers": len(databank),
            "pdfs": sum(1 for r in databank.values() if r.get("pdf_path")),
            "closed": sum(
                1 for r in databank.values() if r.get("access_status") == "closed"
            ),
        },
    }
