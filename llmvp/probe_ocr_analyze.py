#!/usr/bin/env python3
"""Decompose the captured OCR wire trace into decode / encode / fixed.

Reads probe_out/ocr_wire.jsonl (written by probe_ocr_proxy.py while the real
extract_batch.py ran) and answers Phase 0's gate question: what fraction of a
production OCR request is DECODE — the only stage batching multiplexes?

MODEL
    wall = a + b*completion_tokens + c*pixel_area

  a  fixed per-request cost (HTTP, base64 intake, instance checkout, the
     per-request context clear in acquire_vision_instance)
  b  ms per generated token -> decode
  c  ms per pixel -> encode (CLIP + projector), which scales with crop area

WHY AREA IS IN THE FIT. Bigger crops hold more text, so area and output
length are correlated. A single-variable fit would hand the decode slope
part of the encode cost and OVERSTATE decode's share — the direction that
would wrongly justify building the batched path. Fitting both separates them.

QUEUE CONTAMINATION. paddlex fans 4-wide into a pool of 2, so rows recorded
at concurrency > pool_width include time spent WAITING, not working. The fit
uses only rows at concurrency 1; the others are reported separately as the
queueing evidence they are.
"""

from __future__ import annotations

import json
import os
import statistics
import sys

WIRE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "probe_out", "ocr_wire.jsonl")


def solve3(rows):
    """Least squares for wall = a + b*tok + c*area (normal equations)."""
    n = len(rows)
    if n < 6:
        return None
    xs = [(1.0, float(r["completion_tokens"]), float(r["area"])) for r in rows]
    ys = [float(r["wall_ms"]) for r in rows]
    # 3x3 X'X and X'y
    A = [[sum(xs[k][i] * xs[k][j] for k in range(n)) for j in range(3)]
         for i in range(3)]
    bvec = [sum(xs[k][i] * ys[k] for k in range(n)) for i in range(3)]
    # Gaussian elimination with partial pivoting
    M = [A[i][:] + [bvec[i]] for i in range(3)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return None
        M[col], M[piv] = M[piv], M[col]
        for r in range(3):
            if r == col:
                continue
            f = M[r][col] / M[col][col]
            for c in range(col, 4):
                M[r][c] -= f * M[col][c]
    return [M[i][3] / M[i][i] for i in range(3)]


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else WIRE
    rows = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if r.get("status") == 200 and r.get("n_images") and r.get("area"):
            rows.append(r)
    if not rows:
        print("no usable rows", file=sys.stderr)
        return 2

    toks = [r["completion_tokens"] for r in rows]
    areas = [r["area"] for r in rows]
    walls = [r["wall_ms"] for r in rows]
    print("=" * 70)
    print(f"OCR WIRE TRACE — {len(rows)} vision requests")
    print("=" * 70)
    print(f"  crop area px    median {statistics.median(areas):>10,.0f}   "
          f"p10 {sorted(areas)[len(areas)//10]:>8,}  p90 {sorted(areas)[9*len(areas)//10]:>9,}")
    print(f"  output tokens   median {statistics.median(toks):>10.0f}   "
          f"p10 {sorted(toks)[len(toks)//10]:>8}  p90 {sorted(toks)[9*len(toks)//10]:>9}")
    print(f"  wall ms         median {statistics.median(walls):>10.0f}   "
          f"p10 {sorted(walls)[len(walls)//10]:>8.0f}  p90 {sorted(walls)[9*len(walls)//10]:>9.0f}")
    print(f"  total wall      {sum(walls)/1000:.1f} s over "
          f"{(max(r['t'] for r in rows) - min(r['t'] for r in rows)):.1f} s elapsed")

    # concurrency profile — queueing evidence
    print("\n  concurrency  n      median wall ms")
    by_c: dict[int, list[float]] = {}
    for r in rows:
        by_c.setdefault(int(r.get("concurrency") or 1), []).append(r["wall_ms"])
    for c in sorted(by_c):
        v = by_c[c]
        print(f"    {c:>3}        {len(v):>5}   {statistics.median(v):>10.0f}")

    clean = [r for r in rows if int(r.get("concurrency") or 1) == 1]
    print(f"\n  uncontended rows (concurrency==1): {len(clean)}")
    fit_rows = clean if len(clean) >= 12 else rows
    if fit_rows is rows:
        print("  !! too few uncontended rows — fitting ALL rows; the decode")
        print("     share below is an UPPER bound (queue time inflates `a`,")
        print("     but token-correlated waits can leak into `b`).")

    sol = solve3(fit_rows)
    if not sol:
        print("fit failed", file=sys.stderr)
        return 3
    a, b, c = sol
    med_tok = statistics.median([r["completion_tokens"] for r in fit_rows])
    med_area = statistics.median([r["area"] for r in fit_rows])
    dec = b * med_tok
    enc = c * med_area
    tot = a + dec + enc
    print("\n" + "-" * 70)
    print("STAGE DECOMPOSITION  (wall = a + b*tokens + c*area)")
    print("-" * 70)
    print(f"  a  fixed        {a:9.1f} ms          {a/tot*100:5.1f} %")
    print(f"  b  decode       {b:9.3f} ms/tok  -> {dec:7.1f} ms  {dec/tot*100:5.1f} %"
          f"   ({1000/b if b > 0 else 0:.0f} tok/s)")
    print(f"  c  encode       {c*1e6:9.3f} ms/Mpx -> {enc:7.1f} ms  {enc/tot*100:5.1f} %")
    print(f"     modelled total at median request: {tot:.1f} ms "
          f"(observed median {statistics.median([r['wall_ms'] for r in fit_rows]):.0f} ms)")

    share = dec / tot if tot > 0 else 0.0
    pct = share * 100
    print("\n" + "-" * 70)
    print("PRE-REGISTERED VERDICT")
    print("-" * 70)
    print(f"  DECODE SHARE = {pct:.1f} %")
    if pct < 30:
        print("  -> DO NOT BUILD Phase 4 (M-RoPE batched path).")
        print("     Batching multiplexes decode and SERIALIZES encode behind")
        print("     MtmdEncoder's lock; the pool encodes in parallel. At this")
        print("     mix the pool is the correct architecture and batching")
        print("     would make OCR slower, not faster.")
    elif pct <= 50:
        print("  -> MARGINAL. Build only if the pool also plateaus below the")
        print("     width the fixed+encode budget implies is reachable.")
    else:
        print("  -> BUILD Phase 4, subject to P0c neighbour-KV integrity.")
    print(f"\n  (prediction registered 2026-08-29: encode-dominated, <40%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
