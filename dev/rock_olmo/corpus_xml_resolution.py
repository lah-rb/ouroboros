#!/usr/bin/env python3
"""The resolution stage-2 arm (PROCEDURE §22h; rock venv).

WHY. §22g showed the backward lookup IS learnable (bands → name 0.00 → 0.47 on trained
species) but as exact strings: a 1 cm-1 shift erased it. The tokenizer makes this easy to
fall into — OLMo-2 writes every three-digit band as ONE token, so 493 and 495 are unrelated
symbols. This arm keeps §22g's structure and changes only how the spectral cues are shown.

WHAT. Every backward row whose cues include bands is a FRESH INSTRUMENT DRAW
(synth_variance.sample_raman: lab / portable / handheld at .50 / .35 / .15, class-scaled
Gaussian position error, and the calibrated 25 % of non-lab units with a constant 4–9 cm-1
offset; no band loss, no re-ranking — that is the next arm). The jittered positions are
ROUNDED to the class's resolution grid and the grid is written into the record:

    <raman resolution_cm1="5"><top>1005</top><next>495</next><next>415</next><next>1140</next></raman>

    grid: lab 1, portable 5, handheld 10 cm-1

At a coarse grid most draws of a band land on the same rounded value, so the exact lookup
§22g proved learnable can generalise by COVERAGE; at grid 1 it needs genuine numeric
proximity. Mixing grids separates the two, and the field names the grid.

Everything else is §22g: §22's rows minus identity kept byte-identical; the 10 TRAIN_PAIRS;
formula → name, lines → name and lines → formula (no bands) repeated exactly as in §22g;
LIBS lines exact. Exposures per species: 16 for bands → name and bands + lines → name
(coverage of the grids), 8 for every other pair. Val species get two fresh draws per pair.

  ./.venv/bin/python corpus_xml_resolution.py --out ~/corpora/rock-olmo-training/v6/stage2r

§22i (--digits --jitter-lines --out v6/stage2d): the same arm, with band and line values
written digit by digit (corpus_xml.digit_str) and EVERY row that shows LIBS lines drawing
fresh line positions (synth_variance.LIBS_POS_JITTER: ±U(0.02, 0.10) nm, random sign per
line) — §22h left the lines exact and the model routed identification through them. The
band draws are identical to §22h's (same seed namespace), so on the bands only the
rendering changes.
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
import synth_variance as sv  # noqa: E402
from fim_transform import fim_wrap  # noqa: E402

GRID = {"lab": 1, "portable": 5, "handheld": 10}
HEADLINE = ("g:bands>name", "g:bands+lines>name")
EXPOSURES_HEADLINE = 16
EXPOSURES_OTHER = 8
BAND_CUES = ("bands", "bands1", "bands2", "bands3")


def quantize(x: float, grid: int) -> int:
    return int(grid * round(x / grid))


def draw(rec: cx.XmlRecord, *key: object) -> tuple[cx.XmlRecord, int, str, float]:
    """(record with jittered + rounded bands, grid, instrument class, unit offset)."""
    rng = sv.rng_for("s2r", rec.species, *key)
    inst = sv.sample_raman(rng)
    grid = GRID[inst.klass]
    bands = [quantize(b + sv.jitter_raman(rng, inst), grid) for b in rec.bands]
    return dataclasses.replace(rec, bands=bands), grid, inst.klass, inst.offset_cm1


def jitter_lines(rec: cx.XmlRecord, *key: object) -> cx.XmlRecord:
    rng = sv.rng_for("s2d-lines", rec.species, *key)
    lo, hi = sv.LIBS_POS_JITTER
    return dataclasses.replace(rec, libs=[round(x + rng.choice((-1, 1)) * rng.uniform(lo, hi), 2) for x in rec.libs])


def jittered_rows(
    rec: cx.XmlRecord, cues: tuple[str, ...], target: str, n: int, *, val: bool, ns: str,
    digits: bool = False, jitter_libs: bool = False,
) -> list[dict]:
    kind = cg.pair_kind(cues, target)
    both = cg._both_blocks(cues, target)
    has_bands = any(c in BAND_CUES for c in cues)
    rows = []
    for k in range(n):
        if has_bands:
            jrec, grid, klass, off = draw(rec, ns, kind, k)
        else:
            jrec, grid, klass, off = rec, None, "none", 0.0
        if jitter_libs and "lines" in cues:
            jrec = jitter_lines(jrec, ns, kind, k)
        order = cx.ORDERS[k % 2]
        libs_first = both and (k // 2) % 2 == 1
        text = cx.stripped_record(jrec, set(cues), target, libs_first=libs_first, raman_resolution=grid, digits=digits)
        pre, suf = text.split(cx.BLANK)
        rows.append(
            {
                "ex_id": f"xmlr:{rec.species}:{kind}:{ns}{k}",
                "species": rec.species,
                "kind": kind,
                "variant": "stripped·jitter",
                "perm": "libs_first" if libs_first else "raman_first",
                "order": order,
                "prompt": fim_wrap(pre, "", suf, order),
                "completion": cx.stripped_answer(rec, target),
                "val": val,
                "instrument": klass,
                "grid": grid,
                "offset": off,
                "bands_shown": jrec.bands,
                "lines_shown": jrec.libs,
            }
        )
    return rows


def rows_for(rec: cx.XmlRecord, *, val: bool, digits: bool = False, jitter_libs: bool = False) -> list[dict]:
    out = []
    for cues, target in cg.TRAIN_PAIRS:
        kind = cg.pair_kind(cues, target)
        if any(c in BAND_CUES for c in cues) or (jitter_libs and "lines" in cues):
            n = 2 if val else (EXPOSURES_HEADLINE if kind in HEADLINE else EXPOSURES_OTHER)
            out += jittered_rows(rec, cues, target, n, val=val, ns="v" if val else "t", digits=digits, jitter_libs=jitter_libs)
        else:
            out += cg.granular_rows(rec, val=val, pairs=((cues, target),), exposures=0 if val else EXPOSURES_OTHER)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2"))
    ap.add_argument("--out", default=os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2r"))
    ap.add_argument("--digits", action="store_true", help="§22i: band and line values digit by digit")
    ap.add_argument("--jitter-lines", action="store_true", help="§22i: fresh LIBS line draws in every row that shows lines")
    args = ap.parse_args()
    from package import load_tokenizer

    t0 = time.time()
    tok = load_tokenizer()
    src_docs = os.path.join(args.src, "docs")
    recs = json.load(open(os.path.join(src_docs, "xml_records.json")))
    trained = [cx.XmlRecord(**r) for r in recs["trained"]]
    val_sp = {r.species for r in trained if cx.is_val(f"xml:{r.species}")}
    kept = []
    with open(os.path.join(src_docs, "xml_fim.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r["kind"] != "identity":
                kept.append(r)
    new_train, new_val = [], []
    for rec in trained:
        (new_val if rec.species in val_sp else new_train).extend(
            rows_for(rec, val=rec.species in val_sp, digits=args.digits, jitter_libs=args.jitter_lines)
        )
    P = tok([r["prompt"] for r in new_train], add_special_tokens=False)["input_ids"]
    C = tok([r["completion"] for r in new_train], add_special_tokens=False)["input_ids"]
    new_tokens = sum(len(p) + len(c) + 1 for p, c in zip(P, C))
    out_docs = os.path.join(args.out, "docs")
    os.makedirs(out_docs, exist_ok=True)
    with open(os.path.join(out_docs, "xml_fim.jsonl"), "w", encoding="utf-8") as fh:
        for r in kept + new_val + new_train:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    for f in ("xml_plain.jsonl", "xml_records.json"):
        shutil.copyfile(os.path.join(src_docs, f), os.path.join(out_docs, f))
    jit = [r for r in new_train if r.get("grid")]
    by_class = collections.Counter(r["instrument"] for r in jit)
    canon = {r.species: r.bands for r in trained}
    unchanged = collections.defaultdict(lambda: [0, 0])
    for r in jit:
        c = canon[r["species"]]
        for a, b in zip(r["bands_shown"], c):
            u = unchanged[r["instrument"]]
            u[0] += a == b
            u[1] += 1
    distinct = collections.defaultdict(set)
    for r in jit:
        if r["kind"] == "g:bands>name":
            distinct[r["species"]].add(tuple(r["bands_shown"]))
    man = {
        "arm": "digits + jittered lines (§22i)" if args.digits else "resolution (§22h)",
        "digits": args.digits,
        "jitter_lines": args.jitter_lines,
        "grid": GRID,
        "exposures": {"headline": EXPOSURES_HEADLINE, "other": EXPOSURES_OTHER, "headline_pairs": HEADLINE},
        "train_rows_new": len(new_train),
        "train_tokens_new": new_tokens,
        "val_rows_new": len(new_val),
        "kept_rows": {"train": sum(1 for r in kept if not r.get("val")), "val": sum(1 for r in kept if r.get("val"))},
        "rows_by_kind": dict(sorted(collections.Counter(r["kind"] for r in new_train).items())),
        "jittered_rows_by_instrument": dict(by_class),
        "share_of_bands_shown_equal_to_canonical": {k: round(v[0] / v[1], 3) for k, v in unchanged.items()},
        "distinct_band_tuples_per_species_bands_to_name_mean": round(sum(len(v) for v in distinct.values()) / max(1, len(distinct)), 2),
        "species_trained": len(trained) - len(val_sp),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    json.dump(man, open(os.path.join(args.out, "docs_manifest.json"), "w"), indent=1)
    print(json.dumps({k: v for k, v in man.items() if k != "rows_by_kind"}, indent=1))
    print(f"[{time.time()-t0:.0f}s] new train rows {len(new_train):,} = {new_tokens:,} tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
