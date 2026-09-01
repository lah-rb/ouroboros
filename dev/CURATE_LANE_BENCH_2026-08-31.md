# Curate-lane model bench — is gpt-oss the throughput lever?

Mac at 192.168.1.209 (M1 Ultra, 113 GB budget), benched remotely over
LLMVP's GraphQL `swapModel`/`createCompletion`. No SSH needed and no code
changed: the agent's curate path already speaks this API.

## Method

16 **distinct** curator docs (`build_curator_doc`: markdown + inlined
figtext), mean ~23,100 tokens — the real payload, taken from the actual
curate queue. Distinct per call because LLMVP reports `cacheHit`, and reusing
one prompt would let the prefix cache serve later reps and turn a prefill
benchmark into a cache benchmark. `freshPrefillTokens` is reported separately
so a contaminated arm cannot pass silently.

The curate turn is **prefill-dominated**: ~23k in, a few hundred out. Any
bench on short prompts measures the wrong half of the workload.

`docs/hour` is TOTAL throughput — N documents divided by wall clock — not a
per-stream rate.

## Results

| model | conc | prefill tok/s (total) | docs/hour (total) |
|---|---|---|---|
| qwen3.8-27b (incumbent on the mac) | 1 | 172 | 32.6 |
| **gpt-oss-120b-a5** | 1 | **621** | **127.7** |
| gpt-oss-120b-a5 | 2 | 621 | 118.2 |
| gpt-oss-120b-a5 | 4 | 581 | 88.5 |
| gpt-oss-120b-a5-swarm-524k (batched) | 1 | 615 | 126.4 |
| gpt-oss-120b-a5-swarm-524k | 2 | 619 | 117.8 |
| gpt-oss-120b-a5-swarm-524k | 4 | 553 | 84.3 |
| **muse-glimmer-30b-swarm** (128 seats, batched) | 1 | **176** | **36.8** |
| muse-glimmer-30b-swarm | 2 | 177 | 34.2 |
| muse-glimmer-30b-swarm | 4 | 184 | 28.9 |

## Findings

**1. gpt-oss is ~3.5x faster than muse on this workload.** 621 vs 176 tok/s
prefill; 127.7 vs 36.8 docs/hour. That is the throughput lever, and it is
large.

**2. Parallelism is NOT the lever, for either model.** Prefill throughput is
flat in concurrency (gpt-oss 621/621/553, muse 176/177/184) and TOTAL
docs/hour *degrades*: gpt-oss 127.7 -> 88.5, muse 36.8 -> 28.9. Four docs
sequentially on gpt-oss take 113 s; four in parallel take 163 s — parallelism
is 44% slower in aggregate.

The reason is that prefill is compute-bound and already saturates the device.
Batched decode multiplexes DECODE across streams; it cannot manufacture
prefill FLOPs. muse-swarm makes this unambiguous: 128 seats available, all
free, and its prefill rate does not move.

This was tested against the configs that actually carry `decode_mode:
"batched"`. An earlier arm of this bench used the single-seat configs, where
concurrency is refused outright (`active=1, limit=1`) — that arm answers
nothing about parallelism and is not the basis for the claim above.

**3. Context is NOT a differentiator.** An earlier draft claimed muse would
reject 38% of the queue on a 32k limit. That was wrong: under `kv_unified`,
`n_ctx` is the SHARED CELL POOL and `model_max_context` bounds any one
stream. Both models carry `model_max_context: 131072`, and muse's swarm pool
(1,441,792) is larger than gpt-oss's (589,824). At a 131k per-document
ceiling **1.5%** of the queue exceeds it, identically for both.

**4. The immediate bottleneck is contention, not model choice.** The curate
lane is packing 3.2 papers/hour today while even muse — the slower model —
sustains 36.8 docs/hour of raw inference uncontended. The local pool is
pinned at 6/6 in flight with `fig_review` holding 4 of the 6 seats. So most
of the shortfall is queueing and flow overhead, not the model. Moving the
figtext sweep off the local box is worth more, sooner, than swapping the
curate model — and the two are independent.

## Not yet established

- **Quality.** Speed is settled; whether gpt-oss curates at muse's standard is
  not, and it is the gating question. A blind A/B over the same papers,
  scored against existing accepted packs, is the next step.
- Single rep per arm (n=1). The gaps are large relative to any plausible
  run-to-run noise, but the numbers are not tight.
- Decode here is 100–256 tokens; a real curate turn writes a summary plus
  pack JSON. Still prefill-dominated, but decode is under-weighted.

## State left behind

The Mac now has `muse-glimmer-30b-swarm` resident (it was `qwen3.8-27b`).
Swaps were clean throughout — `ok: true`, no rollback, no evicted streams.

Repro: `dev/bench_curate_lane.py --model <name> --concurrency 1,2,4`
