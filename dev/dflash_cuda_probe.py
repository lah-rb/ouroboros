#!/usr/bin/env python3
"""dflash speculative decoding on the 3090/3060 — including a cross-device arm.

dflash was measured NET NEGATIVE on the M1 Ultra at every draft length
(dev/DFLASH_SD_2026-08-14.md): -12% at n_max 3, -50% at 8 and 16, with
acceptance collapsing 0.38 -> 0.11. The explanation offered was physics rather
than software: speculative decoding trades bandwidth-bound sequential decode
for compute-bound parallel verify, and the M1 has abundant bandwidth against
relatively little compute — the worst machine for that trade. An RTX was
predicted to go the other way.

This rig can also do something the M1 could not: put the DRAFT on a different
device from the target. The draft is 1.63 GB and the 3060 is otherwise idle, so
the question is whether drafting can be made free the way the swarm bench made
a second tenant free (S = 0.0011 cross-device).

THREE TRAPS, each of which fakes a verdict — all three confirmed live in this
build:

  1. `--spec-type` defaults to `none`. Passing `-md <draft>` alone loads the
     draft, occupies its VRAM, and does NOTHING. On the M1 that produced three
     arms at byte-identical 20.78 tok/s, which reads as "no speedup" rather
     than "feature never engaged".
  2. Readiness is HTTP 200, not a connection. llama-server binds its port
     before loading weights, so a `curl && break` loop fires requests mid-load
     and reports 0.00 tok/s across the board.
  3. An arm with no `n_drafted`/`n_accept` proved nothing, however plausible
     its tok/s looks. Every arm here is checked for engagement and reports
     acceptance, and an arm that cannot show drafting is marked INERT.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request

_PROMPT = ("Write a detailed technical explanation of how speculative decoding "
           "works, covering the draft model, verification, and acceptance.")


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


def run_arm(binary: str, model: str, draft: str | None, draft_dev: str | None,
            n_max: int, port: int, n_predict: int, log_path: str) -> dict:
    cmd = [binary, "-m", model, "--port", str(port), "--host", "127.0.0.1",
           "-ngl", "99", "-c", "4096", "--no-webui"]
    if draft:
        cmd += ["-md", draft, "-ngld", "99",
                "--spec-type", "draft-dflash",
                "--spec-draft-n-max", str(n_max)]
        if draft_dev:
            cmd += ["--spec-draft-device", draft_dev]
    with open(log_path, "w") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
    try:
        if not wait_ready(port):
            return {"error": "server never answered 200"}
        # warm
        _one(port, 16)
        out = _one(port, n_predict)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()

    text = open(log_path, errors="replace").read()
    # THIS BUILD'S WORDING, verified against a real log. The M1 probe looked
    # for `n_drafted=`/`n_accept=` and this llama.cpp prints
    #   draft acceptance = 0.18310 (  117 accepted /   639 generated), mean len =  2.44
    # so the old pattern matched nothing and the engagement guard fired on arms
    # that WERE drafting — declaring a real +38% result inert. The guard was
    # right to exist and wrong in its pattern; a guard keyed to a log string is
    # only as good as the string, so it is now taken from an observed line.
    pairs = re.findall(
        r"draft acceptance = [0-9.]+ \(\s*(\d+) accepted /\s*(\d+) generated\)",
        text)
    accepted = int(pairs[-1][0]) if pairs else 0
    drafted = int(pairs[-1][1]) if pairs else 0
    mean_len = re.findall(r"mean len =\s*([0-9.]+)", text)
    out_extra = {"mean_accepted_run": float(mean_len[-1]) if mean_len else None}
    out["n_drafted"] = drafted
    out["n_accept"] = accepted
    out["acceptance"] = round(accepted / drafted, 3) if drafted else None
    # An arm that cannot show drafting proved nothing about drafting.
    out.update(out_extra)
    out["engaged"] = bool(draft) and drafted > 0
    if draft and not out["engaged"]:
        out["VERDICT"] = "INERT — no n_drafted in the log; this arm measured plain decode"
    return out


def _one(port: int, n_predict: int) -> dict:
    body = json.dumps({"prompt": _PROMPT, "n_predict": n_predict,
                       "temperature": 0.0, "top_k": 1,
                       "cache_prompt": False}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/completion", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as fh:
        d = json.load(fh)
    t = d.get("timings") or {}
    return {"tokens": t.get("predicted_n") or 0,
            "tok_s": round(t.get("predicted_per_second") or 0.0, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary",
                    default="/home/lah-rb/Repos/llama.cpp/build/bin/llama-server")
    ap.add_argument("--model", default="/home/lah-rb/models/muse-glimmer-30B-kquant-dynamic.gguf")
    ap.add_argument("--draft", default="/home/lah-rb/models/dflash-kquant.gguf")
    ap.add_argument("--n-predict", type=int, default=200)
    args = ap.parse_args()

    arms = [
        ("baseline (no draft)", None, None, 0),
        ("dflash n_max 3, draft on 3090 (same card)", args.draft, "CUDA0", 3),
        ("dflash n_max 3, draft on 3060 (cross-device)", args.draft, "CUDA1", 3),
        ("dflash n_max 8, draft on 3060", args.draft, "CUDA1", 8),
    ]
    rows = []
    base = None
    for i, (label, draft, dev, n_max) in enumerate(arms):
        r = run_arm(args.binary, args.model, draft, dev, n_max, 8180 + i,
                    args.n_predict, f"/home/lah-rb/tmp/dflash_arm{i}.log")
        r["arm"] = label
        if base is None and r.get("tok_s"):
            base = r["tok_s"]
        if base and r.get("tok_s"):
            r["vs_baseline"] = f"{(r['tok_s'] / base - 1) * 100:+.0f}%"
        rows.append(r)
        print(json.dumps(r), flush=True)
    print("\n" + json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
