#!/usr/bin/env python3
"""Context×decode stress probe — can the pool exceed the trained window?

The question (Luke, 2026-07-21): is gpt-oss's 131k trained context a bound
on the POOL (sum across streams) or per-SEQUENCE? Positions are per-seq, so
N×50k=200k of live cells should work on a 224k pool with full per-stream
competency. This measures it: for each (N workers × per-stream depth) point,
N concurrent streams each carry a distinct ~depth-token document with TWO
planted facts (a needle passphrase + two numbers to ADD — retrieval AND
comprehension at depth), then answer both. Deterministic scoring.

Matrix (est actual tokens; filler calibrated ~1.49x char/4):
  1x50k (competency baseline)   2x50k   4x50k (the 200k anchor)
  4x25k   6x25k (150k)
Reports per stream: needle hit, sum correct, decode tok/s, prefill s;
per point: aggregate rate, wall, wired peak (vm_stat sample).

Usage: .venv/bin/python ctx_decode_probe.py > ctxprobe_results.jsonl
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import random
import zlib
import subprocess
import time
import urllib.request

URL = "http://127.0.0.1:8008/graphql"
Q = """query($p:String!,$m:Int!){completion(request:{prompt:$p,maxTokens:$m,temperature:0.2}){
  text generatedTokens decodeMs prefillMs}}"""


def gql(variables, timeout=3000):
    body = {"query": Q, "variables": variables}
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wired_gb() -> float:
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "wired" in line:
                pages = int(line.split()[-1].rstrip("."))
                return round(pages * 16384 / 2**30, 1)
    except Exception:
        pass
    return -1.0


def build_doc(depth_tok: int, seed: int) -> tuple[str, str, int]:
    """~depth_tok tokens of varied filler with a needle at ~40% and two
    numbers (to add) at ~20% and ~70%. Returns (doc, needle, expected_sum)."""
    rng = random.Random(seed)
    needle = f"ZEPHYR-{rng.randint(10000,99999)}-KITE"
    a, b = rng.randint(100, 899), rng.randint(100, 899)
    topics = [
        "harbor tides",
        "alpine soil",
        "printing presses",
        "coral reefs",
        "steam engines",
        "glass blowing",
        "migratory birds",
        "canal locks",
    ]
    para = (
        "In the study of {t}, observers in region {r} recorded measurement "
        "series {s} across {n} field seasons, noting variance bands and "
        "seasonal drift that complicated the calibration of instruments. "
    )
    # ~40 tokens/para estimated at 1.49x char/4 → chars/para ≈ 40*4/1.49*1.49... build by char budget
    char_budget = int(depth_tok * 4 / 1.49) * 1  # calibrated: est_tok = chars/4*1.49
    parts, i = [], 0
    while sum(len(p) for p in parts) < char_budget:
        parts.append(
            para.format(
                t=rng.choice(topics),
                r=rng.randint(1, 99),
                s=rng.randint(1000, 9999),
                n=rng.randint(2, 30),
            )
        )
        i += 1
    n = len(parts)
    parts.insert(int(n * 0.2), f"CALIBRATION CONSTANT ALPHA: {a}. ")
    parts.insert(
        int(n * 0.4), f"The recovery passphrase for this archive is {needle}. "
    )
    parts.insert(int(n * 0.7), f"CALIBRATION CONSTANT BETA: {b}. ")
    doc = "".join(parts)
    prompt = (
        f"{doc}\n\n---\nAnswer BOTH questions about the document above, "
        "exactly in this format and nothing else:\n"
        "PASSPHRASE: <the recovery passphrase>\n"
        "SUM: <ALPHA + BETA as an integer>\n"
    )
    return prompt, needle, a + b


def one(point, i, depth_tok):
    seed = (zlib.crc32(str(point).encode()) % 10**6) * 100 + i
    prompt, needle, want = build_doc(depth_tok, seed=seed)
    t0 = time.time()
    row = {
        "point": point,
        "stream": i,
        "depth_tok_est": len(prompt) // 4,
        "depth_tok_cal": int(len(prompt) / 4 * 1.49),
    }
    try:
        r = gql({"p": prompt, "m": 256})
        row["wall_s"] = round(time.time() - t0, 1)
        if r.get("errors"):
            row["err"] = r["errors"][0]["message"][:160]
            return row
        c = r["data"]["completion"]
        text = c["text"] or ""
        row.update(
            {
                "generated": c["generatedTokens"],
                "decode_tps": (
                    round(c["generatedTokens"] / (c["decodeMs"] / 1000), 1)
                    if c["decodeMs"]
                    else None
                ),
                "prefill_s": round((c["prefillMs"] or 0) / 1000, 1),
                "needle_hit": needle in text,
                "sum_ok": str(want) in text,
                "tail": text[-90:].replace("\n", " "),
            }
        )
    except Exception as e:
        row["wall_s"] = round(time.time() - t0, 1)
        row["err"] = f"{type(e).__name__}: {str(e)[:140]}"
    return row


def burst(label, n, depth_tok):
    print(
        json.dumps(
            {
                "point": label,
                "START": True,
                "n": n,
                "depth_tok": depth_tok,
                "wired_gb": wired_gb(),
            }
        ),
        flush=True,
    )
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        rows = list(ex.map(lambda i: one(label, i, depth_tok), range(n)))
    wall = time.time() - t0
    for r in rows:
        print(json.dumps(r), flush=True)
    ok = [r for r in rows if "generated" in r]
    print(
        json.dumps(
            {
                "point": label,
                "SUMMARY": True,
                "n": n,
                "depth_tok": depth_tok,
                "ok": len(ok),
                "errors": len(rows) - len(ok),
                "needle_acc": sum(1 for r in ok if r.get("needle_hit")),
                "sum_acc": sum(1 for r in ok if r.get("sum_ok")),
                "agg_tps": (
                    round(sum(r["generated"] for r in ok) / wall, 1) if ok else 0
                ),
                "burst_wall_s": round(wall, 1),
                "wired_gb_peak": wired_gb(),
            }
        ),
        flush=True,
    )


def main():
    # ordered smallest→largest total so an edge wedge hits LAST
    for label, n, depth in [
        ("1x50k", 1, 50000),
        ("4x25k", 4, 25000),
        ("2x50k", 2, 50000),
        ("6x25k", 6, 25000),
        ("4x50k", 4, 50000),  # the 200k anchor — past the trained window in SUM
    ]:
        burst(label, n, depth)


if __name__ == "__main__":
    main()
