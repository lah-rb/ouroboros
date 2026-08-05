#!/usr/bin/env python3
"""Does reasoning_effort change Hy3's CODE, now that the control token is real?

    llmvp/.venv/bin/python dev/hy3_coding_reasoning_probe.py

WHY A CODING TASK. Tencent's card names coding as a reasoning_effort use case
("set high for complex tasks — math, coding, reasoning"). The arithmetic probe
answered a $180 bill split and produced an empty think block at every level with
6-of-7 wrong answers, but arithmetic is where a REAP prune is least likely to
show a *recoverable* deficit. Code is checkable: the model either produces a
function that passes the tests or it does not, so "did reasoning help" stops
being a judgement call.

WHY IT IS WORTH RE-RUNNING AT ALL. The earlier arms were compromised. The PREFIX
arm placed the directive in system_content, which is tokenized as CONTENT with
special parsing OFF — so <｜reasoning_mode:opensource｜> (a type-3 CONTROL token,
id 120044) rendered as lookalike text and the model never received it. Only the
APPEND arm was valid, and that placement is now folded into the shipped family.
This is the first test where the control is canonical by construction.

MANACHER'S is the task because it is genuinely hard to get right from memory —
the sentinel interleaving, the mirror index, the centre/right-edge update — and
a model that reasons should beat one that does not. An easy task cannot separate
the levels, which is exactly how the first probe wasted a run.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ENDPOINT = "http://localhost:8008/graphql"
REPS = 3
LEVELS = ("no_think", "low", "high")

TASK = (
    "Implement Manacher's algorithm in Python as a function "
    "`longest_palindrome(s: str) -> str` returning the longest palindromic "
    "substring in O(n) time. Handle the empty string. Return only the code."
)

# Ties are resolved by "any correct answer of maximal length", so the checker
# compares LENGTH and verifies the result really is a palindrome and a substring.
CASES = [
    ("babad", 3),
    ("cbbd", 2),
    ("", 0),
    ("a", 1),
    ("ac", 1),
    ("forgeeksskeegfor", 10),
    ("abacabad", 7),
    ("aaaa", 4),
]

CHECKER = """
import sys
res = []
for s, want in %r:
    try:
        got = longest_palindrome(s)
    except Exception as e:
        res.append(f"EXC {s!r}: {type(e).__name__}"); continue
    if not isinstance(got, str):
        res.append(f"TYPE {s!r}: {type(got).__name__}"); continue
    if len(got) != want:
        res.append(f"LEN {s!r}: got {got!r} ({len(got)}) want {want}"); continue
    if got != got[::-1]:
        res.append(f"NOTPAL {s!r}: {got!r}"); continue
    if got not in s:
        res.append(f"NOTSUB {s!r}: {got!r}"); continue
print("PASS" if not res else "FAIL " + " | ".join(res[:3]))
""" % (CASES,)


def complete(prompt: str, level: str, max_tokens: int = 4000) -> dict:
    q = (
        "query($p:String!,$m:Int!,$r:String){completion(request:"
        "{prompt:$p,maxTokens:$m,reasoning:$r}){text tokensGenerated}}"
    )
    body = json.dumps(
        {"query": q, "variables": {"p": prompt, "m": max_tokens, "r": level}}
    ).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode())


def extract_code(text: str) -> str:
    m = re.findall(r"```(?:python)?\n(.*?)```", text, re.S)
    return m[0] if m else text


def run_check(code: str) -> str:
    """Execute the generated function against the cases in a subprocess — a
    hang or a crash must not take the probe with it."""
    prog = code + "\n" + CHECKER
    try:
        p = subprocess.run(
            [sys.executable, "-c", prog], capture_output=True, text=True, timeout=20
        )
    except subprocess.TimeoutExpired:
        return "FAIL timeout (likely not O(n), or an infinite loop)"
    out = (p.stdout or "").strip().splitlines()
    if out and (out[-1].startswith("PASS") or out[-1].startswith("FAIL")):
        return out[-1]
    err = (p.stderr or "").strip().splitlines()
    return "FAIL " + (err[-1][:90] if err else "no output")


def main() -> int:
    log = Path.home() / "ouroboros-runs" / "hy3_coding_probe.txt"
    lines = []

    def say(s: str) -> None:
        print(s, flush=True)
        lines.append(s)

    say("Hy3 coding reasoning probe — Manacher's, checked by execution")
    say(f"  {REPS} reps x {len(LEVELS)} levels, control token canonical (post_system)")
    say("")
    say(f"  {'level':<10}{'cot_chars':>11}{'code_chars':>12}{'verdict':>10}  detail")

    for level in LEVELS:
        for i in range(REPS):
            try:
                payload = complete(TASK, level)
            except (
                Exception
            ) as exc:  # noqa: BLE001 — one failure must not stop the sweep
                say(f"  {level:<10}{'—':>11}{'—':>12}{'ERROR':>10}  {exc}")
                continue
            errs = payload.get("errors") or []
            if errs:
                say(
                    f"  {level:<10}{'—':>11}{'—':>12}{'ERROR':>10}  "
                    f"{errs[0].get('message','')[:70]}"
                )
                continue
            text = ((payload.get("data") or {}).get("completion") or {}).get(
                "text"
            ) or ""
            code = extract_code(text)
            verdict = run_check(code) if code.strip() else "FAIL empty"
            # CoT is invisible in the returned text (it is stripped server-side),
            # so read it from the thinking endpoint rather than inferring.
            try:
                tq = json.dumps({"query": "{ thinking { content complete } }"}).encode()
                treq = urllib.request.Request(
                    ENDPOINT, data=tq, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(treq, timeout=30) as r:
                    tk = json.loads(r.read().decode())
                cot = len(
                    ((tk.get("data") or {}).get("thinking") or {}).get("content") or ""
                )
            except Exception:  # noqa: BLE001
                cot = -1
            head = verdict.split(" ", 1)
            say(
                f"  {level:<10}{cot:>11}{len(code):>12}{head[0]:>10}  "
                f"{(head[1][:64] if len(head) > 1 else '')}"
            )

    say("")
    say("READ IT AS: cot_chars rising across no_think < low < high means the dial")
    say("works; flat means it does not. PASS/FAIL is the part that matters either")
    say("way — if high does not raise correctness, the dial is not worth wiring")
    say("even if it does change the CoT length.")
    log.write_text("\n".join(lines) + "\n")
    print(f"\nwritten to {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
