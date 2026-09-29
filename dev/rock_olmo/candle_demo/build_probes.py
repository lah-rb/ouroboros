#!/usr/bin/env python3
"""Canned probes for the candle demo (rock venv).

Writes static/probes.json: a fixed set of forward and backward probes on well-known trained
species, byte-identical to the §22l probe prompts (corpus_xml.stripped_record /
render_record + fim_wrap, PSM), with the token ids the probes feed and the bf16 reference
generations of both models (probe_chains.generate: HF transformers, greedy).

Also writes validation.json: the §22l T group (60 seeded species) on the three demo
pairs, with v3l's bf16 generations, so the quantized candle build can be checked
against the published numbers on identical prompts (`spectra-demo run`).

  cd dev/rock_olmo && ./.venv/bin/python candle_demo/build_probes.py
"""

from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from transformers import AutoTokenizer  # noqa: E402

import corpus_xml as cx  # noqa: E402
import probe_chains as pc  # noqa: E402
import probe_pairs as pp  # noqa: E402
from fim_transform import fim_wrap  # noqa: E402
from probe_scoring import score as score_fill  # noqa: E402

MODELS = {
    "base": os.path.expanduser("~/models/OLMo-2-0425-1B"),
    "v3l": os.path.expanduser("~/models/olmo2-1b-spectra-full/v3_stage2l/final"),
}
# Two polymorph pairs first: the same formula, different spectra.
SPECIES = ["Calcite", "Aragonite", "Anatase", "Rutile", "Quartz", "Gypsum", "Hematite", "Baryte", "Pyrite", "Diopside"]
T_SEED = 20260922  # probe_resolution --seed default: the §22j-§22l T group

#: pair -> (direction, label, target, max_new, stop strings for the live server)
PAIRS = {
    "bands>name": ("backward", "Raman bands → name", "name", 24, ['"', "\n"]),
    "bands+lines>name": ("backward", "Raman bands + LIBS lines → name", "name", 24, ['"', "\n"]),
    "formula>name": ("backward", "Formula → name", "name", 24, ['"', "\n"]),
    "name>formula": ("forward", "Name → formula", "formula", 24, ['"', "\n"]),
    "name>bands": ("forward", "Name → strongest Raman bands", "bands", 48, ["</raman>", "\n"]),
}
BLANK_MARK = "▢"


def record_text(pair: str, g: dict, full: cx.XmlRecord) -> tuple[str, str]:
    """(prefix, suffix) of the record around the one blank, as §22l probed it."""
    rec = pp._rec(g)
    if pair == "bands>name":
        text = cx.stripped_record(rec, {"bands"}, "name", raman_resolution=1, digits=True)
    elif pair == "bands+lines>name":
        text = cx.stripped_record(rec, {"bands", "lines"}, "name", raman_resolution=1, digits=True)
    elif pair == "formula>name":
        text = cx.stripped_record(rec, {"formula"}, "name")
    elif pair == "name>formula":
        text = cx.stripped_record(rec, {"name"}, "formula")
    elif pair == "name>bands":  # probe_xml_fill: raman blank, raman_only, held-out attribute order
        pre, _, suf = cx.blank(cx.render_record(full, cx.PROBE_PERM, "raman_only"), "raman")
        return pre, suf
    else:
        raise ValueError(pair)
    pre, suf = text.split(cx.BLANK)
    return pre, suf


def judge(pair: str, gen: str, g: dict) -> tuple[str, str]:
    """(extracted answer, HIT / NEAR / MISS) with the probes' own rules."""
    target = PAIRS[pair][2]
    if target == "bands":
        ans = gen.lstrip().split("\n", 1)[0].strip()
        return ans, "HIT" if score_fill("bands", {"bands": [float(b) for b in g["bands"][:3]]}, gen) else "MISS"
    ans = pc.extract(gen, "xml", target)
    return ans, pc.score(target, ans, g)


def main() -> int:
    t0 = time.time()
    recs = {r["species"]: cx.XmlRecord(**r) for r in json.load(open(pc.RECORDS))["trained"]}
    toks = {k: AutoTokenizer.from_pretrained(p) for k, p in MODELS.items()}

    def build(species: list[str], pairs: list[str]) -> list[dict]:
        out = []
        for sp in species:
            g = pc.load_gold(sp)
            assert g["xml_group"] == "trained", (sp, g["xml_group"])
            for pair in pairs:
                direction, label, target, max_new, stop = PAIRS[pair]
                pre, suf = record_text(pair, g, recs[sp])
                prompt = fim_wrap(pre, "", suf, "psm")
                ids = toks["v3l"](prompt)["input_ids"]
                assert ids == toks["base"](prompt)["input_ids"], "base and v3l tokenizers disagree"
                gold = {"name": g["name"], "formula": g["formula"], "bands": cx.raman_inner(g["bands"])}[target]
                out.append({"id": f"{sp}:{pair}", "species": sp, "pair": pair, "direction": direction, "label": label,
                            "target": target, "record": pre + BLANK_MARK + suf, "prompt": prompt, "ids": ids,
                            "max_new": max_new, "stop": stop, "gold": gold, "gold_rec": g})
        return out

    def reference(jobs: list[dict], model: str) -> None:
        for max_new in sorted({j["max_new"] for j in jobs}):
            sub = [j for j in jobs if j["max_new"] == max_new]
            gens = pc.generate(MODELS[model], [j["prompt"] for j in sub], "cuda:0", max_new)
            for j, gen in zip(sub, gens):
                ans, status = judge(j["pair"], gen, j["gold_rec"])
                j.setdefault("reference", {})[model] = {"text": gen, "answer": ans, "status": status}

    probes = build(SPECIES, list(PAIRS))
    for m in MODELS:
        reference(probes, m)
    val = build(pp.sample_species({"T": 60, "V": 0, "U": 0}, T_SEED)["T"], ["bands>name", "name>formula", "name>bands"])
    reference(val, "v3l")

    species = [{"name": sp, "formula": recs[sp].formula, "system": recs[sp].system, "bands": recs[sp].bands,
                "lines": recs[sp].libs, "laser_nm": recs[sp].laser_nm} for sp in SPECIES]
    meta = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "models": {"base": {"label": "OLMo 2 1B base", "path": MODELS["base"]},
                   "v3l": {"label": "v3l fine-tune", "path": MODELS["v3l"]}},
        "reference_runtime": "HF transformers, bf16, CUDA, greedy",
        # PROCEDURE §22l, trained species, canonical (exact) values
        "published": {"bands>name": 0.89, "name>formula": 0.79, "name>bands": 0.921},
    }
    for path, rows in (("probes.json", probes), ("validation.json", val)):
        for r in rows:
            r.pop("gold_rec")
        doc = {"meta": meta, "species": species, "probes": rows} if path == "probes.json" else {"meta": meta, "probes": rows}
        json.dump(doc, open(os.path.join(HERE, "static" if path == "probes.json" else "", path), "w"), ensure_ascii=False, indent=1)
    for rows, name in ((probes, "canned"), (val, "validation T-60")):
        for m in MODELS:
            st = [r["reference"][m]["status"] for r in rows if m in r["reference"]]
            if st:
                by = {}
                for r in rows:
                    if m in r["reference"]:
                        by.setdefault(r["pair"], []).append(r["reference"][m]["status"] == "HIT")
                print(f"{name} {m}: " + ", ".join(f"{p} {sum(v)}/{len(v)}" for p, v in by.items()))
    print(f"[{time.time() - t0:.0f}s] wrote probes.json ({len(probes)}) and validation.json ({len(val)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
