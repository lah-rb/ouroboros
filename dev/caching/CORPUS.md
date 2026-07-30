# CACHING CORPUS — every strategy, claim, and measurement, in one place

**Assembled 2026-07-29** from three exhaustive sweeps (docs · mechanisms ·
harnesses/results) plus same-day primary material (the 7-row compat matrix, the
hy3 tier arm, the windowing-hazard investigation). Supersedes
`dev/archive/docs/CACHE_STATE.md` as the reference; that document remains the
authoritative *snapshot of 2026-07-12* and is pointed here from its header.

> **ADDENDUM 2026-07-30 — what changed the day after assembly.** Read this
> before citing the mechanism catalog:
> - **M8 (save_state-blob flow cache) and the legacy save_state session splice
>   are DELETED**, not merely unreachable. Two session strategies remain,
>   resident and full-replay. The OPEN_TASKS items that tracked both
>   (legacy-save_state retirement, flow-cache revisit) are closed and deleted.
> - **M9's flow band now works under `decode_mode: batched`** — the shape
>   production actually runs — and is live-accepted on gpt-oss swarm: fresh
>   prefill 4,237 → 17 tokens, 3,970 ms → 0.1 ms per call, needle through the
>   pinned head verbatim. `flow_kv_cache: true` on all 7 servable resident
>   configs. The 2026-06 ban applied to M8 and does not survive it.
> - **The wired limit is NOT a memory ceiling** (§4's ledger implied it could
>   be). `iogpu.wired_limit_mb` = 121.6 GB has been crossed by FOUR separate
>   passing measurements — 116.4 GB (2026-07-28 ladder), then 121.7, 123.0 and
>   123.6 GB (2026-07-30 probe rungs, each 3/3 generations clean). PHYSICAL
>   memory is what kills the machine; both recorded hard reboots ran it out of
>   RAM outright. A clamp to the wired limit was briefly added to the context
>   probe and retracted the same day for refusing production-proven configs.

Caching was the original concern that fueled building LLMVP. This corpus exists
because the evidence had fragmented across ~29 sources, "cache hit rate" had
come to name three different quantities, and at least one doctrine statement
("resident is safe in ANY config") had been falsified without any document
saying so. Companion: `dev/caching/EXPERIMENT.md` — the formal design that
fills the gaps this corpus names.

Rules of the document: every claim carries an anchor; every number is tagged
**[M]** measured, **[C]** claim/design-intent, or **[S]** superseded; a
contradiction is either RESOLVED with its resolution or tagged OPEN.

---

## 1. Source map

### Primary strategy documents

| source | date | status | one-line |
|---|---|---|---|
| `dev/archive/docs/CACHE_STATE.md` | 2026-07-12 | **[S]** self-declared CLOSED, still most-cited | The five-lifetime layer model; the 2026-07-02 arch × mode matrix; the resident-win receipts; the 9-item dead-end history |
| `dev/CACHE_SWEEP_PLAN.md` | 2026-07-29 | [M]+[C] | The hy3 full_replay cost case; fleet table (9/19 on full_replay); pre-registered predictions, one retracted in-file |
| `dev/swarm_performance/FINDINGS.md` | 2026-07-26 | [M] 96 cells | Prefill does not parallelize (≈1.0); static prefix ~94% free (w=0.06, by intervention); interior optimum in N; heterogeneity uncharacterized (+32.5% over-prediction) |
| `dev/serving_perf_reference.md` | 2026-07-20/21 | [M], §1 corrected in-file | Wedge zone; prefill-vs-size bands; 430-round 200k resident session, turn-N prefill 0.0 s |

### Fragment sources (load-bearing lines only)

| source | tag | carries |
|---|---|---|
| `dev/archive/state_exp/README.md` + `ab_comparison.txt` (2026-06-13) | [M] | The save_state-vs-full_replay A/B: wall −2.8%, session-infer mean +14.4%, P90 prefill +44%, overflow 1→0. Origin of "wall-clock-neutral… shallow sessions" |
| `dev/archive/docs/MISTRALRS_SPIKE.md` | [M]→NO-GO | Automatic prefix cache 50–80× cheaper warm — and ~13 tok/s incremental prefill on Metal (~200× slower than cold batched). The road not taken, and why |
| `dev/archive/docs/ADAPTIVE_REASONING_DECISION_LAYER.md` | [M] | Head-swap on the cached system slot: 0.62 ms/swap, 0 corruption over 237 swaps; stateless variant `cached 1809 / fresh 46` at every level |
| `dev/laguna/FINDINGS.md` | [M] | KV-pool exhaustion evicted a 48,318-token batch carrying 15 complete files; per-stream clamps vs sum-budget pool; "context length, not quant, moves decode — an argument for the prefix-cache work" |
| `dev/archive/docs/SWE_PHASE_A_FINDINGS.md:78` | [M], register UNKNOWN | "Hit rates 0.03–0.22 → 0.72–0.90" — most-cited cache number in the repo, anchored to no register (see F5) |
| `dev/swarm_performance/{PREREG_static_prefix,METHODS}.md` | [C]→[M] | The pre-registration discipline + two in-file retractions ("an undocumented measurement is not the same as a false one") |
| `llmvp/configs/*.yaml` headers | [M]/[C] | Per-model measured B/tok vs formula (1.00×–22.59× over), the gemma swa_full trilemma, hy3's declared-baseline handicap block, stale "re-enabled 2026-06-16" comments |
| `PROMPTING_CONVENTIONS.md` §1, §10 | [C] | Cache-lifetime placement doctrine; only a leading contiguous `cache: true` run caches — any dynamic section ends the run |
| `CONTRIBUTING.md:193-235` | [M]-backed rules | KV geometry BEFORE configs; `swa_full` inflates every SWA layer (gemma-4 262k ≈ 480 GB = precomputable death) |
| `AGENT.md:287-297` | **[S]** | "Universal context effectively free… zero marginal cost" — refined by w=0.06; names the legacy save_state mechanic |
| `llmvp/core/config.py` long comments | [C] citing [M] | The much-quoted state_exp summary; the resident gate; the full_replay refusal rationale |
| `blueprint.md:607,1185,1388` | [C] | Flow-design assumptions of KV residency (`:1185` is the turn state_exp caught overflowing) |
| `dev/bakeoff_results/cache_matrix.jsonl` + console log | [M] 2026-07-29 | The 7-row sweep (§4.1) |

**Provenance hazard (fixed with this corpus):** the two most decision-relevant
sources — CACHE_SWEEP_PLAN.md and bakeoff_results/ — were untracked in git,
while the CLOSED archive doc was the only committed reference. They are
committed alongside this corpus.

---

## 2. Mechanism catalog (M1–M20)

Full per-mechanism detail (knobs, anchors, warts) is in the mechanism sweep;
this catalog is the working reference. **Two arithmetic facts come first because
they govern everything below.**

### The band arithmetic (pool mode, resident requested)

```
n_seq_max = 2                                   # SEQ_WORKING=0 + SEQ_STATIC=1
          + 8   flow band     — allocated when flow_kv_cache OR
                                resident_session_flow_fork (DEFAULT TRUE)
          + 2   snapshot band — session_snapshot_max default
          + k   reasoning heads — if reasoning_head_swap (k = pinned levels)
```

Stock resident config → **n_seq_max = 12** (14 on gpt-oss with head-swap).
`inference/seq_layout.py:49-70`, `llama_cpp_backend.py:541-545`.

### The window division

Under `kv_unified: false`, llama.cpp splits the allocation:
**`n_ctx_seq ≈ n_ctx / n_seq_max`** (rounded up to 256). Verified against every
known casualty — all are exactly /12:

| incident | n_ctx | n_ctx_seq | consequence |
|---|---|---|---|
| OLMo 2026-07-22 | 65,536 | 5,632 | first design prompt failed to decode |
| qwen3.5 2026-07-22 | 264,192 | 22,016 | silent context amputation |
| **hy3 2026-07-29 sweep** | 32,768 | **2,730 < 1,793 static prefix** | `Llama.eval(decode): Failed completely` |

Under `kv_unified: true` there is **no division** — n_ctx is one shared cell
pool across all seqs. LLMVP **never reads `llama_n_ctx_seq`** (exposed at
`_internals.py:602`); every guard uses `inst._n_ctx = min(n_ctx,
model_max_context)`, which is why the failure mode is a raw decode error, not a
clean refusal. **Fix = EXPERIMENT P1.**

**Live scope of the hazard (verified 2026-07-29):** production
(gpt-oss-524k) is batched + `kv_unified: true` — no division, never exposed.
**Zero fleet configs today combine resident + pool + kv_unified:false.** The
exposure is temp/experiment configs (how hy3 failed) and any future naive flip.

### The mechanisms

| id | mechanism | knob (default) | shape served | precondition | cost |
|---|---|---|---|---|---|
| M1 | Static token buffer on disk | `knowledge.tokens_bin` | all | — | disk/RAM; auto-rebuilds on staleness (self-healing — see the nopersona null) |
| M2a | Static prefix, legacy blob | (selected when resident inactive, pool) | stateless + replay turns | none — works everywhere | multi-GB memcpy per acquire |
| M2b | Static prefix, resident SEQ_STATIC fork | `resident_seq_cache` (False) | stateless | `memory_can_shift()` | +1 seq |
| M2c | Static prefix, batched persona band | `decode_mode: batched` | all | batched preconditions | +1 seq/persona |
| M3 | Multi-persona heads | `personas:`, `slot_personas` | routing | pool: eager only | +1 seq/persona (batched) |
| M4 | `session_full_replay` | (True; **cannot be False** — validator) | session turns | none — the universal fallback | **O(n²) in turns** [M: hy3 8.1→58.8 s/turn] |
| M5 | Legacy save_state per-turn splice | unreachable via validated config | — | — | retired; rap sheet in validator docstring |
| M6 | Resident live session seq | `resident_seq_cache` | session turns | can_shift | flat per-turn delta [M: 8–12 tok/turn]; costs the band |
| M7 | `resident_strip_reasoning` | (False) + env | session turns, thinking families | resident | shrinks live seq; **changes KV contents** — hold fixed across arms |
| M8/M9 | Flow KV cache (legacy blob / resident band) | `flow_kv_cache` (False), `_max` (8) | **stateless only** | M8: swa_full on SWA; M9: resident | M8: N whole-context blobs; M9: ≤8 seqs. Ignored in batched |
| M10 | Session flow fork | `resident_session_flow_fork` (**True**) | session turn 0 | resident, pool only | **allocates the 8-seq band even if never used** — the dominant hidden cost |
| M11 | Session snapshots (hot) | `session_snapshot_max` (2), TTL 7200 | fan-out / ingest-once | resident + **pool only** (batched raises) | 2 seqs always; capacity-rejected, never evicted |
| M12 | Snapshot replay fallback | same knobs | same | resident inactive | full re-prefill on fork [M: 1849 tok · 14.5 s vs 21 · 0.5 s hot] |
| M13 | Reasoning head-swap | `reasoning_head_swap` (False) | 3 shapes (stateless / turn-0 / mid-session splice) | resident or batched; family levels | +k seqs [M: 0.62 ms/swap] |
| M14 | Batched engine | `decode_mode: batched` | all, concurrently | resident+swa_full+kv_unified validated; non-hybrid; eager | n_seq_max = W+P+R; shared pool; admission + pressure ladder |
| M15 | Windowing `_window_resident_seq` | none (`_WINDOW_GEN_RESERVE`=16384) | resident sessions | resident | free; **silently forgets old turns**; forbidden on snapshot-linked (hard error) |
| M16 | Context refresh | `context_refresh_*` (75 req / 1800 s) | — | — | **the cache destroyer**: wipes flow band, demotes hot snapshots, kills live sessions; `_heal_instance` on decode −3 is the same wipe |
| M17 | Speculative n-gram | `speculative` (False) | decode | not batched | forces `logits_all` → inflates blobs; 23–33% slower on Metal MoE [M] |
| M18 | Hybrid checkpoint cache | not exposed | hybrids | — | per-instance |
| M19 | Non-KV process caches | — | — | — | tokenizer/session-transition/remote-adapter |
| M20 | Budget gates | `n_ctx`, `kv_preflight_gb`, `probe_verified_*` | — | — | preflight refusal; `stream_context_limit` (the P1 subject) |

Pool-path wart [M]: completion static-skip uses the **default persona's** blob
length (`:3715-3723`), so a resident multi-persona pool can mis-skip on
non-default slots. Batched uses per-seat `static_len` correctly.

---

## 3. Composability matrix

> **This matrix is mechanism × mechanism. For the operational view — which
> features a given MODEL can run, what each buys, and what it costs — see
> `dev/caching/FEATURE_MATRIX.md` (2026-07-30), which also corrects the batched
> column below.**
>
> **CORRECTED 2026-07-30:** the `batched` row/column said flow_kv was `⊗ ignored`
> and snapshots `⊗ raises`. Both shipped that day — the flow band (BUILD at
> retire, HIT via the control inbox) and the snapshot port (capture + hot fork).
> **Cold snapshot rebuild remains the batched boundary and still raises.**

Legend: ✔ composable · ⊗ mutually exclusive · ~ caveat · ↑ requires.

|  | resident | full_replay | flow_kv | flow_fork | snapshots(hot) | reasoning swap | batched | windowing | refresh |
|---|---|---|---|---|---|---|---|---|---|
| **resident** | — | ⊗ (takes precedence) | picks M9 | ↑ req | ↑ req | ↑ req | ↑ req active | ↑ req | ~ wiped |
| **full_replay** | ⊗ | — | ✔ completions | ⊗ | ⊗ → M12 replay | ⊗ | ⊗ | ⊗ (raises instead) | ✔ history survives |
| **flow_kv** | M9 | ✔ | — | ~ shares band+LRU | ✔ bands disjoint | **⊗ for completions** | ✔ own band | ✔ | wiped |
| **flow_fork** | ↑ | ⊗ | ~ shares band | — | ✔ | **⊗ at turn 0 only** | ⊗ forced off | ~ sets n_keep | wiped |
| **snapshots** | ↑ hot | M12 only | ✔ | ✔ | — | ✔ | ~ hot ✔, cold ⊗ | **⊗ hard (Overflow)** | hot→cold |
| **reasoning swap** | ↑ | ⊗ | ⊗ compl. | ⊗ t0 | ✔ | — | ✔ own impl | ~ head preserved | re-pinned |
| **batched** | ↑ | ⊗ | ✔ own band | ⊗ forced off | ✔ hot; ⊗ cold rebuild | ✔ own impl | — | ✔ per-seat | ✔ needs drain |
| **speculative** | ✔ | ✔ | ~ inflates blob | ✔ | ✔ | ✔ | **⊗ validated** | ✔ | ✔ |

**True free variables:** `swa_full`, `kv_unified`, `resident_seq_cache`,
`resident_strip_reasoning`, `resident_session_flow_fork`, `flow_kv_cache(_max)`,
`session_snapshot_max/_ttl_s`, `reasoning_head_swap`, `decode_mode`,
`max_concurrent_requests`, `jit_concurrency_limit`, `context_refresh_*`,
`speculative*`, `n_ctx`/`model_max_context`, `thinking_mode`, `LLMVP_THINK_STRIP`.

**Not free:**
- `session_full_replay` — only `true` loads (validator). The legacy path is not
  a configurable arm; studying it requires bypassing validation.
- resident vs full_replay is **config AND `memory_can_shift()`**, decided at
  load — read `health.sessionStrategy/sessionCanShift/residentRequested`,
  never the config.
- Windowing is knobless; disabling it = linking a snapshot = converting it to a
  hard error.
- The flow band is not independent of `resident_session_flow_fork` (default
  True allocates it even with `flow_kv_cache: false`).
- `resident_strip_reasoning` differs per path (legacy: default-on; replay:
  no-op; resident: opt-in) — not a clean single factor.

---

## 4. Results ledger

### 4.1 The two compat matrices

**2026-07-02** (`CACHE_STATE.md:280-286`) — 3-turn probe, ~1.5k doc [M]:

| model | arch | gate | turn-2/needle fresh | fork |
|---|---|---|---|---|
| gpt-oss-120b-a5 | SWA MoE | ✅ | 12 / 21 | 21 · 0.5 s hot |
| gemma-4-31b | SWA dense | ✅ | 37 / 46 | 46 · 1.2 s hot |
| devstral-2-small-24b | dense | ✅ | 8 / 16 | 16 · 0.3 s hot |
| qwen3-next-coder-80b-a3 | hybrid DeltaNet | ✅ | 12 / 21 | 21 · 1.5 s hot |
| qwen3.6-27b | pure recurrent | ❌ forced off | ~1.7k/turn | 1849 · 14.5 s cold |

**2026-07-29** (`dev/bakeoff_results/cache_matrix.jsonl`) — depth-12, strategy
triple recorded, flags as-configured [M]:

| model | swa_full | strategy/can_shift | t1→t2→t12 fresh | Σ prefill | fork |
|---|---|---|---|---|---|
| gpt-oss-a5 (control) | true | resident/T | 1415→12→21 | **2.7 s** | 21 · 0.7 s hot |
| glm-4.7-flash | false | resident/T | 1474→9→18 | **3.3 s** | 18 · 2.3 s hot |
| mistral-medium-3.5 | false | resident/T | 1588→8→16 | 50.9 s (39.1 = turn 1) | 16 · 1.9 s hot |
| laguna-xs-2.1 | false | full_replay/F | 1535→1551→1720 | 23.0 s | 1748 cold |
| gemma-4-26b-a4b | false | full_replay/F | 1475→1494→1693 | 38.1 s | 1724 · 5.2 s cold |
| qwen3.5-122b-a10 | false | full_replay/F | 1533→1675→2114 | 68.7 s | 2226 · 10.9 s cold |
| **hy3-reap-200b** | false | resident/T | **DECODE FAILED** — n_ctx_seq 2,730 < static 1,793 | — | — |

Read: resident ≈ **15–19× cheaper** wall prefill at depth 12, flat 8–12
tok/turn vs +16–43 tok/turn growth. Needles passed on every completed row, both
modes. The three `false` rows are **config verdicts, not architecture verdicts**
(swa_full was off — F7).

### 4.2 The full_replay cost at real magnitudes (hy3 arm, 2026-07-29) [M]

Per-turn: prompt 1591→9929 tok over 10 turns, eval **8.1 → 58.8 s** — the 10th
command cost 58.8 s of prefill for 20 tokens. Worst session: 10 turns, 586 s,
**94.4% prefill** (553 s prefill / 33 s decode), concluded a non-bug. Aggregate:
prefill **60.8% of 3,486 s run wall**; **93% of all prefill was re-reading
tokens the server already had**; goal throughput fell ~5× between half-hours.
Window headroom: peak `total_ctx` 16,066 of 32,768 (49%); max generation at
prompt >10k = 413 tokens — deep prompts and long generations never coincide.

### 4.3 The resident wins (CACHE_STATE receipts) [M]

Session re-prefill eliminated: step37 legacy replay of 11–17k histories at
33–54 s/turn → flat ~0.12 s (gpt-oss). Real-workload A/B: session-turn wall
**14.2→7.4 s mean (−48%)**, 13.0→4.5 s median (−65%). terminal-bench 3/8 →
5/8. Session flow-fork turn-0 TTFT 5.4 s → 0.26 s (~21×). Snapshot 30k-doc:
fork prefill 24 vs 30,060 tokens. 430-round interrogation at 200k live cells:
turn-N prefill **0.0 s**, recall 215/215, 1.1 s median round-trips.

### 4.4 The state_exp A/B (2026-06-13) [M] — read in scope

save/load vs full_replay, ~2 h capped arms: wall −2.8%; session-infer wall mean
+14.4%; prefill mean +2% / **P90 +44%**; overflow 1→0. Conclusion "wall-clock
neutral… tail-concentrated; a deeper-session workload would widen the gap" —
**the caveat was the prediction**: agent PTY charters (mean depth 5.3) made the
P90 tail the common case (RECONCILED, not contradicted, by 4.2).

### 4.5 Swarm-performance capacity model (2026-07-26) [M]

Prefill serialization ≈ 1.0 at every width/size (pre-registered 1.5–2×
**refuted**); slightly negative at 16k/48k. Decode parallelizes 5.41× (50.3 →
271.7 tok/s, N=1→128) **but by depth**: batching gain 2.44× @2.2k → **0.73× @
21.7k — concurrency becomes a loss**. Static prefix **w = 0.06** by
intervention (64 cells; removing 1737 static tokens helps less as N grows — a
per-decode-step cost, not per-stream). Real mixed workload ≈ 104.7 tok/s
effective, 87% decode. Interior optimum: N*≈24 at depth ~4.7k, N*=4 by ~12k.
Heterogeneous workloads UNCHARACTERIZED — the one ragged datapoint is
over-predicted +32.5%. Redundant re-reads: **1.03M of 1.54M prompt tokens
(67%)** on the counterfactual corpus. Pool ceiling: wired limit **113 GB**
(not 116); marginal 71.7 KB/tok vs 72.0 predicted (gpt-oss + swa_full only —
F10).

### 4.6 The road not taken [M]

mistral.rs automatic prefix cache: warm 50–80× cheaper (0.36 s vs 21.27 s on a
3.5k prefix, hitrate log 0→55.6%) — and **incremental prefill ~13 tok/s on
Metal (~200× slower than cold batched)** + idle weight compression → NO-GO.
The conclusion that produced the resident-seq path: use llama.cpp's native
seq primitives instead of a second backend.

### 4.7 Register snapshots from real runs [M]

| run | prefix_reuse_rate | prefill share | decode tps | note |
|---|---|---|---|---|
| mistral boss2 | 0.2997 | **82.0%** | 6.5 | dense = prefill-bound |
| step37 boss | 0.2487 | 28.6% | 34.2 | |
| glm tier arm | 0.262 | 26.3% | 31.8 | decode-dominated counterweight to hy3 |
| hy3 tier arm | "33.4" (mislabeled — F5) | 60.8% | 19.6–25.7 | the full_replay baseline arm |

---

## 5. Contradiction ledger

### Doc-level (12) — resolutions

1. **"Resident safe in ANY config; worst case no change" — FALSIFIED.**
   (`CACHE_STATE.md:12,289` vs hy3 sweep row.) → Demoted to: *safe where
   `n_ctx/n_seq_max` clears the static prefix + working margin, or
   `kv_unified: true`*. hy3 is dense, non-recurrent, gated true — and failed.
2. **"swa_full required for either strategy" — over-broad.** True scope is SWA
   models (corruption at window boundary). glm (MLA) and mistral-medium (dense)
   went resident-live without it. RESOLVED by scoping.
3. **Sweep predictions vs results.** "hy3 = one-line change" retracted (needs
   band suppression or kv_unified too); gemma-26b/laguna-xs sibling predictions
   answered a narrower question than asked (flags off). RESOLVED in-file +
   here.
4. **step37 "expected identical to gpt-oss"** vs measured can_shift=False
   (step35 arch). RESOLVED: the guess was wrong; the probe-before-trusting rule
   was right.
5. **"flow_kv re-enabled 2026-06-16"** config comments vs `false` values.
   RESOLVED: the re-enable regressed under game_challenge and was reverted
   (`OPEN_TASKS:540`); comments are stale — stop-citing register.
6. **swarm-524k config: "655360 not viable (109.9 GB)" header vs n_ctx 744448
   body.** RESOLVED: the header ladder measured *wired* against the 113 GB
   limit; the 2026-07-29 raise is `probe_verified_n_ctx` — measured to load and
   decode — and *resident-at-boot ≠ wired* (weights wire on decode). Both true;
   the header should say so.
7. **"~90 tok/s saturation"** vs the 1→128 ladder. RETRACTED in-file; the
   pre-correction table still reads as current if quoted — stop-citing.
8. **"~200 @ N=64 is an artifact"** vs measured 202.5. RETRACTED in-file;
   lesson kept: an undocumented measurement ≠ a false one.
9. **full_replay "wall-clock-neutral" vs "60.8% of wall."** RECONCILED by
   comparator and depth (§4.4). The corpus's model case of a conclusion read
   outside its workload.
10. **"Static is free" (AGENT.md) vs w=0.06.** RESOLVED: free on prefill (a
    hit), ~6% of a private token on decode; AGENT.md wording + mechanism both
    stale — stop-citing.
11. **Snapshot tier "production machinery" vs hard-error under batched.**
    OPEN as a capability gap: production runs batched, so the snapshot tier is
    **unavailable in the production shape**. EXPERIMENT Block E smokes it.
12. **Provenance:** decision-relevant evidence untracked vs CLOSED doc
    committed. RESOLVED with this corpus (files committed, pointers added).

### Measurement flags (F1–F12) — same quantity, different answers

- **F1** `prefill_grid.json` on disk carries the RETRACTED buggy
  `serialization_x` (0.52–0.88) that FINDINGS corrected to ≈1.0; a re-fit from
  the JSON reproduces the retraction. Also `prefillMs` reads 0 at ≤1k prompts →
  per-request rates there are garbage. **Fixed by P2.**
- **F2/F3** Three incompatible decode-vs-N curves (N=1 spans 50.3–60.6, ±20%)
  across n_ctx/build/depth confounds; FINDINGS §7 pools cells across the
  393k/524k boundary in one regression; "~200 @ 64" is three measurements.
  → Never pool across config boundaries; re-run instead.
- **F4** The regen hold-out depth has three values (566 → 2375 → ≈675); any §5–7
  depth is TOTAL and overstates effective depth by ~1700.
- **F5** **"Cache hit rate" names three quantities.** Trace `hit_rate` =
  fraction of calls flagged hit — saturates at 1.00, uninformative.
  `prefix_reuse_rate` = cached/(cached+fresh) tokens — **the** cache quantity
  (0.25–0.30 band). hy3's `cache_hit_pct: 33.4` is almost certainly
  prefix_reuse mislabeled. SWE's famous **0.72–0.90 names no register** —
  treat as un-anchored until provenance is recovered. → §6 vocabulary.
- **F6** "Turn-3 fresh" means different turns across the two matrices (old
  3-turn needle vs new depth-12 turn 3). Compare turn-2 and the needle turn
  only.
- **F7** The three 2026-07-29 `can_shift=false` rows are config verdicts, not
  architecture verdicts (swa_full off by design). → Block C.
- **F8** gemma-31b (resident-live, flags on) vs gemma-26b (full_replay, flags
  off): different weights AND flags — unattributable. → Block C isolates.
- **F9** Single-stream prefill throughput has three bands (590–794 / 676–963 /
  1.2–1.4k tok/s) with the clean-vs-residue crossover unmeasured.
- **F10** Three models of the wired limit (113 measured / 116 assumed / probe's
  0.871–0.879-of-physical constant); the "exact" KV formula is exact only with
  swa_full on (else 2–22.59× over, per-model measured).
- **F11** hy3's 60.8% is a single-arm baseline with three declared handicaps —
  an arm figure, not a model constant.
- **F12** OPEN_TASKS carries an UNMEASURED inference that reads as a result
  (pool-fit gate over-counting by N×1809; the skipped N=64/8192 cell "would
  fit"). → Block E, one cell.

---

## 6. Vocabulary standard (mandatory for all future cache records)

| term | definition | use |
|---|---|---|
| **`prefix_reuse_rate`** | `cached_prefix / (cached_prefix + fresh_prefill)` tokens | **THE cache quantity.** Never call it "hit rate" |
| `cache.hit_rate` | fraction of calls flagged `cache_hit` | near-saturated (1.00 in every inspected run); report only with its definition |
| `freshPrefillTokens` / `cachedPrefixTokens` | per-request server registers | the per-turn curve is the strategy discriminator |
| `prefillMs` / `decodeMs` | server-measured phase times | `wall − prefill − decode` = queue/network; prefillMs=0 below ~1k prompts |
| `io_ratio.fresh` vs `.context` | fresh/generated vs (cached+fresh)/generated | "the gap is the cache payoff" |
| `rates.prefill_tps` | fresh_prefill / prefill_s | deliberately excludes cached tokens — true, not cache-flattered |
| strategy triple | `health.sessionStrategy/sessionCanShift/residentRequested` | the ONLY authoritative statement of the running strategy |
| `n_ctx_seq` | true per-seq window | report beside n_ctx whenever `kv_unified: false` |

Fold-rule caveat: calls with no real token registers fall to the whitespace
bucket and **silently leave the cache denominator** — a server that stops
reporting shrinks the sample, it does not error.

---

## 7. Stop-citing register

| stale item | where it lives | cite instead |
|---|---|---|
| `serialization_x` 0.52–0.88 | `prefill_grid.json` on disk | FINDINGS §4 (≈1.0) — see P2 annotation |
| "~90 tok/s saturation", §1 table | `serving_perf_reference.md` pre-correction block | the 1→128 ladder |
| "flow_kv_cache re-enabled 2026-06-16" | gpt-oss config headers | reverted; `OPEN_TASKS:540` |
| "universal context effectively free (zero marginal cost)" + save_state framing | `AGENT.md:287-297` | w=0.06, M2b/M2c mechanics |
| "resident safe in ANY config / worst case no change" | `CACHE_STATE.md:12,289` | §5.1 demotion |
| "hit rates 0.72–0.90" as a cache register | `SWE_PHASE_A_FINDINGS.md:78` | un-anchored (F5) until provenance recovered |
| "hy3 can go flat with a one-line change" | `CACHE_SWEEP_PLAN.md:141` | Block D recipe (bands suppressed or kv_unified) |
| the 2-seq "half the window" hy3 arithmetic | `CACHE_SWEEP_PLAN.md:251-272` | /12 with stock defaults; /2 only with bands suppressed |

---

## 8. Gap list → EXPERIMENT.md

1. **Payout × band-layout at real turn sizes** — the probe's 5-tok filler
   understates real growth ~56×; nothing measures resident vs replay at ~900
   tok/turn, nor n_seq_max=2 vs 12 vs kv_unified. → Block B.
2. **The swa_full price, jointly with the resident payout** (gemma-26b: 21.7
   KB/tok without vs ~880 KB/tok class with — never measured together with the
   session win). → Block C.
3. **The corrected hy3 recipe** (the tier arm's declared A/B counterpart).
   → Block D.
4. **Windowing correctness at the TRUE per-seq boundary** (needle past window
   under n_seq_max>1). → P1 + Block A.
5. **Snapshot tier under batched** (the production-shape gap). → Block E.
6. **F12 pool-fit over-count** — one cell. → Block E.
7. **Stateless reuse gap (11b)** — flow-band v2 pilot at realistic head sizes.
   → Block E.
8. **Fleet write-back** — `probe_verified_cache` blocks per servable model.
   → Block F.
9. Un-recovered: the SWE 0.72–0.90 register (needs the original run JSONs);
   heterogeneous-batch characterization (out of scope here; FINDINGS §9).
