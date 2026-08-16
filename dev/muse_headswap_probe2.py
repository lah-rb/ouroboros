#!/usr/bin/env python3
"""Head-swap probe #2 — HARD prompts, uncapped budget, card sampling.

Probe #1 (dev/muse_headswap_probe.py) showed the dial moving for the first
time on muse but with a compressed spread (json: low 120 -> high 159), and
the one-word decision flat at every level. Two confounds could compress it:
maxTokens 1200 (a deliberation cap) and temperature 0.2 (off the card's 1.0
profile). This probe removes both and uses prompts where reasoning depth has
somewhere to go. Verification of the SWAP itself is external: count
"reasoning head-swap" lines in the server log against non-default requests.

Run with the muse server up:
    .venv/bin/python dev/muse_headswap_probe2.py
"""

import json
import sys
import time
import urllib.request

ENDPOINT = "http://localhost:8008/graphql"

PROMPTS = {
    # Multi-step math with a definite answer (correct: 204).
    "math": (
        "A staircase has 12 steps. You can climb 1, 2, or 3 steps at a time, "
        "but you may never take two 3-step climbs in a row. How many distinct "
        "ways are there to climb the staircase? Work it out and give the "
        "final number."
    ),
    # Algorithmic design with edge-case traps — the code-gen shape.
    "algo": (
        "Write a Python function merge_windows(events) that takes a list of "
        "(start, end, weight) tuples and returns the minimal list of "
        "non-overlapping (start, end, total_weight) windows, where windows "
        "that touch or overlap are merged and their weights summed. "
        "Half-open intervals [start, end). Handle: empty input, zero-length "
        "events, exact-touch boundaries, and unsorted input. Return ONLY the "
        "function in a fenced block."
    ),
    # Constraint puzzle — planning shape.
    "plan": (
        "Four services A, B, C, D must be deployed one at a time. B needs A "
        "up first. D must not be deployed immediately after B. C must be "
        "deployed before D. A cannot go last. List EVERY valid deployment "
        "order, then state the count."
    ),
}

LEVELS = ["low", "medium", "high", "xhigh"]
SAMPLES = 2

# The `completion` QUERY is the agent's production path (effects/inference.py
# COMPLETION_QUERY). Probe it, not the createCompletion mutation — the
# mutation silently dropped `reasoning` until 2026-08-16 and both earlier
# probe rounds measured the default head through it.
QUERY = """
query Probe($req: CompletionRequest!) {
  completion(request: $req) {
    text
    generatedTokens
    reasoningTokens
  }
}
"""


def call(prompt: str, level: str):
    req = {
        "prompt": prompt,
        "maxTokens": 8192,
        "temperature": 1.0,
        "reasoning": level,
    }
    body = json.dumps({"query": QUERY, "variables": {"req": req}}).encode()
    r = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(r, timeout=900) as resp:
        out = json.loads(resp.read())
    if out.get("errors"):
        raise RuntimeError(out["errors"][0].get("message", "graphql error"))
    return out["data"]["completion"]


def main() -> int:
    rows = []
    print(f"{'shape':6} {'level':7} {'gen':>6} {'reason':>6} {'frac':>5} {'s':>6}  answer[:48]")
    for shape, prompt in PROMPTS.items():
        for level in LEVELS:
            for _ in range(SAMPLES):
                t0 = time.time()
                try:
                    res = call(prompt, level)
                except Exception as e:  # noqa: BLE001
                    print(f"{shape:6} {level:7} ERROR: {e}")
                    continue
                gen = res.get("generatedTokens") or 0
                rt = res.get("reasoningTokens") or 0
                txt = (res.get("text") or "").strip().replace("\n", " ")
                print(
                    f"{shape:6} {level:7} {gen:6d} {rt:6d} {rt/max(gen,1):4.0%} "
                    f"{time.time()-t0:5.0f}s  {txt[:48]}"
                )
                rows.append((shape, level, gen, rt))
    print("\nmeans (gen | reasoning):")
    for shape in PROMPTS:
        line = f"  {shape:6}"
        for level in LEVELS:
            g = [x[2] for x in rows if x[0] == shape and x[1] == level]
            r = [x[3] for x in rows if x[0] == shape and x[1] == level]
            if g:
                line += f"  {level}={sum(g)//len(g)}|{sum(r)//len(r)}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
