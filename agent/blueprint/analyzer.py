"""Blueprint Analyzer — parses compiled.json flows, action registry, and prompt
templates to produce a BlueprintIR.

This is the heavy lift of the blueprint system. It:
1. Loads all flows from CUE-exported compiled.json.
2. Walks flows/cue/ to build a flow name → CUE source file map.
3. Converts each FlowDefinition → FlowIR with full step details.
4. Introspects the action registry for module paths and effects usage.
5. Builds the context key cross-reference with audit flags.
6. Builds the dependency graph (tail-calls + sub-flows).
7. Extracts prompt template references and pre-compute formatters.
8. Tracks step template usage from CUE source.
9. Computes a source hash for cache invalidation.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.blueprint.ir import (
    ActionIR,
    BlueprintIR,
    BlueprintMeta,
    ConfigIR,
    ConsumerIR,
    ContextKeyIR,
    DependencyGraphIR,
    FlowEdgeIR,
    FlowIR,
    FlowStatsIR,
    InputIR,
    KeyFlowIR,
    OptionIR,
    PublisherIR,
    ResolverIR,
    RuleIR,
    StepIR,
    SubFlowIR,
    TailCallIR,
    TemplateIR,
)
from agent.models import FlowDefinition, StepDefinition


def analyze(flows_dir: str = "flows", agent_dir: str = "agent") -> BlueprintIR:
    """Parse all sources and produce the complete BlueprintIR.

    Args:
        flows_dir: Path to the flows directory.
        agent_dir: Path to the agent source directory.

    Returns:
        A fully populated BlueprintIR.
    """
    # 1. Load all flows from compiled.json
    flow_defs, raw_flow_data = _load_flows_from_compiled(flows_dir)

    # 2. Build flow_name → CUE source file map
    source_map = _build_source_map(flows_dir)

    # 3. Scan CUE files for template usage (_templates.X &)
    template_usage = _scan_template_usage(flows_dir)

    # 4. Convert FlowDefinitions → FlowIRs
    flow_irs: dict[str, FlowIR] = {}
    for flow_name, flow_def in flow_defs.items():
        source_file = source_map.get(flow_name, "")
        category = _categorize_flow(flow_name, source_file)
        raw = raw_flow_data.get(flow_name, {})
        flow_irs[flow_name] = _flow_def_to_ir(flow_def, source_file, category, raw)

    # 5. Introspect action registry
    action_irs = _introspect_actions(flow_irs)

    # 6. Build context key cross-reference
    context_keys = _build_context_keys(flow_irs)

    # 7. Build dependency graph
    dep_graph = _build_dependency_graph(flow_irs)

    # 8. Build template IRs from CUE scan
    template_irs = _build_template_irs_from_cue(flows_dir, template_usage)

    # 9. Compute source hash
    source_hash = _compute_source_hash(flows_dir, agent_dir)

    # Assemble the BlueprintIR
    meta = BlueprintMeta(
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_hash=source_hash,
        flow_count=len(flow_irs),
        action_count=len(action_irs),
        context_key_count=len(context_keys),
    )

    return BlueprintIR(
        meta=meta,
        flows=flow_irs,
        actions=action_irs,
        context_keys=context_keys,
        templates=template_irs,
        dependency_graph=dep_graph,
    )


# ── Flow Loading ────────────────────────────────────────────────────


def _load_flows_from_compiled(flows_dir: str) -> tuple[dict[str, FlowDefinition], dict[str, dict]]:
    """Load all flows from CUE-exported compiled.json.

    Returns both parsed FlowDefinitions and the raw JSON dicts
    (for fields like flow_persona that aren't in the Pydantic model).
    """
    compiled_path = Path(flows_dir) / "compiled.json"
    flows: dict[str, FlowDefinition] = {}
    raw: dict[str, dict] = {}

    if not compiled_path.exists():
        return flows, raw

    with open(compiled_path) as f:
        data = json.load(f)

    if isinstance(data, dict):
        for name, flow_data in data.items():
            if isinstance(flow_data, dict) and "flow" in flow_data:
                try:
                    flow = FlowDefinition(**flow_data)
                    flows[flow.flow] = flow
                    raw[flow.flow] = flow_data
                except Exception:
                    continue

    return flows, raw


# ── Source Discovery ──────────────────────────────────────────────────


def _build_source_map(flows_dir: str) -> dict[str, str]:
    """Walk flows/cue/ and build a mapping of flow name → relative CUE file path.

    Parses each CUE file looking for the pattern `<name>: #FlowDefinition &`.
    """
    source_map: dict[str, str] = {}
    flows_path = Path(flows_dir)
    cue_dir = flows_path / "cue"

    if not cue_dir.is_dir():
        return source_map

    # Pattern: <flow_name>: #FlowDefinition &
    flow_def_pattern = re.compile(r"^(\w+):\s+#FlowDefinition\s+&", re.MULTILINE)

    for cue_file in sorted(cue_dir.glob("*.cue")):
        # Skip schema/utility files
        if cue_file.name in ("flow.cue", "prompt.cue", "lint.cue", "templates.cue"):
            continue
        try:
            content = cue_file.read_text(encoding="utf-8")
            matches = flow_def_pattern.findall(content)
            rel_path = str(cue_file.relative_to(flows_path.parent))
            for flow_name in matches:
                source_map[flow_name] = rel_path
        except Exception:
            continue

    return source_map


# Known flow categorizations for the consolidated CUE flow set.
_ORCHESTRATOR_FLOWS = {"mission_control", "design_and_plan", "revise_plan"}
_TASK_FLOWS = {"file_ops", "project_ops", "interact", "diagnose_issue", "research"}
_SUB_FLOWS = {
    "create",
    "rewrite",
    "patch",
    "prepare_context",
    "quality_gate",
    "run_in_terminal",
    "capture_learnings",
    "retrospective",
    "set_env",
}


def _categorize_flow(flow_name: str, source_file: str) -> str:
    """Determine a flow's category.

    Categories:
    - "orchestrator" — mission_control, design_and_plan, revise_plan
    - "task" — file_write, project_ops, interact, diagnose_issue, research
    - "sub_flow" — create_file, modify_file, prepare_context, etc.
    - "test" — test_* flows
    - "unknown" — unrecognized
    """
    if flow_name.startswith("test_") or flow_name.startswith("test"):
        return "test"
    if flow_name in _ORCHESTRATOR_FLOWS:
        return "orchestrator"
    if flow_name in _TASK_FLOWS:
        return "task"
    if flow_name in _SUB_FLOWS:
        return "sub_flow"
    return "unknown"


# ── FlowDefinition → FlowIR Conversion ───────────────────────────────


def _flow_def_to_ir(
    flow_def: FlowDefinition,
    source_file: str,
    category: str,
    raw: dict | None = None,
) -> FlowIR:
    """Convert a loaded FlowDefinition into a FlowIR."""
    raw = raw or {}
    # Build inputs
    inputs = []
    for name in flow_def.input.required:
        inputs.append(InputIR(name=name, required=True))
    for name in flow_def.input.optional:
        inputs.append(InputIR(name=name, required=False))

    # Build defaults config
    defaults = None
    if flow_def.defaults.config:
        defaults = ConfigIR(
            temperature=flow_def.defaults.config.get("temperature"),
            max_tokens=flow_def.defaults.config.get("max_tokens"),
        )

    # Build steps
    steps: dict[str, StepIR] = {}
    terminal_statuses: list[str] = []
    tail_calls: list[TailCallIR] = []
    sub_flows: list[SubFlowIR] = []

    for step_name, step_def in flow_def.steps.items():
        step_ir = _step_def_to_ir(step_name, step_def, flow_def)
        steps[step_name] = step_ir

        # Collect terminal statuses
        if step_ir.is_terminal and step_ir.terminal_status:
            if step_ir.terminal_status not in terminal_statuses:
                terminal_statuses.append(step_ir.terminal_status)

        # Collect tail-calls
        if step_def.tail_call:
            tc_flow_raw = step_def.tail_call.get("flow", "")
            tc_flow = _resolve_ref_display(tc_flow_raw)
            tc_input_map = step_def.tail_call.get("input_map", {})
            tc_input_map_str = {
                k: _resolve_ref_display(v) for k, v in tc_input_map.items()
            }
            tail_calls.append(
                TailCallIR(
                    target_flow=tc_flow,
                    from_step=step_name,
                    input_map=tc_input_map_str,
                    result_formatter=step_def.tail_call.get("result_formatter"),
                    result_keys=step_def.tail_call.get("result_keys", []),
                )
            )

        # Collect sub-flow invocations
        if step_def.action == "flow" and step_def.flow:
            sf_input_map = step_def.input_map or {}
            sf_input_map_str = {
                k: _resolve_ref_display(v) for k, v in sf_input_map.items()
            }
            sub_flows.append(
                SubFlowIR(
                    flow=step_def.flow,
                    invoked_by_step=step_name,
                    input_map=sf_input_map_str,
                )
            )

    # Compute stats
    stats = _compute_flow_stats(steps)

    # Collect all keys published by any step
    all_published: list[str] = []
    for step_ir in steps.values():
        for key in step_ir.publishes:
            if key not in all_published:
                all_published.append(key)

    return FlowIR(
        name=flow_def.flow,
        version=flow_def.version,
        description=flow_def.description,
        category=category,
        source_file=source_file,
        inputs=inputs,
        terminal_statuses=terminal_statuses,
        publishes_to_parent=all_published,
        tail_calls=tail_calls,
        sub_flows=sub_flows,
        defaults=defaults,
        steps=steps,
        stats=stats,
        # Context Contract Architecture
        context_tier=getattr(flow_def, "context_tier", "") or "",
        returns=getattr(flow_def, "returns", {}) or {},
        state_reads=getattr(flow_def, "state_reads", []) or [],
        # Persona (from raw JSON — not in Pydantic model)
        flow_persona=raw.get("flow_persona", ""),
        known_personas=raw.get("known_personas", []),
    )


def _resolve_ref_display(value: Any) -> str:
    """Convert a value that may be a $ref dict into a human-readable string.

    - Literal string/number/bool → str(value)
    - $ref dict → "$ref:context.dispatch_config.flow"
    - Other dicts/lists → str(value)
    """
    if isinstance(value, dict) and "$ref" in value:
        return f"$ref:{value['$ref']}"
    if isinstance(value, str):
        return value
    return str(value)


def _step_def_to_ir(
    step_name: str,
    step_def: StepDefinition,
    flow_def: FlowDefinition,
) -> StepIR:
    """Convert a StepDefinition into a StepIR."""
    # Determine action type
    action_type = _classify_action_type(step_def.action)

    # Extract prompt template reference
    prompt_template_id = None
    if step_def.prompt_template:
        prompt_template_id = step_def.prompt_template.template

    # Extract prompt injects from prompt_template keys or legacy inline prompt
    prompt_injects: list[str] = []
    if step_def.prompt_template:
        # Collect declared context_keys and input_keys
        prompt_injects.extend(
            f"context.{k}" for k in step_def.prompt_template.context_keys
        )
        prompt_injects.extend(f"input.{k}" for k in step_def.prompt_template.input_keys)
    elif step_def.prompt:
        prompt_injects = _extract_jinja2_injects(step_def.prompt)

    # Extract pre-compute formatter names
    pre_compute_names: list[str] = []
    if step_def.pre_compute:
        for pc in step_def.pre_compute:
            name = pc.formatter if hasattr(pc, "formatter") else pc.get("formatter", "")
            if name:
                pre_compute_names.append(name)

    # Build config
    config = None
    if step_def.config:
        config = ConfigIR(
            temperature=step_def.config.get("temperature"),
            max_tokens=step_def.config.get("max_tokens"),
        )

    # Build resolver
    resolver = _build_resolver_ir(step_def)

    # Determine tail-call target
    tail_call_target = None
    if step_def.tail_call:
        tc_flow_raw = step_def.tail_call.get("flow", "")
        tail_call_target = _resolve_ref_display(tc_flow_raw)

    # Determine sub-flow target
    sub_flow_target = None
    if step_def.action == "flow" and step_def.flow:
        sub_flow_target = step_def.flow

    return StepIR(
        name=step_name,
        action=step_def.action,
        action_type=action_type,
        description=step_def.description,
        context_required=list(step_def.context.required),
        context_optional=list(step_def.context.optional),
        publishes=list(step_def.publishes),
        prompt=step_def.prompt,
        prompt_template=prompt_template_id,
        prompt_injects=prompt_injects,
        pre_compute=pre_compute_names,
        config=config,
        resolver=resolver,
        effects=list(step_def.effects),
        is_terminal=step_def.terminal,
        terminal_status=step_def.status,
        is_entry=(step_name == flow_def.entry),
        tail_call_target=tail_call_target,
        sub_flow_target=sub_flow_target,
    )


def _classify_action_type(action: str) -> str:
    """Classify an action string into its type category."""
    if action == "inference":
        return "inference"
    if action == "flow":
        return "flow"
    if action == "noop":
        return "noop"
    return "action"


def _build_resolver_ir(step_def: StepDefinition) -> ResolverIR:
    """Build a ResolverIR from a StepDefinition's resolver."""
    if not step_def.resolver:
        return ResolverIR(type="none")

    resolver_def = step_def.resolver
    resolver_type = resolver_def.type

    # Build rules
    rules = None
    if resolver_def.rules:
        rules = [
            RuleIR(condition=r.condition, transition=r.transition)
            for r in resolver_def.rules
        ]

    # Build options (for llm_menu)
    options = None
    if resolver_def.options:
        options = {}
        for opt_name, opt_def in resolver_def.options.items():
            if isinstance(opt_def, dict):
                options[opt_name] = OptionIR(
                    description=opt_def.get("description", ""),
                    target=opt_def.get("target"),
                    terminal=opt_def.get("terminal", False),
                )
            else:
                # Simple string description
                options[opt_name] = OptionIR(description=str(opt_def))

    return ResolverIR(
        type=resolver_type,
        rules=rules,
        options=options,
        prompt=resolver_def.prompt,
        publish_selection=resolver_def.publish_selection,
    )


# ── Prompt Inject Extraction ──────────────────────────────────────────


def _extract_jinja2_injects(prompt: str) -> list[str]:
    """Extract Jinja2 variable references from an inline prompt template.

    Matches {{ ... }} patterns, strips whitespace. Deduplicates while
    preserving order. Used only for legacy inline prompts.
    """
    raw = [m.strip() for m in re.findall(r"\{\{(.+?)\}\}", prompt, re.DOTALL)]
    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# Keep the old name as an alias for backward compatibility
extract_injects = _extract_jinja2_injects


# ── Flow Stats ────────────────────────────────────────────────────────


def _compute_flow_stats(steps: dict[str, StepIR]) -> FlowStatsIR:
    """Compute summary statistics for a flow from its steps."""
    step_count = len(steps)
    inference_count = 0
    rule_count = 0
    menu_count = 0

    for step in steps.values():
        if step.action_type == "inference":
            inference_count += 1
        if step.resolver.type == "rule":
            rule_count += 1
        elif step.resolver.type == "llm_menu":
            menu_count += 1

    # Estimate inference calls: each inference step = 1 call,
    # each llm_menu resolver = 1 call. Range accounts for branches.
    min_calls = max(1, inference_count) if inference_count > 0 else 0
    max_calls = inference_count + menu_count
    if min_calls == max_calls:
        estimated = str(min_calls)
    else:
        estimated = f"{min_calls}-{max_calls}"

    return FlowStatsIR(
        step_count=step_count,
        inference_step_count=inference_count,
        rule_resolver_count=rule_count,
        llm_menu_resolver_count=menu_count,
        estimated_inference_calls=estimated,
    )


# ── Action Registry Introspection ────────────────────────────────────


def _introspect_actions(flow_irs: dict[str, FlowIR]) -> dict[str, ActionIR]:
    """Introspect the action registry for module paths and effects usage.

    Also builds the referenced_by list from flow step usage.
    """
    from agent.actions.registry import build_action_registry

    registry = build_action_registry()
    action_irs: dict[str, ActionIR] = {}

    # Build referenced_by from flows
    action_references: dict[str, list[str]] = {}
    for flow_name, flow_ir in flow_irs.items():
        for step_name, step_ir in flow_ir.steps.items():
            action_name = step_ir.action
            # Skip special action types
            if action_name in ("inference", "flow", "noop"):
                continue
            ref = f"{flow_name}.{step_name}"
            action_references.setdefault(action_name, []).append(ref)

    # Introspect each registered action
    for action_name in registry.registered_actions:
        action_fn = registry.get(action_name)

        # Get module path
        module_path = ""
        try:
            mod = inspect.getmodule(action_fn)
            if mod:
                module_path = mod.__name__
        except Exception:
            pass

        # Scan source for effects method calls
        effects_used = _scan_effects_usage(action_fn)

        action_irs[action_name] = ActionIR(
            name=action_name,
            module=module_path,
            effects_used=effects_used,
            referenced_by=action_references.get(action_name, []),
        )

    return action_irs


def _scan_effects_usage(action_fn: Any) -> list[str]:
    """Scan an action function's source for effects interface method calls.

    Looks for patterns like `effects.read_file`, `step_input.effects.write_file`, etc.
    Returns a deduplicated list of method names.
    """
    try:
        source = inspect.getsource(action_fn)
    except (OSError, TypeError):
        return []

    # Match effects.method_name( or .effects.method_name(
    pattern = r"(?:effects|step_input\.effects)\.([\w]+)\s*\("
    matches = re.findall(pattern, source)

    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


# ── Context Key Cross-Reference ───────────────────────────────────────


def _build_context_keys(flow_irs: dict[str, FlowIR]) -> dict[str, ContextKeyIR]:
    """Build the context key dictionary by walking every flow's steps.

    Collects publishers and consumers, computes audit flags:
    - "never_consumed" — published but no consumer
    - "single_consumer" — consumed by only one flow
    - "conditionally_published" — published behind an llm_menu resolver
    """
    keys: dict[str, ContextKeyIR] = {}

    # Collect publishers and consumers
    for flow_name, flow_ir in flow_irs.items():
        for step_name, step_ir in flow_ir.steps.items():
            # Publishers
            for key in step_ir.publishes:
                if key not in keys:
                    keys[key] = ContextKeyIR(name=key)
                keys[key].published_by.append(
                    PublisherIR(flow=flow_name, step=step_name)
                )

            # Consumers (required)
            for key in step_ir.context_required:
                if key not in keys:
                    keys[key] = ContextKeyIR(name=key)
                keys[key].consumed_by.append(
                    ConsumerIR(flow=flow_name, step=step_name, required=True)
                )

            # Consumers (optional)
            for key in step_ir.context_optional:
                if key not in keys:
                    keys[key] = ContextKeyIR(name=key)
                keys[key].consumed_by.append(
                    ConsumerIR(flow=flow_name, step=step_name, required=False)
                )

    # Compute consumer_count and audit flags
    for key_ir in keys.values():
        key_ir.consumer_count = len(key_ir.consumed_by)

        # Audit: never_consumed
        if key_ir.consumer_count == 0 and len(key_ir.published_by) > 0:
            key_ir.audit_flags.append("never_consumed")

        # Audit: single_consumer — consumed by only one unique flow
        consuming_flows = set(c.flow for c in key_ir.consumed_by)
        if len(consuming_flows) == 1 and len(key_ir.published_by) > 0:
            key_ir.audit_flags.append("single_consumer")

        # Audit: conditionally_published — published in a step whose
        # predecessor has an llm_menu resolver (heuristic)
        for pub in key_ir.published_by:
            flow_ir = flow_irs.get(pub.flow)
            if flow_ir:
                step_ir = flow_ir.steps.get(pub.step)
                if step_ir and step_ir.resolver.type == "llm_menu":
                    key_ir.audit_flags.append("conditionally_published")
                    break

    return keys


# ── Dependency Graph ──────────────────────────────────────────────────


def _build_dependency_graph(flow_irs: dict[str, FlowIR]) -> DependencyGraphIR:
    """Build the dependency graph from tail-calls and sub-flow invocations."""
    flow_edges: list[FlowEdgeIR] = []
    key_flows: list[KeyFlowIR] = []

    for flow_name, flow_ir in flow_irs.items():
        # Tail-call edges
        for tc in flow_ir.tail_calls:
            target = tc.target_flow
            # Include static targets; skip dynamic $ref targets for graph edges
            if not target.startswith("$ref:"):
                flow_edges.append(
                    FlowEdgeIR(
                        source=flow_name,
                        target=target,
                        edge_type="tail_call",
                        from_step=tc.from_step,
                    )
                )

        # Sub-flow edges
        for sf in flow_ir.sub_flows:
            flow_edges.append(
                FlowEdgeIR(
                    source=flow_name,
                    target=sf.flow,
                    edge_type="sub_flow",
                    from_step=sf.invoked_by_step,
                )
            )

    # Build key flows — connect each context key's publishers to consumers
    # across flow boundaries. Only include keys that cross flow boundaries.
    all_keys = _build_context_keys(flow_irs)
    for key_name, key_ir in all_keys.items():
        if not key_ir.published_by:
            continue
        # Find consumers in different flows than any publisher
        publisher_flows = set(p.flow for p in key_ir.published_by)
        cross_flow_consumers = [
            c for c in key_ir.consumed_by if c.flow not in publisher_flows
        ]
        if cross_flow_consumers:
            # Use the first publisher as origin
            origin = key_ir.published_by[0]
            key_flows.append(
                KeyFlowIR(
                    key=key_name,
                    origin_flow=origin.flow,
                    origin_step=origin.step,
                    consumers=cross_flow_consumers,
                )
            )

    return DependencyGraphIR(flow_edges=flow_edges, key_flows=key_flows)


# ── Template Usage Tracking ───────────────────────────────────────────


def _scan_template_usage(flows_dir: str) -> dict[str, list[str]]:
    """Scan CUE flow files for '_templates.<name> &' declarations.

    Returns a mapping of template_name → ["flow.step", ...].
    """
    usage: dict[str, list[str]] = {}
    cue_dir = Path(flows_dir) / "cue"

    if not cue_dir.is_dir():
        return usage

    # Pattern: matches _templates.<template_name> anywhere in a step definition
    template_ref_pattern = re.compile(r"_templates\.(\w+)\s+&")

    for cue_file in sorted(cue_dir.glob("*.cue")):
        if cue_file.name in ("flow.cue", "prompt.cue", "lint.cue", "templates.cue"):
            continue
        try:
            content = cue_file.read_text(encoding="utf-8")
            # Find the flow name
            flow_match = re.search(
                r"^(\w+):\s+#FlowDefinition\s+&", content, re.MULTILINE
            )
            if not flow_match:
                continue
            flow_name = flow_match.group(1)

            # Find step blocks that reference templates
            # Pattern: <step_name>: ... _templates.<template> &
            step_template_pattern = re.compile(
                r"(\w+):\s+(?:#StepDefinition\s+&\s+)?_templates\.(\w+)\s+&"
            )
            for step_name, template_name in step_template_pattern.findall(content):
                ref = f"{flow_name}.{step_name}"
                if ref not in usage.get(template_name, []):
                    usage.setdefault(template_name, []).append(ref)
        except Exception:
            continue

    return usage


def _build_template_irs_from_cue(
    flows_dir: str,
    template_usage: dict[str, list[str]],
) -> dict[str, TemplateIR]:
    """Build TemplateIR entries by parsing templates.cue.

    Extracts template names and their base configurations from the CUE source.
    """
    template_irs: dict[str, TemplateIR] = {}
    templates_path = Path(flows_dir) / "cue" / "templates.cue"

    if not templates_path.exists():
        # Fall back: create entries from usage alone
        for template_name, refs in template_usage.items():
            template_irs[template_name] = TemplateIR(
                name=template_name,
                base_config={},
                used_by=refs,
            )
        return template_irs

    try:
        content = templates_path.read_text(encoding="utf-8")
    except Exception:
        return template_irs

    # Extract template names from _templates block
    # Look for top-level definitions: <name>: #StepDefinition & { or <name>: {
    template_def_pattern = re.compile(
        r"^\t(\w+):\s+(?:#StepDefinition\s+&\s+)?{",
        re.MULTILINE,
    )

    for match in template_def_pattern.finditer(content):
        template_name = match.group(1)
        # Extract action if present near the definition
        block_start = match.end()
        block_preview = content[block_start : block_start + 200]
        action_match = re.search(r'action:\s*"(\w+)"', block_preview)

        base_config: dict[str, Any] = {}
        if action_match:
            base_config["action"] = action_match.group(1)

        template_irs[template_name] = TemplateIR(
            name=template_name,
            base_config=base_config,
            used_by=template_usage.get(template_name, []),
        )

    return template_irs


# ── Source Hash ───────────────────────────────────────────────────────


def _compute_source_hash(flows_dir: str, agent_dir: str) -> str:
    """Compute a SHA-256 hash of all input files for cache invalidation.

    Includes CUE flow definitions, compiled.json, prompt templates,
    and the action registry source.
    """
    hasher = hashlib.sha256()
    flows_path = Path(flows_dir)

    # Hash compiled.json
    compiled = flows_path / "compiled.json"
    if compiled.is_file():
        try:
            hasher.update(compiled.read_bytes())
        except Exception:
            pass

    # Hash all CUE files
    cue_dir = flows_path / "cue"
    if cue_dir.is_dir():
        for cue_file in sorted(cue_dir.glob("*.cue")):
            try:
                hasher.update(cue_file.name.encode())
                hasher.update(cue_file.read_bytes())
            except Exception:
                continue

    # Hash the action registry source
    registry_path = Path(agent_dir) / "actions" / "registry.py"
    if registry_path.is_file():
        try:
            hasher.update(registry_path.read_bytes())
        except Exception:
            pass

    return hasher.hexdigest()
