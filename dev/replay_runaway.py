#!/usr/bin/env python3
"""Replay the archived fix-target menu prompt to reproduce the runaway.

The June 11 qwen redo leg burned 43 watchdog-cancelled generations (up
to 130k tokens) on one menu turn whose only completed answer was 35
chars. Hypothesis: EOS starvation + paragraph-scale repetition at
effective temp ~0.24 — below the repetition guard's 8-token cycle
horizon. This script replays the exact archived prompt at the same
temperature and reports what comes back; with the long-cycle guard
deployed, a true runaway now aborts in ~10k tokens and the partial text
lands in llmvp/logs/runaway_captures/ for period analysis.

Run AFTER the benchmark finishes, with a qwen config active (the
harness restores gpt-oss — switch first):
    cd llmvp && printf qwen3-next-coder-80b-a3-jit > active_config.txt \
        && uv run llmvp.py --stop && uv run llmvp.py --backend
    uv run python dev/replay_runaway.py [archive_interactions.jsonl]
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections import Counter

DEFAULT_ARCHIVE = (
    "/Users/lah-rb/ouroboros-overnight/20260611-vbh/"
    "qwen3-redo-ABORTED-menu-runaway/interactions.jsonl"
)
ENDPOINT = "http://localhost:8008/graphql"
TEMPERATURE = 0.24  # t*0.3 against qwen's 0.8 default — the live value
MAX_TOKENS = 20000
RUNS = 3


def load_menu_prompt(path: str) -> str:
    for line in open(path):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "MENU CHOICE" in rec.get("prompt", "")[:40]:
            return rec["prompt"]
    raise SystemExit(f"no menu prompt found in {path}")


def run_inference(prompt: str) -> dict:
    query = {
        "query": """
            mutation Gen($request: CompletionRequest!) {
                createCompletion(request: $request) {
                    text tokensGenerated finished
                }
            }
        """,
        "variables": {
            "request": {
                "prompt": prompt,
                "temperature": TEMPERATURE,
                "maxTokens": MAX_TOKENS,
            }
        },
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(query).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=1800) as resp:
        return json.loads(resp.read())


def analyze(text: str) -> None:
    print(f"  length: {len(text)} chars")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return
    counts = Counter(lines)
    top, n = counts.most_common(1)[0]
    print(f"  distinct lines: {len(counts)}/{len(lines)}")
    if n > 3:
        print(f"  most repeated line (x{n}): {top[:100]!r}")
        # Loop period estimate: average gap between repeats of the top line
        idxs = [i for i, ln in enumerate(lines) if ln == top]
        if len(idxs) > 2:
            gaps = [b - a for a, b in zip(idxs, idxs[1:])]
            print(f"  repeat gap (lines): median≈{sorted(gaps)[len(gaps) // 2]}")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ARCHIVE
    prompt = load_menu_prompt(path)
    print(
        f"replaying menu prompt ({len(prompt)} chars) at T={TEMPERATURE}, {RUNS} runs"
    )
    for i in range(RUNS):
        print(f"\n── run {i + 1}/{RUNS} ──")
        try:
            out = run_inference(prompt)
        except Exception as e:  # noqa: BLE001
            print(f"  request failed/cancelled: {e}")
            print("  (if the long-cycle guard fired, see llmvp/logs/runaway_captures/)")
            continue
        if out.get("errors"):
            print(f"  server error: {out['errors'][0].get('message', '')[:200]}")
            print(
                "  (DegenerateGenerationError == guard fired; check runaway_captures/)"
            )
            continue
        data = (out.get("data") or {}).get("createCompletion") or {}
        text = data.get("text") or ""
        print(
            f"  tokensGenerated: {data.get('tokensGenerated')} "
            f"finished: {data.get('finished')}"
        )
        analyze(text)
        if len(text) < 200:
            print(f"  full response: {text!r}")


if __name__ == "__main__":
    main()
