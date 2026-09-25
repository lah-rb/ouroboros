#!/usr/bin/env python3
"""The representation arm (PROCEDURE §22k; rock venv).

WHY. §22j showed exposure was the binding limit: 64 position-jittered draws per species
took bands → name on fresh synthetic draws to 0.65, 78 % of what a perfect tolerant lookup
over the same four bands gets. Real re-measurements stayed at 0.12–0.14 against a
ceiling of 0.33, and the gap tracks the band SET:
- a re-measurement that keeps the reference's four-band set is named half the time;
- one that keeps only the top band, 0.16;
- one that keeps nothing, 0.

The draws only ever moved bands; real spectra lose them, gain others and re-rank them.
Measured on the 664 re-measurements outside the probe set, a canonical top-4 band is
- missing from the real spectrum 29 % of the time;
- pushed out of the top four 12 %;
and 27 % of a real top four has no canonical counterpart.

WHAT. §22j's arm (digit rendering, jittered LIBS lines, grids and the resolution field,
the same budget) with the band draw and the band list changed:
- DRAWS come from the FULL canonical spectrum (export_spectra.py) through
  synth_variance.vary_spectrum, fitted to those statistics. Each draw has a severity
  u ~ Beta(0.9, 2.1): a canonical band is lost with probability u, and
  u · 14 extra bands (positions of other species' bands) come in with intensity
  U(0, 0.4). The instrument's position error and cutoff are added as before.
- The strongest k are kept, with k ~ U{4..8} per draw, rounded to the class grid,
  de-duplicated, and written POSITION-SORTED as <band> elements
  (corpus_xml.stripped_record sorted_bands). Order no longer depends on which band
  happens to be strongest.
- Pairs and rows per species:
  - bands → name 64, bands + lines → name 16, formula → name 8 (as §22j);
  - name → formula 8 (new; stripped, no spectra).

  §22j's pair drop cost name → formula (0.79 → 0.68). The spectra → formula pairs it
  dropped are not restored: they were present in every arm that showed the
  held-out-order slot confusion (the model writing a formula into the raman blank),
  and absent from the one that did not.

Everything else is §22 stage 2, byte-identical: the kept full-record rows, the bundles and
the val species (two draws per rendered pair).

  ./.venv/bin/python corpus_xml_variation.py --out ~/corpora/rock-olmo-training/v6/stage2k
  ./.venv/bin/python corpus_xml_variation.py --calibrate      # re-check the fit, no build
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
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
import synth_variance as sv  # noqa: E402
from fim_transform import fim_wrap  # noqa: E402

SPECTRA = os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2k/docs/spectra_full.json")
PROBE_ITEMS = os.path.expanduser("~/tmp/analysis/v3/pair_probe/resolution_budget_items_frozen.json")
K_RANGE = (4, 8)
EXPOSURES = {"g:bands>name": 64, "g:bands+lines>name": 16, "g:formula>name": 8, "g:name>formula": 8}
CAL_TARGET = {"top1": 0.630, "set4": 0.173, "present": 0.714, "extra4": 0.267}


def load_spectra(path: str = SPECTRA) -> tuple[dict, dict, list[float]]:
    """(canonical {species: [(pos, rel)]}, real {species: {...}}, pool of every canonical band position)."""
    d = json.load(open(path))
    canon = {s: [tuple(p) for p in pk] for s, pk in d["canonical"].items()}
    pool = sorted(p for pk in canon.values() for p, _ in pk)
    return canon, d["real"], pool


def variation_draw(rec: cx.XmlRecord, peaks, pool, *key) -> tuple[cx.XmlRecord, dict]:
    """A record whose bands are one simulated re-measurement's strongest k, grid-rounded,
    de-duplicated and position-sorted."""
    rng = sv.rng_for("s2k", rec.species, *key)
    inst = sv.sample_raman(rng)
    grid = cr.GRID[inst.klass]
    drawn = sv.vary_spectrum(rng, list(peaks), inst, pool)
    k = rng.randint(*K_RANGE)
    bands = sorted({cr.quantize(p, grid) for p, _ in sv.strongest(drawn, k)})
    meta = {"instrument": inst.klass, "grid": grid, "offset": inst.offset_cm1, "k": k}
    return dataclasses.replace(rec, bands=bands), meta


def variation_rows(
    rec, peaks, pool, cues, target, n, *, val: bool, ns: str, digits: bool = True, jitter_libs: bool = True
) -> list[dict]:
    kind = cg.pair_kind(cues, target)
    both = cg._both_blocks(cues, target)
    rows = []
    for k in range(n):
        jrec, meta = variation_draw(rec, peaks, pool, ns, kind, k)
        if jitter_libs and "lines" in cues:
            jrec = cr.jitter_lines(jrec, ns, kind, k)
        libs_first = both and (k // 2) % 2 == 1
        text = cx.stripped_record(
            jrec, set(cues), target, libs_first=libs_first, raman_resolution=meta["grid"], digits=digits, sorted_bands=True
        )
        pre, suf = text.split(cx.BLANK)
        rows.append({
            "ex_id": f"xmlk:{rec.species}:{kind}:{ns}{k}", "species": rec.species, "kind": kind,
            "variant": "stripped·variation", "perm": "libs_first" if libs_first else "raman_first",
            "order": cx.ORDERS[k % 2], "prompt": fim_wrap(pre, "", suf, cx.ORDERS[k % 2]),
            "completion": cx.stripped_answer(rec, target), "val": val,
            **meta, "bands_shown": jrec.bands, "lines_shown": jrec.libs,
        })  # fmt: skip
    return rows


def rows_for(rec, peaks, pool, *, val: bool, exposures: dict | None = None, digits: bool = True, jitter_libs: bool = True) -> list[dict]:
    exposures = EXPOSURES if exposures is None else exposures
    known = {cg.pair_kind(c, t): (c, t) for c, t in cg.PAIRS}
    out = []
    for kind, n_train in exposures.items():
        cues, target = known[kind]
        n = 2 if val else n_train
        if any(c in cr.BAND_CUES for c in cues):
            out += variation_rows(rec, peaks, pool, cues, target, n, val=val, ns="v" if val else "t", digits=digits, jitter_libs=jitter_libs)
        elif jitter_libs and "lines" in cues:
            out += cr.jittered_rows(rec, cues, target, n, val=val, ns="v" if val else "t", digits=digits, jitter_libs=True)
        else:
            out += cg.granular_rows(rec, val=val, pairs=((cues, target),), exposures=0 if val else n_train)
    return out


# ── calibration check ──────────────────────────────────────────────────


def _near(a, bs, tol=5.0) -> bool:
    return any(abs(a - b) <= tol for b in bs)


def variation_stats(pairs) -> dict:
    """The §22k statistics for (canonical peaks, other peaks) pairs."""
    acc = collections.Counter()
    for cp, rp in pairs:
        c4 = [p for p, _ in sorted(cp, key=lambda x: -x[1])[:4]]
        r4 = [p for p, _ in sorted(rp, key=lambda x: -x[1])[:4]]
        allc, allr = [p for p, _ in cp], [p for p, _ in rp]
        acc["top1"] += abs(c4[0] - r4[0]) <= 5
        acc["set4"] += all(_near(b, r4) for b in c4)
        acc["present"] += sum(_near(b, allr) for b in c4) / 4
        acc["extra4"] += sum(not _near(b, allc) for b in r4) / len(r4)
    return {k: acc[k] / len(pairs) for k in CAL_TARGET}


def calibration_check(draws: int = 4) -> dict:
    """Simulated (fitted constants, lab jitter) vs real, on the re-measurements outside the probe set."""
    canon, real, pool = load_spectra()
    probe = set(json.load(open(PROBE_ITEMS))["R"]) if os.path.exists(PROBE_ITEMS) else set()
    cal = sorted(s for s in real if s not in probe)
    lab = sv.RamanInstrument("lab", 532, 3.0, 75.0, "±1 cm-1", "the 520.5 cm-1 silicon line", 0.0)
    sim = [(canon[s], sv.vary_spectrum(sv.rng_for("cal", s, k), canon[s], lab, pool)) for s in cal for k in range(draws)]
    obs = [(canon[s], [tuple(p) for p in real[s]["peaks"]]) for s in cal]
    return {"species": len(cal), "real": variation_stats(obs), "simulated": variation_stats(sim)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2"))
    ap.add_argument("--out", default=os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2k"))
    ap.add_argument("--calibrate", action="store_true", help="print the fit check and exit")
    args = ap.parse_args()
    if args.calibrate:
        print(json.dumps(calibration_check(), indent=1))
        return 0
    from package import load_tokenizer

    t0 = time.time()
    tok = load_tokenizer()
    canon, _, pool = load_spectra()
    src_docs = os.path.join(args.src, "docs")
    recs = json.load(open(os.path.join(src_docs, "xml_records.json")))
    trained = [cx.XmlRecord(**r) for r in recs["trained"]]
    val_sp = {r.species for r in trained if cx.is_val(f"xml:{r.species}")}
    kept = [r for r in map(json.loads, open(os.path.join(src_docs, "xml_fim.jsonl"), encoding="utf-8")) if r["kind"] != "identity"]
    new_train, new_val = [], []
    for rec in trained:
        (new_val if rec.species in val_sp else new_train).extend(rows_for(rec, canon[rec.species], pool, val=rec.species in val_sp))
    P = tok([r["prompt"] for r in new_train], add_special_tokens=False)["input_ids"]
    C = tok([r["completion"] for r in new_train], add_special_tokens=False)["input_ids"]
    tokens_by_kind = collections.Counter()
    for r, p, c in zip(new_train, P, C):
        tokens_by_kind[r["kind"]] += len(p) + len(c) + 1
    out_docs = os.path.join(args.out, "docs")
    os.makedirs(out_docs, exist_ok=True)
    with open(os.path.join(out_docs, "xml_fim.jsonl"), "w", encoding="utf-8") as fh:
        for r in kept + new_val + new_train:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    for f in ("xml_plain.jsonl", "xml_records.json"):
        shutil.copyfile(os.path.join(src_docs, f), os.path.join(out_docs, f))
    drawn = [r for r in new_train if r.get("k")]
    top4 = {s: [p for p, _ in sorted(pk, key=lambda x: -x[1])[:4]] for s, pk in canon.items()}
    man = {
        "arm": "representation (§22k)",
        "digits": True, "jitter_lines": True, "grid": cr.GRID, "k_range": K_RANGE, "exposures": EXPOSURES,
        "variation": {"alpha": sv.SAMPLE_SEVERITY_ALPHA, "beta": sv.SAMPLE_SEVERITY_BETA, "extra_per_severity": sv.SAMPLE_EXTRA_PER_SEVERITY,
                      "sigma": sv.SAMPLE_INTENSITY_SIGMA, "extra_rel": sv.SAMPLE_EXTRA_REL},
        "train_rows_new": len(new_train), "train_tokens_new": sum(tokens_by_kind.values()), "val_rows_new": len(new_val),
        "kept_rows": {"train": sum(1 for r in kept if not r.get("val")), "val": sum(1 for r in kept if r.get("val"))},
        "rows_by_kind": dict(sorted(collections.Counter(r["kind"] for r in new_train).items())),
        "train_tokens_by_kind": dict(sorted(tokens_by_kind.items())),
        "rows_by_instrument": dict(collections.Counter(r["instrument"] for r in drawn)),
        "bands_shown_mean": round(sum(len(r["bands_shown"]) for r in drawn) / max(1, len(drawn)), 2),
        "share_of_canonical_top4_shown_within_5": round(
            sum(sum(_near(b, r["bands_shown"]) for b in top4[r["species"]]) / 4 for r in drawn) / max(1, len(drawn)), 3),
        "distinct_band_lists_per_species_bands_to_name_mean": round(
            sum(len(v) for v in _distinct(drawn).values()) / max(1, len(_distinct(drawn))), 2),
        "species_trained": len(trained) - len(val_sp),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }  # fmt: skip
    json.dump(man, open(os.path.join(args.out, "docs_manifest.json"), "w"), indent=1)
    print(json.dumps({k: v for k, v in man.items() if k not in ("rows_by_kind",)}, indent=1))
    print(f"[{time.time() - t0:.0f}s] new train rows {len(new_train):,} = {man['train_tokens_new']:,} tokens")
    return 0


def _distinct(drawn: list[dict]) -> dict:
    out = collections.defaultdict(set)
    for r in drawn:
        if r["kind"] == "g:bands>name":
            out[r["species"]].add(tuple(r["bands_shown"]))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
