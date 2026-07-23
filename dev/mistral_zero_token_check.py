"""Characterize Mistral Small 4's "turn produces zero tokens, never recovers" glitch.

Run this repeatedly (e.g. every 5 min) against a live game_challenge run. It
correlates ZERO-TOKEN inference calls with the thing the server log cannot tell
us: the EFFECTIVE temperature and the step that produced them.

Why temperature: mistral-small-4-119b-a6.yaml is the only `thinking: true`
config in the fleet with NO `temperature_floor` and NO `session_temp_floor`.
Ouroboros scales temperature DOWN per step (judge t*0.1, provision/probe
t*0.2), so a 0.5 base yields effective ~0.05-0.10. That is the exact regime in
which step37-flash (also a thinking model) emitted a bare unclosed think-tag
and collapsed to empty — fixed there by adding floors 0.4/0.5. If the zero
turns here cluster at the low end, this is the same bug and the fix transfers.

The server log alone is not enough: without floors configured it never emits
the 🌡️ line, so effective temperature appears only in the agent trace's
`inference_call` records.

Usage:  python dev/mistral_zero_token_check.py <workdir>
"""

from __future__ import annotations

import glob
import json
import statistics
import sys
from collections import Counter


def load_calls(workdir: str) -> list[dict]:
    traces = sorted(glob.glob(f"{workdir}/.agent/traces/*.jsonl"))
    if not traces:
        return []
    calls = []
    for path in traces:
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("event_type") == "inference_call":
                    calls.append(rec)
    return calls


def main() -> None:
    workdir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mistral_glitch/run"
    calls = load_calls(workdir)
    if not calls:
        print("no inference calls yet")
        return

    zero = [c for c in calls if (c.get("tokens_out") or 0) == 0]
    nonzero = [c for c in calls if (c.get("tokens_out") or 0) > 0]
    print(f"calls={len(calls)}  zero_token={len(zero)}  ({len(zero)/len(calls):.1%})")

    # Generated-but-EMPTY-after-strip is reported unconditionally: it is the
    # step37 signature (tokens produced, then an unclosed think-tag makes the
    # FSM strip everything) and is a DIFFERENT bug from generating nothing at
    # all. It must not be gated behind the zero-token early-return.
    stripped = [
        c
        for c in calls
        if (c.get("tokens_out") or 0) > 0
        and not (c.get("response_content") or "").strip()
    ]
    print(f"generated-but-empty-after-strip: {len(stripped)}")
    if stripped:
        print(f"  steps: {Counter(c.get('step') for c in stripped).most_common(5)}")
        print(f"  temps: {sorted(set(round(c.get('temperature') or 0, 3) for c in stripped))}")
        s = stripped[0]
        print(f"  sample thinking: {(s.get('thinking_content') or '')[:200]!r}")

    # A thinking:true model emitting NO thinking anywhere is its own smell
    # (channel not captured, or the model declining to think).
    thinking_calls = sum(1 for c in calls if (c.get("thinking_content") or "").strip())
    print(f"calls with thinking content: {thinking_calls}/{len(calls)}")

    if not zero:
        print("NO zero-token turns yet — glitch not reproduced in this window")
        return

    # 1. WHICH STEPS produce zero turns, and at what temperature.
    print("\nzero-token turns by step (temp of each):")
    by_step: dict[str, list[float]] = {}
    for c in zero:
        by_step.setdefault(c.get("step") or "?", []).append(c.get("temperature") or 0.0)
    for step, temps in sorted(by_step.items(), key=lambda kv: -len(kv[1])):
        uniq = sorted(set(round(t, 3) for t in temps))
        print(f"  {step:26} n={len(temps):3}  temps={uniq}")

    # 2. THE HYPOTHESIS TEST: do zero turns sit at LOWER temperature than
    #    healthy ones? (step37 collapse zone was ~0.06-0.12.)
    zt = [c.get("temperature") or 0.0 for c in zero]
    nt = [c.get("temperature") or 0.0 for c in nonzero]
    print("\ntemperature: zero vs healthy")
    print(f"  zero    n={len(zt):4} min={min(zt):.3f} median={statistics.median(zt):.3f} max={max(zt):.3f}")
    if nt:
        print(f"  healthy n={len(nt):4} min={min(nt):.3f} median={statistics.median(nt):.3f} max={max(nt):.3f}")
        verdict = (
            "CONSISTENT with the low-temp collapse (zero turns are colder)"
            if statistics.median(zt) < statistics.median(nt)
            else "NOT a simple low-temp story (zero turns are not colder)"
        )
        print(f"  -> {verdict}")

    # 3. DOES IT RECOVER? The user's report is that it does not. Walk the call
    #    sequence and find the longest consecutive run of zero-token turns, and
    #    whether ANY non-zero turn occurs after the first zero.
    seq = [1 if (c.get("tokens_out") or 0) == 0 else 0 for c in calls]
    first_zero = seq.index(1)
    after = seq[first_zero:]
    recovered = any(v == 0 for v in after)
    longest = cur = 0
    for v in seq:
        cur = cur + 1 if v else 0
        longest = max(longest, cur)
    print("\nrecovery:")
    print(f"  first zero at call #{first_zero} of {len(calls)}")
    print(f"  longest consecutive zero run: {longest}")
    print(f"  any healthy turn AFTER the first zero: {recovered}")
    if not recovered:
        print("  -> TERMINAL: never recovered (matches the reported glitch)")
    else:
        tail = seq[-20:]
        print(f"  -> recovers; last 20 turns (1=zero): {tail}")


if __name__ == "__main__":
    main()
