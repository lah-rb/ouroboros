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


def _family_stop_sets():
    """family -> the FULL serving stop set, from the renderer itself.

    The first audit compared only the raw ``tokens.gen_stop`` field and
    flagged terminators the LIVE stop set already covers — glm4's eot
    <|user|> is caught by stop_tokens()'s fake-turn-opener derivation,
    so the audit cried wolf on it forever while the genuinely missing
    eom <|observation|> hid in the noise. Measure what serving measures.
    """
    import sys as _sys

    _sys.path.insert(0, os.path.join(ROOT, "llmvp"))
    from types import SimpleNamespace
    from unittest.mock import patch

    import core.config as ccfg
    from formats.registry import clear_cache, get_renderer

    stop_sets = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "llmvp/formats/*.yaml"))):
        d = yaml.safe_load(open(f)) or {}
        fam = d.get("family")
        if not fam:
            continue
        cfg = SimpleNamespace(
            model=SimpleNamespace(
                thinking="per_request", thinking_available=True, family=fam
            )
        )
        try:
            with patch.object(ccfg, "get_config", lambda c=cfg: c):
                clear_cache()
                stop_sets[fam] = list(get_renderer(fam).stop_tokens())
        except Exception:  # noqa: BLE001 — a family that fails to render
            stop_sets[fam] = [(d.get("tokens", {}) or {}).get("gen_stop", "")]
    return stop_sets


def _resolve_config(path: str, depth: int = 0) -> dict:
    """Load a config, following `extends:` for missing keys (family/path).
    The first audit reported every extends-child as FAMILY NOT FOUND."""
    d = yaml.safe_load(open(path)) or {}
    parent = d.get("extends")
    if parent and depth < 4:
        for cand in (
            os.path.join(os.path.dirname(path), f"{parent}.yaml"),
            os.path.join(ROOT, "llmvp/configs", f"{parent}.yaml"),
        ):
            if os.path.exists(cand):
                base = _resolve_config(cand, depth + 1)
                # Capture the parent's model block BEFORE the top-level
                # update overwrites it with the child's partial one — the
                # child declares only `path`/`name`, so a naive update
                # dropped the inherited `family` and every extends-child
                # audited as FAMILY NOT FOUND.
                merged_model = dict(base.get("model") or {})
                merged_model.update(dict(d.get("model") or {}))
                base.update({k: v for k, v in d.items() if v is not None})
                base["model"] = merged_model
                return base
    return d


def main() -> int:
    stop_sets = _family_stop_sets()

    rows, seen = [], set()
    for c in sorted(glob.glob(os.path.join(ROOT, "llmvp/configs/*.yaml"))):
        d = _resolve_config(c)
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
        stops = stop_sets.get(family)
        if stops is None:
            rows.append((name, family or "?", "FAMILY NOT FOUND", None, None, ["?"]))
            continue
        eot, eom = tok("eot_token_id"), tok("eom_token_id")
        # Covered when any serving stop is a substring of the terminator or
        # vice versa (generation breaks on substring match).
        missing = [
            t
            for t in (eot, eom)
            if t and not any(s and (s in t or t in s) for s in stops)
        ]
        stop = ",".join(stops)
        rows.append((name, family, stop, eot, eom, missing))

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
