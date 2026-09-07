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
from templates import FRAMES, PROBE_FRAMES, fields, pick_frames  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE_JSON = os.path.join(HERE, "probe_species.json")
TOL_CM1 = 10.0
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


def build_items(n: int, seed: int, exclude: set[str]) -> list[dict]:
    facts = build_facts(exclude)
    by = {}
    for f in facts:
        by.setdefault(f.kind, {})
        if isinstance(f.species, str):
            if f.kind == "raman_bands" and not f.canonical:
                continue
            by[f.kind][f.species] = f
    species = sorted(s for s in by.get("raman_bands", {}) if s in by.get("formula", {}))
    random.Random(seed).shuffle(species)
    items = []
    for sp in species[:n]:
        r, fo, st = (
            by["raman_bands"][sp],
            by["formula"][sp],
            by.get("structure", {}).get(sp),
        )
        rf = fields(r)
        items.append(
            {
                "species": sp,
                "formula": fo.formula,
                "bands": r.payload["bands_cm1"][:3],
                "system": (
                    (st.payload.get("crystal_system") or "").lower() if st else ""
                ),
                "prompts": {
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


def score(task: str, item: dict, gen: str) -> bool:
    g = gen.lower()
    if task == "formula":
        return re.sub(r"\s+", "", item["formula"].lower()) in re.sub(r"\s+", "", g)
    if task == "crystal_system":
        return bool(item["system"]) and item["system"] in g
    if task == "inverse":
        return item["species"].lower() in g
    if task == "bands":
        nums = [float(x) for x in _NUM.findall(gen)[:12]]
        hits = sum(1 for b in item["bands"] if any(abs(b - x) <= TOL_CM1 for x in nums))
        return hits >= min(2, len(item["bands"]))
    return False


@torch.no_grad()
def run_model(
    name: str, path: str, items: list[dict], max_new: int, device: str
) -> dict:
    tok = AutoTokenizer.from_pretrained(path)
    model = (
        AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16)
        .to(device)
        .eval()
    )
    tally = {}
    samples = []
    t0 = time.time()
    for it in items:
        for task, pr in it["prompts"].items():
            for kind, prompt in pr.items():
                enc = tok(prompt, return_tensors="pt").to(device)
                out = model.generate(
                    **enc,
                    max_new_tokens=max_new,
                    do_sample=False,
                    pad_token_id=tok.pad_token_id,
                )
                gen = tok.decode(
                    out[0, enc["input_ids"].shape[1] :], skip_special_tokens=True
                )
                ok = score(task, it, gen)
                t = tally.setdefault(task, {"trained": [0, 0], "probe": [0, 0]})
                t[kind][0] += ok
                t[kind][1] += 1
                if len(samples) < 24:
                    samples.append(
                        {
                            "species": it["species"],
                            "task": task,
                            "frame": kind,
                            "prompt": prompt[-120:],
                            "gen": gen[:160],
                            "ok": ok,
                        }
                    )
    del model
    torch.cuda.empty_cache()
    res = {
        task: {
            k: {"correct": v[0], "n": v[1], "acc": round(v[0] / max(1, v[1]), 3)}
            for k, v in t.items()
        }
        for task, t in tally.items()
    }
    for task in res:
        res[task]["gap"] = round(
            res[task]["trained"]["acc"] - res[task]["probe"]["acc"], 3
        )
    return {
        "model": name,
        "path": path,
        "results": res,
        "samples": samples,
        "seconds": round(time.time() - t0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True, help="name=DIR[,name=DIR...]")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20260824)
    ap.add_argument("--max-new", type=int, default=40)
    ap.add_argument(
        "--probe-set",
        action="store_true",
        help="score the reference-only PROBE species instead of seen species",
    )
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/probe_recall.json"))
    args = ap.parse_args()
    probe = set(json.load(open(PROBE_JSON))["species"])
    if args.probe_set:
        items = build_items(10**6, args.seed, exclude=set())
        items = [it for it in items if it["species"] in probe][: args.n]
    else:
        items = build_items(args.n, args.seed, exclude=probe)
    print(
        f"{len(items)} species x {len(items[0]['prompts']) if items else 0} tasks x 2 frames",
        flush=True,
    )
    device = "cuda" if torch.cuda.is_available() else "cpu"
    report = {
        "n": len(items),
        "seed": args.seed,
        "probe_set": args.probe_set,
        "models": [],
    }
    for spec in args.models.split(","):
        name, path = spec.split("=", 1)
        r = run_model(name, os.path.expanduser(path), items, args.max_new, device)
        report["models"].append(r)
        print(f"\n{name}:")
        for task, v in r["results"].items():
            print(
                f"  {task:15s} trained {v['trained']['acc']:.3f}  probe {v['probe']['acc']:.3f}  gap {v['gap']:+.3f}   (n={v['trained']['n']})"
            )
    json.dump(report, open(args.out, "w"), indent=1)
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
