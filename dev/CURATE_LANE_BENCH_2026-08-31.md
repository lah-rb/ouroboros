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

---

# 2026-09-02 — qwen3-next-80b-a3 at a 256k seat: the oversize pool

The operator's question: 222 papers are parked `curate_oversize` against the
65k local seat. Does a 256k trained-context model reach them, and is its
judgement close enough to muse's to be trusted on them? Config: the mac's
`qwen3-next-80b-a3` (Qwen3-Next-80B-A3B-Instruct UD-Q4_K_XL, 46 GB), ONE seat
of 262,144 (`nCtxSeq` = pool), 78 GB footprint, ~27 GB headroom.

## Reach

The park records each paper's doc floor (deepest compression rung), so reach
is exact. Usable budget = (seat − 14k turn overhead) / 1.1 park margin.

| seat | usable doc budget | fits at floor | fits raw (no compression) |
|---|---|---|---|
| 65,536 (local muse) | 46,851 | 0 / 219 | 3 |
| 131,072 (mac muse) | 106,429 | 159 | 80 |
| 262,144 (qwen3-next) | 225,585 | **210** | 176 |

The nine over 226k at floor are theses/monographs (226k–708k).

## Verdict agreement (`dev/bench_curate_quality.py`, same design as gpt-oss)

| | accepted | denied | overall | decode faults |
|---|---|---|---|---|
| muse re-run (noise floor) | 8/9 | 10/10 | 18/19 = 95% | 0 |
| gpt-oss-120b-a5 | 7/8 | 8/9 | 15/17 = 88% | 2 |
| **qwen3-next-80b-a3** | 8/9 | **7/10** | 15/19 = 79% | 1 (healed) |

Fisher exact vs muse p = 0.34; vs gpt-oss p = 0.66 — not distinguishable at
this n. The PATTERN is: all three false accepts are muse *scope-rule*
denials that qwen overrode on data quality (dissolved ions in oilfield water
≠ a named material; few-layer WSe₂ ≠ a mineral; XAS ≠ a corpus technique).
It read good spectra and accepted, past the scope list in the same prompt.
Its one denial of an accepted paper (SEM-EDS table, no spectrum) is the
stricter read. Judgement on data: fine. Scope discipline: weak.

## The parked arm — 8/8 served, and the pool is mostly to DENY

Eight parked papers stratified across the floor range, built at the first
ladder rung that fits (as `_build_doc_for` does): all eight returned a
parseable verdict, none truncated, server prompts 71k–248k tokens. The 248k
one exceeded the 225k budget because the char estimator undercounted it, and
the seat absorbed it.

Six of eight DENIED, every denial right on inspection: an OSIRIS-REx mission
overview, a Russian multi-field proceedings volume, a Chinese TB-control
guideline, USGS reference-sample best values (no spectra), a polyurethane
coating thesis, a doped-Si-nanoparticle thesis (scope). Documents are parked
because they are huge, and huge documents are monographs and proceedings.
The two accepts are real: Mastcam multispectral survey of Gale crater (624
spectra) and a 248k-token Raman tissue-diagnostics thesis. Extrapolated, the
pool holds ~50 acceptable papers, not 210.

## Reliability, speed, and the estimator

- One `llama_decode -3` (graph computation failed) at 15.7k tokens, healed by
  a context rebuild; none in the 27 requests after it up to 248k. Headroom
  bottomed at 6.7 GB during a 144k prefill, recovering between turns.
- Wall rate falls 500 tok/s (50k prompts) → 263 tok/s (248k) as the attention
  layers grow; decode 27–29 tok/s single stream. Parked-paper median 411 s;
  the fitting pool ≈ 20–24 h on one stream.
- **The 3.3 chars/token estimator undercounted a table-heavy USGS bulletin
  1.97×** (99k estimated, 195k served). Other docs 0.84–1.13×. Seat budgeting
  for table-heavy docs must use the server's `tokenCount`, not characters.

## Decisions (operator, 2026-09-02)

qwen3-next reviews the rest of the parked pool: close enough in judgement
("gave and gained ground in different areas") and the only engine that fits
the set. Three production changes make that possible without hand-holding:

1. **Park against the LARGEST seat any lane offers** (`_largest_seat_tokens`),
   not the local constant — the bug that hid the pool from the remote lanes.
2. **`OUROBOROS_REMOTE_CURATE_LANES`** sizes the remote lane count to the
   remote engine's seats (qwen's config serves ONE stream).
3. **Provenance names the lane's model** (`_provenance_model`): packs a remote
   lane produced were stamped with the LOCAL server's active config.

And a **front-matter triage** (`dev/triage_parked_frontmatter.py`): title
page + abstract + TOC (~2–12k tokens, ~30 s) → proceed | deny, denying only
the unmistakable non-candidates the bench found the pool is full of. Doubt
means proceed; the triage never accepts. Calibrated on the eight bench papers
first — the two known accepts must come back "proceed".

---

# 2026-09-03 — Staged packing: section-bounded windows recover every paper

## The question

Pack success collapses with document length. Measured over 1,941 accepted
papers (history is muse, not qwen):

| doc tokens | papers | packed |
|---|---|---|
| <10k | 391 | 91% |
| 10-25k | 1,082 | **96%** |
| 25-50k | 356 | 87% |
| 50-100k | 101 | 58% |
| >100k | 11 | 36% |

Two readings fit that curve: model recall decays with context (operator's
hypothesis), or large documents are theses and reports that simply carry less
packable data. The discriminator is the same papers, same model, same gates,
one variable — the window.

## The cut (operator ruling)

**A window ends on a SECTION boundary, never at a length.** Then a fact split
across two windows requires the author to have split it across two sections —
a failure of the paper's organisation, not of our cutter. Whole sections fill
to ~18k tokens (cap 25k); a single section over the cap becomes its own
flagged window and is still never cut. `agent/actions/pack_windows.py`.

## Result — 12/12

Twelve papers that had FAILED whole-document packing, re-packed in windows
through the production prompt, parser, canonicalisation and gates. Each
window grounded against ITSELF (stricter than the production whole-document
check); the merge then faces the production gates against the whole doc.

| doc tok | win | pass | keys | leaves | grounding | conflicts |
|---|---|---|---|---|---|---|
| 51,786 | 4 | 4 | 75 | 418 | 0.995 | 3 |
| 71,777 | 5 | 2 | 52 | 356 | 0.989 | 2 |
| 80,394 | 5 | 4 | 49 | 385 | 0.990 | 1 |
| 83,838 | 6 | 4 | 69 | 471 | 0.998 | 9 |
| 85,808 | 6 | 4 | 114 | 611 | 0.995 | 7 |
| 85,816 (Mastcam Mars) | 5 | 4 | 69 | 501 | **1.000** | 2 |
| 87,863 | 6 | 1 | 23 | 207 | 0.981 | 0 |
| 89,703 | 6 | 2 | 37 | 404 | 0.973 | 2 |
| 98,324 | 6 | 3 | 45 | 545 | 0.996 | 12 |
| 103,503 | 7 | 3 | 136 | 441 | 0.993 | 0 |
| 105,146 | 5 | 2 | 27 | 798 | 0.995 | 1 |
| **386,929** | 12 | 6 | 12 | **1,728** | **1.000** | 0 |

**12/12 packed. 6,865 grounded values, 708 keys, in 5.4 h (median 23
min/paper).** Merged grounding 0.973–1.000 against the whole document, median
0.995, gate needs 0.95. The hypothesis is confirmed: these papers were never
empty, the model could not attend to them whole. The 386,929-token paper is
the clincher — it exceeds the 262k seat itself, so whole-document packing is
not merely unreliable for it, it is impossible on this hardware.

## Window-level behaviour is the real finding

73 windows: **39 passed, 23 fabrication, 9 shape, 2 transport.**

A window failing no longer sinks the paper — it costs one window. Paper 8
passed 1 of 6 and still produced a valid 207-value pack.

**Shape failures are repairable and were 17% of the yield.** Nine windows
failed ONLY on registry shape (a peak list returned as bare numbers where the
registry holds `list[object]`) while carrying 1,454 grounded values at
grounding ≥0.996. The 23 fabrication failures sat at 0.32–0.93. The
populations do not overlap, so `repair_shapes` (d0b77d8) re-wraps a grounded
list into the registry's declared shape — inventing nothing, dropping
nothing.

## The prompt is teaching the model to fabricate

**15 of 42 ungrounded values visible in gate feedback are EXACTLY the
registry exemplar shown for that key in the pack prompt.** The registry block
lists `emission_line_nm ... e.g. [{"wavelength_nm": 311, "sample":
"Cervantes"}]`, and a window packed `emission_line_nm[0].wavelength_nm = 311`
for a paper that never mentions 311 nm. Likewise `raman_spectral_range_cm-1`
at 300–1200 and `ftir_spectral_range_cm-1_max` at 1600, exemplar values
verbatim.

Honest caveat: `xrd_wavelength_angstrom = 1.54` (Cu Kα) and `ftir_scans = 128`
are plausible from world knowledge alone. But 311 nm as a first emission line
is not a natural default.

**Proposed, not yet tested:** strip values from the exemplars so the block
teaches `{"wavelength_nm": <number>, "sample": <string>}` — shape without a
copyable row. Re-pack the fabricating windows with shape-only exemplars; one
variable, and the same windows as control.

## Merge and its open question

Lists concatenate (exact duplicates dropped) — peak tables merge cleanly and
are the corpus's most valuable content. Scalars keep the FIRST value and
record every disagreement: **39 conflicts over 12 papers, median 2.** Real
ones look like two instruments described in different sections
(`ftir_spectral_range_cm-1` 340–2000 vs 379–1400) or a per-section count
meeting a global one (`sample_count` 1 vs 120). First-wins keeps the pack
valid and loses the second reading. Promoting a conflicted scalar to a list
carrying its source window would keep both; that is the next design call.

## Operational consequences

- **Review needs the whole paper; packing does not.** Windows at 15–20k fit
  the LOCAL 65k seat, so staged packing runs on muse locally and takes the
  Mac off the critical path for packing entirely.
- One degeneration abort in 73 windows (`cycle period 6 x 12`, server-caught)
  — qwen3-next is GDN-hybrid, where paragraph orbiting is a family trait.
- Two windows lost to seat contention while the mission's remote lane was
  live; the lane was disabled for the rest of the run and restored after.

## 2026-09-03 — Wired into production as THE pack path

Operator ruling: "there is no reason to maintain the un-windowed strategy
going forward." `_curate_stateless` now calls `_pack_windowed` for every
paper. Per window: production pack prompt + prior gate findings, two attempts,
`canonicalize_pack_keys` → `repair_shapes` → gates against THE WINDOW. Passed
windows merge (lists concatenate, dicts merge, scalars first-wins with every
conflict recorded in `pack_quality.window_conflicts`); the merge then faces
the production gates against the whole document. `pack_quality` carries
`windows`, `windows_passed`, `shape_repairs`, `window_outcomes`.

**Small papers are unchanged by construction, pinned by test:** a document
under the window target is exactly one window, a single window carries no
part-of-N preface, and its gates run once against the whole document — so the
pack prompt and the turn count are byte-for-byte what they were
(`test_a_small_paper_is_one_window_and_the_prompt_is_unchanged`). Window
sizes: `OUROBOROS_PACK_WINDOW_TOKENS` (default 18,000) /
`OUROBOROS_PACK_WINDOW_CAP` (25,000).

Open: scalar conflicts stay first-wins (recorded, not resolved); the
exemplar-value leak is under A/B (`dev/bench_exemplar_leak.py`) — if
confirmed, the registry block shows shapes, not copyable rows.

### First production hours (2026-09-03, v15)

- **Mastcam Mars paper packed** via the remote lane's windowed production
  path: 5/5 windows, 1,075 grounded values, grounding 0.995, provenance
  `qwen3-next-80b-a3`. Three whole-document attempts had failed it.
- Large bin: 5 packed, 0 pack-gate failures. Small bin: 2 packed, 2 genuine
  gate refusals (a unit conversion; a fabricated density), 9 envelope refusals
  for missing DOI/arXiv id — **the identifier rule, not packing, is now the
  dominant loss** (203 of 310 curate-pending papers carry no identifier).
- **Key coinage rose**: windowed packs coin a median 25 new registry keys
  (p90 209) vs 0 for whole-document packs (p90 8; 18 for planetary papers).
  The 209-key pack was 209 bespoke grounded scalars. The Mars pack shows the
  other shape: `spectral_resolution_nm`, `_2`, `_3`, `_4` — numbered variants
  of one quantity across windows/instruments, exactly what promoting
  conflicting scalars to a list would absorb. Fix committed (fb801b5: softened
  preface + `prior_keys` block), held for measurement against the production
  packs of the two high-coinage papers.

## Exemplar leak: a real but small effect — REFUTED as a lever (2026-09-03)

The pack prompt lists each registry key with a real example value
(`emission_line_nm ... e.g. [{"wavelength_nm": 311, "sample": "Cervantes"}]`).
15 of 42 ungrounded values in the staged-pack run were exactly such an
exemplar, so: re-pack all 23 fabricating windows twice, control = the
production block, treatment = the same block with VALUES replaced by type
placeholders (`{"wavelength_nm": <number>}`). Same window, same model,
alternating arm order. `dev/bench_exemplar_leak.py`.

| | control | treatment |
|---|---|---|
| grounding, mean / median | 0.780 / 0.856 | 0.824 / 0.899 |
| windows passed | 4/20 | 5/20 |
| ungrounded values | 214 | 191 |
| ...of those matching an exemplar | 34 (16%) | 19 (10%) |

Paired difference +0.045 grounding, 11 windows better / 8 worse.
**Wilcoxon p = 0.37, sign test p = 0.65 — not distinguishable from noise at
n=20.** The prompt stays as it is.

Two things the run settled anyway:
- **The metric was partly wrong.** Treatment windows still produced values
  "matching an exemplar" they were never shown (19 of them) — registry
  exemplars ARE typical values, so a model completing a schema reaches for
  them either way. The original 15/42 conflated copying with generic
  defaults; the honest reading is that leak explains a *slice* of
  fabrication, not the bulk.
- **The heavy fabricators fabricate regardless.** The laser-ablation paper
  ran 0.19-0.46 grounding under BOTH arms. Windows that fabricate badly do so
  for reasons the prompt's examples do not touch.

Also observed: **6 qwen3-next degeneration aborts** across the day's runs
(`cycle period N x 12`, `long-cycle repetition`), all server-caught. GDN-hybrid
family trait; the retry ladder absorbs them.

## The preface trade: −94% coinage for −32% recall (2026-09-03)

fb801b5 softened the multi-window preface ("Pack ONLY values stated in THIS
part" → "pack this part as you would the whole paper, with the same
selectivity and the registry's vocabulary") and added a `prior_keys` block
handing each later window the keys earlier windows of the same paper used.
Measured by re-running the PRODUCTION `_pack_windowed` on the two
high-coinage papers, their existing packs as controls:

| | before | after |
|---|---|---|
| new registry keys | 270 | **17 (−94%)** |
| grounded values | 409 | 277 (−32%) |
| windows passed | 6/6 | 2/7 |

`doi_10.1029_2006je002728` went from 209 bespoke scalars
(`hematite_concretion_diameter_mm_min`, `omega_vnir_spectral_range_um_min`)
to 24 keys, nearly all registry-standard (`emission_line_nm`,
`ftir_peak_wavenumber_cm-1`, `reflectance_values`), keeping 206 of 220 values.

Production, all multi-window packs (different cohorts — direction only):
coinage median 28 → **0** (max 209 → 19), window pass 88% → 79%.

**KEPT.** The registry had reached 4,722 keys; past some size it stops being
a shared vocabulary, which is the property that makes packs comparable across
papers. The −32% is measured on the two WORST offenders, values missed by
selectivity are recoverable by re-packing, and a polluted registry is
corpus-wide and much harder to undo. Open: whether the recall cost belongs to
the preface wording or the `prior_keys` block — they shipped together and were
measured together.

## Preface A/B: the wording costs recall, the prior_keys block buys it (2026-09-04)

Four arms, same two papers, production `_pack_windowed` with only module
attributes patched (`dev/bench_preface_ab.py`, qwen3-next on the remote
seat). Cells are grounded values / new registry keys / windows passed.
Coinage is measured against TODAY'S registry, which already contains the
209 bespoke keys the Mars paper coined in August — so every arm's coinage
reads far below the August numbers, and only arm-to-arm comparison is valid.

| arm | 2006je002728 (Mars) | 2013je004605 | both papers |
|---|---|---|---|
| A hard/no-prior | 183 / 11 / 1/3 | 106 / 21 / 2/4 | **289** / 32 / 3/7 |
| B soft/no-prior | FAILED / — / 0/3 | 66 / 17 / 2/4 | **66** / 17 / 2/7 |
| C hard/prior | 243 / 8 / 1/3 | 160 / 22 / 2/4 | **403** / 30 / 3/7 |
| D soft/prior | 223 / 5 / 1/3 | 40 / 10 / 1/4 | **263** / 15 / 2/7 |

Two paired comparisons, both directions consistent:

- **Wording (A→B, C→D):** the soft preface loses grounded values both with
  and without prior keys (289→66, 403→263) and passes fewer windows (3/7→2/7
  twice). It does halve coinage (32→17, 30→15).
- **prior_keys block (A→C, B→D):** adds grounded values both times (289→403,
  66→263) at no coinage cost (32→30, 17→15).

So fb801b5's −32% recall belonged to the wording, and its −94% coinage was
mostly the wording too (against the August registry). The block is a pure
win. Arm C — the ORIGINAL preface plus the prior_keys block — is the best
arm on both papers (243 and 160 values, 8 and 22 new keys). n = 2 papers,
one sample each; arm B's Mars failure (0/3 windows at grounding 0.68) is the
one outlier, and a re-run of B alone would say whether it is noise.

**Not applied.** Production stays at arm D pending the operator's ruling;
the candidate change is the one-line preface revert, keeping the block.
