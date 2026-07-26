#!/usr/bin/env python3
"""Prefill ceiling: does concurrent prefill parallelize, and how far?

The decode ladder (dev/decode_ceiling/) answered "how many seats" and found
seats nearly free — but every real workload we run is PREFILL-dominated, and
that axis has never been measured across a grid. The 2026-07-26 corpus regen
made the gap obvious: 48 concurrent streams delivered ~100 tok/s effective
where 16 delivered 113. Seats did not help. This measures why.

THE METRIC CONFUSION THIS EXISTS TO SETTLE. `aggregate_tok_s` in the decode
bench is GENERATED tokens / wall, so prefill sits in the denominator while
contributing nothing to the numerator — an "effective end-to-end" number, not
a decode rate. That is why the same engine reads 271 tok/s on 20-token prompts
and ~100 on real ones. Here we measure prefill on its own terms.

HEADLINE MEASUREMENT — the serialization factor:

    serialization = (N × single_stream_prefill_time) / wall_for_N_concurrent

    ≈ N   -> perfectly parallel (concurrency is free)
    ≈ 1   -> fully serialized (seats CANNOT help prefill-bound work)

PRE-REGISTERED PREDICTION (2026-07-26, before running):
  Decode at N=1 is overhead-bound, not bandwidth-bound — 50.3 tok/s × ~2.5 GB
  per forward pass ≈ 125 GB/s against ~800 GB/s peak, i.e. ~16% utilization.
  Batching drives it to ~678 GB/s (~85%), which is the measured 5.41× gain.
  Prefill is the opposite: matrix-MATRIX, high arithmetic intensity, ~7.9
  TFLOPS at 794 tok/s single-stream — the GPU is already busy. So concurrency
  should help prefill far less than decode. Expected serialization **1.5-2x**,
  NOT the 1.0 that serving_perf_reference §2 asserts from a single
  6-stream observation, because the batched engine interleaves prefill chunks
  with decode tokens in one llama_decode and can share a batch. The thing that
  could still force ~1.0 is Metal's single shared MTLCommandQueue.

  Recording this BEFORE the run so the result can embarrass it.

Usage:
  python bench.py                       # full grid
  python bench.py --sizes 1000,4000 --ladder 1,4,16
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import statistics as st
import time
import urllib.request
from pathlib import Path

URL = os.environ.get("BENCH_URL", "http://127.0.0.1:8008/graphql")

# promptTokens + prefillMs are the whole point; cachedPrefixTokens proves we
# are measuring COLD prefill rather than our own cache.
Q = """query($p:String!,$m:Int!){completion(request:{prompt:$p,maxTokens:$m,temperature:0.7}){
  generatedTokens promptTokens prefillMs decodeMs cachedPrefixTokens freshPrefillTokens}}"""

HEALTH = "{ health { status poolSize kvPoolTokens } }"


def gql(q: str, variables: dict | None = None, timeout: int = 1800) -> dict:
    body = {"query": q}
    if variables:
        body["variables"] = variables
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def make_prompt(target_tokens: int, uniq: str) -> str:
    """A prompt of ~target_tokens that is UNIQUE per request.

    Uniqueness is not cosmetic: prefix caching is the thing this project has
    worked hardest on, and identical prompts would measure cache hits instead
    of prefill. The unique marker goes FIRST so no two requests share a prefix
    at all.
    """
    head = f"[session {uniq}] "
    # ~4 chars/token is the calibrated ratio for this corpus family
    filler = (
        "The quick brown fox examines the repository state and considers "
        "the next action carefully. "
    )
    n = max(1, int(target_tokens * 4 / len(filler)) + 1)
    return head + (filler * n)


def one(args) -> dict:
    idx, size, gen = args
    prompt = make_prompt(size, f"{idx}-{time.time_ns()}")
    t0 = time.time()
    try:
        r = gql(Q, {"p": prompt, "m": gen})
    except Exception as exc:  # noqa: BLE001
        return {"err": f"{type(exc).__name__}: {exc}"[:140]}
    wall = time.time() - t0
    if r.get("errors"):
        return {"err": str(r["errors"][0].get("message", ""))[:140]}
    c = r["data"]["completion"]
    return {
        "prompt_tok": c.get("promptTokens") or 0,
        "fresh": c.get("freshPrefillTokens") or 0,
        "cached": c.get("cachedPrefixTokens") or 0,
        "prefill_ms": c["prefillMs"],
        "decode_ms": c["decodeMs"],
        "gen": c["generatedTokens"],
        "wall": wall,
    }


def cell(size: int, n: int, gen: int, baseline_s: float | None) -> dict:
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        res = list(ex.map(one, [(i, size, gen) for i in range(n)]))
    wall = time.time() - t0
    ok = [r for r in res if "prompt_tok" in r]
    errs = [r for r in res if "err" in r]
    if not ok:
        return {
            "size": size,
            "n": n,
            "errors": len(errs),
            "error_samples": [e["err"] for e in errs[:2]],
        }
    ptok = sum(r["prompt_tok"] for r in ok)
    cached = sum(r["cached"] for r in ok)
    per_prefill = [
        r["prompt_tok"] / (r["prefill_ms"] / 1000) for r in ok if r.get("prefill_ms")
    ]
    single = st.mean([r["prefill_ms"] / 1000 for r in ok]) if ok else 0
    out = {
        "size": size,
        "n": n,
        "prompt_tokens_total": ptok,
        "cached_tokens": cached,  # must be ~0 or we measured the cache
        "wall_s": round(wall, 1),
        "aggregate_prefill_tok_s": round(ptok / wall, 1) if wall else 0,
        "per_request_prefill_tok_s": (
            round(st.mean(per_prefill), 1) if per_prefill else 0
        ),
        "mean_prefill_s": round(single, 2),
        "ttft_p50_s": round(st.median([r["prefill_ms"] / 1000 for r in ok]), 2),
        "errors": len(errs),
    }
    # THE headline: how much of N-way concurrency actually materialized
    if baseline_s:
        out["serialization_x"] = round((n * baseline_s) / wall, 2) if wall else 0
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="1000,4000,16000,48000")
    ap.add_argument("--ladder", default="1,4,16,48")
    ap.add_argument(
        "--gen",
        type=int,
        default=16,
        help="tiny by design — we are measuring prefill, not decode",
    )
    ap.add_argument("--out", default=str(Path(__file__).parent / "results.json"))
    args = ap.parse_args()

    h = gql(HEALTH)["data"]["health"]
    pool = h.get("kvPoolTokens") or 0
    print(f"server: {h}\n")

    sizes = [int(x) for x in args.sizes.split(",")]
    ladder = [int(x) for x in args.ladder.split(",")]
    rows, skipped = [], []

    print(
        f"{'prompt':>8} {'N':>4} {'agg prefill':>12} {'per-req':>9} "
        f"{'serial':>7} {'ttft p50':>9} {'cached':>8} {'err':>4}"
    )
    print("-" * 70)
    for size in sizes:
        baseline = None
        for n in ladder:
            # A cell that cannot fit the pool would measure eviction, not
            # prefill. Skip it LOUDLY — a silent omission reads as coverage.
            need = size * n
            if pool and need > 0.8 * pool:
                skipped.append((size, n, need))
                print(
                    f"{size:>8} {n:>4}   SKIPPED — {need:,} tokens exceeds 80% of the "
                    f"{pool:,}-cell pool"
                )
                continue
            r = cell(size, n, args.gen, baseline)
            if n == 1 and r.get("mean_prefill_s"):
                baseline = r["mean_prefill_s"]
                r["serialization_x"] = 1.0
            rows.append(r)
            print(
                f"{size:>8} {n:>4} {r.get('aggregate_prefill_tok_s',0):>12.1f} "
                f"{r.get('per_request_prefill_tok_s',0):>9.1f} "
                f"{r.get('serialization_x','-'):>7} {r.get('ttft_p50_s',0):>9.2f} "
                f"{r.get('cached_tokens',0):>8} {r.get('errors',0):>4}"
            )
            if r.get("cached_tokens", 0) > 0.05 * r.get("prompt_tokens_total", 1):
                print(
                    "        !! cache hits detected — this cell measured the CACHE, "
                    "not cold prefill"
                )

    print("-" * 70)
    if skipped:
        print(
            f"skipped {len(skipped)} cell(s) that exceed the pool — listed above, "
            f"NOT silently dropped"
        )
    best = [r for r in rows if r.get("serialization_x")]
    if best:
        top = max(best, key=lambda r: r["serialization_x"])
        print(
            f"MAX serialization factor: {top['serialization_x']}x "
            f"(prompt {top['size']}, N={top['n']})"
        )
        print(
            "  ~1.0 => prefill is fully serialized; seats cannot help "
            "prefill-bound work."
        )
        print("  ~N   => prefill parallelizes; seats are the lever after all.")
    Path(args.out).write_text(
        json.dumps({"server": h, "rows": rows, "skipped": skipped}, indent=2)
    )
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
