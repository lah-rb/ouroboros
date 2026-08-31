#!/usr/bin/env python3
"""Can annotation furniture be masked out before curve extraction?

Operator suggestion, 2026-08-31: have paddle block off legend and annotation
space and paint it to background, so an annotated figure becomes extractable
instead of refused.

The mechanism works. The box SOURCE is the problem: paddle cannot supply one,
and neither can a VLM, so the only exact source is the PDF itself -- which
covers the vector-backed tiers only. This script measures that coverage.

    .venv/bin/python dev/figdig_furniture_masking.py
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz  # noqa: E402  # pymupdf

from tools.figure_digitizer import source as S  # noqa: E402

# Tick labels and axis titles live in the outer margin of a figure rect and are
# NOT furniture -- masking them would remove the calibration. Only the plot
# interior is inspected.
_INNER = (0.12, 0.06, 0.04, 0.16)  # left, top, right, bottom insets


def inner_rect(r: fitz.Rect) -> fitz.Rect:
    li, ti, ri, bi = _INNER
    return fitz.Rect(
        r.x0 + li * r.width,
        r.y0 + ti * r.height,
        r.x1 - ri * r.width,
        r.y1 - bi * r.height,
    )


def furniture_in(pg, r: fitz.Rect) -> tuple[int, int]:
    """(text spans, filled boxes) inside a figure's plot interior."""
    inner = inner_rect(r)
    spans = 0
    try:
        for blk in pg.get_text("dict", clip=inner)["blocks"]:
            for ln in blk.get("lines", []):
                spans += len(ln["spans"])
    except Exception:  # noqa: BLE001
        pass
    fills = 0
    try:
        for d in pg.get_drawings():
            dr = d.get("rect")
            if (
                dr is not None
                and d.get("fill") is not None
                and abs((dr & inner).get_area()) > 0
                and 4 < dr.width < 0.6 * r.width
            ):
                fills += 1
    except Exception:  # noqa: BLE001
        pass
    return spans, fills


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/corpora/ouroboros-spectra"))
    args = ap.parse_args()

    tier = collections.Counter()
    withtext = collections.Counter()
    withfill = collections.Counter()
    spans = collections.defaultdict(list)

    for path in sorted(glob.glob(f"{args.root}/databank/figdata/*.json")):
        rec = json.load(open(path, encoding="utf-8"))
        figs = [f for f in rec["figs"] if f["status"] == "sourced"]
        if not figs:
            continue
        pdf = S.find_pdf(args.root, rec["paper_key"])
        if not pdf:
            continue
        try:
            doc = fitz.open(pdf)
        except Exception:  # noqa: BLE001
            continue
        for f in figs:
            src = f["source"]
            tier[src["tier"]] += 1
            n, nf = furniture_in(doc[src["page"]], fitz.Rect(*src["crop_rect_pt"]))
            if n:
                withtext[src["tier"]] += 1
            if nf:
                withfill[src["tier"]] += 1
            spans[src["tier"]].append(n)
        doc.close()

    print(
        f'{"tier":18} {"n":>4} {"interior TEXT":>16} {"filled BOX":>14} {"med spans":>10}'
    )
    for t, c in tier.most_common():
        wt, fl = withtext[t], withfill[t]
        print(
            f"{t:18} {c:4} {wt:6} ({100*wt/c:3.0f}%) {fl:6} ({100*fl/c:3.0f}%) "
            f"{statistics.median(spans[t]):10.0f}"
        )
    tot = sum(tier.values())
    anyt = sum(withtext.values())
    if tot:
        print(
            f"\n{anyt}/{tot} ({100*anyt/tot:.0f}%) of sourced figures carry PDF text "
            f"inside the plot area -- the ceiling on PDF-native masking."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
