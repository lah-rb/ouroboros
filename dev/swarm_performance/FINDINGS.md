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

From the 4,107-request corpus regeneration (c=48, mixed generation lengths):
**1,559,528 tokens in 14,899s = 104.7 tok/s effective.**

**Correction (2026-07-26):** this run's prompts were NOT full prompts. The regen
fed `r["context"]` from the `*_labeling.json` files (`dev/cf_regen_swarm.py:75`),
which is a middle-elided 1510-char preview — see
`dev/ADAPTIVE_THINKING_STATUS.md` §3.5. So the 376-token mean prompt is
correctly measured but describes a **shallow** workload: mean decode depth
≈ 376 + 380/2 = **566 tokens**. That is what makes this run usable as an
optimizer hold-out (§7), and it also explains the 67% "redundant re-read"
figure below — those are distinct turns whose previews collapsed to identical
strings, not repeated work.

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

## 7. Can we derive a swarm optimizer? Not yet — and the reason is structural

`results/context_surface.json` (9 cells, 0 errors, 6 skipped over the 80% pool
guard) is the first data with enough N coverage at fixed depth to locate an
**interior optimum**:

| depth | N=4 | N=16 | N=24 | N=32 | N=64 |
|---|---|---|---|---|---|
| ~4,690 | 60.8 | 76.7 | **79.9** | 70.7 | 68.3 |
| ~12,060 | **38.8** | 32.5 | 28.7 | — | — |
| ~21,950 | **24.7** | — | — | — | — |

**Aggregate is not monotone in N.** At 4.7k depth it peaks at N≈24 and falls
15% by N=64. At 12k depth N=4 is already past the optimum.

### The power-law model fits the easy region and fails the axis that matters

Fitting `log A = a + k(D)·log N + b·log D` over all 30 measured cells (ladder +
curve + surface) gives MAPE 18.0%, max error 76.3%, and:

| depth | k observed | k model | |
|---|---|---|---|
| 148 | 0.313 | 0.295 | OK |
| ~2,400 | 0.430 | 0.173 | model low |
| 4,691 | 0.132 | 0.143 | OK |
| 12,045 | **−0.012** | +0.102 | **SIGN WRONG** |
| 21,880 | **−0.138** | +0.075 | **SIGN WRONG** |

Two problems, the second fatal:

1. Past ~12k depth the model predicts concurrency *gains* where measurement
   shows *losses*.
2. `A = r(D)·N^k(D)` is **monotone in N by construction**, so it cannot express
   an interior peak — the one quantity an optimizer exists to compute. It can
   only ever return N=max (k>0) or N=1 (k<0).

**Hold-out against the regen (N=48, depth 566): predicted 127.4 vs observed
120.0, +6.1%.** Do not read this as validation. That point sits where the
ladder samples N=48 directly, so it is interpolation along N at shallow depth
and tests none of the depth machinery.

### A mechanistic alternative was tried and is worse

Roofline reasoning (weights read once per step and amortized over N; each
stream's KV read every step, scaling N·D) gives `A = N/(a + kv·N·D)`, optionally
`+ c·N²` for scheduling contention:

| model | params | MAPE | max err |
|---|---|---|---|
| power law `r(D)·N^k(D)` | 4 | **18.0%** | 76.3% |
| roofline `N/(a+kv·N·D)` | 2 | 66.7% | 96.9% |
| roofline + contention | 3 | 44.9% | 86.2% |

Both are worse, and the contention form yields a **depth-independent** N*=75
against measured optima of 128 / 24 / 4. The mechanism in §5 is a correct story
about *where the time goes*; it is not yet a correct model of *aggregate vs N*.

### What is actually missing

A true interior peak is measured at exactly **one** depth (4,691). There is no N
sweep between 148 and 4,691 — which is precisely the swarm regime (the regen sat
at 566). `results/swarm_regime_surface.json` fills it: sizes 256/512/1024/2048 ×
N ∈ {4,8,16,24,32,48,64,96}. Until N*(D) is measured across that band, the
honest optimizer is **interpolation over the measured surface**, not a fitted
law — and it should refuse to extrapolate past the sampled depths rather than
return the confident wrong sign the power law gives today.

## 8. The static prefix is ~94% FREE (measured by intervention)

*Pre-registered in `PREREG_static_prefix.md` before the run; harness comparison
`results/nopersona_surface.json` vs `results/swarm_regime_surface.json`.*

**Luke's question:** every request carries the 1809-token knowledge prefix as KV
depth. Is that a real throughput tax, or is it free — meaning the depth axis has
a floor that isn't actually costing anything?

**It is nearly free.** Fitting `D_eff = D_private + w·D_static` jointly over all
64 cells of both configurations:

| w | MAPE |
|---|---|
| 0.00 — static FREE | 5.72% |
| **0.06 — best fit** | **5.28%** |
| 1.00 — costs like fresh | 10.14% |

**A static token costs ~6% of a private token.** The 1809-token prefix
contributes ~109 effective tokens of depth, not 1809.

### Why: shared cells, and the fingerprint that proves it

`batched_engine.py:881` forks the persona head with
`ctx.memory_seq_cp(head.seq, slot.seq, -1, -1)`. In llama.cpp that **adds a
seq_id to existing cells rather than duplicating K/V** — one physical copy
serving all N streams.

The raw deltas show this more clearly than the fit. Removing 1737 static tokens:

| N | size 256 | size 2048 |
|---|---|---|
| 4 | **+22.2%** | +11.5% |
| 24 | +2.8% | +2.2% |
| 96 | **+5.4%** | **−1.2%** |

**The gain SHRINKS as N grows.** If static were per-stream KV traffic, removing
it would help *most* at high N, where KV traffic dominates (measured elasticity
rises from −0.42 at N=4 to −1.41 at N=96). It does the opposite. That is the
signature of a cost paid **once per decode step regardless of N**.

### Why observation could not answer this, and intervention could

`D_static` is CONSTANT at 1809 across every previously collected cell, so `w` is
mathematically **unidentifiable** from that data — any fit merely reallocates a
constant between terms. An earlier refit that appeared to favour "not free"
(private-depth MAPE 16.3% vs total 8.2%) was measuring nothing of the sort. Only
varying `D_static` identifies `w`. **The one-line config change decided in one
run what 62 observational cells could not.**

### Verification caught a self-undoing intervention

The first attempt truncated the derived cache `data/*.tokens.bin` to 8 tokens.
The server detected it as stale and **rebuilt it from `SOUL.md`**, restoring all
1809 tokens. The run returned byte-identical numbers (82.3 tok/s, depth 2253) —
a *perfect null result that meant nothing*. Only an explicit "did the
intervention take?" check (implied static = `cachedPrefixTokens`) caught it. The
real knob is the SOURCE (`prompt.persona_file`), not the cache: 8686 → 29 chars
took the prefix 1809 → 72 tokens.

**Always assert the intervention landed before interpreting its result.** A null
is indistinguishable from a no-op.

### Consequences

1. **Trimming the knowledge prefix is NOT a throughput lever.** Shrinking
   SOUL.md by 96% bought 5.4% at N=96 and −1.2% at N=96/size 2048. Spend the
   prefix budget on capability; it is very nearly free.
2. **Depth accounting must use private tokens.** All depths quoted in §5-§7 are
   TOTAL and overstate effective depth by ~1700. `N*` tracks private depth:
   private 148 → N*=128; 444-1519 → N*=96; 2882 → N*=24; 10250 → N*=4.
3. **The regen hold-out depth has now been wrong twice, both mine.** Original
   566 (static ignored, i.e. w=0), then "corrected" to 2375 (w=1). The truth is
   `566 + 0.06×1809 ≈ 675` — the ORIGINAL figure was nearly right and the
   correction made it worse. Recorded because the correction was committed and
   argued for.
4. **OPEN follow-up — the pool-fit gate may be over-conservative by N×1809.**
   If cells are shared for MEMORY as they are for time, the guard should count
   the static prefix ONCE, not per stream. At size 8192 it skipped N=64 as
   452,160 > 419,430; counting static once gives 64×5256 + 1809 = **338,193,
   which fits**. This is an inference from the same mechanism, NOT measured —
   the w=0.06 result is about decode time, not allocation. Test by attempting
   the skipped cell directly.

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
