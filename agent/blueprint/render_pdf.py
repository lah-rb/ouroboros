"""PDF renderer — produces blueprint.pdf from the BlueprintIR via WeasyPrint.

Generates HTML with embedded CSS, then converts to PDF using WeasyPrint.
Uses the same data as the Markdown renderer but with precise print layout,
page breaks, headers/footers, and the full visual design.

Includes:
- Per-flow Mermaid SVG diagrams alongside flow cards
- System-level Mermaid diagrams (mission_control, all_flows) after front matter
- Landscape orientation for wide tables (Context Dictionary, Action Registry)
- Blueprint symbology throughout
"""

from __future__ import annotations

import html as html_lib
import logging
import time
from typing import Any

from agent.blueprint.ir import (
    BlueprintIR,
    FlowIR,
    StepIR,
)

logger = logging.getLogger(__name__)

# ── Symbol Constants ──────────────────────────────────────────────────

SYM_REQUIRED_INPUT = "○"
SYM_OPTIONAL_INPUT = "◑"
SYM_PUBLISHED = "●"
SYM_TERMINAL = "◆"
SYM_INFERENCE = "▷"
SYM_ACTION = "□"
SYM_SUBFLOW = "↳"
SYM_TAIL_CALL = "⟲"
SYM_NOOP = "∅"
SYM_RULE = "⑂"
SYM_LLM_MENU = "☰"
SYM_FILE_SYSTEM = "𓉗"
SYM_PERSIST_WRITE = "𓇴→"
SYM_PERSIST_READ = "→𓇴"
SYM_NOTES = "𓇆"
SYM_FRUSTRATION = "𓁿"
SYM_SUBPROCESS = "⌘"
SYM_INFERENCE_CALL = "⟶"
SYM_GATE_OPEN = "𓉫"
SYM_GATE_CLOSED = "𓉪"


# ── CSS Foundation ────────────────────────────────────────────────────

CSS = """
@page {
    size: letter;
    margin: 0.75in;
    @top-center {
        content: string(sheet-name);
        font-family: 'Noto Sans', 'Helvetica Neue', Arial, sans-serif;
        font-size: 8pt;
        color: #666;
    }
    @bottom-left {
        content: "Ouroboros Blueprint";
        font-family: 'Noto Sans', 'Helvetica Neue', Arial, sans-serif;
        font-size: 7pt;
        color: #999;
    }
    @bottom-right {
        content: "Page " counter(page) " of " counter(pages);
        font-family: 'Noto Sans', 'Helvetica Neue', Arial, sans-serif;
        font-size: 7pt;
        color: #999;
    }
}

@page landscape {
    size: letter landscape;
    margin: 0.6in;
    @top-center {
        content: string(sheet-name);
        font-family: 'Noto Sans', 'Helvetica Neue', Arial, sans-serif;
        font-size: 8pt;
        color: #666;
    }
    @bottom-left {
        content: "Ouroboros Blueprint";
        font-family: 'Noto Sans', 'Helvetica Neue', Arial, sans-serif;
        font-size: 7pt;
        color: #999;
    }
    @bottom-right {
        content: "Page " counter(page) " of " counter(pages);
        font-family: 'Noto Sans', 'Helvetica Neue', Arial, sans-serif;
        font-size: 7pt;
        color: #999;
    }
}

.landscape-section {
    page: landscape;
    page-break-before: always;
}

body {
    font-family: 'Noto Sans', 'Helvetica Neue', Arial, sans-serif;
    font-size: 9pt;
    line-height: 1.4;
    color: #1a1a1a;
}

h1 { font-size: 22pt; margin-bottom: 4pt; color: #111; }

h2 {
    font-size: 14pt;
    margin-top: 16pt;
    margin-bottom: 6pt;
    color: #222;
    border-bottom: 1.5pt solid #2d5a27;
    padding-bottom: 3pt;
    string-set: sheet-name content();
}

h3 { font-size: 11pt; margin-top: 10pt; margin-bottom: 4pt; color: #333; }
h4 { font-size: 10pt; margin-top: 8pt; margin-bottom: 3pt; color: #2d5a27; }

.sym { color: #2d5a27; font-weight: bold; }

.h {
    font-family: 'Noto Sans Egyptian Hieroglyphs', 'Apple Symbols', 'Segoe UI Symbol', sans-serif;
    color: #2d5a27;
}

.inject {
    color: #8B0000;
    font-family: 'Noto Sans Mono', 'Menlo', monospace;
    font-size: 8pt;
}

code, .mono {
    font-family: 'Noto Sans Mono', 'Menlo', monospace;
    font-size: 8pt;
    background: #f4f4f4;
    padding: 1pt 3pt;
    border-radius: 2pt;
}

table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 6pt;
    margin-bottom: 8pt;
    font-size: 8pt;
}

th {
    background: #2d5a27;
    color: white;
    padding: 4pt 6pt;
    text-align: left;
    font-weight: 600;
    font-size: 7.5pt;
}

td {
    padding: 3pt 6pt;
    border-bottom: 0.5pt solid #ddd;
    vertical-align: top;
}

tr:nth-child(even) td { background: #fafafa; }

.landscape-section table {
    font-size: 7pt;
}

.landscape-section th {
    font-size: 7pt;
    padding: 3pt 4pt;
}

.landscape-section td {
    padding: 2pt 4pt;
}

.flow-card {
    border: 1pt solid #ccc;
    border-left: 3pt solid #2d5a27;
    padding: 8pt 10pt;
    margin-bottom: 10pt;
    background: #fdfdfd;
}

.flow-card h4 { margin-top: 0; margin-bottom: 4pt; }
.flow-card .meta-line { font-size: 8pt; margin: 2pt 0; color: #444; }
.flow-card .desc { font-style: italic; font-size: 8.5pt; color: #555; margin-bottom: 4pt; }

.flow-pair {
    page-break-inside: avoid;
    margin-bottom: 14pt;
}

.flow-diagram {
    text-align: center;
    margin: 6pt 0;
    page-break-inside: avoid;
}

.flow-diagram img {
    max-width: 100%;
    max-height: 400pt;
}

.system-diagram {
    text-align: center;
    margin: 10pt 0;
}

.system-diagram img {
    max-width: 100%;
    max-height: 600pt;
}

.prompt-block {
    border: 0.5pt solid #ccc;
    background: #f8f8f0;
    padding: 4pt 6pt;
    margin: 4pt 0;
    font-size: 7.5pt;
    page-break-inside: avoid;
}

.prompt-block .prompt-header {
    font-weight: bold;
    font-size: 8pt;
    color: #2d5a27;
    margin-bottom: 2pt;
}

.legend-table td:first-child {
    text-align: center;
    font-size: 12pt;
    width: 40pt;
}

.page-break { page-break-before: always; }

.cover-title { font-size: 32pt; color: #2d5a27; margin-top: 120pt; margin-bottom: 8pt; }
.cover-subtitle { font-size: 14pt; color: #666; margin-bottom: 40pt; }
.cover-meta { font-size: 10pt; color: #888; }

.toc a { color: #2d5a27; text-decoration: none; }
.toc li { margin: 3pt 0; }
ul { padding-left: 16pt; margin: 4pt 0; }
li { margin: 2pt 0; }
"""


def render_pdf(ir: BlueprintIR, output_path: str) -> None:
    """Render the BlueprintIR to a PDF file via WeasyPrint.

    Pre-renders all Mermaid diagrams to SVG before building the HTML.
    """
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as e:
        raise ImportError(
            f"WeasyPrint is required for PDF generation. "
            f"Install it with: uv add weasyprint. "
            f"On macOS also: brew install gobject-introspection pango. "
            f"Original error: {e}"
        )

    from agent.blueprint.mermaid import (
        render_all_flow_pngs,
        render_mermaid_to_png,
        mission_control_mermaid,
        system_view_mermaid,
    )

    # Pre-render all Mermaid diagrams as PNG
    # (WeasyPrint cannot render <foreignObject> in SVG — all text would be invisible)
    print("   Rendering Mermaid diagrams (PNG for WeasyPrint)...")
    t0 = time.monotonic()

    flow_pngs = render_all_flow_pngs(ir)
    mc_png = render_mermaid_to_png(mission_control_mermaid(ir))
    sys_png = render_mermaid_to_png(system_view_mermaid(ir))

    rendered = len(flow_pngs) + (1 if mc_png else 0) + (1 if sys_png else 0)
    elapsed = (time.monotonic() - t0) * 1000
    print(f"   Rendered {rendered} diagrams ({elapsed:.0f}ms)")

    html_content = _build_html(ir, flow_pngs, mc_png, sys_png)

    try:
        HTML(string=html_content).write_pdf(output_path)
    except Exception as e:
        raise RuntimeError(f"PDF generation failed: {e}") from e


def _build_html(
    ir: BlueprintIR,
    flow_pngs: dict[str, bytes],
    mc_png: bytes | None,
    sys_png: bytes | None,
) -> str:
    """Build the complete HTML document from the IR and pre-rendered PNGs."""
    sections: list[str] = []

    sections.append(_html_cover(ir))
    sections.append(_html_toc())
    sections.append(_html_legend())
    sections.append(_html_system_diagrams(ir, mc_png, sys_png))
    sections.append(_html_system_context(ir))
    sections.append(_html_mission_lifecycle(ir))
    sections.append(_html_flow_catalog(ir, flow_pngs))
    sections.append(_html_context_dictionary(ir))
    sections.append(_html_action_registry(ir))

    body = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Ouroboros Blueprint</title>
    <style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>"""


# ── Helpers ───────────────────────────────────────────────────────────


def _esc(text: str) -> str:
    return html_lib.escape(str(text))


def _arch_flow_count(ir: BlueprintIR) -> int:
    """Count architectural flows (excluding test flows)."""
    return sum(1 for f in ir.flows.values() if f.category != "test")


def _step_type_sym_html(step: StepIR) -> str:
    syms = {"inference": "▷", "flow": "↳", "noop": "∅", "action": "□"}
    sym = syms.get(step.action_type, "□")
    return f'<span class="sym">{sym}</span>'


def _resolver_sym_html(step: StepIR) -> str:
    if step.resolver.type == "rule":
        return '<span class="sym">⑂</span>'
    if step.resolver.type == "llm_menu":
        return '<span class="sym">☰</span>'
    return ""


def _png_img(png_bytes: bytes | None, max_height: str = "400pt") -> str:
    """Create an img tag from PNG bytes."""
    if not png_bytes:
        return '<p style="color:#999;font-style:italic">Diagram unavailable (mmdc not found)</p>'
    from agent.blueprint.mermaid import png_to_data_uri

    uri = png_to_data_uri(png_bytes)
    return f'<img src="{uri}" style="max-width:100%;max-height:{max_height}" />'


# ── Sheet 0: Cover / Legend ───────────────────────────────────────────


def _html_cover(ir: BlueprintIR) -> str:
    arch_count = _arch_flow_count(ir)
    return f"""
<div class="cover">
    <div class="cover-title">Ouroboros Blueprint</div>
    <div class="cover-subtitle">Architectural Plan Set — Flow-Driven Autonomous Coding Agent</div>
    <div class="cover-meta">
        Generated: {_esc(ir.meta.generated_at)}<br>
        Source Hash: <code>{_esc(ir.meta.source_hash[:12])}…</code><br>
        Flows: <strong>{arch_count}</strong> &middot;
        Actions: <strong>{ir.meta.action_count}</strong> &middot;
        Context Keys: <strong>{ir.meta.context_key_count}</strong>
    </div>
</div>"""


def _html_toc() -> str:
    return """
<div class="page-break"></div>
<h2>Table of Contents</h2>
<ul class="toc">
    <li>0 — Cover &amp; Legend</li>
    <li>1 — System Diagrams (mission_control &amp; all-flows)</li>
    <li>2 — System Context</li>
    <li>3 — Mission Lifecycle</li>
    <li>4 — Flow Catalog (with diagrams)</li>
    <li>5 — Context Key Dictionary (landscape)</li>
    <li>6 — Action Registry (landscape)</li>
</ul>"""


def _html_legend() -> str:
    rows = [
        ("○", "Required Input", "Data the flow cannot execute without"),
        ("◑", "Optional Input", "Data that enriches but isn't required"),
        ("●", "Published Output", "Context key added to accumulator"),
        ("◆", "Terminal Status", "Terminal outcome of a flow"),
        ("▷", "Inference Step", "Step that invokes LLM inference"),
        ("□", "Action Step", "Generic computation (registered callable)"),
        ("↳", "Sub-flow", "Delegates to a child flow"),
        ("⟲", "Tail-call", "Continues execution in another flow"),
        ("∅", "Noop", "Pass-through for routing logic only"),
        ("⑂", "Rule Resolver", "Deterministic condition, no inference cost"),
        ("☰", "LLM Menu", "Constrained LLM choice, one inference call"),
    ]

    hieroglyph_rows = [
        ("𓉗", "File System", "File read/write operations"),
        ("𓇴→", "Persist Write", "Save to persistent state"),
        ("→𓇴", "Persist Read", "Load from persistent state"),
        ("𓇆", "Notes", "Accumulated observations"),
        ("𓁿", "Frustration", "Emotional weight of failure"),
        ("⌘", "Subprocess", "Terminal/shell execution"),
        ("⟶", "Inference", "Token flow to/from model"),
        ("𓉫", "Gate Open", "Checkpoint passed"),
        ("𓉪", "Gate Closed", "Checkpoint failed"),
    ]

    html = '<div class="page-break"></div>\n<h2>Legend</h2>\n'
    html += "<h3>Data Flow, Step Type &amp; Resolver Symbols</h3>\n"
    html += '<table class="legend-table"><tr><th>Symbol</th><th>Name</th><th>Meaning</th></tr>\n'
    for sym, name, meaning in rows:
        html += f'<tr><td><span class="sym">{_esc(sym)}</span></td><td>{_esc(name)}</td><td>{_esc(meaning)}</td></tr>\n'
    html += "</table>\n"

    html += "<h3>Effect &amp; System Symbols</h3>\n"
    html += '<table class="legend-table"><tr><th>Symbol</th><th>Name</th><th>Meaning</th></tr>\n'
    for sym, name, meaning in hieroglyph_rows:
        html += f'<tr><td><span class="h">{_esc(sym)}</span></td><td>{_esc(name)}</td><td>{_esc(meaning)}</td></tr>\n'
    html += "</table>\n"

    return html


# ── Sheet 1: System Diagrams ─────────────────────────────────────────


def _html_system_diagrams(
    ir: BlueprintIR,
    mc_png: bytes | None,
    sys_png: bytes | None,
) -> str:
    html = '<div class="landscape-section">\n'
    html += "<h2>System Diagrams</h2>\n"

    html += "<h3>mission_control — Agent Orchestration Hub</h3>\n"
    html += '<div class="system-diagram">\n'
    html += _png_img(mc_png, "550pt")
    html += "</div>\n"

    html += '<div class="page-break"></div>\n'
    html += "<h3>All Flows — System Architecture View</h3>\n"
    html += '<div class="system-diagram">\n'
    html += _png_img(sys_png, "550pt")
    html += "</div>\n"

    html += "</div>\n"
    return html


# ── Sheet 2: System Context ──────────────────────────────────────────


def _html_system_context(ir: BlueprintIR) -> str:
    orchestrator_count = sum(1 for f in ir.flows.values() if f.category == "orchestrator")
    task_count = sum(1 for f in ir.flows.values() if f.category == "task")
    sub_flow_count = sum(1 for f in ir.flows.values() if f.category == "sub_flow")
    arch_count = _arch_flow_count(ir)

    return f"""
<div class="page-break"></div>
<h2>System Context</h2>
<p><strong>Ouroboros</strong> is a flow-driven autonomous coding agent backed by LLMVP local inference.
It operates as a pure GraphQL client — all inference flows through <code>localhost:8000/graphql</code>.</p>

<h3>Actors</h3>
<ul>
    <li><strong>Shop Director (User)</strong> — Sets missions, checks in periodically via CLI.</li>
    <li><strong>Junior Developer (Local Model)</strong> — Runs continuously via LLMVP, follows structured flows.</li>
    <li><strong>Senior Developer (External API)</strong> — Consulted on escalation (design pending).</li>
</ul>

<h3>Flow Inventory</h3>
<table>
    <tr><th>Category</th><th>Count</th></tr>
    <tr><td>Orchestrator flows</td><td>{orchestrator_count}</td></tr>
    <tr><td>Task flows</td><td>{task_count}</td></tr>
    <tr><td>Sub-flows</td><td>{sub_flow_count}</td></tr>
    <tr><td><strong>Total</strong></td><td><strong>{arch_count}</strong></td></tr>
</table>"""


# ── Sheet 3: Mission Lifecycle ────────────────────────────────────────


def _html_mission_lifecycle(ir: BlueprintIR) -> str:
    mc = ir.flows.get("mission_control")
    if not mc:
        return '<div class="page-break"></div>\n<h2>Mission Lifecycle</h2>\n<p><em>mission_control not found.</em></p>'

    html = '<div class="page-break"></div>\n<h2>Mission Lifecycle</h2>\n'
    html += "<p><code>mission_control</code> is the hub flow orchestrating the entire agent lifecycle. "
    html += "Child task flows tail-call back on completion, creating a continuous cycle.</p>\n"

    html += "<h3>Steps</h3>\n<ul>\n"
    for step_name, step_ir in mc.steps.items():
        sym = _step_type_sym_html(step_ir)
        resolver = _resolver_sym_html(step_ir)
        desc = _esc(step_ir.description or step_name)
        extra = ""
        if step_ir.tail_call_target:
            extra += f' <span class="sym">⟲</span> → <code>{_esc(step_ir.tail_call_target)}</code>'
        if step_ir.is_terminal:
            extra += f' <span class="sym">◆</span> <code>{_esc(step_ir.terminal_status or "")}</code>'
        html += f"<li>{sym} <strong>{_esc(step_name)}</strong> {resolver} — {desc}{extra}</li>\n"
    html += "</ul>\n"

    incoming = [
        e
        for e in ir.dependency_graph.flow_edges
        if e.target == "mission_control" and e.edge_type == "tail_call"
    ]
    if incoming:
        html += "<h3>Incoming Tail-Calls</h3>\n<ul>\n"
        for edge in sorted(incoming, key=lambda e: e.source):
            html += f"<li><code>{_esc(edge.source)}</code> → <code>mission_control</code> (from <code>{_esc(edge.from_step)}</code>)</li>\n"
        html += "</ul>\n"

    return html


# ── Sheet 4: Flow Catalog ────────────────────────────────────────────


def _html_flow_catalog(ir: BlueprintIR, flow_pngs: dict[str, bytes]) -> str:
    html = '<div class="page-break"></div>\n<h2>Flow Catalog</h2>\n'

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

        html += f"<h3>{heading}</h3>\n"
        for flow_ir in sorted(flows, key=lambda f: f.name):
            png = flow_pngs.get(flow_ir.name)
            html += _html_flow_pair(flow_ir, ir, png)

    return html


def _html_flow_pair(flow_ir: FlowIR, ir: BlueprintIR, png: bytes | None) -> str:
    """Render a flow card + its Mermaid diagram as a paired unit."""
    html = '<div class="flow-pair">\n'
    html += _html_flow_card(flow_ir, ir)
    if png:
        html += '<div class="flow-diagram">\n'
        html += _png_img(png, "380pt")
        html += "</div>\n"
    html += "</div>\n"
    return html


def _html_flow_card(flow_ir: FlowIR, ir: BlueprintIR) -> str:
    """Render a single flow card as HTML."""
    html = '<div class="flow-card">\n'
    html += f'<h4>{_esc(flow_ir.name)} <span style="font-weight:normal;color:#888">(v{flow_ir.version})</span></h4>\n'
    html += f'<div class="desc">{_esc(flow_ir.description.strip())}</div>\n'

    # Context Contract
    contract_parts: list[str] = []
    if flow_ir.context_tier:
        contract_parts.append(f'<strong>Tier:</strong> <code>{_esc(flow_ir.context_tier)}</code>')
    if flow_ir.state_reads:
        reads = ", ".join(f'<code>{_esc(r)}</code>' for r in flow_ir.state_reads[:6])
        more = f" (+{len(flow_ir.state_reads) - 6})" if len(flow_ir.state_reads) > 6 else ""
        contract_parts.append(f'<strong>Reads:</strong> {reads}{more}')
    if flow_ir.returns:
        ret_keys = ", ".join(f'<code>{_esc(k)}</code>' for k in list(flow_ir.returns.keys())[:6])
        contract_parts.append(f'<strong>Returns:</strong> {ret_keys}')
    if contract_parts:
        html += f'<div class="meta-line">{" · ".join(contract_parts)}</div>\n'

    # Persona peers
    if flow_ir.known_personas:
        peers = ", ".join(f'<code>{_esc(p)}</code>' for p in flow_ir.known_personas)
        html += f'<div class="meta-line"><strong>Peers:</strong> {peers}</div>\n'

    # Inputs
    parts: list[str] = []
    for inp in flow_ir.inputs:
        if inp.required:
            parts.append(f'<span class="sym">○</span> {_esc(inp.name)}')
        else:
            parts.append(f'<span class="sym">◑</span> {_esc(inp.name)}')
    if parts:
        html += f'<div class="meta-line"><strong>Inputs:</strong> {" · ".join(parts)}</div>\n'

    # Terminal
    if flow_ir.terminal_statuses:
        terms = [
            f'<span class="sym">◆</span> {_esc(s)}' for s in flow_ir.terminal_statuses
        ]
        html += f'<div class="meta-line"><strong>Terminal:</strong> {" · ".join(terms)}</div>\n'

    # Published
    if flow_ir.publishes_to_parent:
        pubs = [
            f'<span class="sym">●</span> {_esc(k)}'
            for k in flow_ir.publishes_to_parent[:8]
        ]
        more = (
            f" (+{len(flow_ir.publishes_to_parent) - 8} more)"
            if len(flow_ir.publishes_to_parent) > 8
            else ""
        )
        html += f'<div class="meta-line"><strong>Publishes:</strong> {" · ".join(pubs)}{more}</div>\n'

    # Sub-flows
    if flow_ir.sub_flows:
        sfs = [
            f'<span class="sym">↳</span> {_esc(sf.flow)}' for sf in flow_ir.sub_flows
        ]
        html += f'<div class="meta-line"><strong>Sub-flows:</strong> {" · ".join(sfs)}</div>\n'

    # Tail-calls
    if flow_ir.tail_calls:
        tcs = sorted(set(tc.target_flow for tc in flow_ir.tail_calls))
        tc_parts = [f'<span class="sym">⟲</span> {_esc(t)}' for t in tcs]
        html += f'<div class="meta-line"><strong>Tail-calls:</strong> {" · ".join(tc_parts)}</div>\n'

    # Stats
    s = flow_ir.stats
    stats_parts = [f"{s.step_count} steps"]
    if s.inference_step_count > 0:
        stats_parts.append(
            f'<span class="sym">▷</span> {s.estimated_inference_calls} inference'
        )
    if s.rule_resolver_count > 0:
        stats_parts.append(f'{s.rule_resolver_count} <span class="sym">⑂</span> rule')
    if s.llm_menu_resolver_count > 0:
        stats_parts.append(
            f'{s.llm_menu_resolver_count} <span class="sym">☰</span> menu'
        )
    html += f'<div class="meta-line"><strong>Stats:</strong> {" · ".join(stats_parts)}</div>\n'

    # Prompt blocks for inference steps
    inference_steps = [
        st for st in flow_ir.steps.values() if st.action_type == "inference"
    ]
    for step in inference_steps:
        html += _html_prompt_block(step)

    html += "</div>\n"
    return html


def _html_prompt_block(step: StepIR) -> str:
    temp = ""
    max_tok = ""
    if step.config:
        if step.config.temperature is not None:
            temp = f"{step.config.temperature}"
        if step.config.max_tokens is not None:
            max_tok = f", {step.config.max_tokens} max"

    config_str = f"{temp}{max_tok}" if temp or max_tok else "default"

    html = '<div class="prompt-block">\n'
    html += f'<div class="prompt-header"><span class="sym">▷</span> PROMPT: {_esc(step.name)} ({config_str})</div>\n'

    if step.prompt:
        import re

        def _highlight_inject(m: re.Match) -> str:
            inner = m.group(1).strip()
            return f'<span class="inject">{{← {_esc(inner)}}}</span>'

        # Show full prompt text with highlighted inject points
        prompt_text = step.prompt.strip()
        highlighted = re.sub(r"\{\{(.+?)\}\}", _highlight_inject, prompt_text)
        # Convert newlines to <br> for readability
        highlighted = highlighted.replace("\n", "<br>\n")
        html += f"<div>{highlighted}</div>\n"

    if step.prompt_injects:
        injects = ", ".join(
            f'<span class="inject">{{← {_esc(inj)}}}</span>'
            for inj in step.prompt_injects[:6]
        )
        more = (
            f" (+{len(step.prompt_injects) - 6})"
            if len(step.prompt_injects) > 6
            else ""
        )
        html += f'<div style="margin-top:3pt;font-size:7pt;"><strong>Injects:</strong> {injects}{more}</div>\n'

    html += "</div>\n"
    return html


# ── Sheet 5: Context Dictionary (Landscape) ──────────────────────────


def _html_context_dictionary(ir: BlueprintIR) -> str:
    html = '<div class="landscape-section">\n'
    html += "<h2>Context Key Dictionary</h2>\n"
    html += "<table>\n"
    html += "<tr><th>Key</th><th>Published By</th><th>Consumed By</th><th>#</th><th>Audit Flags</th></tr>\n"

    for key_name in sorted(ir.context_keys.keys()):
        key_ir = ir.context_keys[key_name]
        pubs = ", ".join(
            f"<code>{_esc(p.flow)}.{_esc(p.step)}</code>"
            for p in key_ir.published_by[:4]
        )
        if len(key_ir.published_by) > 4:
            pubs += f" (+{len(key_ir.published_by) - 4})"

        cons = ", ".join(
            f"<code>{_esc(c.flow)}.{_esc(c.step)}</code>"
            for c in key_ir.consumed_by[:4]
        )
        if len(key_ir.consumed_by) > 4:
            cons += f" (+{len(key_ir.consumed_by) - 4})"

        flags = ", ".join(key_ir.audit_flags) if key_ir.audit_flags else "—"
        html += f"<tr><td><code>{_esc(key_name)}</code></td><td>{pubs}</td><td>{cons}</td><td>{key_ir.consumer_count}</td><td>{_esc(flags)}</td></tr>\n"

    html += "</table>\n"
    html += "</div>\n"
    return html


# ── Sheet 6: Action Registry (Landscape) ─────────────────────────────


def _html_action_registry(ir: BlueprintIR) -> str:
    html = '<div class="landscape-section">\n'
    html += "<h2>Action Registry</h2>\n"
    html += "<table>\n"
    html += "<tr><th>Action</th><th>Module</th><th>Effects Used</th><th>Referenced By</th></tr>\n"

    for name in sorted(ir.actions.keys()):
        action = ir.actions[name]
        effects = ", ".join(action.effects_used[:5]) if action.effects_used else "—"
        if len(action.effects_used) > 5:
            effects += f" (+{len(action.effects_used) - 5})"
        refs = ", ".join(f"<code>{_esc(r)}</code>" for r in action.referenced_by[:4])
        if len(action.referenced_by) > 4:
            refs += f" (+{len(action.referenced_by) - 4})"
        if not refs:
            refs = "—"
        html += f"<tr><td><code>{_esc(name)}</code></td><td><code>{_esc(action.module)}</code></td><td>{_esc(effects)}</td><td>{refs}</td></tr>\n"

    html += "</table>\n"
    html += "</div>\n"
    return html
