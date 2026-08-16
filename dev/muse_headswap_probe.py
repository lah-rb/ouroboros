#!/usr/bin/env python3
"""Verify the muse reasoning HEAD-SWAP moves token counts (2026-08-16 port).

Replays the 2026-08-11 measurement shape that established per-request levels
were a SILENT NO-OP on this family (non-monotonic, thought fraction 88-94%
regardless): two structured prompts x levels x samples, stateless completions
through the GraphQL `reasoning` field. If the port works, levels must now be
MONOTONIC-ish in generated tokens and the reasoning fraction must move.

Run with the muse server already up:
    .venv/bin/python dev/muse_headswap_probe.py
"""

import json
import sys
import time
import urllib.request

ENDPOINT = "http://localhost:8008/graphql"

# The 2026-08-11 shapes, verbatim in spirit: strict-JSON extraction and a
# one-word screening decision.
PROMPTS = {
    "json": (
        "Extract the fields from this record into a JSON object with keys "
        "title, year, pages. Return ONLY the fenced JSON.\n\n"
        "Record: 'Tidal Dynamics of Estuarine Systems, published 2019, "
        "spanning pages 214-241 of the collected proceedings.'"
    ),
    "decision": (
        "Answer with exactly one word, yes or no: is the following paper "
        "about oceanography?\n\nTitle: 'Spectral Analysis of Deep-Water "
        "Wave Propagation in the North Atlantic'"
    ),
}

LEVELS = [None, "low", "medium", "high", "xhigh"]  # None = default (low bake)
SAMPLES = 2

QUERY = """
mutation Probe($req: CompletionRequest!) {
  createCompletion(request: $req) {
    text
    tokensGenerated
    generatedTokens
    reasoningTokens
  }
}
"""


def call(prompt: str, level):
    req = {"prompt": prompt, "maxTokens": 1200, "temperature": 0.2}
    if level is not None:
        req["reasoning"] = level
    body = json.dumps({"query": QUERY, "variables": {"req": req}}).encode()
    r = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(r, timeout=300) as resp:
        out = json.loads(resp.read())
    if out.get("errors"):
        raise RuntimeError(out["errors"][0].get("message", "graphql error"))
    return out["data"]["createCompletion"]


def main() -> int:
    print(f"{'shape':9} {'level':8} {'gen':>5} {'reason':>6} {'frac':>5}  text[:40]")
    rows = []
    for shape, prompt in PROMPTS.items():
        for level in LEVELS:
            for s in range(SAMPLES):
                t0 = time.time()
                try:
                    res = call(prompt, level)
                except Exception as e:  # noqa: BLE001
                    print(f"{shape:9} {str(level):8} ERROR: {e}")
                    continue
                gen = res.get("generatedTokens") or res.get("tokensGenerated") or 0
                rt = res.get("reasoningTokens") or 0
                frac = rt / gen if gen else 0.0
                text = (res.get("text") or "").replace("\n", " ")[:40]
                print(
                    f"{shape:9} {str(level):8} {gen:5d} {rt:6d} {frac:4.0%}"
                    f"  {text}   ({time.time()-t0:.1f}s)"
                )
                rows.append((shape, str(level), gen, rt))
    # Monotonicity read-out: mean gen per level per shape
    print("\nmeans (gen tokens):")
    for shape in PROMPTS:
        line = f"  {shape:9}"
        for level in LEVELS:
            vals = [g for sh, lv, g, _ in rows if sh == shape and lv == str(level)]
            line += f"  {str(level)}={sum(vals)//max(len(vals),1):>4}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
