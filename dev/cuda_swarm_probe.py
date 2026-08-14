#!/usr/bin/env python3
"""Context-swarm behaviour on CUDA: does a second GPU buy free overlap?

THE QUESTION THIS ANSWERS. On the M1 Ultra, two tenants sharing one device
serialized in a strict order (dev/PARALLEL_LANES_2026-08-13.md §7d):

    paddle OCR x muse text   compute x bandwidth   0.342
    OCR x vision             compute x compute     0.524
    text x text              bandwidth x bandwidth 0.575-0.622

The hypothesis fitted to that ordering was UNIFIED MEMORY BANDWIDTH as the
shared bottleneck — two decoders saturate it and contend; a compute-bound
tenant fills the gaps they leave. No bandwidth counters were read, so it stayed
a hypothesis.

This rig has TWO SEPARATE BANDWIDTH DOMAINS: a 3090 (24 GB GDDR6X) and a 3060
(12 GB). That turns the hypothesis into a prediction that can be falsified —
put the same two decoders on different GPUs and serialization should collapse
toward 0, because there is no longer a shared bus to saturate.

THE CONTROL IS THE POINT. Same model, same prompts, same protocol, same
process shape; the ONLY difference between the two arms is which device each
server sits on. The M1 work learned this the hard way: an earlier verdict
("true parallelism requires cross-process") turned out to be an artifact of
comparing two things at once, and a controlled re-run put the delta at 0.044.

SERIALIZATION, defined exactly as on the M1 so the numbers compare:

    S = (t_concurrent - max(ta, tb)) / (ta + tb - max(ta, tb))

    S = 0.0  the second leg was free — perfect overlap
    S = 1.0  the second leg cost its full solo time — no overlap at all

Legs are balanced by construction (same model, same prompt set) because the
M1 run found unbalanced legs inflate the spread without moving S.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_PROMPTS = [
    "Explain why bandwidth rather than FLOPs usually bounds token generation.",
    "Describe the trade-offs between a large batch and low latency when serving.",
    "Summarise how a KV cache changes the cost profile of autoregressive decode.",
    "What limits how many independent sequences one accelerator can serve well?",
]


def wait_ready(port: int, timeout: float = 900.0) -> bool:
    """HTTP 200 on /health, never a bare connection.

    llama-server binds its port immediately and answers 503 while the weights
    load, so `curl && break` returns 0 mid-load. That trap produced a 0.00
    tok/s reading across every arm of the dflash probe before it was caught.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=3
            ) as fh:
                if fh.status == 200:
                    return True
        except (urllib.error.URLError, OSError, urllib.error.HTTPError):
            pass
        time.sleep(2)
    return False


def start_server(binary: str, model: str, port: int, gpu: int, ngl: int,
                 parallel: int, n_ctx: int) -> subprocess.Popen:
    env_prefix = ["env", f"CUDA_VISIBLE_DEVICES={gpu}"]
    cmd = env_prefix + [
        binary, "-m", model, "--port", str(port), "--host", "127.0.0.1",
        "-ngl", str(ngl), "-c", str(n_ctx), "--parallel", str(parallel),
        "--no-webui",
    ]
    return subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def one_request(port: int, prompt: str, n_predict: int) -> dict:
    body = json.dumps({
        "prompt": prompt,
        "n_predict": n_predict,
        "temperature": 0.0,   # greedy: identical work every run
        "top_k": 1,
        "cache_prompt": False,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/completion", data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as fh:
        data = json.load(fh)
    elapsed = time.time() - t0
    timings = data.get("timings") or {}
    return {
        "seconds": elapsed,
        "tokens": timings.get("predicted_n") or 0,
        "tok_s": timings.get("predicted_per_second") or 0.0,
    }


def run_leg(port: int, n_predict: int, rounds: int) -> dict:
    """One leg: every prompt, `rounds` times, serially on that server."""
    t0 = time.time()
    results = []
    for _ in range(rounds):
        for p in _PROMPTS:
            results.append(one_request(port, p, n_predict))
    wall = time.time() - t0
    return {
        "wall": wall,
        "requests": len(results),
        "tokens": sum(r["tokens"] for r in results),
        "tok_s_mean": statistics.mean([r["tok_s"] for r in results]),
    }


def serialization(ta: float, tb: float, tc: float) -> float:
    slower, total = max(ta, tb), ta + tb
    denom = total - slower
    return (tc - slower) / denom if denom > 0 else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--binary", default="/home/lah-rb/Repos/llama.cpp/build/bin/llama-server")
    ap.add_argument("--model", required=True)
    ap.add_argument("--gpus", default="0,0",
                    help="device for leg A and leg B: '0,0' same GPU, '0,1' cross")
    ap.add_argument("--n-predict", type=int, default=128)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--n-ctx", type=int, default=4096)
    ap.add_argument("--ngl", type=int, default=99)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    gpu_a, gpu_b = [int(g) for g in args.gpus.split(",")]
    procs = []
    try:
        procs.append(start_server(args.binary, args.model, 8101, gpu_a,
                                  args.ngl, 1, args.n_ctx))
        procs.append(start_server(args.binary, args.model, 8102, gpu_b,
                                  args.ngl, 1, args.n_ctx))
        for port in (8101, 8102):
            if not wait_ready(port):
                print(f"server on {port} never answered 200", file=sys.stderr)
                return 2

        # Warm both: the first request pays kernel autotune and allocator
        # growth, which would otherwise land entirely in whichever leg ran
        # first and bias the solo baselines.
        for port in (8101, 8102):
            one_request(port, _PROMPTS[0], 16)

        solo_a = run_leg(8101, args.n_predict, args.rounds)
        solo_b = run_leg(8102, args.n_predict, args.rounds)

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(run_leg, 8101, args.n_predict, args.rounds)
            fb = pool.submit(run_leg, 8102, args.n_predict, args.rounds)
            con_a, con_b = fa.result(), fb.result()
        t_con = time.time() - t0

        s = serialization(solo_a["wall"], solo_b["wall"], t_con)
        out = {
            "label": args.label or f"gpus={args.gpus}",
            "gpus": args.gpus,
            "n_predict": args.n_predict,
            "rounds": args.rounds,
            "solo_a_wall": round(solo_a["wall"], 2),
            "solo_b_wall": round(solo_b["wall"], 2),
            "concurrent_wall": round(t_con, 2),
            "serialization": round(s, 4),
            "solo_a_tok_s": round(solo_a["tok_s_mean"], 2),
            "solo_b_tok_s": round(solo_b["tok_s_mean"], 2),
            "con_a_tok_s": round(con_a["tok_s_mean"], 2),
            "con_b_tok_s": round(con_b["tok_s_mean"], 2),
            "aggregate_speedup": round(
                (solo_a["wall"] + solo_b["wall"]) / t_con, 3),
            # Leg balance is REPORTED, not folded into S: the M1 run found an
            # unbalanced pair moves the spread without moving serialization,
            # and treating them as one number hid that.
            "leg_imbalance": round(
                abs(solo_a["wall"] - solo_b["wall"])
                / max(solo_a["wall"], solo_b["wall"]), 3),
        }
        print(json.dumps(out, indent=1))
        return 0
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
