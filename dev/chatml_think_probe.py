#!/usr/bin/env python3
"""ChatML think-budget probe: default vs /no_think, across task shapes.

The Qwopus lesson: a distill's real-world CoT budget can be 10x its
billing, and one 32k-token design monologue burns an hour leg — so new
qwen-family distills get this five-minute spot-check BEFORE any mission
leg. Sends hand-built chatml prompts (template per formats/chatml.yaml)
for three task shapes, in default mode and with the Qwen-family
``/no_think`` soft toggle appended to the user message, and measures the
<think> span + answer length per completion.

Answers: (1) is default-mode thinking sane or runaway; (2) does the
advertised think-off toggle actually work.

Run against the live server. Usage: python dev/chatml_think_probe.py
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
    "design": (
        "Sketch the module layout (files + one-line responsibilities) for a "
        "small text-adventure game engine in Python. Be concise."
    ),
}
TEMPLATE = (
    "<|im_start|>system\n"
    "You are a helpful assistant.<|im_end|>"
    "<|im_start|>user\n{task}{toggle}<|im_end|>"
    "<|im_start|>assistant\n"
)


def gql(query: str, variables: dict, timeout: float = 1200):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def think_split(text: str) -> tuple[int, int]:
    if "<think>" in text:
        pre, _, rest = text.partition("<think>")
        thought, _, answer = rest.partition("</think>")
        return len(thought), len(pre) + len(answer)
    return 0, len(text)


def main() -> None:
    print(
        f"{'mode':10s} {'task':8s} {'think_ch':>9s} {'answer_ch':>10s} {'hit_cap':>8s}"
    )
    stats: dict[str, list[int]] = {}
    for mode, toggle in (("default", ""), ("/no_think", " /no_think")):
        for name, task in TASKS.items():
            prompt = TEMPLATE.format(task=task, toggle=toggle)
            r = gql(
                "query($r: CompletionRequest!){ rawCompletion(request:$r){ rawText } }",
                {"r": {"prompt": prompt, "maxTokens": 6144, "temperature": 0.6}},
            )
            if not r.get("data") or not r["data"].get("rawCompletion"):
                print(f"{mode:10s} {name:8s}  ERROR: {str(r.get('errors'))[:120]}")
                continue
            text = r["data"]["rawCompletion"]["rawText"] or ""
            think, answer = think_split(text)
            cap = "YES" if len(text) > 6144 * 3 else ""  # rough char proxy
            stats.setdefault(mode, []).append(think)
            print(f"{mode:10s} {name:8s} {think:9d} {answer:10d} {cap:>8s}", flush=True)
    print(
        "\nmean think chars: ",
        {m: round(sum(v) / max(len(v), 1)) for m, v in stats.items()},
    )
    d, n = stats.get("default", [0]), stats.get("/no_think", [0])
    md, mn = sum(d) / max(len(d), 1), sum(n) / max(len(n), 1)
    print(
        "VERDICT: default mean %.0f chars — %s; /no_think %s"
        % (
            md,
            (
                "RUNAWAY-CLASS (Qwopus territory)"
                if md > 20000
                else "heavy" if md > 8000 else "sane"
            ),
            (
                f"WORKS ({mn:.0f} chars, {mn / max(md, 1):.0%} of default)"
                if mn < md * 0.25
                else f"NOT effective ({mn:.0f} chars)"
            ),
        )
    )


if __name__ == "__main__":
    main()
