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

    # Step 6: Build schema context (Level 1 + 2 — always on). CAPPED: on a
    # repo-scale checkout this block dwarfs everything else (swe-bench-astropy
    # ingest: 107KB of Data Schemas inside a 154KB / 47k-token prompt that took
    # 167s to prefill — 15% of the task budget before any work started). The
    # repomap itself is budgeted via max_chars; hold the schema appendix to
    # twice that budget so huge repos degrade to "the biggest schemas" instead
    # of "every schema".
    from agent.schema_extract import build_schema_context

    schema_context = build_schema_context(file_contents)
    if schema_context:
        schema_budget = max_chars * 2
        if len(schema_context) > schema_budget:
            schema_context = (
                schema_context[:schema_budget]
                + f"\n… (schema context truncated at {schema_budget} chars — repo-scale checkout)"
            )
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

    # Complete source-file index (the confabulation authority anchor). Unlike the
    # PageRank-/char-budgeted repo_map above (which DROPS lower-ranked files), this
    # lists EVERY source file that exists, so the decompose/diagnose prompts can
    # assert "a path not in this list does not exist" and kill invented module
    # paths (validated: sphinx-9367 decompose confab 8/8 → 0/8, 8/8 gold). A
    # generous char cap degrades the claim to "strong prior" (via an inline note)
    # on repo-scale trees rather than bloating the prompt unboundedly.
    _INDEX_CAP = 60000
    sorted_paths = sorted(matched_paths)
    index_str = "\n".join(sorted_paths)
    if len(index_str) > _INDEX_CAP:
        kept: list[str] = []
        used = 0
        for p in sorted_paths:
            if used + len(p) + 1 > _INDEX_CAP:
                break
            kept.append(p)
            used += len(p) + 1
        index_str = "\n".join(kept) + (
            f"\n… ({len(sorted_paths) - len(kept)} more files not shown — index "
            "truncated for a repo-scale tree; treat the list as a strong prior)"
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
            "repo_file_index": index_str,
            "related_files": unique_related,
            "raw_results": raw_text,
        },
    )


# ── run_git_investigation ─────────────────────────────────────────────


# ── validate_cross_file_consistency ───────────────────────────────────


def _import_bound_names(statement: str) -> set[str]:
    """Names an import statement binds in the importing module.

    ``from m import a, b as c`` → {m, a, c}; ``import x.y as z`` → {z};
    ``import x.y`` → {x}. Python-only (stdlib ast); non-Python import
    statements parse-fail and contribute nothing — same behavior as
    before this helper existed.
    """
    import ast as _ast

    names: set[str] = set()
    try:
        tree = _ast.parse(statement.strip())
    except SyntaxError:
        return names
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


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

    # Two separate indexes — they answer different questions:
    #   known_names      — "could this reference resolve at runtime?"
    #                      (everything: defs of every kind, methods,
    #                      dunders, AND imported names — an import IS
    #                      structural evidence the name resolves)
    #   module_level_defs — "do two files claim the same module-level
    #                      name?" (duplicates check: functions/classes
    #                      only — no methods, no dunders, no variables)
    known_names: set[str] = set()
    module_level_defs: dict[str, list[tuple[str, str]]] = {}
    for fp, info in repo_map.files.items():
        for defn in info.definitions:
            if defn.kind == "import":
                # The repo map stores the WHOLE import statement as the
                # definition name — parse out the names it binds, since
                # an import is structural evidence those names resolve.
                known_names |= _import_bound_names(defn.name)
                continue
            known_names.add(defn.name)
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
            # Module-level variables with the same name across files are
            # harmless under module namespaces (logger, _PATTERNS, …) and
            # idiomatic. Within-file duplicates are caught by the splice
            # and frame-editor guards. Only functions/classes can collide
            # meaningfully across files.
            if defn.kind == "variable":
                continue
            if defn.name not in module_level_defs:
                module_level_defs[defn.name] = []
            module_level_defs[defn.name].append((fp, defn.kind))

    # Check 1: Duplicate definitions (same name in multiple files)
    for name, locations in module_level_defs.items():
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

    # Check 2: Unresolved references. A name is resolvable if any project
    # file defines it (flat-import reach), any file imports it — the
    # import statement itself is structural evidence the name resolves
    # (typing.List, pathlib.Path, third-party) — or it is a Python
    # builtin (runtime-derived from dir(builtins), no curated list;
    # exception classes were the last noise class). What survives is
    # high-signal: referenced, never defined, never imported, not built in.
    import builtins as _builtins

    py_builtins = set(dir(_builtins))
    for fp, info in repo_map.files.items():
        is_python = fp.endswith(".py")
        for ref in info.references:
            if is_python and ref.name in py_builtins:
                continue
            if ref.name not in known_names:
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
                            "message": f"'{ref.name}' referenced in {fp}:{ref.line} but not defined or imported in any project file",
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


# ── validate_data_shapes ──────────────────────────────────────────────
#
# The general class the dialogue-schema war exposed: NESTED data
# contracts with internal key choices (list-vs-dict, deep key names)
# that one-line prose `structure` descriptions cannot pin down. The
# architecture's data_shapes now carry a literal minimal exemplar; this
# checker diffs each real data file against it PATH BY PATH, turning
# "the loader and the data drifted" from a behavioral symptom three
# layers downstream into a named, located fact.
#
# Precision doctrine (learned from the cross-file checker): structural
# checks only — container types and key names. No scalar typing (str vs
# int noise), no value checks. A key present in the data but absent at
# that path in the exemplar is the primary signal (the next_id/next_node
# rename class); a declared key missing from the data is secondary
# (possibly optional) and reported distinctly.
#
# OPEN MAPPINGS: a single-entry exemplar mapping is a KEY PATTERN, not a
# fixed schema — `exits: {north: lighthouse}` declares "direction -> room
# id", and real rooms legitimately have east/south/west. Key names are
# not checked there; each value is checked against the one exemplar
# value. (Live-observed: the first armed gate flagged every non-north
# direction as undeclared; the verification probes refuted all of them
# behaviorally — this rule removes the noise at the source. Fixed
# schemas keep full key checking by carrying 2+ keys, which the
# exemplar convention "every key at every level" already produces.)
#
# OPEN COLLECTIONS (multi-key): a mapping whose exemplar values are ALL
# dicts with IDENTICAL key sets is a homogeneous collection — the keys
# are instance names (dialogue node ids, web routes, i18n locales), not
# schema fields. A struct's values are heterogeneous by nature; identical
# sub-shapes across 2+ entries is the exemplar itself saying "keyed
# collection". (Live-observed: 7 immortal gate findings on NPC dialogue
# node names, re-refuted 48 times across 52 rounds. Cross-domain battery:
# web routes and config maps false-positive identically.)
#
# VARIANT LIST ELEMENTS: when an exemplar LIST carries 2+ elements whose
# key sets DIFFER, the author is declaring variants (CI steps with
# `uses` vs `run`). Data elements are then checked against the UNION of
# sibling keys, and only keys present in EVERY sibling are required.
# Single-element exemplar lists (the documented convention) keep strict
# checking — the rules only widen when the exemplar itself demonstrates
# variance.
#
# KNOWN RESIDUAL: scalar-valued multi-key maps (dependency pins,
# hyperparameter dicts) are structurally indistinguishable from scalar
# structs ({front, back}) — they stay closed and can false-positive.
# That class is handled downstream by refuted-signature suppression,
# not here; opening it structurally would silence real key renames.

_MAX_SHAPE_ISSUES_PER_FILE = 10


def _identical_dict_shapes(values: list) -> bool:
    """True when every value is a dict and all share one key set."""
    if not values or not all(isinstance(v, dict) for v in values):
        return False
    first = set(values[0].keys())
    return all(set(v.keys()) == first for v in values[1:])


def _shape_diff(data: Any, exemplar: Any, path: str, issues: list[dict]) -> None:
    """Recursive structural diff of a parsed data file vs its exemplar."""
    if len(issues) >= _MAX_SHAPE_ISSUES_PER_FILE:
        return
    if isinstance(exemplar, dict):
        if not isinstance(data, dict):
            issues.append(
                {
                    "kind": "type_mismatch",
                    "path": path or "<root>",
                    "detail": (
                        f"exemplar declares a mapping but the file has "
                        f"{type(data).__name__}"
                    ),
                }
            )
            return
        if len(exemplar) == 1 or _identical_dict_shapes(list(exemplar.values())):
            # Open mapping: a single-entry exemplar is a key pattern; a
            # multi-entry exemplar whose values all share one dict shape
            # is a homogeneous collection (instance names, not schema).
            # Validate values against the first exemplar value; key
            # names are the data's business.
            exemplar_value = next(iter(exemplar.values()))
            for key in data:
                if len(issues) >= _MAX_SHAPE_ISSUES_PER_FILE:
                    return
                _shape_diff(
                    data[key],
                    exemplar_value,
                    f"{path}.{key}" if path else str(key),
                    issues,
                )
            return
        for key in data:
            if key not in exemplar and len(issues) < _MAX_SHAPE_ISSUES_PER_FILE:
                issues.append(
                    {
                        "kind": "undeclared_key",
                        "path": path or "<root>",
                        "detail": (
                            f"key '{key}' is not in the declared example "
                            f"(declared keys here: {sorted(exemplar)})"
                        ),
                    }
                )
        for key in exemplar:
            if key not in data and len(issues) < _MAX_SHAPE_ISSUES_PER_FILE:
                issues.append(
                    {
                        "kind": "missing_declared_key",
                        "path": path or "<root>",
                        "detail": f"declared key '{key}' is absent from the file",
                    }
                )
        for key in data:
            if key in exemplar and len(issues) < _MAX_SHAPE_ISSUES_PER_FILE:
                _shape_diff(
                    data[key], exemplar[key], f"{path}.{key}" if path else key, issues
                )
        return
    if isinstance(exemplar, list):
        if not isinstance(data, list):
            issues.append(
                {
                    "kind": "type_mismatch",
                    "path": path or "<root>",
                    "detail": (
                        f"exemplar declares a list but the file has "
                        f"{type(data).__name__}"
                    ),
                }
            )
            return
        if exemplar:
            # Exemplar lists carry ONE element by convention; every real
            # element must conform to it. When the author wrote 2+ DICT
            # elements with DIFFERING key sets, the exemplar itself
            # declares variants (CI steps with `uses` vs `run`): data
            # elements then check against the UNION of sibling keys and
            # only intersection keys are required.
            sibling_dicts = [e for e in exemplar if isinstance(e, dict)]
            divergent = (
                len(sibling_dicts) >= 2
                and len(sibling_dicts) == len(exemplar)
                and not _identical_dict_shapes(sibling_dicts)
            )
            if divergent:
                union: set = set().union(*(set(e.keys()) for e in sibling_dicts))
                required = set.intersection(*(set(e.keys()) for e in sibling_dicts))
                for i, element in enumerate(data):
                    if len(issues) >= _MAX_SHAPE_ISSUES_PER_FILE:
                        return
                    if not isinstance(element, dict):
                        _shape_diff(element, exemplar[0], f"{path}[{i}]", issues)
                        continue
                    epath = f"{path}[{i}]"
                    for key in element:
                        if key not in union:
                            issues.append(
                                {
                                    "kind": "undeclared_key",
                                    "path": epath,
                                    "detail": (
                                        f"key '{key}' is not in any declared "
                                        f"variant (variant keys: {sorted(union)})"
                                    ),
                                }
                            )
                            if len(issues) >= _MAX_SHAPE_ISSUES_PER_FILE:
                                return
                    for key in required:
                        if key not in element:
                            issues.append(
                                {
                                    "kind": "missing_declared_key",
                                    "path": epath,
                                    "detail": (
                                        f"declared key '{key}' (required by "
                                        f"every variant) is absent"
                                    ),
                                }
                            )
                            if len(issues) >= _MAX_SHAPE_ISSUES_PER_FILE:
                                return
                    # Recurse into keys with a matching sibling declaration.
                    for key in element:
                        donor = next((e for e in sibling_dicts if key in e), None)
                        if donor is not None:
                            _shape_diff(
                                element[key], donor[key], f"{epath}.{key}", issues
                            )
                            if len(issues) >= _MAX_SHAPE_ISSUES_PER_FILE:
                                return
                return
            for i, element in enumerate(data):
                if len(issues) >= _MAX_SHAPE_ISSUES_PER_FILE:
                    return
                _shape_diff(element, exemplar[0], f"{path}[{i}]", issues)
        return
    # Scalar exemplar: no value/typing checks (precision over coverage) —
    # except a container where a scalar was declared, which IS structural.
    if isinstance(data, (dict, list)):
        issues.append(
            {
                "kind": "type_mismatch",
                "path": path or "<root>",
                "detail": (
                    f"exemplar declares a scalar but the file has "
                    f"{type(data).__name__}"
                ),
            }
        )


def _parse_data_text(text: str) -> Any:
    """Parse YAML or JSON (YAML is a superset; pyyaml handles both)."""
    import yaml

    return yaml.safe_load(text)


async def action_validate_data_shapes(step_input: StepInput) -> StepOutput:
    """Diff each declared data file against its exemplar contract.

    Skips shapes with no exemplar (pre-contract architectures) and files
    that don't exist yet. An unparseable data file is itself an issue.

    Context: architecture (optional — no architecture means no contracts)
    Result: shapes_checked, issue_count, all_conformant
    Publishes: data_shape_results, data_shape_summary
    """
    effects = step_input.effects
    arch = step_input.context.get("architecture")
    shapes = getattr(arch, "data_shapes", None) or []

    checked = 0
    all_issues: list[dict] = []
    for shape in shapes:
        example = (getattr(shape, "example", "") or "").strip()
        file_path = (getattr(shape, "file", "") or "").strip()
        if not example or not file_path:
            continue
        fc = await effects.read_file(file_path)
        if not getattr(fc, "exists", False):
            continue
        checked += 1
        try:
            exemplar = _parse_data_text(example)
        except Exception as e:
            all_issues.append(
                {
                    "file": file_path,
                    "kind": "exemplar_unparseable",
                    "path": "<contract>",
                    "detail": f"declared example does not parse: {e}",
                }
            )
            continue
        try:
            data = _parse_data_text(fc.content)
        except Exception as e:
            all_issues.append(
                {
                    "file": file_path,
                    "kind": "file_unparseable",
                    "path": "<root>",
                    "detail": f"data file does not parse: {e}",
                }
            )
            continue
        issues: list[dict] = []
        _shape_diff(data, exemplar, "", issues)
        for issue in issues:
            all_issues.append({"file": file_path, **issue})

    if all_issues:
        lines = [
            f"Data-shape contract violations ({len(all_issues)}; the declared "
            f"example in the architecture is the contract):"
        ]
        for issue in all_issues:
            lines.append(
                f"- {issue['file']} at {issue['path']}: [{issue['kind']}] "
                f"{issue['detail']}"
            )
        summary = "\n".join(lines)
    else:
        summary = ""

    return StepOutput(
        result={
            "shapes_checked": checked,
            "issue_count": len(all_issues),
            "all_conformant": not all_issues,
        },
        observations=(
            f"Data shapes: {checked} file(s) checked, "
            f"{len(all_issues)} contract violation(s)"
        ),
        context_updates={
            "data_shape_results": {"issues": all_issues, "files_checked": checked},
            "data_shape_summary": summary,
        },
    )
