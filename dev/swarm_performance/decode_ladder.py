#!/usr/bin/env python3
"""Decode ceiling: aggregate throughput vs concurrent stream count.

Settles the "~200 tok/s at N=64" claim that has been sitting uncited in four
swarm config headers. See METHODS.md for the full protocol and the reasoning
behind each choice.

THE CENTRAL MEASUREMENT TRAP this is built to expose: there are two numbers
that both look like "throughput" and differ by ~2x.

    aggregate_tok_s   = total generated tokens / wall-clock of the wave
                        <- TRUE throughput. What you actually get.
    sum_of_rates      = mean(per-request tok/decodeMs) * N
                        <- NOT a throughput. decodeMs excludes prefill AND
                           queue wait, so each request is credited with a rate
                           measured only over the window it was decoding.

If N requests exceed the server's seats, the surplus queue; their decodeMs
still looks fast because it never counted the wait. Multiply by N and you
manufacture a number that no wall-clock will ever reproduce. This harness
reports BOTH at every rung so the gap is visible rather than inferred.

Usage:
    python bench.py --ladder 1,2,4,8,16,32,48,64,96,128 --repeats 2
    python bench.py --out results.json
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

# `completion` (not rawCompletion) because it returns the server's own
# prefillMs/decodeMs registers — the only way to separate the two phases
# without guessing from wall-clock.
Q = """query($p:String!,$m:Int!){completion(request:{prompt:$p,maxTokens:$m,temperature:0.7}){
  generatedTokens promptTokens decodeMs prefillMs}}"""

HEALTH = "{ health { poolSize availableInstances inFlight kvPoolTokens decodeMode } }"


def gql(q: str, variables: dict | None = None, timeout: int = 900) -> dict:
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


def one(args) -> dict:
    """One request. Small prompt by design — see METHODS.md 'prompt size'."""
    i, gen = args
    prompt = (
        f"Worker {i}: write a detailed, meandering description of a small "
        f"coastal town's morning market. Prose only."
    )
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
        "tok": c["generatedTokens"],
        "prompt_tok": c.get("promptTokens") or 0,
        "decode_ms": c["decodeMs"],
        "prefill_ms": c["prefillMs"],
        "wall": wall,
    }


def rung(n: int, gen: int, repeats: int) -> dict:
    reps = []
    for _ in range(repeats):
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(one, [(i, gen) for i in range(n)]))
        wall = time.time() - t0

        ok = [r for r in results if "tok" in r]
        errs = [r for r in results if "err" in r]
        tot = sum(r["tok"] for r in ok)
        # per-request DECODE rate from server telemetry (excludes prefill+queue)
        per = [r["tok"] / (r["decode_ms"] / 1000) for r in ok if r.get("decode_ms")]
        walls = sorted(r["wall"] for r in ok)
        mean_per = st.mean(per) if per else 0.0
        reps.append(
            {
                "aggregate_tok_s": round(tot / wall, 2) if wall else 0,
                "per_stream_decode_tok_s": round(mean_per, 2),
                "sum_of_rates": round(
                    mean_per * n, 2
                ),  # THE ARTIFACT, shown on purpose
                "total_tokens": tot,
                "wall_s": round(wall, 1),
                "prefill_ms_mean": (
                    round(st.mean([r["prefill_ms"] for r in ok]), 1) if ok else 0
                ),
                "decode_ms_mean": (
                    round(st.mean([r["decode_ms"] for r in ok]), 1) if ok else 0
                ),
                "latency_p50_s": round(st.median(walls), 1) if walls else 0,
                "latency_p95_s": (
                    round(walls[int(len(walls) * 0.95)], 1) if walls else 0
                ),
                "errors": len(errs),
                "error_samples": [e["err"] for e in errs[:3]],
            }
        )
    # report the BEST rep by aggregate (throughput is the max the box sustains;
    # a slow rep means contention, not a lower ceiling) but keep them all
    best = max(reps, key=lambda r: r["aggregate_tok_s"])
    return {"n": n, "gen": gen, "repeats": reps, **best}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ladder", default="1,2,4,8,16,32,48,64,96,128")
    ap.add_argument("--gen", type=int, default=256, help="generated tokens per request")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--out", default=str(Path(__file__).parent / "results.json"))
    args = ap.parse_args()

    h = gql(HEALTH)["data"]["health"]
    print(f"server: {h}")
    seats = h.get("poolSize") or 0
    ladder = [int(x) for x in args.ladder.split(",")]
    over = [n for n in ladder if n > seats]
    if over:
        print(
            f"\n  !! WARNING: rungs {over} EXCEED the server's {seats} seats.\n"
            f"     Those requests will QUEUE, not run concurrently. aggregate_tok_s\n"
            f"     stays honest; sum_of_rates will inflate — which is exactly the\n"
            f"     artifact under test, so this is informative, not fatal.\n"
        )

    print(
        f"\n{'N':>4} {'aggregate':>10} {'per-stream':>11} {'sum-of-rates':>13} "
        f"{'prefill ms':>11} {'p50 s':>7} {'p95 s':>7} {'err':>4}"
    )
    print("-" * 74)
    rows = []
    for n in ladder:
        r = rung(n, args.gen, args.repeats)
        rows.append(r)
        print(
            f"{n:>4} {r['aggregate_tok_s']:>10.1f} {r['per_stream_decode_tok_s']:>11.2f} "
            f"{r['sum_of_rates']:>13.1f} {r['prefill_ms_mean']:>11.0f} "
            f"{r['latency_p50_s']:>7.1f} {r['latency_p95_s']:>7.1f} {r['errors']:>4}"
        )
        if r["errors"]:
            for e in r["error_samples"]:
                print(f"       ERR: {e}")

    peak = max(rows, key=lambda r: r["aggregate_tok_s"])
    base = rows[0]["aggregate_tok_s"] or 1
    print("-" * 74)
    print(f"PEAK aggregate  : {peak['aggregate_tok_s']} tok/s at N={peak['n']}")
    print(f"batching gain   : {peak['aggregate_tok_s']/base:.2f}x over N=1")
    worst = max(rows, key=lambda r: r["sum_of_rates"])
    print(
        f"sum-of-rates max: {worst['sum_of_rates']} at N={worst['n']}  "
        f"<- the artifact; {worst['sum_of_rates']/max(worst['aggregate_tok_s'],1):.1f}x "
        f"the true throughput at that rung"
    )
    Path(args.out).write_text(
        json.dumps({"server": h, "gen": args.gen, "rows": rows}, indent=2)
    )
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
