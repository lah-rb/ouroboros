# Multi-Model LLMVP: Config Hotswap, Co-Residency, Provider Gateway

*Design plan, 2026-07-16. Status: APPROVED DIRECTION, phases pending.
The gating feature set for the LLM boss layer: swap model configs over
GraphQL, run multiple models (load/offload/route), and treat remote
models (claude -p) as registry entries so the boss layer names a brain
without knowing where it lives.*

## Motivation

1. **Boss layer** (τ piece 4 grow-step and beyond): the boss should be a
   *different, more expensive* model than the operator. Today LLMVP
   serves exactly one config per process; a second model means a second
   machine-state we don't have.
2. **Operational toil**: every benchmark constraint ends with "restore
   the production a5 config and restart." `swapModel` between configs of
   the same weights (a5 ↔ gameab ↔ tau) kills that entire class of
   restart ceremony.
3. **Provider abstraction**: once model = a named registry entry, a
   `claude -p` shim is one adapter class — the boss layer's frontier
   upgrade becomes a config edit.

## Ground truth (audited 2026-07-16)

Singletons that assume ONE model per process:

| # | Singleton | Where | Swap impact |
|---|-----------|-------|-------------|
| 1 | `_backend_instance` | `inference/backends/factory.py:24` | The chokepoint. Every request path funnels through `core/inference._get_backend()` (core/inference.py:208). `shutdown_backend_async()` already exists and frees engine → shared contexts → primary model last. |
| 2 | Config global | `core/config.py` `get_config()`/`set_config()` | 14 modules read it. Almost all reads are **call-time** (safe under swap-the-global). Two exceptions below. |
| 3 | **Module-level capture** | `core/inference.py:29` (`config = get_config()` at import) | STALE after swap — `resolve_max_tokens`/`resolve_temperature` would use the old config forever. Pre-req fix. |
| 4 | **Tokenizer cache** | `inference/tokenizer.py:17` (`_tokenizer_cache` global) | Model-bound; stale after swap. Pre-req fix (invalidate on swap or key by model path). |
| 5 | Session manager | `api/graphql_api.py:281` | Built per-backend (`SessionManager(backend)` at :860); init/shutdown helpers already exist (:855, :889). Reusable as-is for Phase 1. |
| 6 | Static-tokens manager | `preprocessing/static_tokens.py:110` (`manager` singleton) | Keyed by PERSONA, not model — two models sharing a persona name would collide, and the builder path reads global config + the global tokenizer cache. Phase 1: reset on swap. Phase 2: key by (model, persona). |

Clean by audit: `llama_cpp_backend.py` and `batched_engine.py` contain
ZERO `get_config()` calls — config is injected at construction. **Phase 1
requires no surgery on the backend file**, which honors the OPEN_TASKS
item-8 soak constraint (four surgeries in one week; leave it alone).

Machinery we already own that swap reuses:

- **Drain lifecycle** (commit 8073353): `_drain_for_refresh` = admission
  gate close → finish window → force-expire sessions
  (`expire_all_sessions`) → evict streams (`evict_all_streams`) →
  rebuild. A model swap is this lifecycle with "rebuild context"
  replaced by "teardown backend, construct new one." NOTE: exactly one
  live drain firing so far and it died (since hardened); Phase 1 wants
  ≥2 clean observed drain cycles first (OPEN_TASKS item 2 feeds this).
- **Client resilience**: Ouroboros retry loops demonstrably ride through
  full server restarts (the SIGSTOP/SIGTERM protocol). A swap window is
  strictly gentler.
- **Page-cache reload**: after unwire, the GGUF lingers as file-backed
  cache (memory-accounting findings) — swap-back to a recently-resident
  model skips disk.

Memory physics (M1 Ultra 128GB, `iogpu.wired_limit_mb=116000`):
gpt-oss-120b F16 wires ~73–75GB hot → ~40GB headroom for a co-resident
model. Two big models hot is impossible; big-boss = swap or remote.

## Concurrency evidence (and the gap)

- **Known dead end (in-process, one model)**: multi-context simultaneous
  decode → shared MTLCommandQueue + unwired-weights command-buffer race →
  negative scaling + errors. Closed; batched single-context engine is
  the production answer for one model.
- **Known good (cross-process)**: LMStudio small models decode alongside
  LLMVP routinely (Luke's workflow). Separate processes = separate Metal
  command queues; the OS arbitrates.
- **THE GAP**: two *different* models, each single-context, in ONE
  process, decoding simultaneously. Between the two regimes — each
  llama_context gets its own command queue, and fully-wired weights may
  avoid the CB race, but this is exactly the kind of thing we've been
  burned assuming. Probe P0.b settles it empirically before Phase 2
  commits to a policy.

## Phase 0 — Probes (RUN 2026-07-16; scripts are keepers in `llmvp/dev/`)

- **P0.a — mid-process teardown/reinit: PASS**
  (`llmvp/dev/probe_p0a_swap_teardown.py`; Olmo-32B-Q6 cold → Devstral-24B
  cold → Olmo warm, one process, wired sampled via vm_stat).
  Teardown released wired EXACTLY every cycle (79.1 → 107.2 → 79.0 GB);
  all decodes coherent; greedy output byte-identical cold vs warm.
  Load times: **10.7s cold, 3.9s page-cache-warm** (25GB GGUF) — weight
  load is NOT the swap bottleneck; swap latency will be dominated by
  static-prefix prefill + warm-up, not I/O. Phase 1's foundation holds;
  swap-by-respawn fallback not needed.
  **Bonus findings**: (a) gpt-oss (idle, 79GB wired) + Olmo (28GB)
  co-resided at 107GB and decoded fine — co-residency is real;
  (b) CORRECTION to the idle-eviction assumption: gpt-oss did NOT
  idle-unwire in 15+ quiet minutes. The bimodal "idle = ~3GB wired"
  state is longer-timescale/pressure-driven — the Phase 2 memory
  governor must count a hot model as fully wired until explicitly
  unloaded, never assume idle shrinkage.
- **P0.b — in-process dual-model concurrent decode: DIRTY → LOCK**
  (`llmvp/dev/probe_p0b_dual_decode.py`; Olmo + Devstral, greedy, 6
  questions × solo-consistency/concurrent/alternated, byte-compared).
  No crashes, no Metal errors — but ONE greedy divergence (q2, Olmo
  concurrent vs its self-consistent solo output). And the throughput
  case for concurrency is EMPTY: concurrent wall ≈ solo-SUM on 5/6
  questions (Metal serializes the two models' kernels; only q0 showed
  ~22% gain). **Phase 2 policy settled: global cross-backend decode
  lock (strict alternation) — costs ~nothing, removes the hazard.**
  The `concurrent_decode_ok` allowlist idea is dead. True parallelism
  = cross-process (the proven LMStudio regime), reachable via the
  Phase 3 `openai_compat` adapter when needed.
  Loose end: final wired read 17GB immediately after both teardowns
  (P0.b samples without P0.a's 2s Metal-settle sleep) — presumed
  sampling timing, worth a settle-and-resample if it recurs.
- **P0.c — drain soak** (still in flight): ≥2 clean
  `proactive-timed-drain` cycles on the production server, traceback
  from the 18:12 death diagnosed (OPEN_TASKS item 2a). Gate for Phase 1
  merge, not for Phase 1 development.

First boss-candidate config landed: `llmvp/configs/olmo-3.1-32b-think.yaml`
(chatml + thinking, template verified against the GGUF's embedded one,
n_ctx 65536 = train max). At ~28GB wired it fits alongside gpt-oss
(~107GB total) — a Phase 2 co-resident boss under the decode lock.

## Phase 1 — `swapModel`: single-resident hotswap — **SHIPPED 2026-07-16**

Live smoke on the production server (boss arms SIGSTOPped for the
foreign-model window): `gameab → olmo-3.1-32b-think` in **16.1s**
(teardown 0.3s, load 12.8s incl. first-ever static-token auto-build for
the chatml persona), Olmo answered through the full production path with
`<think>` correctly FSM-stripped (proves tokenizer/metadata/fsm-family/
renderer all followed the swap), then `olmo → gameab` in **11.0s** — the
65GB gpt-oss reloaded page-cache-warm in 10.8s. Pointer file tracked both
swaps; arms resumed on gpt-oss afterwards. Crossed decode modes both ways
(batched ↔ pool). CLI: `ouroboros.py llmvp models` / `llmvp swap <name>`.
16 unit tests pin the lifecycle (raise-vs-report contract, gate on every
exit path, rollback, drain force-clear, stale-capture regressions).
Implementation notes vs the sketch below: the swap re-runs
`initialize_server_async` wholesale (swap ≡ restart, no drift), and two
MORE stale globals were found and fixed beyond the two predicted
(fsm-family cache, GGUF metadata singleton; `ActiveConfigView` in
core/config.py now gives modules a live config name). Original design
sketch follows.

Semantics: exactly one model resident, swap = drain + teardown + reinit
of the SAME singletons. No registry-of-live-backends yet — the registry
is a *catalog*.

- `llmvp/core/model_registry.py` (NEW): scan `configs/*.yaml`
  (excluding `archive/`), entries `{name, path, config (lazy-loaded/
  validated), model_path, gguf_size_bytes}`. Active = whatever the
  backend was built from.
- **Pre-req fixes** (small, land first, independently green):
  - `core/inference.py:29` module-level `config = get_config()` → call
    time reads (mechanical; regression test: swap config global, assert
    resolvers see new values).
  - `inference/tokenizer.py` `_tokenizer_cache` → invalidated by swap or
    keyed by `config.model.model_path`.
  - Audit sweep for any other import-time captures (none found in
    production paths as of writing; pin with a test that greps? no —
    pin behaviorally via the swap integration test).
- **GraphQL** (`api/graphql_api.py`):
  - `models` query: `[{name, active, family, modelPath, ggufSizeGB}]`.
  - `swapModel(name: String!, drainS: Float)` mutation:
    1. Registry validates name + loads/validates the target Config
       (fail fast, no service interruption on bad configs).
    2. Same-config no-op guard (idempotent success).
    3. Server-level swap gate closes (new: `_swap_state` in the API
       layer or a `backend.begin_teardown_drain()` generalization of
       `_drain_for_refresh` — decide at impl; the drain phases are
       identical, only the post-drain action differs).
    4. Drain window (default = `context_refresh_drain_s`), then
       force-expire sessions + evict streams (existing machinery).
    5. `shutdown_session_manager` (graphql_api:889) →
       `shutdown_backend_async()` → `set_config(new)` + tokenizer-cache
       invalidation → `initialize_backend_async(new)` → re-init session
       manager (graphql_api:855).
    6. Update `active_config.txt` pointer so a later process restart
       agrees with reality.
    7. Return `{ok, name, drainedSessions, evictedStreams, teardownMs,
       loadMs, totalMs}`.
  - Requests arriving mid-swap: fail with a **retriable** GraphQL error
    ("model swap in progress, retry in ~Ns") — Ouroboros retry loops
    already ride through full restarts, so no client change required.
    (Queueing across a multi-minute weight load holds HTTP connections
    open for nothing; explicit retriable error is honest and simpler.)
- **Failure containment**: if the NEW backend fails to initialize,
  attempt rollback to the previous config; if that also fails, the
  server parks in a "no backend" state where health reports it plainly
  (existing `_get_backend()` lazy-init path already tolerates
  not-yet-initialized). Never leave the gate closed on an exit path
  (the drain-refresh finally-pattern applies).
- **Tests** (llmvp venv): registry scan/validation; swap happy path with
  a stub backend (no weights); mid-swap request rejection is retriable;
  pointer-file update; stale-capture regressions (config resolvers +
  tokenizer post-swap); rollback on failed init.
- **CLI convenience** (Ouroboros side, small): `ouroboros.py llmvp
  models` / `llmvp swap <name>` wrapping the query/mutation — replaces
  the restart scripts for config changes.
- **Definition of done**: a5 → gameab → a5 live swap with a mission
  running (mission retries through both windows, completes normally);
  both suites + fence green; a benchmark run switched configs with zero
  process restarts.

## Phase 2 — Co-residency: N hot backends + routing

Gated on P0.a/P0.b verdicts. Semantics: registry holds LIVE entries
`{name → (backend, session_manager, config)}`; one is "primary"
(default route).

- **De-globalize the request path** (the real refactor): the modules
  whose `get_config()` reads are per-MODEL (not app-level) get config
  from their backend instead. Audited surface: `session_manager.py`
  (5 sites — SessionManager already holds `backend`, so
  `self._backend.config`), `formats/renderer.py:241`,
  `inference/tokenizer.py` (per-model instance keyed by model path),
  `preprocessing/static_tokens.py:45`, `inference/metadata.py:162`,
  `core/inference.py` resolvers (take config from the resolved
  backend). App-level reads (logging, resources, API security) stay
  global.
- **Routing**: optional `model: String` on `CompletionRequest`,
  `SessionConfig`, and the raw/chat/tool paths; default = primary.
  Session ops route by session id → owning manager (registry keeps
  `session_id → model` map; ids stay globally unique).
- **Memory governor**: `loadModel(name)` admission = Σ(resident wired
  estimates) + estimate(new) ≤ 116000MB − headroom(~8GB). Estimate =
  GGUF file size + KV budget (n_ctx × layers heuristic, calibrated in
  P0.a). Over budget → explicit refusal listing what to offload;
  `unloadModel(name)` = the Phase-1 teardown path scoped to one entry.
  No silent LRU eviction v1 — the operator (or boss layer) decides.
- **Decode policy** (from P0.b): either per-config
  `concurrent_decode_ok: bool` (small models decode freely, anything
  large is exclusive) or a global cross-backend asyncio decode lock.
  Big models are ALWAYS exclusive regardless of verdict.
- **Telemetry**: per-model token/latency spans; `models` query grows
  `{state: hot|cold, wiredGB, sessions, lastUsed}`.
- **Definition of done**: gpt-oss-120b (primary) + a small boss model
  co-resident; a τ-style episode where boss turns hit the small model
  and operator missions hit gpt-oss, interleaved, no errors, no wired
  creep after both offload.

## Phase 3 — Provider adapters: remote models as registry entries

The registry entry grows a `provider` discriminator:
`local_llama` (default) | `claude_cli` | `openai_compat`.

- **Contract**: remote entries implement chat/completion only. NO
  sessions, NO personas-as-tokens_bin, NO reasoning head-swap —
  `start_session` on a remote model returns a clear error. The boss
  layer holds conversation history anyway; `PersonaSession` grows a
  stateless mode that resends history each turn (persona = system
  prompt text rather than a prefill bin).
- **`claude_cli` adapter**: subprocess `claude -p --output-format json`
  with timeout + the standard error taxonomy mapping; token/cost
  accounting lifted from CLI output into the normal trace fields so
  mission token totals stay comparable across arms. Auth is the CLI's
  problem (no key handling in LLMVP).
- **`openai_compat` adapter**: generic base-URL client. Free bonus: an
  LMStudio-hosted local boss becomes a registry entry too — which
  sidesteps in-process concurrency entirely (cross-process is the
  proven-good regime). If P0.b comes back dirty, THIS is the co-resident
  small-boss path and Phase 2's decode lock matters less.
- Config shape: `llmvp/configs/boss-claude.yaml` with
  `provider: claude_cli`, `model: claude-opus-4-8`, generation defaults
  — same catalog, same `models` query, same routing.
- **Definition of done**: a boss turn served by `claude -p` through the
  SAME GraphQL surface a local boss would use, with tokens in the trace.

## Phase 4 — Ouroboros interface

- `InferenceEffect`: optional `model` (constructor default + per-call
  override), rides the GraphQL variables. `MissionYAMLConfig`: optional
  `llmvp_model`. Boss layer (`agent/chat`): `PersonaSession(model=...)`.
- Flow-level per-step model routing: explicitly OUT of scope (the boss
  layer selects at session level; per-step routing is a future
  adaptive-router-style experiment, noted here so nobody builds it by
  accident).
- **Definition of done**: τ episode config names boss model + operator
  model declaratively; swapping the boss from local to claude is a YAML
  edit.

## Risks

| Risk | Mitigation |
|------|-----------|
| Metal doesn't release wired memory on mid-process close | P0.a before any Phase 1 investment; fallback = swap-by-respawn preserves the feature |
| In-process dual decode hits a CB race variant | P0.b verdict → decode lock; openai_compat/LMStudio path is the escape hatch for co-resident bosses |
| Drain machinery is 1-firing old (and that firing died) | P0.c gate: ≥2 clean cycles + the traceback diagnosed before swapModel merges |
| Stale import-time state beyond the two found | Behavioral swap integration test (swap → assert new family/temps/tokenizer everywhere) rather than trusting the audit |
| Failed init strands the server modelless | Rollback to prior config; health reports "no backend" honestly; gate never left closed |
| Session loss on swap surprises a running mission | By design + documented: drain gives the finish window, force-expiry is the same contract as refresh; client retry loops proven through full restarts |
| Scope creep into llama_cpp_backend.py before it soaks | Phase 1 forbids touching it; Phase 2's de-globalization touches config *consumers*, not the backend |

## Sequencing

P0.a + P0.b can start now (small models, dev server, production
untouched). P0.c rides the boss arms already running. Phase 1 dev can
proceed in parallel with probes (stub-backend tests don't need weights);
its MERGE gates on P0.a + P0.c. Phase 2 gates on P0.b. Phase 3 is
independent of Phase 2 (registry + provider field come from Phase 1)
and could land before it if the boss layer wants claude early — worth
considering, since Phase 3 is small and unlocks the frontier boss
immediately. Phase 4 is a thin client change any time after Phase 1.

Cross-refs: OPEN_TASKS item 2 (drain follow-through) is P0.c. OPEN_TASKS
item 8 (stream-prep extraction) remains soak-gated and is UNRELATED —
do not bundle. The τ growth step (operator tuning + frontier boss)
consumes Phase 3+4.
