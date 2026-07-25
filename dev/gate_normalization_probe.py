#!/usr/bin/env python3
"""Probe 3 — is the design GATE CHAIN what makes production architectures uniform?

The observation that started this: ten greenfield runs across seven model
families produced near-identical architectures (models/loader/parser/engine/
main, +/- combat.py). Probe 1 tested the obvious suspect — the worked example's
filenames — and cleared it: renaming them moved nothing (0/4 adoptions, and
8/8 samples returned main.py even when the example said app.py).

But probe 1 turned up something sharper. Sampling `design_initial` alone gave
EIGHT DIFFERENT SHAPES from eight samples: flat vs src/ vs game/ vs adventure/,
6-8 modules, extras appearing and vanishing. The design step is noisy. The
production runs were not. So the uniformity is probably introduced somewhere
DOWNSTREAM of design_initial — and the chain it passes through is:

    design_initial -> parse_architecture -> design_gate_critique
                   -> design_reconcile (design_architecture with
                      existing_architecture populated)

`design_reconcile` re-renders the SAME template with the prior blueprint in
context and asks for a reconciled version. That is the obvious normalizer: a
step whose job is to make a design agree with itself could easily pull every
design toward one canonical shape.

THE TEST: generate N deliberately diverse blueprints, run each through the
critique + reconcile steps exactly as the flow does, and measure whether the
population CONVERGES.

    convergence = mean pairwise Jaccard similarity of the module file sets,
                  measured BEFORE reconcile and AFTER

If after >> before, the reconcile step is the normalizer and the ten runs'
uniformity is manufactured there, not by the design prompt.
If after ~= before, reconcile preserves diversity and the uniformity has some
other source (model priors, the objective's real structure, later phases).

Fidelity: blueprints are parsed with the REAL action
(action_parse_and_store_architecture) and the context is built with the REAL
formatters the flow's pre_compute declares (format_architecture_listing,
format_existing_architecture, format_tooling_convention), so what the model
sees matches production.

Usage:
    uv run python dev/gate_normalization_probe.py --render-only
    uv run python dev/gate_normalization_probe.py --samples 8 --out r.json
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dev.anchor_probe import OBJECTIVE, TEMPLATE, extract, fire, render  # noqa: E402

CRITIQUE_TEMPLATE = "design_and_plan/critique_coherence"


def _mission(objective: str):
    from agent.persistence.models import MissionConfig, MissionState

    return MissionState(
        objective=objective,
        status="active",
        config=MissionConfig(working_directory="/tmp/gate_probe"),
    )


async def _parse_into_mission(raw: str, mission) -> bool:
    """Parse a blueprint with the same action the flow uses."""
    from agent.actions.mission_actions import action_parse_and_store_architecture
    from agent.effects.mock import MockEffects
    from agent.models import FlowMeta, StepInput

    out = await action_parse_and_store_architecture(
        StepInput(
            context={"mission": mission, "inference_response": raw},
            params={},
            effects=MockEffects(mission=mission),
            meta=FlowMeta(flow_name="design_and_plan", step_id="parse_architecture"),
        )
    )
    return bool(out.result.get("architecture_parsed"))


def _fmt(name: str, arch) -> str:
    from agent import formatters

    return getattr(formatters, name)({"source": arch}, {})


def file_set(arch) -> set[str]:
    return {
        Path(str(getattr(m, "file", ""))).name
        for m in (getattr(arch, "modules", []) or [])
        if getattr(m, "file", "")
    }


def mean_pairwise_jaccard(sets: list[set[str]]) -> float:
    pairs = list(itertools.combinations([s for s in sets if s], 2))
    if not pairs:
        return float("nan")
    return statistics.mean(
        len(a & b) / len(a | b) for a, b in pairs if (a | b)
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    prompts_dir = REPO / "prompts"
    design_prompt = render(prompts_dir)

    if args.render_only:
        print(f"design prompt: {len(design_prompt)} chars")
        print("critique/reconcile prompts are built per-sample from a parsed")
        print("blueprint — run without --render-only to exercise them.")
        return

    from agent.loader import PromptRenderer

    renderer = PromptRenderer(prompts_dir)
    rows: list[dict] = []

    print("── stage 1: generate diverse blueprints ──")
    for i in range(args.samples):
        try:
            raw = fire(design_prompt, args.temperature)
        except Exception as e:  # noqa: BLE001
            print(f"  gen[{i}] FAILED: {type(e).__name__}: {e}")
            continue
        m = _mission(OBJECTIVE)
        if not asyncio.run(_parse_into_mission(raw, m)):
            print(f"  gen[{i}] unparseable — skipped")
            continue
        before = file_set(m.architecture)
        rows.append({"i": i, "mission": m, "before": sorted(before), "raw": raw})
        print(f"  gen[{i}] {len(before)} modules: {sorted(before)}")

    print("\n── stage 2: critique + reconcile each ──")
    for r in rows:
        m = r["mission"]
        arch = m.architecture
        ns_crit = {
            "context": {
                "blueprint_summary": _fmt("format_architecture_listing", arch),
                "mission_objective": OBJECTIVE,
                "tooling_convention": _fmt("format_tooling_convention", arch),
            },
            "input": {},
            "meta": {},
        }
        s, d = renderer.render_with_cache_split(CRITIQUE_TEMPLATE, ns_crit)
        try:
            verdict = fire(s + d, args.temperature)
        except Exception as e:  # noqa: BLE001
            verdict = f"<critique failed: {e}>"
        r["critique"] = verdict[:400]

        ns_rec = {
            "context": {
                "mission_objective": OBJECTIVE,
                "existing_architecture": _fmt("format_existing_architecture", arch),
            },
            "input": {},
            "meta": {},
        }
        s2, d2 = renderer.render_with_cache_split(TEMPLATE, ns_rec)
        try:
            raw2 = fire(s2 + d2, args.temperature)
        except Exception as e:  # noqa: BLE001
            print(f"  reconcile[{r['i']}] FAILED: {e}")
            continue
        after = extract(raw2)
        r["after"] = [Path(f).name for f in (after.get("modules") or []) if f]
        if not r["after"]:
            r["after"] = [
                Path(f).name for f in (after.get("creation_order") or []) if f
            ]
        print(
            f"  [{r['i']}] {len(r['before'])} -> {len(r['after'])} modules: "
            f"{sorted(set(r['after']))}"
        )

    done = [r for r in rows if r.get("after")]
    if len(done) < 2:
        print("\nnot enough completed samples to measure convergence")
        return

    before_sets = [set(r["before"]) for r in done]
    after_sets = [set(r["after"]) for r in done]
    jb, ja = mean_pairwise_jaccard(before_sets), mean_pairwise_jaccard(after_sets)
    ub, ua = len({frozenset(s) for s in before_sets}), len(
        {frozenset(s) for s in after_sets}
    )

    print("\n══ convergence ══")
    print(f"  n = {len(done)}")
    print(f"  mean pairwise Jaccard   before={jb:.3f}   after={ja:.3f}   delta={ja - jb:+.3f}")
    print(f"  distinct file-sets      before={ub}       after={ua}")
    print(
        f"  module count spread     before={max(len(s) for s in before_sets) - min(len(s) for s in before_sets)}"
        f"       after={max(len(s) for s in after_sets) - min(len(s) for s in after_sets)}"
    )
    print("\n  interpretation:")
    if ja - jb >= 0.15:
        print("  -> RECONCILE NORMALIZES: diverse designs converge on one shape.")
    elif abs(ja - jb) < 0.05:
        print("  -> NEUTRAL: reconcile preserves the diversity it was given;")
        print("     the production uniformity comes from somewhere else.")
    else:
        print("  -> WEAK/UNRESOLVED effect — do not over-read at this n.")

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                [
                    {k: v for k, v in r.items() if k not in ("mission", "raw")}
                    for r in rows
                ],
                indent=2,
            )
        )
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
