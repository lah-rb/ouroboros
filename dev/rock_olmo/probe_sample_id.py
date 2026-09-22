#!/usr/bin/env python3
"""Ablation: how much of Raman band recall rides on the RRUFF sample ID in the prompt?

The v4 probe frame for bands reads "Reference Raman peak positions for Gypsum (CaSO4·2H2O),
peak-picked from the RRUFF spectrum (R040029):". v2's stage 2 trained frames carrying the
same ID next to the same bands ("Recorded in RRUFF as R040029, Gypsum …"), so the ID can act
as a memorised key. Three versions of the frame, identical otherwise:

  with_id     … peak-picked from the RRUFF spectrum (R040029):
  no_id       … peak-picked from the RRUFF spectrum:
  bare        Reference Raman peak positions for Gypsum (CaSO4·2H2O):

Same 200 seen species as probe_recall (seed 20260824, probe species excluded); scored by
probe_scoring.score("bands") (>= 2 of the first three bands by position within ±10 cm-1).

  ./.venv/bin/python probe_sample_id.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

from facts import build_facts  # noqa: E402
from probe_recall import PROBE_JSON  # noqa: E402
from probe_scoring import score  # noqa: E402
from templates import fields  # noqa: E402

M = os.path.expanduser("~/models/olmo2-1b-spectra-full")
MODELS = {"v2 stage 2": f"{M}/stage2/final", "v3 stage 1": f"{M}/v3_stage1/final", "v3 stage 2": f"{M}/v3_stage2/final"}


def main() -> int:
    import random

    probe = set(json.load(open(PROBE_JSON))["species"])
    facts = build_facts(set())
    by: dict = {}
    for f in facts:
        if isinstance(f.species, str) and not (f.kind == "raman_bands" and not f.canonical):
            by.setdefault(f.kind, {})[f.species] = f
    species = sorted(s for s in by["raman_bands"] if s in by.get("formula", {}))
    random.Random(20260824).shuffle(species)
    species = [s for s in species if s not in probe][:200]
    items = []
    for sp in species:
        rf = fields(by["raman_bands"][sp])
        base = f"Reference {rf['tech']} peak positions for {sp} ({rf['formula']})"
        items.append(
            {
                "species": sp,
                "bands": by["raman_bands"][sp].payload["bands_cm1"][:3],
                "sample": rf["sample"],
                "with_id": f"{base}, {rf['how']}{rf['sample_paren']}:",
                "no_id": f"{base}, {rf['how']}:",
                "bare": f"{base}:",
            }
        )
    n_id = sum(1 for it in items if it["sample"])
    out = {"n": len(items), "with_sample_id": n_id, "models": {}}
    print(f"{len(items)} species ({n_id} have a sample ID)", flush=True)
    for name, path in MODELS.items():
        tok = AutoTokenizer.from_pretrained(path)
        tok.padding_side = "left"
        if tok.pad_token_id is None:
            tok.pad_token = tok.eos_token
        model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16).to("cuda:0").eval()
        res = {}
        for variant in ("with_id", "no_id", "bare"):
            prompts = [it[variant] for it in items]
            gens = []
            with torch.no_grad():
                for b in range(0, len(prompts), 32):
                    enc = tok(prompts[b : b + 32], return_tensors="pt", padding=True).to("cuda:0")
                    g = model.generate(**enc, max_new_tokens=60, do_sample=False, pad_token_id=tok.pad_token_id)
                    gens += [tok.decode(r[enc["input_ids"].shape[1] :], skip_special_tokens=True) for r in g]
            hits = [score("bands", it, g) for it, g in zip(items, gens)]
            res[variant] = {"acc": round(sum(hits) / len(hits), 3), "gypsum": next((g[:60] for it, g in zip(items, gens) if it["species"] == "Gypsum"), None)}
        out["models"][name] = res
        print(f"{name:11s} with_id {res['with_id']['acc']:.3f} | no_id {res['no_id']['acc']:.3f} | bare {res['bare']['acc']:.3f}", flush=True)
        del model
        torch.cuda.empty_cache()
    os.makedirs(os.path.expanduser("~/tmp/analysis/v3/chain_probe"), exist_ok=True)
    json.dump(out, open(os.path.expanduser("~/tmp/analysis/v3/chain_probe/sample_id_ablation.json"), "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
