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
