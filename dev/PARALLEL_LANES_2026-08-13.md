# Parallel lanes: from swarm fan-out to multi-model multi-flow

*Measured 2026-08-13, off the 9 h overnight scrape
(`~/corpora/ouroboros-spectra/.agent/traces/50e0f9b4b8eb_20260813T045055.jsonl`).
Probe: `dev/gpu_contention_probe.py` → `dev/gpu_contention_results.json`.*

## 1. The overlap today is ZERO

Union/intersection of every `command_run` and `inference_call` span in the
overnight trace:

| | wall | share |
|---|---|---|
| run | 540.6 m | |
| OCR (`command_run`, n=84) | 384.6 m | 71.1% |
| inference (`inference_call`, n=112) | 142.1 m | 26.3% |
| **overlap** | **0.0 m** | **0.0%** |
| idle / other | 13.9 m | 2.5% |

Not "partial" — the OCR subprocess and the resident model never worked at the
same instant across nine hours.

**Why.** Every command is `acquire_catalog / acquire`; every inference call is
`acquire_catalog / tag_papers`. `acquire_overlap_actions.py` gathers the OCR
lane against the *HTTP* lane inside ONE step, and HTTP is the 13.9 m residual.
The 142 m of `tag_papers` runs in the NEXT step, alone. The overlap was built
one step too narrow — which is exactly what its own docstring says it is:
*"the finest granularity a serial runtime offers without background-task
machinery."*

Ceiling on a perfect overlap: `max(384.6, 142.1)` = 384.6 m, i.e. 156 m
(28.9%) on the table — IF one GPU can do both at once.

## 2. It can, at about two thirds efficiency

`dev/gpu_contention_probe.py`, P0.b's protocol adapted: fixed work per leg,
three arms, production flags (`--vl-backend llamacpp`, `--vl-parallel 4`,
2 median-sized PDFs; 14 sequential completions × 640 max tokens at
temperature 0 against the live muse server on :8008).

| arm | wall |
|---|---|
| A — OCR alone | 184.3 s |
| B — inference alone | 197.8 s (3,718 tok @ 19.29 tok/s) |
| **C — both, gathered** | **260.2 s** |
| ideal (`max(A,B)`) | 197.8 s |
| serialized (`A+B`) | 382.1 s |

**Serialization = 0.339** — 34% of the way from ideal to fully serial, so
**66% of the theoretical overlap is realized**. 121.9 s recovered off 382.1 s
(31.9%).

Neither leg is free: OCR slows **1.39×**, inference **1.315×**
(19.29 → 14.63 tok/s). This is a genuine third regime, between the two
already on record in `MULTI_MODEL_PLAN.md`:

* in-process dual model (P0.b) — concurrent ≈ solo **SUM**, serialization 1.0
* cross-process dual model (2026-07-17) — concurrent == slower leg, ≈ 0.0
* **paddle-4-slot OCR × muse-30B decode — 0.339**

The middle value is what `--vl-parallel 4` being "the measured saturation
point" predicts: paddle already keeps the GPU busy, so the second tenant
takes a real cut rather than filling idle gaps. Extrapolating the 2026-07-17
result here would have overstated the win by ~1.75×.

**Fidelity is untouched, both directions.**

| check | result |
|---|---|
| inference text identical (temp 0), solo vs concurrent | **14/14** |
| OCR markdown chars identical | **2/2** (82,421 and 37,771) |
| numeric / span recall | identical to full float precision |

No greedy divergence — the P0.b hazard is in-process only, and this confirms
it for a third pair.

## 3. What that buys the real run

The probe's legs were near-equal (184 s vs 198 s); production is 2.7:1
OCR-heavy, so inference hides inside the OCR window and the OCR tail runs
alone. Applying the measured slowdowns:

```
phase 1 (both)    186.9 m   inference completes; OCR advances 134.4 m-equiv
phase 2 (OCR)     250.2 m
total             437.0 m   vs 526.7 m of serial work   -> 89.7 m saved (17.0%)
wall 540.6 m  ->  450.9 m   (7.5 h vs 9.0 h)
```

**~1.5 h per 9 h run**, ~19% more corpus per night. Not the 156 m the naive
ceiling promised — 90 m of it is real, and 66 m is eaten by contention.

Caveat: n=1 per arm. The 1.39/1.315 slowdowns are single measurements; the
verdict (partial, not free, not serial) is robust to a fair amount of noise,
the projection is not.

## 4. Two levers that are NOT the answer

* **Persistent VL server.** Measured, not guessed: spawn+teardown is **6.5 s**
  per dispatch (leg wall 184.3 s vs 177.8 s of per-paper work), ≈ 9 m across
  the run's 84 dispatches — 2.4%. Worth doing eventually for lane ergonomics,
  worthless as a throughput play.
* **Cache / prefill.** The `cache hit-rate 0% (0/112)` audit flag is cosmetic
  here. Prefill is 10.4 m of 540.6; decode is 92.7% of inference time. Perfect
  caching saves under 2%.

And one lever that is already spent: `thinking_mode: low` is set on
muse-glimmer, and reasoning is *still* **79.8%** of output (119,446 of 149,717
tokens) — consistent with the config's own note that low leaves 85–92%. The
142 m cannot be cut much. Hiding it is the available move.

## 5. The swarm pattern, and what transfers

`agent/actions/fanout.py` + `contract_swarm_actions.py:2034` are six parts:

| part | transfers to lanes? |
|---|---|
| `pool_fit_width()` — ask the SERVER its real budget (`kvPoolTokens`), size to 80% | shape yes; the resource becomes GPU-time, not KV cells |
| `asyncio.Semaphore(width)` wave gate | yes — per resource class, GPU-exclusive = 1 |
| `asyncio.gather` over STATELESS completions | partly — lanes are long-lived, not stateless |
| per-worker exception containment, never raises | yes, directly |
| **serial post-hoc booking** — no concurrent mission mutation | **no — this is the one that breaks** |
| `FanoutPerf` JSONL sidecar | yes, directly |

The swarm is safe because it *defers* every shared-state write until after the
gather. Lanes run for hours and cannot defer. They need the other half of the
same idea: **disjoint ownership**.

## 6. Which is already built

`scholarly_actions.py:298`, `read_databank`, written before this run:

> *"TWO WRITERS, TWO FILES... The scraper owns papers.jsonl; the extractor
> owns extraction.jsonl and overlays it here.* **That is the one thing
> standing between here and running acquisition and OCR concurrently**,
> *which is worth a lot: the extractor flow set contains ZERO LLM turns, so
> gpt-oss idles for the entire OCR stage."*

Everything else the two-lane configuration needs also exists:

* `flow_sets.py:243` — the extractor set is a pure GPU-subprocess lane,
  *"Contains ZERO LLM turns"*; the scraper set is HTTP + LLM. Disjoint
  resource profiles, by construction.
* `mission_runner.py:build_and_save_mission` already takes `agent_dir` and
  `working_directory` SEPARATELY (the container adapters split them).
  Two `.agent/` roots over one corpus is an existing capability.
* `LocalEffects` resolves paths against `working_directory`, independent of
  where `.agent/` lives.
* Per-call model routing shipped in Phase 4 (`config_overrides["model"]`,
  flow steps carry `config: model:`).

**The blocker is not the model layer.** It is `loop.py:388` — one
`await execute_flow(...)` per cycle, one step at a time.

## 7. The design

### 7a. Two lanes, today, no runtime work

The minimum viable realization needs no new machinery:

```
~/corpora/lanes/scrape/.agent/     flow_set: scraper    working_directory: <corpus>
~/corpora/lanes/extract/.agent/    flow_set: extractor  working_directory: <corpus>
```

Two processes (the proven-clean regime), two mission states, one workspace,
disjoint databank files. **One required setting:**
`OUROBOROS_SCRAPER_OVERLAP_PDFS=0` on the scrape lane — otherwise both lanes
select from the same `_extraction_pending` set and double-OCR the same papers
(there is no claim marker; `extraction_status` is only written after the
fact).

Gap: `ouroboros.py` derives `agent_dir` from `--working-dir`
(`PersistenceManager.AGENT_DIR = ".agent"`, a module constant). A small
`--agent-dir` flag, or making that constant a constructor argument, is the
whole CLI change. The Python API supports it now.

### 7b. What a lane supervisor adds beyond that

Two processes get the throughput. A supervisor is for what two processes
cannot do:

* **Admission across lanes.** `pool_fit_width`'s discipline, one level up:
  ask the *system* what is free rather than hardcoding. A GPU-exclusive class
  (OCR batch, VL figure read) holds a semaphore of 1 against itself but
  admits the LLM class concurrently — which is precisely the 0.339 regime
  measured above, and precisely what the operator should be able to re-tune
  per station rather than per code edit.
* **Claim leases.** `extraction_status: "extracting"` written before dispatch
  with an owner + expiry, so lanes can share a work queue instead of being
  partitioned by env var. This is what makes N lanes possible rather than 2.
* **Lane occupancy telemetry.** `FanoutPerf`'s sidecar shape, per lane, so
  "overlap = 0.0 m" is a dashboard number instead of something found by
  re-deriving unions from a trace nine hours later.
* **Backpressure.** The extractor drains faster than the scraper fills; a
  supervisor can throttle a starved lane instead of spinning it.

### 7c. Where the third model comes in

The curator set is the case that actually needs multi-*model*: it reads
figures through `/v1/vision` on muse while the scraper wants muse for text.
Three options, in order of what the evidence supports:

1. **Serialize curator against scraper on the same endpoint** — they share one
   model, so the batched engine already seats them; no new physics.
2. **Curator on its own lane, paddle-style cross-process** — proven regime,
   costs a second copy of the weights.
3. **Two models co-resident in LLMVP** — P0.b measured this as buying nothing
   (concurrent ≈ sum) and parked it. **Do not un-park without new evidence.**

The finding that matters: *co-residency was never the blocker.* The OCR path
has been cross-process since it was written. What was missing is a second
lane asking the machine for work while the first one held the GPU.

## 8. Open

* Repeat the probe at n≥3 before trusting the 89.7 m projection as a number
  rather than a direction.
* Measure the three-tenant case (scraper text + OCR + VL figure read) — the
  0.339 is a two-tenant figure and there is no reason to assume it composes.
* Decide whether the supervisor is worth building or whether two processes
  plus `--agent-dir` is where this stops for now.
