# FEATURE × STRATEGY — what each model can actually run, and what it buys

**Assembled 2026-07-30**, from code at `f68dfd0` rather than from documentation —
several doc claims were stale the day this was written, and the corrections are
in §6. Companion to `CORPUS.md` (mechanisms M1–M20, composability, the
measurement ledger) and `EXPERIMENT.md` (the pre-registered run).

`CORPUS.md` §3 answers "can mechanism A coexist with mechanism B". This document
answers the operational question: **given a model, which features can it have,
what does each one buy, and what does it cost.**

---

## 1. There are only three strategies

Not a combinatorial space. `decode_mode: batched` **hard-requires** the resident
seq cache (`core/config.py:603-614` at load, `llama_cpp_backend.py:2806-2812` at
init), and `session_manager.py:513` therefore always takes the resident branch
under batched — so full_replay is structurally unreachable there. The legacy
save_state splice was deleted 2026-07-30. That leaves:

| | strategy | selected by | fleet |
|---|---|---|---|
| **S1** | pool + full_replay | `resident_seq_cache: false`, or requested and **refused** by `memory_can_shift()` | 4 models |
| **S2** | pool + resident | `resident_seq_cache: true` + can_shift | 11 models |
| **S3** | batched + resident | `decode_mode: batched` (+ resident, swa_full, kv_unified) | 4 configs (production) |

**S1 is not a choice — it is a verdict.** `resident_seq_cache` is a *request*;
`memory_can_shift()` is asked at every load and decides
(`llama_cpp_backend.py:2739-2757`). Read the answer from
`health.sessionStrategy` / `sessionCanShift` / `residentRequested`, never from
the config.

`kv_unified` is a modifier, not a strategy: under `false` the per-seq window is
`n_ctx / n_seq_max`; under `true` there is no division. It is mandatory under S3.

**Naming a strategy (2026-07-30).** A top-level `cache_strategy: replay |
resident | batched` expands to the flags it stands for, so onboarding a model
is one decision instead of four across two sections. It deliberately does NOT
set `swa_full` — that is architecture-determined, and a strategy-level value
would be wrong for half the fleet (glm/MLA and hy3/dense run resident with it
FALSE and are correct; gemma and gpt-oss require it TRUE). Stating a flag that
contradicts the strategy raises at load. `core/config.py:CACHE_STRATEGIES`.

---

## 2. The matrix

✅ works · ⚠️ works with a caveat · ❌ refused loudly · 🔇 **silently no-ops**

| feature | S1 pool+replay | S2 pool+resident | S3 batched+resident | knob (default) |
|---|---|---|---|---|
| Memoryful session | ⚠️ O(n²) re-prefill | ✅ flat | ✅ flat | `resident_seq_cache` (false) |
| Snapshot capture | ⚠️ replay-mode only | ✅ | ✅ *(new 2026-07-30)* | `session_snapshot_max` (2) |
| Snapshot HOT fork | ❌ | ✅ | ✅ *(new)* | ″ |
| Snapshot COLD rebuild | ✅ re-prefill | ✅ re-prefill | ❌ **raises** | ″ |
| Stateless flow cache | 🔇 fallback counter | ✅ | ✅ *(new)* | `flow_kv_cache` (false) |
| Session flow-fork (turn 0) | ❌ | ✅ | 🔇 **force-disabled** | `resident_session_flow_fork` (**true**) |
| Reasoning head-swap (all 3 forms) | ❌ inert | ✅ | ✅ | `reasoning_head_swap` (false) |
| CoT strip (`resident_strip_reasoning`) | ❌ skipped by design | ✅ | 🔇 **pool-only** | `resident_strip_reasoning` (false) |
| Degenerate-turn purge | ✅ n/a (never entered history) | ✅ | ✅ | always armed |
| Deep-session windowing | ❌ raises instead | ✅ | ✅ | knobless |
| Persona heads (`personas`) | ✅ | ✅ | ✅ any seat, any persona | `personas` ({}) |
| Slot personas (`slot_personas`) | ✅ | ✅ | 🔇 ignored (warns at load) | `slot_personas` (null) |
| Speculative decoding | ✅ (but measured harmful) | ✅ | ❌ refused at load | `speculative` (false) |
| Sampling overrides / degen retry | ✅ | ✅ | 🔇 **overrides dropped** | `degen_retry_enabled` (null=off) |
| Context refresh / latch heal | ✅ | ⚠️ wipes resident KV | ⚠️ **guillotines snapshots** | `context_refresh_*` |

**The 🔇 column is the important one.** Five features are on by config and off in
reality, four of them under S3 — which is the production shape. Three announce it
only at `log.debug`/`log.info` (`llama_cpp_backend.py:1077`, `:4372`, `:4665`), so
at the default log level a config can claim a feature it never receives. That is
the same failure class as the transient-files declaration drift: the record says
one thing, the run does another, and nothing reconciles them.

---

## 3. What each feature buys, and what it costs

Numbers are Σ per-turn server `prefillMs` at **depth 16, ~900 tok/turn** unless
noted — *not* wall clock, which was never recorded (§7). Run
`cells_20260729-232251`.

| feature | benefit (measured) | cost (measured) | generality |
|---|---|---|---|
| **Resident vs replay** | **7.8–9.8×** less prefill per session. gpt-oss 207.7→26.4 s · glm 521.4→65.1 s · gemma-26b 271.7→27.6 s · hy3 **1004.4→129.4 s** (~875 s/session) | the seq band (below). Dense models are **token-flat but not time-flat**: hy3 6.2→11.2 s/turn at constant 1,141 fresh tok | **4 architectures + 5 spot models.** Replay growth 6.56–6.99× across 7 models — architecture-independent |
| **Flow cache** | glm **3.51 s/call = 49% of prefill**; gpt-oss live-accept **~4.0 s/call** (4,237→17 fresh tok) | **8 seq ids.** On the swarm those +8 (with +2) took a served 744,448 @131 seqs to Metal-OOM at 141 — settled 589,824, i.e. **~21% of pool surrendered** | ⚠️ **glm only, 11 calls** + one gpt-oss pair. The same 3.51 s is quoted verbatim in gemma-26b/hy3/swarm headers for models it was never measured on |
| **Snapshot hot fork** | **26×–123×** vs cold. hot 16–26 tok / 0.3–1.0 s vs cold 17k–19k tok / 25.8–123.4 s | 2 seq ids, always allocated; capacity-**rejected**, never evicted | 7 models cold, 3 hot at depth 16 |
| **Reasoning head-swap** | **0.62 ms/swap**, 0 corruption in 237 swaps; body never re-prefilled | +k seq ids (never measured in isolation) | gpt-oss only, and the steer itself is gpt-oss-specific |
| **Batched engine** | aggregate **50.3→271.7 tok/s** at N=1→128 (5.41×); vs pool at 3 streams **~5.4×** and no latch death; wired **flat 78 GB** across all W | **the gain inverts with depth**: N=8÷N=1 is 2.44× @2.2k but **0.73× @21.8k**. Real mixed workload 104.7 tok/s — 30% under the ladder. Forfeits 4 features | gpt-oss only, one build |
| **Seq band, `kv_unified: false`** | — | **window ÷ n_seq_max.** Stock=12: glm 202,752→16,896 and **the needle fails**; hy3 32,768→2,730 < 1,793 static prefix → decode failure | 3 models by register + 3 historical casualties |
| **`kv_unified: true`** | full window, needle survives | **2.7%** slower prefill (glm 63.4 vs 65.1 s) | the run's clearest verdict — dominates band suppression |
| **Speculative** | — | **23–33% slower** on Metal MoE, single-stream | gpt-oss, single-stream, file-reproduction prompts — *not* the swarm regime |

**Do not cite "resident is 15–19× cheaper."** That compares different models
against each other on the probe-depth matrix. The same-model, same-workload
figure is **~8×**.

---

## 4. Per-model: what you can layer today

| model | strategy | can add today | blocked, and why |
|---|---|---|---|
| gpt-oss-120b-a5-swarm-524k | **S3** | — (flow + snapshots + head-swap all on) | flow-fork, CoT strip, overrides, speculative, cold-snapshot rebuild |
| gpt-oss-120b-a5 | S2 | snapshots, CoT strip | — |
| glm-4.7-flash (MLA) | S2 | head-swap ⚠️ family has no `reasoning.levels` → won't qualify | needle fails at 16,896/seq — keep `kv_unified: true` |
| hy3-reap-200b-a21 | S2 | snapshots | head-swap (family unmapped). Biggest replay saving in the fleet (875 s/session) |
| gemma-4-26b-a4b | S2 | head-swap (one level ⇒ no band) | — |
| gemma-4-31b | S2 | — (flow cache ON 2026-07-30) | the old `false` was a stale M8-era verdict; payout is INHERITED from glm, not measured here |
| mistral-medium-3.5-128b | S2 | snapshots, head-swap | resident payout unmeasured (no replay arm) |
| devstral-2-small-24b | S2 | flow, snapshots | replay arm unmeasured |
| laguna-xs-2.1 | S2 | head-swap ⚠️ family unmapped | — |
| olmo-3.1-32b ×2 | S2 | flow, snapshots | gate-blocked; family unmapped for head-swap |
| qwen3-next-coder-80b-a3 | **S2 ✅** | flow cache, snapshots | resident **VALIDATED 2026-07-30** (cell RC3): flat to depth 16, needle intact, hot snapshot fork — see §5 |
| laguna-s-2.1 / -apex / -poolside | **S3** | flow cache (currently `false`) | plain `laguna-s-2.1` **does not load** (arch absent from our build) |
| **qwen3.5-122b-a10** | **S1** | **nothing** | can_shift **False** measured — recurrent blocks seq ops regardless of flags |
| **qwen3.6-27b** | **S1** | **nothing** | can_shift **False** measured (811.4 s/session) |
| **step37-flash-196b-a11** | **S1** | **nothing** | can_shift **False**; `reasoning_head_swap: true` is inert and the config says so |
| **qwen3.6-35b-a3** | **S1** | measure can_shift first | `flow_kv_cache: true` with resident off — **buys nothing**, see §5 |

**The S1 four get no cache features at all** — no flow, no snapshots, no
head-swap, no windowing, and O(n²) sessions. Their only lever is **flow design**:
keep agent sessions shallow. That is a real constraint on model choice, not a
config gap, and three of the four are measured-and-correct.

### 4a. S2 is the optimum; track what S1 costs a family

**S2 (pool + resident) is the definite optimum for any model that supports it** —
it gets every feature, and S3 buys concurrency by *forfeiting* four of them
(flow-fork, CoT strip, sampling overrides, cold-snapshot rebuild) plus taking a
throughput inversion past ~12k depth. S3 is the right shape for swarm fan-out
and the wrong shape for a deep single session.

That makes S1 membership a standing handicap, and it is **confounded with model
quality in every tier comparison we run**: the qwen-likes are judged on artifacts
produced while paying 7.8–9.8× the prefill of their S2 competitors and losing
snapshots and head-swap entirely. A tier placement for an S1 model is therefore
not comparable to an S2 model's without saying so.

**Open ask (operator, 2026-07-30): track family performance relative to S2
models.** The cheap version costs nothing extra — every tier arm already records
the strategy triple in health, so tag each arm's result with its strategy and
report S1 placements separately rather than pooled. The expensive version is a
matched A/B, which S1 models cannot run *by construction* (they cannot be put on
S2), so the honest ceiling here is: report the handicap alongside the score,
never subtract it.

---

## 5. Defects this analysis surfaced

Found while building the matrix; none previously recorded. **All seven are now
resolved** — six fixed 2026-07-30 (`20c51f6`), the seventh measured and closed
(`a679548`). Kept here because the failure SHAPE is the reusable part: a config
requests a feature, the run does not deliver it, and nothing reconciles the
two.

1. **FIXED — a cold snapshot fork under S3 permanently leaked a seat.** `start_session`
   pins the seat (`session_manager.py:369`) before the fork; under batched a cold
   entry makes `rebuild_snapshot_cold` **raise** (`llama_cpp_backend.py:2236`), and
   the exception escapes *before* the session is registered — so the seat is never
   released, and the reaper skips it because `pinned` is already true
   (`batched_engine.py:1524`). Every such call costs one of 128 seats, forever.
   Reachable after **any** context refresh, which demotes all hot snapshots.
2. **FIXED — `"flow band ACTIVE in batched mode (8 slots)"` was false on the three
   laguna configs.** `_flow_band` is computed once in `__init__` from
   `_flow_resident or _session_flow_fork` and never recomputed after
   `_session_flow_fork = False`, while the seq map sizes `flow_slots` from
   `flow_kv_cache` alone. Batched + `flow_kv_cache: false` logs an 8-slot band
   that does not exist.
3. **FIXED — two pool-only degradations were `log.debug`** — `resident_strip_reasoning`
   and `sampling_overrides`. A batched config with CoT strip on accumulates
   reasoning across every turn with zero operational signal.
4. **FIXED — dead "legacy_save_state" strings survived** in `_session_strategy()` and the
   can_shift-denial warning ("falling back to the legacy save_state path" — it
   falls back to full_replay). Health inherits the dead value.
5. **FIXED — `qwen3.6-35b-a3` had `flow_kv_cache: true` with resident off.** Since the M8
   blob path was deleted, this takes the retired branch: increments
   `flow_fallbacks` forever and serves from the static base. Turn it off, or
   measure can_shift and flip resident.
6. **RESOLVED — `qwen3-next-coder-80b-a3`'s resident claim was the one in the
   fleet with contradicting family evidence and zero measurement.** Measured
   2026-07-30 (cell RC3, depth 16, ~900 tok/turn): `can_shift=True`, curve flat
   at 1,107 fresh tok/turn, `curve_growth 0.02`, 34.5 s total prefill, needle
   and early fact both intact, and a **hot** snapshot fork at 21 tok / 1.6 s.
   The claim holds. **The family generalization was the wrong part** —
   qwen3.5-122b's header said "the recurrent component blocks seq ops
   regardless of SWA flags", which is true of the GDN models and NOT of this
   DeltaNet one. Scoped at the source. Hybrid-recurrent is not by itself
   disqualifying, so the remaining S1 members are S1 on their own measurements,
   not by family inference.

7. **FIXED — stale write-backs read as current fact.** `gemma-4-26b-a4b` keeps
   `probe_verified_n_ctx: 262144` while its own comment declares it retired;
   `laguna-xs-2.1`'s F2 cell says `can_shift: False` for a config now on resident,
   with no post-flip row to disambiguate.

---

## 6. Corrections to the existing corpus

- `CORPUS.md` §3 still marks flow cache **⊗ ignored** and snapshots **⊗ raises**
  under batched. Both shipped 2026-07-30 (capture + hot fork for snapshots; BUILD
  + HIT for flow). Cold rebuild remains the batched boundary.
- `CORPUS.md` M8 and M5 describe code that no longer exists — both deleted.
- `EXPERIMENT.md` still records **P-E3 as a MISS** with "the 11b question is
  UNANSWERED". That was a client-contract violation, not a server defect; the
  corrected pilot is a HIT at 3.51 s/call. CORPUS carries the correction,
  EXPERIMENT does not.
- M10's "dominant hidden cost" stands and is worth restating: the flow band is
  allocated when `flow_kv_cache` **OR** `resident_session_flow_fork` (default
  **true**) — so a stock resident pool config allocates 8 seqs even with the flow
  cache off. That is where "stock = 12" comes from, and under
  `kv_unified: false` it is what divided the window by 12 in every historical
  casualty.

---

## 7. What we cannot answer yet

The run that produced these numbers pre-registered five response variables and
**collected one**. Absent from every cell: `decode_tps`, `prefix_reuse_rate` (the
corpus's own designated cache quantity), wired GB, the failure counters that were
the declared void criterion, and session **wall clock**. So:

- Every "8× payout" is a **prefill-seconds** ratio, not a wall-clock one.
- P-B2's claim that band layout costs nothing has data for the prefill half only.
- "Zero voids" is asserted against a criterion that was never recorded.

Also unmeasured: flow payout on any model but glm; what the 8-seq band costs in
throughput; the snapshot and reasoning bands in isolation; head-swap on any
non-gpt-oss family; speculative in the swarm regime (**no accepted-vs-proposed
instrumentation exists**); context refresh's re-prefill price; and the
shared-prefix fan-out A/B (OPEN_TASKS §10a), against a standing measured waste of
**1.03M of 1.54M prompt tokens (67%) redundant re-reads**.

The highest-value missing measurement is the one that would change a decision:
**does the flow cache pay on anything but glm**, given it is now on in seven
configs on the strength of eleven calls against one model.

**Measure it in the RIGHT REGIME (operator, 2026-07-30).** The flow cache was
designed for the interact → diagnose → fix cycle, where the same flows are
re-entered many times. A `top_phase: structural` run enters each flow roughly
once, so its hit counts say nothing either way — the 2026-07-30 fleet smoke
recorded 0 hits/1 build (gpt-oss) and 1 hit/2 builds (laguna-xs) and neither
number is evidence. The measurement that decides this feature is a
FUNCTIONAL-phase run with its diagnose loop, not a structural one.
