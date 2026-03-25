"""CLI wiring for the blueprint command.

Provides cmd_blueprint() which is called from ouroboros.py.
"""

from __future__ import annotations

import argparse
import os
import sys
import time


def cmd_blueprint(args: argparse.Namespace) -> None:
    """Generate the architectural blueprint from flow definitions.

    Parses all YAML flows, action registry, and templates to produce
    a comprehensive plan set in Markdown and/or PDF format.
    """
    from agent.blueprint.analyzer import analyze
    from agent.blueprint.render_markdown import render_markdown

    output_dir = args.output or os.getcwd()
    fmt = args.format  # "pdf", "md", or None (both)

    print("🏗  Analyzing flows, actions, and templates...")
    start = time.monotonic()

    flows_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "flows",
    )
    agent_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

    # Use relative paths if running from the project root
    if os.path.isdir("flows"):
        flows_dir = "flows"
    if os.path.isdir("agent"):
        agent_dir = "agent"

    ir = analyze(flows_dir=flows_dir, agent_dir=agent_dir)
    elapsed = (time.monotonic() - start) * 1000

    print(
        f"   Analyzed: {ir.meta.flow_count} flows, "
        f"{ir.meta.action_count} actions, "
        f"{ir.meta.context_key_count} context keys "
        f"({elapsed:.0f}ms)"
    )

    os.makedirs(output_dir, exist_ok=True)

    # Generate Markdown
    if fmt in (None, "md"):
        md_path = os.path.join(output_dir, "blueprint.md")
        md_content = render_markdown(ir)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"✅ Markdown: {md_path} ({len(md_content):,} chars)")

    # Generate PDF
    if fmt in (None, "pdf"):
        pdf_path = os.path.join(output_dir, "blueprint.pdf")
        try:
            from agent.blueprint.render_pdf import render_pdf

            print("   Generating PDF via WeasyPrint...")
            pdf_start = time.monotonic()
            render_pdf(ir, pdf_path)
            pdf_elapsed = (time.monotonic() - pdf_start) * 1000
            file_size = os.path.getsize(pdf_path)
            print(f"✅ PDF: {pdf_path} " f"({file_size:,} bytes, {pdf_elapsed:.0f}ms)")
        except ImportError as e:
            print(f"⚠️  PDF skipped: {e}")
        except RuntimeError as e:
            print(f"❌ PDF failed: {e}")

    total = (time.monotonic() - start) * 1000
    print(f"\n   Blueprint generated in {total:.0f}ms")
