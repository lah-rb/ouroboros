"""Survey: figure-derived precision vs the spectral resolution papers report.

For every LIBS spectrum figure whose VLM reading lists the x-axis TICK MARKS,
the tick extent is the axis span (a regex for "a to b nm" agreed with the
tick list in only 43% of figures -- it grabs feature sub-ranges and
understates span, which flatters d50). The real axes+curve extractor runs on
the stored PNG for interior width (grid) and stroke (pen); position
uncertainty is 0.5 px x grid (measured median |dx| 0.36-0.51 px); d50 comes
from the banked detection curve at the LIBS median line intensity. Each figure
is then joined to its paper's reported spectral resolution: the pack key
`spectral_resolution_nm` first, a prose regex second.
"""

import glob
import json
import os
import re
import sys
import warnings

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

from PIL import Image  # noqa: E402

from tools.figure_digitizer import axes as A  # noqa: E402
from tools.figure_digitizer import confidence as CF  # noqa: E402
from tools.figure_digitizer import curve as C  # noqa: E402
from tools.figure_digitizer.adapters.libs import MEDIAN_LINE_INTENSITY  # noqa: E402

WD = os.path.expanduser("~/corpora/ouroboros-spectra/databank")
OUT = os.environ.get("FIGDIG_OUT", "/tmp/figdig_survey")
os.makedirs(OUT, exist_ok=True)
LIBS = re.compile(r"\blibs\b|laser[- ]induced breakdown", re.I)
TICKS = re.compile(
    r"tick(?: mark| label)?s?[^.\n]{0,40}?((?:\d{3,4}(?:\.\d)?\s*,\s*){2,}\d{3,4}(?:\.\d)?)",
    re.I,
)
RES = re.compile(
    r"(?:spectral\s+)?resolution\s*(?:of|is|was|:|~|≈|about|approximately)?\s*"
    r"(?:better than|below|<|≤)?\s*~?\s*(\d+(?:\.\d+)?)\s*(nm|pm|cm\s*-?\s*1|cm⁻¹)",
    re.I,
)


def reported_resolution() -> dict:
    rep = {}
    for f in glob.glob(f"{WD}/dataset/*.json"):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        v = (d.get("data") or {}).get("spectral_resolution_nm")
        if isinstance(v, (int, float)) and 0 < v < 50:
            rep[os.path.basename(f)[:-5]] = ("pack", float(v))
    for p in glob.glob(f"{WD}/figtext/*.json"):
        k = os.path.basename(p)[:-5]
        md = f"{WD}/markdown/{k}.md"
        if k in rep or not os.path.exists(md):
            continue
        txt = open(md, encoding="utf-8", errors="ignore").read()
        if not LIBS.search(txt[:200000]):
            continue
        vals = []
        for m in RES.finditer(txt):
            x = float(m.group(1))
            u = m.group(2).lower()
            if "cm" in u:
                continue
            if u == "pm":
                x /= 1000
            if 0.005 <= x <= 5:
                vals.append(x)
        if vals:
            rep[k] = ("prose", float(np.median(vals)))
    return rep


def main():
    rep = reported_resolution()
    rows = []
    for p in glob.glob(f"{WD}/figtext/*.json"):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if not LIBS.search(json.dumps(d)):
            continue
        k = os.path.basename(p)[:-5]
        for f in d.get("figs") or []:
            t = (f.get("figtext") or "") + " " + (f.get("caption") or "")
            if not re.search(r"spectr", t, re.I):
                continue
            m = TICKS.search(t)
            if not m:
                continue
            vals = [float(v) for v in re.findall(r"\d{3,4}(?:\.\d)?", m.group(1))]
            span = max(vals) - min(vals)
            if not (3 <= span <= 1000):
                continue
            png = f"{WD}/figures/{k}/{f.get('fig')}"
            if not os.path.exists(png):
                continue
            try:
                mask = A.ink_mask(np.asarray(Image.open(png).convert("L"), dtype=float))
                xa, ya = A.find_axes(mask)
                w = xa.span_px[1] - xa.span_px[0] - 2
                if w < 50:
                    continue
                top, bot = int(min(ya.span_px)) + 1, xa.position_px - 1
                tr = C.extract_trace(
                    mask[top:bot, xa.span_px[0] + 1 : xa.span_px[1] - 1]
                )
                if tr is None:
                    continue
                pen_px = tr.stroke_px
            except Exception:
                continue
            g = span / w
            pen = g * pen_px
            rows.append(
                dict(
                    key=k,
                    fig=f.get("fig"),
                    span=span,
                    width_px=w,
                    grid=g,
                    pen_px=pen_px,
                    pen=pen,
                    pos_unc=0.5 * g,
                    d50=CF.d50(g, pen, MEDIAN_LINE_INTENSITY),
                    reported=rep.get(k, (None, None))[1],
                    rep_src=rep.get(k, (None, None))[0],
                )
            )
    json.dump(rows, open(f"{OUT}/precision_survey.json", "w"), indent=1)
    sp = np.array([r["span"] for r in rows])
    dd = np.array([r["d50"] for r in rows])
    pu = np.array([r["pos_unc"] for r in rows])
    print(f"figures: {len(rows)}   papers with a reported resolution: {len(rep)}")
    print(
        f"  span median {np.median(sp):.0f} nm | d50 median {np.median(dd):.2f} nm "
        f"| pos_unc median {np.median(pu):.3f} nm"
    )
    J = [r for r in rows if r["reported"]]
    if J:
        rj = np.array([r["reported"] for r in J])
        dj = np.array([r["d50"] for r in J])
        pj = np.array([r["pos_unc"] for r in J])
        print(
            f"  joined {len(J)}: pos_unc<=reported {100 * np.mean(pj <= rj):.0f}% | "
            f"d50<=reported {100 * np.mean(dj <= rj):.0f}% | median d50/reported {np.median(dj / rj):.1f}x"
        )


if __name__ == "__main__":
    main()
