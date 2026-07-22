# LLMVP Serving-Substrate Performance Reference

**Measured 2026-07-20/21** · M1 Ultra 128 GB (`iogpu.wired_limit_mb=116000`) ·
gpt-oss-120b F16 · batched single-context engine (`decode_mode: "batched"`,
`kv_unified`, `swa_full`) · harness `dev/` keeper-adjacent
(`bench_matrix.py`, scratchpad session 1ba33a63; raw jsonl in that session's
`bench_out/`). Purpose: **experiment-design reference** — size worker budgets,
concurrency, and prompt loads so experiments never starve workers or wedge the
engine. Cross-checked against real-workload traces from the 2026-07-20
frozen-design ablation arms.

## 1. Decode throughput vs concurrency (256-tok gens, small prompts)

| streams N | aggregate tok/s | per-stream tok/s (med) | config |
|---|---|---|---|
| 1 | 55–56 | 56 | both |
| 2 | 65–67 | 38–40 | both |
| 3 | 76–79 | 27.5 | gameab (W=3) |
| 4 | 83 | 22.5 | swarm (W=6) |
| 6 | 89 | 17.3 | swarm (W=6) |

- Config-independent (same engine); aggregate saturates toward ~90 tok/s,
  per-stream divides. **Rule: per-stream decode ≈ 56/N + batching bonus.**
- Real-workload anchor: flow calls decode at 51–58 tok/s single-stream;
  the 2026-07-20 swarm fan-outs achieved only **59–63 tok/s effective
  aggregate** (staggered short tasks + prefill interleave waste most of the
  batching gain).
- **n=symbols validation (2026-07-21, arm_swarm2, W=32/131k):** 21 symbols,
  pool-fit gate waved to 14 concurrent (est read 125k > 80% of pool) —
  **21/21 landed in 402 s, 1 retry, 28,485 tok (~71 tok/s effective)**.
  Self-annealing measured: early per-stream ~5–7 tok/s under full
  contention → late/big workers 10–26 tok/s as small symbols EOS'd and
  released (GameEngine ran the tail at 25.8 tok/s and generated 5,237 tok —
  28% over the removed 4,096 cap). Graph: dev/plot_swarm_perf.py on
  arm_swarm2/.agent/swarm_perf.jsonl.

## 2. Prefill throughput vs prompt size (single stream, actual tokens)

| prompt (actual tok) | prefill time | tok/s |
|---|---|---|
| ~14.6k (real trace) | 16.2 s | ~794 |
| ~48k | 81 s | ~590 |
| ~95k (gameab) | 244 s | ~390 |

- Smooth quadratic-ish degradation, no cliff — ~halves per doubling.
- **Concurrent prefill contention is real:** 6 × ~6k prompts arriving together
  prefill in 4–24.5 s each (roughly serialized; ~1k tok/s aggregate).
  Fan-outs with many mid-size prompts are prefill-dominated.

## 3. Context capacity — hard caps and the WEDGE ZONE

| edge | behavior |
|---|---|
| combined request tokens > `n_ctx` | **fail-fast** `ValueError: Combined prompt length (…) exceeds the model's context window` — clean, immediate |
| request fits `n_ctx` but **exceeds FREE pool cells** | **NO admission check — the request blocks indefinitely**, wedging the single shared context: every later request queues → seat-wait timeout after `backend_timeout` (180 s) → `All inference instances are busy (active=1, limit=W)`; clients time out; heals only on proactive context refresh (up to 30 min cadence) or config swap (force-evicts streams) |

Measured instance: a ~95k-token prompt on the swarm config (98,304 pool,
carrying ~residue from earlier phases) hung > 15 min and poisoned three
subsequent 6-stream bursts (all 18 requests died: 12 client timeouts @900 s,
6 busy-bounces @180 s). The *identical* prompt on gameab (131,072 pool, clean)
completed in 244 s. **This wedge is the production failure edge** — same
signature as the 2026-07-16 bossgame KV-exhaustion event (1,002 stream
evictions).

- `n_ctx` is the **shared** cell pool across all seats (`kv_unified`):
  budget the SUM of concurrent (prompt + generation) + resident overhead
  (pinned heads / static prefixes / session residue).
- Token estimation trap: char/4 underestimates number-dense text by ~1.49×
  (measured); code ~1.2–1.3×. Size prompts by *actual* tokens.
- `backend_timeout` (180 s) bounds **seat-queue wait**, not generation — long
  generations are never killed by it; queued requests behind a wedge are.

## 3b. Pool-beyond-trained-context: PROVEN (2026-07-21, ctx probe)

**The trained 131k window binds each SEQUENCE, not the pool.** On a 224k
shared pool (`gpt-oss-120b-a5-ctxprobe.yaml`, W=8; KV pre-allocated → wired
FLAT at ~80.3G regardless of load): needle retrieval + cross-position
arithmetic **17/17 perfect** across 1×50k / 4×25k / 2×50k / 6×25k /
**4×50k = 200k live cells (153% of the trained window)** — zero errors, no
wedge, no RoPE degradation. Pool sizing is a CONFIG-time capacity plan
(memory is paid at allocation), not a per-request risk.

Economics at depth (dev/ctx_decode_probe.py, rerunnable):
- Concurrent big prefills SERIALIZE (~1.2–1.4k tok/s on a clean context —
  faster than §2's residue-laden numbers): 4×50k total prefill ≈ 155 s ≈
  90% of the burst wall (169.5 s).
- Decode during the mixed phase is prefill-starved (1–3.5 tok/s while
  sibling prefills run); solo decode at 50k depth = **31 tok/s** (the depth
  penalty vs 56 shallow, now measured).
- Design rule for divide-and-conquer reads (diagnose swarm): keep the
  fan-out's generations SHORT (findings, ~150 tok — wall stays
  prefill-bound), and run long-form synthesis (merge/conclude) at SHALLOW
  context afterwards where decode is ~55 tok/s.
- **Session-resident interrogation at 200k (the production-cache mirror,
  both probes 2026-07-21):** turn-N prefill = 0.0s across 430+ turns (pay
  each slice once); clean concurrent decode at 4×50k occupancy =
  **12.5–12.8 tok/s/stream** (→26–28 as the fleet drains — pure depth×N
  composition, NO extra pool tax); sustained one-at-a-time interrogation
  runs **37→34 tok/s** with **1.1s median round-trips**. Recall from
  resident KV: **215/215 scored probes over a 12-min, 430-round
  coordinator-interrogates-3-workers session** (an early 2/4 wiggle
  blemish did not reproduce — one-off render artifact). Coordinator
  pattern (integrate→ask→answer) rehearsed live: sessions survived 70–215
  turns each, wired flat 80.4G. Deep tracers can be CONVERSATIONAL.

## 4. Worker-shape feasibility (the swarm fan-out workload)

| quantity | value |
|---|---|
| per-stream decode at N=6 | ~17 tok/s |
| time to emit 4,096 tok at N=6 | ~4 min |
| time to emit ~6.5k tok (big class + medium CoT) at N=6 | ~6.5 min |
| 6 × (6k prompt + 8k gen) peak pool draw | ~84k of 98,304 — tight but feasible |

`max_tokens` includes harmony CoT (`thinking_mode: medium` burns ~0.5–1.5k
before content — the model does not budget; it truncates).

## 5. Design rules for experiments

1. **Never set a generation budget below the largest expected artifact +
   2k CoT headroom.** The 2026-07-20 swarm/integrated runs starved workers at
   `worker_max_tokens=4096` while the largest symbol (GameEngine class) was
   ~4.9k tok of code alone → deterministic failure both runs; 2nd-largest
   (~2.8k) coin-flipped. 22/24 symbols ≤ 700 tok passed. Fan-out granularity
   of "one top-level symbol" makes a class the budget-critical unit.
2. **Budget the shared pool as a sum**, with actual-token sizing and resident
   overhead margin; keep peak concurrent draw ≤ ~80% of `n_ctx`.
3. **Do not admit any single request whose prompt approaches free-pool size**
   — it wedges, it doesn't fail. Until the engine gains an admission check,
   cap experiment prompts well below (n_ctx − expected residue).
4. **Schedule big generations at low concurrency.** N=6 divides decode to
   17 tok/s; a 6-min worker attempt is fine alone but multiplies across
   retries. Sort symbols by size; run the biggest first/least-concurrent.
5. **Prefill dominates mid-size fan-outs** — 21 × ~4k prompts costs more wall
   in prefill than decode. Shared-prefix prompt design (cache hits) matters
   more than worker count.
6. After any capacity-edge event, **bounce or swap the config** — stuck
   streams survive until a refresh; a swap force-evicts and heals in ~11 s
   (~42 s with drain).

## 6. What this exonerates / convicts in the 2026-07-20 swarm experiments

- **Convicted:** `worker_max_tokens=4096` (starved the two biggest symbols —
  rule 1); the integrated arm's reconcile-before-serial-fallback ordering
  (stub-masked the starved file, then the repair loop churned 86 diagnose
  calls on it); a contract-authored suicidal doctest (`>>> raise
  NotImplementedError`) that zeroed the doctest gate (0/7).
- **Exonerated:** the serving substrate at the actually-used loads (6×~1k
  gens, ~48k peak pool: all clean); `backend_timeout` (never fired in the
  arms); model capability (serial fallback wrote the 538-LOC engine fine).
- **Meaning:** the swarm-vs-integrated-vs-control quality/cost comparison was
  contaminated by starvation. Re-run with budgets per rule 1 before drawing
  paradigm conclusions.
- **Re-run verdict (2026-07-21, arm_swarm2, uncapped + widened config):**
  starvation eliminated — 21/21 symbols, 7/7 files, typecheck clean, no
  serial fallback. The artifact still does not boot: residual defects are
  pure cross-file SEMANTIC seam drift (loader reads `data/*.yaml`, files
  written top-level; `GameState.__init__` requires `flags`/`player` keys
  `load_world` never produces). The substrate and worker budgets are now
  exonerated end-to-end; what remains is the coordination-paradigm problem.
- **Data-boundary stack verdict (2026-07-21, arm_swarm3):** with the locus
  rule injected + contract/write-time gates live, the path seam is GONE
  (contracts parsed clean ×3, zero `data/` references in the artifact) and
  21/21 symbols landed again (3-for-3 since the uncap; gate waved 15-wide).
  The crash moved one level finer: transfer VALUE shape (engine iterates
  `world_data["rooms"]` as list; loader built a mapping). Seam progression
  across runs — file-not-found → missing top-level keys → value shape —
  each layer of upfront design removes a coarser seam class. NOTE: the
  frozen seed predates the design-prompt transfer-shape requirement, so
  that change is untested until a FRESH design_and_plan run.
- **FAIR ABLATION (2026-07-21, frozab2 — fresh design under the new prompt,
  control vs swarm from the same seed, blind Opus judge PINNED):** the
  design authored transfer contracts unprompted (load_world→GameEngine,
  parse_command→handle_input) and the swarm artifact BOOTED AND PLAYED for
  the first time — every contracted seam held. Verdict: **control 18/25 >
  swarm 9/25**, control 3.5× cheaper (13.1k vs 45.5k OUT), 4.8× faster
  (3m56s vs 18m44s). Swarm's residual defects are seams the design did NOT
  contract: CombatEngine.run returns {outcome,message} but the engine reads
  monster_defeated/damage_taken (an unauthored transfer dict), item type
  vocabulary 'potion' vs 'healing', raise-vs-catch error conventions.
  KEY FINDING: the boundary stack lifted the BATCH baseline too (old
  control 13 → new 18, zero crashes, working save/load) — upfront design
  hardening is paradigm-neutral; the fan-out's coordination penalty is
  paradigm-specific and reappears at whatever level the contracts don't
  reach (the specification-gap "irreducible penalty," now replicated
  in-house across 4 hardening generations). Single-context-scale verdict:
  batch wins every axis. Fan-out's remaining case = artifacts too large
  for one completion (untested regime).
- **Transfer-shape gate SHIPPED (2026-07-21):** deterministic
  producer→consumer dict-key agreement (`_transfer_shape_violations`) in the
  shared `run_batch_file_checks` — retroactively flags BOTH fair-ablation
  swarm defects with exact naming AND 4 latent seams in arm_swarm2's parser
  handoff, zero false positives across 7 historical artifacts. Cross-module
  typecheck (method/arity) generalized to the batch gate tail too — the
  seam class is paradigm-neutral (old-control's Player(**dict) crash).
