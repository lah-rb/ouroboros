#!/usr/bin/env python3
"""Recall probe: does the model state the facts it was trained on? (rock venv)

WHY THIS INSTRUMENT. §11: a respectable loss beside identical band lists for
Quartz and Hematite. §19 drops the species holdout, so the oracle's primary
test is RECALL of seen species, scored on what the model GENERATES — and the
template-collapse metric is the gap between frames the model trained on and
frames it never saw (templates.PROBE_FRAMES).

For N seeded seen species with a canonical Raman fact, four questions each:
  formula          species -> IMA formula          (exact string)
  crystal_system   species -> crystal system       (system word present)
  bands            species -> canonical Raman bands (>=2 of the 3 strongest
                   within ±10 cm-1, the field's comparison tolerance)
  inverse          4 canonical bands -> species     (name present)
and, for the synth pilot (2026-09-11; --target-species synth_species.json
[--group T_R|T_L|T_RL|C]), per-group reporting plus:
  identification   SEEKER schema: an instrument-perturbed full peak list
                   (synth_variance) -> species
  libs_lines       species -> predicted LIBS lines (>=2 of the 3 strongest
                   within ±0.2 nm)                 [species with a LIBS fact]
  libs_inverse     3 strongest LIBS lines -> species
  cross_modal      Raman top-3 + LIBS top-3 -> species
Scorers live in probe_scoring.py (pure; tested without torch).

Each question is asked through one TRAINED frame (the fact's first-ranked
question frame) and through the PROBE frame. Greedy decoding, bf16, one
model at a time, several models per run (`--models base=DIR,stage1=DIR`).

  ./.venv/bin/python probe_recall.py --models base=~/models/OLMo-2-0425-1B --n 200
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

from facts import build_facts  # noqa: E402
from probe_scoring import (  # noqa: E402
    TOL_CM1,
    cross_modal_prompt,
    identification_prompt,
    libs_inverse_prompt,
    score,
    strongest_lines,
)
import synth_variance as sv  # noqa: E402
from templates import FRAMES, PROBE_FRAMES, fields, pick_frames  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE_JSON = os.path.join(HERE, "probe_species.json")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _question_frame(fact_id: str, kind: str):
    picks = pick_frames(
        fact_id,
        kind,
        len(FRAMES[kind]["statements"]) + len(FRAMES[kind]["questions"]),
        questions=True,
        salt="s2",
    )
    return next(fr for _, fr in picks if fr in FRAMES[kind]["questions"])


def build_items(
    n: int,
    seed: int,
    exclude: set[str],
    *,
    only: set[str] | None = None,
    groups: dict[str, str] | None = None,
) -> list[dict]:
    """Probe items. `only` restricts to these species (the synth pilot's
    target/control groups; n is then a cap, 0 = all); `groups` stamps each
    item's group for per-group reporting. Species with a LIBS fact also get
    the libs_lines / libs_inverse tasks; the identification task feeds the
    SEEKER schema on an instrument-perturbed list (synth_variance); species
    with both Raman and LIBS get the cross-modal prompt."""
    facts = build_facts(exclude if not only else set())
    by = {}
    for f in facts:
        by.setdefault(f.kind, {})
        if isinstance(f.species, str):
            if f.kind == "raman_bands" and not f.canonical:
                continue
            by[f.kind][f.species] = f
    species = sorted(s for s in by.get("raman_bands", {}) if s in by.get("formula", {}))
    if only is not None:
        species = [s for s in species if s in only]
    random.Random(seed).shuffle(species)
    if n:
        species = species[:n]
    items = []
    for sp in species:
        r, fo, st = (
            by["raman_bands"][sp],
            by["formula"][sp],
            by.get("structure", {}).get(sp),
        )
        rf = fields(r)
        lf = by.get("libs_lines", {}).get(sp)
        libs_top3 = strongest_lines(lf.payload["groups"]) if lf else []
        bands_all, rel = r.payload["bands_cm1"], r.payload.get("rel") or []
        # the three STRONGEST bands (by intensity when known) are what the
        # cross-modal prompt names; `bands` keeps the first-listed three the
        # v4 probe scored against
        if rel and len(rel) == len(bands_all):
            strongest3 = [
                b for b, _ in sorted(zip(bands_all, rel), key=lambda br: -br[1])[:3]
            ]
        else:
            strongest3 = bands_all[:3]
        prng = sv.rng_for("probe", sp, seed)
        inst = sv.sample_raman(prng)
        peaks = sv.perturb_bands(prng, bands_all, rel, inst)
        extra = {
            "identification": {
                "trained": identification_prompt(
                    peaks, str(r.payload.get("laser_nm") or "")
                ),
                "probe": identification_prompt(peaks, ""),
            }
        }
        if lf:
            extra["libs_lines"] = {
                "trained": _question_frame(lf.fact_id, "libs_lines").prompt.format(
                    **fields(lf)
                ),
                "probe": PROBE_FRAMES["libs_lines"].prompt.format(**fields(lf)),
            }
            extra["libs_inverse"] = {
                "trained": libs_inverse_prompt(libs_top3),
                "probe": libs_inverse_prompt(libs_top3) + " named",
            }
            extra["cross_modal"] = {
                "trained": cross_modal_prompt(strongest3, libs_top3),
                "probe": cross_modal_prompt(strongest3, libs_top3).replace(
                    " — the mineral is", ". Phase:"
                ),
            }
        items.append(
            {
                "species": sp,
                "group": (groups or {}).get(sp, ""),
                "formula": fo.formula,
                "bands": r.payload["bands_cm1"][:3],
                "libs_top3": libs_top3,
                "system": (
                    (st.payload.get("crystal_system") or "").lower() if st else ""
                ),
                "prompts": {
                    **extra,
                    "formula": {
                        "trained": _question_frame(fo.fact_id, "formula").prompt.format(
                            **fields(fo)
                        ),
                        "probe": PROBE_FRAMES["formula"].prompt.format(**fields(fo)),
                    },
                    "bands": {
                        "trained": _question_frame(
                            r.fact_id, "raman_bands"
                        ).prompt.format(**rf),
                        "probe": PROBE_FRAMES["raman_bands"].prompt.format(**rf),
                    },
                    "inverse": {
                        "trained": _question_frame(r.fact_id, "inverse").prompt.format(
                            **rf
                        ),
                        "probe": PROBE_FRAMES["inverse"].prompt.format(**rf),
                    },
                    **(
                        {
                            "crystal_system": {
                                "trained": _question_frame(
                                    st.fact_id, "structure"
                                ).prompt.format(**fields(st), sg_clause=""),
                                "probe": PROBE_FRAMES["structure"].prompt.format(
                                    **fields(st)
                                ),
                            }
                        }
                        if st and st.payload.get("crystal_system")
                        else {}
                    ),
                },
            }
        )
    return items


@torch.no_grad()
def run_model(
    name: str,
    path: str,
    items: list[dict],
    max_new: int,
    device: str,
    batch_size: int = 32,
) -> dict:
    """Greedy generations for every (item, task, frame) prompt, BATCHED with
    left padding (the scoring is per generation and unchanged; single-prompt
    generation over 16,000 prompts per model took hours on the 3090)."""
    tok = AutoTokenizer.from_pretrained(path)
    tok.padding_side = "left"
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    model = (
        AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16)
        .to(device)
        .eval()
    )
    jobs = [
        (it, task, kind, prompt)
        for it in items
        for task, pr in it["prompts"].items()
        for kind, prompt in pr.items()
    ]
    # sort by length so a batch pads little; results are keyed, not ordered
    order = sorted(range(len(jobs)), key=lambda i: len(jobs[i][3]))
    gens: dict[int, str] = {}
    t0 = time.time()
    for b in range(0, len(order), batch_size):
        idx = order[b : b + batch_size]
        prompts = [jobs[i][3] for i in idx]
        enc = tok(prompts, return_tensors="pt", padding=True).to(device)
        out = model.generate(
            **enc,
            max_new_tokens=max_new,
            do_sample=False,
            pad_token_id=tok.pad_token_id,
        )
        cut = enc["input_ids"].shape[1]
        for i, row in zip(idx, out):
            gens[i] = tok.decode(row[cut:], skip_special_tokens=True)
        if (b // batch_size) % 50 == 0:
            print(
                f"    {name}: {min(b + batch_size, len(order)):,}/{len(order):,} generations [{time.time()-t0:.0f}s]",
                flush=True,
            )
    tally = {}
    by_group: dict = {}
    samples = []
    for i, (it, task, kind, prompt) in enumerate(jobs):
        gen = gens[i]
        ok = score(task, it, gen)
        t = tally.setdefault(task, {"trained": [0, 0], "probe": [0, 0]})
        t[kind][0] += ok
        t[kind][1] += 1
        gtally = by_group.setdefault(it.get("group") or "all", {})
        gt = gtally.setdefault(task, {"trained": [0, 0], "probe": [0, 0]})
        gt[kind][0] += ok
        gt[kind][1] += 1
        if len(samples) < 40 and (i % 97 == 0):
            samples.append(
                {
                    "species": it["species"],
                    "group": it.get("group", ""),
                    "task": task,
                    "frame": kind,
                    "prompt": prompt[-120:],
                    "gen": gen[:160],
                    "ok": ok,
                }
            )
    del model
    torch.cuda.empty_cache()

    def _fold(tal: dict) -> dict:
        res = {
            task: {
                k: {"correct": v[0], "n": v[1], "acc": round(v[0] / max(1, v[1]), 3)}
                for k, v in t.items()
            }
            for task, t in tal.items()
        }
        for task in res:
            res[task]["gap"] = round(
                res[task]["trained"]["acc"] - res[task]["probe"]["acc"], 3
            )
        return res

    res = _fold(tally)
    return {
        "model": name,
        "path": path,
        "results": res,
        "by_group": {g: _fold(t) for g, t in by_group.items()},
        "samples": samples,
        "seconds": round(time.time() - t0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True, help="name=DIR[,name=DIR...]")
    ap.add_argument(
        "--n",
        type=int,
        default=None,
        help="cap (default 200; all with --target-species)",
    )
    ap.add_argument("--seed", type=int, default=20260824)
    ap.add_argument("--max-new", type=int, default=60)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument(
        "--target-species",
        default="",
        help="synth_species.json (or a JSON list): score these species, grouped",
    )
    ap.add_argument(
        "--group",
        default="all",
        help="with --target-species: T_R, T_L, T_RL, C or all",
    )
    ap.add_argument(
        "--probe-set",
        action="store_true",
        help="score the reference-only PROBE species instead of seen species",
    )
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/probe_recall.json"))
    args = ap.parse_args()
    probe = set(json.load(open(PROBE_JSON))["species"])
    # Facts are built with NO exclusion (everything trains). `--probe-set`
    # scores the low-exposure probe species; otherwise the sample avoids them
    # so the two numbers are disjoint populations. `--target-species` scores
    # the synth pilot's groups instead (all of a group unless --n caps it).
    only, groups = None, None
    if args.target_species:
        data = json.load(open(os.path.expanduser(args.target_species)))
        if isinstance(data, dict) and "groups" in data:
            groups = {sp: g for g, v in data["groups"].items() for sp in v}
            only = (
                set(groups) if args.group == "all" else set(data["groups"][args.group])
            )
        else:
            only = set(data)
    n = args.n if args.n is not None else (0 if only else 200)
    items = build_items(0, args.seed, exclude=set(), only=only, groups=groups)
    if only is None:
        if args.probe_set:
            items = [it for it in items if it["species"] in probe]
        else:
            items = [it for it in items if it["species"] not in probe]
    if n:
        items = items[:n]
    print(
        f"{len(items)} species x {len(items[0]['prompts']) if items else 0} tasks x 2 frames",
        flush=True,
    )
    device = "cuda" if torch.cuda.is_available() else "cpu"
    report = {
        "n": len(items),
        "seed": args.seed,
        "probe_set": args.probe_set,
        "target_species": args.target_species,
        "group": args.group if args.target_species else "",
        "models": [],
    }
    for spec in args.models.split(","):
        name, path = spec.split("=", 1)
        r = run_model(
            name, os.path.expanduser(path), items, args.max_new, device, args.batch_size
        )
        report["models"].append(r)
        print(f"\n{name}:")
        if groups:
            for g, res in sorted(r["by_group"].items()):
                line = "  ".join(
                    f"{t} {v['probe']['acc']:.3f}" for t, v in sorted(res.items())
                )
                print(f"  [{g}] probe-frame acc: {line}")
        for task, v in r["results"].items():
            print(
                f"  {task:15s} trained {v['trained']['acc']:.3f}  probe {v['probe']['acc']:.3f}  gap {v['gap']:+.3f}   (n={v['trained']['n']})"
            )
    json.dump(report, open(args.out, "w"), indent=1)
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
