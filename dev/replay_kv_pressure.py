#!/usr/bin/env python3
"""Replay the generation that KV pressure destroyed, and check it survives now.

THE CASE. On 2026-07-27 an APEX `=== CODE EDITOR ===` batch turn generated
48,318 tokens containing 15 complete `# === FILE:` blocks, hit
`KV cell pool exhausted — stream evicted`, and the agent received an exception
with none of the work. The prompt is recoverable from `interactions.jsonl`, so
the exact turn can be re-run against the fixed engine.

WHAT "FIXED" LOOKS LIKE — the run must satisfy all three:

  1. no RetriableEngineError reaches the caller
  2. if it was cut, `truncated` is TRUE (a force-window stops BELOW max_tokens,
     so the derived `tokens_generated >= max_tokens` test cannot see it)
  3. complete FILE blocks come back instead of nothing

Run with a laguna config active and the server up:

    uv run python dev/replay_kv_pressure.py [--max-tokens N]

Prints a verdict line and exits non-zero if any check fails, so it can gate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request

ENDPOINT = "http://localhost:8008/graphql"
INTERACTIONS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "llmvp",
    "logs",
    "interactions.jsonl",
)
# The archived turn: the largest response in the eviction window. Matched by
# size rather than timestamp so the script keeps working after log rotation.
_MIN_ARCHIVED_RESPONSE = 200_000


def find_archived_prompt(path: str, tail_bytes: int = 60_000_000) -> tuple[str, int]:
    """(prompt, response_chars) for the archived mega-generation."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - tail_bytes))
        chunk = f.read().decode("utf-8", "replace")
    best = None
    for line in chunk.split("\n")[1:]:  # first line is probably a fragment
        try:
            rec = json.loads(line)
        except Exception:
            continue
        resp = rec.get("response") or ""
        if len(resp) >= _MIN_ARCHIVED_RESPONSE and (
            best is None or len(resp) > len(best[1])
        ):
            best = (rec.get("prompt") or "", resp)
    if not best:
        raise SystemExit(
            f"No archived response >= {_MIN_ARCHIVED_RESPONSE} chars in the tail "
            f"of {path} — nothing to replay."
        )
    return best[0], len(best[1])


def complete(prompt: str, max_tokens: int) -> dict:
    query = """
    query($p: String!, $m: Int!) {
      completion(request: {prompt: $p, maxTokens: $m}) {
        text tokensGenerated truncated finished
      }
    }
    """
    body = json.dumps(
        {"query": query, "variables": {"p": prompt, "m": max_tokens}}
    ).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=3600) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tokens", type=int, default=65536)
    args = ap.parse_args()

    prompt, archived_len = find_archived_prompt(INTERACTIONS)
    print(f"archived prompt : {len(prompt)} chars")
    print(f"archived output : {archived_len} chars (the run that was discarded)")
    print(f"max_tokens      : {args.max_tokens}\n")

    t0 = time.monotonic()
    payload = complete(prompt, args.max_tokens)
    wall = time.monotonic() - t0

    errors = payload.get("errors") or []
    data = (payload.get("data") or {}).get("completion") or {}
    text = data.get("text") or ""
    files = len(re.findall(r"#\s*===\s*FILE:", text))

    print(f"wall            : {wall:.0f}s")
    print(f"errors          : {[e.get('message', '')[:120] for e in errors] or 'none'}")
    print(f"tokensGenerated : {data.get('tokensGenerated')}")
    print(f"truncated       : {data.get('truncated')}")
    print(f"response chars  : {len(text)}")
    print(f"FILE blocks     : {files}\n")

    ok = True
    retriable = any("KV cell pool" in str(e.get("message", "")) for e in errors)
    if retriable:
        print("FAIL: the caller still received a KV-pressure error")
        ok = False
    if errors and not retriable:
        print(f"FAIL: unexpected error: {errors}")
        ok = False
    if not errors and files == 0:
        print("FAIL: completed with zero FILE blocks — nothing usable came back")
        ok = False
    # A cut MUST be announced; a natural stop needs no flag.
    if not errors and data.get("tokensGenerated", 0) >= args.max_tokens:
        if not data.get("truncated"):
            print("FAIL: hit the budget but truncated is False")
            ok = False

    print("VERDICT:", "PASS — work survived" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
