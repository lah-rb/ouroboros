#!/usr/bin/env python3
"""Does speculative decoding still pay once the batch is full?

THE DECISION THIS SERVES. dflash is +38% single-seat on this rig
(dev/DFLASH_CUDA_2026-08-14.md) and batching is worth up to 7.28x aggregate
(dev/CUDA_SWARM_2026-08-14.md). If those two wins stack, drafting is worth
wiring into the serving path. If batching already captures the same win, the
wiring buys nothing for a swarm workload and the draft's VRAM and its claim on
the 3060 are better spent elsewhere.

THE PREDICTION, stated before measuring. Both mechanisms exploit the SAME
slack. Decode is bandwidth-bound: a forward pass reads every weight to produce
one token and leaves the compute units idle. Speculation fills that idle
compute with several drafted tokens for ONE seat; batching fills it with one
token from EACH of many seats. They are two ways to spend the same surplus, so
the gain from drafting should SHRINK as seats rise, and may turn negative once
drafting competes for compute the batch would otherwise use.

That predicts the opposite of the intuition that drafting gets stronger per
seat. It is stated here so the measurement can refute it.

Grid: {no draft, dflash} x {1, 2, 4, 8 seats}, aggregate tokens/second.
Aggregate is the figure that decides this — per-seat throughput necessarily
falls with concurrency under both arms and would make a win look like a loss.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_PROMPTS = [
    "Explain how a KV cache changes the cost of autoregressive decode.",
    "Describe why batching raises aggregate throughput but lowers per-seat.",
    "Summarise the trade-off speculative decoding makes.",
    "What bounds the number of sequences an accelerator can serve well?",
    "Explain prefill versus decode and why they scale differently.",
    "Describe how quantisation changes the bandwidth picture.",
    "Explain why acceptance rate governs speculative decoding's payoff.",
    "What makes a draft model a good match for its target?",
]


def wait_ready(port: int, timeout: float = 900.0) -> bool:
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


def _one(port: int, prompt: str, n_predict: int) -> int:
    body = json.dumps({"prompt": prompt, "n_predict": n_predict,
                       "temperature": 0.0, "top_k": 1, "cache_prompt": False,
                       "ignore_eos": True}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/completion", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as fh:
        d = json.load(fh)
    return int((d.get("timings") or {}).get("predicted_n") or 0)


def acceptance_from(log_path: str) -> tuple:
    """(acceptance, accepted, generated) from THIS build's wording.

    Verified against a real log; the M1's `n_drafted=`/`n_accept=` pattern
    matches nothing here and previously caused a working +38% arm to be
    reported INERT.
    """
    text = open(log_path, errors="replace").read()
    pairs = re.findall(
        r"draft acceptance = [0-9.]+ \(\s*(\d+) accepted /\s*(\d+) generated\)",
        text)
    if not pairs:
        return (None, 0, 0)
    acc, gen = int(pairs[-1][0]), int(pairs[-1][1])
    return (round(acc / gen, 3) if gen else None, acc, gen)


def run_cell(binary, model, draft, seats, n_predict, port, log_path) -> dict:
    cmd = [binary, "-m", model, "--port", str(port), "--host", "127.0.0.1",
           "-ngl", "99", "-c", str(1024 * max(seats, 1)),
           "--parallel", str(seats), "--no-webui"]
    if draft:
        cmd += ["-md", draft, "-ngld", "99", "--spec-type", "draft-dflash",
                "--spec-draft-n-max", "8",
                # The draft MUST sit on the other card: same-card placement
                # fails during llama.cpp's memory fitting with "dflash
                # requires ctx_other to be set".
                "--spec-draft-device", "CUDA1"]
    with open(log_path, "w") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
    try:
        if not wait_ready(port):
            return {"error": "server never answered 200"}
        _one(port, _PROMPTS[0], 16)  # warm
        prompts = [_PROMPTS[i % len(_PROMPTS)] for i in range(seats)]
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=seats) as pool:
            toks = sum(pool.map(lambda p: _one(port, p, n_predict), prompts))
        wall = time.time() - t0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()

    acc, accepted, generated = acceptance_from(log_path)
    out = {"seats": seats, "draft": bool(draft), "wall_s": round(wall, 2),
           "tokens": toks, "aggregate_tok_s": round(toks / wall, 1),
           "acceptance": acc, "drafted": generated}
    if draft and generated == 0:
        out["VERDICT"] = "INERT — no drafting statistics; this cell measured plain decode"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary",
                    default="/home/lah-rb/Repos/llama.cpp/build/bin/llama-server")
    ap.add_argument("--model",
                    default="/home/lah-rb/models/muse-glimmer-30B-kquant-dynamic.gguf")
    ap.add_argument("--draft", default="/home/lah-rb/models/dflash-kquant.gguf")
    ap.add_argument("--seats", default="1,2,4,8")
    ap.add_argument("--n-predict", type=int, default=128)
    args = ap.parse_args()

    rows = []
    port = 8190
    for seats in [int(s) for s in args.seats.split(",")]:
        for draft in (None, args.draft):
            port += 1
            r = run_cell(args.binary, args.model, draft, seats,
                         args.n_predict, port,
                         f"/home/lah-rb/tmp/sxd_{seats}_{bool(draft)}.log")
            rows.append(r)
            print(json.dumps(r), flush=True)
    print("\n" + json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
