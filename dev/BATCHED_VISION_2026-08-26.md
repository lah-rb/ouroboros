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

## Build log

- (P1 begins after this commit.)
