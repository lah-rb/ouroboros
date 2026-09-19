#!/usr/bin/env python3
"""Single-key backward recall: formula -> species, structure -> species.

Every backward number the runs have produced so far carried a MULTI-NUMBER key
(four bands, a seeker peak list, LIBS lines). The synth pilot trained backward
identity families — 67 formula-keyed and 71 structure-keyed framings — but the
probe suite never asked for them. This is that probe: if a single key of the
same shape as a name reverses, the asymmetry is about reading and matching a
set of noisy numbers at 1B scale; if it also sits near zero, it is the
reversal curse proper.

Per species: a forward control (species -> formula, the v4 probe frame) and
two backward tasks, each with a TRAINED wording (a synth-bank backward template)
and an unseen PROBE wording. Scoring: lenient = the species name appears in the
generation; strict = the first line of the generation IS the species name.
Groups: the synth pilot's T_R / T_L / T_RL / C plus the 97 untouched probe
species (U).

  ./.venv/bin/python probe_backward_identity.py --models base=~/models/OLMo-2-0425-1B,stage1=... --device cuda:0
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

from facts import build_facts  # noqa: E402
from templates import PROBE_FRAMES, fields  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

TASKS = {
    # forward control (same frame the v4 probes score)
    "species_to_formula": {
        "probe": lambda f: PROBE_FRAMES["formula"].prompt.format(**f),
    },
    # backward, single key: the formula string
    "formula_to_species": {
        "trained": lambda f: f"Which IMA-approved mineral has the ideal formula {f['formula']}?",
        "probe": lambda f: f"Reference entry. Ideal formula: {f['formula']}. Species:",
    },
    # backward, single structured key: system + cell
    "structure_to_species": {
        "trained": lambda f: f"Unit cell {f['cell']}, {f['system']} system:",
        "probe": lambda f: (
            f"Reference entry. Crystal system: {f['system']}; unit cell {f['cell']}. Species:"
        ),
    },
}


def load_groups() -> dict[str, str]:
    groups: dict[str, str] = {}
    sp = json.load(open(os.path.join(HERE, "synth_species.json")))
    for g, names in (sp.get("groups") or {}).items():
        for n in names:
            groups[n] = g
    ps = os.path.join(HERE, "probe_species.json")
    if os.path.exists(ps):
        d = json.load(open(ps))
        names = d if isinstance(d, list) else d.get("species") or d.get("names") or []
        for n in names:
            groups.setdefault(n if isinstance(n, str) else n.get("species", ""), "U")
    return groups


def build_items(groups: dict[str, str]) -> list[dict]:
    facts = build_facts(set())
    fo_by = {f.species: f for f in facts if f.kind == "formula" and isinstance(f.species, str)}
    st_by = {
        f.species: f
        for f in facts
        if f.kind == "structure"
        and isinstance(f.species, str)
        and f.payload.get("crystal_system")
    }
    items = []
    for sp, g in sorted(groups.items()):
        fo = fo_by.get(sp)
        if not fo or not fo.formula:
            continue
        ff = fields(fo)
        prompts = {
            "species_to_formula": {"probe": TASKS["species_to_formula"]["probe"](ff)},
            "formula_to_species": {
                k: fn(ff) for k, fn in TASKS["formula_to_species"].items()
            },
        }
        st = st_by.get(sp)
        if st:
            sf = fields(st)
            if sf.get("cell") and sf.get("system"):
                prompts["structure_to_species"] = {
                    k: fn(sf) for k, fn in TASKS["structure_to_species"].items()
                }
        items.append({"species": sp, "group": g, "formula": fo.formula, "prompts": prompts})
    return items


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())


def score(task: str, item: dict, gen: str) -> tuple[bool, bool]:
    """(lenient, strict)."""
    g = gen.strip()
    if task == "species_to_formula":
        ok = _norm(item["formula"]) in _norm(g)
        return ok, ok
    name = item["species"].lower()
    lenient = name in g.lower()
    first = re.split(r"[\n.;,(]", g, maxsplit=1)[0].strip().lower()
    strict = first == name or first.rstrip(".") == name
    return lenient, strict


def run_model(name: str, path: str, items: list[dict], device: str, max_new: int, batch: int) -> dict:
    tok = AutoTokenizer.from_pretrained(path)
    tok.padding_side = "left"
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16).to(device).eval()
    jobs = [
        (it, task, kind, prompt)
        for it in items
        for task, pr in it["prompts"].items()
        for kind, prompt in pr.items()
    ]
    order = sorted(range(len(jobs)), key=lambda i: len(jobs[i][3]))
    gens: dict[int, str] = {}
    t0 = time.time()
    with torch.no_grad():
        for b in range(0, len(order), batch):
            idx = order[b : b + batch]
            enc = tok([jobs[i][3] for i in idx], return_tensors="pt", padding=True).to(device)
            out = model.generate(
                **enc, max_new_tokens=max_new, do_sample=False, pad_token_id=tok.pad_token_id
            )
            cut = enc["input_ids"].shape[1]
            for i, row in zip(idx, out):
                gens[i] = tok.decode(row[cut:], skip_special_tokens=True)
            if (b // batch) % 40 == 0:
                print(f"    {name}: {min(b + batch, len(order)):,}/{len(order):,} [{time.time() - t0:.0f}s]", flush=True)
    tally: dict = {}
    samples = []
    for i, (it, task, kind, prompt) in enumerate(jobs):
        lenient, strict = score(task, it, gens[i])
        for grp in ("all", it["group"]):
            t = tally.setdefault(grp, {}).setdefault(task, {}).setdefault(kind, [0, 0, 0])
            t[0] += lenient
            t[1] += strict
            t[2] += 1
        if i % 173 == 0 and len(samples) < 60:
            samples.append({"species": it["species"], "group": it["group"], "task": task, "frame": kind, "prompt": prompt[-100:], "gen": gens[i][:100], "lenient": lenient, "strict": strict})
    del model
    torch.cuda.empty_cache()
    res = {
        grp: {
            task: {kind: {"lenient": round(v[0] / max(1, v[2]), 3), "strict": round(v[1] / max(1, v[2]), 3), "n": v[2]} for kind, v in kinds.items()}
            for task, kinds in tasks.items()
        }
        for grp, tasks in tally.items()
    }
    return {"model": name, "path": path, "results": res, "samples": samples, "seconds": round(time.time() - t0)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True, help="name=DIR[,name=DIR...]")
    ap.add_argument("--device", default="cuda:0")
    # 60, not 12: formulas run past 12 tokens (Mg3Al2(SiO4)3 is 11), so the
    # forward control under-read on 2026-09-19; species names are ~5 tokens
    ap.add_argument("--max-new", type=int, default=60)
    ap.add_argument("--batch", type=int, default=48)
    ap.add_argument("--limit", type=int, default=0, help="cap species per group (0 = all)")
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/analysis/backward_identity/results.json"))
    args = ap.parse_args()
    groups = load_groups()
    items = build_items(groups)
    if args.limit:
        kept: list[dict] = []
        seen: dict[str, int] = {}
        for it in items:
            if seen.get(it["group"], 0) < args.limit:
                kept.append(it)
                seen[it["group"]] = seen.get(it["group"], 0) + 1
        items = kept
    print(f"{len(items)} species | groups {json.dumps({g: sum(1 for i in items if i['group'] == g) for g in sorted({i['group'] for i in items})})} | structure prompts {sum(1 for i in items if 'structure_to_species' in i['prompts'])}", flush=True)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    all_res = []
    for spec in args.models.split(","):
        name, path = spec.split("=", 1)
        path = os.path.expanduser(path)
        print(f"== {name}: {path}", flush=True)
        r = run_model(name, path, items, args.device, args.max_new, args.batch)
        all_res.append(r)
        a = r["results"]["all"]
        line = " | ".join(
            f"{task}[{kind}] {v['lenient']:.3f}/{v['strict']:.3f}"
            for task, kinds in a.items()
            for kind, v in kinds.items()
        )
        print(f"   {name}: {line}  ({r['seconds']}s)", flush=True)
        json.dump(all_res, open(args.out, "w"), indent=1)
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
