#!/usr/bin/env python3
"""Decode scaling: per-instance and aggregate decode throughput vs pool size.

For pool size N: fire N CONCURRENT stateless completions (each ~256 generated
tokens), read the server's own decodeMs/generatedTokens telemetry per request,
compute per-request decode tok/s and aggregate tok/s over the concurrent wall.
Repeats per point for stability. Expectation from past tests: roughly linear
per-instance decrease, upward-inflected aggregate (bandwidth-bound Metal).

Usage: .venv/bin/python dev/decode_scaling_bench.py N [repeats=2]
(driver script bounces the server per pool size; this measures one size)
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import sys
import time
import urllib.request

import os

URL = os.environ.get("BENCH_URL", "http://127.0.0.1:8008/graphql")
Q = """query($p:String!,$m:Int!){completion(request:{prompt:$p,maxTokens:$m,temperature:0.7}){
  text generatedTokens decodeMs prefillMs}}"""


def gql(q, variables=None, timeout=600):
    body = {"query": q}
    if variables:
        body["variables"] = variables
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def one(i: int):
    prompt = (f"Worker {i}: write a detailed, meandering description of a small "
              f"coastal town's morning market. Prose only.")
    t0 = time.time()
    r = gql(Q, {"p": prompt, "m": 256})
    wall = time.time() - t0
    if r.get("errors"):
        return {"err": r["errors"][0]["message"][:120]}
    c = r["data"]["completion"]
    return {"tok": c["generatedTokens"], "decode_ms": c["decodeMs"],
            "prefill_ms": c["prefillMs"], "wall": wall}


def main():
    n = int(sys.argv[1])
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    rows = []
    for rep in range(repeats):
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(one, range(n)))
        wall = time.time() - t0
        errs = [r for r in results if "err" in r]
        ok = [r for r in results if "tok" in r]
        tot_tok = sum(r["tok"] for r in ok)
        per = [r["tok"] / (r["decode_ms"] / 1000) for r in ok if r["decode_ms"]]
        rows.append({
            "n": n, "rep": rep, "errors": len(errs),
            "per_instance_tps": round(sum(per) / len(per), 2) if per else 0,
            "aggregate_tps": round(tot_tok / wall, 2),
            "total_tokens": tot_tok, "concurrent_wall_s": round(wall, 1),
        })
        for e in errs:
            print("ERR:", e["err"], file=sys.stderr)
    for row in rows:
        print(json.dumps(row))


if __name__ == "__main__":
    main()
