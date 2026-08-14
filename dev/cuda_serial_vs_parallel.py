#!/usr/bin/env python3
"""Serial vs parallel calls against ONE server: how much does a slot buy?

`cuda_swarm_probe.py` answered "two tenants, one device or two". This answers
the question that matters for a single large model, where a second copy cannot
fit at all: muse-glimmer-30B is 19.65 GB on a 24 GB card, so two PROCESSES are
impossible and the only concurrency available is slots inside one context.

For N calls it measures both orders against the SAME warm server:

    t_serial     N calls back to back
    t_parallel   the same N calls issued at once

and reports serialization on the same definition used for the two-tenant arms,
generalised from 2 legs to N:

    S = (t_parallel - t_one) / (t_serial - t_one)

    S = 0.0  all N finished in the time of one — perfect overlap
    S = 1.0  concurrency bought nothing over doing them in turn

t_one is measured, not derived from t_serial/N, because the first call in a
serial run pays warm-up that the others do not and dividing would smear it
across every level.

WHY SIZE IS EXPECTED TO MATTER. Decode is bandwidth-bound: each step reads the
whole weight set. Batching amortises that single read across every sequence in
the pass, so the LARGER the model, the more a slot should buy — right up until
KV and compute buffers run out of the headroom the weights left behind. Both
halves of that are measurable here.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_PROMPTS = [
    "Explain why memory bandwidth bounds autoregressive decode.",
    "Describe the trade-off between batch size and per-request latency.",
    "Summarise what a KV cache costs as context grows.",
    "What limits the number of sequences one accelerator can serve?",
    "Explain prefill versus decode and why they scale differently.",
    "Describe how quantisation changes the bandwidth picture.",
    "What is the role of the unified buffer in a batched forward pass?",
    "Explain why per-stream throughput falls as concurrency rises.",
]


def wait_ready(port: int, timeout: float = 1200.0) -> bool:
    """HTTP 200, never a bare connection — it binds before it loads."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=3
            ) as fh:
                if fh.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    return False


def call(port: int, prompt: str, n_predict: int) -> int:
    """Returns TOKENS GENERATED, and that return value is load-bearing.

    An earlier version discarded the response and timed the call alone. It
    reported t_one = 0.11 s for a 30B model — 1163 tok/s, impossible — because
    the model was stopping almost immediately and nothing checked. Every
    serialization number computed from that baseline was meaningless: with
    t_one ~ 0, S collapses to t_parallel/t_serial, which is just the inverse
    speedup wearing the name of a different metric.

    A probe that cannot tell "did the work" from "returned instantly" cannot
    be trusted about anything else either.
    """
    body = json.dumps({
        "prompt": prompt, "n_predict": n_predict,
        "temperature": 0.0, "top_k": 1, "cache_prompt": False,
        "ignore_eos": True,   # fixed work per call, whatever the model wants
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/completion", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=1800) as fh:
        d = json.load(fh)
    return int((d.get("timings") or {}).get("predicted_n") or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary",
                    default="/home/lah-rb/Repos/llama.cpp/build/bin/llama-server")
    ap.add_argument("--model", required=True)
    ap.add_argument("--gpu", type=int, default=0)
    ap.add_argument("--levels", default="2,4,8")
    ap.add_argument("--n-predict", type=int, default=128)
    ap.add_argument("--n-ctx-per-slot", type=int, default=1024)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    levels = [int(x) for x in args.levels.split(",")]
    slots = max(levels)
    port = 8160
    # -c is TOTAL across slots; size it per slot or a wider --parallel silently
    # shrinks every slot's window and the sweep measures truncation too.
    n_ctx = args.n_ctx_per_slot * slots
    proc = subprocess.Popen(
        ["env", f"CUDA_VISIBLE_DEVICES={args.gpu}", args.binary,
         "-m", args.model, "--port", str(port), "--host", "127.0.0.1",
         "-ngl", "99", "-c", str(n_ctx), "--parallel", str(slots),
         "--no-webui"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_ready(port):
            print("server never answered 200", file=sys.stderr)
            return 2
        call(port, _PROMPTS[0], 16)  # warm

        t0 = time.time()
        tok_one = call(port, _PROMPTS[0], args.n_predict)
        t_one = time.time() - t0
        if tok_one < args.n_predict:
            print(f"baseline generated {tok_one}/{args.n_predict} tokens — "
                  f"the metric would be built on a call that did not run",
                  file=sys.stderr)
            return 3

        rows = []
        for n in levels:
            prompts = [_PROMPTS[i % len(_PROMPTS)] for i in range(n)]

            t0 = time.time()
            ser_tokens = sum(call(port, p, args.n_predict) for p in prompts)
            t_serial = time.time() - t0

            t0 = time.time()
            with ThreadPoolExecutor(max_workers=n) as pool:
                par_tokens = sum(pool.map(
                    lambda p: call(port, p, args.n_predict), prompts))
            t_parallel = time.time() - t0
            expected = n * args.n_predict
            if ser_tokens != expected or par_tokens != expected:
                print(f"N={n}: token counts {ser_tokens}/{par_tokens} vs "
                      f"{expected} expected — arms did unequal work",
                      file=sys.stderr)

            denom = t_serial - t_one
            s = (t_parallel - t_one) / denom if denom > 0 else float("nan")
            rows.append({
                "n": n,
                "t_one": round(t_one, 2),
                "t_serial": round(t_serial, 2),
                "t_parallel": round(t_parallel, 2),
                "serialization": round(s, 4),
                "speedup": round(t_serial / t_parallel, 2),
                "tokens_serial": ser_tokens,
                "tokens_parallel": par_tokens,
                "serial_tok_s": round(ser_tokens / t_serial, 1),
                "parallel_tok_s": round(par_tokens / t_parallel, 1),
            })
            print(json.dumps(rows[-1]), flush=True)

        print("\n" + json.dumps(
            {"label": args.label, "model": args.model.split("/")[-1],
             "slots": slots, "n_ctx_total": n_ctx, "rows": rows}, indent=1))
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
