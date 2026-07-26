#!/usr/bin/env python3
"""Decode rate vs CONTEXT LENGTH — the missing axis of the capacity model.

The decode ladder measured 271 tok/s aggregate at N=128 and 173 at N=48, but
with ~20-token prompts and 256-token generations, i.e. decoding into an almost
empty KV. The 2026-07-26 corpus regen decoded into 400-1,200 token contexts and
managed only ~120 tok/s aggregate while decoding — a 30% shortfall against the
ladder at the same width.

That gap is the whole reason effective throughput sits near 100 tok/s rather
than anywhere near the ladder's numbers, and it is NOT prefill: prefill was
only 13% of that run's wall clock. So the ladder's figures are a laboratory
best case, and this measures the curve they sit on.

METHOD. Hold N and generation length FIXED; sweep the PROMPT size, which sets
how deep the KV is before decoding starts. Read the server's own `decodeMs`
register so prefill is excluded by construction rather than subtracted after
the fact — the per-request decode rate is then generatedTokens / decodeMs, a
clean measurement of "how fast does this model decode at depth D".

Two widths: N=1 isolates the context effect from concurrency, N=8 shows
whether depth and width compound.

Cells where N x (prompt + gen) exceeds 80% of the pool are SKIPPED LOUDLY —
a silent omission in a results table reads as coverage.

Usage:
  python context_curve.py
  python context_curve.py --sizes 256,4096,32768 --ladder 1,8 --gen 256
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

Q = """query($p:String!,$m:Int!){completion(request:{prompt:$p,maxTokens:$m,temperature:0.7}){
  generatedTokens promptTokens prefillMs decodeMs cachedPrefixTokens}}"""
HEALTH = "{ health { status poolSize kvPoolTokens } }"


# The static prefix is forked to every seat from SEQ_STATIC and shows up as
# cachedPrefixTokens on every request. That is correct behaviour, not cache
# contamination — the prefill-grid guard mis-flagged it on 2026-07-26 by
# comparing it against total prompt tokens. Subtract it instead.
def _fresh(rec: dict) -> int:
    return max(0, rec.get("prompt_tok", 0) - rec.get("cached", 0))


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
    """Unique per request — identical prompts would measure the cache."""
    filler = (
        "The repository state is examined and the next action considered "
        "carefully before proceeding. "
    )
    n = max(1, int(target_tokens * 4 / len(filler)) + 1)
    return f"[ctx {uniq}] " + (filler * n)


def one(args) -> dict:
    idx, size, gen = args
    t0 = time.time()
    try:
        r = gql(Q, {"p": make_prompt(size, f"{idx}-{time.time_ns()}"), "m": gen})
    except Exception as exc:  # noqa: BLE001
        return {"err": f"{type(exc).__name__}: {exc}"[:140]}
    if r.get("errors"):
        return {"err": str(r["errors"][0].get("message", ""))[:140]}
    c = r["data"]["completion"]
    return {
        "gen": c["generatedTokens"],
        "prompt_tok": c.get("promptTokens") or 0,
        "cached": c.get("cachedPrefixTokens") or 0,
        "decode_ms": c["decodeMs"],
        "prefill_ms": c["prefillMs"],
        "wall": time.time() - t0,
    }


def cell(size: int, n: int, gen: int) -> dict:
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        res = list(ex.map(one, [(i, size, gen) for i in range(n)]))
    wall = time.time() - t0
    ok = [r for r in res if "gen" in r]
    errs = [r for r in res if "err" in r]
    if not ok:
        return {
            "size": size,
            "n": n,
            "errors": len(errs),
            "error_samples": [e["err"] for e in errs[:2]],
        }
    per = [r["gen"] / (r["decode_ms"] / 1000) for r in ok if r.get("decode_ms")]
    gen_tot = sum(r["gen"] for r in ok)
    return {
        "size": size,
        "n": n,
        # context DEPTH each stream decoded into, on average
        "context_depth": round(st.mean([r["prompt_tok"] + r["gen"] for r in ok])),
        "fresh_prompt": round(st.mean([_fresh(r) for r in ok])),
        # THE measurement: decode only, prefill excluded by the register
        "decode_tok_s_per_stream": round(st.mean(per), 2) if per else 0,
        "decode_tok_s_aggregate": round(sum(per), 1) if per else 0,
        "prefill_ms_mean": round(st.mean([r["prefill_ms"] for r in ok])),
        "wall_s": round(wall, 1),
        "effective_tok_s": round(gen_tot / wall, 1) if wall else 0,
        "errors": len(errs),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="256,1024,4096,16384,32768,65536")
    ap.add_argument("--ladder", default="1,8")
    ap.add_argument(
        "--gen",
        type=int,
        default=256,
        help="FIXED so every cell runs the same number of decode steps",
    )
    ap.add_argument("--out", default=str(Path(__file__).parent / "context_curve.json"))
    args = ap.parse_args()

    h = gql(HEALTH)["data"]["health"]
    pool = h.get("kvPoolTokens") or 0
    print(f"server: {h}\n")
    sizes = [int(x) for x in args.sizes.split(",")]
    ladder = [int(x) for x in args.ladder.split(",")]

    print(
        f"{'N':>3} {'prompt':>8} {'depth':>8} {'decode/stream':>14} "
        f"{'decode agg':>11} {'effective':>10} {'err':>4}"
    )
    print("-" * 64)
    rows, skipped = [], []
    for n in ladder:
        base = None
        for size in sizes:
            need = n * (size + args.gen)
            if pool and need > 0.8 * pool:
                skipped.append((n, size, need))
                print(
                    f"{n:>3} {size:>8}   SKIPPED — {need:,} tokens > 80% of the "
                    f"{pool:,}-cell pool"
                )
                continue
            r = cell(size, n, args.gen)
            rows.append(r)
            if base is None:
                base = r.get("decode_tok_s_per_stream") or 1
            # SIGN: negative = SLOWER than the shallowest cell. The first
            # version printed 1 - r/base, so a 26% slowdown rendered as
            # "+26%" and read like a speedup.
            delta = 100 * ((r.get("decode_tok_s_per_stream", 0) / base) - 1)
            print(
                f"{n:>3} {size:>8} {r.get('context_depth',0):>8} "
                f"{r.get('decode_tok_s_per_stream',0):>14.2f} "
                f"{r.get('decode_tok_s_aggregate',0):>11.1f} "
                f"{r.get('effective_tok_s',0):>10.1f} {r.get('errors',0):>4}"
                + (f"   ({delta:+.0f}% vs shallowest)" if abs(delta) >= 0.5 else "")
            )
    print("-" * 64)
    if skipped:
        print(f"skipped {len(skipped)} cell(s) — listed above, NOT silently dropped")
    Path(args.out).write_text(
        json.dumps(
            {"server": h, "gen": args.gen, "rows": rows, "skipped": skipped}, indent=2
        )
    )
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
