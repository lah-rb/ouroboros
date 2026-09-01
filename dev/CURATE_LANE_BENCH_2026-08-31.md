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

> **CORRECTION (2026-09-01, second-reviewer pass).** The "degrades" and
> "44% slower" readings above are a methodology artefact, not a property of
> the engines. The bench rotated through a doc set, so each concurrency arm
> ran DIFFERENT documents — and the higher arms drew larger ones:
>
> | conc | tok/doc (gpt-oss) | prefill tok/s | docs/h | tok/doc (muse) | prefill tok/s | docs/h |
> |---|---|---|---|---|---|---|
> | 1 | 17,513 | 621 | 127.7 | 17,219 | 176 | 36.8 |
> | 2 | 18,912 | 621 | 118.2 | 18,648 | 177 | 34.2 |
> | 4 | 23,630 | 581 | 88.5 | 22,950 | **184** | 28.9 |
>
> The conc=4 arm carried 35% more tokens per document; docs/hour fell 31%;
> aggregate prefill fell 6.5% for gpt-oss and ROSE 4.5% for muse. The
> "113 s sequential vs 163 s parallel" comparison set 4× the small conc=1
> doc against the four larger conc=4 docs — 70k tokens against 94.5k. At
> equal tokens it is 152 s vs 163 s, within single-run noise.
>
> **Corrected finding: aggregate throughput is FLAT in concurrency.** The
> device is prefill-saturated, so concurrency neither buys nor costs
> aggregate throughput. The physics paragraph below stands; the empirical
> "parallel is slower" does not, and the operator's scepticism of it was
> warranted — this would have been the first time parallel-in-aggregate lost
> to serial on this hardware, and it did not. One clean confirmation the
> data does give: the single-seat gpt-oss config QUEUED its four calls
> (per-call 39/76/121/163 s, a staircase) while the swarm config ran them
> truly concurrently (171/169/170/169 s), and both arms finished in the same
> total time. Same tokens, same wall clock, parallel or serial — that is
> what saturation looks like.
>
> Consequences: `remote_text_seat` was raised from 3 to 4 (concurrency is
> free in aggregate and covers booking/gate gaps; a lane that can never run
> is dead weight), and the earlier suggestion to LOWER it to 1–2 is
> withdrawn — it rested on this reading. The bench now runs the same
> document set at every concurrency so total tokens are identical across
> arms.

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

---

# Quality: does gpt-oss reproduce muse's verdicts?

Speed said swap; this asks whether we can afford to. 10 accepted papers
spread across the size range (709 → 152,212 tokens) plus **10 denied** as a
control, on the **production prompt** (`prompts/curator/review_paper`
rendered through the real `PromptRenderer` with the live mission objective,
same turn shape, `max_tokens` 4096, `t*0.4`), parsed with the production
`parse_llm_json`.

**Why the denied arm exists.** Every pack on disk is `accepted`, so an
accept-only test is unfalsifiable — a model that accepts unconditionally
scores 100%. `papers.jsonl` carries 1,466 `review_status: denied` records,
which makes the test able to fail. A false ACCEPT is also the expensive
error: it pollutes the corpus.

**Why muse was re-run.** Comparing gpt-oss against muse's historical verdicts
without knowing muse's own reproducibility would attribute ordinary sampling
noise to the challenger. muse re-run against its OWN past verdicts is the
noise floor.

| | accepted | denied | overall | decode failures |
|---|---|---|---|---|
| **muse-glimmer-30b-swarm** (noise floor) | 8/9 | **10/10** | **18/19 = 95%** | 0 |
| **gpt-oss-120b-a5** | 7/8 | 8/9 | 15/17 = 88% | **2** |

## Findings

**1. The accuracy gap is NOT statistically distinguishable.** Fisher exact on
agreement gives **p = 0.59**. At this n the 88 vs 95 difference is one or two
papers. This sample cannot rank the two models on accuracy, and any writeup
claiming it does is overreading.

**2. muse does not reproduce itself either — the floor is 95%, not 100%.**
It flipped `doi_10.1038_srep29254` from its own historical accept to deny.
So ~5% verdict churn is baseline, not a challenger defect.

**3. The one substantive gpt-oss error is a FALSE ACCEPT, and it is real.**
`doi_10.26896_1028-6861-2019-85-7-7-15` — direct ICP-AES of gasoline,
kerosene and mineral oil — was denied by muse historically AND on re-run
(fuels are not a named material system), and accepted by gpt-oss. Two
independent muse judgements against one gpt-oss judgement makes this an
error rather than a coin flip, and it is in the direction that costs most.

gpt-oss's other disagreement is arguably the STRICTER read: it denied
`doi_10.5755_j02.ms.25190` because the quantitative values live only in VLM
figure readings.

**4. The concrete operational defect is reliability, not judgement.**
gpt-oss hit **2 hard `llama_decode` failures in 20 papers**, muse zero:

```
Fatal Decode Error at Pos 0, Batch size 2048        (on the SMALLEST doc, 709 tok)
Fatal Decode Error at Pos 19708, Batch size 1: llama_decode failed (code -3)
```

muse processed both of those documents without incident, so this is
gpt-oss-specific, not bad data. `p = 0.49` on 2-vs-0, so this too is
under-powered — but a decode fault is a mechanism, not a score, and it echoes
the recorded `flow_kv_cache`/gpt-oss corruption history. It needs a root
cause before any swap.

**5. The 131,072 ceiling is shared, exactly as the operator said.** Both
models refused the 152k-token paper with the same per-stream error. Confirms
the retraction above: context is not a differentiator.

## Verdict

**Not yet.** gpt-oss is 3.5x faster and its judgement is not measurably
worse — but "not measurably worse" at n=17 is a statement about the sample,
not the model. Before swapping:

- root-cause the 2 decode failures (they are a mechanism, and 10% of papers
  is not a rounding error at 1,548 queued);
- widen to ~60 papers per arm, which would make a 7-point gap detectable;
- keep the deny arm at ≥50% of the sample.

And none of this blocks the larger win: **contention**. Curate packs 3.2/h
while muse alone sustains 36.8 docs/h uncontended, with `fig_review` holding
4 of 6 local seats. Moving figtext off-box is independent of the model
question and available now.

Repro: `dev/bench_curate_quality.py --model <name> --accepted 10 --denied 10`


---

# Remote lane: the second leak (2026-09-01, second-reviewer pass)

The dedicated remote lanes (`curate_r1-4`, est_kv=0 / seats=0, own resource)
were verified routing to the mac and running in parallel with the local
lanes. Their throughput was nonetheless well below what the engine allows,
and the trace says why. Over the first 40 minutes of the v6 run
(`step_end` events, flow `curate_drain`, by branch):

```
              rounds   declined <5s   did work   median work-round
  LOCAL          87        78 (90%)       9           344 s
  REMOTE         46        40 (87%)       6         1,076 s
```

The 1,076 s per productive remote round is the mac's prefill rate
(~176 tok/s shared three ways) across two long turns — slow, but expected,
not odd. The loss is the **40 of 46 remote rounds that declined instantly**,
every one with the literal reason `nothing unclaimed fits the seat budget`.

Two places still coupled the remote lanes to the LOCAL server after the
est_kv/seats fix:

1. `_curate_doc_budget_chars` — a remote lane holds no local claim, so
   `claim_tokens` was 0 and the function fell to rung 3: a LIVE SNAPSHOT
   OF THE LOCAL ENGINE. Its document budget was therefore whatever cells
   the five local lanes had left, and it returned 0 outright whenever the
   local queue was non-empty. Fixed with rung 2b: a lane with a domain
   sizes its document against that domain's declared `seat_tokens`
   (131,072 for the mac; undeclared assumes the local seat).
2. `WorkerPool._one_unit` — `CapacityModel.admit()` checks `waiting`,
   `serving` and `engine_fatal` on the local feed BEFORE the seats/kv skip,
   so a remote lane was refused whenever the local server had a queue.
   Fixed: a lane with a domain never consults the local model; it is bounded
   by `max_inflight` for its own resource only.

The "heavier material" hypothesis was checked and does not hold: both lane
groups draw smallest-first from one shared claim set, and under the leak the
remote lanes received SMALLER budgets (local leftovers), not larger papers.

Telemetry gap noted in passing: the run trace recorded `inferences: 0` for
a run that completed 15 curate rounds — inference events are not being
emitted from the drain lanes, so per-turn prefill/decode timings had to be
inferred from step durations. Worth its own look.
