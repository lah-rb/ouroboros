"""Blueprint Analyzer — parses all YAML flows, action registry, and templates
to produce a BlueprintIR.

This is the heavy lift of the blueprint system. It:
1. Walks the flows directory to map flow names to source files and categories.
2. Loads all flows via agent/loader.py (with template merging).
3. Converts each FlowDefinition → FlowIR with full step details.
4. Introspects the action registry for module paths and effects usage.
5. Builds the context key cross-reference with audit flags.
6. Builds the dependency graph (tail-calls + sub-flows).
7. Extracts prompt inject points from Jinja2 templates.
8. Tracks step template usage.
9. Computes a source hash for cache invalidation.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

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
from agent.loader import load_all_flows, load_template_registry
from agent.models import FlowDefinition, StepDefinition


def analyze(flows_dir: str = "flows", agent_dir: str = "agent") -> BlueprintIR:
    """Parse all sources and produce the complete BlueprintIR.

    Args:
        flows_dir: Path to the flows directory.
        agent_dir: Path to the agent source directory.

    Returns:
        A fully populated BlueprintIR.
    """
    # 1. Build flow_name → source_file map and determine categories
    source_map = _build_source_map(flows_dir)

    # 2. Load all flows with template merging
    flow_defs = load_all_flows(flows_dir)

    # 3. Scan raw YAML for template usage (before merging strips 'use:')
    template_usage = _scan_template_usage(flows_dir)

    # 4. Load template registry
    template_registry = load_template_registry(flows_dir)

    # 5. Convert FlowDefinitions → FlowIRs
    flow_irs: dict[str, FlowIR] = {}
    for flow_name, flow_def in flow_defs.items():
        source_file = source_map.get(flow_name, "")
        category = _categorize_flow(flow_name, source_file)
        flow_irs[flow_name] = _flow_def_to_ir(flow_def, source_file, category)

    # 6. Introspect action registry
    action_irs = _introspect_actions(flow_irs)

    # 7. Build context key cross-reference
    context_keys = _build_context_keys(flow_irs)

    # 8. Build dependency graph
    dep_graph = _build_dependency_graph(flow_irs)

    # 9. Build template IRs
    template_irs = _build_template_irs(template_registry, template_usage)

    # 10. Compute source hash
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


# ── Source Discovery ──────────────────────────────────────────────────


def _build_source_map(flows_dir: str) -> dict[str, str]:
    """Walk the flows directory and build a mapping of flow name → relative file path.

    This is needed because load_all_flows() doesn't preserve source file paths.
    We parse each YAML file's 'flow' field to get the flow name.
    """
    source_map: dict[str, str] = {}
    flows_path = Path(flows_dir)
    skip_names = {"registry.yaml", "step_templates.yaml"}

    if not flows_path.is_dir():
        return source_map

    for yaml_file in sorted(flows_path.rglob("*.yaml")):
        if yaml_file.name in skip_names:
            continue
        try:
            with open(yaml_file, "r") as f:
                raw = yaml.safe_load(f)
            if isinstance(raw, dict) and "flow" in raw:
                rel_path = str(yaml_file.relative_to(flows_path.parent))
                source_map[raw["flow"]] = rel_path
        except Exception:
            continue

    return source_map


def _categorize_flow(flow_name: str, source_file: str) -> str:
    """Determine a flow's category from its source file path.

    Categories:
    - "task" — flows/tasks/*.yaml
    - "shared" — flows/shared/*.yaml
    - "control" — flows/mission_control.yaml, flows/create_plan.yaml
    - "test" — flows/test_*.yaml
    """
    if not source_file:
        return "unknown"

    # Normalize separators
    path = source_file.replace("\\", "/")

    if "/tasks/" in path:
        return "task"
    if "/shared/" in path:
        return "shared"
    if flow_name.startswith("test_"):
        return "test"
    # Top-level flows that are control flows
    if flow_name in ("mission_control", "create_plan"):
        return "control"
    # Default: if it's at the top level of flows/, categorize by name
    if flow_name.startswith("test"):
        return "test"
    return "control"


# ── FlowDefinition → FlowIR Conversion ───────────────────────────────


def _flow_def_to_ir(
    flow_def: FlowDefinition,
    source_file: str,
    category: str,
) -> FlowIR:
    """Convert a loaded FlowDefinition into a FlowIR."""
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
            tc_flow = step_def.tail_call.get("flow", "")
            tc_input_map = step_def.tail_call.get("input_map", {})
            # Ensure input_map values are strings
            tc_input_map_str = {k: str(v) for k, v in tc_input_map.items()}
            tail_calls.append(
                TailCallIR(
                    target_flow=tc_flow,
                    from_step=step_name,
                    input_map=tc_input_map_str,
                )
            )

        # Collect sub-flow invocations
        if step_def.action == "flow" and step_def.flow:
            sf_input_map = step_def.input_map or {}
            sf_input_map_str = {k: str(v) for k, v in sf_input_map.items()}
            sub_flows.append(
                SubFlowIR(
                    flow=step_def.flow,
                    invoked_by_step=step_name,
                    input_map=sf_input_map_str,
                )
            )

    # Compute stats
    stats = _compute_flow_stats(steps)

    # Collect all keys published by any step (for publishes_to_parent)
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
    )


def _step_def_to_ir(
    step_name: str,
    step_def: StepDefinition,
    flow_def: FlowDefinition,
) -> StepIR:
    """Convert a StepDefinition into a StepIR."""
    # Determine action type
    action_type = _classify_action_type(step_def.action)

    # Extract prompt injects
    prompt_injects: list[str] = []
    if step_def.prompt:
        prompt_injects = extract_injects(step_def.prompt)

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
        tail_call_target = step_def.tail_call.get("flow", "")

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
        prompt_injects=prompt_injects,
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
    )


# ── Prompt Inject Extraction ──────────────────────────────────────────


def extract_injects(prompt: str) -> list[str]:
    """Extract Jinja2 variable references from a prompt template.

    Matches {{ ... }} patterns, strips whitespace. Deduplicates while
    preserving order.
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
            # Skip template expressions that can't be resolved statically
            if "{{" not in target:
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
    """Scan raw YAML files for 'use: template_name' declarations.

    Returns a mapping of template_name → ["flow.step", ...].
    This must be done on raw YAML because load_all_flows() strips
    the 'use' field after merging.
    """
    usage: dict[str, list[str]] = {}
    flows_path = Path(flows_dir)
    skip_names = {"registry.yaml", "step_templates.yaml"}

    if not flows_path.is_dir():
        return usage

    for yaml_file in sorted(flows_path.rglob("*.yaml")):
        if yaml_file.name in skip_names:
            continue
        try:
            with open(yaml_file, "r") as f:
                raw = yaml.safe_load(f)
            if not isinstance(raw, dict) or "flow" not in raw:
                continue

            flow_name = raw["flow"]
            steps = raw.get("steps", {})
            if not isinstance(steps, dict):
                continue

            for step_name, step_def in steps.items():
                if isinstance(step_def, dict) and "use" in step_def:
                    template_name = step_def["use"]
                    ref = f"{flow_name}.{step_name}"
                    usage.setdefault(template_name, []).append(ref)
        except Exception:
            continue

    return usage


def _build_template_irs(
    template_registry: Any,
    template_usage: dict[str, list[str]],
) -> dict[str, TemplateIR]:
    """Build TemplateIR entries from the loaded template registry."""
    template_irs: dict[str, TemplateIR] = {}

    for name, template in template_registry.templates.items():
        # Build a base_config dict from the template's fields
        base_config: dict[str, Any] = {}
        if template.action:
            base_config["action"] = template.action
        if template.description:
            base_config["description"] = template.description
        if template.context:
            base_config["context"] = template.context
        if template.params:
            base_config["params"] = template.params
        if template.config:
            base_config["config"] = template.config
        if template.flow:
            base_config["flow"] = template.flow
        if template.input_map:
            base_config["input_map"] = template.input_map
        if template.publishes:
            base_config["publishes"] = template.publishes

        template_irs[name] = TemplateIR(
            name=name,
            base_config=base_config,
            used_by=template_usage.get(name, []),
        )

    return template_irs


# ── Source Hash ───────────────────────────────────────────────────────


def _compute_source_hash(flows_dir: str, agent_dir: str) -> str:
    """Compute a SHA-256 hash of all input files for cache invalidation.

    Includes all YAML files in flows_dir and the action registry source.
    """
    hasher = hashlib.sha256()
    flows_path = Path(flows_dir)

    # Hash all YAML files
    if flows_path.is_dir():
        for yaml_file in sorted(flows_path.rglob("*.yaml")):
            try:
                content = yaml_file.read_bytes()
                hasher.update(yaml_file.name.encode())
                hasher.update(content)
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
