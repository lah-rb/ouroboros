#!/usr/bin/env python3
"""JIT lifecycle exercise at FULL 131k context (pool mode, limit 3).

Companion to the batched 131k capacity sweep: measures what "more
instances" costs when an instance is a CONTEXT (~9.2G swa_full KV each at
131k) instead of a batched seat (a seq id, ~free).

Phases (server must already be up on gpt-oss-120b-a5-jit-131k):
  A. baseline    — single completion on the primary; wired sample.
  B. scale-up    — 3 concurrent completions force the JIT batch spawn to
                   the limit; wired sample + per-request outcome (NOTE:
                   concurrent decode across pool contexts is the known
                   Metal dead end — errors here are expected DATA).
  C. alternating — 3 sequential completions (the pool's safe shape) to
                   show the spawned instances serve correctly one at a time.
  D. reap        — idle past instance_idle_ttl (60s); poll health until
                   the scaler reaps back toward 1; wired decline sampled.

Prints a JSON report. Exit 0 always (observational)."""

from __future__ import annotations

import concurrent.futures as cf
import json
import subprocess
import time
import urllib.request

URL = "http://127.0.0.1:8008/graphql"
Q = """query($p:String!,$m:Int!){completion(request:{prompt:$p,maxTokens:$m,temperature:0.7}){
  generatedTokens decodeMs}}"""


def gql(q, variables=None, timeout=600):
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


def wired_gb() -> float:
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "wired" in line:
            return round(int(line.split()[-1].rstrip(".")) * 16384 / 1e9, 1)
    return 0.0


def health() -> dict:
    return gql("query{health{availableInstances activeInstances inFlight}}")["data"][
        "health"
    ]


def one(i: int) -> dict:
    prompt = f"Worker {i}: describe a lighthouse at dusk in flowing prose."
    t0 = time.time()
    try:
        r = gql(Q, {"p": prompt, "m": 128})
        if r.get("errors"):
            return {"i": i, "err": r["errors"][0]["message"][:140]}
        c = r["data"]["completion"]
        return {
            "i": i,
            "tok": c["generatedTokens"],
            "decode_ms": c["decodeMs"],
            "wall_s": round(time.time() - t0, 1),
        }
    except Exception as e:  # noqa: BLE001 — observational harness
        return {"i": i, "err": str(e)[:140]}


def main() -> None:
    report: dict = {}

    report["A_baseline"] = {
        "result": one(0),
        "wired_gb": wired_gb(),
        "health": health(),
    }

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(one, range(3)))
    report["B_concurrent_scaleup"] = {
        "results": results,
        "wall_s": round(time.time() - t0, 1),
        "wired_gb": wired_gb(),
        "health": health(),
    }

    report["C_alternating"] = {
        "results": [one(i) for i in range(3)],
        "wired_gb": wired_gb(),
        "health": health(),
    }

    reap_samples = []
    for _ in range(24):  # up to 4 min: ttl 60s + cooldown 30s + tick cadence
        time.sleep(10)
        h = health()
        reap_samples.append(
            {
                "t_s": len(reap_samples) * 10 + 10,
                "active": h["activeInstances"],
                "available": h["availableInstances"],
                "wired_gb": wired_gb(),
            }
        )
        if h["activeInstances"] <= 1:
            break
    report["D_reap"] = reap_samples

    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
