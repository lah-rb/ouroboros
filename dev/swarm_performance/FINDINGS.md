# Swarm serving performance — measured capacity model

**All numbers measured 2026-07-26 on the M1 Ultra (128 GB, `iogpu.wired_limit_mb`
= **113**), gpt-oss-120b-a5, batched engine, llama.cpp b9860 unless noted.**
Raw data in `results/`; harnesses alongside; protocol notes in `METHODS.md`.

This replaces a set of beliefs that were partly extrapolation and partly
uncited. Where a prior claim was wrong, it is named as wrong below rather than
quietly corrected — several of them were mine.

---

## TL;DR — the capacity model in four lines

1. **Seats are nearly free.** 128 concurrent streams cost 2.7% of a 393k pool.
   The real limiter is pool CELLS, not seat count.
2. **Prefill does not parallelize.** Serialization ≈ 1.0 at every width and
   prompt size. Seats cannot help prefill-bound work; only prefix reuse can.
3. **Decode parallelizes 5.4×… but only at shallow context.** The gain decays
   with depth and turns NEGATIVE past ~20k.
4. **Effective throughput ≈ 100 tok/s** on real mixed workloads, and that is a
   DECODE ceiling at realistic context depth — not a prefill limit.

---

## 1. Seat ceiling — seats are not the constraint

`find_max_seats.sh` bisects the max allocatable width, accepting a rung only if
the server reports the seat count **asked for** *and* decodes a concurrent wave
clean. ("It loaded" is not acceptance: on 07-25 a server came up healthy and
wedged on the first decode, after which the sticky Metal `-3` latch failed
everything downstream.)

**Result: 128 seats allocate and decode clean — the top of the ladder, so not a
discovered ceiling.** 128 streams × ~84 tokens is **2.7%** of a 393,216-cell
pool, and 128 seats vs 32 measured **identical wired memory (100.6 GB)**.

Practical width is therefore set by cells:

```
N_practical ≈ (n_ctx × 0.8) / mean tokens per stream
```

| workload | tokens/stream | N at 80% of 524k |
|---|---|---|
| tiny probe | ~84 | ~5,000 (hits `LLAMA_MAX_SEQ` 256 first) |
| swarm worker (5k read + 4k gen) | ~9,000 | **~46** |
| big rewrite (15k + 8k) | ~23,000 | **~18** |

Three ceilings, in binding order: **pool cells**, then **latency**, then
`LLAMA_MAX_SEQ = 256`.

**Consequence, applied:** `max_concurrent_requests` is now optional and
uncapped by default (batched → 128, pool → 1). The old cap of 32 was costing
~40% of achievable throughput.

## 2. Decode ladder — the "~200 tok/s at N=64" claim was TRUE

256-token generations, ~20-token prompts, 2 repeats, **zero errors at every
rung** (`results/decode_ladder.json`, graph `results/ceiling.png`):

| N | aggregate tok/s | per-stream | p50 latency |
|---|---|---|---|
| 1 | 50.3 | 50.43 | 5.1s |
| 2 | 72.6 | 36.86 | 7.0s |
| 4 | 94.4 | 23.92 | 10.8s |
| 8 | 105.8 | 13.41 | 19.3s |
| 16 | 136.1 | 8.62 | 30.0s |
| 32 | 144.7 | 4.55 | 56.6s |
| 48 | 173.0 | 3.64 | 71.0s |
| **64** | **202.5** | **3.19** | 80.9s |
| 96 | 232.4 | 2.45 | 105.5s |
| 128 | **271.7** | 2.16 | 120.2s |

**Batching gain 5.41×, monotonic, still climbing at 128.**

### Two corrections this forced

**"Aggregate saturates toward ~90 tok/s"** (`serving_perf_reference.md` §1) was
an extrapolation from N≤6 — the only widths ever measured. It triples by 128.

**"The ~200 figure is a `sum_of_rates` artifact"** — my own theory, and wrong.
`sum_of_rates` tracks true aggregate within 1–2% at *every* rung. The inflation
mechanism is real but only when N EXCEEDS available seats (queued requests get
credited a `decodeMs` that never counted their wait); it was never what
produced the original number. **An undocumented measurement is not a false one**,
and "I cannot find where this came from" does not license "it must be an
artifact." The experiment took 20 minutes and should have preceded the argument.

## 3. Pool ceiling — 393216 was conservative by 33%

`memguard_boot_probe.sh`, steady wired, idle box. **The real wired limit is 113
GB** (`sysctl iogpu.wired_limit_mb`), not the 116 several configs assumed:

| n_ctx | predicted | measured | under 113 |
|---|---|---|---|
| 393216 | 94.4 | **91.1** | 21.9 GB (old default) |
| **524288** | 104.0 | **100.6** | 12.4 GB ← production |
| 589824 | 108.7 | **105.2** | 7.8 GB (batch-only variant) |
| 655360 | 113.7 | **109.9** | 3.1 GB — boots, 0.8 GB free, NOT viable |

Marginal cost measured at **71.7 KB/token against 72.0 predicted**; the −3.4 GB
gap is a fixed offset, not drift, so the arithmetic predicts new rungs exactly
(655360 was called at 109.9 before probing and measured 109.9).

**The old "BREACH" verdict on 524k was the probe, not the box** — the
swarmclass ladder used memguard's default 100 GB ceiling, and 524k measured
100.6, killed for missing by **0.6 GB against a guard 12 GB below the real
limit.** A conservative tripwire silently became a capacity decision.

**Wired is not the only budget.** At 655360 the box reads 127.1 of 128 GB with
*only the server running* — nothing left for the agent, Python, an editor.

## 4. Prefill does NOT parallelize

`prefill_grid.py`, unique prompts per request (identical prompts would measure
the cache), 13 cells (`results/prefill_grid.json`).

**Serialization = `N × wall(N=1) / wall(N)`.** ≈N would mean free concurrency;
≈1 means fully serialized.

| prompt tokens | N=4 | N=16 | N=48 |
|---|---|---|---|
| 1,000 | 1.20 | 1.35 | **1.39** |
| 4,000 | 1.07 | 1.05 | 1.04 |
| 16,000 | 0.97 | 0.95 | — |
| 48,000 | 0.95 | — | — |

**≈1.0 everywhere, and slightly NEGATIVE at large prompts.** The 1.39× at 1k
prompts is per-request overhead amortizing, not prefill parallelizing.

I pre-registered a prediction of **1.5–2×** (reasoning that prefill has headroom
above ~7.9 TFLOPS single-stream and the batched engine can share chunks in one
`llama_decode`). **Wrong.** `serving_perf_reference` §2's "roughly serialized",
asserted from a single 6-stream observation, was right.

**Consequence: seats cannot help prefill-bound work. The only lever is not
reading the same tokens twice** — see the shared-prefix-cache item in
OPEN_TASKS §11.

## 5. Decode vs CONTEXT DEPTH — the missing axis

`context_curve.py`: fixed N and fixed generation length, sweeping prompt size
so the KV depth varies. Reads the server's `decodeMs` register, so prefill is
excluded by construction (`results/context_curve.json`).

| depth | N=1 per-stream | N=8 per-stream | N=8 aggregate |
|---|---|---|---|
| 2,253 | 40.28 | 12.31 | **98.5** |
| 2,715 | 40.47 | 10.93 | 87.5 |
| 4,563 | 40.90 | 8.44 | 67.5 |
| 11,917 | 29.82 | 4.32 | 34.6 |
| 21,752 | 26.62 | 2.43 | **19.4** |

**Depth and width COMPOUND.** The same depth increase costs **34% at N=1** and
**80% at N=8**. Batching gain by depth:

| depth | N=8 ÷ N=1 |
|---|---|
| 2,253 | **2.44×** |
| 4,563 | 1.65× |
| 11,917 | 1.16× |
| 21,752 | **0.73× — concurrency is a LOSS** |

**Mechanism (inference, fits the data):** weights are read once per step and
amortized across streams, but **each stream's KV is read every step**, scaling
with `N × depth`. Shallow → weights dominate → batching nearly free. Deep →
KV traffic dominates → extra streams add proportional work. The 5.41× ladder
gain and this 0.73× are the same curve at opposite ends.

*Unexplained:* the N=1 curve **plateaus** (29.8 → 26.6 → 28.6 from 12k to 41k)
rather than degrading smoothly. Not the quadratic-attention shape expected.

## 6. Why real workloads land at ~100 tok/s

From the 4,107-request corpus regeneration (c=48, real prompts, mixed
generation lengths): **1,559,528 tokens in 14,899s = 104.7 tok/s effective.**

Decomposed:

| component | value |
|---|---|
| prompt tokens | 1,544,232 → **1,930s at 800 tok/s = 13% of the run** |
| everything else | 12,969s = **87% — decode** |
| decode aggregate while decoding | **120 tok/s** |
| ladder said, N=48, shallow | 173 tok/s |
| shortfall | **30%** |

**Prefill is NOT the bottleneck** — infinitely fast prefill would only take
105 → 120 tok/s. The ceiling is decode **at realistic context depth**. The
ladder's 173 and 271 are laboratory numbers with near-empty KV.

Per-level evidence that fixed overhead is real but bounded — longer generations
run *faster* per token because overhead amortizes:

| level | gen tokens | per-request rate |
|---|---|---|
| low | 95 | 1.71 tok/s |
| medium | 246 | 2.08 tok/s |
| high | 798 | **2.31 tok/s** |

---

## What this changes

**Shared prefix cache (OPEN_TASKS §11) is the top lever, not merely the biggest.**
Prefill cannot be parallelized, so the only way to reduce it is to stop paying
for the same tokens repeatedly. Measured waste on the counterfactual corpus:
**1.03M of 1.54M prompt tokens (67%) were redundant re-reads** of identical
prompts. Swarm fan-outs pay a shared blueprint/contract context once per worker.

**Speculative decoding (OPEN_TASKS §11c) needs re-analysis at depth.** My
break-even (~70% acceptance at N=64) was computed entirely at *shallow* ladder
conditions. SD verifies K draft tokens **against the same KV read** — so where
KV traffic dominates (deep context), SD amortizes exactly the expensive part,
and its break-even acceptance falls. Deep context is also precisely where
batching has stopped helping (0.73× at 21k). **The acceptance gate should be
measured on deep-context swarm prompts, not shallow ones.**

**Model verdicts have a shelf life.** llama.cpp b10131 (bumped 2026-07-26)
allocates ~1 GB less wired for an identical config, and qwen3-next reportedly
went 10 → 55 tok/s across recent builds. Any tier assignment whose *reason* was
throughput is provisional until re-measured.

---

## Method notes worth keeping

- **Acceptance is not "it loaded."** A rung passes only if the server reports
  the width asked for AND decodes clean. The 07-25 server came up healthy and
  wedged on first decode.
- **Skip loudly.** Cells exceeding 80% of the pool are printed as skipped with
  the arithmetic. A gap in a results table reads as coverage.
- **Unique prompts.** Prefix caching is the thing this project has worked
  hardest on; identical prompts measure the cache, not prefill.
- **`cachedPrefixTokens` ≈ 1809 × N is CORRECT** — the static prefix forked per
  seat from `SEQ_STATIC`. The prefill grid's contamination guard mis-flagged it
  by comparing against total prompt tokens; subtract it instead.
- **Pre-register predictions.** The prefill prediction (1.5–2×) is in the
  harness docstring, written before the run, and the result refuted it. That is
  the point — it stops a wrong model being retrofitted into a right one.

### Harness bugs found in these experiments (all fixed)

| bug | symptom | why it mattered |
|---|---|---|
| serialization vs prefill-time | read 0.52 (worse than serial) | compared `N × prefill` against total wall incl. decode+HTTP |
| contamination guard | fired on every cell | static prefix counted as cache leakage |
| context-curve sign | "+26%" for a 26% SLOWDOWN | inverted delta; read as a speedup |

Two of the three produced *plausible-looking* numbers. Check the metric
definition before believing a surprising result.
