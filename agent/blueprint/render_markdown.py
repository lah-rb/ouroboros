"""Markdown renderer — produces blueprint.md from the BlueprintIR.

This file is designed to be ingested by AI developers as context.
It should be scannable and cross-referenced without being overwhelming.
"""

from __future__ import annotations

from agent.blueprint.ir import (
    BlueprintIR,
    FlowIR,
    StepIR,
    ActionIR,
    ContextKeyIR,
    TemplateIR,
)

# ── Symbol Constants ──────────────────────────────────────────────────

# Data Flow
SYM_REQUIRED_INPUT = "○"
SYM_OPTIONAL_INPUT = "◑"
SYM_PUBLISHED = "●"
SYM_TERMINAL = "◆"

# Step Type
SYM_INFERENCE = "▷"
SYM_ACTION = "□"
SYM_SUBFLOW = "↳"
SYM_TAIL_CALL = "⟲"
SYM_NOOP = "∅"

# Resolver
SYM_RULE = "⑂"
SYM_LLM_MENU = "☰"

# Effects (Egyptian Hieroglyphs)
SYM_FILE_SYSTEM = "𓉗"
SYM_PERSIST_WRITE = "𓇴→"
SYM_PERSIST_READ = "→𓇴"
SYM_NOTES = "𓇆"
SYM_FRUSTRATION = "𓁿"
SYM_SUBPROCESS = "⌘"
SYM_INFERENCE_CALL = "⟶"

# Gates
SYM_GATE_OPEN = "𓉫"
SYM_GATE_CLOSED = "𓉪"


def render_markdown(ir: BlueprintIR) -> str:
    """Render the complete BlueprintIR as a Markdown document.

    Returns:
        The complete Markdown string.
    """
    sections: list[str] = []

    sections.append(_render_header(ir))
    sections.append(_render_legend())
    sections.append(_render_system_diagrams(ir))
    sections.append(_render_system_context(ir))
    sections.append(_render_mission_lifecycle(ir))
    sections.append(_render_flow_catalog(ir))
    sections.append(_render_context_dictionary(ir))
    sections.append(_render_action_registry(ir))
    sections.append(_render_template_registry(ir))

    return "\n\n".join(sections)


def _arch_flow_count(ir: BlueprintIR) -> int:
    """Count architectural flows (excluding test flows)."""
    return sum(1 for f in ir.flows.values() if f.category != "test")


# ── Header ────────────────────────────────────────────────────────────


def _render_header(ir: BlueprintIR) -> str:
    arch_count = _arch_flow_count(ir)
    return f"""# Ouroboros Blueprint

Generated: {ir.meta.generated_at}
Source Hash: `{ir.meta.source_hash[:12]}…`
Flows: **{arch_count}** | Actions: **{ir.meta.action_count}** | Context Keys: **{ir.meta.context_key_count}**"""


# ── Legend ─────────────────────────────────────────────────────────────


def _render_legend() -> str:
    return """## Legend

### Data Flow Symbols
| Symbol | Name | Meaning |
|--------|------|---------|
| ○ | Required Input | Data the flow cannot execute without |
| ◑ | Optional Input | Data that enriches but isn't required |
| ● | Published Output | Context key added to accumulator |
| ◆ | Terminal Status | Terminal outcome of a flow |

### Step Type Symbols
| Symbol | Name | Meaning |
|--------|------|---------|
| ▷ | Inference Step | Step that invokes LLM inference |
| □ | Action Step | Generic computation (registered callable) |
| ↳ | Sub-flow Invocation | Delegates to a child flow |
| ⟲ | Tail-call | Continues execution in another flow |
| ∅ | Noop Step | Pass-through for routing logic only |

### Resolver Symbols
| Symbol | Name | Meaning |
|--------|------|---------|
| ⑂ | Rule Resolver | Deterministic condition evaluation, no inference cost |
| ☰ | LLM Menu Resolver | Constrained LLM choice, one inference call |

### Effect & System Symbols
| Symbol | Name | Meaning |
|--------|------|---------|
| 𓉗 | File System | File read/write operations |
| 𓇴→ | Persistence Write | Save to persistent state |
| →𓇴 | Persistence Read | Load from persistent state |
| 𓇆 | Notes/Learnings | Accumulated observations and learnings |
| 𓁿 | Frustration | Emotional weight of accumulated failure |
| ⌘ | Subprocess | Terminal/shell execution |
| ⟶ | Inference Call | Token flow to/from model |

### Gate Symbols
| Symbol | Name | Meaning |
|--------|------|---------|
| 𓉫 | Gate Open | Checkpoint passed, path available |
| 𓉪 | Gate Closed | Checkpoint failed, path blocked |"""


# ── System Diagrams ───────────────────────────────────────────────────


def _render_system_diagrams(ir: BlueprintIR) -> str:
    from agent.blueprint.mermaid import mission_control_mermaid, system_view_mermaid

    lines = ["## System Diagrams", ""]
    lines.append("### mission_control — Agent Orchestration Hub")
    lines.append("")
    lines.append("```mermaid")
    lines.append(mission_control_mermaid(ir))
    lines.append("```")
    lines.append("")
    lines.append("### All Flows — System Architecture View")
    lines.append("")
    lines.append("```mermaid")
    lines.append(system_view_mermaid(ir))
    lines.append("```")

    return "\n".join(lines)


# ── System Context ────────────────────────────────────────────────────


def _render_system_context(ir: BlueprintIR) -> str:
    # Count categories
    orchestrator_count = sum(1 for f in ir.flows.values() if f.category == "orchestrator")
    task_count = sum(1 for f in ir.flows.values() if f.category == "task")
    sub_flow_count = sum(1 for f in ir.flows.values() if f.category == "sub_flow")
    other_count = sum(1 for f in ir.flows.values() if f.category in ("unknown", "test"))

    return f"""## System Context

**Ouroboros** is a flow-driven autonomous coding agent backed by LLMVP local inference.
It operates as a pure GraphQL client — all inference flows through `localhost:8000/graphql`.

### Actors
- **Shop Director (User)** — Sets missions, checks in periodically via CLI.
- **Junior Developer (Local Model)** — Runs continuously via LLMVP, follows structured flows.
- **Senior Developer (External API)** — Consulted on escalation (design pending).

### Subsystem Boundaries
- **Flow Engine** — Declarative CUE graphs with typed I/O and explicit transitions.
- **Effects Interface** — Swappable protocol for all side effects (file I/O, subprocess, inference, persistence).
- **Persistence** — File-backed JSON in `.agent/` with atomic writes.
- **LLMVP** — External GraphQL inference server (separate project).

### Flow Inventory
| Category | Count |
|----------|-------|
| Orchestrator flows | {orchestrator_count} |
| Task flows | {task_count} |
| Sub-flows | {sub_flow_count} |
| Other | {other_count} |
| **Total** | **{ir.meta.flow_count}** |"""


# ── Mission Lifecycle ─────────────────────────────────────────────────


def _render_mission_lifecycle(ir: BlueprintIR) -> str:
    mc = ir.flows.get("mission_control")
    if not mc:
        return "## Mission Lifecycle\n\n*mission_control flow not found.*"

    lines = ["## Mission Lifecycle", ""]
    lines.append(
        "`mission_control` is the hub flow orchestrating the entire agent lifecycle."
    )
    lines.append(
        "Child task flows tail-call back to `mission_control` on completion, creating a continuous cycle."
    )
    lines.append("")

    # Show steps in order
    lines.append("### mission_control Steps")
    lines.append("")
    for step_name, step_ir in mc.steps.items():
        sym = _step_type_symbol(step_ir)
        resolver_sym = _resolver_symbol(step_ir)
        desc = step_ir.description or step_name
        line = f"- {sym} **{step_name}** {resolver_sym} — {desc}"
        if step_ir.tail_call_target:
            line += f" {SYM_TAIL_CALL} → `{step_ir.tail_call_target}`"
        if step_ir.is_terminal:
            line += f" {SYM_TERMINAL} `{step_ir.terminal_status}`"
        lines.append(line)

    # Show tail-call targets
    lines.append("")
    lines.append("### Tail-Call Targets (flows that return to mission_control)")
    lines.append("")
    incoming = [
        e
        for e in ir.dependency_graph.flow_edges
        if e.target == "mission_control" and e.edge_type == "tail_call"
    ]
    if incoming:
        for edge in sorted(incoming, key=lambda e: e.source):
            lines.append(
                f"- `{edge.source}` → `mission_control` (from step `{edge.from_step}`)"
            )
    else:
        lines.append("*No incoming tail-calls found.*")

    return "\n".join(lines)


# ── Flow Catalog ──────────────────────────────────────────────────────


def _render_flow_catalog(ir: BlueprintIR) -> str:
    lines = ["## Flow Catalog", ""]

    categories = [
        ("Orchestrator Flows", "orchestrator"),
        ("Task Flows", "task"),
        ("Sub-flows", "sub_flow"),
        ("Other Flows", "unknown"),
    ]

    for heading, category in categories:
        flows = [f for f in ir.flows.values() if f.category == category]
        if not flows:
            continue

        lines.append(f"### {heading}")
        lines.append("")

        for flow_ir in sorted(flows, key=lambda f: f.name):
            lines.append(_render_flow_card(flow_ir, ir))
            lines.append("")

    return "\n".join(lines)


def _render_flow_card(flow_ir: FlowIR, ir: BlueprintIR) -> str:
    """Render a single flow card in Markdown."""
    lines: list[str] = []

    # Header
    lines.append(f"#### {flow_ir.name} (v{flow_ir.version})")
    lines.append(f"*{flow_ir.description.strip()}*")
    lines.append("")

    # Context Contract
    contract_parts = []
    if flow_ir.context_tier:
        contract_parts.append(f"**Tier:** `{flow_ir.context_tier}`")
    if flow_ir.state_reads:
        reads = ", ".join(f"`{r}`" for r in flow_ir.state_reads[:6])
        more = f" (+{len(flow_ir.state_reads) - 6})" if len(flow_ir.state_reads) > 6 else ""
        contract_parts.append(f"**Reads:** {reads}{more}")
    if flow_ir.returns:
        ret_keys = ", ".join(f"`{k}`" for k in list(flow_ir.returns.keys())[:6])
        contract_parts.append(f"**Returns:** {ret_keys}")
    if contract_parts:
        lines.append(" · ".join(contract_parts))

    # Persona
    if flow_ir.known_personas:
        peers = ", ".join(f"`{p}`" for p in flow_ir.known_personas)
        lines.append(f"**Peers:** {peers}")

    # Inputs
    req_inputs = [
        f"{SYM_REQUIRED_INPUT} {i.name}" for i in flow_ir.inputs if i.required
    ]
    opt_inputs = [
        f"{SYM_OPTIONAL_INPUT} {i.name}" for i in flow_ir.inputs if not i.required
    ]
    all_inputs = req_inputs + opt_inputs
    if all_inputs:
        lines.append(f"**Inputs:** {' · '.join(all_inputs)}")

    # Terminal statuses
    if flow_ir.terminal_statuses:
        statuses = [f"{SYM_TERMINAL} {s}" for s in flow_ir.terminal_statuses]
        lines.append(f"**Terminal:** {' · '.join(statuses)}")

    # Published keys
    if flow_ir.publishes_to_parent:
        pubs = [f"{SYM_PUBLISHED} {k}" for k in flow_ir.publishes_to_parent[:10]]
        suffix = (
            f" (+{len(flow_ir.publishes_to_parent) - 10} more)"
            if len(flow_ir.publishes_to_parent) > 10
            else ""
        )
        lines.append(f"**Publishes:** {' · '.join(pubs)}{suffix}")

    # Sub-flows
    if flow_ir.sub_flows:
        sfs = [f"{SYM_SUBFLOW} {sf.flow}" for sf in flow_ir.sub_flows]
        lines.append(f"**Sub-flows:** {' · '.join(sfs)}")

    # Tail-calls
    if flow_ir.tail_calls:
        tcs = set(tc.target_flow for tc in flow_ir.tail_calls)
        tc_strs = [f"{SYM_TAIL_CALL} {t}" for t in sorted(tcs)]
        lines.append(f"**Tail-calls:** {' · '.join(tc_strs)}")

    # Effects summary (from declared effects on steps)
    effects = _summarize_flow_effects(flow_ir, ir)
    if effects:
        lines.append(f"**Effects:** {' · '.join(effects)}")

    # Stats
    stats = flow_ir.stats
    stats_parts = [f"{stats.step_count} steps"]
    if stats.inference_step_count > 0:
        stats_parts.append(
            f"{SYM_INFERENCE} {stats.estimated_inference_calls} inference"
        )
    if stats.rule_resolver_count > 0:
        stats_parts.append(f"{stats.rule_resolver_count} {SYM_RULE} rule")
    if stats.llm_menu_resolver_count > 0:
        stats_parts.append(f"{stats.llm_menu_resolver_count} {SYM_LLM_MENU} menu")
    lines.append(f"**Stats:** {' · '.join(stats_parts)}")

    # Prompt details for inference steps
    inference_steps = [
        s for s in flow_ir.steps.values() if s.action_type == "inference"
    ]
    if inference_steps:
        lines.append("")
        lines.append("**Prompts:**")
        for step in inference_steps:
            temp_str = ""
            if step.config and step.config.temperature is not None:
                temp_str = f", {step.config.temperature}"
            injects = ", ".join(f"{{← {inj}}}" for inj in step.prompt_injects[:5])
            more = (
                f" (+{len(step.prompt_injects) - 5} more)"
                if len(step.prompt_injects) > 5
                else ""
            )
            lines.append(
                f"- **{step.name}** {SYM_INFERENCE} ({temp_str.lstrip(', ')}): "
                f"{step.description or 'inference step'}"
            )
            if injects:
                lines.append(f"  Injects: {injects}{more}")

    # Mermaid diagram
    from agent.blueprint.mermaid import flow_ir_to_mermaid

    lines.append("")
    lines.append("```mermaid")
    lines.append(flow_ir_to_mermaid(flow_ir))
    lines.append("```")

    return "\n".join(lines)


def _summarize_flow_effects(flow_ir: FlowIR, ir: BlueprintIR) -> list[str]:
    """Build a summary of effects used by a flow's actions."""
    effects_set: set[str] = set()

    for step_ir in flow_ir.steps.values():
        # From declared effects on steps
        for eff in step_ir.effects:
            effects_set.add(str(eff))

        # From action registry introspection
        if step_ir.action_type == "action":
            action_ir = ir.actions.get(step_ir.action)
            if action_ir:
                for eu in action_ir.effects_used:
                    effects_set.add(eu)

        # Inference steps always use inference
        if step_ir.action_type == "inference":
            effects_set.add("inference")

    # Map effects to symbols
    effect_symbols: list[str] = []
    effect_map = {
        "read_file": f"{SYM_FILE_SYSTEM} file read",
        "write_file": f"{SYM_FILE_SYSTEM} file write",
        "list_directory": f"{SYM_FILE_SYSTEM} list dir",
        "search_files": f"{SYM_FILE_SYSTEM} search",
        "run_command": f"{SYM_SUBPROCESS} command",
        "run_inference": f"{SYM_INFERENCE_CALL} inference",
        "inference": f"{SYM_INFERENCE_CALL} inference",
        "load_mission": f"{SYM_PERSIST_READ} load mission",
        "save_mission": f"{SYM_PERSIST_WRITE} save mission",
        "read_events": f"{SYM_PERSIST_READ} read events",
        "push_event": f"{SYM_PERSIST_WRITE} push event",
        "save_artifact": f"{SYM_PERSIST_WRITE} save artifact",
        "read_state": f"{SYM_PERSIST_READ} read state",
        "write_state": f"{SYM_PERSIST_WRITE} write state",
        "start_terminal": f"{SYM_SUBPROCESS} terminal",
        "send_to_terminal": f"{SYM_SUBPROCESS} terminal cmd",
        "close_terminal": f"{SYM_SUBPROCESS} close terminal",
    }

    seen: set[str] = set()
    for eff in sorted(effects_set):
        symbol = effect_map.get(eff, eff)
        if symbol not in seen:
            seen.add(symbol)
            effect_symbols.append(symbol)

    return effect_symbols


# ── Context Dictionary ────────────────────────────────────────────────


def _render_context_dictionary(ir: BlueprintIR) -> str:
    lines = ["## Context Key Dictionary", ""]
    lines.append("| Key | Published By | Consumed By | Consumers | Audit Flags |")
    lines.append("|-----|-------------|-------------|-----------|-------------|")

    for key_name in sorted(ir.context_keys.keys()):
        key_ir = ir.context_keys[key_name]
        publishers = ", ".join(f"`{p.flow}.{p.step}`" for p in key_ir.published_by[:3])
        if len(key_ir.published_by) > 3:
            publishers += f" (+{len(key_ir.published_by) - 3})"

        consumers = ", ".join(f"`{c.flow}.{c.step}`" for c in key_ir.consumed_by[:3])
        if len(key_ir.consumed_by) > 3:
            consumers += f" (+{len(key_ir.consumed_by) - 3})"

        flags = ", ".join(key_ir.audit_flags) if key_ir.audit_flags else "—"
        lines.append(
            f"| `{key_name}` | {publishers} | {consumers} | {key_ir.consumer_count} | {flags} |"
        )

    return "\n".join(lines)


# ── Action Registry ───────────────────────────────────────────────────


def _render_action_registry(ir: BlueprintIR) -> str:
    lines = ["## Action Registry", ""]
    lines.append("| Action | Module | Effects Used | Referenced By |")
    lines.append("|--------|--------|-------------|---------------|")

    for name in sorted(ir.actions.keys()):
        action = ir.actions[name]
        effects = ", ".join(action.effects_used[:4]) if action.effects_used else "—"
        if len(action.effects_used) > 4:
            effects += f" (+{len(action.effects_used) - 4})"
        refs = ", ".join(f"`{r}`" for r in action.referenced_by[:3])
        if len(action.referenced_by) > 3:
            refs += f" (+{len(action.referenced_by) - 3})"
        if not refs:
            refs = "—"
        lines.append(f"| `{name}` | `{action.module}` | {effects} | {refs} |")

    return "\n".join(lines)


# ── Template Registry ─────────────────────────────────────────────────


def _render_template_registry(ir: BlueprintIR) -> str:
    if not ir.templates:
        return ""

    lines = ["## Step Templates", ""]
    lines.append("| Template | Action | Used By |")
    lines.append("|----------|--------|---------|")

    for name in sorted(ir.templates.keys()):
        template = ir.templates[name]
        action = template.base_config.get("action", "—")
        used = ", ".join(f"`{u}`" for u in template.used_by[:5])
        if len(template.used_by) > 5:
            used += f" (+{len(template.used_by) - 5})"
        if not used:
            used = "—"
        lines.append(f"| `{name}` | `{action}` | {used} |")

    return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────


def _step_type_symbol(step: StepIR) -> str:
    """Return the symbol for a step's type."""
    if step.action_type == "inference":
        return SYM_INFERENCE
    if step.action_type == "flow":
        return SYM_SUBFLOW
    if step.action_type == "noop":
        return SYM_NOOP
    return SYM_ACTION


def _resolver_symbol(step: StepIR) -> str:
    """Return the symbol for a step's resolver type."""
    if step.resolver.type == "rule":
        return SYM_RULE
    if step.resolver.type == "llm_menu":
        return SYM_LLM_MENU
    return ""
