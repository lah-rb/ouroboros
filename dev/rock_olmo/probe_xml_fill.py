#!/usr/bin/env python3
"""Fill probe for the §22 XML records (rock venv): can the model fill each
blank kind of a record through the FIM sentinels?

Items are rebuilt from corpus_xml (xml_records.json, written by the render)
so train and probe records are byte-identical. Every species gets, per
variant (full, raman_only) and blank kind, a prompt at the TRAINED
permutation 0 and at the never-rendered PROBE permutation, in PSM and SPM
order; plus one plain left-to-right control (`<mineral species="X" formula="`).

Groups: T = trained species (a seeded sample), V = the 1 % val species (facts
rendered only into the val sets), U = the untouched probe species; with
--target-species the synth pilot's groups label their members instead of T.

Scoring (probe_scoring rules where one exists):
  species   lenient: the name appears; strict: the first attribute value IS the name
  identity  lenient: the name appears; strict: species="…" in the generation equals it
  formula   normalised formula string present
  raman     ≥ 2 of the 3 strongest bands within ±10 cm-1 (v2 scored the first-listed 3)
  libs      ≥ 2 of the 3 strongest lines within ±0.2 nm
  top       first number within ±10 of the strongest band
  system    the crystal-system word present

  ./.venv/bin/python probe_xml_fill.py --records ~/corpora/rock-olmo-training/v6/stage2/docs/xml_records.json \\
      --models base=~/models/OLMo-2-0425-1B,s1=... --n 200 --device cuda:0
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

import corpus_xml as cx  # noqa: E402
from probe_scoring import numbers, score  # noqa: E402

PERMS = {"trained": 0, "probe": cx.PROBE_PERM}
_SPECIES_ATTR = re.compile(r'species="([^"]*)"')


def load_records(path: str) -> tuple[list[cx.XmlRecord], list[cx.XmlRecord]]:
    d = json.load(open(path))
    return [cx.XmlRecord(**r) for r in d["trained"]], [cx.XmlRecord(**r) for r in d["untouched"]]


def build_jobs(items: list[dict], orders: tuple[str, ...], variants: tuple[str, ...]) -> list[dict]:
    jobs = []
    for it in items:
        rec = it["rec"]
        for variant in variants:
            for kind in cx.kinds_for(rec, variant):
                for plabel, perm in PERMS.items():
                    for order in orders:
                        ex = cx.fim_example(rec, kind, variant, perm, order)
                        jobs.append({**it, "kind": kind, "variant": variant, "perm": plabel, "order": order, "prompt": ex["prompt"], "answer": ex["completion"]})
        jobs.append({**it, "kind": "plain_formula", "variant": "full", "perm": "trained", "order": "ltr", "prompt": f'<mineral species="{cx._esc(rec.species)}" formula="', "answer": rec.formula})
    return jobs


def judge(job: dict, gen: str) -> tuple[bool, bool]:
    """(lenient, strict) — strict == lenient for the numeric kinds."""
    rec: cx.XmlRecord = job["rec"]
    kind = job["kind"]
    g = gen.strip()
    name = rec.species.lower()
    if kind == "species":
        first = re.split(r'["\n<]', g, maxsplit=1)[0].strip().lower()
        return name in g.lower(), first == name
    if kind == "identity":
        m = _SPECIES_ATTR.search(g)
        return name in g.lower(), bool(m) and m.group(1).strip().lower() == name
    if kind in ("formula", "plain_formula"):
        ok = score("formula", {"formula": rec.formula}, g)
        return ok, ok
    if kind == "raman":
        ok = score("bands", {"bands": [float(b) for b in rec.bands[:3]]}, g)
        return ok, ok
    if kind == "libs":
        ok = score("libs_lines", {"libs_top3": rec.libs[:3]}, g)
        return ok, ok
    if kind == "top":
        nums = numbers(g, 1)
        ok = bool(nums) and abs(nums[0] - rec.bands[0]) <= 10.0
        return ok, ok
    if kind == "system":
        ok = bool(rec.system) and rec.system in g.lower()
        return ok, ok
    return False, False


def _fold(rows: list[tuple[dict, bool, bool]]) -> dict:
    n = len(rows)
    return {"n": n, "lenient": round(sum(r[1] for r in rows) / max(1, n), 3), "strict": round(sum(r[2] for r in rows) / max(1, n), 3)}


@torch.no_grad()
def run_model(name: str, path: str, jobs: list[dict], device: str, batch: int, max_new: int) -> dict:
    try:
        tok = AutoTokenizer.from_pretrained(path)
    except (OSError, ValueError):  # a Trainer checkpoint dir carries no tokenizer
        tok = AutoTokenizer.from_pretrained(os.path.expanduser("~/models/OLMo-2-0425-1B"))
    tok.padding_side = "left"
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16).to(device).eval()
    order = sorted(range(len(jobs)), key=lambda i: len(jobs[i]["prompt"]))
    gens: dict[int, str] = {}
    t0 = time.time()
    for b in range(0, len(order), batch):
        idx = order[b : b + batch]
        enc = tok([jobs[i]["prompt"] for i in idx], return_tensors="pt", padding=True).to(device)
        out = model.generate(**enc, max_new_tokens=max_new, do_sample=False, pad_token_id=tok.pad_token_id)
        cut = enc["input_ids"].shape[1]
        for i, row in zip(idx, out):
            gens[i] = tok.decode(row[cut:], skip_special_tokens=True)
        if (b // batch) % 50 == 0:
            print(f"    {name}: {min(b + batch, len(order)):,}/{len(order):,} [{time.time()-t0:.0f}s]", flush=True)
    rows = [(j, *judge(j, gens[i])) for i, j in enumerate(jobs)]
    del model
    torch.cuda.empty_cache()

    def by(keyfn) -> dict:
        groups: dict = {}
        for r in rows:
            groups.setdefault(keyfn(r[0]), []).append(r)
        return {k: _fold(v) for k, v in sorted(groups.items())}

    res = {
        "model": name,
        "path": path,
        "seconds": round(time.time() - t0),
        "by_kind_group": by(lambda j: f"{j['kind']}|{j['group']}"),
        "by_kind_group_perm": by(lambda j: f"{j['kind']}|{j['group']}|{j['perm']}"),
        "by_kind_group_variant": by(lambda j: f"{j['kind']}|{j['group']}|{j['variant']}"),
        "by_kind_group_order": by(lambda j: f"{j['kind']}|{j['group']}|{j['order']}"),
        "samples": [
            {"species": j["species"], "group": j["group"], "kind": j["kind"], "variant": j["variant"], "perm": j["perm"], "order": j["order"], "prompt_tail": j["prompt"][-90:], "answer": j["answer"][:60], "gen": gens[i][:90], "lenient": le, "strict": st}
            for i, (j, le, st) in enumerate(rows)
            if i % 211 == 0
        ][:60],
    }
    # the collapse metric: trained-perm − probe-perm gap per kind (all groups pooled over T)
    gaps = {}
    for kind in cx.KINDS_BY_VARIANT["full"]:
        tr = res["by_kind_group_perm"].get(f"{kind}|T|trained", {}).get("lenient")
        pr = res["by_kind_group_perm"].get(f"{kind}|T|probe", {}).get("lenient")
        if tr is not None and pr is not None:
            gaps[kind] = round(tr - pr, 3)
    res["perm_gap_T"] = gaps
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", default=os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2/docs/xml_records.json"))
    ap.add_argument("--models", required=True, help="name=DIR[,name=DIR...]")
    ap.add_argument("--n", type=int, default=200, help="trained species sampled (0 = all)")
    ap.add_argument("--seed", type=int, default=20260919)
    ap.add_argument("--target-species", default="", help="synth_species.json: label its groups")
    ap.add_argument("--orders", default="psm,spm")
    ap.add_argument("--variants", default="full,raman_only")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--batch", type=int, default=24)
    ap.add_argument("--max-new", type=int, default=60)
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/analysis/xml_fill/results.json"))
    args = ap.parse_args()
    trained, untouched = load_records(args.records)
    val_names = {r.species for r in trained if cx.is_val(f"xml:{r.species}")}
    groups: dict[str, str] = {}
    if args.target_species:
        data = json.load(open(os.path.expanduser(args.target_species)))
        for g, names in (data.get("groups") or {}).items():
            for nme in names:
                groups[nme] = g
    pool = [r for r in trained if r.species not in val_names]
    random.Random(args.seed).shuffle(pool)
    sample = pool[: args.n] if args.n else pool
    items = (
        [{"rec": r, "species": r.species, "group": groups.get(r.species, "T")} for r in sample]
        + [{"rec": r, "species": r.species, "group": "V"} for r in trained if r.species in val_names]
        + [{"rec": r, "species": r.species, "group": "U"} for r in untouched]
    )
    jobs = build_jobs(items, tuple(args.orders.split(",")), tuple(args.variants.split(",")))
    print(f"{len(items)} species ({len(sample)} T, {len(val_names)} V, {len(untouched)} U) -> {len(jobs):,} prompts", flush=True)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    report = {"n_species": len(items), "seed": args.seed, "records": args.records, "models": []}
    for spec in args.models.split(","):
        name, path = spec.split("=", 1)
        r = run_model(name, os.path.expanduser(path), jobs, args.device, args.batch, args.max_new)
        report["models"].append(r)
        print(f"\n{name} ({r['seconds']}s):")
        for kind in list(cx.KINDS_BY_VARIANT["full"]) + ["plain_formula"]:
            cells = []
            for g in ("T", "V", "U"):
                v = r["by_kind_group"].get(f"{kind}|{g}")
                if v:
                    cells.append(f"{g} {v['lenient']:.3f}/{v['strict']:.3f}")
            print(f"  {kind:14s} {'  '.join(cells)}   gap(T) {r['perm_gap_T'].get(kind, float('nan')):+.3f}")
        json.dump(report, open(args.out, "w"), indent=1)
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
