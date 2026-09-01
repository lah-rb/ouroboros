"""How many SPECTRUM figures are recoverable as vector, at scale?

The 60-paper figdata sample gives 6.2% but its vector figures turned out to
be UV-Vis / Raman / fluorescence / time traces -- the `technique: libs` tag is
the run PARAMETER, not detected content. This scans the PDF corpus directly:
for every page carrying a curve-shaped vector path, measure the curve's own
sample count (unique x positions), which is the figure's true information
ceiling regardless of how it is later rasterised.

Cheap by design: no relocation, no rendering. get_drawings() per page plus the
existing shape filter.
"""

import glob
import json
import os
import sys
import warnings
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")
import fitz  # noqa: E402 -- path set above
from tools.figure_digitizer.source import (  # noqa: E402 -- path set above
    _polylines,
    _CURVE_MIN_X_EXTENT_PT,
    _CURVE_MIN_Y_EXTENT_PT,
    _CURVE_MIN_MONOTONE_FRAC,
)

PDFS = sorted(glob.glob(os.path.expanduser("~/corpora/ouroboros-spectra/pdfs/*.pdf")))
LIMIT = int(os.environ.get("LIMIT", "600"))
OUT = "/tmp/figdig_vec_survey.json"


def curve_like(pts):
    x, y = pts[:, 0], pts[:, 1]
    if x.max() - x.min() < _CURVE_MIN_X_EXTENT_PT:
        return None
    if y.max() - y.min() < _CURVE_MIN_Y_EXTENT_PT:
        return None
    d = np.diff(x)
    nz = d[d != 0]
    if nz.size == 0:
        return None
    mono = max((nz > 0).mean(), (nz < 0).mean())
    if mono < _CURVE_MIN_MONOTONE_FRAC:
        return None
    return dict(
        uniq_x=int(np.unique(np.round(x, 3)).size),
        w_pt=float(x.max() - x.min()),
        n=int(len(pts)),
    )


rows = []
for i, p in enumerate(PDFS[:LIMIT]):
    key = os.path.basename(p)[:-4]
    try:
        doc = fitz.open(p)
    except Exception:
        continue
    try:
        best = None
        for pno in range(min(len(doc), 60)):
            try:
                pl = _polylines(doc[pno], doc[pno].rect)
            except Exception:
                continue
            for pts in pl:
                c = curve_like(pts)
                if c and (best is None or c["uniq_x"] > best["uniq_x"]):
                    best = dict(c, page=pno)
        if best:
            rows.append(dict(key=key, **best))
    finally:
        doc.close()
    if (i + 1) % 50 == 0:
        print(
            f"  {i+1}/{min(LIMIT,len(PDFS))} scanned, {len(rows)} with vector curves",
            flush=True,
        )

json.dump(rows, open(OUT, "w"), indent=1)
n = min(LIMIT, len(PDFS))
print(
    f"\n{len(rows)}/{n} papers ({100*len(rows)/n:.1f}%) carry a curve-shaped vector path"
)
if rows:
    ux = np.array([r["uniq_x"] for r in rows])
    w = np.array([r["w_pt"] for r in rows])
    dens = ux / w
    print(
        f"  unique-x samples: median {np.median(ux):.0f}  range {ux.min()}-{ux.max()}"
    )
    for label, cols in (
        ("beats 160dpi raster", 160),
        ("beats 300dpi", 300),
        ("beats 600dpi", 600),
    ):
        print(f"  {label:<22} {100*np.mean(dens > cols/72):>5.1f}% of vector papers")
