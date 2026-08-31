"""Does a finer pen ever lose information, or only my detector?

Last sweep coupled TWO strokes: the pen that draws the figure and the
stroke_px handed to pick(), which sets the noise window (15x). This run
separates them. Arms per (sample, span, stroke):
  retention   : a distinct local apex exists within tolerance (no threshold)
  rec_coupled : 10-sigma, window follows the measured stroke (last turn)
  rec_fixed   : 10-sigma, window pinned to the 1.0pt arm's measured stroke
Also records the median estimated sigma under the coupled window.
"""

import sys
import os
import collections
import json
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
DPI, SIGMA = 160, 10.0
OUT = os.environ.get("FIGDIG_OUT", "/tmp/figdig_stroke")
os.makedirs(OUT, exist_ok=True)


def load(path):
    d = np.genfromtxt(path, delimiter=",", skip_header=1)
    return d[:, 0], d[:, 1]


def render_extract(x, y, lo, hi, stroke, tag):
    m = (x >= lo) & (x <= hi)
    xs, ys = x[m], y[m]
    spec = S.PlotSpec(x_range=(lo, hi), stroke_pt=stroke)
    pdf, png = f"{OUT}/{tag}.pdf", f"{OUT}/{tag}.png"
    gt = S.render(xs, ys, spec, pdf)
    S.rasterize(pdf, png, dpi=DPI)
    img = Image.open(png).convert("L")
    mask = A.ink_mask(np.asarray(img, dtype=float))
    xa, ya = A.find_axes(mask)
    left, right = xa.span_px[0] + 1, xa.span_px[1] - 1
    top, bottom = int(min(ya.span_px)) + 1, xa.position_px - 1
    tr = C.extract_trace(mask[top:bottom, left:right])
    os.unlink(pdf)
    os.unlink(png)
    if tr is None:
        return None
    ilo, _, ihi, _ = gt.interior_pt
    x0, x1 = gt.x_domain
    pt = (left + tr.x_px) * 72.0 / DPI
    nm = x0 + (pt - ilo) / (ihi - ilo) * (x1 - x0)
    yrel = C.relative_intensity(-tr.y_px)
    fin = np.isfinite(tr.y_px)
    extent = float(np.nanmax(tr.y_px[fin]) - np.nanmin(tr.y_px[fin]))
    return nm, yrel, tr.stroke_px, extent


def truth_peaks(x, y, lo, hi):
    m = (x >= lo) & (x <= hi)
    got = PK.pick(
        x[m],
        C.relative_intensity(y[m]),
        criterion="snr",
        snr_sigma=SIGMA,
        stroke_px=2.0,
        min_distance_px=1,
    )
    return [p.position for p in got]


def recovered(found_pos, truth, tol):
    hit = 0
    used = np.zeros(len(found_pos), dtype=bool)
    fp = np.asarray(found_pos)
    for t in truth:
        if fp.size == 0:
            break
        d = np.abs(fp - t)
        d[used] = np.inf
        i = int(np.argmin(d))
        if d[i] <= tol:
            used[i] = True
            hit += 1
    return hit


rows = []
for span, (lo, hi) in SPANS:
    for name, path in SAMPLES.items():
        x, y = load(path)
        truth = truth_peaks(x, y, lo, hi)
        if len(truth) < 3:
            continue
        # pin: measured stroke of the 1.0pt render for this cell
        pin = None
        got = {}
        for st in STROKES:
            r = render_extract(x, y, lo, hi, st, f"t_{name}_{span}_{st}")
            if r is not None:
                got[st] = r
                if st == 1.0:
                    pin = r[2]
        for st, (nm, yrel, meas, extent) in got.items():
            grid = (hi - lo) / len(nm)
            resolv = grid * meas
            tol = max(1.5 * resolv, 0.05)
            fin = np.isfinite(nm) & np.isfinite(yrel)
            nmf, yf = nm[fin], yrel[fin]
            # retention: any distinct local apex, no threshold
            idx, _ = find_peaks(yf, prominence=1e-4, distance=1)
            ret = recovered(nmf[idx], truth, tol)
            # coupled vs fixed 10-sigma
            pc = PK.pick(
                nmf,
                yf,
                criterion="snr",
                snr_sigma=SIGMA,
                stroke_px=meas,
                min_distance_px=1,
            )
            pf = PK.pick(
                nmf,
                yf,
                criterion="snr",
                snr_sigma=SIGMA,
                stroke_px=(pin or meas),
                min_distance_px=1,
            )
            pq = PK.pick(
                nmf,
                yf,
                criterion="snr",
                snr_sigma=SIGMA,
                stroke_px=meas,
                min_distance_px=1,
                noise_floor=PK.trace_noise_floor(extent),
            )
            rc = recovered([p.position for p in pc], truth, tol)
            rf = recovered([p.position for p in pf], truth, tol)
            rq = recovered([p.position for p in pq], truth, tol)
            _, sig_c = PK._local_background_and_noise(yf, meas)
            rows.append(
                dict(
                    sample=name,
                    span=span,
                    stroke=st,
                    meas=round(meas, 2),
                    grid=round(grid, 4),
                    resolv=round(resolv, 3),
                    n_truth=len(truth),
                    ret=ret,
                    rec_c=rc,
                    rec_f=rf,
                    rec_q=rq,
                    sigma=round(float(np.median(sig_c)), 5),
                )
            )
        print(f"done {name} span{span}", flush=True)

with open(f"{OUT}/stroke_deconfound.json", "w") as f:
    json.dump(rows, f, indent=1)

# aggregate

agg = collections.defaultdict(lambda: [0, 0, 0, 0, 0, 0.0, 0.0, 0])
for r in rows:
    k = (r["span"], r["stroke"])
    a = agg[k]
    a[0] += r["ret"]
    a[1] += r["rec_c"]
    a[2] += r["rec_q"]
    a[3] += r["n_truth"]
    a[4] += 1
    a[5] += r["resolv"]
    a[6] += r["sigma"]
    a[7] += r["meas"]
print(
    f"\n{'span':>5} {'pt':>4} {'px':>5} {'resolv':>7} | {'ret%':>6} {'coupled%':>8} {'floored%':>8} | {'med_sigma':>9}"
)
for (span, st), a in sorted(agg.items()):
    n = a[3]
    print(
        f"{span:>5} {st:>4} {a[7]/a[4]:>5.2f} {a[5]/a[4]:>6.3f} | {100*a[0]/n:>5.1f} {100*a[1]/n:>8.1f} {100*a[2]/n:>7.1f} | {a[6]/a[4]:>9.5f}"
    )
