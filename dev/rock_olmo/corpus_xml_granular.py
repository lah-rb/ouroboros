#!/usr/bin/env python3
"""The granular stage-2 blank policy (PROCEDURE §22g; rock venv — needs only the rendered
§22 records and the tokenizer, not the facts layer).

WHY. §22's identity blank (species + formula from the rest of a FULL record, crystal
system and laser always visible) reached 0.014 against a 0.946 ceiling after 16 exposures
per species — the same count at which formula / raman / top reached ~0.92. The chained
probe (§22f) showed that half of its backward cells asked for mappings neither model was
ever trained on (lines → name, bands → formula, spectra → formula without the name first),
and the ceilings showed that four bands identify a species only JOINTLY (one band leaves
~42 candidates). This arm changes one thing: the blank policy.

WHAT. Identical to §22 stage 2 except:
  * §22's `identity` rows are REMOVED (the blank being redesigned);
  * in their place, STRIPPED records (corpus_xml.stripped_record: only the cue fields, one
    blank, no system, no laser) for the 10 TRAIN_PAIRS that ask for an identity or a
    formula from spectra — including a band-count progression (the strongest 1, 2, 3 bands
    → name) so the narrowing from ~42 candidates to one is itself trained — each at
    EXPOSURES (8) rows per species, the count at which §22's libs and system blanks reached
    1.00 / 0.92. The other 9 PAIRS (forward, and spectra-as-context) are probed, not
    trained: §22's full-record blanks already train those directions, so they measure
    transfer. SIZING (recorded before training): at the identity rows' own budget
    (2.59 M tokens) the 19 pairs would get ~1 exposure per species each — a test that
    cannot distinguish "does not learn" from "never shown"; the backward share therefore
    grows and the stage with it (~26.8 M → ~33 M tokens, same 65/20/7/8 mix);
  * every other §22 row (formula, raman, top, libs, system, and the species blank with the
    formula visible) is kept byte-identical, and so are the plain bundles.
Species-first attribute order only (formula-first stays held out for the probe); both
block orders where both blocks are present; PSM and SPM; identical rows are repeated to
reach EXPOSURES and spread by the packer's global shuffle. Val species (the §22 1 %)
keep one pass of every pair for val-xml_fim; the untouched probe species are never rendered.

  ./.venv/bin/python corpus_xml_granular.py --src ~/corpora/rock-olmo-training/v6/stage2 \\
      --out ~/corpora/rock-olmo-training/v6/stage2g
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import corpus_xml as cx  # noqa: E402
from fim_transform import fim_wrap  # noqa: E402

#: (cue fields, target) — the chained probe's steps, deduplicated, plus the progression
PAIRS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("name",), "formula"),
    (("name", "formula"), "top"),
    (("name", "formula", "top"), "line"),
    (("formula",), "name"),
    (("bands",), "name"),
    (("bands", "name"), "formula"),
    (("bands",), "formula"),
    (("bands", "formula"), "name"),
    (("lines",), "name"),
    (("lines", "name"), "formula"),
    (("lines",), "formula"),
    (("lines", "formula"), "name"),
    (("bands", "lines"), "name"),
    (("bands", "lines", "name"), "formula"),
    (("bands", "lines"), "formula"),
    (("bands", "lines", "formula"), "name"),
    (("bands1",), "name"),
    (("bands2",), "name"),
    (("bands3",), "name"),
)
#: the pairs this arm TRAINS: every identity or formula target whose cues are spectra only
TRAIN_PAIRS = tuple(
    (c, tg)
    for c, tg in PAIRS
    if tg in ("name", "formula") and set(c) <= {"bands", "lines", "bands1", "bands2", "bands3"} or (c, tg) == (("formula",), "name")
)
EXPOSURES = 8  # rows per (pair, species) in train; val species get one pass
SEED = 20260922


def pair_kind(cues: tuple[str, ...], target: str) -> str:
    return "g:" + "+".join(cues) + ">" + target


def _both_blocks(cues: tuple[str, ...], target: str) -> bool:
    has_raman = any(c in ("bands", "bands1", "bands2", "bands3", "top") for c in cues) or target == "top"
    has_libs = any(c in ("lines", "line") for c in cues) or target == "line"
    return has_raman and has_libs


def granular_rows(
    rec: cx.XmlRecord, *, val: bool, formula_first: bool = False, pairs=PAIRS, exposures: int = 0
) -> list[dict]:
    """One pass = both FIM orders (x both block orders when both blocks are present).
    `exposures` > 0 repeats the pass until each pair has that many rows."""
    rows = []
    for cues, target in pairs:
        one = []
        for libs_first in ((False, True) if _both_blocks(cues, target) else (False,)):
            text = cx.stripped_record(rec, set(cues), target, formula_first=formula_first, libs_first=libs_first)
            prefix, suffix = text.split(cx.BLANK)
            for order in cx.ORDERS:
                one.append(
                    {
                        "ex_id": f"xmlg:{rec.species}:{pair_kind(cues, target)}:{'L' if libs_first else 'R'}:{'ff' if formula_first else 'sf'}:{order}",
                        "species": rec.species,
                        "kind": pair_kind(cues, target),
                        "variant": "stripped",
                        "perm": ("libs_first" if libs_first else "raman_first") + ("/formula_first" if formula_first else ""),
                        "order": order,
                        "prompt": fim_wrap(prefix, "", suffix, order),
                        "completion": cx.stripped_answer(rec, target),
                        "val": val,
                    }
                )
        reps = max(1, exposures // len(one)) if exposures else 1
        rows += [dict(r, ex_id=r["ex_id"] + (f"#{k}" if reps > 1 else "")) for k in range(reps) for r in one]
    return rows


def fit_to_budget(rows: list[dict], tokens: list[int], budget: int, rng: random.Random) -> tuple[list[int], int]:
    # retained for reuse; the §22g build sizes by exposures instead
    """Indices of rows whose token total lands on `budget`: one seeded shuffled pass after
    another (rows repeat only when a full pass is under budget), stopping at the budget."""
    chosen, used = [], 0
    if not rows:
        return chosen, used
    while used < budget:
        idx = list(range(len(rows)))
        rng.shuffle(idx)
        for i in idx:
            if used + tokens[i] > budget:
                continue
            chosen.append(i)
            used += tokens[i]
            if used >= budget - 64:
                return chosen, used
        if not chosen:
            break
    return chosen, used


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2"))
    ap.add_argument("--out", default=os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2g"))
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    from package import load_tokenizer

    t0 = time.time()
    tok = load_tokenizer()
    rng = random.Random(args.seed)
    src_docs = os.path.join(args.src, "docs")
    recs = json.load(open(os.path.join(src_docs, "xml_records.json")))
    trained = [cx.XmlRecord(**r) for r in recs["trained"]]
    val_sp = {r.species for r in trained if cx.is_val(f"xml:{r.species}")}

    kept, identity = [], []
    with open(os.path.join(src_docs, "xml_fim.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            (identity if r["kind"] == "identity" else kept).append(r)
    id_train = [r for r in identity if not r.get("val")]

    def ntok(rows: list[dict]) -> list[int]:
        P = tok([r["prompt"] for r in rows], add_special_tokens=False)["input_ids"]
        C = tok([r["completion"] for r in rows], add_special_tokens=False)["input_ids"]
        return [len(p) + len(c) + 1 for p, c in zip(P, C)]

    budget = sum(ntok(id_train))
    g_train, g_val = [], []
    for rec in trained:
        if rec.species in val_sp:
            g_val.extend(granular_rows(rec, val=True, pairs=TRAIN_PAIRS))
        else:
            g_train.extend(granular_rows(rec, val=False, pairs=TRAIN_PAIRS, exposures=EXPOSURES))
    g_tok = ntok(g_train)
    used = sum(g_tok)
    chosen = g_train
    reps = collections.Counter(r["ex_id"].split("#")[0] for r in chosen)

    out_docs = os.path.join(args.out, "docs")
    os.makedirs(out_docs, exist_ok=True)
    with open(os.path.join(out_docs, "xml_fim.jsonl"), "w", encoding="utf-8") as fh:
        for r in kept + g_val:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        for r in chosen:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    for f in ("xml_plain.jsonl", "xml_records.json"):
        shutil.copyfile(os.path.join(src_docs, f), os.path.join(out_docs, f))
    by_kind = collections.Counter(r["kind"] for r in chosen)
    man = {
        "arm": "granular (§22g)",
        "seed": args.seed,
        "src": args.src,
        "removed_identity_rows": {"train": len(id_train), "val": len(identity) - len(id_train)},
        "identity_tokens_removed": budget,
        "granular_train_rows": len(chosen),
        "granular_train_tokens": used,
        "net_added_fim_tokens": used - budget,
        "exposures_per_pair_per_species": EXPOSURES,
        "distinct_granular_rows": len(reps),
        "train_pairs": [pair_kind(c, t_) for c, t_ in TRAIN_PAIRS],
        "probe_only_pairs": [pair_kind(c, t_) for c, t_ in PAIRS if (c, t_) not in TRAIN_PAIRS],
        "granular_rows_by_kind": dict(sorted(by_kind.items())),
        "granular_val_rows": len(g_val),
        "kept_rows": {"train": sum(1 for r in kept if not r.get("val")), "val": sum(1 for r in kept if r.get("val"))},
        "pairs": [pair_kind(c, t) for c, t in PAIRS],
        "species_trained": len(trained) - len(val_sp),
        "species_val": sorted(val_sp),
        "src_records_sha256": hashlib.sha256(open(os.path.join(src_docs, "xml_records.json"), "rb").read()).hexdigest()[:16],
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    json.dump(man, open(os.path.join(args.out, "docs_manifest.json"), "w"), indent=1)
    print(
        f"[{time.time()-t0:.0f}s] identity removed: {len(id_train):,} train rows = {budget:,} tokens | granular "
        f"{len(chosen):,} rows = {used:,} tokens ({EXPOSURES} per pair per species, {len(reps):,} distinct) | net +{used - budget:,} | "
        f"kept {man['kept_rows']} | val granular {len(g_val):,}",
        flush=True,
    )
    for k, v in sorted(by_kind.items()):
        print(f"   {k:40s} {v:6,d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
