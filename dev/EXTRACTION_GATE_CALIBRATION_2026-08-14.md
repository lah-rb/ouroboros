# The extraction gate: the threshold was never the problem, the ruler was

*48 extractions judged blind by 12 independent Opus agents against their source
PDFs (10 spot-checks each, 6 numeric + 4 prose, verified page-by-page), scored
on a fit-to-train tier. 29 papers the gate REJECTED plus 19 it PASSED, shuffled
and de-identified so no judge could see a rate or an arm. Rubric and raw
verdicts in the session scratchpad; per-paper numbers below are reproducible
from `dev/` scripts noted at the end.*

## The question

`MIN_NUMERIC_RATE = 0.85`, `MIN_SPAN_RATE = 0.75`. Is the split point too
strict, and if so where does the metric start agreeing with a careful reader?

## The answer

It never starts agreeing. **After both measurement bugs below are fixed, the
correlation between the numeric rate and the blind quality tier is +0.02.**
Tier medians are flat and out of order:

| tier | n | fixed numeric | fixed span | usable fraction |
|---|---|---|---|---|
| A — faithful | 5 | 0.907 | 0.868 | 0.97 |
| B — trainable | 14 | 0.925 | 0.891 | 0.95 |
| C — hold | 25 | **0.929** | 0.833 | 0.85 |
| D — discard | 4 | 0.869 | 0.884 | 0.80 |

Tier C scores *higher* than tier A. The gate is not measuring what we thought
it was measuring, and no threshold on it can sort trainable from not.

Two separate findings explain that, and they want different fixes.

## Bug 1: the truth contains the manuscript's line numbers

`_prose_text` reads PyMuPDF `"blocks"`, and PyMuPDF merges a margin
line-number into the same block as the prose line beside it:

```
*To whom correspondence may be addressed. Email: vmerk@fau.edu   |11 |
aLawrence Berkeley National Laboratory, 1 Cyclotron Rd., Berkeley, CA 94720 |7 |
```

`_NUM_RE` matches bare 2+ digit integers, so **every line of text on a
line-numbered manuscript contributes one phantom miss** — ~40 per page. Our
markdown correctly omits them; the gate scores that as failure.

Confirmed at span level rather than inferred: on these PDFs the pure-digit
spans sit at x≈42 while prose starts at x≈71, and 40 of 40 are consecutive
integers. One blind judge found the same thing from the other side, reporting
an author rendered as `8 Brynn Hibbert`.

**Measured, isolated** (figure filtering deliberately left at block granularity
so the delta is attributable to one change):

* 12 of the 48 sampled papers carry line numbers — **all 12 in the reject arm,
  zero in the pass arm**.
* Their numeric goes **0.528 → 0.977** median. The other 36 move by 0.000.
* Corpus-wide: 16 of 259 papers (6%), of which **12 sit in the rejected pile**
  — 40% of everything the gate is currently holding.
* **11 papers are reclaimed by this fix alone**, at the existing 0.85/0.75.

This is the whole deep-reject cluster. Every paper scoring 0.43–0.55 is a
line-numbered Elsevier accepted manuscript, not a bad extraction.

## Bug 2: the truth contains publisher furniture

The remaining low scorers on *faithful* papers are download stamps, DOIs,
ISSNs, licence versions and fax numbers:

```
216.71  ...downloaded from ip address 216.71.8.37 on 11/08/2026 at 22:57
  2405  ...doi.org/10.1016/j.heliyon.2019.e01505  2405 8440/ 2019 published by
   351  ...1649 003 lisboa, portugal. tel.: +351 219946065; fax: +351 219946285
```

On a short paper this is the *entire* miss set — one tier-A conference paper
lost 47 of 47 numbers to an IOP download stamp and scored 0.736. It is a fixed
cost charged against a small denominator, which is why it hits short faithful
papers hardest.

A line-wise pattern filter moves the DOI-footer papers +0.09 to +0.22 and
leaves everything else within ±0.01. **The pattern used here is a probe, not
shippable** — it drops any line containing `licence|issn|doi:|fax|©`, which
would also drop a body sentence discussing a DOI. Tighten before it goes near
production.

## Why no threshold can work

The metric is **recall with no binding check**: does this number appear
*anywhere* on the page. A table whose header is dropped and whose values bind
to the wrong row still contains every one of its numbers and still scores 1.00.

That is precisely the defect the judges found most often:

| tier | dominant defect |
|---|---|
| A (5) | `none` ×4 |
| B (14) | `math_noise` ×4, `figure_caption` ×3, `reference_garble` ×2, … |
| C (25) | **`table_structure` ×8, `hallucination` ×7, `dropped_content` ×7** |
| D (4) | `degenerate_repetition` ×2, `hallucination` ×1, `dropped_content` ×1 |

Concrete C-tier examples the numeric rate scores clean:

* Table 8 loses its `Length` header, so **6100 Gt CO₂ becomes `1`** and 33 000 km²
  becomes `55`.
* A species column offset by one row: *Escherichia coli* → genus *Xanthomonas*.
* Table 2 keeps 6 of 31 rows of the paper's core dataset.
* `O1 Y=0.28700` and `O2 Y=0.00800` both rendered `0.08600`.

Every number is present. Every binding is wrong.

## The numbers, if a threshold is still wanted

Same 48 papers, sweeping the **fixed** ruler:

| numeric | span | trainable kept | trainable LOST | C/D cut |
|---|---|---|---|---|
| 0.85 | 0.75 | 15/19 | 4 | 8/29 |
| 0.80 | 0.75 | 15/19 | 4 | 6/29 |
| 0.78 | 0.72 | 18/19 | 1 | 5/29 |
| **0.75** | **0.70** | **19/19** | **0** | 4/29 |
| 0.70 | 0.65 | 19/19 | 0 | 2/29 |

For contrast, **the shipping ruler at 0.85/0.75 keeps 11 of 19 trainable
papers — it rejects 42%, including 3 of the 5 papers judged fully faithful**
(P02, P26, P42 at 0.75, 0.74, 0.77).

Recommendation: **fix the ruler, then set 0.75 / 0.70.** Not because 0.75 is
principled — with r = +0.02 nothing in 0.70–0.80 is — but because the gate's
honest job is a floor against catastrophic extraction failure, and at 0.75/0.70
it loses nothing trainable while still catching the four worst documents in the
sample (usable fractions 0.50, 0.65, 0.80, 0.92).

Corpus effect of fix + floor: 13 held papers admitted at the current threshold,
11 more at 0.75/0.70; 6 currently-extracted papers would newly fail.

## What to add instead

The gate needs checks aimed at the defects that actually occur.

**1. Degenerate repetition — ready now.** Words spanned by the longest
back-to-back repeated block. *(An earlier draft of this note reported this
metric as "characters" from a `run * period` expression whose unit was actually
words × period — a mislabel, not a different conclusion. The numbers below are
the corrected metric, and the threshold is derived from these.)*

```
P48  D  1598      <- "NGO-PEG per 100 uL of NGO-PEG" loop
P13  D   675      <- 17% of the file
P22  C   107      <- worst NON-degenerate defect
P23  C    99
P17  C    79
...
max across all 19 tier-A and tier-B papers: 19
```

The separation is enormous — 19 for the worst trainable paper against 675 for
the milder of the two degenerate ones — and there is an empty gap between 107
and 675. **The cut goes in that gap at 200 words**: >10× above anything
trainable, >3× below the lowest degenerate document, and deliberately NOT low
enough to also catch table damage, which it is not measuring and would be
catching for the wrong reason. Pure string work — no PDF, no model.

**2. Table binding.** Header-row presence, row count against the source, and
`<lcel>/<fcel>/<nl>` token leakage (one paper emitted a 96-value table as raw
token soup). Harder, but it is where 8 of 25 C-tier defects live.

**3. Truncated acquisition.** One D-tier paper is page 37 of a 5-page article —
the PDF itself is wrong. Extraction was faithful to what it was given. Page
count against the record's own metadata catches this; no quality metric can.

## Caveats

* Blind tiers are one judge per paper, not a panel. The A/B vs C/D split is
  robust (defects are quoted with page references and were independently
  reproducible on spot checks); the C/D boundary is thin.
* The C bar is strict by construction — "would a model learn anything FALSE" —
  so C means *hold for inspection*, not *discard*. Several C papers carry
  usable fractions of 0.90+ with damage confined to one table.
* The corpus-wide scan applied the line-number fix only; the furniture filter
  postdates it. Corpus figures for bug 2 are therefore not yet measured.
* Some recorded rates predate the spatial figure filter (2026-08-12) and do not
  reproduce exactly on recompute. Two of the 13 reclaimed papers pass for that
  reason rather than the line-number fix — the honest count for bug 1 is 11.

## What shipped, and what it did to the corpus

Landed the same day: the two oracle fixes in `extract_batch._prose_text`, the
`_max_repeat_words` check, thresholds at 0.75/0.70 with `MAX_REPEAT_WORDS=200`,
and a full corpus re-verification.

```
                    before   after
extracted              229     260
rejected                47      16
```

Rejections now, and they are all legible:

| bucket | n | what it is |
|---|---|---|
| `degenerate` | 6 | looped decode — the new check |
| `span` | 3 | genuine low span |
| `no-text-layer` | 3 | scans, terminal by policy |
| `error` | 3 | VLM worker crash |
| `toolchain` | 1 | timeout |

**The repetition check earned its place immediately.** It flagged 6 papers,
zero false positives against the 19 known-trainable, and **5 of the 6 had
CLEAN rates and were sitting in the corpus** — including the paper a blind
judge independently tiered D for a 675-word loop. Verified by reading each
one's repeated span:

```
3003x  "both both both both both ..."
3001x  "due to the increase in the concentration of the ZnO nanomaterials"
1883x  "and more well-defined, and more well-defined, ..."
1598x  "NGO-PEG per 100 uL of NGO-PEG ..."
1416x  (heliyon.2024.e33646)
 675x  "the concentration of the<Thai glyphs> (Si-Si-Fe) ..."
```

### Two more bugs found while implementing

**`reverify.py` was writing to a file nobody reads.** It read and appended
`papers.jsonl`, which predates the two-file split — `read_databank` overlays
`extraction.jsonl` ON TOP of it, so every verdict this script produced was
masked by the sidecar and no downstream stage ever saw one. Now reads merged,
writes the sidecar.

**`reverify.py`'s gate was missing `verified_pages > 0`.** With nothing
checkable the rates are vacuous 1.00s, so an unverifiable extraction passed —
and one had: a paper with no text layer was sitting in the corpus marked
`extracted` on a 1.00/1.00 it never earned. This is the vacuous-verification
trap the extractor's own gate was hardened against; the copy in this tool never
got the fix. Now `extract_unverified`, which is where the policy already said
it belonged.

It also **preserves the 34 manual promotions** — a human who read the markdown
against its PDF outranks this script, so rescoring refreshes their numbers and
never revokes their verdict. Without that guard, running it once would have
silently undone the whole promotion pass.

### Still admitted, honestly

One paper the blind audit tiered D is now in the corpus: the source PDF is page
37 of a 5-page article, so the extraction is *faithful to what it was given*
and no text-comparison metric can see the problem. It needs a page-count check
against the record's own bibliographic metadata — a different signal, not a
tighter threshold.

Table binding — the largest C-tier defect class at 8 of 25 — remains unchecked.

## Scripts

`scratchpad/calib/`: `attribute.py` (per-page miss attribution),
`corrected.py` (both fixes, A/B against the shipping oracle),
`key.json` / `verdicts.json` (sample and blind tiers).
Promote to `dev/` if the fixes land.

## Postscript: graphically-encoded tables are recoverable, and vision already saw them

The PTAL paper (`doi_10.1002_jrs.5652`) was denied for "tables rendered with
empty cells", read as extraction damage. It is not. Its mineral-detection matrix
encodes presence as **cell fill patterns** — stipple, vertical hatch, horizontal
hatch — against a legend printed below the table. The PDF's own text layer holds
exactly the same empty cells our markdown does, because there is no text to
read. The extraction was faithful.

**Vision had already seen it.** PaddleOCR-VL *is* a VL model; the layout stage
classified the region as a table and a vision model transcribed it. It returned
correct row labels and column headers with empty data cells, because it was
asked to transcribe a TABLE and the fills are not cell text. The lever is not
"get vision onto the region" — it is **what the model is asked to produce for
it**.

Probed directly: the same region cropped at 300 dpi with its legend, handed to
muse-glimmer-30b over `/v1/chat/completions` with a "read the fills against the
legend" ask.

```
23 of 23 filled cells correct, twice, on independent runs
  ~95 s per table region, ~1.4k completion tokens
```

Every mineral, every sample row, and every legend mapping — including the
three-way distinction between "both methods", "RLS simulator" and "microRaman".

### What this does NOT justify

**Reclassifying such tables as figures.** The row labels, sample IDs and column
headers extract correctly today; swapping the grid for a prose description
trades a reliable structure for a generated one. The recovery path is BOTH —
keep the HTML table, and additionally crop the table region into the figure set
so figtext reads the fills. Both are text by the time the curator sees them, so
grounding still works.

**A heuristic trigger.** Measured across 40 papers: "cells mostly empty AND
dense vector fill inside the table bbox" fires on **43.7% of detected tables**,
because it rides on `find_tables()`, which is unreliable on this corpus (one
"table" carried 834 vector objects across 3 rows — a figure). There is no cheap
detector.

The trigger already exists and is better: `deny_category: data_not_in_text` is a
per-paper, reasoned verdict from the curator. Papers carrying it are the work
list for a targeted re-pass. Curator flags → crop that paper's table regions →
figtext reads them → re-review. A feedback loop rather than a guess, and it only
spends the ~95 s where something said it would pay.
