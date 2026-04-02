"""Mermaid diagram generation with blueprint symbology.

Generates Mermaid flowchart source using the Ouroboros symbol set,
and renders to SVG via mmdc (Mermaid CLI).
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from typing import Any

from agent.blueprint.ir import BlueprintIR, FlowIR, StepIR

# ── Symbol Set ────────────────────────────────────────────────────────

SYM_INFERENCE = "▷"
SYM_ACTION = "□"
SYM_SUBFLOW = "↳"
SYM_TAIL_CALL = "⟲"
SYM_NOOP = "∅"
SYM_RULE = "⑂"
SYM_LLM_MENU = "☰"
SYM_TERMINAL = "◆"
SYM_REQUIRED = "○"
SYM_OPTIONAL = "◑"
SYM_PUBLISHED = "●"

# Mermaid-unsafe characters → safe Unicode replacements
_MERMAID_SPECIAL: dict[str, str] = {
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
    '"': "'",
}

# Custom Mermaid theme with forest green accents
MERMAID_THEME_CONFIG = """{
  "theme": "base",
  "themeVariables": {
    "primaryColor": "#e8f0e6",
    "primaryTextColor": "#1a1a1a",
    "primaryBorderColor": "#2d5a27",
    "lineColor": "#2d5a27",
    "secondaryColor": "#f0f4ef",
    "tertiaryColor": "#fafafa",
    "fontSize": "14px",
    "fontFamily": "Helvetica, Arial, sans-serif"
  }
}"""


def _sanitize(text: str) -> str:
    """Replace Mermaid-special characters with safe equivalents."""
    text = " ".join(text.split())
    for char, replacement in _MERMAID_SPECIAL.items():
        text = text.replace(char, replacement)
    return text


def _truncate(text: str, max_len: int = 40) -> str:
    """Truncate text with ellipsis."""
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


# ── Per-Flow Diagram ──────────────────────────────────────────────────


def _inline_legend() -> list[str]:
    """Generate a compact inline legend subgraph for diagrams."""
    return [
        '    subgraph Legend[" "]',
        f'        L1["{SYM_INFERENCE} Inference  {SYM_ACTION} Action  {SYM_SUBFLOW} Sub-flow  {SYM_NOOP} Noop"]',
        f'        L2["{SYM_RULE} Rule resolver  {SYM_LLM_MENU} LLM menu  {SYM_TERMINAL} Terminal  {SYM_TAIL_CALL} Tail-call"]',
        "    end",
        "    style Legend fill:#f5f5f5,stroke:#ccc,stroke-width:1px",
        "    style L1 fill:#f5f5f5,stroke:none,color:#555",
        "    style L2 fill:#f5f5f5,stroke:none,color:#555",
        "",
    ]


def flow_ir_to_mermaid(flow_ir: FlowIR) -> str:
    """Generate a Mermaid flowchart for a single flow using blueprint symbology.

    Args:
        flow_ir: The flow's IR representation.

    Returns:
        Mermaid flowchart source string.
    """
    lines: list[str] = []
    lines.append("flowchart TD")
    lines.append(f"    %% {flow_ir.name} v{flow_ir.version}")
    lines.append("")

    # Inline legend
    lines.extend(_inline_legend())

    # Generate nodes — concise labels for readability.
    # Node shape conveys type, symbol reinforces it.
    # Full descriptions are in the flow card below the diagram.
    for step_name, step_ir in flow_ir.steps.items():
        sym = _step_symbol(step_ir)
        resolver_sym = _resolver_label(step_ir)
        label = f"{sym} {step_name}{resolver_sym}"

        node_def = _mermaid_node(step_name, label, step_ir)
        lines.append(f"    {node_def}")

    lines.append("")

    # Entry step styling
    for step_name, step_ir in flow_ir.steps.items():
        if step_ir.is_entry:
            lines.append(f"    style {step_name} stroke-width:3px,stroke:#2d5a27")
            break
    lines.append("")

    # Generate edges from resolvers
    for step_name, step_ir in flow_ir.steps.items():
        if step_ir.resolver.type == "rule" and step_ir.resolver.rules:
            for rule in step_ir.resolver.rules:
                cond = rule.condition
                if cond == "true":
                    cond = "always"
                safe_cond = _sanitize(cond)
                lines.append(
                    f"    {step_name} -->|{SYM_RULE} {safe_cond}| {rule.transition}"
                )

        elif step_ir.resolver.type == "llm_menu" and step_ir.resolver.options:
            for opt_name, opt_def in step_ir.resolver.options.items():
                target = opt_def.target or opt_name
                # Use the option key name — short and cross-referenceable
                safe_name = _sanitize(opt_name)
                if target in flow_ir.steps:
                    lines.append(
                        f"    {step_name} -.->|{SYM_LLM_MENU} {safe_name}| {target}"
                    )

        # Tail-call edges
        if step_ir.tail_call_target:
            tc_target = step_ir.tail_call_target
            if "{{" in tc_target:
                tc_target = "dynamic"
            tc_node = f"tc_{step_name}"
            lines.append(f'    {tc_node}(("{SYM_TAIL_CALL} {_sanitize(tc_target)}"))')
            lines.append(f"    style {tc_node} fill:#f0e6f6,stroke:#663399")
            lines.append(f"    {step_name} -.->|tail-call| {tc_node}")

    lines.append("")

    # Style terminal nodes
    for step_name, step_ir in flow_ir.steps.items():
        if step_ir.is_terminal and step_ir.terminal_status:
            status = step_ir.terminal_status
            if status in ("completed", "success"):
                lines.append(f"    style {step_name} fill:#c8e6c9,stroke:#2d5a27")
            elif status in ("aborted", "failed", "abandoned"):
                lines.append(f"    style {step_name} fill:#ffcdd2,stroke:#b71c1c")
            elif status == "escalated":
                lines.append(f"    style {step_name} fill:#fff9c4,stroke:#f57f17")

    return "\n".join(lines)


def _step_symbol(step_ir: StepIR) -> str:
    """Get the blueprint symbol for a step's type."""
    return {
        "inference": SYM_INFERENCE,
        "flow": SYM_SUBFLOW,
        "noop": SYM_NOOP,
        "action": SYM_ACTION,
    }.get(step_ir.action_type, SYM_ACTION)


def _resolver_label(step_ir: StepIR) -> str:
    """Get a compact resolver label suffix."""
    if step_ir.resolver.type == "rule":
        return f" {SYM_RULE}"
    if step_ir.resolver.type == "llm_menu":
        return f" {SYM_LLM_MENU}"
    return ""


def _mermaid_node(step_name: str, label: str, step_ir: StepIR) -> str:
    """Generate a Mermaid node definition with type-appropriate shape."""
    safe = label.replace('"', "'")

    if step_ir.is_terminal:
        return f'{step_name}(["{SYM_TERMINAL} {safe}"])'
    if step_ir.tail_call_target:
        return f'{step_name}[/"{SYM_TAIL_CALL} {safe}"\\]'
    if step_ir.action_type == "inference":
        return f'{step_name}{{{{"{safe}"}}}}'
    if step_ir.action_type == "flow":
        return f'{step_name}[["{safe}"]]'
    if step_ir.action_type == "noop":
        return f'{step_name}(["{safe}"])'
    return f'{step_name}["{safe}"]'


# ── System-Level Diagrams ─────────────────────────────────────────────


def mission_control_mermaid(ir: BlueprintIR) -> str:
    """Generate a detailed mission_control Mermaid diagram.

    Shows the full step graph with all transitions and tail-call targets.
    """
    mc = ir.flows.get("mission_control")
    if not mc:
        return "flowchart TD\n    no_mc[mission_control not found]"
    return flow_ir_to_mermaid(mc)


def system_view_mermaid(ir: BlueprintIR) -> str:
    """Generate an all-flows system view Mermaid diagram.

    Shows each architectural flow as a node with tail-call and sub-flow edges.
    Uses blueprint symbology for node labels.
    """
    lines: list[str] = []
    lines.append("flowchart TD")
    lines.append("    %% Ouroboros System View — All Architectural Flows")
    lines.append("")

    # Inline legend
    lines.extend(_inline_legend())

    # Node per flow (exclude test flows)
    for flow_name in sorted(ir.flows.keys()):
        flow_ir = ir.flows[flow_name]
        if flow_ir.category == "test":
            continue

        stats = flow_ir.stats
        inference_label = (
            f" {SYM_INFERENCE}{stats.inference_step_count}"
            if stats.inference_step_count > 0
            else ""
        )
        label = f"{flow_name}\\n{stats.step_count} steps{inference_label}"

        if flow_ir.category == "control":
            lines.append(f'    {flow_name}["{label}"]')
        elif flow_ir.category == "shared":
            lines.append(f'    {flow_name}[["{label}"]]')
        else:
            lines.append(f'    {flow_name}["{label}"]')

    lines.append("")

    # Edges
    seen_edges: set[str] = set()
    for edge in ir.dependency_graph.flow_edges:
        # Skip test flows
        source_flow = ir.flows.get(edge.source)
        target_flow = ir.flows.get(edge.target)
        if source_flow and source_flow.category == "test":
            continue
        if target_flow and target_flow.category == "test":
            continue

        edge_key = f"{edge.source}->{edge.target}:{edge.edge_type}"
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)

        safe_step = _sanitize(edge.from_step)
        if edge.edge_type == "tail_call":
            lines.append(
                f"    {edge.source} -.->|{SYM_TAIL_CALL} {safe_step}| {edge.target}"
            )
        else:
            lines.append(
                f"    {edge.source} ==>|{SYM_SUBFLOW} {safe_step}| {edge.target}"
            )

    lines.append("")

    # Style
    lines.append(
        "    style mission_control fill:#e8f0e6,stroke:#2d5a27,stroke-width:3px"
    )
    lines.append("    style create_plan fill:#e8f0e6,stroke:#2d5a27,stroke-width:2px")

    return "\n".join(lines)


# ── SVG Rendering ─────────────────────────────────────────────────────


def render_mermaid_to_svg(source: str) -> str | None:
    """Render Mermaid source to SVG via mmdc.

    Args:
        source: Mermaid flowchart source.

    Returns:
        SVG content as a string, or None if mmdc is not available.
    """
    mmdc = shutil.which("mmdc")
    if mmdc is None:
        return None

    tmpdir = tempfile.mkdtemp(prefix="ouroboros_bp_")
    try:
        input_file = os.path.join(tmpdir, "input.mmd")
        output_file = os.path.join(tmpdir, "output.svg")
        config_file = os.path.join(tmpdir, "config.json")

        with open(input_file, "w", encoding="utf-8") as f:
            f.write(source)

        with open(config_file, "w", encoding="utf-8") as f:
            f.write(MERMAID_THEME_CONFIG)

        result = subprocess.run(
            [
                mmdc,
                "-i",
                input_file,
                "-o",
                output_file,
                "-b",
                "transparent",
                "-c",
                config_file,
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return None

        with open(output_file, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def svg_to_data_uri(svg_content: str) -> str:
    """Convert SVG content to a data URI for embedding in HTML."""
    encoded = base64.b64encode(svg_content.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def render_all_flow_svgs(ir: BlueprintIR) -> dict[str, str]:
    """Render Mermaid SVGs for all architectural flows.

    Returns:
        Dict mapping flow name → SVG content string.
        Flows that fail to render are omitted.
    """
    svgs: dict[str, str] = {}

    for flow_name, flow_ir in ir.flows.items():
        if flow_ir.category == "test":
            continue
        mermaid_src = flow_ir_to_mermaid(flow_ir)
        svg = render_mermaid_to_svg(mermaid_src)
        if svg:
            svgs[flow_name] = svg

    return svgs


# ── PNG Rendering (for WeasyPrint PDF) ────────────────────────────────
#
# WeasyPrint does not support <foreignObject> in SVG, which Mermaid uses
# for ALL text rendering. This means SVG diagrams in the PDF show boxes
# and arrows but zero text. PNG rendering avoids this entirely.


def render_mermaid_to_png(source: str, scale: int = 2) -> bytes | None:
    """Render Mermaid source to PNG via mmdc.

    Args:
        source: Mermaid flowchart source.
        scale: Pixel scale factor (2 = 2x resolution for print clarity).

    Returns:
        PNG image bytes, or None if mmdc is not available or rendering fails.
    """
    mmdc = shutil.which("mmdc")
    if mmdc is None:
        return None

    tmpdir = tempfile.mkdtemp(prefix="ouroboros_bp_png_")
    try:
        input_file = os.path.join(tmpdir, "input.mmd")
        output_file = os.path.join(tmpdir, "output.png")
        config_file = os.path.join(tmpdir, "config.json")

        with open(input_file, "w", encoding="utf-8") as f:
            f.write(source)

        with open(config_file, "w", encoding="utf-8") as f:
            f.write(MERMAID_THEME_CONFIG)

        result = subprocess.run(
            [
                mmdc,
                "-i",
                input_file,
                "-o",
                output_file,
                "-b",
                "white",
                "-c",
                config_file,
                "-s",
                str(scale),
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return None

        with open(output_file, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def png_to_data_uri(png_bytes: bytes) -> str:
    """Convert PNG bytes to a data URI for embedding in HTML."""
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def render_all_flow_pngs(ir: BlueprintIR) -> dict[str, bytes]:
    """Render Mermaid PNGs for all architectural flows.

    Returns:
        Dict mapping flow name → PNG bytes.
        Flows that fail to render are omitted.
    """
    pngs: dict[str, bytes] = {}

    for flow_name, flow_ir in ir.flows.items():
        if flow_ir.category == "test":
            continue
        mermaid_src = flow_ir_to_mermaid(flow_ir)
        png = render_mermaid_to_png(mermaid_src)
        if png:
            pngs[flow_name] = png

    return pngs
