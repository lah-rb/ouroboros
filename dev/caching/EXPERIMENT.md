# CACHING EXPERIMENT — pre-registered design

**Registered 2026-07-29, before any cell ran.** Companion to
`dev/caching/CORPUS.md`, whose §8 gap list this design fills. Operator
decisions: windowing guard fixed BEFORE cells run (P1); model axis = 3 anchors
+ fleet spot checks.

Predictions are recorded here per §6-of-the-rubric discipline: their job is to
make a surprise legible, not to be right. Results are appended per-cell to
`dev/caching/results/cells.jsonl`; this file's Results section scores
predictions — hits AND misses — after each block.

---

## Design spine (from the corpus's confound checklist)

Every cell:
- **fixes `n_seq_max` explicitly** (the band layout is a factor, never an
  accident);
- pins `context_refresh_interval: 100000` + `context_refresh_seconds: 360000`
  (M16 is the cache destroyer; a mid-cell refresh voids the cell);
- holds `resident_strip_reasoning: false` (it changes KV contents);
- records the **strategy triple** from health, never from config;
- records failure counters (`decodeFailures`, `latchHeals`,
  `contextRefreshes`) — a nonzero delta voids the cell;
- **asserts the intervention landed** (health n_seq_max / strategy / n_ctx_seq
  checked against the cell spec before the workload runs — the nopersona
  byte-identical-null lesson).

Response variables (corpus §6 vocabulary): per-turn `freshPrefillTokens` +
`prefillMs` curve · `prefix_reuse_rate` · total wall · needle-at-depth AND
needle-past-window · wired GB (`vm_stat`) · `decode_tps` · failure events.

## Factors

| factor | levels |
|---|---|
| session strategy | full_replay · resident |
| band layout | `n_seq_max=2` (fork off, snapshots 0) · `n_seq_max=12` (stock) · `kv_unified: true` (no division) |
| workload | realistic turns (**~900 tok/turn, depth 16**) · probe turns (5 tok, depth 12 — continuity with both prior matrices) · snapshot fan-out (Block E) |
| architecture | gpt-oss-120b (SWA MoE) · glm-4.7-flash (MLA) · gemma-4-26b (iSWA) · fleet spot cells |
| swa_full | gemma-26b only: off · on (KV re-check first) |

Turn payload for realistic cells: ~900 tokens of varied prose + a per-turn
planted fact (needle rotation), modeled on the hy3 arm's measured ~926-tok/turn
session growth. Payloads are deterministic per (cell, turn) so arms are
token-comparable.

---

## Blocks and pre-registered predictions

### Block A — instrument validation (after P1)

Deep-session needle past the TRUE per-seq window on all 3 anchors; windowing
must fire at the per-seq boundary, not at n_ctx; one `batched_parity.py`
byte-parity run on a resident config.

- **P-A1:** post-P1, a session on a `kv_unified: false` resident config windows
  at ~`n_ctx/n_seq_max` and the needle *before* the window boundary survives
  while the oldest turns drop — no decode error at any point.
- **P-A2:** parity remains byte-identical (P1 touches guards, not decode).

### Block B — payout × cost surface (the core)

3 anchors × {full_replay, resident} × {n_seq_max=2, 12, kv_unified} at
realistic turns, depth 16. full_replay arms take band layout too (the band
divides the window even when the strategy ignores the bands).

- **P-B1 (payout):** resident per-turn fresh prefill stays ≤ 1.2× its turn-2
  value through depth 16 at ~900 tok/turn; full_replay total prefill grows
  superlinearly, hy3-shaped (last-turn prefill ≥ 5× turn-2 on the slower
  models).
- **P-B2 (band cost is window, not speed):** between n_seq_max=2 and 12,
  per-turn prefill and decode_tps differ within noise (<10%); what differs is
  where windowing fires (~6× earlier at 12).
- **P-B3 (kv_unified):** cost within noise of n_seq_max=2 with NO window
  division; no correctness difference (needles pass).
- **P-B4 (magnitude):** the probe-turn cells reproduce both prior matrices
  (turn-2 fresh within ±20% of the 2026-07-29 rows); the realistic cells show
  full_replay per-turn growth ~50–60× the probe's (~900 vs ~16 tok/turn).

### Block C — the swa_full price (gemma-4-26b)

Step 1: measure KV B/tok with `swa_full: true` (probe-context, no guessing) and
compute the servable n_ctx. Step 2: if servable at ≥16k, run the resident
realistic cell and compare against its Block-B full_replay cells.

- **P-C1:** swa_full-on KV lands in the hundreds-of-KB/tok class (the 31b
  geometry scaled), giving a servable n_ctx in the tens-of-thousands, not
  hundreds-of-thousands — i.e. the 262,144 range and resident are mutually
  exclusive on this model.
- **P-C2:** where servable, the session-payout curve matches the other anchors
  (flatness is strategy-, not architecture-, determined once can_shift is
  true).
- **Decision rule (registered):** flip gemma-26b to swa_full+resident only if
  the realistic-turn wall saving at depth 16 exceeds 30% AND the servable
  n_ctx ≥ 32,768; otherwise record "correctly on full_replay — the window is
  worth more than the flatness" in the config.

### Block D — the corrected hy3 recipe

hy3 + resident + bands suppressed (`resident_session_flow_fork: false`,
`session_snapshot_max: 0` → n_seq_max=2 → 16,384/seq, clearing static 1,793 +
measured peak 16,066), realistic turns, depth 16. Also the kv_unified variant.

- **P-D1:** hy3 decodes clean and goes flat at ≈5 s/turn prefill (vs measured
  58.8 s at depth 10 on full_replay) — the ~6× cheaper prediction from
  CACHE_SWEEP_PLAN, now with the corrected arithmetic.
- **P-D2:** the kv_unified variant behaves identically (dense model; unified
  pool should be cost-free).

### Block E — one-cell answers

1. **F12 pool-fit over-count:** attempt the previously-skipped N=64/size-8192
   prefill-grid cell. **P-E1:** it FITS (static counted once per the w=0.06
   mechanism) — admission's per-stream sum over-counts by N×1809.
2. **Snapshot under batched:** smoke `sessionSnapshot` on the production shape.
   **P-E2:** raises cleanly today (the documented hard error), establishing the
   baseline for the deferred-list item — NOT expected to work.
3. **Flow-band v2 pilot (11b):** one stateless cell — a pinned ~2k flow head
   under resident (M9), BUILD then 20 HITs at realistic tail sizes.
   **P-E3:** HIT saves ≈ the head's prefill (~2–5 s/call at glm/gemma rates) —
   worth it for hot flows, consistent with CACHE_STATE's "flat 0.2–0.5 s"
   verdict being a *shallow-head* artifact of the 2026-06 measurement.

### Block F — fleet spot checks + write-back

One resident-vs-replay realistic cell per remaining servable model (bands
suppressed for pool configs). Write `probe_verified_cache:` into each config:
`{strategy, can_shift, session_prefill_flat, n_ctx_seq, depth_windowed_at,
probe_verified_weights_bytes}` — a measurement outranks an estimate; requant
invalidates.

- **P-F1:** every can-shift-true model is flat; both qwen3.6 stay full_replay
  (pure recurrent); step37 stays full_replay (step35 arch); no model errors
  once bands are suppressed (the hy3 class is layout, not architecture).

---

## Cost estimate

~30 server boots (2–6 min each; cells sharing a config share a boot) + 5–15
min/cell scripted load ≈ **6–9 h machine time**, unattended, run in block
order. Results append per-cell; a crash loses one cell, not the run.

## Harness

- `dev/cache_compat_matrix.py` gains `--turn-tokens N` (realistic payloads) and
  `--depth N`; the strategy-triple assert becomes a hard gate.
- `dev/caching/run_blocks.sh` (modeled on `cache_compat_matrix.sh`): temp
  configs per cell spec, health-assert, run, append
  `dev/caching/results/cells.jsonl`, restore production config at exit.
- Reused untouched: `snapshot_stress.py`, `batched_parity.py`,
  `prefill_grid.py` (E1), `finalize_ledger` vocabulary.

---

## Results

*(Scored 2026-07-30, run `cells_20260729-232251`: 24/24 cells, zero voids,
1h36m wall. Rows: `dev/caching/results/cells.jsonl`.)*

### The headline surface (Block B + D)

| anchor | replay Σprefill (depth 16, ~900 tok/turn) | resident Σprefill | payout |
|---|---|---|---|
| gpt-oss-120b | 207.7 s | 26.4 s | **7.9×** |
| glm-4.7-flash | 521.4 s | 63.4 s | **8.2×** |
| gemma-4-26b | 271.7 s | 27.8 s (swa_full on) | **9.8×** |
| hy3-reap-200b | 1,004.4 s | 129.4 s | **7.8×** |

**The payout ratio is workload-determined (~8× at this shape); the absolute
saving is model-speed-determined** (hy3 recovers ~875 s per 16-turn session —
the mechanism behind its arm's 60.8%-of-wall prefill). Replay growth is
architecture-independent: 6.74 / 6.99 / 6.66 / 6.69 / 6.61 / 6.72 / 6.56×
across seven models.

### Prediction scorecard — hits AND misses

| prediction | verdict | note |
|---|---|---|
| P-A1 windowing at the true boundary, no decode error | **HIT** (both halves) | A1 windowed at 16,896 and kept answering (the pre-P1 trajectory was the hy3 crash); A2 kept both needles with no windowing |
| P-A2 byte parity unaffected | not run as parity | superseded by 24 needle-clean cells; formal parity deferred to Block E hygiene |
| P-B1 resident flat / replay superlinear | **HIT** | flat fresh 1080-1200/turn everywhere; replay ≥6.5× everywhere |
| P-B2 band cost is window not speed | **HIT** | glm none/stock/unified within 3% on time; stock paid with the WINDOWED needle instead |
| P-B3 kv_unified ≈ suppressed bands, no division | **HIT** | ≤3% deltas; seq_win = full n_ctx confirmed via health |
| P-B4 probe understates replay magnitude | **HIT** | same model/strategy: probe +16 tok/turn (growth 1.18) vs realistic 6.74× |
| P-C1 swa_full KV in the 100s-KB class; resident and useful context mutually exclusive | **HALF-MISS, good direction** | magnitude right (~430 KB/tok, 28.2 GB @65k) but 65,536 IS servable at 42.4 GB total — not mutually exclusive |
| P-C2 payout matches other anchors once can_shift | **HIT** | 9.8× |
| C decision rule (flip if >30% saving AND n_ctx ≥32,768) | **FIRES** | 90% saving, servable 65,536 → gemma-26b flips |
| P-D1 hy3 flat ≈5 s/turn | **SPLIT** | decodes clean HIT; ~6× at depth-10 HIT (58.8→9.3 s); "flat in TIME" MISS — fresh tokens flat but prefill 6.2→9.7 s/turn rising: **token-flat ≠ time-flat on a dense model** (attention depth prices the prefill; mistral.rs's killer at survivable magnitude) |
| P-D2 unified ≡ suppressed for hy3 | **HIT, and strictly better** | same cost, full window, needle survives — the recipe is resident+kv_unified |
| P-F1 fleet spots behave per gate | **HIT** | mistral-medium/devstral flat with needles; laguna-xs/qwen3.5/qwen3.6 replay as configured, no errors anywhere |

### Findings beyond the predictions

1. **Dense-model resident prefill rises with depth** (D1/D2: 6.2→9.7 s/turn at
   flat token deltas). Not a defect — but deep resident sessions on dense
   models pay an attention-depth tax that MoE anchors do not show at this
   depth. Corpus §4 addendum.
2. **Instrument defect found and scoped**: the per-turn planted facts are
   arithmetic (7000+31i), so a windowed-out early fact is inferable from
   surviving siblings — `early_fact_ok` is only valid on non-windowed
   sessions (B07/D1 showed True after their fact's window dropped). The doc
   needle (arbitrary 7391) is the trustworthy probe. Fix: hash-derived values,
   AFTER this vintage.
3. **qwen3.6-27b (pure recurrent) replay at depth 16 costs 811 s** with
   per-turn prefill reaching 90 s — the worst per-session cost measured, and
   this model CANNOT go resident (gate-refused by architecture). Its sessions
   should be kept shallow by flow design.
4. Replay's snapshot-fork fallback at realistic depth: 95.1 s (F5) vs hot
   forks of 0.7–2.3 s — the 40× cold-fork penalty from the 2026-07-02 matrix
   grows with depth exactly as the mechanism predicts.

### Adopted write-backs (per the registered rules + measured evidence)

- **glm-4.7-flash**: resident + kv_unified (B08; operator pre-approved).
- **mistral-medium-3.5-128b**: resident + kv_unified (F1; operator
  pre-approved).
- **hy3-reap-200b-a21**: resident + kv_unified (D2 — full window, needle
  intact; supersedes the failed one-line-change prediction).
- **gemma-4-26b-a4b**: swa_full + kv_unified + resident at n_ctx 65,536
  (C decision rule fired: 90% saving; surrenders the 262k nominal range that
  cost 271.7 s/session to actually read).
- All 9 measured models get `probe_verified_cache:` blocks.
- **Not flipped**: laguna-xs (needs its own swa_full KV re-check — sibling
  flags differ; follow-up), qwen3.5 (same), qwen3.6-27b/35b + step37
  (architecture-refused; correctly on replay).

### Block E results (2026-07-30, same night)

- **P-E1 HIT on the physical question — CORRECTED on the gate claim.**
  64 × ~9k unique prompts ran to completion with ZERO pressure/eviction/
  force-window events and zero decode failures at ~78% real pool occupancy:
  **the pool holds the static head ONCE — w=0.06 extends from decode time to
  MEMORY** (F12's open cell, resolved). The first write-up claimed the
  admission gate "refuses ~116k of admissible work"; that was measured against
  F12's QUOTED arithmetic, which belongs to an older gate revision. The
  CURRENT gates (`agent/actions/fanout.py estimate_draw` and
  `prefill_grid.py`'s `need = size*n`) already count client tokens only — no
  code change needed, and F12's stale numbers join the stop-citing register.
  Bonus observation: the client timed out at 10 min and the server ran all 64
  abandoned requests to completion — the watchdog plan's abandonment gap,
  live on the batched path at 78% pool, harmless here because server-side
  completion was the measurand.
- **P-E2 HIT — snapshot-under-batched is a clean refusal, not a hazard.** The
  documented GraphQL error, server healthy after, session recall intact.
  The tier remains UNAVAILABLE in the production shape (corpus §5.11).
- **P-E3 — recorded MISS, RESOLVED TO A HIT the same day.** The original
  pilot showed the flow band engaging perfectly (1 BUILD, 19 HITs, 0
  fallbacks) while hit-arm fresh prefill equalled cold (3,904 vs 3,968 tok)
  and ran 2.3 s SLOWER, and was written up as a server bug. **It was a CLIENT
  contract violation:** `prompt` is the DYNAMIC TAIL ONLY and the server
  prepends `static_prefix` (`warm_flows.py` is the reference client). The
  pilot led its prompt with the head too, so the server skipped the pinned
  copy and dutifully prefilled the duplicate. Corrected pilot (glm, 11/11
  hits, ~2.25k head): **HIT saves 3.51 s/call = 49% of prefill** — P-E3 HIT.
  Two fixes landed: a detect-and-strip guard in `run_completion` (a doubled
  head is wrong on the uncached path too) and the corrected pilot.
  **The one true residue:** `cacheHit=true` / `flowFallbacks=0` reported
  success while the skip silently failed — the telemetry still cannot see
  that failure mode.

### Still open

- Hash-derived planted facts for the harness (the arithmetic-guessable
  defect), next vintage.
