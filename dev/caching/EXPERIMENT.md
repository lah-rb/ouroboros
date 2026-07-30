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

*(Appended per block as cells complete. Predictions scored against results —
hits AND misses.)*
