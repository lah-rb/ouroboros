#!/usr/bin/env python3
"""Does every model's turn-terminator appear in its family's stop set?

    llmvp/.venv/bin/python dev/stop_token_audit.py

WHY THIS EXISTS. On 2026-07-29 the glm-4.7-flash tier arm burned 83 minutes and
produced zero files. Nothing was broken in the agent, the server, or the model:
`llmvp/formats/glm4.yaml` declared `gen_stop: "<|endoftext|>"` (the GGUF's
`eos_token_id`) while GLM actually ends an assistant turn on `eot_token_id` =
`<|user|>`. The model finished its work, emitted `<|user|>`, we did not stop, and
it wrote the other half of the conversation — for 70,687 tokens.

WHY NO GUARD CAUGHT IT. Every degeneration detector we own looks for repetition:
RepetitionGuard at token scale, the long-cycle guard at ~2k-token windows. A
model role-playing both sides of a chat is novel text forever. `decodeFailures 0`,
`unservable false`, long-cycle 0 across ~35 windows — all correct, all useless
here. A missing stop token is invisible to repetition detection by construction,
which is exactly why it needs a header check instead.

The check is one GGUF read per model and belongs in the new-family battery
alongside the fsm_labeller registration check.

READ IT AS: a MISSING flag means the model has a way to end its turn that we will
not honour. Empty eot/eom columns are fine — most families carry only eos.
"""

from __future__ import annotations

import glob
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gguf_geometry import read  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find(node, key):
    """Configs nest `family` and `path` under different sections depending on
    vintage. A recursive search beats guessing — the first version of this audit
    looked only at the top level, found nothing, and reported every model as
    broken."""
    if isinstance(node, dict):
        if isinstance(node.get(key), str):
            return node[key]
        for v in node.values():
            hit = find(v, key)
            if hit:
                return hit
    return None


def main() -> int:
    families = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "llmvp/formats/*.yaml"))):
        d = yaml.safe_load(open(f)) or {}
        families[d.get("family")] = (d.get("tokens", {}) or {}).get("gen_stop", "")

    rows, seen = [], set()
    for c in sorted(glob.glob(os.path.join(ROOT, "llmvp/configs/*.yaml"))):
        d = yaml.safe_load(open(c)) or {}
        path, family = find(d, "path"), find(d, "family")
        if not path or not os.path.exists(path) or path in seen:
            continue
        seen.add(path)
        try:
            kv, _ = read(path)
        except Exception:  # noqa: BLE001 — one unreadable GGUF must not stop the sweep
            continue
        toks = kv.get("tokenizer.ggml.tokens", [])

        def tok(k):
            i = kv.get(f"tokenizer.ggml.{k}")
            return toks[i] if isinstance(i, int) and i < len(toks) else None

        name = os.path.basename(c)[:-5]
        stop = families.get(family)
        if stop is None:
            rows.append((name, family or "?", "FAMILY NOT FOUND", None, None, ["?"]))
            continue
        eot, eom = tok("eot_token_id"), tok("eom_token_id")
        rows.append((name, family, stop, eot, eom,
                     [t for t in (eot, eom) if t and t not in stop]))

    print(f"{'config':<30}{'family':<11}{'gen_stop':<22}{'eot':<17}{'eom':<17}")
    for n, f, s, eot, eom, miss in rows:
        flag = "  <<< MISSING " + ",".join(map(str, miss)) if miss else ""
        print(f"{n:<30}{f:<11}{str(s):<22}{str(eot):<17}{str(eom):<17}{flag}")

    bad = [r for r in rows if r[5]]
    print(f"\n{len(bad)} of {len(rows)} models declare a turn-terminator "
          f"the family does not stop on.")
    for r in bad:
        print(f"  {r[0]} ({r[1]}): stops on {r[2]!r}, model also ends on {r[5]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
