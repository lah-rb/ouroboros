# Speculative decoding on the 3090/3060

*Three measurements, in the order they were taken: dflash alone, dflash
against a full batch, and dflash sharing the 3060 with the OCR lane. The
second and third each overturned a conclusion drawn from the one before,
so read them in order.*

## Part 1 — dflash alone: NET POSITIVE, and the M1 prediction holds

*Measured 2026-08-14. Probe: `dev/dflash_cuda_probe.py`. Target
muse-glimmer-30B-kquant-dynamic on the 3090, draft dflash-kquant (1.63 GB),
greedy (temp 0 / top_k 1), `-c 4096`, n_predict 200.*

## Result

| arm | tok/s | vs baseline | acceptance | mean accepted run |
|---|---|---|---|---|
| baseline (no draft) | 26.35 | — | — | — |
| dflash n_max 3, draft on the 3090 | — | — | — | failed to start |
| dflash n_max 3, draft on the 3060 | 29.19 | **+11%** | 0.332 | 1.99 |
| dflash n_max 8, draft on the 3060 | **36.33** | **+38%** | 0.183 | 2.44 |

**The M1's prediction was right.** That write-up closed dflash for Apple silicon
with "the 3.1x on an RTX 5090 is the same physics pointing the other way —
re-run this probe on the 3090/3060 rig before assuming either result". It is
the other way.

The comparison that makes it physics rather than luck is the acceptance rate.
At n_max 8 the M1 measured 0.185 and this rig measures 0.183 — the SAME draft
quality, since it is the same draft model against the same target. The M1 turned
that into **-50%** and this turns it into **+38%**. Nothing about the prediction
changed; only the cost of the verify step did. Speculative decoding trades
bandwidth-bound sequential decode for compute-bound parallel verify, and these
two machines sit on opposite sides of that trade.

Note also that longer drafts help HERE and hurt THERE. On the M1 the default
n_max 3 was the best case and 8 was catastrophic; here 8 beats 3 by a further
25 points despite acceptance falling 0.332 -> 0.183, because a wider verify
batch is nearly free on a GPU with compute to spare.

## The draft goes on the OTHER card

Same-card placement never started:

```
llama_init_from_model: failed to initialize the context: dflash requires
  ctx_other to be set (this warning is normal during memory fitting)
srv load_model: [spec] failed to measure draft model memory
```

`--spec-draft-device CUDA1` puts the draft on the 3060 and works first time.
That is the same lesson as the swarm bench from a different angle: this rig's
second card is the place to put anything that would otherwise contend with the
target, and drafting is exactly such a thing.

## The engagement guard fired on a REAL result, and that is worth recording

The probe marks an arm INERT when it cannot see drafting statistics, because
the M1 run was corrupted by `--spec-type` defaulting to `none` — three arms at
byte-identical tok/s that read as "no speedup" rather than "never engaged".

That guard was carried over with the M1's log pattern (`n_drafted=` /
`n_accept=`). This llama.cpp prints:

```
draft acceptance = 0.18310 (  117 accepted /   639 generated), mean len =  2.44
```

so the pattern matched nothing and both working arms were reported INERT — a
+38% result declared meaningless. The guard was right to exist and wrong in its
string. **A guard keyed to a log message is only as good as that message**, and
a ported guard needs its pattern re-confirmed against a real log on the new
build before its verdict is trusted in either direction.

## Open

* Same-card drafting is unexplored — it fails during llama.cpp's memory
  fitting, not obviously from real exhaustion (muse 18.3 GiB + draft ~1.5 GiB +
  KV should fit 24 GiB). Worth one look if the 3060 is ever wanted for OCR at
  the same time, since these two uses now compete for it.
* n_max above 8 is untested here; the M1's collapse at 16 may not repeat.
* Not yet wired into LLMVP — llama-cpp-python exposes no model-drafting API
  (dev/DFLASH_SD_2026-08-14.md), so this remains a llama-server capability.

---

## Part 2 — drafting against a full batch

*Probe: `dev/dflash_x_swarm.py`.*

## Result

| seats | no draft | dflash | gain | acceptance |
|---|---|---|---|---|
| 1 | 26.0 | 41.6 | **+60%** | 0.237 |
| 2 | 48.1 | 61.6 | +28% | 0.212 |
| 4 | 72.2 | 92.4 | +28% | 0.193 |
| 8 | 83.8 | **104.6** | **+25%** | 0.149 |

**Two expectations went in, both wrong.**

The operator's was that drafting gets STRONGER per seat, since each seat's
decode is slower under concurrency. It does not: the gain falls monotonically,
+60% -> +25%, and acceptance decays with it (0.237 -> 0.149). More seats make
each draft LESS useful, not more.

Mine was that the gain would vanish or go negative, because speculation and
batching spend the same surplus — a bandwidth-bound forward pass reads every
weight to make one token and leaves compute idle; speculation fills that idle
compute with drafted tokens for one seat, batching fills it with one token from
each of many seats. That reasoning predicted the DIRECTION correctly and the
MAGNITUDE badly. The gain shrinks and then plateaus around +25%; it never
turns negative through 8 seats. The two wins do stack, just not additively.

## What decides the wiring question

Batching is the larger lever by a wide margin:

```
drafting alone (1 seat)      26.0 -> 41.6    1.6x
batching alone (8 seats)     26.0 -> 83.8    3.2x
both                         26.0 -> 104.6   4.0x
```

So for a workload that can fill seats, drafting is the second-order win — worth
+25%, not worth reorganising anything around.

**And on this rig it is not free, because it costs the second card.** The
draft must live on the 3060 (same-card placement fails during llama.cpp's
memory fitting), and the 3060 is also where the OCR lane belongs — the swarm
bench put a tenant there at serialization 0.0011, i.e. genuinely free beside
the 3090. Two tenants on ONE device serialize completely (S = 1.043), so
running the draft and paddle together on the 3060 would make them take turns.

For the scraper/curator pipeline, which runs OCR continuously, that trade is
poor: +25% on the text lane in exchange for roughly halving the OCR lane. The
3060 is worth more as a free OCR device than as a draft device.

**Where drafting IS worth it: single-seat, latency-shaped work with the 3060
otherwise idle** — a serial agent turn, an interactive session, a boss/user-sim
pair. There it is +60% and nothing else is competing for the card.

## Caveats

* Seats above 8 untested; the gain is still 25% at 8 and may hold or decay.
* Acceptance falling with seats is consistent with a ragged batch — seats
  accept different numbers of drafted tokens, so the verify batch cannot stay
  full — but no counters were read to confirm that mechanism.
* n_max fixed at 8 (the best single-seat arm). The optimum may move with seat
  count and was not swept.
* Not reachable from LLMVP: llama-cpp-python exposes no model-drafting API, so
  every number here is llama-server only.

---

## Part 3 — drafting beside the OCR lane

*Probe: `dev/dflash_ocr_contention.py`.*

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

---

## What to do with this, in one place

| workload | 3060 holds | text gain |
|---|---|---|
| single seat, OCR idle | draft | **+60%** |
| 8 seats, OCR idle | draft | **+25%** |
| 8 seats, OCR running | draft + OCR | **+18%**, OCR pays ~1% |

Run the draft AND the OCR lane on the 3060. Batching remains the larger lever
(3.2x against drafting's 1.6x, 4.0x together), so fill seats first — but
drafting is additive on top and does not cost the OCR lane, which is the
opposite of what Part 2's reasoning predicted before Part 3 measured it.

The blocker is plumbing, not physics: llama-cpp-python exposes no model-drafting
API (`dev/DFLASH_SD_2026-08-14.md`), so every number here is llama-server only
and unreachable from LLMVP as it stands.
