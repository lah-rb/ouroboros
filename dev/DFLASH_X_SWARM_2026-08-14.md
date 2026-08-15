# Does drafting still pay once the batch is full?

*Measured 2026-08-14. Probe: `dev/dflash_x_swarm.py`. muse-glimmer-30B on the
3090, dflash draft on the 3060, n_max 8, greedy, 128 tokens/seat. Aggregate
tokens/second — per-seat necessarily falls with concurrency under both arms and
would make a win look like a loss.*

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
