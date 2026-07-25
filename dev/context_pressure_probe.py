#!/usr/bin/env python3
"""Context pressure probe — measure what a serving config ACTUALLY costs
as its KV pool fills (stdlib only; run against the live server).

Lazy KV wiring means boot-time wired says little about loaded cost (gemma
booted at 48GB against a 74GB theoretical total, 2026-07-25). This probe
fills the context in stages with real prefill traffic, samples wired
memory throughout, and reports the measured marginal bytes/token — the
number that extrapolates to a trustworthy n_ctx ceiling.

Per stage: build a filler prompt targeting a fraction of the pool
(chars-per-token self-calibrates off the first response's promptTokens),
send one rawCompletion (256 gen tokens), sample `vm_stat` wired every 2s
in a thread, record peak/settled wired + server-reported token registers +
any errors. SAFETY: wired above --kill-gb (default 105) kills the server
and aborts the run.

Usage: python dev/context_pressure_probe.py [--kill-gb 105] [--gen 256]
(targets are derived from the server's own kvPoolTokens/modelMaxContext)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
import urllib.request

URL = "http://localhost:8008/graphql"


def gql(query: str, variables: dict | None = None, timeout: float = 1800):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wired_gb() -> float:
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "wired" in line:
            pages = int(line.split()[-1].rstrip("."))
            return pages * 16384 / 1e9
    return 0.0


class WiredSampler(threading.Thread):
    def __init__(self, kill_gb: float):
        super().__init__(daemon=True)
        self.kill_gb = kill_gb
        self.peak = 0.0
        self.tripped = False
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            w = wired_gb()
            self.peak = max(self.peak, w)
            if w > self.kill_gb:
                self.tripped = True
                subprocess.run(["pkill", "-KILL", "-f", "api/main.py"])
                print(f"!! KILL CEILING: wired={w:.1f}GB > {self.kill_gb}GB "
                      f"— server killed", flush=True)
                return
            time.sleep(2)

    def stop(self) -> float:
        self._halt.set()
        self.join(timeout=5)
        return self.peak


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kill-gb", type=float, default=105.0)
    ap.add_argument("--gen", type=int, default=256)
    args = ap.parse_args()

    health = gql("{ health { status kvPoolTokens modelMaxContext decodeMode } }")[
        "data"
    ]["health"]
    pool = int(health.get("kvPoolTokens") or 0)
    stream_lim = int(health.get("modelMaxContext") or pool) or pool
    per_stream = min(pool, stream_lim)
    print(f"server: {health['status']} pool={pool} streamLimit={stream_lim} "
          f"mode={health['decodeMode']}")
    base = wired_gb()
    print(f"baseline wired: {base:.1f}GB")

    # Self-calibrate chars/token on a small request.
    line = "Line %d: the quick brown fox jumps over the lazy dog and returns. \n"
    cal_prompt = "".join(line % i for i in range(80))
    r = gql(
        "query($r: CompletionRequest!){ completion(request:$r){ promptTokens } }",
        {"r": {"prompt": cal_prompt, "maxTokens": 8, "temperature": 0.3}},
    )
    if not r.get("data") or not r["data"].get("completion"):
        raise SystemExit(f"calibration request failed: {r.get('errors')}")
    cal_tokens = int(r["data"]["completion"]["promptTokens"])
    chars_per_tok = len(cal_prompt) / max(cal_tokens - 2000, 1)  # minus static
    if chars_per_tok <= 0 or chars_per_tok > 20:
        chars_per_tok = 3.2  # calibration swamped by static prefix — default
    print(f"calibration: {cal_tokens} prompt tokens → {chars_per_tok:.2f} chars/tok")

    # Fill ladder: leave room for static prefix + generation + 5% margin.
    budget = per_stream - 2600 - args.gen - per_stream // 20
    rows = []
    for frac in (0.25, 0.5, 0.75, 1.0):
        target = int(budget * frac)
        n_chars = int(target * chars_per_tok)
        n_lines = max(n_chars // len(line % 0), 1)
        prompt = "".join(line % i for i in range(n_lines))
        sampler = WiredSampler(args.kill_gb)
        sampler.start()
        t0 = time.time()
        err = ""
        ptok = gen = 0
        try:
            r = gql(
                "query($r: CompletionRequest!){ completion(request:$r){ "
                "promptTokens generatedTokens } }",
                {"r": {"prompt": prompt, "maxTokens": args.gen,
                       "temperature": 0.3}},
            )
            if "errors" in r:
                err = str(r["errors"])[:160]
            else:
                d = r["data"]["completion"]
                ptok = int(d.get("promptTokens") or 0)
                gen = int(d.get("generatedTokens") or 0)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:160]
        peak = sampler.stop()
        settled = wired_gb()
        dt = time.time() - t0
        rows.append((ptok, peak))
        print(
            f"fill {frac:4.0%}: target≈{target} actual={ptok} tok, gen={gen}, "
            f"{dt:.0f}s, peak wired={peak:.1f}GB settled={settled:.1f}GB"
            + (f"  ERROR: {err}" if err else ""),
            flush=True,
        )
        if sampler.tripped:
            print("ABORT: kill ceiling tripped")
            return

    # Marginal cost from first→last stage (both include static+weights).
    (t0_, w0), (t1, w1) = rows[0], rows[-1]
    if t1 > t0_:
        mb_per_tok = (w1 - w0) * 1e3 / (t1 - t0_)
        print(f"\nmeasured marginal cost ≈ {mb_per_tok:.2f} MB/token "
              f"(Δwired {w1 - w0:.1f}GB over Δ{t1 - t0_} tokens)")
        for ceiling in (100.0, 110.0):
            if mb_per_tok > 0:
                max_tok = int((ceiling - w0) * 1e3 / mb_per_tok) + t0_
                print(f"  extrapolated max fill at {ceiling:.0f}GB wired ≈ "
                      f"{max_tok:,} tokens")
    print("done")


if __name__ == "__main__":
    main()
