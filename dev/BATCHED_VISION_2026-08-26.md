# Batched vision — vision decode inside the multi-seq text engine

**Trigger:** the figtext campaign (~30k figures) is bound by single-stream
vision decode (~20 tok/s, ~168 figs/h); a second dedicated vision context
does not fit 24 GB (three ggml aborts, 2026-08-26), and the GPU split slows
single-stream vision 35%. Operator ruling: build option 1 (engine-resident
vision streams) — "strictly the better future-aware move."
**Plan:** ~/.claude/plans/gentle-herding-kitten.md (approved 2026-08-26).

## Pre-registered predictions (write verdicts next to these, don't edit)

P0-P1. seq A's temp-0 continuation is byte-identical before/after and
       during a full multimodal install on seq B.
       → **VERDICT 2026-08-26: PASS** (arm2 and interleaved arm3 both
       byte-identical, probe_vision_kv_integrity.py).
P0-P2. muse shows no M-RoPE position/cell divergence
       (new_n_past == old + chunk tokens).
       → **VERDICT: PASS** (12 + 2976 → 2988). R3 cell-debt machinery
       collapses to nothing for muse. Also measured:
       `mtmd_decode_use_non_causal = False`, `use_mrope = False` — the
       image feed may be CHUNKED into ≤n_batch embd sub-batches (~0.4 s
       control ops) instead of one atomic 2.5 s op; keep the atomic
       helper branch for models where either flag is true.
P0-P3. the atomic image-chunk install stalls < 1.5 s.
       → **VERDICT: MISS at 2.53 s** (2,976 image tokens @ n_batch 512;
       encode 2.43 s separate). Chunked-embd feed (legal per P0-P2)
       brings the per-op stall to ~0.4 s — adopted into P1 design.
P0-P4. concurrent mtmd encode beside live 2-seq decode: no corruption,
       < 10% decode tax.
       → **VERDICT: SPLIT.** No corruption (tokens byte-identical — the
       safety half PASSES). Tax FAILS as measured: ~79-90% while an
       encode is in flight, device-agnostic (also with the projector on
       CUDA1, and untouched by mtmd n_threads) — suspicion: per-encode
       cudaMalloc/free storms driver-syncing the whole process.
       Realistic duty (one ~2.4 s encode per figure per stream, not
       back-to-back) nets ≈ 0.66-0.83× during-encode windows → expected
       aggregate at 3 streams ~1.7-2.3× instead of the clean 2.34×.
       Revised P4 target band: **250-380 figs/h** (was 300-450).
       Mitigation to try in P1: mtmd_batch_* batch-encode; encode on
       CUDA1 vs CUDA0 A/B under real duty.

P4-level predictions (soak, unchanged otherwise from the plan):
P4-P1. in-situ encode costs < 10% of the TEXT lanes' tps at real duty.
P4-P2. 3 vision streams ≥ 2.2× single-stream figs/h; 4 streams ≥ 2.6×.
P4-P3. vision+vision serialization S ≤ 0.40 (recreate
       dev/batched_modality_mix.py — referenced in configs, absent).
P4-P4. VRAM delta ≤ ~1.3 GB (mtmd ctx + embd scratch); zero ggml aborts
       / latch recoveries over ≥ 6 h at 3 streams.

## Baseline (captured before any product code)

- Unsplit campaign, 2 fig lanes on 1 vision context: **~168 figs/h**
  (160 /v1/vision calls in 57 min, 2026-08-26 12:18-13:15 log), vision
  decode 20.3 tok/s single-stream (400-tok canary), muse whole on the
  3090, projector CUDA1, vision 8192×1.

## P0 probes (llmvp/, probe convention)

- `probe_vision_kv_integrity.py` — the go/no-go. GO on 2026-08-26.
- `probe_vision_concurrent_encode.py` — adversarial continuous-encode
  arm; safety PASS / tax MISS as recorded above. Projector-device and
  mtmd-thread knobs included.
- Outputs in llmvp/probe_out/*.jsonl.

## P3 quality gate — CLOSED (2026-08-26 ~17:00)

Blind pairwise per the VL-bake-off method (10 corpus figures, both
paths at temp 0 / 700 tok, hash-derived A/B shuffle per figure, one
blind judging agent scoring fidelity + fabrications, keymap applied in
exactly one place):

  preferred: batched 4, pool 2, tie 4
  fabrications: batched 3, pool 4  (both shared 2: a CAS number and an
  NH3->NH4 misread — model-level, path-independent)
  empty/truncated-at-zero outputs: none on either side

The judge twice flagged the POOL side for trailing meta/OCR leakage —
the forced content channel makes the batched output cleaner. Latency on
the same 10 pairs: 212s vs 332s total (batched 1.56x) under concurrent
campaign load. GATE: within-one-letter equivalence exceeded (batched is
slightly PREFERRED); fabrications not increased. Batched path stands.

## P4 soak — CLOSED (2026-08-26 21:40, 354 min ≥ the 6 h window)

Verdicts against the pre-registered predictions:

P4-P1 (encode tax < 10% of text lanes): PASS by observation — text
      turns ran normally throughout (catalog/biblio streams at ~33
      tok/s in the log); the adversarial continuous-encode tax never
      materialises at real one-encode-per-figure duty.
P4-P2 (3 streams ≥ 2.2x): EXCEEDED AT TWO — 420 figs/h campaign-pure
      over 354 min (2503 serves) = 2.47x the 168/h baseline, ABOVE the
      revised 250-380 band, with max_streams still 2.
P4-P3 (serialization S ≤ 0.40): not separately measured — superseded by
      the direct end-to-end rate; the modality-mix bench recreation
      stays queued as optional follow-up.
P4-P4 (VRAM + faults): PASS — 3090 flat at ~23.2-23.6/24.6 GiB, 3060
      holds the projector alone (5.4 GiB); ZERO ggml/CUDA faults, zero
      latch recoveries, zero seq wedges across the window.

Residual: 2 of ~2,400 installs (19:15:37/:39) hit a seq holding 2,784
stale KV positions against an empty slot — llama_decode refused, both
requests pool-fallback self-healed, no recurrence. Root cause open;
the CLASS is closed by the install preflight guard (dc7f18f: verify
memory_seq_pos_max agrees with the slot before row one, scrub loudly),
live from the P5 ramp bounce.

## P5 ramp

- Step 1 (21:40): max_streams 2 -> 3, server bounce (activates the
  preflight guard). Gate to hold: figs/h not below 415, zero faults
  over ≥ 1 h.


Flag flipped ~15:35; the campaign's own fig lanes are the soak load.
First 21 min: ~217 figs/h campaign-pure (~274 mixed with the A/B), zero
fallbacks, zero install failures, at max_streams 2. Baseline was 168.

## Build log

- P1 (8fbffef): engine install helpers (eval_tokens_on_slot /
  eval_embd_on_slot, memmove embd feed, MEDIA_SENTINEL, has_media +
  snapshot refusal), inference/vision_batched.py (MtmdEncoder,
  split_prompt, family-renderer prompt, async install orchestrator with
  the position law enforced), config flags + empty vision persona.
- P2 (d7439a4): run_vision_completion routing switch; fallback-to-pool
  is the contract (any batched failure = pool answer, never a new error
  shape); semaphore cap; counters in backend health dict (NOT surfaced
  over GraphQL — the typed HealthStatus is flat; soak instrument is the
  server log's fallback warnings + databank figs/h).
- P3a (bb0617d): first live request leaked ` to=self` — the bare
  generation head lets a channel family choose; the head now forces the
  content channel (schema-reconstructed). Live A/B canary after fix:
  clean description, 21s/300tok batched vs 34s pool (single request
  1.6-1.9x). Flag ON at max_streams 2 — THE CAMPAIGN'S FIG LANES ROUTE
  BATCHED FROM THIS BOOT (P4 soak start ~15:35). First 9-pair A/B:
  batched faster in 9/9 (e.g. 17.0s vs 41.2s at equal budgets).
