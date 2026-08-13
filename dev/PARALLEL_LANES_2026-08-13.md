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

## 7d. ADDENDUM — topology is NOT the variable; the workload pair is

*Measured 2026-08-13 after §7c was written. It changes §7c's conclusion.*

§7c ranked the options on the P0.b verdict: in-process co-residency buys no
parallelism, cross-process does, so keep paddle in its own process. **Both
halves of that turned out to be wrong, and the second one is why.**

`llmvp/dev/probe_dual_model_strategies.py` re-ran P0.b on the current build,
then ran the SAME pair, SAME questions, SAME arm protocol across two
processes — the control that isolates topology. One `run_arms()` serves both
paths so the protocol cannot drift between them.

| topology | pair | tokens | serialization | clean |
|---|---|---|---|---|
| in-process | olmo+devstral | 128 | **0.619** | yes |
| in-process | olmo+devstral | 512 | **0.622** | yes |
| in-process | muse+devstral | 128 | **0.603** | yes |
| cross-process | olmo+devstral | 128 | **0.575** | yes |

**Δ(in-process, cross-process) = 0.044**, and the probe biases *against*
cross-process (pipe overhead lands in the concurrent wall, not in ta/tb), so
the true gap is at most that. Two models in one process cost essentially
nothing versus two processes. P0.b's "true parallelism = cross-process" does
not hold on this build.

**So what produced 0.342?** Not process separation — that run was
cross-process too. The other variable: it paired prefill-heavy OCR against
bandwidth-bound text decode, where every ~0.6 measurement pairs two text
decoders. Ordering every pair measured today:

| pair | shape | serialization |
|---|---|---|
| paddle OCR × muse text | compute-bound × bandwidth-bound | **0.342** |
| paddle OCR × muse vision | compute × compute | **0.524** |
| text decode × text decode | bandwidth × bandwidth | **0.575–0.622** |
| muse text × muse vision | one server, `generation_guard` | **0.773** |

Consistent with unified memory bandwidth being the shared bottleneck: two
decoders saturate it and contend, while a compute-bound tenant fills gaps
they leave. That is a HYPOTHESIS fitting the ordering — no bandwidth
counters were read — but the ordering is what a scheduler should act on.

**Revised guidance.** Pair complementary workload SHAPES; do not pay for
process separation expecting throughput. Concretely §7c's ranking inverts:
holding paddle and muse both hot inside LLMVP is free, so the hot-registry
integration the operator wants costs nothing in throughput — and pairing the
OCR lane against the text lane (0.342) beats pairing it against the vision
lane (0.524).

### Two measurement traps found, both of which had corrupted a verdict

1. **KV accumulation across arms.** `generate_stream_sync` runs
   `Llama.generate(reset=False)` by design. A probe reusing one instance
   across every arm accumulates dynamic context: ~7k tokens at
   `max_tokens=128` (under the 8192 ceiling by luck), ~32k at 512 — four
   times over, which sent one run to 4x its predicted wall at 98% GPU.
   It also manufactures "corruption": the same prompt at a different context
   depth sees a different KV state, so greedy picks differently. That
   produced divergences at exactly q4/q5 — late, when the context was
   nearly full — and never at q0–q3. With a per-decode restore the
   divergences vanish entirely and serialization tightens from a 0.20–1.09
   scatter to 0.61–0.62. **P0.b's lone divergence is the same shape, and its
   DIRTY verdict rested on it.**
2. **`% wall saved` is not a transferable figure.** At identical
   serialization (0.619 vs 0.622) it fell 11.6% → 4.1%, purely because
   olmo is a thinking model that spends the whole 512-token budget while
   devstral stops early, pushing the legs from 2.31:1 to 8.41:1. Two legs
   overlap only over the span they share, so the ceiling is `ideal/serial`
   (0.698 → 0.894). **Report serialization; treat leg balance as a separate
   scheduling lever** — and note the scraper's 2.7:1 OCR-to-inference ratio
   is exactly this problem.

### Void arm, recorded so it is not re-run blind

muse × paddle in-process **did not measure anything**. Paddle contributed
1.4% of summed leg time (0.05–0.12 s against muse's 2–6.5 s) and threw
`DegenerateGenerationError: cycle period 2 x 12` — a vision model with no
image has nothing to do. With `serial ≈ ideal` the ratio's denominator
collapses: two questions returned NEGATIVE serialization and one NaN, and
the script still printed a tidy "median 0.067". **That number is division by
approximately zero, not near-free overlap.** Testing the production pair
needs paddle doing real vision work in-process — its mmproj bound through
LLMVP the way muse's is — which is a build, not a config file.

Still untested: the SIZE-ratio variable. muse+paddle is 36:1 where every
clean pair here is 1.65–2.31:1, and there is no sub-1 GB text decoder on
disk to stand in (fleet floor is devstral at 11.1 GB; the smallest GGUF of
any kind is paddle itself).

## 7e. BUILT + MEASURED — in-process residency, and what it actually buys

*2026-08-13, after §7d. Commits 7c59761 (registry), 0218b8e (vision pool),
this one (toolchain).*

Phase 2b shipped on §7d's evidence: N models hot at once, addressable by
name, **without** the decode lock Phase 2 had settled on — that lock existed
to contain a hazard the re-measure showed was a probe artifact.

**The end-to-end A/B, same PDFs, same flags, one paper per batch:**

| | LLMVP resident | subprocess | |
|---|---|---|---|
| paper 1 numeric / span | 0.8401826484018264 / 0.9444444444444444 | **identical** | 9/9 verified |
| paper 2 numeric / span | 0.9359756097560976 / 0.9 | **identical** | 10/10 verified |
| figures kept / dropped | 12/27, 14/14 | **identical** | |
| paper seconds | 60.2 + 77.7 = 137.9 | 55.0 + 76.8 = 131.8 | +4.6% |
| wall seconds | 64.3 + 81.8 = 146.1 | 61.4 + 83.2 = 144.6 | **+1.0%** |

Verification rates match to sixteen decimal places on both papers, on both
paths. Markdown differs by 0.23% of characters — different wording in
places, same numbers and same spans recovered, i.e. sampling.

**THROUGHPUT IS A WASH.** Resident costs ~4.6% of compute and saves the
~6.5 s per-batch spawn, netting ~1% of wall at n=2 — inside the noise. At
production's batch-of-2 the spawn amortises further, so the subprocess edges
ahead on compute-bound batches and residency edges ahead on small ones.

**So residency is a MANAGEMENT win, not a speed win, and the honest framing
matters**: model choice moves out of a constant in a tool and into LLMVP's
config; the OCR stage becomes visible to the fleet's model management and
telemetry; Ouroboros stops owning a server lifecycle; and the stage travels
with the fleet to CUDA instead of being pinned to a locally-spawned binary.
It does NOT make OCR faster, and the earlier expectation that removing ~84
spawns would matter was already measured wrong in §4 (6.5 s each, 2.4%).

The overlap story is UNCHANGED by residency, which is easy to get backwards:
the subprocess path was already cross-process and already overlapped at
0.342. Residency does not unlock the pairing win — §7d showed topology is
not the variable. It just puts the tenant under one roof.

### The vision pool, and the race it closed

The design had to answer why paddle's `--vl-parallel 4` did not obviously
survive the move, since `_create_vision_instance` builds ONE context at
`n_seq_max=1`. Two findings:

* `mtmd_helper_eval_chunk_single`'s **seq_id is a real C parameter** the
  Python wrapper hard-codes to 0 — so the binding is patchable. But patching
  it alone buys nothing: the handler keeps its token ledger (`n_tokens`,
  `input_ids`) on the Llama OBJECT, so N sequences in one context race on
  that ledger whatever seq id the eval is handed. N private contexts keep
  every assumption true by construction, in our own code, with no fork of a
  vendored file that a reinstall would revert.
* **A live race predated all of this.** Every vision request shared one
  instance with no mutual exclusion; the call sat inside `generation_guard`
  under a comment claiming that "serializes with text generation". It does
  not — the guard is a COUNTING guard that holds off drains and scaling and
  deliberately lets generations run together. Two concurrent vision requests
  would have raced. It never fired only because every caller is sequential.
  A checkout queue closes it at width 1 and makes width > 1 parallel.

Measured, 4 figures at temperature 0: serial 8.7 s → concurrent 5.2 s
(**1.66x**, serialization 0.30), character counts IDENTICAL both ways
(856/690/7/281). Short of 4x because the legs are 5:1 unequal — the same
leg-balance ceiling as §7d, not a pool defect.

**Correction to the docstring this feature was designed around:**
`memory_clear(True)` — the "clears EVERY sequence" hazard cited as the reason
vision must be private — fires only on the HYBRID branch. A plain transformer
takes `memory_seq_rm(0, longest_prefix, -1)`, scoped to seq 0. The
private-context conclusion still holds and the hazard is real for
hybrid/recurrent architectures, but as written it overstates the danger for
the models actually served.

### Governor gaps found by building on it

Pricing the pool surfaced that **vision KV was never priced at all**: muse
goes 26.1 → 32.6 GB once its 131k vision context is counted, paddle 1.36 →
3.61 GB with 4 x 32768. The pool is built on first request and never
released, so "not allocated yet" was a timing detail, not a saving.

### Two integration defects, both found live rather than reasoned about

* An unknown `model` name returned a bare **500** — `KeyError` escaped the
  REST handler. Now 400 with the name, which is the one thing a caller needs.
* `AsyncOpenAI` appends `/chat/completions` to its base URL, so a bare host
  posts to `/chat/completions`. llama-server answers there AND under `/v1`,
  which is why the spawned backend works with a root URL; LLMVP mounts its
  shim only under `/v1` and 404s. Cost one live run to find.

## 8. Open

* Repeat the probe at n≥3 before trusting the 89.7 m projection as a number
  rather than a direction.
* Measure the three-tenant case (scraper text + OCR + VL figure read) — the
  0.339 is a two-tenant figure and there is no reason to assume it composes.
* Decide whether the supervisor is worth building or whether two processes
  plus `--agent-dir` is where this stops for now.
