"""Trace — symbol-level code tracing for diagnosis.

Provides trace_function: given a symbol name, extracts its body
and all immediately referenced symbols (same-file bodies or signatures,
cross-file signatures only). Produces a compact, high-signal context
string for the diagnosis inference step.

Not a registered action — called directly as a helper from
diagnosis_session_actions during guided ReAct investigation.

Design rationale:
  - Full file reads dump 10-15K of mostly irrelevant code
  - Architecture summaries have zero code, model can't trace execution
  - trace_function gives ~1-3K of precisely the code the model needs:
    the target symbol, what it calls, and what data it touches

References are found by:
  1. Name-matching the body text against the symbol_table (same-file defs)
  2. Scanning for self.X patterns where X is not a known method →
     extracting the assignment from class-body interstitial regions
  3. Matching import names against architecture interfaces (cross-file)
"""

from __future__ import annotations

import logging
import re

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

# Threshold: include full body for symbols up to this many lines,
# signature only for larger ones.
SMALL_SYMBOL_MAX_LINES = 20


async def trace_function(step_input: StepInput) -> StepOutput:
    """Trace a symbol's execution context for diagnosis.

    Reads the selected symbol's body from the symbol_table, finds all
    same-file and cross-file references, and produces a formatted
    context string suitable for injection into the diagnosis prompt.

    Context required:
        selected_symbol_name — string, e.g. "GameEngine.run"
        symbol_table — list of symbol dicts from extract_symbol_bodies
        target_file — dict with 'path' and 'content'

    Context optional:
        file_context — projection dict (for cross-file interfaces)

    Publishes:
        traced_context — formatted string with symbol body + references
    """
    symbol_name = step_input.context.get("selected_symbol_name", "")
    symbol_table = step_input.context.get("symbol_table", [])
    target_file = step_input.context.get("target_file", {})
    file_context = step_input.context.get("file_context")

    file_path = target_file.get("path", "")
    file_content = target_file.get("content", "")

    if not symbol_name or not symbol_table:
        return StepOutput(
            result={"traced": False, "symbol_count": 0},
            observations="No symbol name or symbol table — cannot trace",
            context_updates={"traced_context": ""},
        )

    # ── 1. Find the target symbol ────────────────────────────────
    target_sym = _find_symbol(symbol_name, symbol_table)
    if not target_sym:
        return StepOutput(
            result={"traced": False, "symbol_count": 0},
            observations=f"Symbol {symbol_name!r} not found in symbol table",
            context_updates={"traced_context": ""},
        )

    target_body = target_sym.get("body", "")
    if not target_body:
        return StepOutput(
            result={"traced": False, "symbol_count": 0},
            observations=f"Symbol {symbol_name!r} has no body",
            context_updates={"traced_context": ""},
        )

    # ── 2. Find same-file symbol references ──────────────────────
    same_file_refs = _find_same_file_refs(target_body, target_sym, symbol_table)

    # ── 3. Find class-level data references (self.X assignments) ─
    class_data_refs = []
    if file_content and target_sym.get("parent"):
        class_data_refs = _find_class_data_refs(
            target_body, target_sym, symbol_table, file_content
        )

    # ── 4. Find cross-file references ────────────────────────────
    cross_file_refs = _find_cross_file_refs(target_body, file_context, file_content)

    # ── 5. Find upward callers (f3d round) ───────────────────────
    # Reverse-deps in file_context list which OTHER files import
    # symbols from THIS file, with attached call_sites that show
    # exactly where those imports are invoked. Filter to the rows
    # that name our target symbol, then surface the call site rows
    # so diagnose can see the boundary where the contract is met.
    # Critical for "expected A but got B" diagnoses where the bug
    # lives at the producer side, not in the symbol throwing the
    # error. f3d cycle 27 shows this exact failure pattern: the
    # reporter (Room.from_dict) was patched repeatedly while the
    # actual issue lived in loader._load_rooms passing already-
    # constructed Room objects through.
    upward_callers = _find_upward_callers(target_sym, file_context)

    # ── 5. Format the traced context ─────────────────────────────
    traced = _format_traced_context(
        target_sym=target_sym,
        file_path=file_path,
        same_file_refs=same_file_refs,
        class_data_refs=class_data_refs,
        cross_file_refs=cross_file_refs,
        upward_callers=upward_callers,
    )

    total_refs = (
        len(same_file_refs)
        + len(class_data_refs)
        + len(cross_file_refs)
        + len(upward_callers)
    )

    return StepOutput(
        result={"traced": True, "symbol_count": total_refs + 1},
        observations=(
            f"Traced {symbol_name}: {len(same_file_refs)} same-file refs, "
            f"{len(class_data_refs)} class data refs, "
            f"{len(cross_file_refs)} cross-file refs"
        ),
        context_updates={
            "traced_context": traced,
            "diagnosis_context": traced,  # direct injection for prompt template
        },
    )


# ── Internal helpers ─────────────────────────────────────────────────


def _find_symbol(name: str, symbol_table: list[dict]) -> dict | None:
    """Find a symbol by name in the symbol table.

    Matches against both the full qualified name and the bare name.
    """
    for sym in symbol_table:
        if sym.get("name") == name:
            return sym
    # Fallback: match bare name (e.g. "run" matches "GameEngine.run")
    for sym in symbol_table:
        sym_name = sym.get("name", "")
        bare = sym_name.rsplit(".", 1)[-1] if "." in sym_name else sym_name
        if bare == name:
            return sym
    return None


def _find_same_file_refs(
    body: str,
    target_sym: dict,
    symbol_table: list[dict],
) -> list[dict]:
    """Find symbols in the same file that the target body references.

    Scans the body text for names matching other symbol_table entries.
    Excludes the target symbol itself and its parent class (if any).
    Returns dicts with 'name', 'signature', 'body' (if small), 'kind'.
    """
    target_name = target_sym.get("name", "")
    parent_class = target_sym.get("parent", "")
    refs = []
    seen = set()

    for sym in symbol_table:
        sym_name = sym.get("name", "")
        if sym_name == target_name:
            continue

        # Get the bare name for matching (e.g. "handle_take" from
        # "GameEngine.handle_take")
        bare = sym_name.rsplit(".", 1)[-1] if "." in sym_name else sym_name

        # Skip the parent class itself (we're inside it)
        if sym.get("kind") == "class" and bare == parent_class:
            continue

        if bare in seen:
            continue

        # Check if this name appears in the target body
        # Match as a word boundary to avoid false positives
        # (e.g. "items" shouldn't match "items_list")
        pattern = r"(?<![a-zA-Z_])" + re.escape(bare) + r"(?![a-zA-Z_0-9])"
        if not re.search(pattern, body):
            continue

        seen.add(bare)
        sym_body = sym.get("body", "")
        line_count = sym_body.count("\n") + 1 if sym_body else 0

        refs.append(
            {
                "name": sym_name,
                "kind": sym.get("kind", ""),
                "signature": sym.get("signature", ""),
                "line": sym.get("line", 0),
                "end_line": sym.get("end_line", 0),
                "body": sym_body if line_count <= SMALL_SYMBOL_MAX_LINES else "",
                "included": line_count <= SMALL_SYMBOL_MAX_LINES,
            }
        )

    return refs


def _find_class_data_refs(
    body: str,
    target_sym: dict,
    symbol_table: list[dict],
    file_content: str,
) -> list[dict]:
    """Find class-level data (self.X assignments) referenced in the body.

    When the body contains self.X and X is not a known method in the
    symbol table, search the class body's interstitial regions (code
    between named methods) for assignments to X.

    Returns dicts with 'name', 'content' (the assignment region).
    """
    # Collect all self.X references
    self_refs = set(re.findall(r"self\.([a-zA-Z_][a-zA-Z_0-9]*)", body))

    # Remove known methods/functions from the set
    known_symbols = set()
    for sym in symbol_table:
        sym_name = sym.get("name", "")
        bare = sym_name.rsplit(".", 1)[-1] if "." in sym_name else sym_name
        known_symbols.add(bare)

    unknown_attrs = self_refs - known_symbols
    if not unknown_attrs:
        return []

    # Also check for class-level (no self.) references like _dispatch
    # used via self._dispatch but defined as a class variable
    class_level_refs = (
        set(re.findall(r"(?<![a-zA-Z_])([a-zA-Z_][a-zA-Z_0-9]*)\s*[=:]", body))
        - known_symbols
    )

    # Find the parent class boundaries
    parent_class = target_sym.get("parent", "")
    if not parent_class:
        return []

    class_sym = None
    for sym in symbol_table:
        if sym.get("kind") == "class" and (
            sym.get("name") == parent_class
            or sym.get("name", "").rsplit(".", 1)[-1] == parent_class
        ):
            class_sym = sym
            break

    if not class_sym:
        return []

    # Build a map of method line ranges within the class
    class_start = class_sym.get("line", 0)
    content_lines = file_content.splitlines()

    method_ranges = []
    for sym in symbol_table:
        if sym.get("parent") == parent_class and sym.get("kind") in (
            "method",
            "function",
        ):
            method_ranges.append((sym.get("line", 0), sym.get("end_line", 0)))
    method_ranges.sort()

    # Extract interstitial regions (class body between methods)
    interstitial_regions = []

    # Region before first method (after class def line)
    if method_ranges:
        first_method_start = method_ranges[0][0]
        if first_method_start > class_start + 1:
            region = "\n".join(content_lines[class_start : first_method_start - 1])
            interstitial_regions.append(
                (class_start + 1, first_method_start - 1, region)
            )

        # Regions between methods
        for i in range(len(method_ranges) - 1):
            gap_start = method_ranges[i][1]
            gap_end = method_ranges[i + 1][0]
            if gap_end > gap_start + 1:
                region = "\n".join(content_lines[gap_start : gap_end - 1])
                interstitial_regions.append((gap_start + 1, gap_end - 1, region))

    # Search interstitial regions for assignments to unknown attrs
    results = []
    all_targets = unknown_attrs | class_level_refs
    for attr_name in all_targets:
        for start_line, end_line, region in interstitial_regions:
            # Look for assignment patterns: attr_name = ... or
            # attr_name: Type = ...
            pattern = r"(?:^|\n)\s*" + re.escape(attr_name) + r"\s*[=:]"
            if re.search(pattern, region):
                results.append(
                    {
                        "name": attr_name,
                        "line": start_line,
                        "end_line": end_line,
                        "content": region.strip(),
                    }
                )
                break  # found it, stop searching regions

    return results


def _find_cross_file_refs(
    body: str,
    file_context: dict | None,
    file_content: str,
) -> list[dict]:
    """Find cross-file symbol references in the body.

    Uses the file_context projection's interfaces and the file's
    import lines to identify cross-file calls, returning signatures.
    """
    refs = []
    seen = set()

    # Extract import names from the file content
    imported_names: dict[str, str] = {}  # name → module
    if file_content:
        for line in file_content.splitlines():
            stripped = line.strip()
            # from parser import parse_command, Command
            m = re.match(r"from\s+(\S+)\s+import\s+(.+)", stripped)
            if m:
                module = m.group(1)
                names = [n.strip() for n in m.group(2).split(",")]
                for name in names:
                    # Handle 'as' aliases
                    parts = name.split(" as ")
                    imported_names[parts[-1].strip()] = module
            # import loader
            m = re.match(r"import\s+(\S+)", stripped)
            if m and not stripped.startswith("from"):
                mod = m.group(1)
                imported_names[mod] = mod

    # Check which imported names appear in the body
    for name, module in imported_names.items():
        if name in seen:
            continue
        pattern = r"(?<![a-zA-Z_])" + re.escape(name) + r"(?![a-zA-Z_0-9])"
        if re.search(pattern, body):
            seen.add(name)
            # Try to find a signature from file_context interfaces
            signature = ""
            if file_context and isinstance(file_context, dict):
                for iface in file_context.get("interfaces", []):
                    if isinstance(iface, dict):
                        if iface.get("symbol", "").split("(")[0] == name:
                            signature = iface.get("signature", "")
                            break
            refs.append(
                {
                    "name": name,
                    "module": module,
                    "signature": signature,
                }
            )

    return refs


def _find_upward_callers(target_sym: dict, file_context: dict | None) -> list[dict]:
    """Find files that import + call this symbol from outside.

    Reads ``file_context.reverse_deps`` (built by
    ``projections._build_reverse_deps`` with call sites attached)
    and filters to call sites whose ``symbol`` matches the target.

    Two name forms are checked:
      - The bare symbol name (``from_dict``) — typical when reverse
        dep imports the parent class and calls ``Room.from_dict``,
        in which case the call site grep will have matched on the
        parent class's name and recorded ``Room`` as the symbol.
      - Class.method form, in case the import was the bare method.

    Returns a list of ``{file, responsibility, line, snippet}``
    dicts. Empty when no reverse-dep call sites mention the target.
    """
    if not file_context or not isinstance(file_context, dict):
        return []
    reverse_deps = file_context.get("reverse_deps") or []
    if not reverse_deps:
        return []

    target_name = target_sym.get("name", "")
    target_parent = target_sym.get("parent") or ""
    # The match set: both the bare name and the parent (which is
    # what reverse-dep call site grep would have matched on, since
    # the importing file usually uses ``ParentClass.method(...)``).
    candidates = {target_name}
    if target_parent:
        candidates.add(target_parent)

    matches: list[dict] = []
    for dep in reverse_deps:
        if not isinstance(dep, dict):
            continue
        call_sites = dep.get("call_sites") or []
        for site in call_sites:
            if not isinstance(site, dict):
                continue
            site_sym = site.get("symbol", "")
            if site_sym in candidates:
                matches.append(
                    {
                        "file": dep.get("file", ""),
                        "responsibility": dep.get("responsibility", ""),
                        "line": site.get("line", 0),
                        "snippet": site.get("snippet", ""),
                        "matched_via": site_sym,
                    }
                )
    return matches


def _format_traced_context(
    target_sym: dict,
    file_path: str,
    same_file_refs: list[dict],
    class_data_refs: list[dict],
    cross_file_refs: list[dict],
    upward_callers: list[dict] | None = None,
) -> str:
    """Format the traced context as a structured string.

    The output is queued via ``session_injections`` and arrives in
    the model's next turn as ambient context. Without explicit
    framing the model interprets the leading ``=== file: symbol ===``
    header as project context (file listing, code snippet) rather
    than as the result of its previous trace request.

    e39 round confirmed the failure mode: across six runs the model
    re-requested traces of already-traced symbols 46-79% of the time.
    Session #2 of e39 traced ``parser.py:parse_command`` eight times
    in nine seconds, with each turn's CoT saying "we need to actually
    trace parse_command" or "format is wrong, retry the JSON" — the
    model literally could not recognize the body in its prompt as
    the tool return.

    Wrapping with explicit ``Observation (from your trace of ...):``
    aligns with the strongest cross-framework training-data anchor
    for tool-result delivery (ReAct / HotpotQA / LangChain tutorials
    all use ``Observation:`` as the labeled prefix). The closing
    marker provides a clear boundary so the model knows where its
    tool's data ends and the next turn's prompt begins.

    Line-number annotations like ``(lines 67-165)`` are stripped from
    body headers — the body content speaks for itself, and the line
    ranges shift between cycles when the file is edited, adding
    visual differences without semantic content. Call-site line
    numbers in the upstream section are kept because those identify
    specific points in caller files, which is actionable.
    """
    name = target_sym.get("name", "unknown")
    body = target_sym.get("body", "")

    qualified = f"{file_path}:{name}"

    lines = [f"Observation (from your trace of `{qualified}`):"]
    lines.append("")
    lines.append(body)

    # Same-file references
    if same_file_refs:
        lines.append("")
        lines.append("--- Other symbols in the same file ---")
        for ref in same_file_refs:
            ref_name = ref["name"]
            ref_kind = ref["kind"]

            if ref.get("included") and ref.get("body"):
                lines.append(f"\n  {ref_name} ({ref_kind}):")
                # Indent the body for visual clarity
                for bline in ref["body"].splitlines():
                    lines.append(f"  {bline}")
            else:
                lines.append(f"  {ref_name} ({ref_kind}): {ref['signature']}")

    # Class-level data references
    if class_data_refs:
        lines.append("")
        lines.append("--- Class-level data ---")
        for ref in class_data_refs:
            lines.append(f"\n  {ref['name']}:")
            for cline in ref["content"].splitlines():
                lines.append(f"  {cline}")

    # Cross-file references
    if cross_file_refs:
        lines.append("")
        lines.append("--- Cross-file references ---")
        for ref in cross_file_refs:
            sig = ref.get("signature", "")
            if sig:
                lines.append(f"  {ref['name']} (from {ref['module']}): {sig}")
            else:
                lines.append(f"  {ref['name']} (from {ref['module']})")

    # f3d round — upward callers (who calls THIS symbol from other
    # files). Critical for one-hop upstream walking when evidence
    # suggests a data-flow contract mismatch. The receiver of bad
    # data reports the error; the producer is in one of these
    # files. Showing the call site lets diagnose see exactly the
    # boundary and the surrounding context where the mismatch was
    # introduced. Line numbers ARE useful here — they identify the
    # specific call site, not a body range.
    if upward_callers:
        lines.append("")
        lines.append("--- Called by (upstream) ---")
        for caller in upward_callers:
            file_part = caller.get("file", "")
            line_part = caller.get("line", "")
            snippet = caller.get("snippet", "")
            resp = caller.get("responsibility", "")
            if resp:
                lines.append(f"  {file_part}:{line_part}  ({resp})")
            else:
                lines.append(f"  {file_part}:{line_part}")
            if snippet:
                lines.append(f"    {snippet}")

    lines.append("")
    lines.append("(End of observation.)")

    return "\n".join(lines)
