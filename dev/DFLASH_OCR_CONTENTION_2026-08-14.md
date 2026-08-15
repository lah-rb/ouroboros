# Draft and OCR on the same card: both survive

*Measured 2026-08-14. Probe: `dev/dflash_ocr_contention.py`. muse-glimmer-30B
on the 3090 at 8 seats; OCR (paddle, 12-page paper) on the 3060; draft, when
present, also on the 3060. Text throughput sampled in whole rounds WHILE the
OCR job is in flight.*

## Result

| | 3060 = OCR only | 3060 = draft + OCR |
|---|---|---|
| text, solo | 79.0 tok/s | 100.8 tok/s |
| text, while OCR runs | 73.0 tok/s | **86.1 tok/s** |
| text cost of OCR contention | −7.6% | −14.6% |
| OCR wall clock | 84.1 s | **83.3 s** |
| OCR tool seconds | 72.4 s | 75.5 s |
| extraction numeric rate | 0.9715 | 0.9689 |

**Both halves of my earlier recommendation were wrong.**

I predicted the draft would roughly HALVE the OCR lane, extrapolating from the
swarm bench: two tenants on one CUDA device serialize completely (S = 1.043).
The OCR lane is essentially untouched — 83.3 s against 84.1 s of wall clock,
with tool seconds 75.5 against 72.4 (+4%, and that figure includes a model load
each run). Extraction quality is unchanged, 0.969 against 0.972.

The swarm result does not transfer because it measured two SUSTAINED decoders
each saturating the device. A draft is neither: 1.63 GB doing short bursts
between verify steps, leaving most of the 3060 idle for OCR to use. "Two
tenants on one device serialize" is true of two streams that each want the
whole card, and false of a small intermittent one beside a large one.

**And the drafting benefit survives contention.** Under OCR load the text lane
runs 86.1 against 73.0 tok/s, still **+18%** — down from +25% uncontended but
far from erased. The draft does make the text lane more sensitive to OCR
(−14.6% against −7.6%, since it now has a stake in the busy card), yet it ends
up comfortably ahead in absolute terms.

## Revised guidance

The earlier conclusion — "the 3060 is worth more as a free OCR device than as a
draft device" — was reasoned from the wrong measurement and should not be
followed. The two uses coexist: **run the draft AND the OCR lane on the 3060.**
Text gains ~18% during OCR and ~25% when the OCR lane is idle, and OCR pays
about 1% of wall clock for it.

That makes drafting worth the wiring for the pipeline workflow after all, not
just for single-seat latency work.

## Caveats

* One OCR paper (12 pages) and 6–7 text rounds per arm. The direction is large
  and consistent; the exact percentages are not tight.
* OCR "tool seconds" include spawning and loading a llama-server per run, which
  inflates both arms equally but blunts the resolution of the +4%.
* Sustained OCR (a full extraction sweep, back-to-back papers) is untested —
  this measures one job. A continuously saturated OCR lane may contend harder.
* Still llama-server only: llama-cpp-python exposes no model-drafting API, so
  none of this is reachable from LLMVP as it stands.
