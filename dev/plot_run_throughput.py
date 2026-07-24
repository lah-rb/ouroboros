#!/usr/bin/env python3
"""Whole-RUN throughput figures (companion to plot_swarm_perf.py's
single-burst view).

Merges the two timing sources a run leaves behind:
  - the trace (<workdir>/.agent/traces/<id>.jsonl): every step inference
    with prefill_ms / decode_ms / generated_tokens / fresh_prefill_tokens
    and a monotonic end timestamp;
  - the swarm sidecar (<workdir>/.agent/swarm_perf.jsonl): fan-out worker
    completions (symbol workers carry full prefill/decode spans; content/
    diagnose workers carry generated tokens + t_done) anchored to wall
    time via each burst's t0_epoch.

Renders one PNG with:
  (a) TOTAL throughput vs run time — aggregate decode tok/s and prefill
      tok/s in fixed bins (each event's tokens spread uniformly over its
      measured span), with burst windows shaded;
  (b) PER-INSTANCE tracking — every inference span drawn as a lane
      segment (greedy interval packing = concurrent stream slots),
      colored by its own decode rate. Lane count over time IS the live
      concurrency; segment color shows the per-stream rate dividing as
      lanes fill (the 56/N law) and recovering as they drain.

Usage: uv run python dev/plot_run_throughput.py <workdir> [out.png]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

BIN_S = 10.0


def _load_trace(workdir: Path) -> list[dict]:
    traces = [
        p
        for p in (workdir / ".agent" / "traces").glob("*.jsonl")
        if "summary" not in p.name
    ]
    if not traces:
        return []
    spans = []
    for line in traces[0].open():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event_type") != "inference_call":
            continue
        end = float(e.get("timestamp", 0) or 0)
        wall_ms = float(e.get("wall_ms", 0) or 0)
        prefill_ms = float(e.get("prefill_ms", 0) or 0)
        decode_ms = float(e.get("decode_ms", 0) or 0)
        spans.append(
            {
                "label": f"{e.get('flow', '?')}/{e.get('step', '?')}",
                "end": end,
                "start": end - wall_ms / 1000.0,
                "prefill_s": prefill_ms / 1000.0,
                "decode_s": decode_ms / 1000.0,
                "gen": int(float(e.get("generated_tokens", 0) or 0)),
                "prefill_tok": int(float(e.get("fresh_prefill_tokens", 0) or 0)),
                "wall_end": e.get("wall_time", ""),
                "kind": "step",
            }
        )
    return spans


def _wall_to_mono(spans: list[dict]) -> tuple[float, float] | None:
    """(epoch, monotonic) anchor from any trace event carrying both."""
    for s in spans:
        if s["wall_end"]:
            try:
                epoch = datetime.fromisoformat(s["wall_end"]).timestamp()
                return epoch, s["end"]
            except ValueError:
                continue
    return None


def _load_sidecar(workdir: Path, anchor: tuple[float, float] | None) -> list[dict]:
    path = workdir / ".agent" / "swarm_perf.jsonl"
    if not path.exists() or anchor is None:
        return []
    epoch0, mono0 = anchor
    spans = []
    burst_t0_mono = None
    for line in path.open():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") == "burst":
            burst_t0_mono = float(r.get("t0_epoch", 0) or 0) - epoch0 + mono0
            continue
        if r.get("event") != "worker" or burst_t0_mono is None:
            continue
        t_done = burst_t0_mono + float(r.get("t_done", 0) or 0)
        gen = int(float(r.get("generated_tokens", 0) or 0))
        if "prefill_ms" in r:  # symbol workers: full span data
            t_submit = burst_t0_mono + float(r.get("t_submit", 0) or 0)
            prefill_s = float(r.get("prefill_ms", 0) or 0) / 1000.0
            decode_s = float(r.get("decode_ms", 0) or 0) / 1000.0
            start = t_done - prefill_s - decode_s
            if start < t_submit:
                start = t_submit
        else:  # content/diagnose workers: approximate span = t0..t_done
            t_submit = burst_t0_mono + float(r.get("t_submit", 0) or 0)
            start = t_submit
            prefill_s = 0.0
            decode_s = max(t_done - start, 1e-3)
        spans.append(
            {
                "label": r.get("kind", "worker"),
                "start": start,
                "end": t_done,
                "prefill_s": prefill_s,
                "decode_s": decode_s,
                "gen": gen,
                "prefill_tok": int(float(r.get("prompt_tokens", 0) or 0)),
                "kind": "swarm",
            }
        )
    return spans


def plot(spans: list[dict], out: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.cm import viridis
    from matplotlib.colors import Normalize

    t0 = min(s["start"] for s in spans)
    t1 = max(s["end"] for s in spans)
    n_bins = max(int((t1 - t0) / BIN_S) + 1, 1)
    decode_rate = [0.0] * n_bins
    prefill_rate = [0.0] * n_bins

    def _spread(tok: float, a: float, b: float, series: list[float]) -> None:
        if tok <= 0 or b <= a:
            return
        rate = tok / (b - a)
        i0, i1 = int((a - t0) / BIN_S), int((b - t0) / BIN_S)
        for i in range(max(i0, 0), min(i1, n_bins - 1) + 1):
            lo, hi = t0 + i * BIN_S, t0 + (i + 1) * BIN_S
            series[i] += rate * max(0.0, min(b, hi) - max(a, lo)) / BIN_S

    for s in spans:
        d_start = s["end"] - s["decode_s"]
        _spread(s["gen"], d_start, s["end"], decode_rate)
        if s["prefill_s"] > 0:
            _spread(s["prefill_tok"], d_start - s["prefill_s"], d_start, prefill_rate)

    # Decode and prefill get SEPARATE panels: prefill peaks (thousands of
    # tok/s at burst admission) are 50x the decode scale and flatten it to
    # the floor on a shared axis.
    fig, (ax_decode, ax_prefill, ax_lanes) = plt.subplots(
        3, 1, figsize=(14, 11), sharex=True, height_ratios=[1, 0.7, 1.4]
    )
    xs = [(t0 + (i + 0.5) * BIN_S - t0) / 60.0 for i in range(n_bins)]
    ax_decode.plot(xs, decode_rate, color="tab:blue", lw=1.6)
    ax_prefill.plot(xs, prefill_rate, color="tab:orange", lw=1.2)
    for ax, label in ((ax_decode, "decode tok/s"), (ax_prefill, "prefill tok/s")):
        for s in spans:
            if s["kind"] == "swarm":
                ax.axvspan(
                    (s["start"] - t0) / 60.0,
                    (s["end"] - t0) / 60.0,
                    color="tab:green",
                    alpha=0.03,
                    lw=0,
                )
        ax.set_ylabel(f"aggregate {label}")
        ax.grid(True, axis="y", lw=0.5, alpha=0.4)
        ax.set_ylim(bottom=0)
    ax_decode.set_title(title)

    # Greedy lane packing: lane = a concurrent stream slot.
    lanes: list[float] = []
    norm = Normalize(vmin=0, vmax=60)
    for s in sorted(spans, key=lambda x: x["start"]):
        lane = next((i for i, free in enumerate(lanes) if free <= s["start"]), None)
        if lane is None:
            lane = len(lanes)
            lanes.append(0.0)
        lanes[lane] = s["end"]
        rate = s["gen"] / s["decode_s"] if s["decode_s"] > 0 else 0.0
        ax_lanes.barh(
            lane,
            (s["end"] - s["start"]) / 60.0,
            left=(s["start"] - t0) / 60.0,
            height=0.8,
            color=viridis(norm(rate)),
            edgecolor="none",
        )
    sm = plt.cm.ScalarMappable(cmap=viridis, norm=norm)
    fig.colorbar(sm, ax=ax_lanes, label="per-stream decode tok/s", pad=0.01)
    ax_lanes.set_ylabel(f"concurrent stream lanes (peak {len(lanes)})")
    ax_lanes.set_xlabel("run time (min)")
    ax_lanes.grid(True, axis="y", lw=0.5, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    print(f"wrote {out} ({len(spans)} spans, peak {len(lanes)} lanes)")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    workdir = Path(sys.argv[1]).expanduser()
    out = Path(sys.argv[2] if len(sys.argv) > 2 else workdir / "run_throughput.png")
    trace_spans = _load_trace(workdir)
    sidecar_spans = _load_sidecar(workdir, _wall_to_mono(trace_spans))
    spans = trace_spans + sidecar_spans
    if not spans:
        sys.exit(f"no inference spans found under {workdir}")
    print(f"{len(trace_spans)} trace spans + {len(sidecar_spans)} swarm worker spans")
    plot(spans, out, f"{workdir.name}: whole-run throughput")


if __name__ == "__main__":
    main()
