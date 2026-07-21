#!/usr/bin/env python3
"""Plot a swarm fan-out's per-worker performance over time.

Reads the tracking sidecar `swarm_generate_symbols` writes
(<working_dir>/.agent/swarm_perf.jsonl — see
agent/actions/contract_swarm_actions.py) and renders one PNG with:

  (a) active concurrent streams + cumulative generated tokens vs time,
  (b) a per-symbol gantt (submit→done per attempt; green ok, red fail,
      orange truncated/ramble),
  (c) RECONSTRUCTED per-stream decode rate r(t) + aggregate k(t)*r(t).

Panel (c) is a piecewise-constant least-squares reconstruction, not a
smear: in batched continuous decode every active stream earns tokens at
the same instantaneous rate (fair scheduling), so between stream
start/stop events the per-stream rate r_i is one unknown, and each
stream's known total = sum of overlap(stream, interval) * r_i. Decode
windows are [t_submit + prefill, t_done] per attempt. Validated against
the swarm2 run 2026-07-21: reconstruction reproduced the independent
scaling bench at N=1/3/4 (51≈56, 24.7≈27.5, 20.7≈22.5 tok/s) and proved
the release-bounce is real (per-stream 7.7 @ N=14 → 51 solo). Caveat:
the system is underdetermined on short (<2 s) slivers — those are
dropped from the plot.

Also prints a text summary (per-symbol actuals: prompt/generated tokens,
prefill/decode ms) so the numbers are usable without the image. The point:
verify the n=symbols self-annealing shape — early-EOS workers releasing
pool cells + decode share back to the big symbols (dev/serving_perf_reference.md).

Usage: .venv/bin/python dev/plot_swarm_perf.py <swarm_perf.jsonl> [out.png]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: Path) -> tuple[dict, list[dict], dict]:
    burst: dict = {}
    workers: list[dict] = []
    done: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = row.get("event")
        if ev == "burst":
            burst = row
        elif ev == "worker":
            workers.append(row)
        elif ev == "burst_done":
            done = row
    return burst, workers, done


def summarize(burst: dict, workers: list[dict], done: dict) -> None:
    print(
        f"burst: {burst.get('symbols')} symbols, sem={burst.get('sem')} "
        f"[{burst.get('gate')}], pool={burst.get('pool_budget')}, "
        f"est prompts={burst.get('est_prompt_tok_sum')} tok"
    )
    for w in sorted(workers, key=lambda r: r.get("t_submit", 0)):
        dur = (w.get("t_done") or 0) - (w.get("t_submit") or 0)
        dec = w.get("decode_ms") or 0
        gen = w.get("generated_tokens") or 0
        tps = f"{gen / (dec / 1000):.1f}" if dec else "—"
        state = (
            "TRUNC"
            if w.get("truncated")
            else ("ok" if w.get("ok") else (w.get("error_kind") or "fail"))
        )
        print(
            f"  {w.get('symbol', '?'):<22} a{w.get('attempt')} "
            f"[{w.get('t_submit', 0):>7.1f}s → {w.get('t_done', 0):>7.1f}s "
            f"({dur:>6.1f}s)] prompt={w.get('prompt_tokens', 0):<6} "
            f"gen={gen:<6} decode={tps:>6} tok/s  {state}"
        )
    if done:
        print(
            f"burst done @ {done.get('t_done')}s: {done.get('ok')} ok / "
            f"{done.get('failed')} failed / {done.get('retried')} retried, "
            f"{done.get('worker_tokens')} worker tokens"
        )


def _reconstruct_rates(
    workers: list[dict],
) -> list[tuple[float, float, int, float]]:
    """Piecewise-constant per-stream decode rate via least squares.

    Returns [(t_start, t_end, active_count, r_per_stream), ...] for
    intervals ≥ 2 s (shorter slivers are underdetermined noise).
    """
    import numpy as np

    rows = []
    for w in workers:
        gen = w.get("generated_tokens") or 0
        if gen <= 0:
            continue
        s = (w.get("t_submit") or 0) + (w.get("prefill_ms") or 0.0) / 1000.0
        e = w.get("t_done") or 0
        if e > s + 0.5:
            rows.append((s, e, float(gen)))
    if len(rows) < 2:
        return []
    edges = sorted({s for s, _, _ in rows} | {e for _, e, _ in rows})
    ivals = [(a, b) for a, b in zip(edges, edges[1:]) if b - a > 1e-6]
    A = np.zeros((len(rows), len(ivals)))
    g = np.array([gen for _, _, gen in rows])
    for j, (s, e, _) in enumerate(rows):
        for i, (a, b) in enumerate(ivals):
            A[j, i] = max(0.0, min(e, b) - max(s, a))
    r, *_ = np.linalg.lstsq(A, g, rcond=None)
    for _ in range(6):
        neg = r < 0
        if not neg.any():
            break
        keep = ~neg
        r = np.zeros_like(r)
        sub, *_ = np.linalg.lstsq(A[:, keep], g, rcond=None)
        r[keep] = sub
    out = []
    for i, (a, b) in enumerate(ivals):
        if b - a < 2.0:
            continue
        k = sum(1 for s, e, _ in rows if s < b and e > a)
        out.append((a, b, k, float(r[i])))
    return out


def plot(burst: dict, workers: list[dict], done: dict, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax_load, ax_gantt, ax_tps) = plt.subplots(
        3, 1, figsize=(12, 10), height_ratios=[1, 2, 1], sharex=True
    )
    t_end = max((w.get("t_done") or 0) for w in workers) if workers else 1.0

    # (a) active streams + cumulative tokens
    edges = sorted(
        {0.0, t_end}
        | {w.get("t_submit") or 0 for w in workers}
        | {w.get("t_done") or 0 for w in workers}
    )
    active = [
        sum(
            1 for w in workers if (w.get("t_submit") or 0) <= t < (w.get("t_done") or 0)
        )
        for t in edges
    ]
    ax_load.step(edges, active, where="post", color="tab:blue", label="active streams")
    ax_load.set_ylabel("active streams")
    ax_c = ax_load.twinx()
    by_done = sorted(workers, key=lambda w: w.get("t_done") or 0)
    cum, cum_t, cum_v = 0, [0.0], [0]
    for w in by_done:
        cum += w.get("generated_tokens") or 0
        cum_t.append(w.get("t_done") or 0)
        cum_v.append(cum)
    ax_c.plot(cum_t, cum_v, color="tab:green", alpha=0.7, label="cumulative tokens")
    ax_c.set_ylabel("cumulative generated tok")
    ax_load.set_title(
        f"swarm fan-out: {burst.get('symbols')} symbols, sem={burst.get('sem')} "
        f"[{burst.get('gate')}]"
    )

    # (b) gantt
    rows = sorted(workers, key=lambda w: (w.get("t_submit") or 0))
    labels, seen = [], {}
    for w in rows:
        key = f"{w.get('symbol')}#a{w.get('attempt')}"
        seen[key] = len(labels)
        labels.append(key)
        color = (
            "tab:orange"
            if w.get("truncated")
            else ("tab:green" if w.get("ok") else "tab:red")
        )
        y = seen[key]
        ax_gantt.barh(
            y,
            (w.get("t_done") or 0) - (w.get("t_submit") or 0),
            left=w.get("t_submit") or 0,
            height=0.6,
            color=color,
            alpha=0.85,
        )
        gen = w.get("generated_tokens") or 0
        if gen:
            ax_gantt.text(
                (w.get("t_done") or 0) + t_end * 0.005,
                y,
                f"{gen}",
                va="center",
                fontsize=7,
            )
    ax_gantt.set_yticks(range(len(labels)))
    ax_gantt.set_yticklabels(labels, fontsize=7)
    ax_gantt.invert_yaxis()
    ax_gantt.set_ylabel("worker attempts")

    # (c) reconstructed per-stream rate r(t) + aggregate — see module
    # docstring. Falls back to nothing (empty panel note) without numpy.
    try:
        recon = _reconstruct_rates(workers)
    except Exception as e:  # numpy missing / degenerate data
        recon = []
        ax_tps.text(0.02, 0.5, f"(reconstruction unavailable: {e})", fontsize=8)
    for a, b, k, r in recon:
        ax_tps.plot([a, b], [k * r, k * r], color="tab:purple", lw=2)
        ax_tps.plot([a, b], [r, r], color="tab:blue", lw=1.5)
    if recon:
        ax_tps.plot([], [], color="tab:purple", lw=2, label="aggregate k·r(t)")
        ax_tps.plot([], [], color="tab:blue", lw=1.5, label="per-stream r(t)")
        ax_tps.legend(fontsize=8, loc="upper right")
    ax_tps.set_ylabel("decode tok/s (reconstructed)")
    ax_tps.set_xlabel("seconds since burst start")

    fig.tight_layout()
    fig.savefig(out, dpi=130)
    print(f"wrote {out}")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".png")
    burst, workers, done = load(src)
    if not workers:
        sys.exit(f"no worker events in {src}")
    summarize(burst, workers, done)
    try:
        recon = _reconstruct_rates(workers)
        if recon:
            print("\nreconstructed decode rate (intervals ≥ 2 s):")
            print(f"{'interval':>19}  {'active':>6}  {'r tok/s':>8}  {'agg':>7}")
            for a, b, k, r in recon:
                print(f"[{a:8.1f},{b:8.1f})  {k:6d}  {r:8.1f}  {k*r:7.1f}")
    except Exception as e:  # noqa: BLE001 — summary must not fail on numpy
        print(f"(rate reconstruction unavailable: {e})")
    plot(burst, workers, done, out)


if __name__ == "__main__":
    main()
