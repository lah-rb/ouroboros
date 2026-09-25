#!/usr/bin/env python3
"""The v3b anneal (PROCEDURE §22l; rock venv).

WHY. §22k trained real-like band variation from the stage-1 endpoint and bands → name
collapsed to one answer ("Sulphur"), 0.00 even on its own training prompts, while every
other pair trained. Two readings fit:
- an optimization plateau (models repeat one output while attention learns slowly;
  Gopalani & Hu 2025, Hoffmann et al. 2024);
- ambiguity (a perfect lookup names only 39 % of those draws).

The literature favours clean first and noise raised in steps (Wei et al. 2021), with the
clean data kept in the mix and the learning rate re-warmed (Ibrahim et al. 2024). v3b
already reads bands at 0.78 on exact values. This arm anneals it; it does not start over.

WHAT (per trained species, v3b's own format: the strongest four, intensity-ranked,
<top>/<next>, digits, jittered lines, resolution field):
- bands → name, clean: 12 fresh position-jitter draws (§22h model, new seed namespace):
  the key, replayed.
- bands → name, variation: 12 draws at each of three severity tiers, ⅓, ⅔ and full
  of the §22k fit (synth_variance.vary_spectrum; severity u = tier × Beta(0.9, 2.1)).
  The strongest four of each draw are kept by intensity, grid-rounded and
  de-duplicated. The tiers are MIXED, not ordered: the packer and the trainer both
  shuffle, and v3b itself is the clean first stage.
- bands → reference (option 4, the denoise half of a two-step identification):
  5 draws per tier. The measured spectrum is shown, and the blank is the species'
  canonical strongest four (corpus_xml.denoise_record); step two is bands → name on
  the reconstructed bands.
- bands + lines → name 4 clean, formula → name 4, name → formula 8 (replay; §22k's
  formula recovery).
- 20 % of the §22 full-record rows (stable hash of ex_id) for forward fill; every
  bundle; every val row.

  ./.venv/bin/python corpus_xml_anneal.py --out ~/corpora/rock-olmo-training/v6/stage2l
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import corpus_xml as cx  # noqa: E402
import corpus_xml_granular as cg  # noqa: E402
import corpus_xml_resolution as cr  # noqa: E402
import corpus_xml_variation as cv  # noqa: E402
import synth_variance as sv  # noqa: E402
from fim_transform import fim_wrap  # noqa: E402

TIERS = ((1, 1 / 3), (2, 2 / 3), (3, 1.0))
ROWS = {"clean": 12, "variation_per_tier": 12, "lines_clean": 4, "reference_per_tier": 5, "formula>name": 4, "name>formula": 8}
KEPT_SHARE = 0.20
REFERENCE_KIND = "g:bands>reference"


def severity_draw(rec: cx.XmlRecord, peaks, pool, scale: float, *key) -> tuple[list[int], dict]:
    """(the draw's strongest four, intensity-ranked, grid-rounded, de-duplicated; meta)."""
    rng = sv.rng_for("s2l", rec.species, *key)
    inst = sv.sample_raman(rng)
    u = scale * rng.betavariate(sv.SAMPLE_SEVERITY_ALPHA, sv.SAMPLE_SEVERITY_BETA)
    drawn = sv.vary_spectrum(rng, list(peaks), inst, pool, severity=u)
    grid = cr.GRID[inst.klass]
    bands: list[int] = []
    for p, _ in sorted(drawn, key=lambda x: -x[1]):
        q = cr.quantize(p, grid)
        if q not in bands:
            bands.append(q)
        if len(bands) == 4:
            break
    return bands, {"instrument": inst.klass, "grid": grid, "offset": inst.offset_cm1, "severity": round(u, 3)}


def _row(rec, kind, variant, ex_id, text, completion, k, val, meta, shown) -> dict:
    pre, suf = text.split(cx.BLANK)
    order = cx.ORDERS[k % 2]
    return {"ex_id": ex_id, "species": rec.species, "kind": kind, "variant": variant, "perm": "raman_first", "order": order,
            "prompt": fim_wrap(pre, "", suf, order), "completion": completion, "val": val, **meta, "bands_shown": shown}


def variation_rows(rec, peaks, pool, n: int, *, val: bool, ns: str) -> list[dict]:
    kind = "g:bands>name"
    rows = []
    for tier, scale in TIERS:
        for k in range(n):
            bands, meta = severity_draw(rec, peaks, pool, scale, ns, kind, tier, k)
            text = cx.stripped_record(dataclasses.replace(rec, bands=bands), {"bands"}, "name", raman_resolution=meta["grid"], digits=True)
            rows.append(_row(rec, kind, f"stripped·variation·tier{tier}", f"xmll:{rec.species}:{kind}:t{tier}:{ns}{k}", text,
                             rec.species, k, val, {**meta, "tier": tier}, bands))
    return rows


def reference_rows(rec, peaks, pool, n: int, *, val: bool, ns: str) -> list[dict]:
    rows = []
    for tier, scale in TIERS:
        for k in range(n):
            bands, meta = severity_draw(rec, peaks, pool, scale, ns, REFERENCE_KIND, tier, k)
            text = cx.denoise_record(dataclasses.replace(rec, bands=bands), raman_resolution=meta["grid"], digits=True)
            rows.append(_row(rec, REFERENCE_KIND, f"denoise·tier{tier}", f"xmll:{rec.species}:{REFERENCE_KIND}:t{tier}:{ns}{k}",
                             text, cx.raman_inner(rec.bands, digits=True), k, val, {**meta, "tier": tier}, bands))
    return rows


def rows_for(rec: cx.XmlRecord, peaks, pool, *, val: bool) -> list[dict]:
    ns = "av" if val else "a"
    n = (lambda key: 2) if val else (lambda key: ROWS[key])
    out = cr.jittered_rows(rec, ("bands",), "name", n("clean"), val=val, ns=ns, digits=True, jitter_libs=True)
    out += variation_rows(rec, peaks, pool, n("variation_per_tier"), val=val, ns=ns)
    out += reference_rows(rec, peaks, pool, n("reference_per_tier"), val=val, ns=ns)
    out += cr.jittered_rows(rec, ("bands", "lines"), "name", n("lines_clean"), val=val, ns=ns, digits=True, jitter_libs=True)
    out += cg.granular_rows(rec, val=val, pairs=((("formula",), "name"),), exposures=0 if val else ROWS["formula>name"])
    out += cg.granular_rows(rec, val=val, pairs=((("name",), "formula"),), exposures=0 if val else ROWS["name>formula"])
    return out


def kept_sample(ex_id: str) -> bool:
    return int(hashlib.sha256(ex_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < KEPT_SHARE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2"))
    ap.add_argument("--out", default=os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2l"))
    args = ap.parse_args()
    from package import load_tokenizer

    t0 = time.time()
    tok = load_tokenizer()
    canon, _, pool = cv.load_spectra()
    src_docs = os.path.join(args.src, "docs")
    recs = json.load(open(os.path.join(src_docs, "xml_records.json")))
    trained = [cx.XmlRecord(**r) for r in recs["trained"]]
    val_sp = {r.species for r in trained if cx.is_val(f"xml:{r.species}")}
    kept = [r for r in map(json.loads, open(os.path.join(src_docs, "xml_fim.jsonl"), encoding="utf-8"))
            if r["kind"] != "identity" and (r.get("val") or kept_sample(r["ex_id"]))]
    new_train, new_val = [], []
    for rec in trained:
        (new_val if rec.species in val_sp else new_train).extend(rows_for(rec, canon[rec.species], pool, val=rec.species in val_sp))
    P = tok([r["prompt"] for r in new_train], add_special_tokens=False)["input_ids"]
    C = tok([r["completion"] for r in new_train], add_special_tokens=False)["input_ids"]
    tokens_by = collections.Counter()
    for r, p, c in zip(new_train, P, C):
        tokens_by[f"{r['kind']} {r['variant']}"] += len(p) + len(c) + 1
    out_docs = os.path.join(args.out, "docs")
    os.makedirs(out_docs, exist_ok=True)
    with open(os.path.join(out_docs, "xml_fim.jsonl"), "w", encoding="utf-8") as fh:
        for r in kept + new_val + new_train:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    for f in ("xml_plain.jsonl", "xml_records.json"):
        shutil.copyfile(os.path.join(src_docs, f), os.path.join(out_docs, f))
    tier_stats = collections.defaultdict(lambda: [0.0, 0.0, 0])
    top4 = {x.species: x.bands for x in trained}
    for r in new_train:
        if r.get("tier") and r["kind"] == "g:bands>name":
            s = tier_stats[r["tier"]]
            s[0] += sum(cv._near(b, r["bands_shown"]) for b in top4[r["species"]]) / 4
            s[1] += r["severity"]
            s[2] += 1
    kept_train = [r for r in kept if not r.get("val")]
    man = {
        "arm": "v3b anneal (§22l)", "init": "v3_stage2b/final_fp32", "tiers": TIERS, "rows_per_species": ROWS, "kept_share": KEPT_SHARE,
        "train_rows_new": len(new_train), "train_tokens_new": sum(tokens_by.values()), "val_rows_new": len(new_val),
        "kept_rows": {"train": len(kept_train), "val": len(kept) - len(kept_train)},
        "rows_by_kind_variant": dict(sorted(collections.Counter(f"{r['kind']} {r['variant']}" for r in new_train).items())),
        "train_tokens_by_kind_variant": dict(sorted(tokens_by.items())),
        "variation_by_tier": {t: {"canonical_top4_shown": round(s[0] / s[2], 3), "mean_severity": round(s[1] / s[2], 3)} for t, s in sorted(tier_stats.items())},
        "species_trained": len(trained) - len(val_sp),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }  # fmt: skip
    json.dump(man, open(os.path.join(args.out, "docs_manifest.json"), "w"), indent=1)
    print(json.dumps(man, indent=1))
    print(f"[{time.time() - t0:.0f}s] new train rows {len(new_train):,} = {man['train_tokens_new']:,} tokens; kept {len(kept_train):,} train rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
