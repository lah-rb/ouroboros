"""Trace CLI — render trace summaries from JSONL trace files.

Reads .agent/traces/*.jsonl files and produces human-readable summaries
with flow breakdown, token counts, resolver decisions, and audit warnings.
"""

from __future__ import annotations

import argparse
import json
import os
import glob
from collections import defaultdict
from typing import Any


def find_trace_files(
    working_dir: str = ".", mission_id: str | None = None
) -> list[str]:
    """Find trace JSONL files in .agent/traces/, newest first."""
    traces_dir = os.path.join(working_dir, ".agent", "traces")
    if not os.path.isdir(traces_dir):
        return []

    pattern = os.path.join(traces_dir, "*.jsonl")
    files = glob.glob(pattern)

    if mission_id:
        files = [f for f in files if mission_id in os.path.basename(f)]

    # Sort by modification time, newest first
    files.sort(key=os.path.getmtime, reverse=True)
    return files


def load_events(trace_path: str) -> list[dict]:
    """Load events from a JSONL trace file, line by line (lazy-friendly)."""
    events = []
    with open(trace_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


def render_summary(events: list[dict], trace_path: str) -> str:
    """Render a summary report from trace events."""
    if not events:
        return "No trace events found."

    lines: list[str] = []

    # Extract mission info
    mission_id = events[0].get("mission_id", "unknown")
    lines.append(f"Mission: {mission_id}")
    lines.append(f"Trace: {os.path.basename(trace_path)}")
    lines.append("")

    # Cycle stats
    cycle_starts = [e for e in events if e["event_type"] == "cycle_start"]
    cycle_ends = [e for e in events if e["event_type"] == "cycle_end"]
    total_cycles = len(cycle_starts)

    total_duration_ms = sum(e.get("cycle_duration_ms", 0) for e in cycle_ends)
    total_secs = total_duration_ms / 1000

    if total_secs >= 60:
        duration_str = f"{int(total_secs // 60)}m {int(total_secs % 60)}s"
    else:
        duration_str = f"{total_secs:.1f}s"

    # Count unique flows
    flows_executed = set(e.get("flow", "") for e in cycle_starts)
    lines.append(
        f"Duration: {duration_str} | Cycles: {total_cycles} | "
        f"Flows executed: {len(flows_executed)} unique"
    )
    lines.append("")

    # Flow breakdown
    flow_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "cycles": 0,
            "inference_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
        }
    )

    for e in events:
        flow = e.get("flow", "")
        if e["event_type"] == "cycle_start":
            flow_stats[flow]["cycles"] += 1
        elif e["event_type"] == "inference_call":
            flow_stats[flow]["inference_calls"] += 1
            flow_stats[flow]["tokens_in"] += e.get("tokens_in", 0)
            flow_stats[flow]["tokens_out"] += e.get("tokens_out", 0)

    lines.append("Flow Breakdown:")
    for flow, stats in sorted(flow_stats.items(), key=lambda x: -x[1]["cycles"]):
        if not flow:
            continue
        tok_in = f"{stats['tokens_in']:,}"
        tok_out = f"{stats['tokens_out']:,}"
        lines.append(
            f"  {flow:<24s} × {stats['cycles']} cycles   "
            f"▷ {stats['inference_calls']} inference   "
            f"⟶ {tok_in} tok in / {tok_out} tok out"
        )
    lines.append("")

    # Totals
    total_inference = sum(1 for e in events if e["event_type"] == "inference_call")
    total_tok_in = sum(
        e.get("tokens_in", 0) for e in events if e["event_type"] == "inference_call"
    )
    total_tok_out = sum(
        e.get("tokens_out", 0) for e in events if e["event_type"] == "inference_call"
    )
    avg_in = total_tok_in // total_inference if total_inference else 0
    avg_out = total_tok_out // total_inference if total_inference else 0

    lines.append("Totals:")
    lines.append(f"  Inference calls: {total_inference}")
    lines.append(f"  Tokens in:  {total_tok_in:,} (avg {avg_in:,}/call)")
    lines.append(f"  Tokens out: {total_tok_out:,} (avg {avg_out:,}/call)")
    lines.append("")

    # Resolver decisions
    rule_count = 0
    menu_count = 0
    menu_choices: dict[str, int] = defaultdict(int)

    for e in events:
        if e["event_type"] == "step_end":
            rt = e.get("resolver_type", "")
            if rt == "rule":
                rule_count += 1
            elif rt == "llm_menu":
                menu_count += 1
                decision = e.get("resolver_decision", "")
                if decision:
                    menu_choices[decision] += 1

    lines.append("Resolver Decisions:")
    lines.append(f"  ⑂ rule: {rule_count} decisions")
    lines.append(f"  ☰ menu: {menu_count} decisions")
    for choice, count in sorted(menu_choices.items(), key=lambda x: -x[1]):
        lines.append(f"    → {choice}: {count}")
    lines.append("")

    # Audit warnings
    audit: list[str] = []

    # High token steps
    step_tokens: dict[str, list[int]] = defaultdict(list)
    for e in events:
        if e["event_type"] == "inference_call":
            key = f"{e.get('flow', '')}/{e.get('step', '')}"
            step_tokens[key].append(e.get("tokens_in", 0))

    for key, tok_list in step_tokens.items():
        avg = sum(tok_list) // len(tok_list) if tok_list else 0
        if avg > 3000:
            audit.append(f"⚠ {key} averaged {avg:,} tokens in (highest step)")

    # Excessive cycles per flow
    for flow, stats in flow_stats.items():
        if stats["cycles"] > 5:
            audit.append(
                f"⚠ {flow} ran {stats['cycles']} cycles "
                f"(check for unnecessary re-entry)"
            )

    if audit:
        lines.append("Audit:")
        for a in audit:
            lines.append(f"  {a}")
        lines.append("")

    return "\n".join(lines)


def render_detail(events: list[dict], trace_path: str) -> str:
    """Render a detailed per-step breakdown from trace events."""
    lines: list[str] = []
    lines.append(render_summary(events, trace_path))
    lines.append("=" * 60)
    lines.append("DETAILED EVENT LOG")
    lines.append("=" * 60)
    lines.append("")

    current_cycle = 0
    for e in events:
        et = e["event_type"]

        if et == "cycle_start":
            current_cycle = e.get("cycle", 0)
            lines.append(
                f"── Cycle {current_cycle}: {e.get('flow', '')} "
                f"(inputs: {e.get('entry_inputs', [])}) ──"
            )

        elif et == "step_start":
            action_sym = {
                "inference": "▷",
                "action": "□",
                "flow": "↳",
                "noop": "∅",
            }.get(e.get("action_type", ""), "?")
            lines.append(
                f"  {action_sym} {e.get('step', '')} " f"({e.get('action', '')})"
            )

        elif et == "inference_call":
            lines.append(
                f"    ⟶ {e.get('tokens_in', 0)} tok in → "
                f"{e.get('tokens_out', 0)} tok out "
                f"({e.get('wall_ms', 0):.0f}ms, "
                f"purpose={e.get('purpose', '')})"
            )

        elif et == "flow_invoke":
            lines.append(
                f"    ↳ invoke {e.get('child_flow', '')} "
                f"(inputs: {e.get('child_inputs', [])})"
            )

        elif et == "flow_return":
            lines.append(
                f"    ↳ return {e.get('child_flow', '')} "
                f"→ {e.get('return_status', '')} "
                f"({e.get('child_duration_ms', 0):.0f}ms)"
            )

        elif et == "step_end":
            resolver_sym = {"rule": "⑂", "llm_menu": "☰"}.get(
                e.get("resolver_type", ""), "→"
            )
            lines.append(
                f"    {resolver_sym} → {e.get('resolver_decision', '')} "
                f"({e.get('step_duration_ms', 0):.0f}ms) "
                f"published: {e.get('published', [])}"
            )

        elif et == "cycle_end":
            outcome = e.get("outcome", "")
            if outcome == "tail_call":
                lines.append(
                    f"  ⟲ tail_call → {e.get('target_flow', '')} "
                    f"({e.get('cycle_duration_ms', 0):.0f}ms)"
                )
            else:
                lines.append(
                    f"  ◆ terminated: {e.get('status', '')} "
                    f"({e.get('cycle_duration_ms', 0):.0f}ms)"
                )
            lines.append("")

    return "\n".join(lines)


def _derive_output_path(
    events: list[dict], fmt: str, output_override: str | None
) -> str:
    """Derive the output file path for the trace report."""
    if output_override:
        return output_override

    # Build default: trace_{mission_id}.md in cwd
    mission_id = events[0].get("mission_id", "unknown") if events else "unknown"
    # Use short mission id (first 8 chars) for readable filenames
    short_id = mission_id[:8] if len(mission_id) > 8 else mission_id
    return f"trace_{short_id}.md"


def cmd_trace(args: argparse.Namespace) -> None:
    """Handle the `ouroboros.py trace` CLI command."""
    working_dir = getattr(args, "working_dir", None) or "."
    mission_id = getattr(args, "mission", None)
    fmt = getattr(args, "format", "summary") or "summary"
    output_override = getattr(args, "output", None)

    files = find_trace_files(working_dir, mission_id)

    if not files:
        print("No trace files found in .agent/traces/")
        if mission_id:
            print(f"  (filtered for mission: {mission_id})")
        return

    # Use the latest trace file
    trace_path = files[0]
    events = load_events(trace_path)

    if not events:
        print(f"Trace file is empty: {trace_path}")
        return

    if fmt == "summary":
        content = render_summary(events, trace_path)
    elif fmt == "detail":
        content = render_detail(events, trace_path)
    else:
        print(f"Unknown format: {fmt}")
        return

    # Write to file (default behavior)
    output_path = _derive_output_path(events, fmt, output_override)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.write("\n")
    print(f"✅ Trace written to {output_path}")
