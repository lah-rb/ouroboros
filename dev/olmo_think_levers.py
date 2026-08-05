#!/usr/bin/env python3
"""OLMo thinking-vs-sampling-levers probe (2026-07-23).

Question (Luke): is OLMo-Think's enormous decisional churn ("Alternatively"
x20 per think, 5:1-30:1 think:answer ratios) just slight undertuning given
its lineage — i.e., does mild repetition/presence penalty shorten the churn
without hurting answer quality?

Design: 4 sampling arms x 3 prompts, stateless rawCompletion (rawText
includes the <think> body — the FSM strips only downstream). The caller
(dev shell or CC session) edits llmvp/configs/olmo-3.1-32b-think.yaml per
arm and bounces the server; this script runs the prompt battery against
whatever is serving and appends one JSON line per generation to
dev/olmo_think_levers.results.jsonl.

Metrics per generation: think/answer chars+words, distinct 8-gram ratio
(runaway-guard style; verbatim-loop detector), churn markers per 1k words
(the decisional-repetition signature n-grams can't see), capped?, wall
seconds, auto-graded answer correctness.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request

ENDPOINT = "http://localhost:8008/graphql"
MAX_TOKENS = 8192
OUT = "dev/olmo_think_levers.results.jsonl"

PROMPTS = {
    "mechanical": "What is 17*24? Answer with just the number.",
    "decision": (
        "A Python file fails its checks with: SyntaxError line 1 (stray token"
        " 'x>'), and a lint error 'expected except/finally after try' at line"
        " 27. You have these actions: read_file, patch_line, rewrite_file,"
        " run_lint. Choose ONE next action. Answer with only a JSON object:"
        ' {"choice": "<action>", "why": "<one sentence>"}'
    ),
    "code": (
        "Write a Python function merge_intervals(intervals) that takes a list"
        " of [start, end] pairs and returns them merged and sorted. Return"
        " only the code in a fenced block."
    ),
}


def grade(name: str, answer: str) -> bool:
    if name == "mechanical":
        return "408" in answer
    if name == "decision":
        m = re.search(r"\{.*\}", answer, re.S)
        if not m:
            return False
        try:
            d = json.loads(m.group(0))
        except Exception:
            return False
        return d.get("choice") in {
            "read_file",
            "patch_line",
            "rewrite_file",
            "run_lint",
        }
    if name == "code":
        m = re.search(r"```(?:python)?\n(.*?)```", answer, re.S)
        src = m.group(1) if m else answer
        try:
            ns: dict = {}
            exec(src, ns)  # noqa: S102 — our own probe, local model output
            f = ns["merge_intervals"]
            return (
                f([[1, 3], [2, 6], [8, 10]]) == [[1, 6], [8, 10]]
                and f([]) == []
                and f([[5, 6], [1, 2]]) == [[1, 2], [5, 6]]
            )
        except Exception:
            return False
    return False


def metrics(think: str) -> dict:
    words = think.split()
    n = 8
    grams = [" ".join(words[i : i + n]) for i in range(max(0, len(words) - n))]
    ratio = (len(set(grams)) / len(grams)) if grams else 1.0
    per_k = (len(words) / 1000.0) or 1e-9
    churn = {
        p: len(re.findall(p, think))
        for p in ("Alternatively", "But wait", "Actually", "But note")
    }
    return {
        "think_words": len(words),
        "distinct_8gram": round(ratio, 3),
        "churn_per_1k_words": round(sum(churn.values()) / per_k, 1),
        "churn_counts": churn,
    }


def run_one(name: str, prompt: str) -> dict:
    q = {
        "query": "query($r: CompletionRequest!){ rawCompletion(request:$r){ rawText } }",
        "variables": {
            "r": {"prompt": prompt, "maxTokens": MAX_TOKENS, "temperature": 0.6}
        },
    }
    t0 = time.time()
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(q).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=1800) as resp:
        body = json.load(resp)
    wall = time.time() - t0
    raw = (body.get("data") or {}).get("rawCompletion", {}).get("rawText", "") or ""
    cut = raw.find("</think>")
    think = raw[:cut] if cut >= 0 else raw
    answer = raw[cut + 8 :] if cut >= 0 else ""
    rec = {
        "arm": sys.argv[1] if len(sys.argv) > 1 else "unlabeled",
        "prompt": name,
        "wall_s": round(wall, 1),
        "raw_chars": len(raw),
        "answer_chars": len(answer),
        "terminated": cut >= 0,
        "correct": grade(name, answer),
        **metrics(think),
    }
    return rec


def main() -> None:
    arm = sys.argv[1] if len(sys.argv) > 1 else "unlabeled"
    print(f"== arm {arm}")
    with open(OUT, "a") as out:
        for name, prompt in PROMPTS.items():
            rec = run_one(name, prompt)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(
                f"  {name:<10} think={rec['think_words']:>5}w "
                f"8gram={rec['distinct_8gram']} churn/1k={rec['churn_per_1k_words']:>5} "
                f"term={rec['terminated']} ok={rec['correct']} {rec['wall_s']}s"
            )


if __name__ == "__main__":
    main()
