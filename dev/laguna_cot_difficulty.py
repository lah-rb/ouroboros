#!/usr/bin/env python3
"""Does laguna scale its CoT with difficulty, or always deliberate to the cap?

THE QUESTION. In thinking mode, 6/6 samples on the batch-structural prompt ran
to 20,000 tokens and produced ZERO files. The CoT is not degenerate — distinct
200-char windows 384/384 = 1.000, one sentence repeated more than twice. It is
genuine exploration that never converges: 109 "Let me", 40 "Actually", 30 "Let
me think" in 77k chars.

Two readings, and they imply opposite fixes:

  (a) laguna always deliberates to whatever budget it is given. Then thinking
      mode is unusable for us at any size, and the lever is reasoning_budget.
  (b) laguna scales CoT with difficulty, and the batch prompt — eleven complete
      files in one turn — is simply beyond what it will commit to. Then thinking
      mode is fine for ordinary turns and the batch turn needs decomposing.

A trivial prompt discriminates: if "capital of France" also runs to 20k, it is
(a). If it answers in a few hundred tokens, it is (b) and the ceiling is a
property of the ASK, not the model.

Run with a laguna thinking config active:
    uv run python dev/laguna_cot_difficulty.py [--max-tokens N] [--samples N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request

ENDPOINT = "http://localhost:8008/graphql"

# Ascending difficulty. The point is the SHAPE of the curve, so the rungs are
# deliberately far apart rather than finely graded.
LADDER = [
    ("trivial", "What is the capital of France?"),
    ("easy", "Write a Python function that reverses a string. Return only the code."),
    (
        "moderate",
        "Write a Python class `Stack` with push, pop, peek and is_empty, "
        "raising IndexError on empty pop/peek. Return only the code.",
    ),
    (
        "hard",
        "Write a complete Python module `parser.py` for a text adventure: parse "
        "input into a Command dataclass with verb/noun/modifier, handling "
        "movement (go north/south/east/west and bare directions), inventory "
        "(take, drop, use, examine), talk to <npc>, combat (attack, flee), and "
        "look/status/help/quit. Include synonyms and graceful unknown-verb "
        "handling. Return only the code.",
    ),
    (
        "very hard",
        "Build a complete text adventure game in Python as SIX files "
        "(models.py, parser.py, loader.py, combat.py, engine.py, main.py) plus "
        "YAML world data. Every file complete and consistent with the others. "
        "Emit each as a fenced block headed `# === FILE: <path> ===`.",
    ),
]


def complete(prompt: str, max_tokens: int) -> dict:
    q = """
    query($p: String!, $m: Int!) {
      completion(request: {prompt: $p, maxTokens: $m}) {
        text tokensGenerated truncated
      }
    }
    """
    body = json.dumps({"query": q, "variables": {"p": prompt, "m": max_tokens}}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=3600) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tokens", type=int, default=20000)
    ap.add_argument("--samples", type=int, default=2)
    args = ap.parse_args()

    print(f"laguna CoT difficulty ladder — max_tokens={args.max_tokens}, "
          f"{args.samples} samples/rung\n")
    print(f"{'rung':<11} {'sample':>6} {'tokens':>8} {'chars':>8} {'files':>6} "
          f"{'wall':>6}  verdict")
    rows = []
    for name, prompt in LADDER:
        for i in range(1, args.samples + 1):
            t0 = time.monotonic()
            try:
                payload = complete(prompt, args.max_tokens)
            except Exception as exc:  # noqa: BLE001 — a rung failing must not stop the ladder
                print(f"{name:<11} {i:>6}  ERROR {exc}")
                continue
            wall = time.monotonic() - t0
            errs = payload.get("errors") or []
            d = (payload.get("data") or {}).get("completion") or {}
            text = d.get("text") or ""
            toks = d.get("tokensGenerated") or 0
            files = len(re.findall(r"#\s*===\s*FILE:", text))
            if errs:
                verdict = f"ERROR {errs[0].get('message','')[:50]}"
            elif toks >= args.max_tokens:
                verdict = "RAN TO CAP — never converged"
            else:
                verdict = f"terminated ({toks} tok)"
            print(f"{name:<11} {i:>6} {toks:>8} {len(text):>8,} {files:>6} "
                  f"{wall:>5.0f}s  {verdict}")
            rows.append((name, toks, len(text), files, toks >= args.max_tokens))

    print("\n── summary ─────────────────────────────────────────────")
    for name, _ in LADDER:
        r = [x for x in rows if x[0] == name]
        if not r:
            continue
        capped = sum(1 for x in r if x[4])
        med = sorted(x[1] for x in r)[len(r) // 2]
        print(f"  {name:<11} median {med:>7} tok   ran-to-cap {capped}/{len(r)}")

    trivial = [x for x in rows if x[0] == "trivial"]
    if trivial and all(x[4] for x in trivial):
        print("\nVERDICT (a): even a trivial prompt runs to the cap — laguna "
              "deliberates to whatever budget it is given.\n"
              "             Thinking mode needs reasoning_budget to be usable "
              "at all; a bigger context will not help.")
    elif trivial:
        print("\nVERDICT (b): trivial prompts terminate — CoT scales with the "
              "ASK, not the budget.\n"
              "             The batch turn is beyond what it will commit to; "
              "an uncapped max-context run is worth trying, and decomposing "
              "the batch is the structural fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
