#!/usr/bin/env python3
"""Step-3.7 reasoning-level probe: is the "Reasoning: <level>" knob connected?

The boss night run showed a 71% thinking share under thinking_mode: medium.
chatml renders the level as a soft system-message line ("Reasoning: {level}"
— mirroring Step's own GGUF chat template per formats/chatml.yaml), so the
knob only works if the model was actually trained to honor it. This probe
sends hand-built chatml prompts (template replicated exactly) at low /
medium / high across three task types via rawCompletion and measures the
<think>-span length and total output per level.

Verdict: monotonic, well-separated thinking lengths (low < medium < high)
= the knob works and the adaptive router has a real lever on step37; flat
= the line is decoration and CoT verbosity needs a different control.

Run with the step37 server serving. Usage: python dev/step37_reasoning_probe.py
"""

from __future__ import annotations

import json
import urllib.request

URL = "http://localhost:8008/graphql"
TASKS = {
    "code": (
        "Write a Python function `merge_intervals(intervals)` that merges "
        "overlapping [start, end] intervals, plus two doctests."
    ),
    "factual": (
        "A train travels 240 km in 3 hours, then 180 km in 2 hours. What is "
        "its average speed for the whole journey? Answer with the number."
    ),
    "planning": (
        "List the key steps to migrate a small Flask app from SQLite to "
        "PostgreSQL with minimal downtime. Be concise."
    ),
}
# --prefill-think appends the opener: v1 found step37 does NOT think at
# all without it (zero <think> spans at every level — thinking is
# prefill-gated, the gemma pad mechanism). v2 asks whether the Reasoning
# line modulates length INSIDE an opened think block.
import sys as _sys

PREFILL = "<think>" if "--prefill-think" in _sys.argv else ""
TEMPLATE = (
    "<|im_start|>system\n"
    "Reasoning: {level}\n\n"
    "You are a helpful assistant.<|im_end|>"
    "<|im_start|>user\n{task}<|im_end|>"
    "<|im_start|>assistant\n" + PREFILL
)


def gql(query: str, variables: dict, timeout: float = 900):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def think_split(text: str) -> tuple[int, int]:
    """(thinking_chars, answer_chars) from inline <think> tags."""
    if PREFILL and "</think>" in text:
        thought, _, answer = text.partition("</think>")
        return len(thought), len(answer)
    if "<think>" in text:
        pre, _, rest = text.partition("<think>")
        thought, _, answer = rest.partition("</think>")
        return len(thought), len(pre) + len(answer)
    return 0, len(text)


def main() -> None:
    print(f"{'level':8s} {'task':10s} {'think_ch':>9s} {'answer_ch':>10s}")
    totals: dict[str, list[int]] = {}
    for level in ("low", "medium", "high"):
        for name, task in TASKS.items():
            prompt = TEMPLATE.format(level=level, task=task)
            r = gql(
                "query($r: CompletionRequest!){ rawCompletion(request:$r){ rawText } }",
                {"r": {"prompt": prompt, "maxTokens": 3072, "temperature": 0.6}},
            )
            if not r.get("data") or not r["data"].get("rawCompletion"):
                print(f"{level:8s} {name:10s}  ERROR: {str(r.get('errors'))[:120]}")
                continue
            text = r["data"]["rawCompletion"]["rawText"] or ""
            think, answer = think_split(text)
            totals.setdefault(level, []).append(think)
            print(f"{level:8s} {name:10s} {think:9d} {answer:10d}", flush=True)
    print("\nmean thinking chars per level:")
    means = {}
    for level, vals in totals.items():
        means[level] = sum(vals) / max(len(vals), 1)
        print(f"  {level:8s} {means[level]:8.0f}")
    if all(k in means for k in ("low", "medium", "high")):
        monotonic = means["low"] < means["medium"] < means["high"]
        separated = means["high"] > 2 * max(means["low"], 1)
        print(
            f"\nVERDICT: monotonic={monotonic} separated={separated} — "
            + ("the Reasoning knob WORKS on step37"
               if monotonic and separated
               else "the Reasoning line is NOT meaningfully steering CoT")
        )


if __name__ == "__main__":
    main()
