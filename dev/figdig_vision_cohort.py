#!/usr/bin/env python3
"""Score P9 and P10 on real corpus figures.

P9  the structured vision tick-count disagrees with the CV tick-count on
    >=20% of figures. If it disagrees on <5%, the two-source design is
    over-engineering and collapses to one source.
P10 figtext alone yields both axis ranges for <=35% of nm-bearing spectrum
    figures (measured 28.9%); a structured re-ask raises usable metadata
    to >=80%.

Bounded on purpose: the vision server is also serving the live mission's
figtext lanes, so this runs one request at a time over a small cohort.

    .venv/bin/python dev/figdig_vision_cohort.py --limit 40
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import fitz  # noqa: E402  # pymupdf
from tools.figure_digitizer import axes as A  # noqa: E402
from tools.figure_digitizer import graphmeta as G  # noqa: E402
from tools.figure_digitizer import source as S  # noqa: E402

# The naive figtext parse P10 is measured against: two numeric ranges
# anywhere in the description.
_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)")


def figtext_yields_both_ranges(text: str) -> bool:
    return len(_RANGE_RE.findall(text or "")) >= 2


def usable(meta: dict | None) -> bool:
    """Enough to calibrate: a unit and at least two labels on each axis."""
    if not meta or not meta.get("is_plot"):
        return False
    return (
        bool(meta.get("x_unit"))
        and len(meta.get("x_tick_labels") or []) >= 2
        and len(meta.get("y_tick_labels") or []) >= 2
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/corpora/ouroboros-spectra"))
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/figdig_cohort"))
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--seed", type=int, default=23)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    # Cohort: figures already sourced by phase 1a, LIBS, not full-page leaks.
    cands: list[tuple[str, str]] = []
    import glob

    for path in sorted(glob.glob(f"{args.root}/databank/figdata/*.json")):
        rec = json.load(open(path, encoding="utf-8"))
        for fig in rec["figs"]:
            if fig["status"] == "sourced" and fig.get("technique") == "libs":
                cands.append((rec["paper_key"], fig["fig"]))
    random.Random(args.seed).shuffle(cands)
    cands = cands[: args.limit]
    print(f"cohort: {len(cands)} sourced LIBS figures", flush=True)

    rows = []
    t0 = time.time()
    for n, (key, fig) in enumerate(cands, 1):
        pdf = S.find_pdf(args.root, key)
        if not pdf:
            continue
        row: dict = {"key": key, "fig": fig}
        try:
            doc = fitz.open(pdf)
            crop = os.path.join(args.root, "databank", "figures", key, fig)
            reloc = S.relocate(crop, doc)
            if reloc is None or not reloc.located:
                doc.close()
                continue
            src = S.choose_source(doc, reloc)
            img = S.load_pixels(doc, src)
            doc.close()
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
            continue

        png = os.path.join(args.out, f"{key}__{fig}")
        img.save(png)
        row["tier"] = src.tier
        row["px"] = list(img.size)

        gray = np.asarray(img.convert("L"), dtype=float)
        xa, ya = A.find_axes(A.ink_mask(gray))
        row["cv_x_ticks"] = xa.n_ticks if xa else 0
        row["cv_y_ticks"] = ya.n_ticks if ya else 0

        # figtext's own reading, for the P10 baseline
        hint = next((h for h in G.hints(args.root, key) if h.fig == fig), None)
        row["figtext_two_ranges"] = (
            figtext_yields_both_ranges(f"{hint.caption} {hint.text}") if hint else False
        )

        meta = G.ask_structured(png)
        row["meta"] = meta
        row["usable"] = usable(meta)
        row["vis_x_ticks"] = len(meta.get("x_tick_labels") or []) if meta else 0
        row["vis_y_ticks"] = len(meta.get("y_tick_labels") or []) if meta else 0

        # End-to-end: can a real figure be calibrated?
        if xa and meta and meta.get("x_tick_labels"):
            t, v, margin = A.match_labels_to_ticks(xa.ticks_px, meta["x_tick_labels"])
            cal = A.calibrate(t, v, source_of_labels="vision", alignment_margin=margin)
            row["cal_ok"] = bool(cal.ok)
            row["cal_res_px"] = round(cal.max_residual_px, 3)
            row["cal_scale"] = cal.scale
            row["cal_n"] = cal.n_points
            if cal.ok and xa.ticks_px:
                lo = float(cal.to_value(min(xa.ticks_px)))
                hi = float(cal.to_value(max(xa.ticks_px)))
                row["cal_range"] = [round(lo, 2), round(hi, 2)]
        rows.append(row)
        print(
            f"  [{n}/{len(cands)}] {key[:34]:34} {fig:11} tier={row.get('tier','?'):14} "
            f"cv={row['cv_x_ticks']:2} vis={row['vis_x_ticks']:2} "
            f"usable={row['usable']} cal_ok={row.get('cal_ok')} "
            f"({time.time() - t0:.0f}s)",
            flush=True,
        )

    with open(os.path.join(args.out, "rows.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1)

    scored = [r for r in rows if "meta" in r]
    n = len(scored)
    if not n:
        print("no rows scored")
        return 1
    got_meta = [r for r in scored if r["meta"]]
    disagree = [
        r for r in got_meta if r["vis_x_ticks"] and r["vis_x_ticks"] != r["cv_x_ticks"]
    ]
    comparable = [r for r in got_meta if r["vis_x_ticks"] and r["cv_x_ticks"]]
    print()
    print(f"scored figures            : {n}")
    print(f"vision replied+parsed     : {len(got_meta)} ({100*len(got_meta)/n:.0f}%)")
    print(
        f"figtext gave two ranges   : {sum(r['figtext_two_ranges'] for r in scored)}"
        f" ({100*sum(r['figtext_two_ranges'] for r in scored)/n:.0f}%)   <- P10 baseline"
    )
    print(
        f"structured ask USABLE     : {sum(r['usable'] for r in scored)}"
        f" ({100*sum(r['usable'] for r in scored)/n:.0f}%)   <- P10 target >=80%"
    )
    if comparable:
        print(
            f"tick-count disagreement   : {len(disagree)}/{len(comparable)}"
            f" ({100*len(disagree)/len(comparable):.0f}%)   <- P9 (>=20% keeps two sources,"
            f" <5% collapses to one)"
        )
    cal_ok = [r for r in scored if r.get("cal_ok")]
    print(f"end-to-end calibration ok : {len(cal_ok)}/{n} ({100*len(cal_ok)/n:.0f}%)")
    if cal_ok:
        res = sorted(r["cal_res_px"] for r in cal_ok)
        print(f"   median residual        : {res[len(res)//2]:.2f} px")
    return 0


if __name__ == "__main__":
    sys.exit(main())
