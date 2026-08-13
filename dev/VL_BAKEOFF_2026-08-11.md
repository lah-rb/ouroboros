# VL figure-transcription bake-off — 10-figure held-out set, 2026-08-11

Which local model reads a scientific figure most faithfully, for the extractor
stage that mines spectra out of OA PDFs. A 2-figure screen ranked the field
first; this is the held-out set that decides it, because two figures cannot
tell a capability gap from a fluke — and it turned out they could not.

## Method

**The field, and what was already eliminated.** An initial pass ran EVERY
mmproj-bearing model available, which included the whole Qwen3-VL MLX family
(4B / 8B / 30B-A3B). That family was dominated on BOTH axes — speed and
transcription quality — by the larger suite, and did not advance. The five
entrants below are the survivors, not the whole field.

Recording this because the absence was later misread as "never benched": the
incumbent `FIG_MODEL` was Qwen3-VL-8B, and a reader who saw it missing from
the results table could conclude it was untested rather than beaten. It was
beaten. (Operator correction, 2026-08-12 — the claim "never in it" in commit
2e72752's message and in the code comments it introduced is WRONG.)

**Figures.** 10 held out from the same 37-figure extraction pool the screen
drew from, spanning 5 papers and 4 aspect classes, chosen for mixed figure
TYPE rather than mixed topic: stick pattern, histogram, dense-text card,
ternary GUI screenshot, IR spectra, 5-panel refractive index, stacked
mineralogy, two reflectance comparisons, photographs.

**Reference.** One blind Opus agent per figure, seeing only the image — no
caption, no model output, no other agent's work. Each produced a full
description plus 12-27 atomic checkable facts; 192 facts total. They cropped
and upscaled 4-26x and measured from raw RGB rather than reading the
downscaled page, which is what let them catch things like a bar recoloured
mauve against lavender neighbours.

**Scoring.** One judge per figure against `dev/VL_JUDGE_RUBRIC.md`. Candidates
were relabelled A-E with a DIFFERENT hash-derived shuffle per figure, so no
judge could carry an impression across figures or know which model was the
incumbent. De-anonymisation happens once, in `dev/vl_set10_aggregate.py`.

The rubric's load-bearing rule: **a confident invention scores worse than an
admission.** Where a reference marks an item genuinely unreadable, a candidate
that hedges is RIGHT and one that states a crisp value is WRONG. Every
candidate was told "a stated uncertainty is worth more than a confident
invention", so scoring it any other way would reward the exact failure mode
that poisons a corpus.

## Results

| model | /192 | % | figures w/ fabrication |
|---|---|---|---|
| **muse-glimmer-30b** | **145** | **75.5%** | 5/10 |
| qwen3.6-27b | 134 | 69.8% | **3/10** |
| qwen3.5-122b-a10 | 132 | 68.8% | 4/10 |
| qwen3.6-35b-a3 | 126 | 65.6% | 9/10 |
| step37-flash | 100 | 52.1% | 9/10 |

Per-figure scores are in `dev/vl_set10_aggregate.py` output; judge tables and
NOTABLE sections in the session scratchpad `vl_judge/`.

## What the 10-figure set changed

**The 2-figure screen overstated the winner and mis-ranked the middle.** Muse
scored 13/14 on the screen, which reads as near-perfect; it is 75.5% here and
fabricates on half the figures. qwen3.6-35b-a3 looked tied-2nd on the screen
(11/14) and lands 4th with fabrications on 9 of 10. step37-flash's weak screen
result (9/14) was real and got worse. Only the top two survived the screen's
ordering.

**Fabrication does not track accuracy.** qwen3.6-27b is 11 facts behind Muse
but fabricates on 3 figures against Muse's 5. If the downstream use punished
invented values harder than it rewarded recall, that ordering would flip — for
the scrape it does not, because a curation pass follows, but it is the number
to re-read if the extractor ever writes straight to a training set.

**Two figures defeated everyone.** All five models fabricated on `mineralogy`
and `craters`. Those carry clipped captions and sub-pixel detail, so 2 of
Muse's 5 fabrication figures are properties of the figure, not the model.

## Notes worth keeping

* **The judges verified the references and one found a reference WRONG** — the
  card_ocr K subscript the reference called "genuinely ambiguous between 5 and
  6" resolves at 26x. It said so instead of silently scoring against a bad
  fact. No cells moved.
* **No contamination.** The reference agents briefly shared a crops directory
  and one read back a legend from a different figure before catching itself.
  A cross-figure identifier scan came back clean, and the "no candidate got
  this" facts all turned out to be genuinely hard (a recoloured bar, an absent
  wavenumber axis, the printed space in `200 µ m`, minor-phase attributions)
  rather than out-of-place content.
* **qwen3.6-35b-a3's zero-token failure is a `max_tokens` effect — CORRECTED
  2026-08-12.** This section previously called it a binding gap
  ("GenericMTMDChatHandler mis-positions KV after splicing the image
  embedding... a binding gap, not a model limit"). That was wrong, and so was
  the follow-up guess that a dedicated `Qwen3VLChatHandler` would fix it.
  Measured, same model, same mmproj, same image, only `max_tokens` varying:

      short prompt, max_tokens 300    -> 138 tokens   OK
      long  prompt, max_tokens 300    -> 300 tokens   OK
      long  prompt, max_tokens 1400   ->   0 tokens   FAILS

  Both handlers produce tokens at 300; both produce nothing at 1400. The
  `find_slot: non-consecutive token position` lines appear in the WORKING runs
  too, so they are noise, not the cause — which is exactly why they misled the
  original diagnosis. The bake-off harness used `max_tokens=1400`, so this
  model's 126/192 was scored through the CLI (`-n 1400`, which handles it)
  while the python path returned nothing at the same budget.

  Threshold between 300 and 1400 is not yet bisected. Practical consequence
  for LLMVP vision: bound `max_tokens` on the vision endpoint and treat a
  zero-token vision response as a budget symptom, not a broken model.
  Repro: `dev/deepstack_handler_probe.py`.

---

# Addendum, 2026-08-12 — paired blind re-judge of the endpoint answers

Two questions, one run: does the 2,048 `max_tokens` cap suppress the endpoint's
real quality, and does `mineralogy`'s large fabrication count survive at full
budget?

**Method.** The same 10-figure references and rubric. For each of the 5 figures
whose first endpoint run hit the cap, ONE judge received ONE bundle holding the
capped answer and an uncapped re-run (server default, 16,384) as anonymous
candidates A/B under a per-figure shuffle. Pairing them inside one bundle is the
point: a plain re-score compares today's judge against this morning's, and
judge-strictness drift would be indistinguishable from a real change. Judges
were not told a fabrication count was under investigation.

| figure | capped | untruncated | fabs capped | fabs untrunc |
|---|---|---|---|---|
| card_ocr | 18/18 | 16/18 | 2 | 2 |
| ir_spectra | 9/18 | 9/18 | 1 | 2 |
| refidx_2panel | 15/18 | 15/18 | 1 | 2 |
| mineralogy | 16/18 | 16/18 | **13** | **12** |
| lunar_b | 13/18 | 12/18 | 0 | 0 |
| **total** | **71/90 (78.9%)** | **68/90 (75.6%)** | **17** | **18** |

**The two are the same within noise, and the cap explains none of it.** The
first reading of this table said "removing the cap is NET NEGATIVE"; that is
WRONG and the error is methodological, not arithmetic. The uncapped answers are
FRESH GENERATIONS, not the capped ones extended — so every difference between
the columns is sampling variation at temperature 0.2, of which the cap is at
most one contributor and cannot be isolated. Three of five figures are
identical (9/9, 15/15, 16/16); the whole delta is -2 on one figure and -1 on
another. A 3-fact spread over 90 with n=1 per cell supports no causal claim in
either direction.

What the table DOES support: the endpoint's quality is stable across budgets,
so the cap was never the confound the morning's reading feared. Leave the
generation budget alone. Truncating a fully formed answer is not a quality
lever, and nothing here argues for making one.

**A real defect, independent of the scores.** The vision path bypasses LLMVP's
FSM extraction AND its `single_turn` seal, so muse's known post-answer orbit
runs unchecked. Judges independently reported duplicated restatements on three
figures and, on `lunar_b`, the raw marker `<|start|>assistant to=user<|message|>`
verbatim mid-answer. It appears in CAPPED and UNCAPPED answers alike, so it is
a property of the path, not of the budget — which is also why it explains the
`lunar_b` token anomaly (2,048 once, 1,219 next) without implicating the cap:
one sample orbited, the other did not.

The fix is the SEAL, not a smaller budget. Honouring the family's own
end-of-turn token stops a finished turn from restarting; that is the opposite
of truncating a fully formed answer, and it is what the text path already does.

**The mineralogy result is REAL and STABLE — 13 vs 12 — but it is a MISS ON A
HARD TASK, not fabrication.** The rubric's term is "fabrication" because it
scores any stated-but-absent content that way, and this addendum first carried
that word into the analysis. The operator reviewed the figure against the
reference and rejected the framing, correctly: the disputed segments are 1-5 %
slivers a few pixels tall in a 13-bar stacked chart. Getting them wrong is a
resolution limit, not confabulation, and the distinction changes the remedy —
you do not prompt a model out of an eyesight problem the way you prompt it out
of a habit.

The mechanism is **pattern completion across the bar series**. Nine of the 13
bars genuinely end `… carbonates, iron oxides` at the top of the stack; the
model applies that template to the exceptions. It reads Niger and Bodele with
carbonates (~5 %, ~10 %) where the reference says there are none, and gives the
two Iceland bars quartz/clays/carbonates which they do not have. Same phantom,
same stack position, same thin-segment magnitude.

Two properties support the "resolution, not invention" reading:

* **Large segments are read well.** Morocco illite 38 % vs 38.5 % measured;
  Namib-1 total clays 75 % vs 75.5 %; Kuwait 55 % vs 57 %. Accuracy collapses
  only at the slivers.
* **The percentages sum to ~100**, so an invented sliver has to be funded out
  of a real segment — which is why Iceland-H feldspars reads 40 %/30 % against
  a true 53 %. The error propagates into numbers that ARE real.

Downstream, `numeric_overlap_rate` remains the right advisory: these
percentages will not appear in the paper's prose either way.

**Judge consistency.** The same five capped answers scored 72/90 this morning
and 71/90 here, by different judges — a 1-fact spread, which is the strongest
evidence yet that the earlier 155/192 figure is stable.

**Consequences.**

1. **Leave the generation budget alone.** Neither raising nor lowering it is
   supported. The morning's "the cap is hiding quality" reading and the first
   draft of this addendum's "the cap is helping" reading are both artefacts of
   reading n=1 differences as signal.
2. **Fix the seal and the leak** — the format family's terminators on the
   vision path, and strip control tokens before returning. Justified by the
   markers appearing in returned text at all, not by any score.
3. **Fabrication is a PROMPTING problem, and that is where to work.** The model
   invents composition numbers on segmented figures under a prompt that already
   says "a stated uncertainty is worth more than a confident invention" — so
   the existing nudge is not reaching this failure mode. Candidate levers, none
   yet tested: naming the segmented-figure case explicitly, asking for
   per-segment confidence, or asking it to state which categories it can and
   cannot resolve BEFORE giving percentages. Test them against these same 5
   figures and this same paired protocol; the references and rubric are the
   held-out set that makes that measurable.

---

# Muse detection tool: NO-GO, 2026-08-12

Proposed port: use muse's "native object detection" to get bounding boxes for
chart elements, then crop-and-requery to fix the thin-segment misses. Killed
before implementation. **Muse has no detection capability, and asking for one
produces confabulated coordinates that look exactly like real ones.**

**Provenance of the error, because it was mine.** The HF *blog* says the model
does "open ended object detection ... returning structured JSON output with
bounding box coordinates", and I relayed that as fact. The MODEL CARD and the
Meta developer docs both state the opposite — text+image in, text out, no
coordinate grounding documented anywhere. A blog sentence is not a capability
statement, and I should have checked the card before proposing a port on it.

**The probe (`dev/muse_detect_probe.py`), on mineralogy.png, a 1406x439 image:**

* "Return the result in your native object-detection format" — the model
  correctly says there are no detectable objects and answers in prose. There is
  no native mode to invoke.
* An EXPLICIT JSON request produces beautifully well-formed output:

      [{"label": "Morocco",  "box_2d": [115, 150, 165, 750]},
       {"label": "Niger",    "box_2d": [175, 150, 225, 750]},
       {"label": "Bodele",   "box_2d": [235, 150, 285, 750]}, ...]

  Which is fabricated. `x1` is an exact arithmetic progression (+60, nine times
  running), every width is exactly 50, every box spans y 150-750 — and the
  image is **439 pixels tall**, so 10 of 10 boxes fall outside it. This is a
  generated grid wearing the shape of a measurement.
* "Point to the Iceland-M bar" returns normalised coords for the 12th of 13
  bars at x~0.70 of image width. The left panel ends at x~690 of 1406, so the
  bar is near x~0.45. Confidently wrong, no hedge.

**This is the SAME failure as the mineralogy segments, in coordinate form** —
plausible structure generated where measurement was required. Building a crop
loop on it would have fed invented crop regions into a pipeline whose whole
purpose is fidelity, and the crops would have been of the wrong regions with no
error raised anywhere.

## WHAT THIS PROBE DOES *NOT* ESTABLISH (operator's caution, and he is right)

Two different claims, and only the first is settled:

* **SETTLED: muse has no native detection mode to invoke.** Model card,
  developer docs and the model's own answer all agree.
* **NOT SETTLED: that no prompting strategy can elicit usable coordinates.**
  The probe is three prompt shapes on ONE image and is a weak test of the
  general claim. Its most obvious flaw: **it never told the model the image
  dimensions.** A model asked for pixel coordinates in a space whose size it
  was never given cannot do better than guess a plausible range — which is
  precisely the failure observed, and it may be the probe's fault rather than
  the model's.

Untested and worth trying before concluding anything: supplying the image size
in the prompt; normalised 0-1 or 0-1000 output instead of pixels; a drawn grid
or ruler overlay; Set-of-Mark style numbered annotation of candidate regions
(which turns "where is it" into "which numbered region is it", a much easier
question); and asking on a CROP rather than a full page strip. Coordinates from
this model are unproven, not disproven — `dev/muse_detect_probe.py` is the
harness to extend.

**What survives.** Crop-and-requery is still the right idea; the coordinates
must come from something that MEASURES. Two sources already in the tree:

1. **PP-DocLayoutV3**, the layout detector inside the PaddleOCR-VL pipeline we
   already run — real detection, already producing the figure crops in
   `databank/figures/`.
2. **Deterministic image analysis** for charts specifically: bar columns are
   findable by column-wise pixel statistics, exactly, in numpy. The reference
   agents did this by hand ("read off pixel positions against the 0-100% axis")
   and it is what made the reference trustworthy.

Neither needs the VLM to know where anything is. Test either against these same
10 figures before adopting.

---

# CORRECTION to 802250f's validation, 2026-08-13

That commit claimed the verifier changes "recover 11 and regress ZERO" against
the live corpus. **The measurement was wrong, and the real number is smaller.**

`rescore.py` compared the WHOLE markdown against each page's prose layer.
Production compares that PAGE's markdown against that page's prose
(`extract_paper`, the `_verify_page(page_md, truth)` call). Whole-document
matching lets a number on page 3 satisfy a truth token from page 7, so every
rate came out flattering.

The live run showed it immediately: extraction success moved 47.8% -> 49.0%
across the fix, not the ~33% jump the rescore implied.

RE-MEASURED PAGE-ALIGNED (page boundaries are recoverable — extract_paper joins
pages with "\n\n---\n\n", and the reconstruction matches the PDF page count on
40/40 papers checked):

    recovered 10    regressed 1    unchanged 60    (n=71)

So the fix is worth keeping — roughly +13% on the papers it touches — but it is
not free. `doi_10.1038_srep01554` went 0.87 -> 0.83 numeric and now fails.
Diagnosed: the spatial filter removed only **0.6%** of that paper's prose, but
that fragment was numeric-dense and matching. A knife-edge against the 0.85
gate rather than systematic over-exclusion, which is the failure mode that
would have mattered.

METHOD NOTE, since this is the second time today: a validation harness that
does not reproduce the production code path measures something else. The tell
was available immediately — the live pre/post rate barely moved while the
rescore claimed a large gain — and disagreement between a harness and
production should be treated as the harness being wrong until shown otherwise.
