"""Flow contract linter — static analysis of flow YAML definitions.

Analyzes all flows via the blueprint IR and reports warnings about
contract violations: unused inputs, orphaned publishes, broken
context chains, prompt convention issues, and resolver anti-patterns.

Advisory only — does not block mission creation.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from agent.blueprint.ir import BlueprintIR, FlowIR, StepIR


@dataclass
class LintResult:
    """A single lint finding."""

    level: str  # "ERROR" | "WARNING" | "INFO"
    flow: str
    step: str | None
    check: str  # Short check identifier
    message: str

    def __str__(self) -> str:
        loc = f"{self.flow}.{self.step}" if self.step else self.flow
        return f"{self.level}: {loc} — {self.message}"


# ── Main entry point ──────────────────────────────────────────────────


def lint_flows(ir: BlueprintIR, verbose: bool = False) -> list[LintResult]:
    """Run all lint checks against the blueprint IR.

    Args:
        ir: The full BlueprintIR from the analyzer.
        verbose: If True, include INFO-level findings.

    Returns:
        List of LintResult findings sorted by severity.
    """
    results: list[LintResult] = []
    results.extend(_check_unused_optional_inputs(ir))
    results.extend(_check_published_never_consumed(ir))
    results.extend(_check_consumed_never_published(ir))
    results.extend(_check_resolver_conventions(ir))
    results.extend(_check_duplicate_class_defs())

    if not verbose:
        results = [r for r in results if r.level != "INFO"]

    # Sort: ERROR first, then WARNING, then INFO
    severity_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
    results.sort(key=lambda r: severity_order.get(r.level, 3))

    return results


# ── Check 1: Unused optional inputs ──────────────────────────────────


def _check_unused_optional_inputs(ir: BlueprintIR) -> list[LintResult]:
    """Warn about flow optional inputs that no step references."""
    import re

    # Pattern to extract input.<name> refs from resolver condition strings.
    # Matches: input.foo, input.get('foo'), input.get("foo").
    input_ref_patterns = [
        re.compile(r"\binput\.(\w+)"),
        re.compile(r"\binput\.get\(\s*['\"](\w+)['\"]"),
    ]

    results = []

    for flow_name, flow in ir.flows.items():
        if not isinstance(flow, FlowIR):
            continue

        optional_inputs = {inp.name for inp in flow.inputs if not inp.required}
        if not optional_inputs:
            continue

        # Collect all references across steps
        referenced: set[str] = set()
        for step in flow.steps.values():
            if not isinstance(step, StepIR):
                continue
            referenced.update(step.context_required)
            referenced.update(step.context_optional)
            # Check param template references
            for pkey in step.prompt_injects:
                if pkey.startswith("input."):
                    referenced.add(pkey[6:])
            # Pre-compute $ref:input.<n> references
            referenced.update(step.pre_compute_input_refs)
            # Step-level params $ref:input.<n> references
            referenced.update(step.params_input_refs)
            # Resolver rule conditions that reference input.<name>
            if step.resolver and step.resolver.rules:
                for rule in step.resolver.rules:
                    for pat in input_ref_patterns:
                        referenced.update(pat.findall(rule.condition))
            # LLM menu resolver prompts can also reference input.<name>
            if step.resolver and step.resolver.prompt:
                for pat in input_ref_patterns:
                    referenced.update(pat.findall(step.resolver.prompt))

        # Also scan sub-flow invocations and tail-calls for $ref:input.<x>
        # references — these are how dispatchable flows pass their inputs
        # down to sub-flows or back up to mission_control. input_map values
        # may be raw $ref strings or stringified dicts/lists (e.g., list
        # elements that contain $refs get rendered as Python-repr strings).
        _embedded_ref = re.compile(r"\$ref:input\.(\w+)|'\$ref':\s*'input\.(\w+)")
        for sub in flow.sub_flows:
            for v in sub.input_map.values():
                if isinstance(v, str):
                    for m in _embedded_ref.finditer(v):
                        referenced.add(m.group(1) or m.group(2))
        for tc in flow.tail_calls:
            for v in tc.input_map.values():
                if isinstance(v, str):
                    for m in _embedded_ref.finditer(v):
                        referenced.add(m.group(1) or m.group(2))

        for opt in optional_inputs:
            if opt not in referenced:
                results.append(
                    LintResult(
                        level="WARNING",
                        flow=flow_name,
                        step=None,
                        check="unused_optional_input",
                        message=f"accepts optional input '{opt}' but no step references it",
                    )
                )

    return results


# ── Check 2: Published-but-never-consumed context keys ───────────────


def _check_published_never_consumed(ir: BlueprintIR) -> list[LintResult]:
    """Warn about context keys published within a flow but never consumed."""
    results = []

    for flow_name, flow in ir.flows.items():
        if not isinstance(flow, FlowIR):
            continue

        # Collect all published keys and all consumed keys within this flow
        all_published: dict[str, str] = {}  # key -> step_name
        all_consumed: set[str] = set()

        for step_name, step in flow.steps.items():
            if not isinstance(step, StepIR):
                continue
            for key in step.publishes:
                all_published[key] = step_name
            all_consumed.update(step.context_required)
            all_consumed.update(step.context_optional)

        # Keys published to parent (terminal step outputs) are consumed externally
        parent_exports = set(flow.publishes_to_parent)

        for key, publisher_step in all_published.items():
            if key not in all_consumed and key not in parent_exports:
                results.append(
                    LintResult(
                        level="WARNING",
                        flow=flow_name,
                        step=publisher_step,
                        check="published_never_consumed",
                        message=f"publishes '{key}' but no step in this flow consumes it",
                    )
                )

    return results


# ── Check 3: Consumed-but-never-published context keys ───────────────


def _check_consumed_never_published(ir: BlueprintIR) -> list[LintResult]:
    """Error on required context keys that have no publisher upstream."""
    results = []

    for flow_name, flow in ir.flows.items():
        if not isinstance(flow, FlowIR):
            continue

        # Collect all published keys within this flow + flow inputs
        available_keys: set[str] = set()
        for inp in flow.inputs:
            available_keys.add(inp.name)
        for step in flow.steps.values():
            if not isinstance(step, StepIR):
                continue
            available_keys.update(step.publishes)

        # Check each step's required context
        for step_name, step in flow.steps.items():
            if not isinstance(step, StepIR):
                continue
            for key in step.context_required:
                if key not in available_keys:
                    results.append(
                        LintResult(
                            level="ERROR",
                            flow=flow_name,
                            step=step_name,
                            check="consumed_never_published",
                            message=f"requires '{key}' but no upstream step or input publishes it",
                        )
                    )

    return results


# ── Check 5: Resolver convention checks ──────────────────────────────


def _check_resolver_conventions(ir: BlueprintIR) -> list[LintResult]:
    """Warn about resolver anti-patterns."""
    results = []

    for flow_name, flow in ir.flows.items():
        if not isinstance(flow, FlowIR):
            continue

        for step_name, step in flow.steps.items():
            if not isinstance(step, StepIR):
                continue

            resolver = step.resolver
            if resolver.type == "rule" and resolver.rules:
                for rule in resolver.rules:
                    # Check for string-match on result.text (should be llm_menu)
                    cond = rule.condition
                    if "'in result.text" in cond or "in result.text" in cond:
                        results.append(
                            LintResult(
                                level="WARNING",
                                flow=flow_name,
                                step=step_name,
                                check="string_match_in_result_text",
                                message=(
                                    f"rule condition uses string match on result.text "
                                    f"({cond[:60]}...) — consider llm_menu resolver instead"
                                ),
                            )
                        )

    return results


# ── Check: Duplicate class definitions across agent/ ─────────────────


def _check_duplicate_class_defs() -> list[LintResult]:
    """Flag top-level class names defined in more than one file under agent/.

    Catches the trap where the same class name (e.g., FlowRuntimeError)
    exists in multiple modules — callers importing one file's version will
    not catch instances from the other, leading to silent exception-handler
    misses. Detection is a simple grep for `^class Name\\b`; nested classes
    are excluded by the leading-edge anchor.
    """
    results: list[LintResult] = []

    agent_root = Path(__file__).resolve().parent.parent
    if not agent_root.exists() or agent_root.name != "agent":
        return results

    class_pattern = re.compile(r"^class (\w+)\b", re.MULTILINE)
    # name -> list of file paths (relative to agent_root) declaring it
    occurrences: dict[str, list[str]] = defaultdict(list)

    for py in agent_root.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            source = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(py.relative_to(agent_root))
        for m in class_pattern.finditer(source):
            occurrences[m.group(1)].append(rel)

    for name, paths in sorted(occurrences.items()):
        if len(paths) > 1:
            results.append(
                LintResult(
                    level="WARNING",
                    flow="(agent)",
                    step=None,
                    check="duplicate_class_def",
                    message=(
                        f"class '{name}' defined in {len(paths)} files: "
                        f"{', '.join(sorted(paths))} — "
                        "caller using one import will not catch instances from the other"
                    ),
                )
            )

    return results
