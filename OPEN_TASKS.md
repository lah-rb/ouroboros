# Open Tasks — Delegation Brief

*Deferred work with enough context to pick up any item cold.*

**This is a LIVING doc.** When an item completes and needs no follow-up,
**delete it** — don't leave a "CLOSED" tombstone. When an item completes but
leaves residue, replace it with the residue only. Findings worth keeping past
the task belong in the memory files, `dev/` docs, or a config comment, not here.

## Standing constraints (read before touching anything)

- Explicit `git add <files>` only — never `-A`; commits end with the
  Co-Authored-By trailer of whoever/whatever authored them.
- NEVER bare `uv sync` (prunes out-of-band packages: terminal_bench,
  torch, mlx, the editable ~/Repos/tau-bench install). `uv pip install`
  or `uv sync --inexact`.
- llmvp tests run under llmvp's OWN venv: `cd llmvp && .venv/bin/python
  -m pytest tests/`. Main suite: `uv run pytest tests/`. (2026-07-25:
  1590 main / 330 llmvp collected — run them, don't trust the number.)
- Verification fence per change: both suites + `ouroboros.py lint-flows`
  (0 errors / 1 standing advisory) + `smoke` (44/44) + `cli-smoke`
  (19/19) + black/ruff clean. `flows/compiled.json` commits force all
  touched .cue sources into the same commit.
- **Config hygiene:** the last-served config may stay resident between runs —
  the work is varied enough that snapping back to a "production" default has
  no value. What matters is that the active config AND the mission parameters
  are verified correct *before* a run starts. Repoint only when the next run
  needs a different model.
- Test-writing rules: `TESTING.md`. Repo layout: `CONTRIBUTING.md`.
- Server restarts are cheap and pre-approved when a soured/wedged server
  is burning hours (SIGSTOP mission processes → SIGTERM server →
  relaunch → SIGCONT; the agent retry loops ride through).
- **Serving geometry is measured, not guessed.** KV per-token laws, the
  swa_full ceilings, and the preflight guard live in the config headers
  (`llmvp/configs/*.yaml`) and the `kv-geometry-before-swarm` memory. Compute
  before allocating — three reboots were precomputable.

---

## 1. SWE-bench: issue-guided retrieval (the big score lever)

Pilot verdict (memory: swe-bench-verified-pilot): 1/12, with 10/12
failures wall-bound on repo-scale localization — the agent burns its
budget finding WHERE to edit. Build issue-guided retrieval: use the
problem statement to rank/prefetch candidate files+symbols (repomap
PageRank + lexical/embedding match against the issue text) and seed the
diagnose/ingest path with them. Evidence: `runs/swe/` per-instance
traces; `dev/swe_taxonomy.py` classifies failures. Success = localization
time collapses on the pilot set; rerun the 12-instance pilot
(`dev/swe_eval.sh`, predictions archive in `dev/archive/swe_reports/`).

*Build + unit tests need no server; evaluation does.*

## 2. TRAP_BRIEF re-validation — ENV SUB-CASE FIXED 2026-07-27

`dev/TRAP_BRIEF.md` documents the deterministic-startup-fail
blind-diagnose trap. A live instance was root-caused and fixed on
2026-07-27 — see `dev/POOLSIDE_TRAP_ROOTCAUSE.md`. **The escalate-flow
wiring this item called for is DONE** (`e61fdc0`): a failed dependency
install now routes to `escalate` (bounded read/run/write) instead of
dead-ending, and its result is re-verified rather than trusted.

Seven fixes landed, 33 mutation-verified tests. The trigger was found:
`_uvize_install_commands` created a venv whenever a `py` SECTION existed
rather than when an install would run into it, so a model omitting
`install_command` got an EMPTY venv that reported success and then
shadowed a working system interpreter.

**Still worth doing, but narrower than originally scoped:** rerun one
canonical trap task (thompson-nfa-regex-engine or bplus-tree greenfield)
to check the OTHER sub-case. TRAP_BRIEF §7's A′ analysis — ground truth
present but IGNORED, static tracing mis-localising to the wrong package —
describes a *different* failure from the env one just fixed, and nothing
here addresses it. Localisation was correct on every cycle in the env
case; the remedy was simply inexpressible.

### 2b. NOT a trap sub-case — a self-reverting write. Found live 2026-07-27

Caught watching the poolside 2h run (`/tmp/tier/poolside-2h-v2`), goal
`1a7564ac`. `engine.py` calls `random.choice(exits)` with **no `import
random`** — a NameError that crashes `flee`. It was re-diagnosed all run.

**An earlier revision of this entry blamed symbol-splice granularity and
claimed ruff never runs. Both were wrong.** Every upstream stage worked:

- `ruff check --fix {file}` IS the configured lint tier and DOES run
- it reported `F821 Undefined name 'random'` with file:line
- that reached the prompt intact — `checks_failed: ['lint: engine.py']`
  plus the full finding in `terminal_output` (the `gate_output` threading
  added after the 2026-07-16 "rewrote engine.py 360×" loop)
- the model diagnosed it correctly and declared `kind: "module_fix"` with
  `module_statement: "import random"` — **five separate times**
- `check_module_fix` routed to `run_module_frame_edit` all five times
- `action_splice_frame` called `effects.write_file` and logged success

The frame editor was selected correctly and the import reached disk. It is
absent at the end of the run because **the pass reverts its own write.**

#### The revert

`read_target` loads the file into `context.target_file.content`.
`run_module_frame_edit` writes the frame-edited content to disk but
publishes only `files_changed` and `edit_summary` — the sub-flow's
`file_content_updated` is **not lifted back into file_ops' context**. When
`module_fix_symbol_continue` is true the flow continues to
`extract_symbols` → `run_patch`, and `run_patch` sources `file_content`
from `context.target_file.content` — the pre-frame-edit snapshot. The
symbol rewrite is spliced into stale content and `write_patched_file`
writes it back, erasing the import. Then F821 fires again. Forever.

Only the **paired** form self-reverts. A module fix with no body change
routes `success → lookup_env` and persists fine, which is why the path
looks healthy elsewhere. The `CONCLUDE_PROMPT` actively invites the paired
form and promises *"both edits are applied, module line first"* — the
second edit reverts the first.

#### FIXED 2026-07-27

`file_ops` gained `reread_after_module_fix` on that one arc: a frame edit
that continues into symbol routing re-reads the file first, so both halves
of a multi-part fix see the same bytes. Disk is the source of truth —
`effects.read_file` opens fresh with no cache and the frame edit's write is
the last thing to touch the file.

**The narrower fix would have been worse than the bug.** Publishing
`file_content_updated` and having `run_patch` prefer it leaves
`action_extract_symbol_bodies` deriving line/byte offsets from the stale
snapshot, so v0's offsets get spliced into v1's bytes — silent off-by-one
corruption instead of a clean revert. `test_stale_offsets_truncate_the_
symbol_in_the_new_file` demonstrates it concretely: the stale range ends a
line short and orphans the symbol's final statement. That is why the
refresh is of `target_file` itself and sits *before* `extract_symbols`.

12 tests in `tests/test_module_fix_symbol_continue_clobber.py`, both
mutations caught (direct edge restored; fixture import removed).

#### The separate, real gap: lint is advisory

`pipeline_actions.py:757` sets `"required": tier == "syntax"`, and the
batch recorder scores a file as passed unless a **required** check fails:

    "passed": not any(not c.get("passed") and c.get("required") for c in checks)

So a file with `F821 Undefined name` is recorded as **passing** its
structural gate and the goal closes. The defect resurfaces much later
through the functional sweep, where it costs a diagnose loop instead of a
one-line fix at write time. Luke's position (2026-07-27) is that ruff
should gate every structural goal, on the first edit as well as later ones
— it already *runs* there; what is missing is that failing it should mean
something.

Arm 3 of the quant chain (`dev/poolside_v3_followon.sh`) records `F821`
and `repeat_warn` in its OUTCOME file so this is measured next run.

## 3. TB2: the next measurement

Canary complete — **adaptive 2/8 (first-ever winning-avg-corewars pass) vs
flat 1/8**. The oracle fixes shipped and were retested: **chess-best-move and
mteb-retrieve were rerun on gpt-oss and did NOT flip**, which leans capability
ceiling rather than framework adjustment. No retake is owed on those two
unless new work changes the substrate (a stronger daily model, a retrieval
lever, a routing change that touches answer-profile tasks) — then they are the
canonical pair to re-probe.

Open decision: **full-89 single-arm adaptive run** against the 18.7% Terminus
baseline. Also still deferred: the context-budget arm (reduced `prepare_context`
budgets for fix-mode rewrites — the 26k-token rewrite prompts were the KV-
collision trigger; Luke wants this validated carefully, not assumed).

## 4. Retire the legacy save_state session path

**See `dev/caching/CORPUS.md` (2026-07-29) — the single reference for all cache
strategy state; this section's cost framing is folded into its §4/§5.**

**AMENDED 2026-07-29 — "safe" is not one bit, it is two.** The framing below
(either flag ⇒ fine) is correct about SAFETY and hides an order-of-magnitude
COST difference. Measured on the hy3 tier arm: full_replay re-prefills the whole
session every turn, so the 10th PTY command cost **58.8s of prefill for 20
tokens**, prefill reached **60.8% of run wall**, and goal throughput fell ~5×
between the first and second half hour. Resident is flat. Both are safe.

So this task's end state is three-valued, not two:

| landing | safe? | cost | verdict |
|---|---|---|---|
| legacy save_state | NO | — | delete (this task) |
| full_replay | yes | **O(n²) per session** | acceptable fallback only |
| resident | yes | flat | the target |

**9 of 19 configs are on full_replay today** (5 explicitly, 4 by omission —
`resident_seq_cache` defaults to False). See `dev/CACHE_SWEEP_PLAN.md` for the
fleet table, pre-registered per-model predictions, and the sweep that decides
which of the 9 can go flat. That sweep GATES further tier runs and re-runs.

**LANDED 2026-07-29:** `memory_can_shift()` is now asked at every load whether or
not resident was requested, and the effective strategy is logged in one line with
`n_ctx_seq` and an explicit "resident AVAILABLE but not enabled" callout. Before
this, the gate ran only when resident was REQUESTED, so a config with the flag
off produced no evidence either way — hy3 ran a full arm on the quadratic path
with its can_shift answer nonexistent. `info["session_strategy"]`,
`["session_can_shift"]`, `["resident_requested"]` expose it programmatically;
unknown is `None` and is never collapsed to False.
(`llmvp/tests/test_session_strategy_report.py`, 19 tests, 7 mutations bite.)

**ALSO LANDED 2026-07-29 — the third branch, enforced.** `Config` now REFUSES
`session_full_replay: false` at load, because architecture is unknowable from
config: a resident request is never a guarantee, so disarming the fallback leaves
a config one refused gate away from the retired path. Requesting resident
explicitly does NOT excuse it — that is precisely the case that needs a fallback.
Since resident ignores `session_full_replay` while active, keeping it true costs
nothing and arms the catch. The refusal names the fix. All 20 shipped configs load
unchanged (none set it false). `llmvp/tests/test_config_inheritance.py::
TestSessionStrategyValidation`, 5 tests, 3 mutations bite.

**POSITION 2026-07-30:** the fleet is now three-valued as this section asked:
11 configs resident (flat, measured ~8x cheaper at realistic session shape),
5 replay-by-ARCHITECTURE (both qwen3.6, qwen3.5 — measured refusals — plus
step37; correctly so), legacy unreachable via validated config. Remaining work
here is pure deletion: the save_state else-arm in session_turn AND the M8
legacy flow-blob branch (see 11b position — every flow-capable config is
resident, so both paths are dead code with a rap sheet).

**Still owed here:** deleting the save_state branch in
`core/session_manager.session_turn` (the `else` arm) plus its purge path. The
validator now makes that arm unreachable from any loadable config, so the deletion
is dead-code removal rather than a behaviour change — but it is not done, and the
code is still there.

Note also the prior A/B in `config.py`'s comment ("wall-clock-neutral, +44%
prefill at P90") is NOT contradicted by the hy3 numbers: it compared full_replay
to legacy save_state, not to resident, and its own caveat — cost concentrates in
deep-session tails — is exactly what agent PTY charters now hit as the common
case. See `dev/CACHE_SWEEP_PLAN.md` §reconciling.

Every served config now uses a SAFE session path (`session_full_replay: true`
or `resident_seq_cache: true`), so the legacy save_state/load_state per-turn
KV-surgery path is effectively dead in production. Its rap sheet: it is what
`session_full_replay` was introduced to bypass for qwen's degeneration; a
gpt-oss control hit `SystemError: Negative size passed to
PyBytes_FromStringAndSize` inside save_state at deep context; save_state churn
corrupts the static KV over a run (the flow_kv_cache finding); and a corrupted
saved state is RELOADED every subsequent turn, which uniquely explains "never
recovers".

**Retire it**: delete the save_state branch in `core/session_manager.session_turn`
(the `else` arm after resident/full_replay) plus its purge path, and make a safe
strategy mandatory at config validation so a new model cannot be onboarded onto
the dead path by omission.

**NEW WRINKLE (2026-07-25, must be handled):** step35-arch models report
`memory_can_shift=False`, so `resident_seq_cache: true` **silently falls back to
the legacy path** — `step37-flash-196b-a11.yaml` is in exactly that state today.
Validation therefore needs three branches, not two: resident (shiftable arch),
full_replay, and *resident-requested-but-unsupported* → must hard-require
full_replay rather than pass validation while running legacy.

Related open trap (latent, not the above): `formats/tekken.yaml` declares
`[THINK]`/`[/THINK]` inline-tag thinking and the FSM will swallow an UNCLOSED
`[THINK]`, stripping the response to empty. Mistral emits no `[THINK]` today
(0/6 runs, 0/4 probes) — real, currently unreachable.

## 5. Adaptive thinking — router DEMOTED to experimental (2026-07-25)

**Read `dev/ADAPTIVE_THINKING_STATUS.md` first.** It carries the methodology
of record, the failure shape, the live results, the web research, and the
tree-walk plan that replaces the current collection method.

The **learned router is EXPERIMENTAL**; the cue-authored static highs are NOT
demoted (they raise thinking at known-hard steps, independent positive
evidence). Headlines: the shipped artifact has classes `['low','medium']` only
— highs were quarantined by JUDGE_STANDARD's pairwise gate and never
certified, so it **cannot escalate to high at all**; it has been effectively
inert since ~07-17 (0–3% activation vs 25–70% in early July); and the −42%
canary is **withdrawn** — "always low" is indistinguishable from a fixed low
policy, i.e. gpt-oss faster at low, not smarter when adaptive.

**DECISION (Luke, 2026-07-26): start fresh — capture dynamic content only for
training, present only the dynamic portion to the router. Phase 0 is dead.**

Next actions live in that file's §8. ~~Phase 0 pairwise panels on the 84
quarantined highs~~ — **CANCELLED.** Those labels were assigned against a prompt
composition production no longer sends, so certifying them banks work that
cannot transfer.

The cause is dated and precise (§3.5, §8.0): the router is handed the **entire**
turn render, and a **1,459-char static instruction block**
(`run_in_terminal/plan_interaction_rules`, the `---ACT AS---` section) was added
to `plan_interaction` *after* the corpus was collected. It appears in **20.9% of
training prompts and 99.1% of live prompts**, and within training it is a
near-deterministic low marker (1.6% medium with it vs 16.5% without). The
observed live activation of 1.55% **is** that conditional — the router is an
ACT-AS detector, not a state router. Ablating that one section moves live
prompts 1.55% → 40.52%.

**Dead:** Phase 0; `trusted_labels_v1` + `*_labeling.json` as router-training
sources; the regenerated corpus for router purposes (actions conditioned on
elided previews). **Survives:** the methodology, the harnesses, the static
cue-authored highs, the tree-walk plan, and the regen as a serving-performance
artifact.

**First rung is now §8.0.1: one shared representation function** imported by
both the runtime router call and the capture tooling — the bug class was two
paths assumed to produce the same string and never compared. Nothing else
starts until train and serve provably share one representation.

Remaining format work (unchanged, still useful):

- **Gate membership dial.** Today `gate_levels: ["medium","high"]`; levels are
  now identity (low→low, medium→medium, high→high) after the blind boss panel
  — the earlier collapse mapped medium→step-low, so the router could only open
  or close the gate and every thinking turn thought at the floor. NOTE this is
  nearly inert in practice while the router activates 0.5% of the time.
- **Head-swap is INERT on step35** (`memory_can_shift=False` → resident
  fallback), so routed medium and high are currently identical in DEPTH — the
  gate is the only real dial. The flags are kept as intent; the depth dial
  self-activates if upstream makes step35 shiftable.
- **Gemma**: pick a strip-proof filler (`renderer.py:150` does
  `system_content.strip()`, which EATS the whitespace padding the equal-length
  splice needs), re-run the probe arm, then wire `reasoning.levels`. Also
  unvalidated live: the padded-off state with the OPEN opener prefilled.
- **Mistral Small 4** (tekken, `[THINK]`/`reasoning_effort` none|high) — its
  toggle position needs the same equal-length check. Note `thinking: true` is
  currently *lying*: we never render the system directive that activates
  Mistral's native reasoning.

## 6. Escalation-economy rungs — live validation (partial)

The three rungs from 2026-07-23 are unit-tested; live observation status:

- **Verify-only re-cert rung: CONFIRMED LIVE** (25 firings in the step37 boss
  baseline run). Nothing further owed.
- **Localization rung: NOT OBSERVED** — 0 firings in the same run
  ("Localization: <file> → symbol ..."). Either no symbol-less `file_ops`
  dispatches occurred, or it isn't reachable on this mission shape. Determine
  which; it should be displacing whole-file rewrites.
- **Phase-exit seam gate: observability SHIPPED 2026-07-25**, live observation still
  owed. `_phase_exit_seam_gate` used to return `None` silently on all three
  pass paths, so "0 hits in the log" could not distinguish *ran and passed*
  from *never ran*. Now every outcome logs: `Seam gate: clean — N file(s)
  checked` (INFO), `Seam gate: inert — ...` (INFO, <2 py files or unreadable),
  and `Seam gate: attempt bound reached (3/3) — failing OPEN` (WARNING — the
  phase exits with KNOWN-BAD seams; that line is a defect signal, not noise).
  The next structural run closes this item by log evidence: `clean` = gate
  validated live; `inert` = the mission shape never gives it 2+ files (a
  coverage question, not a gate bug); the WARNING = a real seam that survived
  three fix attempts.

Also open from that batch: `resident_seq_cache` for mistral-family (biggest
slow-model lever, ~13 of 34 min measured) and a prefill-rate-scaled
context-diet knob (server advertises the rate via health).

## 7. Swarm / research arc residue

The swarm-class arc landed (seat-leak fix, server-derived pool-fit gate,
deep_research v2 opt-in, content + diagnosis fan-outs, KV preflight guard).
What it left behind:

- **Live batched leak drill** — cancel a session turn under a swarm config and
  watch `checkedOut`/`engineActiveStreams` return to 0 within a reaper sweep.
  The fix is unit-tested and behaved in the wild; this is the deliberate drill.
- **gemma@65536 window-boundary session canary** — the measured pressure law
  raised n_ctx; probe for the -3 corruption class at the boundary before
  trusting it with a deep-session workload.
- **deep_research calibration** — extract-step INSUFFICIENT rate was high, and
  the single skeptic returned 26/28 unsupported on the first live run. Run the
  verify panel against a factual brief with known-good answers to calibrate the
  three-way verdict before trusting the [UNVERIFIED] flags.
- **Substrate-aware research budget** — scale wave count by measured decode
  rate so the same flow isn't ruinous on a 35 tok/s model.
- **Richer doctest-failure capture in gate reports** — the diagnosis triage
  fan-out correctly DEFERRED on impoverished doctest output; the fix is
  capturing more of the failure, not loosening the gate.

## 8. Test-suite work — what's left after the 2026-07-25 pass

The consolidation roadmap ran to completion (C0-C9) and a follow-on hardening
pass acted on two agent surveys. Landed: the parametrize tables, the
byte-identical merges, compiled.json anchoring, the reasoning-strip coverage,
six unfailable guard tests given teeth, and eight risk items from the
uncovered-code survey — including three production bugs (backend teardown
orphaning the pool, `push_event` truncating the event history before it
serialized, `stop_background_server` SIGKILLing any process holding the pid).
Root 1611 -> 1643, llmvp 330 -> 390. Details in the commit messages;
`TESTING.md` carries the durable rules.

**Deliberately deferred rows** (`TESTING.md` says why): `test_data_ops.py` /
`test_schema_registry.py` / `test_frame_editor.py` parametrization — seven
parametrization files in one pass is where quality drops, and the mechanics
are now proven, so these are cheap whenever wanted.

**Not done, and the reason matters:**

- `Mutation.swap_model` (llmvp graphql resolver) — it mutates the
  `_session_manager` module global with three outcomes and is tested nowhere;
  `test_model_swap.py` covers `core/model_swap.py` beneath it, not the
  resolver. Skipped only because it is net-new test design against the API
  layer, not because it is low value.
- **Runtime error-publication semantics — an OPEN QUESTION, not a gap.**
  `agent/runtime.py:1095-1104`: when an inference returns `result.error`, the
  runtime publishes `result.text` (empty on error) under EVERY key in
  `step_def.publishes`. Downstream steps then run on `""` rather than seeing a
  failure. That may be intended fail-soft or may be a bug; nothing pins it
  either way, and pinning the wrong one as "the contract" is worse than
  leaving it unpinned. **Decide the intent first, then test it.**

**Method note worth keeping** (now in `TESTING.md`): the mutation
spot-check earned its place four times in one day, twice by catching a test
the author had just written and believed. Its `assert count == 1` on the
pattern match is load-bearing — a mutation that silently fails to apply
reports "all green" and reads as confirmation.

## 9. generate_stream_sync request-prep extraction (SOAK-GATED)

The last "giants" item: shared `_prepare_stream_request()` for the
static/dynamic split + stop defaulting + kv_base + tracker.start that
`generate_stream_sync` and `_batched_stream` both do.

**Soak clock reset 2026-07-25** — `llama_cpp_backend.py` took three more
surgeries this week (seat-leak drain, health fields, KV preflight guard). Do
not start until those have soaked under real load for several days.

## 10. Transient inference errors kill missions at step one (COST A 7h RUN)

**2026-07-26, diagnosed from a live failure.** The mistral boss run terminated
after 5 minutes with zero goals:

```
[design_initial] Inference error: All inference instances are busy (active=1, limit=1)
[failed] Failed to design architecture and derive goals
```

A pressure probe had leaked a server-side prefill (its client timed out at
1802s but never CANCELLED, so a 57k-token eval kept the single seat). The boss
asked for that seat, got "busy", and died. "Busy" is definitionally transient —
the server raises it only after `backend_timeout` — so the seat was free
minutes later. The relaunch ran fine.

**Mechanism.** `design_initial`'s resolver cannot tell a substantive failure
from an infrastructure one:

```cue
{condition: "result.tokens_generated > 0", transition: "parse_architecture"},
{condition: "true",                        transition: "failed"},   // terminal
```

"The model generated nothing" and "we never got an instance" both land on
`failed`. **5 steps share this shape** (design_and_plan ×2, replan ×2,
plan_research ×1) — all PLANNING steps, i.e. exactly the ones whose failure is
terminal for the whole mission.

**Recommended fix — the effect layer, not the flows.** Retry-with-backoff on
transient inference errors belongs in `agent/effects/inference.py`, where it
fixes all five (and every future) call site at once; making each flow author
handle "busy" is how one gets missed. Distinguish transient (busy / no
instance / connection reset) from substantive (a real empty generation) and
retry only the former, bounded, with the existing watchdog as the outer bound.
Flow-level retries are the fallback if per-step policy turns out to differ.

**Also fix:** `dev/context_pressure_probe.py` must cancel server-side on client
timeout. An abandoned socket leaves the server working, which is what
manufactured the "busy" in the first place.

## 11. Shared prefix cache — THE next performance lever (Luke, 2026-07-26)

**See `dev/caching/CORPUS.md` + `EXPERIMENT.md` (2026-07-29): Block E carries
11a's pilot and the F12 one-cell test; 11b's workload map is corpus §8.**

**F12 RESOLVED 2026-07-30 (Block E1): cells are shared for MEMORY as for
time.** 64 × ~9k unique prompts ran clean at ~78% real occupancy where
static-per-stream counting predicted 93%. No gate change needed — the current
gates already count client tokens only; F12's quoted skip arithmetic was an
older revision's.

**PROMOTED on measured evidence.** The swarm-performance study
(`dev/swarm_performance/FINDINGS.md`) closes with two independent measurements
that make this the highest-value work available:

- **Prefill does not parallelize** (§4) — serialization ≈ 1.0 at every width and
  prompt size, so seats cannot help prefill-bound work. The only lever is not
  reading the same tokens twice.
- **Shared KV costs ~6% of private KV** (§8) — `w = 0.06`, measured by
  intervention (empty persona vs production, 64 cells). Tokens that are SHARED
  are nearly free in both phases: no prefill on a hit, and ~6% of decode.
  Tokens that are PRIVATE pay full freight in both.

So the whole serving-performance question reduces to **what fraction of a prompt
can be made shared.** Today exactly one block qualifies — the persona head.
Everything a swarm genuinely re-reads (blueprint, contract, shared design
context, common file bodies) is private and paid N times over. The mechanism is
already in the engine and already proven at 16× discount; it is simply pointed
at one block of text.

**Caveat carried from FINDINGS §9:** all of the above is homogeneous-batch
measurement. Heterogeneous workloads are uncharacterized, and the one ragged
datapoint (the corpus regen) is over-predicted by the model by +32.5%. That does
not weaken the cache case — prefill serialization and w=0.06 are both
workload-shape independent — but any *sizing* recommendation from that study is
an upper bound, not a prediction.

**Finding (2026-07-26).** There is NO opportunistic prefix cache. Reuse is
entirely via explicitly pinned seq bands:

    seq 0            SEQ_WORKING  live generation
    seq 1            SEQ_STATIC   static prefix, forked per seat (memory_seq_cp)
    [2, 2+flow)      flow band, caller must pass flow_key + static_prefix
    [snap_base, ..)  session snapshots
    [reason_base,..) pinned reasoning heads

`prepare_seat` CLEARS a seat (`memory_seq_rm(seq, 0, -1)`) before reuse, so no
request ever hits another request's KV. Two consequences:

- **Seat count cannot raise the hit rate.** The static prefix is forked to
  every seat regardless of width; there is no cross-seat sharing to accelerate.
  (This refutes the intuition that 48 seats would reuse a common prefix faster
  than 16 — worth recording because it is a reasonable guess.)
- **Repeated prompts prefill cold every time.** Measured on the counterfactual
  corpus, where each turn is sent 3x at different reasoning levels:
  **1.03M of 1.54M prompt tokens (67%) are redundant re-reads.**

### 11a. The swarm case — where this is worth the most

A contract-swarm fan-out runs N independent workers that share a large common
context (blueprint, contracts, interface vocabulary) and differ only in their
symbol. Today each worker prefills that shared context independently, so a
20-worker wave pays for it 20 times. Pinning it ONCE and forking to each worker
seat is exactly what `SEQ_STATIC` already does for the global static prefix —
the machinery exists, it is just not reachable per-workload.

**Experiment to queue:** measure the shared fraction of a real fan-out's worker
prompts, then A/B a pinned-shared-prefix wave against the current cold-prefill
wave at matched N. Report prefill tokens saved, wall-clock delta, and whether
the forked KV stays correct across workers (the correctness bar, not just the
speed one). `dev/swarm_performance/prefill_grid.py` already measures cold prefill and
asserts `cachedPrefixTokens ~= 0`, so it is the natural base to extend.

### 11b. Revisit flow_kv_cache — built before we understood seq shifting

**REPRIORITIZED 2026-07-29, not strengthened.** The hy3 arm measured a third
reuse regime — the PTY session — and found it costs 60.8% of run wall on the
full-replay path. But that does NOT argue for 11b, because resident already
solves sessions on shiftable archs (flat, verified on gpt-oss/Devstral). What it
changes is the map of what is left:

| workload | shape | today |
|---|---|---|
| session, shiftable arch | append-only | **flat** — solved by resident |
| session, non-shiftable arch | append-only | **O(n²)** — 9 of 19 configs |
| stateless completion | shared head, varying tail | cold every time — **11b** |
| swarm fan-out | big shared block × N | cold × N — **11a** |

Row 2 may be fixable by CONFIG rather than by build, and that inverts the order:
run the `memory_can_shift` sweep (`dev/CACHE_SWEEP_PLAN.md`, free now that the
query is unconditional) BEFORE building v2. If most of the 9 can flip, 11b
shrinks to rows 3-4 — its original scope — instead of also carrying row 2.
Predictions are pre-registered there; the headline ones are that hy3 and
qwen3.6-35b-a3 can go flat with a one-line change, and that step37 reports
can_shift=False despite having perfect flags.


`flow_kv_cache: false` everywhere today because **save_state churn corrupts the
120B static KV over a run** (decode -3 at Pos 1809), and the 2026-06 swa_full
re-enable REGRESSED under game_challenge and was reverted.

But that verdict is about the MECHANISM, not the idea. flow_kv_cache was built
on `save_state`/`load_state`; everything learned since — the resident-seq cache,
`memory_seq_cp`/`memory_seq_rm` forking, the seq-band planner — says per-sequence
ops are the safe primitive and full-context save_state is the fragile one
(§4 retires the legacy save_state session path for exactly this reason).

**So: flow_kv_cache v2 on seq ops.** Same goal (pin a reusable prefix per flow),
different primitive (fork from a pinned seq rather than save/restore a whole
context). If that holds up it is a general framework lever, not a per-workload
hack — every fan-out, every repeated-prompt batch, and the stateless-completion
gap in 11 all get it at once.

**Prerequisite:** stateless completions currently have NO safe reuse path on
gpt-oss (flow_kv_cache unsafe, resident_seq_cache is session-scoped). That gap
is the thing 11b would close.

### 11b — POSITION 2026-07-30: the ban is obsolete; one build separates the
### flow cache from production

**The "flow-skip bug" resolved as a CLIENT-CONTRACT violation, not a server
defect.** The contract: `prompt` is the DYNAMIC TAIL ONLY; the server prepends
`static_prefix` (warm_flows.py is the reference client). The 2026-07-30 pilot
led its prompt with the head too, so the server skipped the pinned copy and
dutifully prefilled the duplicate — total_ctx 7,947 = static 1,786 + head
2,255 + duplicated head+tail 3,906, each "HIT" 2.3s SLOWER than cold with
cacheHit=true. Two fixes landed: a loud detect-and-strip guard in
run_completion (a doubled head is wrong on the uncached path too), and the
corrected pilot.

**The corrected measurement (glm, pool, resident M9 band, 2.3k head + 1.7k
tails): HIT saves 3.51 s/call = 49% of prefill** (1,652 fresh vs 3,907; 11/11
hits). This refines CACHE_STATE's "flat 0.2–0.5 s/call — not the lever"
verdict: that was measured on ~2k TOTAL prompts in the shallow-head era; at
realistic head sizes the flow cache IS a lever.

**Why the ban is obsolete (operator's framing, confirmed):** the ban hit M8 —
save_state BLOBS whose churn corrupted the static KV (the 2026-06 code -3
regression). M9 (seq-ops, the "v2" this section called for) has existed since
Phase 2, shares none of that mechanism, and postdates the ban's evidence. The
seq robustness + refresh tooling (windowing, latch-heal, per-seq purge,
context refresh) that keeps deep sessions healthy applies to flow seqs
identically.

**What separates it from production:**
1. **Batched mode ignores the flow band entirely** (`_warm_batched` forces it
   off; persona heads cover only the GLOBAL static, not per-flow heads) — and
   production runs batched. THE remaining build: port the flow band to batched
   using the snapshot-band pattern that shipped 2026-07-30 (band seqs above
   snapshots in plan_seq_map, control-inbox surgery, seat fork; BUILD = eval
   the head via a normal stream then capture-style seq_cp). Medium effort,
   pattern proven.
2. Fleet configs all carry `flow_kv_cache: false`. The POOL resident configs
   (glm, hy3, mistral-medium, gemma-26b, laguna-xs) can flip on today's
   measured evidence; the gpt-oss production flip waits on (1).
3. The M8 legacy blob path is now dead weight: every flow-capable config is
   resident, so the save_state flow branch (`_flow_states` blobs) is
   unreachable in practice — delete with §4's else-arm.

### 11c. Speculative decoding for swarm decode — GATED on one measurement

The lesser lever (prefill is ~82% of wall clock on real workloads; this trims
the other 18%), but the precondition turns out to be satisfied and it has
never been tested in the regime that matters.

**Why revisit.** SD was measured HARMFUL single-stream at ~65 tok/s. That says
little about swarm workers at 2-6 tok/s per stream — a rate which looks slow
but is the correct consequence of sharing one device across many streams, not
idle hardware.

**The precondition IS met.** Derived from the 2026-07-26 decode ladder, the
marginal cost of adding a token to a batched step falls monotonically and has
NOT flattened at N=128:

    N       1     2     4     8    16    32    48    64    96   128
    ms/tok 19.8  13.6  10.5   9.3   7.3   6.9   5.7   4.9   4.25  3.62

Cheap extra tokens per step is exactly what SD needs. Had this flattened, the
idea would be dead.

**The catch: SD and batching harvest the SAME slack.** At N=1 a token costs
19.8 ms — huge headroom, which is why SD is attractive single-stream in theory.
By N=64 it is 4.9 ms and most of that slack is already banked by batching.
Break-even acceptance at N=64 (sub-linear batch-cost fit):

    K=3  batch 64->192 (x2.21 step)  needs ~65% acceptance
    K=4  batch 64->256 (x2.71 step)  needs ~70%
    K=6  batch 64->384 (x3.63 step)  needs ~78%

Note it gets HARDER as K grows — the opposite of the single-stream case.

**Two blockers.**
- The batched engine has NO speculative path. `LlamaNGramMapDecoding` is wired
  per-instance via `draft_model=` on the POOL path only; `grep draft
  inference/batched_engine.py` returns nothing. Swarms run batched, so this is
  a build, not a config flip.
- Our draft is n-gram/prompt-lookup, not a draft model (EAGLE-3 is in llama.cpp
  C++ but unbound in our fork — see the llmvp-binding memory). Prompt-lookup
  accepts well only when output COPIES from input. Swarm workers do echo
  contract signatures, type names and imports from a shared spec, which is
  plausibly our best case; novel function bodies will not copy, and those are
  most of the tokens.

**THE GATE — do this before writing any engine code.** Pool mode already has
working speculative. Run it on representative swarm worker prompts and record
accepted-vs-proposed tokens. One afternoon, no new engine code, decisive:

    acceptance >= 70%  -> build batched SD; expect ~1.1-1.3x on swarm decode
    acceptance <  65%  -> it can only lose. Close the question for good rather
                          than relitigating it every few months.

Record the measured rate either way — a null result here is worth keeping,
because "speculative decoding on Mac" keeps coming back up.

## 11d. Transient-file flush defeated by declaration drift — CONTAMINATES EVERY
## game_challenge ARM (found 2026-07-29, hy3 tier arm)

`flush_transient_files` deletes program-written side-effect files after a test
session, reading the patterns from `architecture.transient_files`. Its docstring
names the failure it was built to stop: *"The gemma run's poison class: quitting
saved `game_over: true` to state.json, main.py auto-loaded it on launch, and
every later test saw 'game has already ended' — **25 fix rounds against a symptom
no code change could clear**."*

**It ran 22 times in hy3's arm and matched nothing, all 22 times.** The
architecture declared `transient_files: ['save.json', '*.autosave.json']` at
design time; the code that got built writes `game_state.json` (`main.py:10`).
`fnmatch` matches neither, so the save survived every session:

| | |
|---|---|
| reports that launched the game | 22 |
| **resumed a save instead of starting fresh** | **20 (91%)** |
| rooms resumed into | Cave Mouth 13, Echoing Hall 11, Quiet Shrine 9, Old Armory 1 |
| `No transient files matched [...]` | 22/22 |

Downstream cost, traced end to end: an eval resumed in Quiet Shrine, tried
`go north` (invalid there — the shrine only connects south), got the CORRECT
rejection, and filed a parser-failure report *whose own summary states "the
shrine only connects south"*. That false failure consumed 3 goal attempts and
triggered a 10-turn / 586-second `diagnose_issue` session which concluded the
code was fine — and investigated `look` while the report was about movement.

**FIXED 2026-07-29 — declare it where the code exists, plus a tripwire.**
Operator decision: keep the declaration (auditable, reuses an existing step)
rather than switching to pure observation, and accept that brownfield /
`top_phase: structural` runs get the tripwire only.

1. **The declaration moved out of `design_architecture` into `project_ops`**
   (`declare_artifacts` → `persist_artifacts`, first two steps of the flow), which
   runs after the structural phase and before the first behavioural session — so
   the answer can be READ instead of predicted. New schema
   `schemas/runtime_artifacts.json` requires a `written_by` citation per entry,
   which is what forces the model to look. New action
   `action_persist_transient_files`. Placed FIRST in the flow deliberately:
   installs can fail → `build_report_failure`, and `environment_verified` is set
   even on failure, so a late step would be skipped exactly when the run is
   already struggling.
2. **The evidence got fixed at the source.** `_extract_python_signature` now
   emits module-level path constants, so `format_project_listing` shows
   `SAVE_FILE = "game_state.json"` — the fact that was invisible. This was
   cheaper than a new formatter and it improves **every** consumer, including
   the brownfield `extract_architecture`, which already asked for
   `transient_files` from these same signatures.
3. **Reconcile can no longer wipe it.** `action_parse_and_store_architecture`
   builds a brand-new `ArchitectureState` and never shows the model the current
   value, so a reconcile pass silently reset the correction (the way
   `coherence_*` gets wiped). It now carries the prior value forward **on
   omission only** — a supplied value still wins.
4. **The tripwire** reports unaccounted program-generated files and pushes ONE
   `failure_analysis` note so the next diagnostician is told the run may have
   resumed rather than started fresh. Not gated on `flushed == 0` (a partial
   mismatch contaminates identically), and it fires when NOTHING is declared —
   an early return had made that case invisible, which was the more likely
   failure once the declaration moved.

Verified end to end on the real hy3 artifact: the guess
`['save.json','*.autosave.json']` is replaced by `['game_state.json']` and the
flush deletes it. 1845 agent + 550 llmvp green; 17 mutations bite across the two
test modules; `cue-compile` / `lint` / `lint-flows` clean for `project_ops`.

**Still open here:** brownfield `ingest_workspace` and `top_phase: structural`
never reach `project_ops`, so they rely on `extract_architecture`'s (now
better-grounded) declaration and the tripwire. Pure observation — snapshot at
session start, flush what appeared — remains the universal fix if that gap ever
bites.

### 11d-adjacent: three defects found while mapping `project_ops` (unfixed)

Recorded, not chased — none is this change's business:

- **`setup_result` is never published.** `project_ops.cue:25`
  (`returns.setup_complete ← context.setup_result`) and `build_report_success`'s
  optional context both read it, but the only publisher was the deleted
  `run_setup_commands` step. So `setup_complete` is permanently absent from the
  flow's returns, and `reporting_actions.py:333,342-343` renders a `Setup: …`
  line that can never appear for project_ops.
- **`test_install_command` is forbidden by its own schema.**
  `collect_test_installs` (`project_ops.cue`) reads that field and
  `prompts/set_env/detect_tooling_rules.yaml` asks the model for it at length —
  but `schemas/validation_env_config.json` sets `additionalProperties: false` on
  `LanguageCommands`, so a schema-conforming response can never contain it. The
  step therefore always finds nothing and falls through. Either add the field to
  the schema or drop the prompt paragraph and the step.
- **`write_files`' resolver is unconditional** (`{condition: "true"}`), so
  `all_written == false` or "No file blocks found" routes onward silently.

Related, same run: **7 of 9 failed reports recorded ZERO checks**
(`checks_passed: [] / checks_failed: []`). A failure verdict with no checkable
items is unfalsifiable — the vacuous-verification shape again.

## 12. Small items (grab-bag)

- Agent-side identical-retry backoff: the KV-eviction and anti-gut loops
  both retried the same dispatch unchanged for hours. Auto-refresh bounds
  the souring case; a dispatch-level "same goal+flow failed N× in a row →
  backoff/escalate" guard bounds the class.
- Standing lint advisory: `add_symbol.generate_new_symbol` string-match
  condition (arguably a linter over-trigger — either exempt emptiness
  checks in flow_lint or convert the step).
- mission.json archival (memory: memory-hardening-audit).
- Qwen3-Next resident-cache validation + windowing follow-ups (memory:
  resident-seq-cache-implemented).
- Branch `ingest-workspace-and-tb-comparison` merge decision.

## 13. Parked until triggered (do NOT start unprompted)

- **Polish/creativity gate** (rank 60 reserved in PHASE_RANKS): a
  `flows/code_core/polish_gate.cue` modeled on quality_gate.cue (review →
  structured findings → harvest `type="polish"` goals → `polish_verified`
  flag); dimensions = player-facing prose, creative richness, UX affordances,
  thematic consistency. Quality-gate pass would switch to setting
  `quality_verified` with the terminal moving to rank 60; mirror rules in both
  swarm controllers; prompts under `prompts/code_core/polish_gate/`; MUST fail
  on zero checkable items (the vacuous-verification trap); opt-in via
  `top_phase: polish`.
- 615-turn quarantine panel (K=7 highs + splits) under
  `dev/JUDGE_STANDARD.md` + the 250-turn post-featurizer-fix
  regeneration calibration. Trigger: wanting to grow/re-train the
  reasoning router (provenance chain preserved in dev/ for exactly this).
- Context-refresh stub-rate auto-trigger (the timed cap + drain covers
  the operational need; a signal-based trigger is a refinement).
- MTP / speculative decode: NO-GO recorded — the fork exposes the nextn layer
  but hidden-state handoff is absent (0–1% acceptance). Wait for upstream; do
  not re-spike without an upstream change.
