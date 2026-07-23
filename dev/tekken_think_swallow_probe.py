"""Does an unclosed [THINK] swallow the whole response? (Mistral Small 4 / tekken)

Mechanism under test (core/fsm_labeller._bracket_think_start_phase):
tekken declares inline_tags thinking with open_tag "[THINK]" / close_tag
"[/THINK]", and the renderer does NOT prime the opener (the generation prompt
is empty for tekken — the model generates bare after [/INST]). So the model
emits both tags itself, and the FSM starts in CONTENT and flips to THINKING on
"[THINK]".

If the model opens [THINK] and never closes it — truncated at max_tokens, or it
simply doesn't emit the closer — the FSM stays in THINKING to the end of the
stream and the ENTIRE response is labelled reasoning and stripped. The caller
sees empty content: a turn that "produced no tokens" despite the model having
generated plenty. That is the swallow.

This probe compares RAW (unstripped) against STRIPPED output for the same
prompt, and deliberately includes a low-max_tokens arm to force truncation
mid-think.

Usage:  python dev/tekken_think_swallow_probe.py
"""

from __future__ import annotations

import json
import urllib.request

URL = "http://localhost:8008/graphql"

RAW_Q = """query($r: CompletionRequest!){ rawCompletion(request: $r){ rawText tokensGenerated } }"""
STRIP_Q = """query($r: CompletionRequest!){ completion(request: $r){ text tokensGenerated truncated } }"""


def call(query: str, prompt: str, max_tokens: int, temperature: float) -> dict:
    body = json.dumps(
        {
            "query": query,
            "variables": {
                "r": {
                    "prompt": prompt,
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                }
            },
        }
    ).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())


PROMPTS = [
    (
        "reasoning",
        "A farmer has 3 pens with 4 sheep each. Two sheep escape, then he buys "
        "5 more and splits them evenly across the pens. How many per pen? "
        "Think it through step by step.",
    ),
    (
        "trivial",
        "Say the single word: hello",
    ),
]

# (label, max_tokens) — the SMALL budget is the one that should truncate the
# model mid-[THINK] and expose the swallow.
BUDGETS = [("ample", 400), ("truncating", 60)]


def main() -> None:
    print(f"{'case':22} {'raw_tok':>7} {'raw_len':>7} {'strip_len':>9}  open  close  VERDICT")
    print("-" * 78)
    for pname, prompt in PROMPTS:
        for bname, budget in BUDGETS:
            try:
                raw = call(RAW_Q, prompt, budget, 0.5)
                strip = call(STRIP_Q, prompt, budget, 0.5)
            except Exception as exc:  # noqa: BLE001
                print(f"{pname}/{bname:12} ERROR {exc}")
                continue
            rd = (raw.get("data") or {}).get("rawCompletion") or {}
            sd = (strip.get("data") or {}).get("completion") or {}
            raw_text = rd.get("rawText") or ""
            strip_text = sd.get("text") or ""
            has_open = "[THINK]" in raw_text
            has_close = "[/THINK]" in raw_text
            swallowed = bool(raw_text.strip()) and not strip_text.strip()
            verdict = (
                "SWALLOWED (raw non-empty, stripped EMPTY)"
                if swallowed
                else ("ok" if strip_text.strip() else "both empty")
            )
            print(
                f"{pname + '/' + bname:22} {rd.get('tokensGenerated', 0):>7} "
                f"{len(raw_text):>7} {len(strip_text):>9}  "
                f"{'Y' if has_open else '.':^5} {'Y' if has_close else '.':^6} {verdict}"
            )
            if swallowed:
                print(f"    raw head: {raw_text[:200]!r}")
                print(f"    raw tail: {raw_text[-120:]!r}")


if __name__ == "__main__":
    main()
