#!/usr/bin/env python3
"""Graph the decode ceiling: aggregate throughput (y) vs concurrent streams (x).

Plots the true measurement against the artifact on the same axes, because the
whole point of the experiment is that these two diverge and only one of them
is a throughput.

Usage: python plot.py [results.json] [-o ceiling.png]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def main() -> None:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="?", default=str(here / "results.json"))
    ap.add_argument("-o", "--out", default=str(here / "ceiling.png"))
    args = ap.parse_args()

    d = json.loads(Path(args.results).read_text())
    rows = sorted(d["rows"], key=lambda r: r["n"])
    n = [r["n"] for r in rows]
    agg = [r["aggregate_tok_s"] for r in rows]
    sor = [r["sum_of_rates"] for r in rows]
    seats = (d.get("server") or {}).get("poolSize")

    fig, (ax, ax2) = plt.subplots(
        2, 1, figsize=(10, 9), gridspec_kw={"height_ratios": [2, 1]}, sharex=True
    )

    ax.plot(
        n,
        agg,
        "o-",
        lw=2.4,
        ms=7,
        color="#1b6ca8",
        label="aggregate tok/s (TRUE throughput)",
    )
    ax.plot(
        n,
        sor,
        "s--",
        lw=1.6,
        ms=5,
        color="#c44",
        alpha=0.85,
        label="mean per-stream decode x N (ARTIFACT — not a throughput)",
    )

    peak = max(rows, key=lambda r: r["aggregate_tok_s"])
    ax.axhline(peak["aggregate_tok_s"], color="#1b6ca8", ls=":", alpha=0.5)
    ax.annotate(
        f"measured ceiling ≈ {peak['aggregate_tok_s']:.0f} tok/s (N={peak['n']})",
        xy=(peak["n"], peak["aggregate_tok_s"]),
        xytext=(8, 14),
        textcoords="offset points",
        fontsize=10,
        color="#1b6ca8",
        fontweight="bold",
    )
    # the claim under test
    ax.axhline(200, color="#888", ls="-.", alpha=0.7)
    ax.annotate(
        "the '~200 tok/s at N=64' claim under test",
        xy=(n[len(n) // 2], 200),
        xytext=(0, 6),
        textcoords="offset points",
        fontsize=9,
        color="#555",
    )
    if seats:
        ax.axvline(seats, color="#999", ls=":", alpha=0.6)
        ax.annotate(
            f"server seats = {seats}",
            xy=(seats, ax.get_ylim()[1] * 0.06),
            rotation=90,
            fontsize=8,
            color="#666",
            ha="right",
        )

    ax.set_ylabel("tokens / second")
    ax.set_title(
        "gpt-oss-120b decode ceiling — aggregate throughput vs concurrent streams\n"
        f"(batched engine, {d.get('gen', 256)}-token generations, small prompts)",
        fontsize=12,
    )
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.3)
    ax.set_xscale("log", base=2)

    ax2.plot(
        n,
        [r["per_stream_decode_tok_s"] for r in rows],
        "^-",
        color="#2a8",
        lw=1.8,
        label="per-stream decode tok/s",
    )
    ax2.plot(
        n,
        [r["latency_p95_s"] for r in rows],
        "v-",
        color="#a5a",
        lw=1.4,
        label="p95 latency (s)",
    )
    ax2.set_xlabel("N — concurrent streams")
    ax2.set_ylabel("per-stream rate / latency")
    ax2.legend(fontsize=9)
    ax2.grid(alpha=0.3)
    ax2.set_xscale("log", base=2)
    ax2.set_xticks(n)
    ax2.set_xticklabels([str(x) for x in n])

    fig.tight_layout()
    fig.savefig(args.out, dpi=140)
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
