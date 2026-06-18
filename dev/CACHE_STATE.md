# Cache State — KV reuse across the LLMVP server

_Last updated: 2026-06-18. Scope: every KV-prefix/state reuse layer Ouroboros flows use against the local LLMVP server (`llama-cpp-python` 0.3.39 embedded). Supersedes the 2026-06-16 revision (which predated the resident-sequence transition and listed session caching as the top open lever — now solved)._

## TL;DR

- **Caching is organized by LIFETIME**, not by code path. Four layers: **permanent** (global static buffer), **semi-permanent / flow** (`flow_kv_cache`), **semi-permanent / session** (memoryful session KV), and **single-turn** (the dynamic tail, uncached). See [Cache layers by lifetime](#cache-layers-by-lifetime).
- **Two implementation strategies coexist, flag-switched:**
  - **Legacy (currently LIVE in every config):** whole-context `save_state`/`load_state` (`LlamaState` blob) for the static buffer + flow cache; `session_full_replay` (re-prefill the whole history each turn) for sessions.
  - **Resident (implemented + validated, DORMANT — `resident_seq_cache: false` everywhere):** KV stays **live in-context** on dedicated `seq_id`s, forked with `llama_memory_seq_cp`, never serialized. Replaces all three legacy paths. Flip `resident_seq_cache: true` to switch a model over. See [Implementation strategy](#implementation-strategy-resident-in-context-sequences).
- **The resident path closes the deep-session problem** the legacy path can't: no `save_state` blob → no ~2 GB overflow crash; the session seq stays live → no per-turn re-prefill (the deep-session timeout lever). Validated flat per-turn prefill on gpt-oss-120b / Devstral / Qwen3-Next; bit-identical to legacy at temp 0.
- **Hard dependency on SWA models: `swa_full: true`** (+ `kv_unified: true`) — without it any pinned/forked prefix corrupts (`llama_decode code -3` at the SWA window boundary). True for both strategies. See [SWA dependency](#swa-dependency).

---

## Cache layers by lifetime

| Lifetime | Layer | Holds | Shared by | Eviction | Content it should carry |
|---|---|---|---|---|---|
| **Permanent** (process) | Global static buffer (`SOUL.md` + knowledge) | the invariant identity/knowledge head (~1.8 k tok) | **every** request & flow, server-wide | server restart / buffer rebuild | only **generalizable** identity + principles that apply to *all* flows |
| **Semi-permanent / flow** | `flow_kv_cache` static head | `[global static + flow head]` | all tasks/cycles of one `(flow, step)` | LRU, bound `flow_kv_cache_max` (8); restart | **task-invariant** per-flow framing — role · instructions · output-format |
| **Semi-permanent / session** | memoryful session KV | the live multi-turn transcript | all turns of one session (pinned instance) | session TTL (default **300 s**) / `end_session` | **persistent turn details** later turns reference (investigation transcript, plan state, prior answers) |
| **Single-turn** | the dynamic tail | this request's variable prompt | nothing — prefilled then discarded | immediate (post-generation) | only the details **relevant to the exact task at hand** |

> The **session flow-fork** (`resident_session_flow_fork`, Phase 2b) bridges the two semi-permanent layers: a session can *start* from a pinned flow head (the per-flow invariant preamble) and then go live — so the preamble is forked once per instance instead of re-prefilled every new session.

The lifetime hierarchy is a **prefix tree**: `global static → flow head → live session turns → dynamic tail`. Each deeper layer extends the one above it; the content guidance above just says "put a fact at the shallowest layer whose lifetime/scope it actually needs." A fact placed too deep is re-prefilled needlessly; placed too shallow it's wasted KV on every unrelated request (and, for the permanent buffer, can't change without a rebuild). Authoring guidance for prompt authors lives in [PROMPTING_CONVENTIONS.md §1 "Cache lifetimes."](../PROMPTING_CONVENTIONS.md)

---

## Implementation strategy: resident in-context sequences

The go-forward strategy (dormant behind `resident_seq_cache` until enabled per model). **Principle:** reuse KV by keeping it *resident on dedicated sequences inside the one live context*, forking cheaply between them — never serializing a `LlamaState` blob. Mirrors llama-server's slot pattern, built on the seq primitives `llama-cpp-python` 0.3.39 already exposes.

### Reserved sequence map

```
SEQ_WORKING  = 0    the live generation / session stream (the ONLY seq generated on)
SEQ_STATIC   = 1    pristine global static template (fork source; never generated on)
SEQ_FLOW_BASE= 2    per-flow prefixes occupy the band [2, 2 + flow_hot_set)
```

`n_seq_max` widens to `2 + flow_hot_set` when the flow band is active, else `2`, else `1`.

### Primitives (no serialization)

- `memory_seq_cp(src, dst, -1, -1)` — **fork**: clone a pinned template's KV onto the working seq. The keystone op for every "restore."
- `memory_seq_rm(seq, p0, p1)` — **clear / purge**: drop a seq, or roll a degenerate turn back to `pre_turn_pos`.
- `memory_seq_add(seq, p0, p1, -n)` — **window shift**: slide a deep session past `n_ctx` (see Windowing).
- `memory_can_shift()` — **the gate**: true on SWA+`swa_full` and can-shift hybrids (Qwen3-Next); **false on pure-recurrent** (Qwen3.6 family). When false the warm-up forces the legacy path. This makes resident **auto-safe on any model**.

### How each layer maps to sequences

| Layer | Resident mechanic | Legacy mechanic (still live) |
|---|---|---|
| **Permanent** | warm-up evals the static once onto `SEQ_STATIC`; `acquire_instance` forks `SEQ_STATIC → SEQ_WORKING` (cheap intra-context copy) | `load_state(_static_state)` — multi-GB memcpy per acquire |
| **Flow** | BUILD: eval flow head on `SEQ_WORKING`, `seq_cp` it to a band seq; HIT: `seq_cp(flow_seq → SEQ_WORKING)`. Per-instance, LRU over the band (Phase 2) | `_flow_states` OrderedDict of `save_state` blobs (shared across instances) |
| **Session** | `SEQ_WORKING` kept **live** across turns — no restore, append the new turn only, no `save_state` (Phase 1). Optional in-place CoT strip (`resident_strip_reasoning`); optional turn-0 flow-fork (Phase 2b) | per-turn `save_state` (splice) **or** `session_full_replay` (re-prefill all history each turn) |
| **Single-turn** | forked static + dynamic tail on `SEQ_WORKING`; cleared on release | dynamic tail prefilled on the loaded static; discarded |

### Windowing (deep sessions past `n_ctx`)

When `pre_turn_pos + turn_tokens + max_tokens ≥ n_ctx`, `_window_resident_seq` drops the oldest ~half of the conversation (`memory_seq_rm`) and shifts the recent tail down (`memory_seq_add`), **keeping the static head** (`n_keep` = the session's `static_base` — the flow head if a flow-fork session pinned one, else the global static). Only the tail *beyond* the head is shifted, so the static-prefix positions are untouched (the SWA `seq_add` pos-min hazard the strip path warned about does not apply). Replaces the legacy hard `ValueError` at the context-window guard; lets a session run indefinitely. Validated on gpt-oss SWA: coherent post-window, old turns correctly fall out.

### Flags (all default off / safe)

| Flag | Effect |
|---|---|
| `resident_seq_cache` | master switch: static-fork + sessions resident. Gated by `memory_can_shift()`. |
| `resident_strip_reasoning` | in-place Factor-4 CoT strip on the live session seq (thinking families). |
| `resident_session_flow_fork` | turn-0 fork of a pinned flow head onto a session (needs `flow_key` + `static_prefix` at `start_session`). |
| `flow_kv_cache` / `flow_kv_cache_max` | enable the flow layer / its LRU bound (8). Consumed by both strategies. |
| `swa_full` / `kv_unified` | **required** on SWA models for either strategy (see below). |
| `session_full_replay` | legacy session path (default **true**); ignored when resident is active. |

---

## SWA dependency

gpt-oss-120b, step37 (SWA-512), and gemma use **sliding-window attention**. The static prefix (~1.8 k tok) exceeds the SWA window, so a pinned/forked KV whose SWA layers held only a short tail is **inconsistent at the window boundary** → the next decode fails `llama_decode code -3` (observed at pos ~1809). This is the original `flow_kv_cache` corruptor and applies equally to resident forks.

**Fix:** `swa_full: true` keeps the full-size SWA KV for those layers so the pinned prefix stays consistent (and sets `memory_can_shift() = True`, which the resident gate requires). `kv_unified: true` bounds the extra SWA-KV cost on unified (Metal) memory. **Both are mandatory** alongside `flow_kv_cache: true` or `resident_seq_cache: true` on any SWA model. Validated stable (server logs `using full-size SWA cache`; zero `code -3` over the stability probe). See memory `flow-kv-cache-corrupts-gptoss`, `save-state-failure-modes-and-seq-state-path`.

The campaign harness ([cross_val_campaign.sh](cross_val_campaign.sh)) runs a 10-cycle BUILD/HIT stability probe after each restart and **auto-disables `flow_kv_cache`** for a model if any `code -3` appears.

---

## Per-model config flags (current, LIVE)

All models currently run **legacy** (`resident_seq_cache` unset → off). Resident is validated for the can-shift models and ready to enable.

| Model | `flow_kv_cache` | `swa_full`·`kv_unified` | `session_full_replay` | `can_shift` (resident-eligible?) |
|---|---|---|---|---|
| gpt-oss-120b-a5 | ✅ | ✅ | ✅ | ✅ (SWA+swa_full) — validated |
| step37-flash-196b-a11 | ✅ | ✅ | — | ✅ (SWA-512) — needs `temperature_floor` |
| gemma-4-31b | ✅ | ✅ | — | ✅ (SWA) — resident **flow** validated |
| qwen3.6-35b-a3 | ✅ | ✅ | ✅ | ❌ pure-recurrent → gate forces legacy |
| qwen3-next-coder-80b | — | — | ✅ | ✅ (can-shift hybrid) — validated |

**To enable resident on a validated model:** set `resident_seq_cache: true` (and optionally `resident_strip_reasoning: true` for thinking families / `resident_session_flow_fork: true` for preamble-heavy session flows). The `can_shift` gate auto-reverts to legacy if the model can't support it, so the flag is safe to set anywhere.

---

## Measured benefit

### Legacy flow-HIT (2026-06-16, 22,904 generation records)

Prefill is strongly **sublinear** — cost concentrates at high context positions, so caching the *cheap leading* ~2 k head saves a flat ~0.2–0.5 s/call regardless of model:

| Model | prefill ~2k | ~4k | ~8k | HIT eval | saved/call |
|---|---|---|---|---|---|
| gpt-oss-120B | 0.6s | 3.1s | 10.6s | 0.10s | ~0.5s |
| step37 | 1.0s | 7.9s | 22.5s | 0.50s | ~0.5s |
| qwen3.6 | 0.9s | 3.5s | 7.3s | 0.70s | ~0.2s |
| gemma-4-31B | 0.9s | 14.0s | 40.2s | 0.40s | ~0.5s |

The flow cache is a correct, cheap conformance optimization — **not** the lever that moves bench wall-clock.

### Resident wins (the levers that DO move wall-clock)

- **Session re-prefill eliminated.** Legacy `session_full_replay` re-prefills 11 k–17 k-token histories at **33–54 s each** on step37. Resident keeps the seq live → per-turn prefill is **flat** (~0.12 s gpt-oss / ~0.78 s qwen-next), independent of depth. This is the direct fix for the deep-session timeout losses (memory `framework-overhead-timeouts`).
- **Deep-session crash removed.** The ~2 GB `save_state` overflow (~30–34 k tok) can't occur — nothing is serialized.
- **Session flow-fork: turn-0 TTFT 5.4 s → 0.26 s (~21×)** on gemma-4 — a 920-token preamble forked via `seq_cp` instead of re-prefilled on every new session (HIT vs BUILD), byte-identical output.

### Validated on real workloads (2026-06-18)

- **A/B on a real feature task** (gpt-oss, same replay-point + directive, legacy `full_replay` vs resident; `dev/ab_resident_test.sh` → `ab_trace_compare.py`): **session-turn wall 14.2 s → 7.4 s mean (−48 %), 13.0 s → 4.5 s median (−65 %)** — ~2× faster session turns. `tokens_in` is flat both arms (it's the agent-sent turn; the discriminator is server-side `wall_ms`). 0 overflow either arm.
- **terminal-bench retest** (gpt-oss, resident + pre-warmed flow cache, 8-set; `dev/tb_resident_gptoss.sh`): **3/8 (37.5 %) → 5/8 (62.5 %)**, tying the Terminus 62.5 % baseline — the ~2× faster session turns convert `agent_timeout` losses into completions (memory `framework-overhead-timeouts`). Pre-warm: 16 flows BUILT, 620 HITs over the run, **0 code-3, 0 crashes, 0 windowing thrash**.
- **Windowing-trigger bug found + fixed here:** the trigger reserved the FULL `max_tokens` (agent passes ≈ n_ctx for thinking turns) → windowed every turn and gutted session memory. Capped at `_WINDOW_GEN_RESERVE=16384`. The verify harness missed it (small max_tokens); the real-workload A/B caught it. Lesson: validate with realistic max_tokens.

---

## History of previous cache attempts & failures

Terse, chronological — why the current design looks the way it does. Each line is a dead end we already paid for.

1. **Whole-context `save_state`/`load_state` for sessions** — the original memoryful path. The `LlamaState` blob grows ~70 KB/token (with `swa_full`, which gpt-oss requires) and **overflows 2³¹ bytes at ~30–34 k tokens** → `"Negative size passed to PyBytes_FromStringAndSize"` crash on ~40-turn gpt-oss sessions. Dead end for deep sessions.
2. **Per-sequence state (`llama_state_seq_get/set_data`)** as a smaller blob — hits the **same ~2 GB wall** for a single deep sequence. No serialization format escapes a deep session.
3. **`flow_kv_cache` via `save_state` on SWA without `swa_full`** — the pinned ~1.8 k prefix corrupted at the SWA window boundary → `llama_decode code -3` at pos ~1809, over a run. **Disabled `flow_kv_cache` entirely** until `swa_full: true` was found to fix it (memory `flow-kv-cache-corrupts-gptoss`).
4. **`session_full_replay` as the workaround** — dodges the overflow + corruption by re-prefilling the whole history every turn. Correct but **re-prefills a growing transcript cold each turn** → 33–54 s/turn deep, the dominant deep-session timeout cost. Still the live default; the resident path is what retires it.
5. **mistral.rs as a second backend** (for its in-memory PagedAttention prefix cache) — **NO-GO.** Incremental prefill over a deep cached prefix ran ~13 tok/s on Metal (~200× slower than cold batched; no chunked prefill), and macOS compressed the ~70 GB weights when the server idled (re-fault penalty; no mlock). llama.cpp remains the better Metal option (memory `mistralrs-nogo-use-llamacpp-native-cache`, [MISTRALRS_SPIKE.md](MISTRALRS_SPIKE.md)).
6. **Resident strip on truncated turns** (during Phase 1 validation) — a turn that hit `max_tokens` mid-analysis (no final channel) replayed an **empty** `<|channel|>final<|message|>` → answerless turns that compounded. Fixed with an empty-content guard in `_maybe_strip_reasoning` (keep the raw generation when there's no clean answer to replay).
7. **`_resident_generate` multi-seq decode loop** (planned keystone) — turned out **unnecessary**: each context runs one working stream, so the working seq is always seq 0 and the existing `Llama.generate()`/sampler/guards are reused verbatim. Eliminated the #1 sampler-fidelity risk by *not* building it.

**Net lesson:** serialization (`save_state`, per-seq blobs, cross-backend) is the recurring failure locus for deep state; resident in-context sequences sidestep it by never leaving the context. The `can_shift` gate + `swa_full` are the two invariants that keep it correct.

---

## Prompt convention (static → dynamic ordering)

Unchanged by the resident transition — it governs *where the cache boundary falls in a prompt*, independent of how the KV is stored. A cacheable template orders `sections:` so every **task-invariant** section (`cache: true`) leads and all **dynamic** sections follow. `PromptRenderer.render_with_cache_split(template, ns)` ([agent/loader.py:319](../agent/loader.py#L319)) returns `(static_prefix, dynamic_suffix)` with exact reconstruction (`static_prefix + dynamic_suffix == render(...)`). Because the static prefix references no task variables it is **byte-identical across tasks** → its hash is stable → one cache entry is shared bench-wide. Authoring rules: PROMPTING_CONVENTIONS.md §10 "Cache-aware ordering."

The agent sends `prompt` (dynamic tail) + `static_prefix` + `flow_cache_key`; the server pins per `flow_key`. `dev/warm_flows.py` pre-fires one BUILD per `(flow, step)` so the run sees only HITs. Run order per model: **restart → stability probe → `warm_flows.py` → bench.**

---

## Pointers

- **Memory:** `resident-seq-cache-implemented` (the full transition record), `save-state-failure-modes-and-seq-state-path`, `flow-kv-cache-corrupts-gptoss`, `mistralrs-nogo-use-llamacpp-native-cache`, `framework-overhead-timeouts`.
- **Code:** [llmvp/inference/backends/llama_cpp_backend.py](../llmvp/inference/backends/llama_cpp_backend.py) (seq map, `_resident_flow`, `_window_resident_seq`, `_resident_restore_static`), [llmvp/core/session_manager.py](../llmvp/core/session_manager.py) (resident session turn, flow-fork, windowing), [llmvp/core/config.py](../llmvp/core/config.py) (flags).
- **Dev harnesses** (in `llmvp/dev/` unless noted): `verify_resident_session.py` (sessions + windowing via `nctx=`), `verify_resident_jit.py` (JIT identity), `verify_resident_flow.py` (stateless flow BUILD/HIT), `verify_resident_session_flow.py` (session flow-fork); `dev/cache_strategy_stress.py` (seq-op stress, repo-root `dev/`).
