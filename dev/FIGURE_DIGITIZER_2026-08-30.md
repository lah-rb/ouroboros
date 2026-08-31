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

---

## The detection criterion was the wrong KIND of threshold (2026-08-31)

Operator challenge: peaks are being lost across the whole spectrum including
quiet zones with visually isolated peaks, so the criterion for calling a peak
may be muddled. **Correct**, and a framing of mine needed retracting with it:
of the 23 peaks separated by more than the 1.87 nm resolution limit, the
shipped rule recovered 12. Calling that "close to the ceiling" was wrong — it
was discarding **half of what the figure could physically resolve**.

**What was shipped:** `prominence >= 5% of the full intensity range`. That is
not a standard criterion, and it is hostage to the brightest feature on the
page — with the tallest line near 25,000 counts it imposes a flat ~1,250-count
floor everywhere, so a clean isolated line in a quiet region is discarded
because of a large line hundreds of nm away.

**What standard practice is:** detection is defined against LOCAL noise.
IUPAC/NIST put the limit of detection at 3 sigma over local background and the
limit of quantitation at 10; LIBS line identification conventionally uses
SNR >= 3 over the local continuum. Nothing standard is relative to the global
range.

Shipped: `criterion="snr"` at 5 sigma, on the conservative side of the IUPAC
LOD. Background is a rolling minimum smoothed by a median; noise is a rolling
MAD scaled by 1.4826. Both windows are expressed in SAMPLES tied to the stroke
rather than in nm, because a fixed nm window that suits a 780 nm survey is
wider than a zoomed panel's whole range. The old rule stays available as
`criterion="prominence"` for comparison only.

Re-scored over 5 samples × 14 arms:

| criterion | peaks found | resolvable recovered | \|dx\| median | \|dI\| median |
|---|---|---|---|---|
| prominence (old) | 2,246 | 893/1271 (**70%**) | 0.545 px | 0.0090 |
| local SNR 5σ (new) | 4,764 | 1041/1271 (**82%**) | 0.538 px | 0.0104 |

Position accuracy is unchanged and intensity accuracy stays 5× inside the P3
threshold.

### Two validations that turned out VACUOUS

The extra peaks cannot be shown to be real, and both attempts to show it
failed against a random control — the second one only after it had produced a
result I was ready to act on.

- *"every found peak matches a local maximum in the source"* scored 1.000.
  Random positions score **0.980**. Lift 1.02×. The source has 1,725 local
  maxima over 600 nm, so nearly any position matches.
- *"every found peak matches a NIST line of an element the XRF confirms in
  this pellet"* scored 0.91. Random scores **0.78**. Lift 1.18×.

The second is a finding in its own right. For the 20 elements this pellet
contains there are **15,076 NIST lines between 200–800 nm, one every 0.04 nm**,
against a position uncertainty of ~0.5 px (≈0.47 nm). **Peak identification at
survey resolution is close to unfalsifiable** — four-fifths of arbitrary
wavelengths match a real atomic line. That is independent support for the
adapter refusing assignment rather than choosing a candidate, and it is the
anti-circularity concern in the plan arriving from a direction nobody expected.

The change is therefore justified on the verifiable half only: the standard
criterion, dynamic-range independence, and 70% → 82% on peaks whose positions
can be checked. It makes IDENTIFICATION harder, not easier, which points the
same way as everything else here — survey figures should yield positions with
assignment refused; zoomed panels are where identification is legitimate.

---

## A half-pixel in the tick geometry — P1 now fully confirmed

Chasing whether the Savitzky-Golay position shift was correctable turned up a
systematic bias in the RAW positions: mean signed error +0.399 px, i.e. every
extracted position sat about half a pixel to the right of truth.

**Cause.** A tick occupying columns `a..b` covers the continuous span
`[a, b+1)`, so its centre is `(a+b)/2 + 0.5`. `_marks_on_side` returned
`np.mean(group)` without the half — while the TRACE's samples were already
being reported at column centres. The calibration's pixel origin and the
trace's therefore disagreed by exactly half a pixel, and everything read half
a pixel right.

Fixed. Re-scored over 5 samples x 18 arms:

| | before | after |
|---|---|---|
| P1 \|dx\| median | 0.508 px | **0.199 px** |
| P1 \|dx\| p95 | 0.987 px | **0.752 px** |

**P1 goes from partially met to CONFIRMED** — both the median (target ≤ 0.3)
and the p95 (target ≤ 1.0) now clear. It is the first pre-registered verdict
in this project to improve on a bug fix rather than a threshold change.

### The second bias term is the RENDERER's, and is not corrected

A residual remains and it varies with dpi: +0.07 px at 160, −0.14 at 300,
−0.57 at 600. Two candidate fixes were tested and both rejected:

- **Undoing the pen morphologically.** The topmost-ink series is close to a
  grey erosion of the drawn path by the pen, so dilating back should undo it.
  Measured: bias at 600 dpi improves (−0.359 → −0.214) but median error gets
  worse at every dpi and a quarter of the peaks are lost.
- **Baking an empirical stroke-dependent offset.** Rejected on evidence, after
  the operator asked whether the residual might be machine-specific. Changing
  ONLY the rasteriser's antialiasing level — same geometry, same data — moves
  the bias by 0.2 px:

  ```
  aa_level=0:  bias -0.228   med|err| 0.479
  aa_level=4:  bias -0.027   med|err| 0.369
  aa_level=8:  bias -0.055   med|err| 0.411
  ```

  So the residual tracks the rasteriser, not the figure. Fitting a constant to
  it would be fitting to MuPDF at one antialiasing setting, and 80% of the
  corpus is `native_raster` rasterised by publishers with unknown settings. It
  would not transfer.

Recorded as `RESIDUAL_POSITION_BIAS_PX = 0.25` and reported as uncertainty
rather than subtracted. **A bias that depends on who drew the figure is
uncertainty, not a correction.**

### Savitzky-Golay, revisited in this light

The SG shift is largely correctable, which was the operator's question. SG
does not randomise position — it trades scatter for bias: raw error is mean
+0.399 sd 0.754, SG error is mean +0.545 sd **0.328**. One reference peak of
known wavelength takes SG from 0.584 to 0.338 px, and a perfect offset reaches
0.258. LIBS figures routinely label a line, so the reference is usually
printed on the figure. Still not adopted by default, but the objection that
its shift is irrecoverable does not hold.

---

## Reference alignment — correcting a wrong axis, not a right one

Operator direction: take the reference from the same LLM pass that supplies
the other figure metadata, and fall back on Ca II 393 when a figure labels
nothing.

**Mechanism.** The tick fit gives `nm = slope x px + intercept`. Alignment
finds the detected peak nearest a reference wavelength and adds ONE offset to
every peak — algebraically an intercept correction, with the slope untouched.
That bounds what it can do: a single reference fixes an axis OFFSET exactly
and cannot fix an axis SCALE error, which would need two references fitted
jointly. Nonlinearity is neither's job; the calibration residual gate refuses
it.

It is calibration, not identification, and the distinction is what keeps it
inside `interconnect.py:31-40`. One offset applied to every peak leaves the
relative structure untouched and stays falsifiable — a mismatched reference
moves everything and the disagreement shows. Snapping each peak to its nearest
catalogue value would manufacture agreement instead.

**The first result was that it made things worse**: median position error
0.183 -> 0.343 nm on a correctly calibrated axis. After the tick-centre fix
the residual bias is -0.029 px while the single-reference estimate carries
sd 0.638 px, so correcting a near-zero bias injects the reference's own noise.
A synthetic axis is perfect by construction, so that test could only ever show
the cost. Injecting a known axis error shows the benefit:

| axis error | raw | printed labels | Ca II fallback |
|---|---|---|---|
| 0.0 nm | 0.183 nm | **0.183** (0/10 applied) | 0.204 (4/10) |
| 0.5 nm | 0.454 | 0.416 | 0.450 |
| 1.0 nm | 0.945 | **0.205** | 0.603 |
| 2.0 nm | 1.880 | **0.276** | 0.482 |
| 5.0 nm | 1.915 | 1.150 (4/10) | 0.976 |

Printed labels do nothing on a correct axis and recover ~7x on a wrong one.
The Ca II fallback is strictly weaker, as it should be — a catalogue line
assumed to be the tallest thing nearby is a weaker claim than the authors'
own annotation, and the artifact records which was used.

**Three refusals, each added because a measurement demanded it:**

* **dead-band** (0.25 resolvable widths) — without it, correcting a
  well-calibrated axis was worse than leaving it alone;
* **cap** (2 widths) — an implied shift larger than the figure can plausibly
  be wrong by means the reference matched the wrong line;
* **agreement** (0.75 widths) — independent references must tell the same
  story, which is how a large axis error is caught: it fails by locking onto a
  NEIGHBOURING line and implying a plausible but wrong shift.

### Multi-reference: fitting the slope as well as the offset

Two or more independent references pin the SCALE, which is the error a single
reference is blind to. Against an axis given both a 1 nm offset and a stretch
about 200 nm:

| offset | stretch | end error | raw | 1 ref | 3 refs |
|---|---|---|---|---|---|
| 0.0 nm | 1.0000 | 0.00 nm | 0.183 | — | **0.186** (1/10 applied) |
| 1.0 nm | 1.0000 | 1.00 nm | 0.945 | 0.603 | **0.218** (9/10) |
| 1.0 nm | 1.0020 | 2.56 nm | 1.585 | 0.792 | **0.213** (10/10) |
| 1.0 nm | 1.0050 | 4.90 nm | 1.798 | 1.101 | **0.345** (9/10) |
| 1.0 nm | 1.0100 | 8.80 nm | 1.928 | 1.571 | 1.732 (5/10) |

A correct axis is still left alone; a wrong one is corrected to within a
quarter of a resolvable width up to roughly the reference search window, and
beyond that the guards refuse rather than corrupt. Three references and four
perform the same, so three is enough. The ceiling is structural: the search
window is 3 resolvable widths (7.46 nm here), and an axis wrong by more than
that AT a reference cannot find its own line.

**Two ordering bugs, both found by tests rather than review.**

The raw shifts were being required to agree BEFORE the fit. But a genuine
scale error *is* different shifts at different references — that check
rejected exactly the case the joint fit exists to handle. Agreement is now
tested afterwards, as the residual of the fit. Removing it took the 1.005
stretch case from 1.593 nm at 2/10 applied to 0.345 nm at 9/10.

And with EXACTLY two references the fit is exact and its residual is
identically zero, so the scale bound is the only thing between a mismatched
pair and a confident wrong answer. At the original 5% a pair implying a 2.7%
stretch sailed through — a 21 nm error over a 780 nm survey. Tightened to 2%.

**A design bug the agreement check found in itself.** Ca II H and K are
3.481 nm apart — inside the 3-width search window at survey resolution — so
both locked onto the SAME peak and then "disagreed" by exactly their own
separation. Every cell refused with `references disagree by 3.4810`.
References closer together than the search window are now pruned to one, and
the alignment records `single reference, no cross-check` so a weaker claim is
never presented as a stronger one.

Also fixed: the label regex matched a three-digit run inside a longer number,
turning `0.387 wt%` into 387 nm and `2024` into 202 nm.

---

## What "resolvable recovered" actually counts — a reporting correction

Operator question: how dramatic is the loss between 5 sigma on the image and 5
sigma on the raw data, and is that the "resolvable" column being quoted?

**It is not.** The detector was switched to 5 sigma but the TRUTH SET was left
on the old prominence criterion, and that was never flagged. Measured over
five SME survey figures at 160 dpi:

```
peaks by 5 sigma on the RAW spectrum (0.033 nm sampling) :  6,707
peaks by prominence 0.05 on the RAW spectrum             :  1,102   <- truth-set basis
peaks by 5 sigma on the IMAGE trace  (0.93 nm sampling)  :    337
```

| truth set | raw peaks | separable in the figure | recovered |
|---|---|---|---|
| prominence >= 0.05 (**the column quoted throughout**) | 1,102 | 97 (8.8%) | 78/97 = **80%** |
| local 5 sigma (matches the detector) | 6,707 | 19 (0.3%) | 1/19 = **5%** |

End-to-end against every raw 5-sigma peak: **1 of 6,707**.

So "80% of resolvable peaks" means *of the strong, well-separated peaks, we get
80%* — a far narrower claim than it reads as. The figure preserves roughly 5%
of the features the raw data resolves, and of the 0.3% that survive the
separability filter almost none is recovered, because a peak detected at 5
sigma in a 40-shot average can be very small and does not clear the image's own
5 sigma after rasterisation.

**The counter-caveat.** 6,707 is not obviously the right denominator either: 5
sigma on a 40-shot average is a permissive bar, one peak every 0.12 nm. It is
not physically absurd — NIST lists one line every 0.04 nm for the 20 elements
this pellet contains — but the number is sensitive to how the source was
averaged.

**Rule going forward: state the truth set with every recovery figure.** The
choice moves the headline by more than an order of magnitude, and quoting the
most favourable one without saying so is the kind of silent bias this project
exists to avoid.

---

## Survey vs zoom, on the same window — the acceptance case

Everything below is restricted to 390-410 nm, on the same spectra, with the
same 5-sigma criterion applied to the raw data and to the extracted trace.
The ONLY thing that differs is the figure the data was drawn into.

| figure | nm/px | resolvable | raw 5σ peaks | separable | image peaks | recovered |
|---|---|---|---|---|---|---|
| survey 180-961 nm | 0.933 | 2.612 nm | 217 | **0 (0%)** | 19 | 0/217 = **0.0%** |
| zoom 390-410 nm | 0.024 | 0.072 nm | 200 | **200 (100%)** | 210 | 198/200 = **99.0%** |

This is not a difference of degree. A survey figure of this spectrum resolves
NOTHING in the window — every one of the 217 lines the instrument recorded sits
closer to a neighbour than the 2.6 nm the pen can separate. The same data drawn
as a 20 nm panel resolves all 200 and the digitiser recovers 198.

Two honest caveats. The raw counts differ (217 vs 200) because each figure's
5-sigma threshold is computed against its own dynamic range, and a zoomed panel
has a different one — a real property of the figures, not an artefact, but it
means the two denominators are not identical. And the zoom finds 210 peaks
against 200 raw, so roughly a dozen are spurious; precision there is ~94%.

### Tightening to 10 sigma changes nothing

The IUPAC limit of QUANTITATION rather than detection, applied to raw and
image alike, in the same window:

| figure | sigma | raw | separable | recovered | end-to-end |
|---|---|---|---|---|---|
| survey | 3 | 219 | 0 (0%) | — | 0.0% |
| survey | 5 | 217 | 0 (0%) | — | 0.0% |
| survey | 10 | 214 | 0 (0%) | — | **0.0%** |
| zoom | 3 | 207 | 207 (100%) | 204/207 | 98.6% |
| zoom | 5 | 200 | 200 (100%) | 198/200 | 99.0% |
| zoom | 10 | 190 | 190 (100%) | 189/190 | **99.5%** |

A stricter bar was expected to thin the line list and so reduce crowding. It
does not: 219 -> 214 is a 2% cut, because on a 40-shot average the peaks sit
far above even 10 sigma. The threshold was never the binding constraint — the
lines are real, strong and unambiguous in the raw data, and the survey
destroys them purely by drawing.

Pushed to absurdity to find the ceiling, one sample, same window:

```
sigma   raw peaks   density    separable
    3          43   0.465nm    0/43  (0%)
   10          43   0.465nm    0/43  (0%)
   50          39   0.513nm    0/39  (0%)
  250          28   0.714nm    0/28  (0%)
  500          27   0.741nm    0/27  (0%)
```

At 2.61 nm resolution a 20 nm window can hold **at most 7** separable lines.
The spectrum still carries 27 at 500 sigma. **No detection threshold rescues a
survey figure of a dense spectrum** — the limit is geometric, not statistical,
and the only lever is a figure drawn at a finer scale.

**The acceptance rule follows directly.** A zoomed panel yields a faithful line
list and should be accepted as a peak set. A survey figure of a dense spectrum
yields a sparse, crowding-determined sample of its strong lines and should be
recorded as a partial observation with its `resolvable_unit` attached — never
as a peak set for the mineral. The difference between them is 0% and 99% of the
same underlying spectrum.

---

## How much of the corpus is zoomed? — and why figtext cannot say

Since a survey figure yields no line list and a zoomed panel yields a faithful
one, the corpus's value hinges on the split. figtext looked like the cheap way
to measure it. **It is not, and the failure is systematic.**

Parsing stated wavelength ranges out of figtext gives a median span of 100 nm
and suggests ~64% of figures are under 200 nm. Validated against tick labels
read by the structured vision ask on the same figures, that agrees **1 time in
15 (7%)** and understates span by roughly 8x:

```
doi_10.1016_j.microc.2019.104388  fig_17   figtext 200nm   actual 800nm
doi_10.1585_pfr.17.2406018        fig_11   figtext  30nm   actual 600nm
doi_10.1186_s40494-016-0075-4     fig_04   figtext  20nm   actual 600nm
median figtext 60 nm   vs   median vision 490 nm
```

The cause is plain once seen: a description says "peaks between 390 and 410
nm" — a FEATURE of interest, not the axis extent — and the parse takes the
sub-range for the axis. This is the same weakness recorded earlier under P10
("ranging from 378 to 390" never says which axis), arriving in a more
expensive form: here it does not merely fail to answer, it answers wrongly and
in a consistently optimistic direction.

**The reliable measure**, from vision-read tick labels (n=31, median span
400 nm):

| band | count | share |
|---|---|---|
| zoom < 50 nm | 9 | **29.0%** |
| narrow 50-200 nm | 1 | 3.2% |
| wide 200-500 nm | 9 | 29.0% |
| survey > 500 nm | 12 | **38.7%** |

About 29% are true zoomed panels; 68% are 200 nm or wider. Small sample, and
it is the cohort already run through the vision ask rather than a fresh draw,
so it should be re-measured over a larger cohort before the number is leaned
on. But the direction is clear and it is NOT the optimistic figtext picture.

**Consequence for yield.** If ~29% holds, roughly 1,500 of the 5,254 LIBS
candidate figures are panels that can yield a line list, and the rest are
partial observations. That is the number that predicts this project's value —
far more than any property of the extractor.

---

## Recovery against figure span, and why sigma does not matter

Windows centred on 420 nm (the dense Ca/Fe region), five SME spectra, the same
sigma applied to the raw data and the extracted trace.

| span | resolvable | sep% | rec% s=3 | rec% s=5 | rec% s=10 |
|---|---|---|---|---|---|
| 10 nm | 0.036 nm | 100 | 99.1 | 99.1 | 99.0 |
| 20 nm | 0.072 | 100 | 99.6 | 99.6 | 99.5 |
| 30 nm | 0.107 | 100 | 95.6 | 96.6 | 97.9 |
| 40 nm | 0.143 | 100 | 84.5 | 86.6 | 89.5 |
| 50 nm | 0.179 | 99.6 | 69.7 | 70.0 | 70.4 |
| 75 nm | 0.269 | 78 | 36.0 | 34.5 | 32.3 |
| 100 nm | 0.406 | 26 | 10.9 | 10.0 | 8.9 |
| 150 nm | 0.573 | 4 | 0.9 | 1.0 | 1.0 |
| 200 nm | 0.907 | 0.2 | 0.1 | 0.1 | 0.1 |
| >=250 nm | >=1.3 | 0 | 0.0 | 0.0 | 0.0 |

**Sigma changes nothing.** Every cell is within two or three points across
3, 5 and 10 sigma. The threshold was never the binding constraint; span is.

**Two regimes, and the boundary is at 50 nm.** Up to 50 nm separability holds
at ~100% while recovery falls 99% -> 70%: the lines ARE distinguishable and
the extractor is what misses them, so extractor work pays there. Past 50 nm
separability itself collapses — 78% at 75 nm, 26% at 100, 4% at 150, zero
from 200 — and nothing about detection helps, because the information is gone
from the drawing.

**Banded against the corpus** (share from vision-read tick labels, n=31):

| band | share of corpus | recovery |
|---|---|---|
| zoom < 50 nm | 29% | 70-99% |
| narrow 50-200 nm | 3% | 1-70%, mostly poor |
| wide 200-500 nm | 29% | ~0% |
| survey > 500 nm | 39% | ~0% |

Median span is the wrong summary for a corpus this bimodal — roughly a third
sits in the usable regime and two thirds are past the cliff, and an average
between them describes no actual figure.

---

## Does stroke width substitute for span?

If `resolvable = nm-per-px x stroke`, then a thin pen on a wide figure should
behave like a thick pen on a narrow one, and recovery should collapse onto a
single function of resolvable. Swept 5 spans x 4 stroke widths x 5 samples at
10 sigma:

| resolvable | span | stroke | sep% | rec% |
|---|---|---|---|---|
| 0.119 nm | 20 nm | 2.0 pt | 100% | **98.6%** |
| 0.119 nm | 50 nm | 0.5 pt | 100% | **38.1%** |
| 0.179 nm | 50 nm | 1.0 pt | 99.6% | 70.4% |
| 0.186 nm | 20 nm | 3.0 pt | 100% | 90.0% |
| 0.239 nm | 100 nm | 0.5 pt | 95.4% | 24.2% |
| 0.328 nm | 50 nm | 2.0 pt | 61.9% | 39.4% |

**It does not collapse.** At an identical 0.119 nm — and identical 100%
separability — recovery is 98.6% or 38.1% depending on how that resolvable was
produced. A coarser 0.328 nm beats a finer 0.239 nm.

**But resolvable is still the best single predictor**, and an earlier draft of
this section overstated the case by saying it does not predict recovery at all.
Rank correlations over the 20 cells:

```
resolvable = nm/px x stroke      rho -0.972   p 8.1e-13
span                             rho -0.939   p 8.4e-10
nm per pixel                     rho -0.922   p 7.9e-09
stroke in pixels alone           rho -0.413   p 7.0e-02   (not significant)
```

The accurate statement is that resolvable is a strong monotone predictor and
an insufficient one: it ranks figures well but leaves a factor of 2.6 in
recovery unexplained at a fixed value. **So the artifact should record span and
stroke alongside `resolvable_unit`, not resolvable alone** — a consumer
choosing figures on resolvable would rank them correctly and still be surprised
by which ones actually yield.

**One unexplained result, flagged rather than claimed.** At 50 nm the stroke
sweep is non-monotone — 38.1 / 70.4 / 39.4 / 7.3 for 0.5 / 1.0 / 2.0 / 3.0 pt,
peaking in the middle — while every other span falls monotonically. This may
be self-inflicted: the local-noise window is `15 x stroke_px`, so changing the
stroke changes the detection window, and a 2 px stroke sits at the estimator's
floor. Treat it as a suspected artifact of our own windowing until isolated.

---

## Stroke width: accurate to measure, wrong to sample at

Operator question: how accurately is stroke width detected, and have we tried
scanning AT the stroke width as the peak resolution?

**The estimator is essentially exact modulo pixel quantisation.** Against
known drawn widths from 0.5 to 3.0 pt across 80-600 dpi (20 conditions, 5
samples each):

```
bias +0.67 px    sd 0.24 px    median |err| 0.67 px
```

Measured is reliably `ceil(true)` — a stroke of true width 2.22 px covers
three pixel rows once its antialiased edges clear the ink threshold, and the
mask counts all three. The +0.67 is that edge, not scatter; sd 0.24 across a
45x range of widths is the number that matters. Like the position residual it
is an antialiasing artifact, so it is not subtracted.

**Sampling at that width is harmful, and monotonically so.** Resampling the
trace to one sample per stroke before detection:

| bin size | samples per resolvable element | peaks | resolvable | \|dx\| med |
|---|---|---|---|---|
| none (native columns) | 1.0 | 1,988 | 248/283 (**88%**) | 0.302 px |
| stroke/4 | 4.0 | 1,573 | 243/283 (86%) | 0.303 |
| stroke/2 | 2.0 | 900 | 199/283 (70%) | 0.429 |
| stroke | 1.0 | 388 | 107/283 (**38%**) | 0.625 |
| 2 x stroke | 0.5 | 179 | 48/283 (17%) | 0.470 |

A Nyquist argument predicts a plateau at stroke/2 — two samples per element —
and there is none: recall falls 88% -> 70% there and keeps falling. Nyquist
governs bandlimited resampling, and max-binning an upper envelope is a
nonlinear morphological operation that merges and displaces rather than
filters. The native pixel grid is already the finest sampling available and
any coarsening only destroys.

Setting `min_distance` to the stroke instead of binning is harmless but
pointless: 580/725 against the baseline's 583/725, identical median position
error. Requiring `fwhm >= stroke` is slightly worse (77% vs 80%).

**The distinction worth keeping.** Stroke width is the right thing to REPORT
as the resolution limit — `resolvable = grid x stroke` is exactly what the
precision block emits — and the wrong thing to SAMPLE at. Those are different
uses of the same number, and only the first is sound.

---

## Savitzky-Golay smoothing: does the LIBS preprocessing standard transfer?

Operator question: LIBS work commonly denoises with Savitzky-Golay — how does
it compare to the 5-sigma rule? It is not an alternative to it: SG is a
smoothing filter and the sigma rule is a detection criterion, so the real
question is whether SG-then-detect beats detect-on-raw.

Published practice, for reference: Franco, Milori & Villas Boas compared every
combination of noise filter and baseline method on LIBS soil spectra and found
Savitzky-Golay paired with 4S Peak Filling best (r = 0.93, RMSE = 0.21) —
smoothing first, baseline second, then quantification. Typical guidance is
polynomial order 2-3 with the window kept well inside the peak width.

Tested as a pre-step to the 5-sigma detector, 5 samples per cell:

| dpi | extracted peak FWHM | raw resolvable | SG w=7 p=3 | raw \|dx\| | SG \|dx\| |
|---|---|---|---|---|---|
| 160 | 2.7 samples | 16/23 (70%) | 16/23 (70%) | 0.430 px | 0.496 px |
| 300 | 4.5 samples | 82/91 (**90%**) | 90/91 (**99%**) | 0.297 px | 0.582 px |
| 600 | 7.5 samples | 14/17 (82%) | 14/17 (82%) | 0.241 px | 1.759 px |

**It costs position accuracy everywhere** — 1.2x worse at 160 dpi, 2x at 300,
7x at 600 — and its recall benefit exists in exactly one regime. At w=5 and
600 dpi it fails outright, recovering 1 of 17 while emitting 440 peaks:
a low-order polynomial fitted across a staircase rings, and the spurious
maxima win the match.

**Why it does not transfer cleanly.** The extracted trace is quantised to
integer pixel rows — measured, exactly one distinct fractional part, steps of
1.000 px. Its "noise" is bounded quantisation correlated with local slope,
not the independent photon/detector noise SG is derived for, and smoothing a
staircase displaces the apex rather than averaging noise away. The window
also has no room: at 160 dpi a peak is 2.7 samples across, so even a 5-point
window spans the whole feature. In the source spectrum a peak is 5.7 samples
across at 0.033 nm sampling — the regime the literature works in.

**Verdict: not adopted by default.** Position is the product this tool can
actually validate, and SG degrades it in every cell; the recall it buys is in
peaks that are individually unverifiable against a catalogue with a line every
0.04 nm. Worth revisiting as a CONDITIONAL step: the corpus median effective
dpi is 286, squarely in the one regime where it wins 90% -> 99%, so a rule
gated on measured peak FWHM in samples (roughly 3.5-6) would be defensible if
the recall matters more than half a pixel of position.

Sources: arXiv 1805.03695 (Franco et al., baseline correction for LIBS);
SciPy Cookbook and SpectroChemPy for parameter guidance.

---

## Side experiment — can annotation furniture be masked out? (2026-08-31)

Operator suggestion: have paddle block off legend and annotation space and
paint it to background, so an annotated figure becomes extractable instead of
refused.

**The mechanism works, and well.** Masking text spans PLUS filled boxes PLUS
the strokes touching them, against exact truth:

| figure | F1 before | F1 after | \|dI\| before | \|dI\| after |
|---|---|---|---|---|
| annot ×4 | 0.133 | **0.627** | 0.855 | **0.055** |
| annot + legend | 0.000 | **0.784** | — | **0.056** |
| legend only | 0.823, flagged | 0.823, flag cleared | | |
| clean (control) | 0.823 | 0.823, nothing masked | | |

Against a clean baseline of 0.823 that is near-full recovery, and the
intensity error falls by 15×.

**A trap found on the way.** Masking the TEXT LAYER ALONE changes nothing —
F1 stays at 0.133 — because the harm is done by the box outlines and the
callout arrows, which are vector drawings rather than text. Worse, it clears
the `annotation_overlap` flag while leaving the extraction broken, so the
figure stops refusing and starts emitting garbage. Text-only masking is
strictly more dangerous than no masking at all.

### But paddle cannot supply the boxes

Two independent routes, both closed:

- **PP-DocLayoutV3 / PaddleOCRVL**: a chart is ATOMIC. The pipeline returns
  `figure_title` plus one `chart` box covering the whole figure and never
  decomposes its interior; `overall_ocr_res` comes back empty for charts.
- **PP-OCRv5 / PP-OCRv4 text detection**: both download, neither runs —
  `NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support`, a
  paddle runtime mismatch in `tools/pdf_extract/.venv`. Not fixed, because
  the OCR lane depends on that venv and the mission is live.

Nor can a VLM. Asked for annotation boxes with ground truth to score against,
`muse` reasoned in prose, emitted no coordinates, and invented two label
values that are not in the figure. `paddle-ocr-vl` ignored the question
entirely and transcribed the chart as a data table — **about 35 fabricated
wavelength/intensity pairs**. That is a clean live demonstration of the
unbounded reading this whole tool exists to replace.

### The PDF can, for 18% of figures

The only exact box source is the document itself, and its reach is measured:

```
tier                  n    interior TEXT     filled BOX
native_raster       128      6 (  5%)      3 (  2%)
render_vector        17     12 ( 71%)     10 ( 59%)
vector               10     10 (100%)      4 ( 40%)
render_resampled      5      0 (  0%)      0 (  0%)
28/160 (18%) carry PDF text inside the plot area
```

A prior assumption of mine is refuted here: publishers do NOT generally
typeset labels over a raster figure. In the native-raster tier — which is 80%
of the corpus — the annotation is baked into the pixels, and only 5% carry any
PDF text inside the plot at all.

**Verdict.** Worth implementing for the vector-backed tiers, where it is free,
exact, and recovers most of the loss. Not a general solution: for the
native-raster majority no box source exists — not paddle, not a VLM, not the
PDF — so refusal stands there. Masking must also always include drawings, never
text alone.

Repro: `dev/figdig_furniture_masking.py`.

## The pen was innocent: the stroke anomaly was a vacuous sigma (2026-08-31)

The operator challenged the stroke table above on physical grounds: the pen is
what abstracts peak detection, so at a fixed nm-per-px a finer pen cannot lose
more information than a thicker one — "a confound between higher resolution
sample and finer pen seems impossible." The challenge was correct.

**De-confounded methodology.** The old sweep coupled two strokes: the pen that
draws the figure and the `stroke_px` handed to `pick()`, which scales the
noise window. The re-run separates detection from ink with arms that differ
ONLY in the detector (same renders, same traces, same truth, same tolerance):

- **retention** — a distinct local apex exists within tolerance, no threshold
  at all. Pure "did the pen keep it."
- **coupled 10-sigma** — last turn's detector, window at `15 x measured px`.
- **floored 10-sigma** — same, with the quantisation noise floor below.

```
 span   pt    px  resolv |   ret% coupled% floored% | med_sigma
   20  0.5  2.00  0.048  |  95.9    95.9     87.3   |  0.00000
   20  1.0  3.20  0.077  |  94.1    94.1     86.8   |  0.00000
   20  2.0  6.40  0.153  |  90.9    90.9     81.4   |  0.00000
   20  3.0 10.00  0.239  |  84.5    84.5     77.7   |  0.00000
   50  0.5  2.00  0.120  |  92.4    52.9     52.9   |  0.00274
   50  1.0  3.20  0.191  |  84.5    76.5     61.8   |  0.00057
   50  2.0  6.00  0.358  |  62.0    62.0     52.7   |  0.00000
   50  3.0 10.80  0.645  |  52.9    52.9     47.3   |  0.00000
  100  0.5  2.00  0.239  |  58.4    20.8     20.8   |  0.00274
  100  1.0  3.20  0.382  |  48.7    24.0     24.0   |  0.00275
  100  2.0  6.00  0.716  |  38.5    38.5     29.4   |  0.00000
  100  3.0  8.20  0.979  |  32.3    32.3     24.7   |  0.00000
  200  0.5  2.00  0.477  |  35.2     8.5      8.5   |  0.00272
  200  1.0  3.00  0.717  |  28.7    12.3     12.3   |  0.00272
  200  2.0  5.60  1.337  |  21.1    21.1     15.5   |  0.00000
  200  3.0  8.00  1.910  |  16.7    16.7     12.6   |  0.00000
  600  0.5  2.00  1.432  |  14.3     4.3      4.3   |  0.00378
  600  1.0  3.20  2.293  |  11.4     4.9      4.9   |  0.00271
  600  2.0  5.40  3.868  |   8.3     6.3      5.3   |  0.00109
  600  3.0  8.20  5.870  |   7.6     7.6      6.1   |  0.00000
```

**The mechanism is the sigma column, and it is the vacuous-gate trap again**
(fourth confirmed instance in this repo). A thick pen's topmost-ink envelope
is smooth at the 5-sample detrend scale, so the rolling MAD estimates ~0 and
"10 sigma above local noise" degenerates into "any local maximum": every
thick-pen arm shows sigma 0.00000 and its coupled recovery EQUALS its
thresholdless retention, digit for digit. The thin pen faithfully renders the
spectrum's real fine structure, estimates an honest sigma, and gets a
genuinely enforced bar. The old table compared a vacuously-passed criterion
against an enforced one and read the difference as "thick pen wins."

The mechanism I suspected in the previous entry — the `15 x stroke_px`
WINDOW — was wrong: pinning the window while sweeping the pen moved almost
nothing. It was the estimate, not the window.

**Fix shipped.** `peaks.trace_noise_floor()`: an extracted trace cannot
measure noise below its own pixel grid, so sigma is floored at the
quantisation sigma of a uniform half-pixel rounding error, `1/sqrt(12)` px in
the caller's normalised units. Regression test renders the trap both ways.

**What this retracts, and what stands:**

- RETRACTED: the 50 nm non-monotonicity (38.1/70.4/39.4/7.3) — retention is
  monotone there, 92.4/84.5/62.0/52.9. Retracted with it: "a coarser 0.328 nm
  beats a finer 0.239 nm" and the 98.6-vs-38.1 reading of the matched-
  resolvable pair as an ink property. All three were the detector.
- CONFIRMED (the operator's physics): a finer pen is monotonically better at
  every span, once you measure the ink instead of the detector.
- STANDS: **stroke does not substitute for span.** Even under retention,
  matched resolvable splits by span (0.239 nm: 84.5 vs 58.4; 0.716/0.717 nm:
  38.5 vs 28.7) — and the reason is now stateable: the loss has two terms,
  grid alone (at a 200 nm span each column max-pools ~7 raw samples, so
  sub-pixel peaks collapse regardless of pen) and grid x stroke (pen
  merging). A single product cannot collapse a two-argument function.
- REAL RESIDUAL, no longer anomalous: with the floor in place a 2 pt pen
  still edges the 0.5 pt pen in 10-sigma RECOVERY at 100-200 nm (29.4 vs
  20.8) while losing in RETENTION (38.5 vs 58.4). The max-envelope of a
  thick pen is a crude smoother: it suppresses column-to-column jitter —
  mostly unresolved real lines that read as noise at survey grid — more than
  it suppresses broad peaks, the same trade Savitzky-Golay makes on purpose.
  Detectability and separability are different currencies; the thin pen
  retains more but can honestly ATTEST less at N sigma, because it also
  renders the noise.

Repro: `dev/figdig_stroke_deconfound.py` (the previous sweep's script was
ephemeral and lost — which is why this one is committed).

---

## How dpi interleaves, once the pen is deconvoluted (2026-08-31)

With the detector honest, dpi becomes measurable. The geometry predicts it
should not matter at all: `resolvable = grid x stroke_px`, grid = span/columns
and stroke_px = stroke_pt x dpi/72, so **dpi cancels in the product**. Four
predictions were registered before the run (D1 flat above 160 at 20 nm; D2
wide spans climb but saturate far below narrow; D3 thin pens gain more than
thick; D4 floored recovery gains MORE than retention).

Swept 5 spans x {0.5, 2.0} pt x {80, 160, 300, 600} dpi x 5 samples.

```
 span   pt  dpi meas_px eff_pt  resolv |   ret% floored%
   20  0.5  160    2.00   0.90   0.048 |  95.9     87.3
   20  0.5  300    3.00   0.72   0.038 |  98.2     91.4
   20  0.5  600    5.00   0.60   0.032 |  99.5     96.4
   50  0.5  160    2.00   0.90   0.120 |  92.4     52.9
   50  0.5  300    3.00   0.72   0.096 |  96.9     90.1
   50  0.5  600    5.00   0.60   0.080 |  99.4     96.0
  100  0.5   80    1.00   0.90   0.240 |  32.0      4.4   (n=1, see floor)
  100  0.5  160    2.00   0.90   0.239 |  58.4     20.8
  100  0.5  300    3.00   0.72   0.191 |  77.8     35.9
  100  0.5  600    5.00   0.60   0.159 |  92.2     76.4
  200  0.5  160    2.00   0.90   0.477 |  35.2      8.5
  200  0.5  600    5.20   0.62   0.331 |  58.8     44.8
  600  0.5  160    2.00   0.90   1.432 |  14.3      4.3
  600  0.5  600    5.20   0.62   0.992 |  28.9     22.1
  100  2.0  160    6.00   2.70   0.716 |  38.5     29.4
  100  2.0  600   19.80   2.38   0.630 |  51.3     41.8
  600  2.0  160    5.40   2.43   3.868 |   8.3      5.3
  600  2.0  600   18.80   2.26   3.588 |  13.3     10.9
```

**The cancellation is exact at 80->160 and breaks above it.** At 100 nm the
resolvable is 0.240 then 0.239 — the pen is physically IDENTICAL, 0.90 pt in
both. Above 160 dpi it decays: 0.90 / 0.90 / 0.73 / 0.61 pt for a nominal
0.5 pt pen. The cause is a fixed ~1 px ink overhead — antialiasing plus the
mask counting whole pixels means measured_px ~ ceil(nominal_px), and a
constant *pixel* overhead is worth progressively less *physical* width as dpi
rises. So dpi does not cancel in practice; it thins the effective pen.

**The 80->160 step is a clean natural experiment** and it settles the two-term
decomposition. The pen is unchanged in physical units, resolvable is unchanged
to three decimals, and retention still nearly doubles:

```
  100 nm   resolvable 0.240 -> 0.239 (pen 0.90 -> 0.90 pt)   retention 32.0 -> 58.4
  200 nm   resolvable 0.480 -> 0.477 (pen 0.90 -> 0.90 pt)   retention 15.8 -> 35.2
  600 nm   resolvable 1.436 -> 1.432 (pen 0.90 -> 0.90 pt)   retention  7.6 -> 14.3
```

That is the **grid term moving with the pen held fixed** — the max-pooling of
raw samples into columns, isolated. It also makes resolvable insufficient on a
third independent axis: 0.240 and 0.239 give 32.0% and 58.4%.

**Scoring.** D1 **REFUTED** — retention is not flat above 160 dpi even at
20 nm (95.9 -> 99.5); dpi buys something everywhere. D2 **MIXED** — "climbs"
confirmed, "saturates far below" refuted: 100 nm at 600 dpi reaches 92.2%,
which is what 20 nm gave at 160 dpi. No saturation appears anywhere in range.
D3 **CONFIRMED** (from the fully-sampled 160 dpi baseline; the 80 dpi thin-pen
row is n=1-2, see the floor below): at 600 nm the thin pen gains 2.01x against
the thick pen's 1.60x. D4 **CONFIRMED, and it is the largest effect in the
table**: at 100 nm retention gains 1.58x while floored 10-sigma recovery gains
3.67x, because the quantisation floor itself shrinks as the trace grows in
pixels. The fraction of retained peaks that are *attestable* at 10 sigma goes
0.36 -> 0.83.

**The boundary moves.** The previously-recorded "50 nm boundary, and nothing
helps above it" was measured at a fixed 160 dpi. Retention at 0.5 pt:

```
   dpi     20nm     50nm    100nm    200nm    600nm
   160     95.9     92.4     58.4     35.2     14.3
   300     98.2     96.9     77.8     45.6     21.1
   600     99.5     99.4     92.2     58.8     28.9
```

At 600 dpi the usable boundary sits near **100 nm**, not 50. Wide-but-not-
survey figures move from unusable to mostly recoverable.

**A refusal floor falls out.** At 0.5 pt / 80 dpi (0.56 px nominal) the trace
extractor lost the curve on **16 of 20 renders**; at 160 dpi (1.11 px) it
extracted 20/20. A pen under ~1 px does not extract at all — a hard floor,
independent of any detection threshold, and the first empirical version of the
refusal number the plan wanted from Gate B.

**But the lever is only available on 17% of the corpus.** Over the 160 plot
figures with source blocks on disk:

```
tier                  n      %   dpi we are stuck with (p25/med/p75/max)
native_raster       128  80.0%   200 / 300 / 300 / 1158
render_vector        17  10.6%   -- ours to choose
vector               10   6.2%   -- ours to choose
render_resampled      5   3.1%   160 / 160 / 160
```

For the native-raster majority dpi is the publisher's decision, median 300 and
only 9% at >=600 — so those figures sit on the middle row of the table above,
permanently. Re-rendering them finer is `render_resampled`: a finer grid over
the same information, which buys nothing and must not be reported as
precision.

**Actionable gap, not yet applied.** `source.load_pixels` defaults to
`src.effective_dpi`, which for a vector-backed render is the 160 dpi crop
resolution. The module docstring (lines 20-21) already states that
re-rendering vector art at 600 dpi is a genuine information gain — but the
default does not act on it. On the 27 vector-backed figures that is a
measured 1.6-2.0x in retention and up to 3.7x in 10-sigma recovery, from a
default change. Left for the operator's call since this turn's question was
diagnostic.

Repro: `dev/figdig_dpi_interleave.py`.

---

## Can we predict minimum resolvable feature from figure metadata? (2026-08-31)

The operator's question: given a figure's native resolution (taking the max
available), stroke width and span, can we predict its minimum resolvable
feature? Measured over **116,808 peak-observations across 300 cells** (5 spans
x 4 strokes x 3 dpi x 5 real spectra), recording for every truth peak its raw
FWHM, its nearest-neighbour separation, and whether it won its own detection.

**A scorer bug had to be fixed first, and it is the vacuous-gate shape again
(fifth instance).** The first version asked "is there A detection within
tolerance of this truth peak" -- which lets ONE merged blob satisfy BOTH peaks
under it, so merging, the exact failure being measured, could not fail the
test. Detection read 90-97% across every separation quartile while true
retention was 49%. Assignment must be one-to-one and greedy on distance, each
detection consumable once. After the fix the per-peak rates reproduce the dpi
sweep exactly (58.4 / 92.2 / 14.3 / 95.9), which is the cross-check that the
broken version failed silently.

**Reframe: it is a separation limit, not a width limit.** Width is the weaker
variable and its correlation with detection changes sign across span (+0.488
at 20 nm, +0.080 at 100, -0.070 at 600). Separation is the physically right
criterion (Rayleigh), so the quantity fitted is s50 -- the neighbour
separation at which a peak has a 50% chance of winning its own detection.

**The headline fit looks excellent and is half illusion.**

```
  s50_nm = 3.48*grid + 0.225*pen     R2 = 0.863   (thresholdless retention)
  s50_nm = 7.64*grid + 0.298*pen     R2 = 0.889   (floored 10-sigma recovery)
```

But `pen = grid x stroke_px`, so this is algebraically `s50 = grid x (a + b*
stroke_px)` -- and `grid` ranges 120x across these cells. **Expressed in
pixels, where that scale is removed, R2 falls from 0.86 to 0.19.** Nearly all
of the nm-space R2 is the model tracking the grid scale, which is the part
that was never in doubt. Reporting the 0.86 without this decomposition would
have been the same error class as the vacuous gate: a number that cannot fail.

**What is actually there, in pixels** (s50 binned by pen width, no fit):

```
  stroke_px   n   median s50_px      IQR
      0-3     3        5.5         4.5 - 10.7
      3-5     6        6.5         3.6 -  9.9
      5-8    11        6.1         5.8 -  9.0
     8-12    12        9.1         7.3 - 11.3
    12-20     7       14.8        12.7 - 17.1
    20-40     7       17.6        14.7 - 20.1
```

Monotone and significant (rho +0.641, p 1.2e-06). A robust (Theil-Sen) fit
gives `s50_px ~ 4.7 + 0.45*stroke_px` at a median error of 27%, but R2 is
NEGATIVE (-1.5): the fit tracks the bulk and is destroyed by a heavy tail.
Held-out extrapolation is 15-44% median with worst cases of 500-800%.

**Why the tail is not fixable from metadata.** Two hypotheses were tested.
Identifiability -- that s50 is only estimable when a cell's detection rate is
mid-range -- is **REFUTED** (rho -0.135, p 0.37; restricting to the mid band
made the fit worse). The second is confirmed: holding span, pen and dpi FIXED
and splitting by which of the five real spectra was plotted,

```
  config                  sme1  sme2  sme3  sme4  sme6   spread
  span 100 1.0pt 160dpi   12.9   9.5  22.3  15.5  26.7     36%
  span 200 1.0pt 300dpi    7.2   8.8  22.8  14.4  10.1     44%
  span 200 2.0pt 600dpi   17.3  15.5  20.3  32.4  41.0     39%
```

Between-sample spread is a median **31%** of the mean against a total
across-cell spread of 73% -- so roughly **42% of all scatter comes from the
spectrum's own crowding**, at identical figure geometry. That is not figure
metadata and no amount of span/stroke/dpi can recover it.

**Verdict: yes for triage, no for a per-peak number.**

- The SCALE is predictable and that is the useful part: `s50 ~ grid x 10 px`
  at 10 sigma, `grid = span / (interior_width_pt x dpi/72)`. Good to roughly a
  factor of 1.5-2, which is enough to rank figures and to refuse the hopeless
  ones before spending a vision call.
- A point estimate of a given figure's resolvable separation is NOT available
  from these three variables; the residual is 27-37% with a heavy tail, and
  the largest single missing term is a property of the spectrum, knowable only
  after extraction.

**Actionable, and it is a correction to what we currently emit.** The
precision block reports `resolvable_unit = grid x stroke_px` -- the pen term
ALONE. The measurement says the binding constraint is a grid floor of ~5 px
(retention) to ~10 px (10 sigma) that applies regardless of pen. For a typical
3 px stroke the model gives 6.1 px against the emitted 3.0 px, so
**`resolvable_unit` currently understates the real resolvable separation by
about 2x**, and by more for fine pens. It should either be renamed to
`pen_merge_unit` or redefined as `grid x (4.7 + 0.45 * stroke_px)` with the
scatter recorded alongside. Not applied -- this turn's question was
diagnostic.

Repro: `dev/figdig_resolvable_model.py`.

---

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
