#!/usr/bin/env python3
"""3-process swarm bench: 8 concurrent completions against EACH of three
llmvp processes (ports 8008/8018/8028, batched W=8, mmap-shared weights) —
24 streams total across 3 separate 131k-capable contexts.

The comparison target is single-process batched W=24 on ONE context
(dev/decode_scaling_131k.csv): if decode is weight-read/bandwidth limited,
splitting the 24-stream batch into 3x8 forfeits amortization (each
process's llama_decode reads the weights separately) and lands BELOW the
single-process number.

Usage: .venv/bin/python dev/swarm_3proc_bench.py [repeats=2]
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import subprocess
import sys
import time
import urllib.request

PORTS = (8008, 8018, 8028)
PER_PORT = 8
Q = """query($p:String!,$m:Int!){completion(request:{prompt:$p,maxTokens:$m,temperature:0.7}){
  generatedTokens decodeMs prefillMs}}"""


def gql(port: int, q: str, variables=None, timeout=600):
    body = {"query": q}
    if variables:
        body["variables"] = variables
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/graphql",
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


def one(port: int, i: int):
    prompt = (f"Worker {port}-{i}: write a detailed, meandering description "
              f"of a small coastal town's morning market. Prose only.")
    try:
        r = gql(port, Q, {"p": prompt, "m": 256})
        if r.get("errors"):
            return {"port": port, "i": i, "err": r["errors"][0]["message"][:120]}
        c = r["data"]["completion"]
        return {"port": port, "i": i, "tok": c["generatedTokens"],
                "decode_ms": c["decodeMs"]}
    except Exception as e:  # noqa: BLE001 — observational harness
        return {"port": port, "i": i, "err": str(e)[:120]}


def main():
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    for rep in range(repeats):
        jobs = [(p, i) for p in PORTS for i in range(PER_PORT)]
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            results = list(ex.map(lambda a: one(*a), jobs))
        wall = time.time() - t0
        ok = [r for r in results if "tok" in r]
        errs = [r for r in results if "err" in r]
        tot = sum(r["tok"] for r in ok)
        per = [r["tok"] / (r["decode_ms"] / 1000) for r in ok if r["decode_ms"]]
        per_port = {
            p: round(sum(r["tok"] for r in ok if r["port"] == p) / wall, 1)
            for p in PORTS
        }
        print(json.dumps({
            "rep": rep, "streams": len(jobs), "errors": len(errs),
            "per_stream_tps": round(sum(per) / len(per), 2) if per else 0,
            "aggregate_tps": round(tot / wall, 2),
            "per_port_agg": per_port,
            "total_tokens": tot, "wall_s": round(wall, 1),
            "wired_gb": wired_gb(),
        }))
        for e in errs[:6]:
            print("ERR:", e, file=sys.stderr)


if __name__ == "__main__":
    main()
