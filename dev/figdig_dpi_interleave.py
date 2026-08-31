"""How does dpi interleave with the pen, now the detector is honest?

Geometry says dpi CANCELS in resolvable: grid = span/columns and
stroke_px = stroke_pt*dpi/72, so their product is span*stroke_pt/interior_pt.
The two-term loss decomposition therefore predicts, pre-registered:

  D1  span 20: retention flat 160->600 dpi; the 80->160 step visible.
  D2  wide spans, thin pen: retention climbs with dpi but saturates far
      below the narrow-span level (merging is dpi-invariant).
  D3  the dpi gain is larger for thin pens than thick ones.
  D4  floored recovery gains an EXTRA dpi benefit beyond retention (the
      quantisation floor shrinks as y-extent in px grows).

Suspected leak: antialiasing imposes a ~2 px minimum measured stroke, so at
low dpi a thin pen is effectively THICKENED in physical units and the
cancellation breaks exactly there.
"""

import collections
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
STROKES = [0.5, 2.0]
DPIS = [80, 160, 300, 600]
SIGMA = 10.0
OUT = os.environ.get("FIGDIG_OUT", "/tmp/figdig_dpi")
os.makedirs(OUT, exist_ok=True)


def load(path):
    d = np.genfromtxt(path, delimiter=",", skip_header=1)
    return d[:, 0], d[:, 1]


def render_extract(x, y, lo, hi, stroke, dpi, tag):
    m = (x >= lo) & (x <= hi)
    xs, ys = x[m], y[m]
    spec = S.PlotSpec(x_range=(lo, hi), stroke_pt=stroke)
    pdf, png = f"{OUT}/{tag}.pdf", f"{OUT}/{tag}.png"
    gt = S.render(xs, ys, spec, pdf)
    S.rasterize(pdf, png, dpi=dpi)
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
    pt = (left + tr.x_px) * 72.0 / dpi
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
        for st in STROKES:
            for dpi in DPIS:
                r = render_extract(x, y, lo, hi, st, dpi, f"d_{name}_{span}_{st}_{dpi}")
                if r is None:
                    print(f"MISS {name} span{span} {st}pt {dpi}dpi", flush=True)
                    continue
                nm, yrel, meas, extent = r
                grid = (hi - lo) / len(nm)
                resolv = grid * meas
                tol = max(1.5 * resolv, 0.05)
                fin = np.isfinite(nm) & np.isfinite(yrel)
                nmf, yf = nm[fin], yrel[fin]
                idx, _ = find_peaks(yf, prominence=1e-4, distance=1)
                ret = recovered(nmf[idx], truth, tol)
                pq = PK.pick(
                    nmf,
                    yf,
                    criterion="snr",
                    snr_sigma=SIGMA,
                    stroke_px=meas,
                    min_distance_px=1,
                    noise_floor=PK.trace_noise_floor(extent),
                )
                rq = recovered([p.position for p in pq], truth, tol)
                rows.append(
                    dict(
                        sample=name,
                        span=span,
                        stroke=st,
                        dpi=dpi,
                        meas=round(meas, 2),
                        eff_pt=round(meas * 72.0 / dpi, 2),
                        resolv=round(resolv, 3),
                        n_truth=len(truth),
                        ret=ret,
                        rec_q=rq,
                    )
                )
        print(f"done {name} span{span}", flush=True)

with open(f"{OUT}/dpi_interleave.json", "w") as f:
    json.dump(rows, f, indent=1)

agg = collections.defaultdict(lambda: [0, 0, 0, 0, 0.0, 0.0, 0.0])
for r in rows:
    k = (r["span"], r["stroke"], r["dpi"])
    a = agg[k]
    a[0] += r["ret"]
    a[1] += r["rec_q"]
    a[2] += r["n_truth"]
    a[3] += 1
    a[4] += r["resolv"]
    a[5] += r["meas"]
    a[6] += r["eff_pt"]

hdr = f"{'span':>5} {'pt':>4} {'dpi':>4} {'meas_px':>7} {'eff_pt':>6} {'resolv':>7} | {'ret%':>6} {'floored%':>8}"
print("\n" + hdr)
last = None
for (span, st, dpi), a in sorted(agg.items()):
    if last is not None and (span, st) != last:
        print()
    last = (span, st)
    n = a[2]
    print(
        f"{span:>5} {st:>4} {dpi:>4} {a[5]/a[3]:>7.2f} {a[6]/a[3]:>6.2f} "
        f"{a[4]/a[3]:>7.3f} | {100*a[0]/n:>5.1f} {100*a[1]/n:>8.1f}"
    )
