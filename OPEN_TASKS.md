# Open Tasks — Delegation Brief

*Deferred work with enough context to pick up any item cold.*

**This is a LIVING doc.** When an item completes and needs no follow-up,
**delete it** — don't leave a "CLOSED" tombstone. When an item completes but
leaves residue, replace it with the residue only. Findings worth keeping past
the task belong in the memory files, `dev/` docs, or a config comment, not here.

*Last audited 2026-07-30 — every section below was checked against the code,
the run logs, and git history; ten closed items were deleted.*

## Standing constraints (read before touching anything)

- Explicit `git add <files>` only — never `-A`; commits end with the
  Co-Authored-By trailer of whoever/whatever authored them.
- NEVER bare `uv sync` (prunes out-of-band packages: terminal_bench,
  torch, mlx, the editable ~/Repos/tau-bench install). `uv pip install`
  or `uv sync --inexact`.
- llmvp tests run under llmvp's OWN venv: `cd llmvp && .venv/bin/python
  -m pytest tests/`. Main suite: `uv run pytest tests/`. (2026-07-30:
  1845 main / 601 llmvp passing — run them, don't trust the number.)
- Verification fence per change: both suites + `ouroboros.py lint-flows`
  (0 errors / 1 standing advisory) + `smoke` (47/47) + `cli-smoke`
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
  before allocating — three reboots were precomputable. Note the wired limit
  (`iogpu.wired_limit_mb`) is NOT one of those ceilings: four passing
  measurements cross it. Physical memory is what kills the box.

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

Nothing has been built: `adapters/swe/runner.py:76,79` passes
`problem_statement` through as free text, and `ingest_workspace.cue:90-102`
runs the repomap with no query/focus params. The hook exists but is unwired —
`research_actions.py` documents `focus_files`, populated only from
`context.target_file_path`. Newest pilot artifact is 2026-07-09.

*Build + unit tests need no server; evaluation does.*

## 2. TRAP_BRIEF: the A′ sub-case is still unmeasured

`dev/TRAP_BRIEF.md` documents the deterministic-startup-fail blind-diagnose
trap. The ENV sub-case was root-caused and fixed 2026-07-27 (`e61fdc0`, seven
fixes, 33 mutation-verified tests; `dev/POOLSIDE_TRAP_ROOTCAUSE.md`), as was
the self-reverting-write defect found alongside it (`ea09dd8`,
`tests/test_module_fix_symbol_continue_clobber.py`).

**What is left:** TRAP_BRIEF §7's A′ analysis — ground truth present but
IGNORED, static tracing mis-localising to the wrong package — is a *different*
failure from the env one, and nothing addresses it. Localisation was correct on
every cycle in the env case; the remedy was simply inexpressible. Rerun one
canonical trap task (thompson-nfa-regex-engine or bplus-tree greenfield) to
check it. Confirmed never rerun: no `*nfa*`/`*bplus*` run dirs anywhere, no
goal spec in `dev/`.

**The measurement vehicle exists but has never run.** `dev/poolside_v3_followon.sh`
records `F821` and `repeat_warn` in its OUTCOME file (lines 136-141), but its
only log is 336 bytes and ends at "waiting for the quant A/B driver" — there is
no arm-3 work dir and no OUTCOME sidecar carrying those counters.

**Related, and the reason arm 3 matters — lint asks, and that is FINAL.**
`753ccf8` + `9194f7a` made a failed `lint:` check set `structural_block_reason`
= "lint" until `goal.lint_reviewed` (`reporting_actions.py:402`,
`models.py:300`), so an F821 blocks for exactly one fix-or-defer pass and is
then accepted. **This is the settled design, not a half-measure** (operator,
2026-07-30): hard-gating lint creates inescapable trap surface — a goal whose
lint finding the model cannot resolve would loop forever with no route out,
which is precisely the trap class `dev/TRAP_BRIEF.md` exists to document. The
one-pass ask gets the finding in front of the model at write time without
manufacturing a new dead end. `pipeline_actions.py:757` keeping
`"required": tier == "syntax"` is therefore correct and should stay.

What is genuinely owed is only the **measurement**: zero live firings of
`Structural sweep: lint review` across every run to date, including
post-commit ones. The behaviour has never been observed in the wild, so we
know it ships but not what it does. That is what arm 3 would show.

## 3. TB2: the next measurement

Canary complete — **adaptive 2/8 (first-ever winning-avg-corewars pass) vs
flat 1/8**. The oracle fixes shipped and were retested: **chess-best-move and
mteb-retrieve were rerun on gpt-oss and did NOT flip**, which leans capability
ceiling rather than framework adjustment. No retake is owed on those two
unless new work changes the substrate — then they are the canonical pair.

Open decision: **full-89 single-arm adaptive run** against the 18.7% Terminus
baseline (`dev/tb2_harbor_gptoss.sh`). The only large TB2 run on disk is
`runs/tb2-harbor-gptoss/2026-06-18__21-40-35/` (35 tasks), which predates the
adaptive work entirely.

Also still deferred: the **context-budget arm** — reduced `prepare_context`
budgets for fix-mode rewrites, since the 26k-token rewrite prompts were the
KV-collision trigger. Unvalidated and untouched: `prepare_context.cue:41` is
still `context_budget: 8`, `:73 max_chars: 3000`. Luke wants this validated
carefully, not assumed.

## 4. Adaptive thinking — router DEMOTED to experimental (2026-07-25)

**Read `dev/ADAPTIVE_THINKING_STATUS.md` first** — it carries the methodology,
the failure shape, the live results, and the tree-walk plan. The short version:
the learned router is EXPERIMENTAL and effectively inert (0–3% activation); its
shipped artifact has classes `['low','medium']` only, so it cannot escalate to
high at all; the −42% canary is withdrawn. The cue-authored static highs are NOT
demoted. **DECISION (Luke, 2026-07-26): start fresh — capture dynamic content
only, present only the dynamic portion to the router. Phase 0 is dead.**

**First rung, and nothing else starts before it (§8.0.1): one shared
representation function** imported by both the runtime router call and the
capture tooling — the bug class was two paths assumed to produce the same
string and never compared. NOT implemented: `reasoning_router.py:127-132` still
takes the whole `prompt`, both call sites (`runtime.py:992-996`, `:1244-1248`)
hand it the full render, and `turn_renderer.render()` has no dynamic-only
variant. No capture-side assertion, no extractor hash.

Remaining format work:

- **Gate membership dial.** `llmvp/formats/chatml.yaml:35` is still
  `gate_levels: ["medium","high"]`; levels are identity after the blind boss
  panel. Nearly inert in practice while the router activates <1% of the time.
- **Head-swap is INERT on step37** (`step37-flash-196b-a11.yaml`, formerly
  called step35 here — no `step35` exists in the repo any more).
  `reasoning_head_swap: true` at `:35` but `resident_seq_cache: false` at `:57`
  because `memory_can_shift=False`, and the config says so itself. Routed
  medium and high are therefore identical in DEPTH — the gate is the only real
  dial. Flags kept as intent; the depth dial self-activates if upstream makes
  step37 shiftable.

## 5. Seam gate: the fail-open WARNING still lies, and the counter is mis-scoped

`_phase_exit_seam_gate` (`mission_actions.py:2063`) is validated live — 156×
`Seam gate: clean`. It also fired 8× `attempt bound reached (3/3) — failing
OPEN, phase exits with UNRESOLVED seams`. **Those eight were read on
2026-07-30 and the WARNING is mostly wrong.** Three defects; **the third is
fixed**, the first two are open and are the reason this section stays:

**1. The bound check runs BEFORE any analysis, so the claim is never
established.** `mission_actions.py:2100-2110` returns `None` on the attempt
count without reading a file, without `_transfer_shape_violations`, without
`action_run_contract_typecheck`. "Phase exits with UNRESOLVED seams" is
asserted from a stale note, not measured. Verified against the artifacts: of
the 8 firings, **3 had every seam already fixed** (qwen3-next's `_ask` /
`_handle_dialogue_choice` / `world_data` all resolved in the final
`engine.py`; laguna's `_restart` fixed at cycle 33, *before* the first
WARNING). The other 5 are one devstral run re-reporting a single stale note
set at five successive phase exits. Fix: re-run the (deterministic, cheap)
checks before claiming anything, so a fail-open says what is actually broken.

**2. The counter is mission-lifetime, never reset on a clean pass.**
`attempts` sums every `seam_gate`-tagged note over the whole mission. The qwen
log is the proof: `Seam gate: clean` at four separate exits interleaved
between the failures, counter climbing throughout — three *unrelated,
sequentially-arising* seams, each fixed on the first try, and the mission was
then permanently locked out of the gate. Key the counter to the seam identity
(or reset it on a clean pass); the bound is mis-scoped, not too low.

**3. FIXED 2026-07-30 (`fbad927`) — the missing-self-method shape.** Devstral
hit `GameEngine._handle_flee` three times with byte-identical seam text
because the dispatch localized to the **calling** symbol and said "fix
{target} so its cross-module calls match", so a missing-*definition* seam got
its caller re-edited three times and the method was never written. The seam
was also unreachable: it sat in `_handle_command`, a second dispatcher the
model wrote mid-migration to helper style and never wired up. Three changes
landed: `_symbol_reachability()` (in `batch_structural_actions.py`, beside
`_transfer_shape_violations`) makes a seam whose every access site is dead
report-only rather than blocking; a missing definition now dispatches "ADD
these members, do not edit the call sites"; and dead duplicates plus orphaned
module-level methods are surfaced under a `seam_gate_advisory` tag that
cannot consume the attempt budget. Biased toward LIVE throughout — dunders,
decorated symbols, `main`, `test_*` and any name in a string are live,
recursion is not reachability, unknown access sites stay blocking. 20 tests,
5/5 mutations bite.

Still true, and unfixed: the gate is **misnamed for what fires** — all 8
surviving seams were intra-module self-attribute errors from the typecheck
half, while the cross-module transfer-shape half produced zero, yet the
non-missing-definition message still says "cross-module"; and the note stores
`seams[:300]`, truncating mid-token in all 8, while the WARNING tells the
operator the evidence is in those notes.

Also open: a **prefill-rate-scaled context-diet knob** (server advertises the
rate via health). Not implemented anywhere — `decode_tps` exists only as a
reporting field (`agent/trace.py:727`), never as an input to a budget.

## 6. Swarm / research arc residue

- **Live batched leak drill** — cancel a session turn under a swarm config and
  watch `checkedOut`/`engineActiveStreams` return to 0 within a reaper sweep.
  The fix is unit-tested (`tests/test_watchdog_identity.py`) and behaved in the
  wild; this is the deliberate drill. No drill script exists yet.
- **deep_research calibration** — extract-step INSUFFICIENT rate was high, and
  the single skeptic returned 26/28 unsupported on the first live run. Run the
  verify panel against a factual brief with known-good answers to calibrate the
  three-way verdict before trusting the [UNVERIFIED] flags.
- **Substrate-aware research budget** — scale wave count by measured decode
  rate so the same flow isn't ruinous on a 35 tok/s model.
  `deep_research_actions.py:66-69` is still fixed constants.
- **Richer doctest-failure capture in gate reports** — the diagnosis triage
  fan-out correctly DEFERRED on impoverished doctest output; the fix is
  capturing more of the failure, not loosening the gate.
  `contract_swarm_actions.py:1396-1414` still runs bare `python -m doctest`
  with no report flags and truncates at 800 chars.

## 7. Test-suite work — what's left

**Deliberately deferred rows** (`TESTING.md` says why): `test_data_ops.py` /
`test_schema_registry.py` / `test_frame_editor.py` parametrization — seven
parametrization files in one pass is where quality drops, and the mechanics are
now proven, so these are cheap whenever wanted. (Still zero `parametrize` in all
three.)

**Not done, and the reason matters:**

- `Mutation.swap_model` (`llmvp/api/graphql_api.py:983`) — it mutates the
  `_session_manager` module global with three outcomes and is tested nowhere;
  `test_model_swap.py` covers `core/model_swap.py` beneath it, not the
  resolver. Skipped only because it is net-new test design against the API
  layer, not because it is low value.
- **Runtime error-publication semantics — an OPEN QUESTION, not a gap.**
  `agent/runtime.py:1096-1113`: when an inference returns `result.error`, the
  runtime publishes `result.text` (empty on error) under EVERY key in
  `step_def.publishes`. Downstream steps then run on `""` rather than seeing a
  failure. That may be intended fail-soft or may be a bug; nothing pins it
  either way, and pinning the wrong one as "the contract" is worse than
  leaving it unpinned. **Decide the intent first, then test it.**

**Method note worth keeping** (now in `TESTING.md`): the mutation spot-check
earned its place four times in one day, twice by catching a test the author had
just written and believed. Its `assert count == 1` on the pattern match is
load-bearing — a mutation that silently fails to apply reports "all green" and
reads as confirmation.

## 8. generate_stream_sync request-prep extraction (SOAK-GATED)

The last "giants" item: shared `_prepare_stream_request()` for the
static/dynamic split + stop defaulting + kv_base + tracker.start that
`generate_stream_sync` (`llama_cpp_backend.py:3781`) and `_batched_stream`
(`:4315`) both do. The duplication is intact.

**Soak clock reset again 2026-07-30** — the backend took three more surgeries
this week (batched snapshots `c3b90dd`, the batched flow band `59ba951`, the
legacy-splice deletion `25b8af2`). Do not start until those have soaked under
real load for several days.

## 9. Abandoned requests + terminal planning steps

The **retry half of this item shipped** (`21e3d2e`): transient inference errors
now retry with backoff at the effect layer
(`agent/effects/inference.py:616-628`, markers `"instances are busy"` /
`"connection error"`, backoff 5/15/45s, bounded by `OURO_TRANSIENT_RETRIES`).
Marker coverage is narrow — a literal "connection reset" or a differently
worded no-instance error would not match.

**Still open, and it re-bit on 2026-07-30:**

- `dev/context_pressure_probe.py` has **no cancellation path at all** — it
  abandons the socket on timeout and never sends a request id, so it cannot
  cancel. An abandoned socket leaves the server working, which is what
  manufactured the original "busy". Block E of the caching experiment observed
  the same shape again: a 10-minute client timeout left all 64 abandoned
  generations running to completion. (Server-side cancel exists —
  `batched_engine.py:515` — this is unwired client, not missing capability.
  Note the server now retires abandoned NON-streaming completions on
  disconnect, `7eff4d7`, which covers the API path but not this probe.)
- **Five planning steps are still terminal on an infrastructure error.** The
  shape `{result.tokens_generated > 0 → parse} / {true → failed}` means "the
  model generated nothing" and "we never got an instance" land identically on
  a terminal `failed`: `design_and_plan.cue:158-159` and `:175-176`,
  `replan.cue:127-128` and `:169-170`, `plan_research.cue:63-64`. The effect
  layer mitigates but does not remove this.

## 10. Shared prefix cache — the remaining two workloads

**Reference: `dev/caching/CORPUS.md` + `EXPERIMENT.md`.** The case is measured
and settled: prefill does not parallelize (serialization ≈ 1.0 at every width,
so seats cannot help prefill-bound work), and shared KV costs ~6% of private KV
(`w = 0.06`, measured by intervention). So the whole serving-performance
question reduces to **what fraction of a prompt can be made shared** — and
tokens that are PRIVATE pay full freight in both phases.

The stateless-completion row is now solved (flow band, shipped and
live-accepted 2026-07-30). Two rows remain:

### 10a. The swarm fan-out case — where this is worth the most

A contract-swarm fan-out runs N independent workers that share a large common
context (blueprint, contracts, interface vocabulary) and differ only in their
symbol. Today each worker prefills that shared context independently, so a
20-worker wave pays for it 20 times. Pinning it ONCE and forking to each worker
seat is exactly what `SEQ_STATIC` already does for the global static prefix —
the machinery exists, it is just not reachable per-workload.

**The experiment has NOT been run.** Block E resolved a different question (the
pool-fit over-count, F12 — 64 × 9k unique prompts at ~78% occupancy, single
arm, no comparison wave). What 10a still needs: measure the shared fraction of
a real fan-out's worker prompts, then A/B a pinned-shared-prefix wave against
the current cold-prefill wave **at matched N**, reporting prefill tokens saved,
wall-clock delta, and whether the forked KV stays correct across workers (the
correctness bar, not just the speed one).
`dev/swarm_performance/prefill_grid.py` still has only the cold arm —
`cachedPrefixTokens` appears solely as the proof-of-cold assertion — so it
remains the natural base to extend.

### 10b. Speculative decoding for swarm decode — GATED on one measurement

The lesser lever (prefill is ~82% of wall clock; this trims the other 18%), but
the precondition is satisfied and it has never been tested in the regime that
matters. SD was measured HARMFUL single-stream at ~65 tok/s; that says little
about swarm workers at 2-6 tok/s per stream.

**The precondition IS met.** From the 2026-07-26 decode ladder, the marginal
cost of a token in a batched step falls monotonically and has NOT flattened:

    N       1     2     4     8    16    32    48    64    96   128
    ms/tok 19.8  13.6  10.5   9.3   7.3   6.9   5.7   4.9   4.25  3.62

**The catch: SD and batching harvest the SAME slack.** Break-even acceptance at
N=64 (sub-linear batch-cost fit) — note it gets HARDER as K grows, the opposite
of the single-stream case:

    K=3  batch 64->192 (x2.21 step)  needs ~65% acceptance
    K=4  batch 64->256 (x2.71 step)  needs ~70%
    K=6  batch 64->384 (x3.63 step)  needs ~78%

**Two blockers.** The batched engine has NO speculative path (`grep -i draft
llmvp/inference/batched_engine.py` → 0 matches; `config.py:615` raises on
`decode_mode: batched` + `speculative`), and swarms run batched — so this is a
build, not a config flip. And our draft is n-gram/prompt-lookup, not a draft
model (EAGLE-3 is in llama.cpp C++ but unbound in our fork — see the
llmvp-binding memory); prompt-lookup accepts well only when output COPIES from
input.

**THE GATE — do this before writing any engine code.** Pool mode already has
working speculative. Run it on representative swarm worker prompts and record
accepted-vs-proposed tokens:

    acceptance >= 70%  -> build batched SD; expect ~1.1-1.3x on swarm decode
    acceptance <  65%  -> it can only lose. Close the question for good rather
                          than relitigating it every few months.

Not yet runnable as specified: there is **no accepted-vs-proposed
instrumentation** in `llmvp/`. `dev/spec_bench.py` is the *single-stream*
June benchmark, keyed on `decodeTpsRecent` with file-reproduction prompts — it
is the inadequate result this gate exists to supersede, not the gate. Record
the measured rate either way; a null result is worth keeping, because
"speculative decoding on Mac" keeps coming back.

## 11. Transient-file flush — the brownfield gap

The declaration-drift defect is FIXED (2026-07-29): `transient_files` is now
declared in `project_ops` (`declare_artifacts` → `persist_artifacts`) where the
code already exists and can be READ instead of predicted, backed by
`schemas/runtime_artifacts.json` requiring a `written_by` citation;
`_extract_python_signature` emits module-level path constants so the fact is
visible; reconcile carries the prior value forward on omission; and a tripwire
reports unaccounted program-generated files. Verified end to end on the hy3
artifact.

**Still open:** brownfield `ingest_workspace` and `top_phase: structural` never
reach `project_ops`, so they rely on `extract_architecture`'s (now
better-grounded) declaration and the tripwire alone. Pure observation —
snapshot at session start, flush what appeared — remains the universal fix if
that gap ever bites.

### 11a. Vacuous verification, same run

**The three `project_ops` defects recorded here were fixed 2026-07-30**
(`91451f7`, alongside the plan_setup split): the phantom `setup_complete`
return is removed, `test_install_command` was added to the schema that
forbade it, and `write_files` now reports `parse_failed` and routes it to
`build_report_failure` instead of treating a fence-parse failure as success.

Still open, from the same run: **7 of 9 failed reports recorded ZERO checks**
(`checks_passed: [] / checks_failed: []`). A failure verdict with no checkable
items is unfalsifiable — the vacuous-verification shape again.

## 12. Tier scores are confounded by cache strategy (S1 handicap)

**Operator ask, 2026-07-30.** `dev/caching/FEATURE_MATRIX.md` establishes that
S2 (pool + resident) is the optimum for any model that supports it, and that the
four S1 models (qwen3.5-122b, qwen3.6-27b, step37, qwen3.6-35b) can layer
**nothing** — no flow cache, no snapshots, no head-swap, no windowing, and
sessions that re-prefill entirely every turn.

So every tier placement for an S1 model was earned while paying **7.8–9.8× the
prefill** of its S2 competitors, and the losses we already attribute to "capacity"
(memory: framework-overhead-timeouts) may be partly strategy. Right now the
ledger pools them as if the substrate were equal.

**The cheap fix costs nothing extra:** every arm already reports the strategy
triple in health (`sessionStrategy` / `sessionCanShift` / `residentRequested`).
Tag each tier result with its strategy and report S1 placements separately
rather than pooled.

**The honest ceiling:** a matched A/B is impossible by construction — an S1
model cannot be put on S2, that is what S1 *means*. So report the handicap
alongside the score; never subtract it and never impute a counterfactual.

## 13. The 1800s context refresh is an undeclared generation ceiling

Found 2026-07-30 while characterizing the laguna ramble. `context_refresh_seconds`
(default 1800) fires on a WALL CLOCK, and under batched with a drain window it
fires while busy — so **any generation longer than ~30 minutes is retired**,
whatever `max_tokens` says. Three separate arms hit it (60,659 / 50,497 /
39,711 tokens, all evicted mid-generation, all shipping zero files).

Nobody set it as a generation limit. It is invisible in the config, it is not
mentioned where `max_tokens_default` is chosen, and the failure it produces is
the expensive one: the work is discarded AND the refresh wipes the flow band
and demotes hot snapshots, so it degrades the following arm too.

Not obviously wrong — a 30-minute single generation is usually pathological —
but it should be a DECLARED policy with its own name rather than a side effect
of a cache-hygiene timer. Options: a real `max_generation_seconds`, or exempting
an in-flight generation from the timed refresh, or leaving it and documenting
it where generation budgets are set.

**Caveat on any past reading of `degen`:** the tier degeneration counter read
the wrong log file and could never rise (fixed 2026-07-30, `5fbe939`). Every
tier arm on disk — 15 across all sweeps — recorded `degen=0`, including one
whose long-cycle guard demonstrably fired. Any conclusion resting on a clean
degen reading is unsupported, not confirmed.

**And `degen` is a LOOP detector, not a waste detector.** It measures distinct
n-gram ratio against a threshold tuned for repetition (0.125). The two most
expensive failures measured — 110k+ wasted tokens across two arms — scored
0.62 and 0.67 and never tripped it, because coherent deliberation is not
repetitive. Files-per-minute and the presence of an eviction capture are the
signals that actually discriminated.

## 14. Small items (grab-bag)

- Agent-side identical-retry backoff: the KV-eviction and anti-gut loops
  both retried the same dispatch unchanged for hours. What exists today is
  one-shot escalation on a single goal (`models.py:262,344,904-906`), not a
  dispatch-level "same goal+flow failed N× in a row → backoff/escalate" guard.
- Standing lint advisory: `add_symbol.generate_new_symbol` string-match
  condition (arguably a linter over-trigger — either exempt emptiness
  checks in flow_lint or convert the step). Distinct from the flow-contract errors fixed 2026-07-30.
- **Qwen3-Next resident-cache re-verification.** It carries
  `resident_seq_cache: true` on a 2026-07-02 three-turn probe and has no
  `probe_verified_cache:` block; it was not in the Block F sweep, so its
  resident claim has never been checked under the depth-12 / flipped-flags
  regime that reclassified several other models. Windowing follow-ups from the
  `resident-seq-cache-implemented` memory ride along.
- Branch `ingest-workspace-and-tb-comparison` merge decision — now **413
  commits** ahead of `main`, whose tip is `4198104`. The longer this sits the
  less "decision" and the more "migration" it becomes.

## 15. Parked until triggered (do NOT start unprompted)

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
