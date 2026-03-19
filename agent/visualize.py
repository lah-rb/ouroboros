"""Flow visualizer — generates Mermaid and DOT diagrams from flow definitions.

Converts YAML flow definitions into visual graph representations for
auditing, documentation, and understanding execution paths.

Supports:
- Single flow diagrams (step graph with transitions)
- Cross-flow system view (all flows + tail-call/sub-flow edges)
- Mermaid output (GitHub/VS Code compatible)
- Graphviz DOT output (for SVG/PNG via `dot` CLI)
- SVG export via mmdc (Mermaid CLI) or dot (Graphviz)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any

from agent.models import FlowDefinition, StepDefinition

# ── Node Classification ───────────────────────────────────────────────


def _step_type(step_name: str, step_def: StepDefinition) -> str:
    """Classify a step for visual styling."""
    if step_def.terminal:
        return "terminal"
    if step_def.tail_call:
        return "tail_call"
    if step_def.action == "inference":
        return "inference"
    if step_def.action == "flow":
        return "subflow"
    return "action"


def _step_icon(step_type: str) -> str:
    """Get an icon for the step type."""
    return {
        "action": "🔵",
        "inference": "🧠",
        "subflow": "📦",
        "terminal": "🏁",
        "tail_call": "🚀",
    }.get(step_type, "⬜")


# ── Mermaid Text Sanitization ─────────────────────────────────────────

# Characters that Mermaid interprets as syntax when they appear in labels.
# Brackets, braces, parens, pipes, angle-brackets, and hash are all
# shape/link/comment delimiters in the Mermaid grammar.
_MERMAID_SPECIAL_CHARS: dict[str, str] = {
    "[": "⟦",
    "]": "⟧",
    "{": "⦃",
    "}": "⦄",
    "(": "⟮",
    ")": "⟯",
    "|": "¦",
    "<": "‹",
    ">": "›",
    "#": "♯",
}


def _sanitize_mermaid_text(text: str) -> str:
    """Replace characters that break Mermaid parsing with safe equivalents.

    Mermaid uses ``[ ] { } ( ) | < > #`` as syntax delimiters for node
    shapes, edge labels, comments, and directives. When these characters
    appear inside label text they cause parse errors. This function
    replaces each one with a visually similar Unicode character that
    Mermaid treats as plain text.

    Also normalises internal whitespace (newlines → spaces, collapse runs).
    """
    # Normalise whitespace first (multi-line YAML conditions)
    text = " ".join(text.split())
    for char, replacement in _MERMAID_SPECIAL_CHARS.items():
        text = text.replace(char, replacement)
    return text


# ── Mermaid Generation ────────────────────────────────────────────────


def _mermaid_node_shape(step_name: str, label: str, step_type: str) -> str:
    """Generate a Mermaid node definition with type-appropriate shape."""
    # Escape quotes in labels
    safe_label = label.replace('"', "'")

    if step_type == "terminal":
        return f'    {step_name}(["{safe_label}"])'
    elif step_type == "tail_call":
        return f'    {step_name}[/"{safe_label}"\\]'
    elif step_type == "inference":
        return f'    {step_name}{{{{"{safe_label}"}}}}'
    elif step_type == "subflow":
        return f'    {step_name}[["{safe_label}"]]'
    else:
        return f'    {step_name}["{safe_label}"]'


def _mermaid_edge(
    from_step: str,
    to_step: str,
    label: str | None = None,
    style: str = "solid",
) -> str:
    """Generate a Mermaid edge."""
    safe_label = label.replace('"', "'") if label else None

    if style == "dashed":
        arrow = "-.->"
    elif style == "dotted":
        arrow = "-..->"
    else:
        arrow = "-->"

    if safe_label:
        # Sanitize Mermaid-special characters before truncating
        safe_label = _sanitize_mermaid_text(safe_label)
        # Truncate long conditions
        if len(safe_label) > 50:
            safe_label = safe_label[:47] + "..."
        return f"    {from_step} {arrow}|{safe_label}| {to_step}"
    return f"    {from_step} {arrow} {to_step}"


def flow_to_mermaid(flow_def: FlowDefinition) -> str:
    """Convert a single flow definition to Mermaid flowchart syntax.

    Args:
        flow_def: The flow definition to visualize.

    Returns:
        Mermaid flowchart source as a string.
    """
    lines: list[str] = []
    lines.append(f"flowchart TD")
    lines.append(f"    %% Flow: {flow_def.flow} (v{flow_def.version})")
    if flow_def.description:
        desc = flow_def.description.strip().replace("\n", " ")[:80]
        lines.append(f"    %% {desc}")
    lines.append("")

    # Generate nodes
    for step_name, step_def in flow_def.steps.items():
        stype = _step_type(step_name, step_def)
        icon = _step_icon(stype)
        desc = step_def.description[:40] if step_def.description else step_name
        label = f"{icon} {step_name}\\n{desc}"
        lines.append(_mermaid_node_shape(step_name, label, stype))

    lines.append("")

    # Mark entry point with thick border
    lines.append(f"    style {flow_def.entry} stroke-width:3px")
    lines.append("")

    # Generate edges from resolvers
    for step_name, step_def in flow_def.steps.items():
        if step_def.resolver:
            # Rule-based transitions
            if step_def.resolver.rules:
                for rule in step_def.resolver.rules:
                    cond = rule.condition
                    # Simplify common conditions for readability
                    if cond == "true":
                        cond = "always"
                    lines.append(_mermaid_edge(step_name, rule.transition, cond))

            # LLM menu options
            if step_def.resolver.options:
                for opt_name, opt_def in step_def.resolver.options.items():
                    if isinstance(opt_def, dict):
                        target = opt_def.get("target", opt_name)
                        desc = opt_def.get("description", opt_name)[:40]
                    else:
                        target = opt_name
                        desc = str(opt_def)[:40]
                    # Only add edge if target exists as a step
                    if target in flow_def.steps:
                        lines.append(
                            _mermaid_edge(step_name, target, desc, style="dashed")
                        )

        # Tail-call edges (inter-flow)
        if step_def.tail_call:
            tc_flow = step_def.tail_call.get("flow", "?")
            # Show as a note if the target is a template
            if "{{" in str(tc_flow):
                tc_flow = "dynamic"
            tc_node = f"tc_{step_name}"
            lines.append(f'    {tc_node}(("{tc_flow}"))')
            lines.append(f"    style {tc_node} fill:#f9f,stroke:#333")
            lines.append(_mermaid_edge(step_name, tc_node, "tail-call", style="dotted"))

    # Style terminal nodes
    for step_name, step_def in flow_def.steps.items():
        if step_def.terminal:
            status = step_def.status or "?"
            if status in ("completed", "success"):
                lines.append(f"    style {step_name} fill:#9f9,stroke:#393")
            elif status in ("aborted", "failed", "abandoned"):
                lines.append(f"    style {step_name} fill:#f99,stroke:#933")
            elif status == "escalated":
                lines.append(f"    style {step_name} fill:#ff9,stroke:#993")

    return "\n".join(lines)


def all_flows_to_mermaid(
    registry: dict[str, FlowDefinition],
    show_internal_steps: bool = False,
) -> str:
    """Generate a Mermaid diagram showing all flows and inter-flow relationships.

    Shows each flow as a subgraph with its steps (if show_internal_steps)
    or as a single node. Edges show tail-call and sub-flow relationships.

    Args:
        registry: Dictionary mapping flow names to FlowDefinition objects.
        show_internal_steps: If True, show steps within each flow subgraph.

    Returns:
        Mermaid flowchart source as a string.
    """
    lines: list[str] = []
    lines.append("flowchart TD")
    lines.append("    %% Ouroboros — Cross-Flow System View")
    lines.append("")

    # Collect inter-flow edges
    tail_call_edges: list[tuple[str, str, str]] = []  # (from_flow, to_flow, label)
    subflow_edges: list[tuple[str, str, str]] = []

    for flow_name, flow_def in sorted(registry.items()):
        for step_name, step_def in flow_def.steps.items():
            # Tail-call edges
            if step_def.tail_call:
                tc_flow = step_def.tail_call.get("flow", "")
                if "{{" not in str(tc_flow):
                    tail_call_edges.append((flow_name, tc_flow, f"{step_name}"))
                else:
                    tail_call_edges.append(
                        (flow_name, "dynamic_dispatch", f"{step_name}")
                    )

            # Sub-flow invocation edges
            if step_def.action == "flow" and step_def.flow:
                subflow_edges.append((flow_name, step_def.flow, f"{step_name}"))

    if show_internal_steps:
        # Each flow as a subgraph with its steps
        for flow_name, flow_def in sorted(registry.items()):
            lines.append(f"    subgraph {flow_name}[{flow_name}]")
            for step_name, step_def in flow_def.steps.items():
                stype = _step_type(step_name, step_def)
                icon = _step_icon(stype)
                node_id = f"{flow_name}__{step_name}"
                lines.append(f'        {node_id}["{icon} {step_name}"]')
            lines.append("    end")
            lines.append("")
    else:
        # Each flow as a single node
        for flow_name, flow_def in sorted(registry.items()):
            desc = flow_def.description.strip().replace("\n", " ")[:50]
            step_count = len(flow_def.steps)
            terminal_count = sum(1 for s in flow_def.steps.values() if s.terminal)
            tc_count = sum(1 for s in flow_def.steps.values() if s.tail_call)
            label = f"{flow_name}\\n{step_count} steps"
            lines.append(f'    {flow_name}["{label}"]')

    lines.append("")

    # Draw tail-call edges
    if tail_call_edges:
        lines.append("    %% Tail-call edges")
        for from_flow, to_flow, label in tail_call_edges:
            if to_flow not in registry and to_flow != "dynamic_dispatch":
                continue
            if to_flow == "dynamic_dispatch":
                lines.append(f'    dynamic_dispatch(("dynamic\\ndispatch"))')
                lines.append(f"    style dynamic_dispatch fill:#f9f,stroke:#333")
            safe = label[:30]
            lines.append(f"    {from_flow} -.->|{safe}| {to_flow}")
        lines.append("")

    # Draw sub-flow edges
    if subflow_edges:
        lines.append("    %% Sub-flow invocation edges")
        for from_flow, to_flow, label in subflow_edges:
            if to_flow not in registry:
                continue
            safe = label[:30]
            lines.append(f"    {from_flow} ==>|{safe}| {to_flow}")
        lines.append("")

    # Style mission_control specially
    if "mission_control" in registry:
        lines.append("    style mission_control fill:#ddf,stroke:#339,stroke-width:3px")

    return "\n".join(lines)


# ── Graphviz DOT Generation ──────────────────────────────────────────


def flow_to_dot(flow_def: FlowDefinition) -> str:
    """Convert a single flow definition to Graphviz DOT format.

    Args:
        flow_def: The flow definition to visualize.

    Returns:
        DOT source as a string.
    """
    lines: list[str] = []
    lines.append(f'digraph "{flow_def.flow}" {{')
    lines.append("    rankdir=TD;")
    lines.append('    node [fontname="Helvetica" fontsize=10];')
    lines.append('    edge [fontname="Helvetica" fontsize=8];')
    lines.append("")

    # Nodes
    for step_name, step_def in flow_def.steps.items():
        stype = _step_type(step_name, step_def)
        desc = step_def.description[:40] if step_def.description else ""
        label = f"{step_name}\\n{desc}"

        attrs: list[str] = [f'label="{label}"']

        if stype == "terminal":
            attrs.append("shape=doubleoctagon")
            status = step_def.status or ""
            if status in ("completed", "success"):
                attrs.append('fillcolor="#ccffcc" style=filled')
            elif status in ("aborted", "failed", "abandoned"):
                attrs.append('fillcolor="#ffcccc" style=filled')
            elif status == "escalated":
                attrs.append('fillcolor="#ffffcc" style=filled')
        elif stype == "tail_call":
            attrs.append("shape=parallelogram")
            attrs.append('fillcolor="#ffccff" style=filled')
        elif stype == "inference":
            attrs.append("shape=hexagon")
            attrs.append('fillcolor="#cceeff" style=filled')
        elif stype == "subflow":
            attrs.append("shape=box3d")
            attrs.append('fillcolor="#eeeeff" style=filled')
        else:
            attrs.append("shape=box")
            attrs.append('fillcolor="#ffffff" style=filled')

        # Bold border for entry step
        if step_name == flow_def.entry:
            attrs.append("penwidth=3")

        lines.append(f'    {step_name} [{", ".join(attrs)}];')

    lines.append("")

    # Edges
    for step_name, step_def in flow_def.steps.items():
        if step_def.resolver:
            if step_def.resolver.rules:
                for rule in step_def.resolver.rules:
                    cond = rule.condition
                    if cond == "true":
                        cond = "always"
                    elif len(cond) > 40:
                        cond = cond[:37] + "..."
                    safe_cond = cond.replace('"', '\\"')
                    lines.append(
                        f"    {step_name} -> {rule.transition} "
                        f'[label="{safe_cond}"];'
                    )

            if step_def.resolver.options:
                for opt_name, opt_def in step_def.resolver.options.items():
                    if isinstance(opt_def, dict):
                        target = opt_def.get("target", opt_name)
                    else:
                        target = opt_name
                    if target in flow_def.steps:
                        lines.append(
                            f"    {step_name} -> {target} "
                            f'[label="{opt_name}" style=dashed];'
                        )

        # Tail-call edges
        if step_def.tail_call:
            tc_flow = step_def.tail_call.get("flow", "?")
            if "{{" in str(tc_flow):
                tc_flow = "dynamic"
            tc_node = f"tc_{step_name}"
            lines.append(
                f'    {tc_node} [label="{tc_flow}" shape=oval '
                f'fillcolor="#ffccff" style=filled];'
            )
            lines.append(
                f"    {step_name} -> {tc_node} " f'[label="tail-call" style=dotted];'
            )

    lines.append("}")
    return "\n".join(lines)


# ── SVG Export ────────────────────────────────────────────────────────


def render_to_svg(
    source: str,
    source_format: str,
    output_path: str,
) -> None:
    """Render a Mermaid or DOT diagram source to an SVG file.

    For Mermaid sources, uses ``mmdc`` (Mermaid CLI / @mermaid-js/mermaid-cli).
    For DOT sources, uses ``dot`` (Graphviz).

    Args:
        source: The diagram source text (Mermaid or DOT).
        source_format: ``"mermaid"`` or ``"dot"``.
        output_path: Filesystem path for the output SVG.

    Raises:
        RuntimeError: If the required CLI tool is not installed or the
            render subprocess exits with a non-zero code.
    """
    output_path = os.path.realpath(output_path)

    if source_format == "mermaid":
        _render_mermaid_svg(source, output_path)
    elif source_format == "dot":
        _render_dot_svg(source, output_path)
    else:
        raise ValueError(f"Unsupported source_format: {source_format!r}")


def _render_mermaid_svg(source: str, output_path: str) -> None:
    """Render Mermaid source to SVG via ``mmdc``."""
    mmdc = shutil.which("mmdc")
    if mmdc is None:
        raise RuntimeError(
            "mmdc (Mermaid CLI) not found on PATH.\n"
            "Install it with:  npm install -g @mermaid-js/mermaid-cli"
        )

    tmpdir = tempfile.mkdtemp(prefix="ouroboros_viz_")
    try:
        input_file = os.path.join(tmpdir, "input.mmd")
        with open(input_file, "w", encoding="utf-8") as f:
            f.write(source)

        result = subprocess.run(
            [mmdc, "-i", input_file, "-o", output_path, "-b", "transparent"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"mmdc exited with code {result.returncode}: {detail}")
    finally:
        # Clean up temp directory
        shutil.rmtree(tmpdir, ignore_errors=True)


def _render_dot_svg(source: str, output_path: str) -> None:
    """Render DOT source to SVG via ``dot``."""
    dot = shutil.which("dot")
    if dot is None:
        raise RuntimeError(
            "dot (Graphviz) not found on PATH.\n"
            "Install it with:  brew install graphviz  (macOS)"
        )

    result = subprocess.run(
        [dot, "-Tsvg", "-o", output_path],
        input=source,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"dot exited with code {result.returncode}: {detail}")


def all_flows_to_dot(registry: dict[str, FlowDefinition]) -> str:
    """Generate a DOT diagram showing all flows and inter-flow relationships.

    Args:
        registry: Dictionary mapping flow names to FlowDefinition objects.

    Returns:
        DOT source as a string.
    """
    lines: list[str] = []
    lines.append('digraph "ouroboros_system" {')
    lines.append("    rankdir=TD;")
    lines.append('    node [fontname="Helvetica" fontsize=11 shape=box style=filled];')
    lines.append('    edge [fontname="Helvetica" fontsize=9];')
    lines.append("")

    # Flow nodes
    for flow_name, flow_def in sorted(registry.items()):
        step_count = len(flow_def.steps)
        label = f"{flow_name}\\n({step_count} steps)"

        if flow_name == "mission_control":
            color = "#ccccff"
            pw = "3"
        else:
            color = "#ffffff"
            pw = "1"

        lines.append(
            f'    {flow_name} [label="{label}" ' f'fillcolor="{color}" penwidth={pw}];'
        )

    lines.append("")

    # Edges
    seen_edges: set[tuple[str, str, str]] = set()
    for flow_name, flow_def in sorted(registry.items()):
        for step_name, step_def in flow_def.steps.items():
            if step_def.tail_call:
                tc_flow = step_def.tail_call.get("flow", "")
                if "{{" not in str(tc_flow) and tc_flow in registry:
                    edge_key = (flow_name, tc_flow, "tail-call")
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        lines.append(
                            f"    {flow_name} -> {tc_flow} "
                            f'[label="{step_name}" style=dashed];'
                        )

            if step_def.action == "flow" and step_def.flow:
                if step_def.flow in registry:
                    edge_key = (flow_name, step_def.flow, "subflow")
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        lines.append(
                            f"    {flow_name} -> {step_def.flow} "
                            f'[label="{step_name}" style=bold];'
                        )

    lines.append("}")
    return "\n".join(lines)
