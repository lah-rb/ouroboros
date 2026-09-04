"""Preface A/B: separate the two changes fb801b5 shipped together.

fb801b5 softened the multi-window pack preface AND added a `prior_keys`
block handing later windows the keys earlier windows used. Measured
together on two high-coinage papers: -94% new keys, -32% grounded values.
Which change owns the recall cost was never measured. Four arms, same two
papers, PRODUCTION `_pack_windowed`, no booking:

    A  hard preface, no prior_keys   (the original, re-run for a same-day pair)
    B  soft preface, no prior_keys
    C  hard preface, prior_keys
    D  soft preface, prior_keys      (production today)

Patches are module attributes only: `_PACK_PREFACE` (the string) and
`_render_prompt` (a wrapper that drops `prior_keys` from the context), so
the code under test is exactly production's.

    setsid nohup .venv/bin/python dev/bench_preface_ab.py > ~/tmp/preface_ab.log 2>&1 &
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.actions import curation_actions as ca  # noqa: E402
from agent.actions.scholarly_actions import read_databank  # noqa: E402
from agent.effects.child import ChildEffects  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402
from bench_staged_pack import _doc_for  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
DOMAIN = os.environ.get("MEASURE_DOMAIN", "curate_remote")
OUT = os.path.expanduser("~/tmp/preface_ab.json")
PAPERS = ["doi_10.1029_2006je002728", "doi_10.1002_2013je004605"]

HARD = (
    "[This is part {n} of {total} of one paper — {sections} consecutive "
    'section(s) starting at "{heading}". Pack ONLY values stated in THIS '
    "part; other parts are packed separately and merged.]\n\n"
)
SOFT = ca._PACK_PREFACE
ARMS = {
    "A hard/no-prior": (HARD, False),
    "B soft/no-prior": (SOFT, False),
    "C hard/prior": (HARD, True),
    "D soft/prior": (SOFT, True),
}

_orig_render = ca._render_prompt


def _renderer(prior: bool):
    async def render(name, ctx):
        if not prior:
            ctx = {k: v for k, v in ctx.items() if k != "prior_keys"}
        return await _orig_render(name, ctx)

    return render


async def main() -> None:
    cfg = json.load(open(f"{ROOT}/.agent/mission.json"))["config"]
    parent = LocalEffects(ROOT, llmvp_domains=cfg.get("llmvp_domains"))
    fx = ChildEffects(parent, branch="bench:preface_ab", inference_domain=DOMAIN)
    bank = await read_databank(fx)
    registry = await ca._load_registry(fx)
    rows = []
    if os.path.exists(OUT):
        rows = json.load(open(OUT))
    done = {(r["arm"], r["key"]) for r in rows}
    for arm, (preface, prior) in ARMS.items():
        ca._PACK_PREFACE = preface
        ca._render_prompt = _renderer(prior)
        for key in PAPERS:
            if (arm, key) in done:
                continue
            doc = _doc_for(ROOT, key)
            t0 = time.time()
            pack = await ca._pack_windowed(fx, doc, registry)
            q = pack.get("quality") or {}
            row = dict(
                arm=arm,
                key=key,
                status=pack["status"],
                seconds=round(time.time() - t0),
                new_keys=q.get("new_keys"),
                reused_keys=q.get("reused_keys"),
                windows=q.get("windows"),
                windows_passed=q.get("windows_passed"),
                leaves=q.get("numeric_leaves"),
                grounding=q.get("grounding_rate"),
                keys=sorted((pack.get("data") or {}).keys()),
            )
            rows.append(row)
            json.dump(rows, open(OUT, "w"), indent=1)
            print(
                f"{arm:<16} {key:<30} {pack['status']:<11} new {q.get('new_keys')} "
                f"reused {q.get('reused_keys')} leaves {q.get('numeric_leaves')} "
                f"win {q.get('windows_passed')}/{q.get('windows')} gr {q.get('grounding_rate')} "
                f"{row['seconds']}s",
                flush=True,
            )
    ca._PACK_PREFACE = SOFT
    ca._render_prompt = _orig_render
    print("\narm              papers  new_keys  leaves  windows")
    for arm in ARMS:
        rs = [r for r in rows if r["arm"] == arm]
        if not rs:
            continue
        nk = sum(r["new_keys"] or 0 for r in rs)
        lv = sum(r["leaves"] or 0 for r in rs)
        wp = sum(r["windows_passed"] or 0 for r in rs)
        wt = sum(r["windows"] or 0 for r in rs)
        print(f"{arm:<16} {len(rs):>6}  {nk:>8}  {lv:>6}  {wp}/{wt}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
