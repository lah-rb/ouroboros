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

---

## Phase 1b — the instrument, and the ground truth it runs on

Built BEFORE the extractor on purpose. An extractor written first gets tuned
against figures somebody eyeballed, and every number it then produces is
unfalsifiable.

### The operator's data (`~/Downloads`, 1.7 GB)

Far richer than the plan assumed — it is not just spectra, it is a **paired
LIBS↔XRF calibration series**.

| set | contents |
|---|---|
| `HandheldLIBS/` | 5 SME iron-oxide pellets × 6–7 spots × ~40 shots + `average.csv`. **1,730 spectra.** |
| `GroundTruthXRF/` | the same pellets by XRF: `result.yaml` with quantitative composition per element |
| `Au_Summer2024LIBS/` | 268 spectra over 14 gold-bearing localities, plus rendered PNGs and `ploting.py` |

All spectra share one format: `wavelength,intensity`, **23,431 points,
180–961 nm at 0.0333 nm/px**. Iron-oxide spectra carry ~245 peaks at 5%
prominence; the Au spectra ~165. Recognisable lines land where they should
(Ca II 393.3/396.8, Na D 589.0, the Fe cluster near 373–382).

**The pairing is the valuable part.** Directory names encode Fe content, and
every one has matching LIBS:

```
SME #4  Fe=46.870%   7 spots   371 spectra
SME #1  Fe=57.843%   6 spots   246 spectra
SME #3  Fe=62.881%   7 spots   343 spectra
SME #2  Fe=76.429%   7 spots   343 spectra   (site 2)
SME #2  Fe=77.003%   7 spots   343 spectra   (site 1)
SME #6  Fe=86.009%   7 spots   427 spectra
```

Five Fe concentrations spanning 46.9–86.0% with hundreds of spectra each is a
real calibration series — which is exactly the "intensities vary because they
carry information" signal the corpus currently cannot teach. It is a Phase 2
asset (it needs multi-series and legend semantics), recorded here so it is not
rediscovered later.

**The Au PNGs are not usable as digitiser targets**, and it is worth saying
why so nobody tries: `ploting.py` draws with `ax.plot(x, y, 'o')`, so they are
scatter markers, not traces — published figures are lines. They are useful for
a different reason: `mplcursors` annotations bake **exact peak positions and
intensities** into the image, so they self-label, and they exhibit every
hazard the harness models (boxed frame, `1e10` exponent, 11 annotation boxes
with arrows crossing the plot).

### The renderer

`synth.py` renders a spectrum to a vector PDF via pymupdf and returns exactly
what it drew: the polyline, the axis mapping, tick values and positions, the
exponent, the stroke width, and where the drawn axis stops. Rendering the full
23,431-point survey takes 0.26 s. Nothing is downsampled — collapsing ~39 data
points into one column IS the rasterisation loss under measurement, and
downsampling here would hide it.

Independently toggleable arms: frame (2/4 spines), ticks (in/out/both,
major±minor), tick density, gridlines, log y, exponent multiplier, axis
extends past last tick, stroke width, annotation boxes with arrows into the
tallest peaks, legend, x-range (survey vs zoom), render dpi, JPEG quality.

At default geometry a full survey samples 0.93 nm/px at 160 dpi and 0.25 at
600; a 390–410 nm window samples 0.024 nm/px. That single fact is the whole
resolution argument, and it is now an assertion rather than a claim.

Four more bugs, all caught by the tests rather than by looking:

1. **The first render came out blank** — no axes, no trace. pymupdf's
   `finish()` only closes a path group; `commit()` is what writes it. Every
   arm would have scored a perfect zero against an empty page.
2. **Tick density was not actually an arm.** Choosing "the first step above the
   raw spacing" returned the same 4 ticks for every request from 5 to 9 on a
   180–961 nm range, so a sweep of 3/5/7/9 would have measured one setting
   four times. Now the step is chosen by closest resulting count.
3. **`axis_extends` was unmodelled in ground truth.** The drawn axis ran past
   the interior but nothing recorded where it stopped — which is precisely the
   quantity a digitiser would misread as the data range. Now `x_axis_end_pt`.
4. A dataclass field with a default placed before non-defaulted ones.

---

## Phase 1c — axis calibration (the unbounded error term)

Scored over **5 SME samples × 15 adversarial arms = 75 cells**, against exact
truth, reported per sample so a headline cannot hide one sample's failure.

| outcome | count | |
|---|---|---|
| accepted, correct | **63 / 75 (84%)** | every one sub-pixel: 0.39–0.68 px |
| refused | 12 / 75 (16%) | honest outcome, not an error |
| **accepted and WRONG** | **0 / 75** | the number that matters |

Per sample: 13/13/12/14/11 accepted, no sample an outlier.

Y-axis, measured separately because the log arm lives there:

```
linear y-axis accurate (<1% of range): 10/10
log detection recall:                   5/5
FALSE log calls on linear axes:         0/10
```

### P4 verdict — partially met

Pre-registered: *"max residual ≤ 1.0 px on ≥90% of cases; log detection recall
≥95% with zero false positives on linear."*

- Residual criterion: **met** — every accepted case is far inside 1.0 px.
- Log criteria: **met** — 5/5 recall, 0 false positives.
- The ≥90% rate: **not met at 84%**, and the shortfall is entirely
  REFUSALS. Since the refusals are the guard working, this is recorded as
  partially met rather than refuted; the honest headline is *zero wrong
  answers at an 84% yield*, and whether 16% refusal is acceptable is a yield
  question for later phases, not a correctness one.

### Six bugs, and a pattern worth naming

Five of the six produced a WRONG ANSWER THAT LOOKED RIGHT — a low residual on
a calibration that was badly off. That is the signature failure of this module
and the reason residual alone can never be the acceptance test.

1. **Tick length is dpi-relative, not a pixel count.** A 4 pt tick is 9 px at
   160 dpi and 33 px at 600. Fixed bounds found no ticks above ~300 dpi, then
   rejected every axis for having none.
2. **The trace touching the axis fakes inward ticks.** Short perpendicular
   runs at many columns, indistinguishable from ticks one at a time — and
   obviously not ticks once required to be regular.
3. **A proportional grid tolerance lets a wrong step survive.** Seeded on a
   frame corner, a chain drifted 21 px per interval and still fit inside 18%
   of a 128 px step, swallowing the true 107 px grid. The tolerance is now
   absolute, because ticks are placed to sub-pixel accuracy.
4. **Minor ticks are regular too.** Kept by the regularity filter and paired
   ordinally against major labels, they put the calibration out by **700 nm**
   at a 0.37 px residual. Fixed by length clustering — which must run AFTER
   the regularity filter, never before: applied to raw marks it reads its
   statistics off a population contaminated by trace touch points and evicts
   the real ticks, taking a working case from 8 correct ticks to 4 wrong ones.
5. **Ordinal truncation shifts every pair when a tick is missing in the
   MIDDLE.** A sliding contiguous window cannot express a gap; both runs are
   now indexed onto their own grids so alignment is a single integer offset
   and holes are free.
6. **A y-axis calibrated upside down.** Page y grows downward while the
   plotted quantity grows upward, so sorting both sequences ascending mirrors
   the axis — and evenly spaced ticks fit a straight line just as well
   reversed. All ten rendered y-axes were wrong at a 0.31 px residual, and the
   count guard cannot see it because the counts match.

The defence that caught the worst case is the same margin logic that guards
relocation: when tick and label counts disagree, the winning alignment must
beat the runner-up. Without it, a thick trace hid five of eight ticks, the
best of six alignments won on noise, and the calibration came out **6,343 nm
off at a 0.28 px residual**. With it, that case refuses — and so does one case
it would have got right. That asymmetry is deliberate.

**Not yet done in 1c:** the structured vision ask for tick LABELS, and with it
P9 and P10. The CV half supplies tick POSITIONS; label values are still taken
from ground truth. Deferred rather than skipped, because the vision server is
serving the live mission's figtext lanes.

## Files

- `tools/figure_digitizer/{__init__,source,graphmeta,schema,digitize,synth,axes}.py`
- `tests/test_figure_digitizer_{source,graphmeta,schema,synth,axes}.py` (52 tests)
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
