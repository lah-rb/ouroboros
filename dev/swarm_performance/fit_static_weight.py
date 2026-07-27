#!/usr/bin/env python3
"""Fit w in D_eff = D_private + w*D_static. Reproduces FINDINGS §8.

Requires BOTH configurations — w is unidentifiable from either alone, because
D_static is constant within a single config (see FINDINGS §8).

    uv run python dev/swarm_performance/fit_static_weight.py
"""
import collections, json
import numpy as np

R = "dev/swarm_performance/results/"
CONFIGS = [("swarm_regime_surface.json", 1809), ("nopersona_surface.json", 72)]


def main():
    rows = []
    for f, static in CONFIGS:
        d = json.load(open(R + f))
        for r in d["rows"]:
            rows.append((r["n"], r["context_depth"] - static + d["gen"] / 2,
                         static, r["decode_tok_s_aggregate"]))
    N = np.array([r[0] for r in rows], float)
    P = np.array([r[1] for r in rows], float)
    S = np.array([r[2] for r in rows], float)
    A = np.array([r[3] for r in rows], float)

    def mape(w):
        D = P + w * S
        X = np.column_stack([np.ones_like(N), np.log(N), np.log(N) * np.log(D), np.log(D)])
        b, *_ = np.linalg.lstsq(X, np.log(A), rcond=None)
        return 100 * np.abs((np.exp(X @ b) - A) / A).mean()

    grid = np.concatenate([np.arange(0, 0.31, 0.01), np.arange(0.35, 1.05, 0.05)])
    best = min(grid, key=mape)
    print(f"joint fit over {len(rows)} cells, D_eff = D_private + w*D_static\n")
    for w in (0.0, 0.05, 0.10, 0.20, 0.50, 1.0):
        note = {0.0: "  <- static FREE", 1.0: "  <- static costs like fresh"}.get(w, "")
        print(f"   w={w:<5.2f}  MAPE={mape(w):5.2f}%{note}")
    print(f"\n   BEST w = {best:.2f}  (MAPE {mape(best):.2f}%)")
    print(f"   a static token costs ~{100*best:.0f}% of a private token")

    # the fingerprint: gain from removing static SHRINKS with N
    idx = lambda f: {(r["size"], r["n"]): r for r in json.load(open(R + f))["rows"]}
    b, t = idx(CONFIGS[0][0]), idx(CONFIGS[1][0])
    print("\n   delta from removing 1737 static tokens (gain shrinks as N grows ->")
    print("   a per-STEP cost, not per-stream):")
    for size in (256, 2048):
        deltas = [f"N={n}: {100*(t[(size,n)]['decode_tok_s_aggregate']/b[(size,n)]['decode_tok_s_aggregate']-1):+5.1f}%"
                  for n in (4, 24, 96)]
        print(f"     size {size:<5} " + "   ".join(deltas))


if __name__ == "__main__":
    main()
