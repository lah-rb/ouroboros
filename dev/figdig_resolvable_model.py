"""Can we PREDICT a figure's minimum resolvable feature from its metadata?

Everything measured so far is a retention RATE over a truth set. The
operator's question is sharper: given a figure's native resolution, pen width
and span, what is the narrowest feature it can still resolve? That is a
per-feature threshold, and it is measurable directly -- every truth peak has a
known FWHM in the raw spectrum, so detection-vs-width is a curve whose 50%
crossing is the resolvable limit for that configuration.

The physics predicts the form. Measured stroke is nominal + a fixed ~0.9 px
antialiasing overhead, so

    pen_nm = span*stroke_pt/W_pt  +  72*c*span/(W_pt*dpi)
             ^ dpi-invariant          ^ decays as 1/dpi

and the grid term (columns max-pooling raw samples) is 2*grid at Nyquist.
So the candidate model is linear in (grid, pen), fit and validated here.

Records per TRUTH PEAK: its raw FWHM, its distance to the nearest neighbour,
and whether it won its OWN detection under one-to-one assignment.

That last word is load-bearing, and a first version of this script got it
wrong in the vacuous-gate shape this project keeps re-finding: asking "is
there A detection within tolerance" lets ONE merged blob satisfy BOTH of the
peaks under it, so merging -- the exact failure being measured -- cannot fail
the test. Detection then reads 90-97% across every separation quartile while
the true retention is ~49%. Assignment must be one-to-one, greedy on
distance, each detection consumable once.

Fit is validated by EXTRAPOLATION to held-out configurations, not a random
split.
"""

import json
import os
import sys
import warnings

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

from PIL import Image  # noqa: E402
from scipy.signal import find_peaks  # noqa: E402

from tools.figure_digitizer import (  # noqa: E402 -- path set above
    axes as A,
    curve as C,
    peaks as PK,
    synth as S,
)

SAMPLES = {
    f"sme{n}": f"/home/lah-rb/Downloads/HandheldLIBS/SME_sample_#{n}/spot_1/average.csv"
    for n in (1, 2, 3, 4, 6)
}
SPANS = [
    (20, (380.0, 400.0)),
    (50, (380.0, 430.0)),
    (100, (380.0, 480.0)),
    (200, (380.0, 580.0)),
    (600, (200.0, 800.0)),
]
STROKES = [0.5, 1.0, 2.0, 3.0]
DPIS = [160, 300, 600]
SIGMA = 10.0
OUT = os.environ.get("FIGDIG_OUT", "/tmp/figdig_model")
os.makedirs(OUT, exist_ok=True)


def load(path):
    d = np.genfromtxt(path, delimiter=",", skip_header=1)
    return d[:, 0], d[:, 1]


def render_extract(x, y, lo, hi, stroke, dpi, tag):
    m = (x >= lo) & (x <= hi)
    spec = S.PlotSpec(x_range=(lo, hi), stroke_pt=stroke)
    pdf, png = f"{OUT}/{tag}.pdf", f"{OUT}/{tag}.png"
    gt = S.render(x[m], y[m], spec, pdf)
    S.rasterize(pdf, png, dpi=dpi)
    img = Image.open(png).convert("L")
    mask = A.ink_mask(np.asarray(img, dtype=float))
    xa, ya = A.find_axes(mask)
    left = xa.span_px[0] + 1
    top, bottom = int(min(ya.span_px)) + 1, xa.position_px - 1
    tr = C.extract_trace(mask[top:bottom, left : xa.span_px[1] - 1])
    os.unlink(pdf)
    os.unlink(png)
    if tr is None:
        return None
    ilo, _, ihi, _ = gt.interior_pt
    x0, x1 = gt.x_domain
    pt = (left + tr.x_px) * 72.0 / dpi
    nm = x0 + (pt - ilo) / (ihi - ilo) * (x1 - x0)
    yrel = C.relative_intensity(-tr.y_px)
    fin = np.isfinite(tr.y_px)
    extent = float(np.nanmax(tr.y_px[fin]) - np.nanmin(tr.y_px[fin]))
    return nm, yrel, tr.stroke_px, extent, (ihi - ilo)


def truth_peaks(x, y, lo, hi):
    """Truth peaks WITH their raw width and nearest-neighbour separation."""
    m = (x >= lo) & (x <= hi)
    got = PK.pick(
        x[m],
        C.relative_intensity(y[m]),
        criterion="snr",
        snr_sigma=SIGMA,
        stroke_px=2.0,
        min_distance_px=1,
    )
    pos = np.array([p.position for p in got])
    out = []
    for k, p in enumerate(got):
        if p.fwhm is None or not np.isfinite(p.fwhm):
            continue
        d = np.abs(pos - p.position)
        d[k] = np.inf
        out.append(
            dict(
                pos=p.position,
                fwhm=float(p.fwhm),
                sep=float(d.min()) if pos.size > 1 else np.inf,
                inten=p.relative_intensity,
            )
        )
    return out


def assign(found, targets, tol):
    """Greedy one-to-one assignment, nearest pair first.

    Returns a boolean per target. A detection is consumed by the target it is
    assigned to, so two truth peaks merged into one blob yield exactly one
    hit -- which is the whole point.
    """
    got = np.zeros(len(targets), dtype=bool)
    if not len(found):
        return got
    f = np.asarray(found, dtype=float)
    t = np.asarray(targets, dtype=float)
    d = np.abs(t[:, None] - f[None, :])
    used_f = np.zeros(f.size, dtype=bool)
    while True:
        d_m = np.where(got[:, None] | used_f[None, :], np.inf, d)
        if not np.isfinite(d_m).any():
            break
        i, j = np.unravel_index(np.argmin(d_m), d_m.shape)
        if d_m[i, j] > tol:
            break
        got[i] = True
        used_f[j] = True
    return got


rows = []
for span, (lo, hi) in SPANS:
    for name, path in SAMPLES.items():
        x, y = load(path)
        truth = truth_peaks(x, y, lo, hi)
        if len(truth) < 5:
            continue
        for st in STROKES:
            for dpi in DPIS:
                r = render_extract(x, y, lo, hi, st, dpi, f"m_{name}_{span}_{st}_{dpi}")
                if r is None:
                    continue
                nm, yrel, meas, extent, w_pt = r
                grid = (hi - lo) / len(nm)
                pen = grid * meas
                tol = max(1.5 * pen, 0.05)
                fin = np.isfinite(nm) & np.isfinite(yrel)
                nmf, yf = nm[fin], yrel[fin]
                idx, _ = find_peaks(yf, prominence=1e-4, distance=1)
                ret_pos = nmf[idx]
                rec_pos = [
                    p.position
                    for p in PK.pick(
                        nmf,
                        yf,
                        criterion="snr",
                        snr_sigma=SIGMA,
                        stroke_px=meas,
                        min_distance_px=1,
                        noise_floor=PK.trace_noise_floor(extent),
                    )
                ]
                tpos = [t["pos"] for t in truth]
                got_ret = assign(ret_pos, tpos, tol)
                got_rec = assign(rec_pos, tpos, tol)
                for k, t in enumerate(truth):
                    rows.append(
                        dict(
                            sample=name,
                            span=span,
                            stroke=st,
                            dpi=dpi,
                            grid=grid,
                            pen=pen,
                            meas=meas,
                            w_pt=w_pt,
                            fwhm=t["fwhm"],
                            sep=t["sep"],
                            inten=t["inten"],
                            ret=bool(got_ret[k]),
                            rec=bool(got_rec[k]),
                        )
                    )
        print(f"done {name} span{span}", flush=True)

with open(f"{OUT}/resolvable_model.json", "w") as f:
    json.dump(rows, f)
print(
    f"\n{len(rows)} peak-observations over "
    f"{len(set((r['span'],r['stroke'],r['dpi'],r['sample']) for r in rows))} cells"
)
