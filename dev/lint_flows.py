"""Flow context linter — static analysis of compiled CUE flow definitions.

Operates directly on flows/compiled.json and action source code.
No dependency on the old YAML loader or BlueprintIR.

Checks (new):
  1. Action contract: action code reads context/param keys not declared by step
  2. Cycle guards: step graph cycles without meta.attempt or action-level guards
  3. Publish/consume chains: required context keys with no upstream publisher
  4. Prompt/parser contracts: LLM response field names vs parsing code

Checks (ported from blueprint/lint.py):
  5. Unused optional inputs: flow accepts optional inputs no step references
  6. Prompt conventions: inference prompts missing ✅/❌ examples
  7. Resolver conventions: rule conditions using string-match anti-patterns

Usage:
    python lint_flows.py [--verbose] [--compiled PATH]
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ── Result dataclass ─────────────────────────────────────────────────


@dataclass
class LintResult:
    level: str  # "ERROR" | "WARNING" | "INFO"
    flow: str
    step: str | None
    check: str
    message: str

    def __str__(self) -> str:
        loc = f"{self.flow}.{self.step}" if self.step else self.flow
        return f"  {self.level}: {loc} — {self.message}"


# ── Source introspection helpers ─────────────────────────────────────


def _find_enclosing_action(source: str, pos: int) -> str | None:
    """Find the action_* function that encloses a given source position."""
    best = None
    for fm in re.finditer(r"async def (action_\w+)", source[:pos]):
        best = fm.group(1)
    return best


def _extract_action_context_reads(action_dir: Path) -> dict[str, set[str]]:
    """Extract context keys each action function reads via step_input.context.get()."""
    reads: dict[str, set[str]] = {}
    for f in action_dir.glob("*.py"):
        if f.name in ("__init__.py", "registry.py"):
            continue
        source = f.read_text()
        for match in re.finditer(r"step_input\.context\.get\([\"'](\w+)[\"']", source):
            func = _find_enclosing_action(source, match.start())
            if func:
                reads.setdefault(func, set()).add(match.group(1))
    return reads


def _extract_action_param_reads(action_dir: Path) -> dict[str, set[str]]:
    """Extract param keys each action function reads via step_input.params.get()."""
    reads: dict[str, set[str]] = {}
    for f in action_dir.glob("*.py"):
        if f.name in ("__init__.py", "registry.py"):
            continue
        source = f.read_text()
        for match in re.finditer(r"step_input\.params\.get\([\"'](\w+)[\"']", source):
            func = _find_enclosing_action(source, match.start())
            if func:
                reads.setdefault(func, set()).add(match.group(1))
    return reads


def _extract_action_internal_guards(action_dir: Path) -> dict[str, list[str]]:
    """Extract context-based loop counters from action code.

    Recognizes patterns like:
        selection_turn = int(step_input.context.get("selection_turn", 0)) + 1
        if selection_turn > max_turns:

    Returns: func_name -> list of counter key names used as guards.
    """
    guards: dict[str, list[str]] = {}
    counter_re = re.compile(r'context\.get\(["\'](\w+)["\'].*\)\s*\+\s*1')
    for f in action_dir.glob("*.py"):
        if f.name in ("__init__.py", "registry.py"):
            continue
        source = f.read_text()
        for match in counter_re.finditer(source):
            func = _find_enclosing_action(source, match.start())
            if func:
                guards.setdefault(func, []).append(match.group(1))
    return guards


def _build_action_name_map(registry_path: Path) -> dict[str, str]:
    """Map registered action names to function names."""
    source = registry_path.read_text()
    mapping = {}
    for match in re.finditer(r'registry\.register\(["\'](\w+)["\'],\s*(\w+)', source):
        mapping[match.group(1)] = match.group(2)
    return mapping


# ── Check 1: Action contract violations ──────────────────────────────


def check_action_contracts(
    flows: dict,
    action_name_map: dict[str, str],
    context_reads: dict[str, set[str]],
    param_reads: dict[str, set[str]],
) -> list[LintResult]:
    """Flag steps where the action reads context/param keys the step doesn't declare.

    When _build_step_input filters the accumulator to declared keys only,
    undeclared reads silently get default values, causing incorrect behavior.
    """
    results = []

    for flow_name, flow_def in _iter_flows(flows):
        for step_name, step_def in flow_def["steps"].items():
            action = step_def.get("action", "")
            if action in ("inference", "flow", "noop"):
                continue

            func_name = action_name_map.get(action, "")
            if not func_name:
                continue

            # Check context reads
            declared_ctx = set(
                step_def.get("context", {}).get("required", [])
                + step_def.get("context", {}).get("optional", [])
            )
            action_ctx_reads = context_reads.get(func_name, set())
            undeclared_ctx = action_ctx_reads - declared_ctx
            if undeclared_ctx:
                results.append(
                    LintResult(
                        level="WARNING",
                        flow=flow_name,
                        step=step_name,
                        check="action_reads_undeclared_context",
                        message=(
                            f"action '{action}' reads context keys "
                            f"{sorted(undeclared_ctx)} not in step's "
                            f"context declaration "
                            f"{sorted(declared_ctx) or '(empty)'}"
                        ),
                    )
                )

            # Check param reads (INFO — params usually have safe defaults)
            declared_params = set(step_def.get("params", {}).keys())
            action_param_reads = param_reads.get(func_name, set())
            undeclared_params = action_param_reads - declared_params
            if undeclared_params:
                results.append(
                    LintResult(
                        level="INFO",
                        flow=flow_name,
                        step=step_name,
                        check="action_reads_undeclared_param",
                        message=(
                            f"action '{action}' reads param keys "
                            f"{sorted(undeclared_params)} not in step's "
                            f"params block "
                            f"{sorted(declared_params) or '(empty)'}"
                        ),
                    )
                )

    return results


# ── Check 2: Unguarded graph cycles ─────────────────────────────────


def check_unguarded_cycles(
    flows: dict,
    action_name_map: dict[str, str],
    internal_guards: dict[str, list[str]],
) -> list[LintResult]:
    """Detect cycles in step graphs that lack loop guards.

    A cycle is considered guarded if at least one step has:
      - A resolver condition referencing meta.attempt, OR
      - An action with an internal context-based counter guard
        (e.g., selection_turn in action_select_symbol_turn)

    Unguarded cycles are WARNING; they may still be safe if bounded
    by LLM behavior, but deserve scrutiny.
    """
    results = []

    # Build set of function names that have internal guards
    guarded_funcs: set[str] = {
        func for func, counters in internal_guards.items() if counters
    }

    for flow_name, flow_def in _iter_flows(flows):
        steps = flow_def["steps"]
        adj = _build_adjacency(steps)
        cycles = _find_cycles(adj)

        for cycle in cycles:
            guarded = False

            for step_name in cycle:
                step_def = steps.get(step_name, {})

                # Check meta.attempt in resolver conditions
                for rule in step_def.get("resolver", {}).get("rules", []):
                    if "meta.attempt" in rule.get("condition", ""):
                        guarded = True
                        break

                # Check action-level internal guards
                if not guarded:
                    action = step_def.get("action", "")
                    func = action_name_map.get(action, "")
                    if func in guarded_funcs:
                        guarded = True

                if guarded:
                    break

            if not guarded:
                cycle_str = " \u2192 ".join(cycle + [cycle[0]])
                results.append(
                    LintResult(
                        level="WARNING",
                        flow=flow_name,
                        step=cycle[0],
                        check="unguarded_cycle",
                        message=(
                            f"cycle {cycle_str} has no meta.attempt or "
                            f"action-level guard \u2014 potential infinite loop"
                        ),
                    )
                )

    return results


def _build_adjacency(steps: dict) -> dict[str, list[str]]:
    """Build step adjacency list from resolver transitions."""
    adj: dict[str, list[str]] = {}
    for step_name, step_def in steps.items():
        targets: list[str] = []
        resolver = step_def.get("resolver", {})
        for rule in resolver.get("rules", []):
            t = rule.get("transition", "")
            if t:
                targets.append(t)
        dt = resolver.get("default_transition", "")
        if dt:
            targets.append(dt)
        if resolver.get("options"):
            for opt_name, opt_def in resolver["options"].items():
                if isinstance(opt_def, dict):
                    targets.append(opt_def.get("target", opt_name))
                else:
                    targets.append(opt_name)
        adj[step_name] = targets
    return adj


def _find_cycles(adj: dict[str, list[str]]) -> list[list[str]]:
    """Find all simple cycles in a directed graph. Returns deduplicated cycles."""
    cycles: list[list[str]] = []
    visited: set[str] = set()

    def dfs(node: str, path: list[str], on_stack: set[str]) -> None:
        visited.add(node)
        on_stack.add(node)
        path.append(node)

        for neighbor in adj.get(node, []):
            if neighbor not in visited:
                dfs(neighbor, path, on_stack)
            elif neighbor in on_stack:
                cycle_start = path.index(neighbor)
                cycles.append(path[cycle_start:])

        path.pop()
        on_stack.discard(node)

    for node in adj:
        if node not in visited:
            dfs(node, [], set())

    # Deduplicate: normalize each cycle to start with its smallest element
    seen: set[tuple[str, ...]] = set()
    unique: list[list[str]] = []
    for cycle in cycles:
        min_idx = cycle.index(min(cycle))
        normalized = tuple(cycle[min_idx:] + cycle[:min_idx])
        if normalized not in seen:
            seen.add(normalized)
            unique.append(list(normalized))

    return unique


# ── Check 3: Publish/consume chain validation ────────────────────────


def check_publish_consume_chains(flows: dict) -> list[LintResult]:
    """Verify required context keys have an upstream publisher.

    Also flags keys published but never consumed (INFO only \u2014 sub-flow
    exports are expected to be consumed by parent flows).
    """
    results = []

    for flow_name, flow_def in _iter_flows(flows):
        steps = flow_def["steps"]
        flow_inputs = _flow_input_keys(flow_def)

        all_published: dict[str, list[str]] = {}
        all_consumed: set[str] = set()

        for step_name, step_def in steps.items():
            for key in step_def.get("publishes", []):
                all_published.setdefault(key, []).append(step_name)

            ctx = step_def.get("context", {})
            all_consumed.update(ctx.get("required", []))
            all_consumed.update(ctx.get("optional", []))

            # $ref to context.* in input_map and pre_compute
            all_consumed.update(_extract_context_refs(step_def))

        available = set(all_published.keys()) | flow_inputs

        # Required keys with no publisher
        for step_name, step_def in steps.items():
            for key in step_def.get("context", {}).get("required", []):
                if key not in available:
                    results.append(
                        LintResult(
                            level="ERROR",
                            flow=flow_name,
                            step=step_name,
                            check="required_key_no_publisher",
                            message=(
                                f"requires context key '{key}' but no step "
                                f"or input in this flow publishes it"
                            ),
                        )
                    )

        # Published but never consumed (INFO)
        for key, publishers in all_published.items():
            if key not in all_consumed and key not in flow_inputs:
                results.append(
                    LintResult(
                        level="INFO",
                        flow=flow_name,
                        step=publishers[0],
                        check="published_never_consumed",
                        message=(
                            f"publishes '{key}' but no step in this flow "
                            f"consumes it (may be consumed by parent flow)"
                        ),
                    )
                )

    return results


# ── Check 4: Prompt/parser contract mismatches ───────────────────────


def check_prompt_parser_contracts(
    prompts_dir: Path, action_dir: Path
) -> list[LintResult]:
    """Check that JSON field names in prompt examples match parsing code.

    Scans prompt templates for JSON examples (in \u2705 CORRECT blocks),
    extracts the top-level keys, then checks if the corresponding
    action's parsing code reads those keys or different ones.
    """
    results = []

    # Map: (prompt_template_path, action_source_file, parser_function)
    # Manually maintained \u2014 these are flows where the LLM returns
    # structured JSON that gets parsed by a specific action.
    prompt_action_pairs = [
        (
            "quality_gate/summarize",
            "refinement_actions.py",
            "_parse_quality_summary",
        ),
        (
            "set_env/detect_tooling",
            "pipeline_actions.py",
            "action_persist_validation_env",
        ),
    ]

    for prompt_path, action_file, parser_func in prompt_action_pairs:
        prompt_file = prompts_dir / f"{prompt_path}.yaml"
        if not prompt_file.exists():
            continue

        prompt_text = prompt_file.read_text()
        prompt_keys = _extract_json_keys_from_prompt(prompt_text)
        if not prompt_keys:
            continue

        action_source_path = action_dir / action_file
        if not action_source_path.exists():
            continue

        action_source = action_source_path.read_text()
        parser_keys = _extract_parser_reads(action_source, parser_func)
        if not parser_keys:
            continue

        prompt_only = prompt_keys - parser_keys
        parser_only = parser_keys - prompt_keys

        if prompt_only or parser_only:
            details = []
            if prompt_only:
                details.append(
                    f"prompt defines {sorted(prompt_only)} " f"but parser ignores them"
                )
            if parser_only:
                details.append(
                    f"parser reads {sorted(parser_only)} "
                    f"but prompt doesn't define them"
                )

            results.append(
                LintResult(
                    level="WARNING",
                    flow=prompt_path.split("/")[0],
                    step=None,
                    check="prompt_parser_mismatch",
                    message=f"{prompt_path}: {'; '.join(details)}",
                )
            )

    return results


def _extract_json_keys_from_prompt(prompt_text: str) -> set[str]:
    """Extract top-level JSON keys from \u2705 CORRECT example blocks."""
    keys: set[str] = set()
    correct_pos = prompt_text.find("CORRECT")
    if correct_pos < 0:
        return keys

    rest = prompt_text[correct_pos:]
    json_match = re.search(r"\{[\s\S]*?\}", rest)
    if json_match:
        try:
            obj = json.loads(json_match.group())
            if isinstance(obj, dict):
                keys = set(obj.keys())
        except json.JSONDecodeError:
            pass

    return keys


def _extract_parser_reads(source: str, func_name: str) -> set[str]:
    """Extract dict keys read by .get("key") in a function.

    Correctly bounds the function body by finding the next def/async def
    at the same or lesser indentation level.
    """
    keys: set[str] = set()
    func_start = source.find(f"def {func_name}")
    if func_start < 0:
        func_start = source.find(func_name)
        if func_start < 0:
            return keys

    # Determine indentation of this function
    line_start = source.rfind("\n", 0, func_start) + 1
    indent = func_start - line_start

    # Find the next def/async def at same or lesser indentation
    pattern = re.compile(r"\n( *)(?:async )?def \w+")
    func_end = len(source)
    for m in pattern.finditer(source, func_start + 10):
        if len(m.group(1)) <= indent:
            func_end = m.start()
            break

    func_body = source[func_start:func_end]

    for match in re.finditer(r'\.get\(["\'](\w+)["\']', func_body):
        keys.add(match.group(1))

    return keys


# ── Check 5: Unused optional inputs (ported from blueprint/lint.py) ──


def check_unused_optional_inputs(flows: dict) -> list[LintResult]:
    """Warn about flow optional inputs that no step references.

    An optional input that nothing reads is dead weight in the flow
    definition and a signal that something was renamed or removed
    without updating the input declaration.
    """
    results = []

    for flow_name, flow_def in _iter_flows(flows):
        optional_inputs = set(flow_def.get("input", {}).get("optional", []))
        if not optional_inputs:
            continue

        referenced: set[str] = set()
        for step_name, step_def in flow_def["steps"].items():
            referenced.update(_extract_input_refs(step_def))

        unused = optional_inputs - referenced
        for inp in sorted(unused):
            results.append(
                LintResult(
                    level="INFO",
                    flow=flow_name,
                    step=None,
                    check="unused_optional_input",
                    message=(
                        f"accepts optional input '{inp}' " f"but no step references it"
                    ),
                )
            )

    return results


# ── Check 6: Prompt conventions (ported from blueprint/lint.py) ──────


def check_prompt_conventions(flows: dict, prompts_dir: Path) -> list[LintResult]:
    """Warn about inference prompts missing \u2705/\u274c output examples.

    Prompts that return structured JSON should include a \u2705 CORRECT
    example and a \u274c WRONG example to guide the LLM. This is a
    convention from PROMPTING_CONVENTIONS.md.
    """
    results = []

    for flow_name, flow_def in _iter_flows(flows):
        for step_name, step_def in flow_def["steps"].items():
            if step_def.get("action") != "inference":
                continue

            pt = step_def.get("prompt_template", {})
            template_path = pt.get("template", "")
            if not template_path:
                continue

            prompt_file = prompts_dir / f"{template_path}.yaml"
            if not prompt_file.exists():
                results.append(
                    LintResult(
                        level="WARNING",
                        flow=flow_name,
                        step=step_name,
                        check="prompt_template_missing",
                        message=(
                            f"references prompt template "
                            f"'{template_path}' but file not found"
                        ),
                    )
                )
                continue

            prompt_text = prompt_file.read_text()

            has_correct = "\u2705" in prompt_text or "CORRECT" in prompt_text
            has_wrong = "\u274c" in prompt_text or "WRONG" in prompt_text

            if not has_correct:
                results.append(
                    LintResult(
                        level="INFO",
                        flow=flow_name,
                        step=step_name,
                        check="prompt_missing_correct_example",
                        message=(
                            f"prompt '{template_path}' missing "
                            f"\u2705 CORRECT output example"
                        ),
                    )
                )

            if not has_wrong:
                results.append(
                    LintResult(
                        level="INFO",
                        flow=flow_name,
                        step=step_name,
                        check="prompt_missing_wrong_example",
                        message=(
                            f"prompt '{template_path}' missing "
                            f"\u274c WRONG output example"
                        ),
                    )
                )

    return results


# ── Check 7: Resolver conventions (ported from blueprint/lint.py) ────


def check_resolver_conventions(flows: dict) -> list[LintResult]:
    """Warn about resolver anti-patterns.

    - Rule conditions doing string-match on result.text (should use llm_menu)
    - Fallthrough catch-all rule that isn't last
    - Conditions referencing result keys from a noop action
    """
    results = []

    for flow_name, flow_def in _iter_flows(flows):
        for step_name, step_def in flow_def["steps"].items():
            resolver = step_def.get("resolver", {})
            rules = resolver.get("rules", [])
            if not rules:
                continue

            action = step_def.get("action", "")

            for i, rule in enumerate(rules):
                cond = rule.get("condition", "")

                # Anti-pattern: string match on result.text
                if "result.text" in cond:
                    results.append(
                        LintResult(
                            level="WARNING",
                            flow=flow_name,
                            step=step_name,
                            check="string_match_in_result_text",
                            message=(
                                f"rule condition uses string match on "
                                f"result.text ({cond[:60]}) \u2014 "
                                f"consider llm_menu resolver instead"
                            ),
                        )
                    )

                # Anti-pattern: catch-all "true" not in last position
                if cond == "true" and i < len(rules) - 1:
                    results.append(
                        LintResult(
                            level="WARNING",
                            flow=flow_name,
                            step=step_name,
                            check="catch_all_not_last",
                            message=(
                                f"catch-all condition 'true' is rule "
                                f"{i + 1}/{len(rules)} \u2014 rules after "
                                f"it are unreachable"
                            ),
                        )
                    )

                # Anti-pattern: noop action with result.X conditions
                if action == "noop" and "result." in cond:
                    results.append(
                        LintResult(
                            level="WARNING",
                            flow=flow_name,
                            step=step_name,
                            check="noop_result_condition",
                            message=(
                                f"noop action but condition references "
                                f"result ({cond[:60]}) \u2014 noop result "
                                f"is always empty"
                            ),
                        )
                    )

    return results


# ── Shared helpers ───────────────────────────────────────────────────


def _iter_flows(flows: dict):
    """Yield (flow_name, flow_def) for valid flow definitions."""
    for flow_name, flow_def in sorted(flows.items()):
        if isinstance(flow_def, dict) and "steps" in flow_def:
            yield flow_name, flow_def


def _flow_input_keys(flow_def: dict) -> set[str]:
    """Get all input key names (required + optional) for a flow."""
    inp = flow_def.get("input", {})
    return set(inp.get("required", []) + inp.get("optional", []))


def _extract_context_refs(step_def: dict) -> set[str]:
    """Extract context.* references from input_map and pre_compute $refs."""
    refs: set[str] = set()
    for v in step_def.get("input_map", {}).values():
        if isinstance(v, dict) and "$ref" in v:
            ref = v["$ref"]
            if ref.startswith("context."):
                refs.add(ref.split(".")[1])
    for pc in step_def.get("pre_compute", []):
        for v in pc.get("params", {}).values():
            if isinstance(v, dict) and "$ref" in v:
                ref = v["$ref"]
                if ref.startswith("context."):
                    refs.add(ref.split(".")[1])
    return refs


def _extract_input_refs(step_def: dict) -> set[str]:
    """Extract input.* references from params, input_map, pre_compute, prompt_template."""
    refs: set[str] = set()

    # $ref in params
    for v in step_def.get("params", {}).values():
        if isinstance(v, dict) and "$ref" in v:
            ref = v["$ref"]
            if ref.startswith("input."):
                refs.add(ref.split(".")[1])

    # $ref in input_map
    for v in step_def.get("input_map", {}).values():
        if isinstance(v, dict) and "$ref" in v:
            ref = v["$ref"]
            if ref.startswith("input."):
                refs.add(ref.split(".")[1])

    # $ref in pre_compute params
    for pc in step_def.get("pre_compute", []):
        for v in pc.get("params", {}).values():
            if isinstance(v, dict) and "$ref" in v:
                ref = v["$ref"]
                if ref.startswith("input."):
                    refs.add(ref.split(".")[1])

    # prompt_template input_keys
    pt = step_def.get("prompt_template", {})
    for key in pt.get("input_keys", []):
        refs.add(key)

    return refs


# ── Main ─────────────────────────────────────────────────────────────


def lint(
    compiled_path: str = "flows/compiled.json",
    action_dir: str = "agent/actions",
    prompts_dir: str = "prompts",
    verbose: bool = False,
) -> list[LintResult]:
    """Run all lint checks and return findings."""
    compiled = Path(compiled_path)
    actions = Path(action_dir)
    prompts = Path(prompts_dir)

    if not compiled.exists():
        print(f"ERROR: {compiled} not found")
        sys.exit(1)

    with open(compiled) as f:
        flows = json.load(f)

    # Build introspection data
    action_name_map = _build_action_name_map(actions / "registry.py")
    context_reads = _extract_action_context_reads(actions)
    param_reads = _extract_action_param_reads(actions)
    internal_guards = _extract_action_internal_guards(actions)

    results: list[LintResult] = []

    # Strategy 1: Action contract violations
    results.extend(
        check_action_contracts(flows, action_name_map, context_reads, param_reads)
    )

    # Strategy 2: Unguarded cycles
    results.extend(check_unguarded_cycles(flows, action_name_map, internal_guards))

    # Strategy 3: Publish/consume chains
    results.extend(check_publish_consume_chains(flows))

    # Strategy 4: Prompt/parser contracts
    results.extend(check_prompt_parser_contracts(prompts, actions))

    # Ported 5: Unused optional inputs
    results.extend(check_unused_optional_inputs(flows))

    # Ported 6: Prompt conventions
    results.extend(check_prompt_conventions(flows, prompts))

    # Ported 7: Resolver conventions
    results.extend(check_resolver_conventions(flows))

    if not verbose:
        results = [r for r in results if r.level != "INFO"]

    # Sort: ERROR first, then WARNING, then INFO; then by flow/step
    severity_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
    results.sort(
        key=lambda r: (
            severity_order.get(r.level, 3),
            r.flow,
            r.step or "",
        )
    )

    return results


def main() -> None:
    verbose = "--verbose" in sys.argv
    compiled = "flows/compiled.json"
    for i, arg in enumerate(sys.argv):
        if arg == "--compiled" and i + 1 < len(sys.argv):
            compiled = sys.argv[i + 1]

    results = lint(compiled_path=compiled, verbose=verbose)

    errors = [r for r in results if r.level == "ERROR"]
    warnings = [r for r in results if r.level == "WARNING"]
    infos = [r for r in results if r.level == "INFO"]

    for r in results:
        print(r)

    print()
    print(f"{'=' * 60}")
    print(
        f"Lint: {len(errors)} errors, {len(warnings)} warnings"
        + (f", {len(infos)} info" if verbose else "")
    )

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
