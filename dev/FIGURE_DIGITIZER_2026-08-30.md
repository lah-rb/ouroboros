# Figure digitizer — can a published plot be read as a matrix?

**Question.** Spectrum plots are the universal intake form for this data:
papers that dump raw spectra are rare, papers that print a figure are
everywhere. Today those figures reach the corpus only as figtext — a vision
model's prose, lossy in a way nobody can bound. Read as a MATRIX instead, the
loss concentrates in the data→pixels rasterisation step, which is bounded and
measurable. Is the substrate good enough to try?

**Phase 1a scope (this doc).** Relocation and tiering only: find each stored
crop in its source PDF, choose the best pixel source, and record what that
source can resolve. Axes, curve tracking and peaks are later phases. P5 and P6
gate all of them, which is why they are measured before any extractor exists.

Nothing here touches the running mission. Reads the corpus; writes only
`databank/figdata/`.

---

## The correction that reshaped the plan

I told the operator "two-thirds of these figures are vector, which skips the
CV pipeline entirely." **That was wrong**, and it was wrong in the most
ordinary way: I counted `get_drawings()` path segments without checking their
SHAPE. They are text glyph outlines — a typical "longest path" ran 113
segments over a 55×6 pt box, non-monotone in x.

A curve-shaped detector (≥40 vertices, ≥120 pt x-extent, ≥30 pt y-extent, ≥90%
monotone-x) finds plot curves recoverable as vector paths in **2 of 24 LIBS
papers (8%)**, and both are theses — LaTeX/gnuplot output, not journal
typesetting. Measured again over this survey's figures: **10 of 160 (6.2%)**.

So the raster CV path is tier 1, not the fallback. The vector tier's real value
is as ground truth: a curve recovered there has zero rasterisation loss, so
rendering the same rect and re-extracting measures exactly what rasterisation
costs (Gate B, Phase 1e).

---

## Pre-registered predictions and verdicts

Predictions were written into the plan before the survey ran.

| # | Prediction | Result | Verdict |
|---|---|---|---|
| P5 | NCC relocation recovers (page, x0, y0) at r ≥ 0.98 for ≥ **95%** of LIBS crops | **190/190 = 100%**; min ncc 0.9847, median 0.9972 | **CONFIRMED** |
| P6a | **Tier N available** for ≥ **70%** of LIBS spectrum figures | **128/160 = 80.0%** | **CONFIRMED** |
| P6b | ...at median resolution gain ≥ **2.0×** | **median 1.79×**; only 9.3% reach 2× | **REFUTED** |

P1–P4 and P7–P11 belong to later phases and are not yet scored.

### Survey

60 LIBS-candidate papers (seed 17), ≤6 candidate figures each, 190 figures.

```
status:  sourced 160 | not_a_plot 27 (full-page leak) | rejected 3 (ambiguous)
tier among the 160 sourced:
  native_raster     128   80.0%
  render_vector      17   10.6%
  vector             10    6.2%
  render_resampled    5    3.1%
effective dpi of native sources: median 286, p90 309, max 1158
resolution gain: median 1.79x, p75 1.88x, p90 1.93x, >=2x 9.3%, >1x 73.3%
relocation: median ncc 0.9972, median margin 0.588
```

---

## What P6b's refutation actually means

The gain distribution is not broad and disappointing — it is **tight and
pinned**. Native sources sit at a median 286 dpi with p90 309, because
publishers standardise on 300 dpi artwork. Against a 160-dpi crop that is
300/160 = 1.875, and the measured median of 1.79 is that convention showing
through. The corpus-median "2.8×, max 21×" figure from the earlier small probe
does **not** survive the larger sample and should not be quoted again.

Consequence, stated plainly: at 524 px median crop → ~940 px native, a
200–900 nm survey samples at ~0.75 nm/px. That is a real improvement over
1.67 nm/px, and it is still nowhere near splitting Mo I 379.83 from Fe I
379.85 (~0.02 nm). **Wide survey plots stay unresolvable at the line level;
zoomed windows are where assignment can work.** Native extraction is a 1.8×
improvement, not a category change — which is exactly what the
resolution-derived tolerance in the LIBS adapter exists to express, and why
"unresolvable at this figure's sampling" has to be a first-class output rather
than a caveat in a doc.

This does not threaten position accuracy for well-separated peaks. It
threatens ASSIGNMENT, and the adapter is designed to refuse there.

---

## Four bugs found by measurement, all mine

Each was caught by a gate rather than by review, and each would have degraded
the corpus quietly.

1. **A 40-page sweep cap silently truncated relocation.** The first survey
   reported an 88% relocation rate. 26 of the 30 failures were figures on page
   41 or later of a long paper — reported as "could not place this" when the
   sweep had never looked. Removing the cap took the rate to 100%. Ranking
   pages by dimension match keeps the usual cost at one or two correlations
   regardless of document length, so the cap bought nothing it cost.

2. **Full-page crops all bound to the same wrong page.** Every page of a
   uniform document matches a page-sized crop's dimensions, so returning on the
   first dimension match bound the crop to whichever page sorted first — two
   different crops both claimed page 4. The shape says WHAT the crop is; only
   correlation says WHICH page. Fixed with a direct Pearson r at equal size.

3. **A native image smaller than its crop rect was discarded.** Crop boxes
   routinely take in axis labels and margin that are page text, so the
   placement sits INSIDE the rect and covers well under 95% of it. Requiring
   the image to cover the rect threw away a real 300-dpi source and fell back
   to a 160-dpi render. Now either containment direction qualifies, with a 40%
   floor so a logo inside a figure box cannot win.

4. **The figtext prefilter vetoed on the whole description.** One passing word
   decided: "LIBS/ChemCam targets displaying Ca-sulfate signature (solid
   spectra)" was rejected because "micrograph" appeared later in the same
   paragraph. **1,929 otherwise-qualifying figures** were being dropped. The
   veto now reads only the identity zone — caption plus the opening 240
   characters, where the model states what the image IS. Recovered 1,243
   candidates; the LIBS cohort went 896 → 1,025 papers.

A fifth, caught by a unit test rather than the corpus: `_polylines` aborted an
entire path on hitting a rectangle item, losing the trace whenever a plot's
frame and curve were emitted as one drawing.

---

## Two free results

**The full-page leak identifies itself.** 27 of 190 figures (14%) are the known
"full-page render leaks in as a low-numbered fig_NN" artifact, and they are
unambiguous: NCC exactly 1.0 at page-equal dimensions. A zero-cost `not_a_plot`
filter falls out of a step that had to run anyway.

**The margin guard works.** 3 figures relocated at ncc ≥ 0.98 but with a
runner-up too close to separate — repeated panels on one page, risk #2 in the
plan. They are refused as ambiguous rather than bound to a coin-flip page.

---

## Where this leaves the build

Both gating predictions clear their thresholds on availability, so Phase 1b
(the synthetic instrument) and onward proceed. The one refuted half changes an
expectation, not a design: it sharpens how loudly the tool must refuse
assignment on wide survey plots, and it raises the value of Gate B's dpi sweep,
which now has to answer where the refusal floor sits given that native
extraction buys 1.8× rather than 2.8×.

Standing epistemic rule for everything downstream (`interconnect.py:31-40`):
the reference catalogue is a grounding point, not an authority. A digitised
position is never shifted toward a catalogue value; disagreement is flagged,
never corrected.

## Files

- `tools/figure_digitizer/{__init__,source,graphmeta,schema,digitize}.py`
- `tests/test_figure_digitizer_{source,graphmeta,schema}.py` (23 tests)
- Artifacts: `databank/figdata/<key>.json`, run reports in `figdata/_runs/`

## Repro

```
.venv/bin/python -m tools.figure_digitizer.digitize --survey \
    --technique libs --limit 60 --max-figs 6
```

## Unrelated defect noticed

`uv run ruff check .` cannot run repo-wide: `dev/archived_runs/step37_boss/
ruff.toml` and `dev/archived_runs/mistral_boss2/ruff.toml` (committed in
18bf2d5) use a `[tool.ruff]` header, which is valid only inside
`pyproject.toml`. Ruff aborts the tree walk on them. Checking explicit source
paths works. Not fixed here — it is not this change's to make.
