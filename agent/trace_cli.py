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

from agent.trace import summarize_events


def _fmt_ms(ms: float) -> str:
    """Human-friendly duration from milliseconds."""
    secs = (ms or 0) / 1000.0
    if secs >= 60:
        return f"{int(secs // 60)}m {secs % 60:04.1f}s"
    if secs >= 1:
        return f"{secs:.2f}s"
    return f"{ms:.0f}ms"


def _load_summary_json(trace_path: str) -> dict | None:
    """Load the companion <trace>.summary.json (the canonical finite head),
    returning its inner ``summary`` dict — or None if absent/unreadable."""
    if not trace_path.endswith(".jsonl"):
        return None
    path = trace_path[:-6] + ".summary.json"
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            record = json.load(f)
        return record.get("summary") or None
    except Exception:
        return None


def render_finite_breakdown(summary: dict) -> list[str]:
    """The finite time + cache-aware token head, from a summary dict (the
    companion summary.json, or summarize_events for an old trace)."""
    lines: list[str] = []
    total = summary.get("total_wall_ms", 0.0) or 0.0
    completeness = summary.get("completeness_pct", 0.0)
    residual_ms = summary.get("residual_ms", 0.0)
    residual_pct = summary.get("residual_pct", 0.0)
    time_ms = summary.get("time_ms", {})
    time_pct = summary.get("time_pct", {})

    lines.append("Time Breakdown (Σ categories + residual = total):")
    lines.append(
        f"  total wall {_fmt_ms(total)}  |  accounted {completeness:.1f}%  "
        f"|  residual {_fmt_ms(residual_ms)} ({residual_pct:.1f}%)"
    )
    # Categories, largest first; hide dead-zero buckets for readability.
    for cat, ms in sorted(time_ms.items(), key=lambda kv: -kv[1]):
        if ms <= 0:
            continue
        lines.append(f"    {cat:<16s} {_fmt_ms(ms):>9s}  {time_pct.get(cat, 0.0):5.1f}%")
    lines.append(f"    {'residual':<16s} {_fmt_ms(residual_ms):>9s}  {residual_pct:5.1f}%")
    # Sub-split of the inference bucket: prefill (prompt eval) vs decode.
    ph = summary.get("inference_phase", {})
    if ph and (ph.get("prefill_ms") or ph.get("decode_ms")):
        lines.append(
            f"    └ of inference: prefill {_fmt_ms(ph.get('prefill_ms',0))} "
            f"({ph.get('prefill_pct',0)}%) | decode {_fmt_ms(ph.get('decode_ms',0))} "
            f"({ph.get('decode_pct',0)}%) | server-other {_fmt_ms(ph.get('server_other_ms',0))}"
        )
    span = summary.get("session_span_ms", 0.0)
    if span:
        lines.append(f"  (sessions live {_fmt_ms(span)} total — overlaps inference)")
    lines.append("")

    # Cache-aware token panel.
    tok = summary.get("tokens", {})
    cache = summary.get("cache", {})
    io = summary.get("io_ratio", {})
    cached = tok.get("cached_prefix", 0)
    fresh = tok.get("fresh_prefill", 0)
    gen = tok.get("generated", 0)
    lines.append("Tokens (cache-aware):")
    if cached or fresh or gen:
        lines.append(
            f"  input: fresh {fresh:,} + cached {cached:,} = {fresh + cached:,}"
            f"   |   generated {gen:,}"
        )
        hr = cache.get("hit_rate")
        pr = cache.get("prefix_reuse_rate")
        hits, miss = cache.get("hit", 0), cache.get("miss", 0)
        if hr is not None:
            lines.append(
                f"  cache hit-rate {hr * 100:.0f}% ({hits}/{hits + miss} calls)"
                + (f"   |   prefix reuse {pr * 100:.0f}%" if pr is not None else "")
            )
        fresh_io, ctx_io = io.get("fresh"), io.get("context")
        if fresh_io is not None:
            lines.append(
                f"  in:out  {fresh_io} fresh:gen   |   {ctx_io} context:gen"
            )
    ws_calls = tok.get("whitespace_calls", 0)
    if ws_calls:
        lines.append(
            f"  ({ws_calls} call(s) whitespace-approx — server predates cache "
            f"telemetry: {tok.get('whitespace_in', 0):,} in / "
            f"{tok.get('whitespace_out', 0):,} out)"
        )
    lines.append("")
    return lines


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

    # ── The head: finite time breakdown + cache-aware token panel ──────
    # Prefer the companion summary.json (authoritative — it has flush/
    # persistence time the events don't carry); else recompute from events,
    # using the legacy Σ cycle_duration_ms as the wall-clock denominator.
    summary = _load_summary_json(trace_path)
    if summary is None:
        summary = summarize_events(events, total_wall_ms=total_duration_ms)
    lines.extend(render_finite_breakdown(summary))

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

    # Observability-layer counts. Zero is a meaningful signal here — the
    # instrumentation is new (see §4.7 plumbing + Tier 2 events), so
    # "0 session_start events" in a trace may mean "ran before
    # instrumentation landed" rather than "no sessions happened."
    # Rendering the counts even when zero makes that unambiguous.
    session_starts = sum(1 for e in events if e["event_type"] == "session_start")
    session_ends = sum(1 for e in events if e["event_type"] == "session_end")
    commands = sum(1 for e in events if e["event_type"] == "command_run")
    command_timeouts = sum(
        1
        for e in events
        if e["event_type"] == "command_run" and e.get("timed_out", False)
    )
    mcp_calls = sum(1 for e in events if e["event_type"] == "mcp_tool_call")
    mcp_errors = sum(
        1 for e in events if e["event_type"] == "mcp_tool_call" and e.get("error", "")
    )
    notes = sum(1 for e in events if e["event_type"] == "note_pushed")

    lines.append(f"  Sessions: {session_starts} started, {session_ends} ended")
    timeout_note = f", {command_timeouts} timed out" if command_timeouts else ""
    lines.append(f"  Commands: {commands}{timeout_note}")
    mcp_err_note = f", {mcp_errors} errors" if mcp_errors else ""
    lines.append(f"  MCP tool calls: {mcp_calls}{mcp_err_note}")
    lines.append(f"  Notes pushed: {notes}")
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
            # Include chain-of-thought content in detail view
            thinking = e.get("thinking_content", "")
            if thinking:
                # Indent and truncate thinking for readability
                thinking_lines = thinking.strip().splitlines()
                lines.append("    💭 CoT:")
                for tl in thinking_lines[:20]:
                    lines.append(f"      {tl}")
                if len(thinking_lines) > 20:
                    lines.append(f"      ... ({len(thinking_lines) - 20} more lines)")
            # Include full prompt when --trace-prompts was used
            prompt_content = e.get("prompt_content", "")
            if prompt_content:
                prompt_lines = prompt_content.strip().splitlines()
                lines.append("    📝 Prompt:")
                for pl in prompt_lines[:30]:
                    lines.append(f"      {pl}")
                if len(prompt_lines) > 30:
                    lines.append(f"      ... ({len(prompt_lines) - 30} more lines)")
            # Include raw model response when --trace-prompts was used
            response_content = e.get("response_content", "")
            if response_content:
                resp_lines = response_content.strip().splitlines()
                lines.append("    📤 Response:")
                for rl in resp_lines[:30]:
                    lines.append(f"      {rl}")
                if len(resp_lines) > 30:
                    lines.append(f"      ... ({len(resp_lines) - 30} more lines)")

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

        elif et == "session_start":
            lines.append(
                f"    ◈ session_start {e.get('session_id', '')!r} "
                f"(config={e.get('config', {})})"
            )

        elif et == "session_end":
            status = "ok" if e.get("success", False) else "no-op"
            lines.append(
                f"    ◇ session_end {e.get('session_id', '')!r} "
                f"[{status}] ({e.get('wall_ms', 0):.0f}ms)"
            )

        elif et == "command_run":
            timed_out = " TIMEOUT" if e.get("timed_out", False) else ""
            lines.append(
                f"    $ {e.get('command', '')} "
                f"→ rc={e.get('return_code', 0)}{timed_out} "
                f"({e.get('wall_ms', 0):.0f}ms)"
            )
            stdout_prev = e.get("stdout_preview", "")
            if stdout_prev.strip():
                out_lines = stdout_prev.splitlines()
                lines.append("      stdout:")
                for ol in out_lines[:15]:
                    lines.append(f"        {ol}")
                if len(out_lines) > 15:
                    lines.append(f"        ... ({len(out_lines) - 15} more lines)")
            stderr_prev = e.get("stderr_preview", "")
            if stderr_prev.strip():
                err_lines = stderr_prev.splitlines()
                lines.append("      stderr:")
                for el in err_lines[:15]:
                    lines.append(f"        {el}")
                if len(err_lines) > 15:
                    lines.append(f"        ... ({len(err_lines) - 15} more lines)")

        elif et == "mcp_tool_call":
            error = e.get("error", "")
            status = "error" if error else "ok"
            lines.append(
                f"    ⊕ mcp {e.get('server', '')}/{e.get('tool', '')} "
                f"args={e.get('arg_keys', [])} "
                f"[{status}] ({e.get('wall_ms', 0):.0f}ms)"
            )
            if error:
                lines.append(f"      error: {error}")
            result_prev = e.get("result_preview", "")
            if result_prev.strip():
                rp_lines = result_prev.splitlines()
                lines.append("      result:")
                for rl in rp_lines[:10]:
                    lines.append(f"        {rl}")
                if len(rp_lines) > 10:
                    lines.append(f"        ... ({len(rp_lines) - 10} more lines)")

        elif et == "note_pushed":
            ok_mark = "✓" if e.get("success", True) else "✗"
            lines.append(
                f"    📝 note [{e.get('category', '')}] "
                f"{ok_mark} tags={e.get('tags', [])} "
                f"source={e.get('source_flow', '')!r}"
            )
            preview = e.get("content_preview", "")
            if preview.strip():
                p_lines = preview.splitlines()
                for pl in p_lines[:6]:
                    lines.append(f"      │ {pl}")
                if len(p_lines) > 6:
                    lines.append(f"      │ ... ({len(p_lines) - 6} more lines)")

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


def _derive_output_path(events: list[dict], output_override: str | None) -> str:
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
    output_path = _derive_output_path(events, output_override)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.write("\n")
    print(f"✅ Trace written to {output_path}")
