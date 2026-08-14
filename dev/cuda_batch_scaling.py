#!/usr/bin/env python3
"""Batched decode scaling: ONE context, N sequence slots, on one GPU.

The companion question to cuda_swarm_probe.py. That probe showed two SEPARATE
server processes on one CUDA device serialize completely (S = 1.04, aggregate
speedup 0.98) — CUDA time-slices between processes, so a second process buys
nothing.

This measures the other shape: one process, one context, N slots decoded in a
single batched forward pass. That is what the M1's batched engine did, where it
took 60.6 -> 93.3 tok/s going 1 -> 4 streams (dev/PARALLEL_LANES §7e).

Aggregate throughput is the figure that matters, NOT per-stream: batching
trades a little latency per sequence for more total tokens, and reporting only
per-stream would make a win look like a regression.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_PROMPT = "Explain, in detail, why memory bandwidth bounds autoregressive decode."


def wait_ready(port: int, timeout: float = 900.0) -> bool:
    """HTTP 200, not a bare connection — the server binds before it loads."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=3
            ) as fh:
                if fh.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — any failure means not ready yet
            pass
        time.sleep(2)
    return False


def one(port: int, n_predict: int) -> dict:
    body = json.dumps({
        "prompt": _PROMPT, "n_predict": n_predict,
        "temperature": 0.0, "top_k": 1, "cache_prompt": False,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/completion", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=900) as fh:
        d = json.load(fh)
    t = d.get("timings") or {}
    return {"tokens": t.get("predicted_n") or 0,
            "tok_s": t.get("predicted_per_second") or 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary",
                    default="/home/lah-rb/Repos/llama.cpp/build/bin/llama-server")
    ap.add_argument("--model", required=True)
    ap.add_argument("--gpu", type=int, default=0)
    ap.add_argument("--streams", default="1,2,4,8")
    ap.add_argument("--n-predict", type=int, default=128)
    ap.add_argument("--n-ctx-per-slot", type=int, default=2048)
    ap.add_argument("--ngl", type=int, default=99)
    args = ap.parse_args()

    levels = [int(s) for s in args.streams.split(",")]
    slots = max(levels)
    port = 8140
    # -c IS TOTAL AND DIVIDED ACROSS SLOTS, not per slot. A fixed -c silently
    # shrinks every slot's window as --parallel grows, so a scaling sweep would
    # be measuring truncation as well as concurrency.
    n_ctx = args.n_ctx_per_slot * slots
    proc = subprocess.Popen(
        ["env", f"CUDA_VISIBLE_DEVICES={args.gpu}", args.binary,
         "-m", args.model, "--port", str(port), "--host", "127.0.0.1",
         "-ngl", str(args.ngl), "-c", str(n_ctx),
         "--parallel", str(slots), "--no-webui"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_ready(port):
            print("server never answered 200", file=sys.stderr)
            return 2
        one(port, 16)  # warm: first call pays autotune + allocator growth

        rows = []
        base = None
        for n in levels:
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=n) as pool:
                res = list(pool.map(lambda _: one(port, args.n_predict),
                                    range(n)))
            wall = time.time() - t0
            total_tokens = sum(r["tokens"] for r in res)
            agg = total_tokens / wall
            per = sum(r["tok_s"] for r in res) / len(res)
            base = agg if base is None else base
            rows.append({"streams": n, "wall_s": round(wall, 2),
                         "tokens": total_tokens,
                         "aggregate_tok_s": round(agg, 1),
                         "per_stream_tok_s": round(per, 1),
                         "speedup_vs_1": round(agg / base, 2)})
            print(json.dumps(rows[-1]), flush=True)
        print("\n" + json.dumps({"gpu": args.gpu, "slots": slots,
                                 "n_ctx_total": n_ctx, "rows": rows}, indent=1))
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
