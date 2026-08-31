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

---

## Phase 1c part 2 — the structured vision ask, on 40 real corpus figures

Run against the live server (`muse-glimmer-30b-cuda`) at one request at a
time, ~80 s each. Images are sent as base64 data URIs, not paths: the server
refuses any path outside `model.vision_image_roots`, and this tool renders its
own crops.

```
scored figures            : 40
vision replied+parsed     : 38 (95%)
structured ask USABLE     : 21 (52%)      <- P10 target >=80%
tick-count disagreement   : 19/26 (73%)   <- P9 target >=20%
end-to-end calibration ok : 12/40 (30%), median residual 0.30 px
```

### P9 — CONFIRMED, and stronger than the prediction asked for

73% disagreement, far above the 20% floor and nowhere near the 5% that would
have collapsed the design to one source. But the count that matters is not the
disagreement rate, it is what agreement PREDICTS:

| CV x-tick count | vision agrees on count | calibrated |
|---|---|---|
| plausible (2–12), n=20 | 12 | **12 / 12 (100%)** |
| plausible, counts disagree, n=8 | — | 0 / 8 |

**Every successful calibration came from a case where the two independent
readings agreed on the tick count, and every such case succeeded.** The
two-source design is not redundancy — the agreement IS the acceptance test,
and it is free.

### P10 — REFUTED, with a caveat about its baseline

The structured ask reached **52% usable** against an 80% target, where usable
means a unit plus at least two numeric labels on each axis. Refuted.

The baseline half of P10 should NOT be read as refuted, because the two
numbers measure different things. This cohort's "figtext yields two ranges"
scored 85%, against the 28.9% quoted corpus-wide — but the cohort probe counts
any `N–N` pattern anywhere in caption plus description, including "5–10 mg"
and "Figure 3–4", and it never checks that a range belongs to an axis or
WHICH axis. It is a loose proxy that overstates; the 28.9% stands.

**Why 52% is low is itself the finding: only 26 of 40 figures carry two or
more numeric Y labels at all**, and `y_unit` came back null on 19 of 38.
For LIBS that is correct behaviour by the figures, not a failure to read them
— intensity is routinely printed as "a.u." with no numeric scale. Requiring a
calibrated y-axis demands something real spectra frequently do not provide and
do not need for peak POSITION. Scored x-axis-only, usable metadata is 65%.
This vindicates the existing decision to emit `relative_intensity` normalised
0–1 and make `intensity_data_units` optional.

### The real bottleneck is CV axis detection on real figures

| outcome | n | share |
|---|---|---|
| CV found no x-axis | 9 | 22% |
| CV over-detected (14–60 ticks) | 7 | 18% |
| CV plausible (2–12 ticks) | 24 | 60% |

The detector that scored 84% with zero errors on synthetics finds nothing or
over-counts on 40% of real figures. **The synthetic harness is too clean** —
it models one plot per page with one trace, while real figures are multi-panel
composites with subplot labels, colour legends, marker scatter and inset axes.
That is the honest limit of a self-built instrument, and the reason Gate B and
Gate C exist. Fixing it is Phase 1d work, not a tuning pass.

### A contamination finding that changes the technique routing

The 12 calibrated x-ranges expose the cohort selector, not the calibrator:

```
cm⁻¹  249 - 1750     <- Raman, not LIBS
s       0 - 10       <- a time series
None    0 - 0.7  /  0.2 - 1.0  /  1.05 - 2.62   <- not spectra at all
nm   1000 - 1600     <- NIR, not LIBS emission
nm    200 - 600 / 400 - 700 / 200 - 1000        <- genuinely LIBS
```

Only about half are LIBS emission spectra. The figtext prefilter assigns
technique by keyword — "nm" or "wavelength" appearing anywhere — which sweeps
in Raman (whose excitation is quoted in nm), absorbance spectra, and time
series from papers that merely mention LIBS.

**Correction to the design: technique must be assigned from the axis unit the
vision ask reads, not from a figtext keyword.** `x_unit` is the authoritative
signal (`cm⁻¹` → Raman, `2θ` → XRD, `nm` → LIBS or absorbance), with the
figtext guess kept only as a cross-check. The prefilter stays permissive; the
routing moves downstream of the ask.

---

## Do we actually need an accurate tick count?

Operator challenge, 2026-08-30: *"I am not sure if we really need an accurate
tick count if we have accurate ranges. Did this come up as required in
testing?"*

**It had not.** The 12/12-vs-0/8 cohort result is downstream of a pipeline
built to require tick matching, so it showed that the DESIGN needs tick
counts, not that the TASK does. The alternative was never implemented or
scored. Measured properly, over 5 samples × 15 arms against exact truth:

| method | correct | refused | **accepted-wrong** |
|---|---|---|---|
| N-point (tick-matched) | 63 | 12 | **0** |
| 2-point (extreme ticks) | 63 | 0 | **12** |
| 2-point + interior consistency check | 63 | 0 | **12** |
| frame edges + range, no ticks at all | 55 | 0 | **20** |

**The operator is right about accuracy and it is worth stating plainly: the
tick count buys none.** All three tick-using methods score exactly 63 correct.
On the real cohort the two agree to a median 0.028% of span (p90 0.07%).

What the count buys is **detectability of failure**. Two points fit a line
exactly by construction, so the residual is identically zero and nothing
signals that the extremes were spurious. The 12 cases N-point refuses are
precisely the 12 that 2-point gets wrong and cannot notice.

Three findings that were not obvious going in:

1. **The validator is the alignment MARGIN, not the residual.** What catches a
   bad fit is that no single pairing beats its alternatives — a runner-up
   comparison, the same mechanism that guards NCC relocation. Residual alone
   is worthless here, and that is why every accuracy failure in this module
   has arrived wearing a clean residual.
2. **A consistency check against interior ticks is VACUOUS.** Detected marks
   are uniform and printed labels are uniform, so any affine map aligning the
   extremes lands the interior marks on the label grid. Uniform maps onto
   uniform. The property that makes ticks findable makes them useless as a
   cross-check on an endpoint fit — this is why method 3 caught nothing.
3. **Range-only is the WORST option tested**, not a simplification: 20 wrong.
   The longest horizontal run is not reliably the data domain (it is the frame
   including margin, or it extends past the range), and with no ticks there is
   nothing to catch it.

**Ruling: keep tick positions, stop treating the count as the thing.** Use the
extremes for the answer and the full set purely as the over-determination that
licenses accepting it. An accurate count is not required; an over-determined
SET is.

Also corrected by this measurement: my own guess that coupling axis-line
detection to tick detection caused the cohort's 22% "no axis found". Detecting
lines WITHOUT requiring ticks recovers 32/40 against 31/40 — one figure. The
missing 20% is genuinely "no long contiguous run exists", a multi-panel and
broken-axis problem, not a tick problem.

**Open lever, not yet built:** a genuinely third source. The tick fit and the
vision labels are not independent, so neither can validate the other beyond
what the margin already tests. The figtext prose ("the x-axis runs 200–900 nm")
is independent of both, and could license accepting some of the 16% currently
refused.

---

## Phase 1d — curve extraction and peaks

Scored over 5 samples × 18 arms = 90 cells, against **continuous** source
positions rather than a binned reference, restricted to peaks the figure's own
sampling can separate.

```
extracted 63 / 90     refused 27  (annotation 10, multi-series 10, no calibration 7)
P1  |dx| median 0.508 px   p95 0.987 px      target med<=0.3, p95<=1.0   n=862 peaks
P2  precision 0.965   recall 0.704   F1 0.814    target F1>=0.95
P3  |dI| median 0.0094   p95 0.0310            target <=0.05
```

| # | verdict |
|---|---|
| P1 | **partially met** — p95 inside 1.0 px, median 0.508 above the 0.3 target |
| P2 | **REFUTED** at 0.814. Precision 0.965 is the half that matters; the shortfall is recall |
| P2b | **REFUTED** hard — see below |
| P3 | **CONFIRMED**, 5× inside the threshold |
| P11 | **CONFIRMED** — 12/12, series counted exactly for 1/2/3/4 |

### What limits recall is the PEN, not the sampling

Recall tracks stroke width and nothing else: 0.763 at 0.5 pt, 0.576 at 1.0 pt,
0.406 at 2.5 pt. And rendering at higher dpi does **not** recover it — recall
falls from 0.753 at 80 dpi to 0.277 at 600, because a finer grid resolves more
truth while the pen merges the same physical neighbours. This is the sibling of
the P6b finding: neither native resolution nor render dpi buys back peaks the
pen has already merged. `resolvable = grid × stroke` is the right model, and it
is what the precision block already emits.

### P2b: annotation is as dangerous as predicted

The plan called in-interior annotation the highest-probability killer. It is:
following the box tops gave **F1 0.13, four peaks recovered out of sixty, and
intensities out by 0.72** on a 0–1 scale. The pre-registered fallback applies —
refuse on `annotation_overlap` — and the yield cost is 10 of 90 cells.

The planned detector did not work. It looked for annotation as a LEFTOVER
component, but an arrow physically connects the box to the curve, so the two
become one component and nothing is left over. What does work is detecting the
effect rather than the cause: a box top puts the topmost-ink series on a long
flat shelf far above the trace, and a shelf ends with an abrupt jump where a
broad peak's apex does not.

### A process failure worth recording

Six of the Phase 1d bugs were found and fixed cleanly. The seventh — a
synthetic test fixture that returned no trace — I chased through **four**
changes to the extractor, each of which regressed it on real figures, until a
direct check showed coverage had gone from 1.00 to `None` on almost every real
arm. The mistake was tuning production code to satisfy a fixture instead of
first asking whether the fixture was representative. It was not: a
mathematically exact flat baseline is genuinely ambiguous with a frame rule,
and a lone ultra-steep Gaussian fragments under antialiasing. Neither occurs in
the corpus.

The fix was to test against a REAL spectrum — `tests/data/libs_sme1_spot1.txt`,
SME #1 at native sampling — after which the same code passed unchanged. Sample
DENSITY turned out to matter as much as shape: reduced to 4,000 samples that
spectrum extracts at 0.70 coverage, at native sampling 1.00, because near one
sample per rendered column the vertical connectors become thin antialiased
strokes that fall under the ink threshold.

**Rule for later phases: when a synthetic fixture and real data disagree,
check the fixture first.**

## Files

- `tools/figure_digitizer/{__init__,source,graphmeta,schema,digitize,synth,axes,curve,peaks}.py`
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
