"""Finite time + cache-aware token summary — the trace "head" contract.

Goal 1: every second of a run lands in a sub-category; categories + residual
== total wall, residual ≥ 0, and residual/total ("completeness") is the metric.
Goal 2: tokens split into cached-prefix vs fresh-prefill vs generated, with the
in/out ratio computed on real backend counts (whitespace only as fallback).
Goal 3 (soundness): old trace JSONL (no new fields) still folds/renders.

These exercise the pure ledger (agent.trace) and the markdown head (trace_cli)
with no LLMVP / no effects — the summary math is deterministic.
"""

from __future__ import annotations

import json

from agent.trace import (
    TIME_CATEGORIES,
    fold_event,
    finalize_ledger,
    ledger_add_ms,
    new_ledger,
    summarize_events,
)


def _events():
    return [
        {"event_type": "cycle_start", "flow": "f", "cycle": 0},
        {
            "event_type": "step_start",
            "flow": "f",
            "step": "plan",
            "input_build_ms": 4.0,
        },
        {
            "event_type": "inference_call",
            "flow": "f",
            "step": "plan",
            "wall_ms": 1000.0,
            "prompt_render_ms": 30.0,
            "pre_compute_ms": 12.0,
            "cached_prefix_tokens": 900,
            "fresh_prefill_tokens": 100,
            "generated_tokens": 50,
            "cache_hit": True,
        },
        {
            "event_type": "inference_call",
            "flow": "f",
            "step": "judge",
            "wall_ms": 500.0,
            "tokens_in": 42,
            "tokens_out": 7,  # whitespace fallback
        },
        {"event_type": "command_run", "flow": "f", "wall_ms": 300.0},
        {"event_type": "mcp_tool_call", "flow": "f", "wall_ms": 200.0},
        {"event_type": "step_end", "flow": "f", "step": "plan", "resolver_ms": 2.0},
        {
            "event_type": "cycle_end",
            "flow": "f",
            "cycle": 0,
            "cycle_duration_ms": 2100.0,
            "projection_ms": 8.0,
            "tail_resolution_ms": 3.0,
        },
    ]


def test_categories_plus_residual_equals_total():
    led = new_ledger()
    for e in _events():
        fold_event(led, e)
    ledger_add_ms(led, "persistence", 15.0)
    ledger_add_ms(led, "flush", 5.0)
    total = 2500.0
    s = finalize_ledger(led, total)
    # exhaustive partition: leaves + residual == total, exactly
    assert abs(s["accounted_ms"] + s["residual_ms"] - total) < 1e-6
    # residual is non-negative by construction (leaves are disjoint sub-spans)
    assert s["residual_ms"] >= 0
    # completeness is the accounted fraction
    assert abs(s["completeness_pct"] - 100.0 * s["accounted_ms"] / total) < 1e-6
    # each measured category landed where expected (disjoint)
    t = s["time_ms"]
    assert t["inference"] == 1500.0  # 1000 + 500
    assert t["terminal"] == 300.0 and t["mcp"] == 200.0
    assert t["prompt_render"] == 30.0 and t["pre_compute"] == 12.0
    assert t["input_build"] == 4.0 and t["resolver"] == 2.0
    assert t["projection"] == 8.0 and t["tail_resolution"] == 3.0
    assert t["persistence"] == 15.0 and t["flush"] == 5.0


def test_no_category_double_counts():
    # every TIME_CATEGORIES key is present; sum of leaves never exceeds total
    led = new_ledger()
    for e in _events():
        fold_event(led, e)
    s = finalize_ledger(led, 10_000.0)
    assert set(s["time_ms"].keys()) == set(TIME_CATEGORIES)
    assert sum(s["time_ms"].values()) <= 10_000.0 + 1e-6


def test_cache_aware_tokens_and_ratios():
    led = new_ledger()
    for e in _events():
        fold_event(led, e)
    s = finalize_ledger(led, 2500.0)
    tok = s["tokens"]
    # real counts (call 1) tracked separately from whitespace fallback (call 2)
    assert tok["cached_prefix"] == 900 and tok["fresh_prefill"] == 100
    assert tok["generated"] == 50 and tok["real_input_total"] == 1000
    assert tok["real_calls"] == 1 and tok["whitespace_calls"] == 1
    assert tok["whitespace_in"] == 42 and tok["whitespace_out"] == 7
    # cache hit-rate counts ONLY real-token calls (1 real, it was a hit)
    assert s["cache"]["hit"] == 1 and s["cache"]["miss"] == 0
    assert s["cache"]["hit_rate"] == 1.0
    assert s["cache"]["prefix_reuse_rate"] == round(900 / 1000, 4)
    # in/out reported both ways: fresh compute vs full context
    assert s["io_ratio"]["fresh"] == round(100 / 50, 3)
    assert s["io_ratio"]["context"] == round(1000 / 50, 3)


def test_per_flow_rollup_sums_to_inference_total():
    led = new_ledger()
    for e in _events():
        fold_event(led, e)
    s = finalize_ledger(led, 2500.0)
    f = s["flows"]["f"]
    assert f["cycles"] == 1 and f["inferences"] == 2
    assert f["inference_ms"] == 1500.0 == s["time_ms"]["inference"]
    # per-flow real tokens roll up to the global real totals
    assert f["cached_prefix"] == s["tokens"]["cached_prefix"]
    assert f["generated"] == s["tokens"]["generated"]


def test_real_counts_take_precedence_over_whitespace():
    # a call with BOTH real and (stale) whitespace fields → real wins, ws ignored
    led = new_ledger()
    fold_event(
        led,
        {
            "event_type": "inference_call",
            "flow": "f",
            "tokens_in": 5,
            "tokens_out": 1,  # whitespace (should be ignored)
            "cached_prefix_tokens": 800,
            "fresh_prefill_tokens": 200,
            "generated_tokens": 40,
        },
    )
    s = finalize_ledger(led, 1000.0)
    assert s["tokens"]["real_input_total"] == 1000  # not 5
    assert s["tokens"]["generated"] == 40  # not 1
    assert s["tokens"]["whitespace_calls"] == 0  # the real call didn't fall back


def test_old_trace_without_new_fields_folds_to_zeros():
    # back-compat: a trace from before this change (only legacy fields) must
    # fold without KeyError and contribute its inference/whitespace cleanly.
    old = [
        {"event_type": "cycle_start", "flow": "f", "cycle": 0},
        {
            "event_type": "inference_call",
            "flow": "f",
            "wall_ms": 100.0,
            "tokens_in": 80,
            "tokens_out": 20,
        },
        {
            "event_type": "cycle_end",
            "flow": "f",
            "cycle": 0,
            "cycle_duration_ms": 120.0,
        },
    ]
    s = summarize_events(old)  # total falls back to Σ cycle_duration_ms = 120
    assert s["total_wall_ms"] == 120.0
    assert s["time_ms"]["inference"] == 100.0
    # no real counts → all in whitespace fallback, no cache stats
    assert s["tokens"]["whitespace_in"] == 80 and s["tokens"]["whitespace_out"] == 20
    assert s["tokens"]["cached_prefix"] == 0
    assert s["cache"]["hit_rate"] is None and s["io_ratio"]["fresh"] is None


def test_render_head_from_events_and_companion_json(tmp_path):
    from agent.trace_cli import render_summary, load_events, _load_summary_json

    trace = tmp_path / "m1_20260619T000000.jsonl"
    trace.write_text("\n".join(json.dumps(e) for e in _events()) + "\n")
    out = render_summary(load_events(str(trace)), str(trace))
    assert "Time Breakdown" in out and "Tokens (cache-aware)" in out
    assert "residual" in out

    # companion summary.json is preferred when present
    summary = summarize_events(_events(), total_wall_ms=2500.0)
    (tmp_path / "m1_20260619T000000.summary.json").write_text(
        json.dumps({"summary": summary})
    )
    assert _load_summary_json(str(trace)) is not None
    out2 = render_summary(load_events(str(trace)), str(trace))
    assert "Time Breakdown" in out2
