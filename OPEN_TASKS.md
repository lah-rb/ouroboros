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
  (The POOL-mode sibling — CancelledError in `_heal_instance` stranding a
  limit=1 pool's only instance — is CLOSED 2026-08-03, `9da59d0`:
  shield + finally-requeue mirroring `_release_seat`, pinned by
  `llmvp/tests/test_pool_release_hardening.py`.)
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

**RESOLVED 2026-08-06/07 — the universal fix was built, then improved.** It
bit (hy3: save.json undeclared for a full run), and the observation design
shipped as written: pre-session snapshot + post-session diff
(`action_snapshot_workspace` / `flush_transient_files`), so the transient set
is observed on EVERY interact regardless of project_ops — the brownfield gap
is closed. The operator then ruled deletion be DEFERRED (2026-08-07): session
end records to `mission.observed_transient_files`; deletion happens at the
next interact entry, on pause, and at completion, so artifacts stay
inspectable between sessions. Postscript on the declaration path: its
warning-driven repair abandoned after two dispatches because
`architecture.transient_files` is mission metadata no fix flow can write —
observation IS the declaration now.

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

**2026-08-08 update — the OPPOSITE failure found, and pool mode is now
structurally exempt.** On `decode_mode: pool` the refresh NEVER fired at all:
trigger (a) needs an idle poll pinned sessions never allow, and (b)'s
fire-while-busy drain is batched-only — a 45h hy3 run logged ZERO refreshes,
the deferral counter wasn't exposed, and the un-refreshed rot climbed to
22+ degeneration events and killed the server twice. Fixed (`6141f1d`):
past-due counters piggyback the heal-on-release path, so pool-mode refreshes
fire only BETWEEN sessions — which also resolves this section's ceiling
concern for pool mode by construction (a release-time refresh cannot retire
an in-flight generation). The undeclared-ceiling question now applies ONLY
to batched-drain configs. `refresh_deferred` is exposed in health.

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
  **Sharper as of 2026-08-08:** the uncapped retest produced the same shape
  (phase two: 33 honored retests; quit: 23 via the accommodation loop), and
  the raw counters a backoff would key on now EXIST on the goal
  (`retest_count`, `escalation_count`, `last_escalation_attempts`) — the
  guard is a policy over data already collected.
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
## 16. Batch-creation fallback: session-driven batch or swarm serial

**Not a defect — the CURRENT fallback works and the shape is expected**
(operator, 2026-07-31). Logged so the option is not re-derived from scratch
later.

Batch structural delivery is BIMODAL, and that is the normal shape rather
than a problem. In the 2026-07-30 smoke (18 arms):

| batch outcome | arms |
|---|---|
| wrote 100% of declared | devstral 9/9 · gpt-oss 11/11 · gpt-oss-swarm 11/11 · hy3 6/6 · laguna-xs 7/7 · mistral 10/10 · qwen3.6-27b 11/11 · qwen3.6-35b 7/7 · step37 8/8 |
| near-complete | qwen3.5 11/12 |
| wrote 0 or 1 | qwen-next 1/8 · glm 1/7 · laguna-s-apex 0/7 · olmo-instruct 0/7 |

Operator reading: the success rate is BETTER than expected. qwen3.6-27b landed
a batch for the FIRST time. laguna and glm have both batched successfully
before, so they are FLIPPY on consistency rather than incapable. qwen-next and
olmo are par for the course. qwen3.6 MoE "always lands like that"; a different
shape would be the surprising result.

**What serial fallback currently buys**, from the four collapse cases — it is
the difference between a salvaged arm and a lost one:

    qwen-next    batch 1/8 -> 8 files  (fully recovered)
    glm          batch 1/7 -> 3 files  (partial; the 14.3k-token long-cycle
                                        loop happened HERE, in serial)
    olmo-instr   batch 0/7 -> 2 files
    laguna-apex  batch 0/7 -> 0 files  (batch failure WAS the run failure at
                                        the 20m cap; the same config produced
                                        13 files at 2h)

**The two candidate replacements**, neither worth doing now:

1. **Session-driven batch** — retry the batch inside a live session rather than
   dropping to per-file serial, so the model keeps the declared file list in
   context instead of re-deriving each file cold.
2. **Swarm serial** — fan the missing files out to parallel one-shot workers
   instead of walking them serially. The arm most damaged by serial (glm) lost
   its time to ONE file's loop; parallel workers bound that blast radius to a
   single worker.

**Trigger to revisit:** a 2h arm where serial fallback still fails to recover a
collapsed batch. The smoke's evidence is confounded by the 20-minute cap —
laguna-apex looked like a total loss there and produced 13 files at 2h.

---

## 17. The design gate rejects a VALID src/ layout — 5 arms lost

**CONFIRMED DEFECT, not a model failure.**
**Status:** FIX LANDED 2026-07-31 (`5852992`): third exemplar (coherent NESTED)
plus the sys.path discriminator in the critic prompt. REMAINING = the live
retest: re-run the three gemma arms (~2 min each) at epoch v2.0 open to
validate the fix on the exact failing designs. The deterministic
materialise-and-import refutation below stays as OPTIONAL hardening — land it
only if the retest still shows a false incoherence.

### What happens

gemma-4-26b designs this, three times, across two sampling regimes and two
decode strategies, in wording that is near-verbatim identical each time:

    import_scheme: package     init_files: True
    run_command: python main.py    working_directory: project root
    modules: src/models.py src/loader.py src/parser.py src/io_handler.py
             src/engine.py main.py

`design_gate_critique` returns incoherent with:

    "run_command `python main.py` is at the project root, but `main.py`
     attempts to import from `src/engine`, which is not importable as `src`
     unless `src` is treated as a package, but the import scheme is 'package'
     and the modules are nested under `src/`"

Three reconcile attempts reproduce the same layout, the budget exhausts, the
mission dies at 1-2 minutes with ZERO files.

### The claim is false. Verified three ways (2026-07-31)

    scheme=package, init_files=True, `python main.py` from root  -> IMPORTS FINE
    the same layout with NO __init__.py                          -> IMPORTS FINE
      (PEP 420 namespace packages, Python 3.3+; this box runs 3.14.6)

`init_files: True` is declared IN THE ARCHITECTURE THE CRITIC IS READING, so
`src/__init__.py` would exist and `src` is a regular package. The critic's own
objection is answered by a field in its input.

### Why the over-block guard did not catch it

`action_ground_design_gate_verdict` flips an incoherent verdict to coherent when
NO CONCRETE CRITERION SURVIVES grounding. This criterion is concrete: it names
real files and states a specific mechanism. It is simply WRONG. **The guard
filters UNGROUNDED critiques, not INCORRECT ones**, and nothing anywhere in the
pipeline ever attempts the import it is adjudicating.

Same class as the acceptance-check permanent veto (fixed 2026-07-18): a
mis-grounded derived check vetoing correct work.

### Cost so far

    tier_20260730-220500  arm05 gemma-4-26b-a4b          0 files, 2 min
    tier_20260730-220500  arm06 gemma-4-26b-a4b-batched  0 files, 2 min
    tier_20260731-050209  arm05 gemma-4-26b-a4b          0 files, 2 min
    tier_20260731-050209  arm06 gemma-4-26b-a4b-batched  0 files, 1 min

Four arms to this defect. A fifth (gemma-4-31b, smoke arm07) died separately to
the degen-abort, now fixed.

WARNING FOR THE LEDGER: the 2026-07-31 morning read of the smoke recorded
"gemma-26b fails identically under both strategies, so the failure is
model-level with no strategy component." That conclusion is WITHDRAWN. The
identical wording across independent runs was the tell — models do not usually
reproduce prose verbatim; a deterministic gate does.

### The fix

Before accepting a layout-incoherence verdict, make the claim decidable:
materialise the declared module paths in a temp dir with the declared
`init_files`, and attempt the import implied by `run_command`. If it imports,
REFUTE the criterion. Milliseconds, and it converts an LLM opinion into a
deterministic check on the one thing here that is mechanically decidable.

### Timing

Recommended: land AFTER the current sweep, then re-run the three gemma arms —
they cost ~2 minutes each, so it is a ~6-minute retest rather than a restart.
Landing mid-sweep changes which architectures survive for arms 7-18, which is a
larger contamination than the design_initial retry (that only fired where an arm
was already dead).

---

## 18. The seam gate holds the decisive defect in its hand and discards it

**Status:** CLOSED 2026-08-02 (`1de18a7`): live→dead seam transitions BLOCK
(static false-dead never transitions), the gate runs on every fileset change,
and a zero-gate-run park WARNS (§19's half). Remaining validation is simply the
v2 field runs exercising it.
**Found:** 2026-08-01, arm13 (qwen3.5-122b) of `tier_20260731-050209`.
**Reproduce:** `uv run python -P dev/blind_panel/seam_deadcheck.py`

### What happened

Arm13 shipped a well-built world attached to a game that cannot be finished. A
blind judge reached the Boss Chamber in two moves, holding the exact item the
boss is weak to, and got:

    A Dark Lord blocks your path!
    Type 'attack' to engage the enemy.
    > Unknown command: attack

`GameEngine.initiate_combat()` — a complete, working turn-based system with
phase transitions, a weakness bonus and a flee roll — has **zero callers in the
shipped tree**. Three monsters are inert, the two-phase boss is inert, and a
fully-written victory screen, defeat screen and `restart` branch are all
permanently unreachable. Conformance scored 9/10; `no_broken_functions` scored
6/20. The gap between reading this artifact and playing it is one missing
`elif`.

### It was an edit that did it, and the gate watched it happen

`adventure/engine.py-e` (02:30) is the pre-edit copy; the shipped `engine.py`
(03:07) is newer. The diff is 31 lines and bundles two unrelated changes:

    <             self.initiate_combat(next_room.monster)
    <         else:
    <             self.display_current_room()
    ---
    >             monster_obj = self.monsters[next_room.monster]
    >             self.io.display_message(f"A {monster_obj.name} blocks your path!")
    >             self.io.display_message("Type 'attack' to engage the enemy.")

...alongside `for i,` → `for _i,` in three loops. The lint diagnosis on record is
*"Root cause: Two enumerate() loops in adventure/engine.py use..."* — so a
cosmetic lint fix was dispatched, localization selected `symbol GameEngine` (the
whole god-class), and the rewrite silently deleted the only call into combat
while satisfying the lint complaint it was sent to fix.

Immediately after, the log reads:

    Cross-module type check: 8/8 files clean
    Seam gate: clean — 8 file(s) checked (transfer-shape + typecheck),
               no reachable mismatches (0 unreachable seam(s), 0 cruft item(s))

### The gate already computed the answer

`_phase_exit_seam_gate` calls `_symbol_reachability(sources)`, whose `dead` key
is documented as *"defined, never referenced anywhere else."* Run over the
shipped tree it returns **two** symbols:

    adventure/engine.py::GameEngine.initiate_combat     <-- the decisive defect
    adventure/state.py::StateManager.has_save

One of two on this arm. (That ratio is arm13-specific -- see the campaign-wide
check below, which found 47 dead symbols across 12 arms and a real
false-positive mode.) The gate had the finding and threw it away.

**Why it is invisible.** `dead` is consulted only inside the loop over
`problems`, and `problems` is populated exclusively from transfer-shape and
typecheck output — i.e. `has no attribute/method 'X'` mismatches. It is used to
DECIDE WHETHER A REPORTED MISMATCH IS BLOCKING, never to raise one. A method
that exists, is correct, and simply has no callers generates no mismatch, so it
never enters `problems`, `unreachable` stays empty, and the gate reports clean.

The gate detects *"A calls B.x and B has no x."* It cannot detect *"B.x is
perfect and nobody calls it"* — which is the shape that killed this artifact,
and the shape the campaign's own §3.1 numbers say is the field's dominant
failure (28.6% earned across seven models, see
`dev/blind_panel/RESULTS_tier_2026-07-31.md`).

### Campaign-wide check — which decides the fix

**CORRECTION to the paragraph above.** "One of two, no noise" is true of arm13
and does NOT generalise; it was measured on one arm and should not have been
written as a property of the signal. Swept across all 12 staged arms
(`dev/blind_panel/seam_deadcheck.py`):

| | |
|---|---|
| total dead symbols, 12 arms | **47** |
| dead symbols in the core playable loop | 8 |
| arms with a core-loop dead symbol | 6 of 12 |
| arms where the dead set is **CONFOUNDED** | 2 of 12 |

Two findings change the recommendation:

1. **Dead code is normal, not exceptional.** 47 symbols across 12 arms, most of
   them unused accessors and helpers (`WorldState.get_room`,
   `DialogueEngine.reset`, `Player.get_total_defense`). Blocking on the raw dead
   set would stall healthy runs constantly.

2. **The dead set has a false-positive mode, and it is not rare.** arm04 reports
   21 dead symbols including its entire `handle_*` family — yet that arm scored
   53 and played fine. Cause: `engine.py:58` dispatches with
   `getattr(self, handler_name, self.handle_unknown)`, a COMPUTED name no static
   scan can resolve, and the arm ships two complete trees (`engine.py` and
   `src/engine.py`, each with its own `GameEngine`). `_symbol_reachability`'s
   docstring says dynamically dispatched symbols are treated as live, but that
   only holds for literal attribute names. arm07 is confounded too.

### FIXED (2026-08-01) — regression gate + cadence landed together with §19

`_symbol_reachability` now returns the full `defined` universe; the gate keeps
a process-local per-mission memo (previous defined/dead sets, run counter,
fileset hash) and BLOCKS on the live→dead TRANSITION: a symbol reachable at
the previous check whose last caller an edit removed. The directive names the
severed call and offers the deliberate-removal exit (deleting the dead
definition clears it), and baseline retention re-flags an unresolved
regression instead of adopting it as the new normal. The arm04 confound
cannot trip it — static false-dead never transitions. Tests:
`tests/test_seam_gate_regression_and_cadence.py` (the arm13 shape end-to-end,
stable-dead immunity, deliberate-removal clearing, re-flag persistence).

### The original fix sketch (superseded by the above, kept for the record)

1. **Advisory (safe, low value on its own).** Add core-loop dead symbols to the
   existing `cruft` channel, which already writes a `seam_gate_advisory` note.
   Changes no control flow. Must stay non-blocking per §2154-2160. With 47
   symbols across 12 arms this is mostly noise, so treat it as breadcrumbs for a
   human reading the notes, not as a mechanism.

2. **Blocking on a reachability REGRESSION — this is the one to build.**
   Persist the previous phase-exit dead set on the mission; block when a symbol
   that was LIVE becomes DEAD. `initiate_combat` was live at 02:30 and dead at
   03:07.

   **The sweep supplies the argument that intuition could not:** a static false
   positive is STABLE ACROSS PHASE EXITS. arm04's `handle_*` methods look dead at
   every single check, so no live→dead transition ever fires and the confound is
   structurally invisible to a regression test. Only a genuine change in
   reachability trips it. The regression formulation is therefore immune to the
   exact failure mode that makes option 1 unusable as a gate — which is a
   stronger reason to prefer it than "it is higher signal."

   Scope it to the core loop or to symbols above a size floor if the first
   trial is noisy, but the transition test is the mechanism.

### Timing

Land AFTER the sweep completes. Arms 1-14 ran with the current gate; changing
what blocks mid-flight makes later arms non-comparable and contaminates the DoE
table. Same reasoning as §17.

### Loose end (unresolved, low priority)

`adventure/engine.py-e` shipped inside the package and cost the artifact a point
on organization. The `-e` suffix is the BSD `sed -i -e` signature, but there is
no `sed` anywhere in the arm's run log and no `-e` backup writer in our tree, so
its origin is unexplained. Not chased further; noted so the next occurrence is
recognised rather than re-investigated.

---

## 19. The seam gate never ran on two arms — including one that shipped the exact defect it exists to catch

**Status:** CLOSED 2026-08-02 (`1de18a7`, with §18): the gate cadence keys on
fileset change (not the old trigger), and a run that parks with ZERO seam-gate
executions logs a WARNING into the driver record.
**Found:** 2026-08-01, arm14 (qwen3.6-27b) of `tier_20260731-050209`.
**Distinct from §18.** §18 is the gate computing the answer and discarding it.
This is the gate NOT RUNNING AT ALL.

### The defect it would have caught

arm14 scored 54 (tier 1 by score) with conformance 50/53 — and 4 of its 8 rooms,
all four monsters, the boss, both terminal states and half its items are
unreachable, because entering any monster room kills the process:

    File ".../engine.py", line 149, in _cmd_move
        narration += get_combat_narration(self.active_combat)
    File ".../combat.py", line 216, in get_combat_narration
        monster_hp_pct = monster.hp / monster.max_hp ...
    AttributeError: 'Monster' object has no attribute 'max_hp'

`combat.get_combat_narration` reads `monster.hp`, `monster.max_hp`, `player.hp`,
`player.max_hp`, `combat.turn` and calls `combat.is_boss()` — SIX attribute
names that do not exist (the real ones are `monster_health`,
`monster_max_health`, `health`, `max_health`, `turn_count`, and `is_boss` is a
property). The judge repaired only those six names on a scratch copy and the
entire game worked: block/dodge/poison behaviours, armour reduction, the phase-2
shift, the Sunstone weakness doubling damage, and the victory screen.

**This is a REACHABLE cross-module attribute mismatch — the single class the
seam gate's transfer-shape and typecheck analyses exist to detect**, and the
gate's `_missing_attr` parses exactly this shape (`has no attribute/method
'X'`). It is not a dead-symbol case; `_cmd_move` is live and on the happy path.

### The gate never ran

`_phase_exit_seam_gate`'s docstring is explicit, and it is what makes this
provable:

> EVERY outcome logs. The gate originally returned None silently on all three
> pass paths, which made it unobservable in run logs: "no seam-gate lines" could
> not distinguish *ran and passed* from *never ran* [...] Clean and inert log
> INFO; the fail-open bound logs WARNING.

`grep -cE "Seam gate:"` over arm14's 1014-line run log returns **0**. Not clean,
not inert, not fail-open — never invoked. (The lone `Cross-module type check:
7/7 files clean` at line 138 comes from batch structural creation, and predates
`combat.py` being patched at lines 187 and 433.) That earlier design decision to
log every outcome is the only reason this is diagnosable rather than ambiguous —
it paid for itself here.

### Coverage across the sweep is wildly uneven

| arm | work cycles | seam-gate runs |
|---|---|---|
| devstral | 173 | 41 |
| gpt-oss | 117 | 24 |
| qwen3-next | 99 | 22 |
| gpt-oss-swarm | 140 | 19 |
| hy3 | 64 | 10 |
| glm | 36 | 8 |
| laguna-xs | 31 | 8 |
| laguna-s | 40 | 5 |
| qwen3.5 | 37 | 5 |
| mistral-medium | 17 | 2 |
| **gemma-4-31b** | **109** | **0** |
| **qwen3.6-27b** | **19** | **0** |

`gemma-4-31b` is the alarming row: 109 work cycles — a full run's worth of
effort — with no cross-module check ever performed.

### Root cause and the shape of the fix

The gate runs at **serial structural-phase EXIT** only. A run that never exits
the structural phase is never seam-checked, no matter how much work it does or
how many files it writes. And the runs that fail to exit the phase are precisely
the ones shipping half-finished, unintegrated work — so **coverage is
anti-correlated with need**. arm14 wrote 16 files across 128 minutes and 19
cycles and was never checked once.

FIXED (2026-08-01), landed together with §18:

1. **Cadence** — `_seam_gate_cadence` runs from the sweep walk on every
   structural fileset CONTENT CHANGE (sha256 over the readable .py set),
   throttled so an unchanged tree never re-gates; the phase-exit call now
   routes through the same throttle (a clean verdict on identical content is
   not recomputed; an unresolved BLOCK keeps its re-run pressure so the
   attempts bound still works). A run that loops inside the structural phase
   is now gated within one cycle of every real edit.
2. Park-time run: superseded by (1) — every edit is gated when it lands, so a
   parked artifact has already been checked against its final content.
3. **Zero-run WARNING at park** — agent/loop.py logs when a mission parks with
   >=2 structural .py files and zero gate runs, so "never ran" is visible in
   the run log instead of requiring a cross-arm grep.

Tests: tests/test_seam_gate_regression_and_cadence.py (hash throttle gates
once per content change; the <2-file inert verdict stays observable at exit
per the every-outcome-logs rule).

### Timing

After the sweep, with §17 and §18. Changing what blocks — or how often —
mid-flight makes the remaining arms non-comparable.

---

## 20. Every README in the sweep was truncated by OUR extraction, and every model was docked for it

**Status:** CLOSED 2026-08-02 (`a0ec769`): batch prompts mandate 4-backtick
fences for `.md` files, and `_restitch_truncated_md` repairs the legacy shape
on extraction. The v1.2 judging records remain contaminated as documented —
scores stand pre-epoch; no rejudge.
**Found:** 2026-08-01, prompted by the operator's rubric audit.
**Same class as** [[featurizer-bare-lt-marker-corruption]]: the extraction layer
corrupting valid model output, systematically misattributed to models.

### The signature

11 of 12 staged arms ship a README that ends at (or just before) an install
command inside an unterminated code fence:

    arm01  ends 'pip install -e .'    unterminated ```bash
    arm03  ends 'pip install .'       unterminated
    arm04  ends 'pip install -r …'    unterminated
    arm08  ends 'pip install -e.'     unterminated
    arm10  ends 'cd ouroboros-adventure'  unterminated (INDENTED fence)
    arm11  ends 'cd text-adventure-game'  unterminated (INDENTED fence)
    arm12, arm13, arm16  end 'pip install -e .'   unterminated
    arm14, arm15  end 'python main.py'            unterminated

Every model family. Judges in at least five arms docked §3.10 for "truncated
mid-fence, never says how to run the game" — the run instructions were always
in the SECOND fenced block, which never survived.

### Root cause — CommonMark, not the model

Batch structural creation has models emit files as fenced blocks with
`# === FILE: path ===` markers. A triple-backtick fence CANNOT CONTAIN another
triple-backtick fence — per CommonMark, the first interior line matching a bare
close TERMINATES the outer block. So any markdown file whose content includes
its own ``` blocks is amputated at its first interior close, and the remainder
is discarded as non-block text.

Reproduction (byte-identical to the field signature):

    reply = fenced batch of [main.py, README.md-with-two-bash-blocks]
    parse_file_blocks(reply) ->
      FILE 'README.md': 92B | last_line='pip install -e .' | run section GONE

This is not the regex fallback — markdown-it-py is installed and CommonMark
semantics produce the same cut. The models' replies were VALID and complete;
the wrapping convention is what cannot represent them.

### Consequences

1. **§3.10 is contaminated field-wide.** Every documentation score in the
   campaign partially measures this bug. The dimension was already the weakest
   (2-4 across the field, hard-capped often) — some unknown fraction of that is
   ours. Do not draw model conclusions from the docs dimension of this sweep.
2. Possibly related, unconfirmed: the shared broken build-backend string
   (§ cross-arm signal) also lives in the same always-truncated packaging
   region; whether truncation and that string share provenance is NOT
   established.

### FIXED (2026-08-01) — both halves landed

1. **Prompt-side:** `generate_all_instruction.yaml` now teaches a FOUR-backtick
   outer fence for .md files with a worked example, and
   `reconcile_instruction.yaml` + `escalate/work_instruction.yaml` carry the
   one-line rule. (`author_instruction.yaml` has no md surface — untouched.)
2. **Extraction-side:** `_restitch_truncated_md` in `agent/markdown_fence.py` —
   trigger is a .md target with ODD fence parity; repair re-reads the raw reply
   from the file's marker to the next FILE marker (or EOF) and peels the true
   outer close; declines unless the repair EXTENDS the truncated content and
   reaches even parity. AND the fix found a second casualty class while being
   tested: the truncation RE-PAIRS every fence after the md file (a bare close
   swallows the next block's opener), silently DROPPING subsequent files —
   plausibly some of the sweep's "N missing (serial fallback)" batch losses.
   parse_file_blocks now re-parses the reply from each re-stitched file's true
   end and recovers those blocks (dedup-by-path keeps it safe).

Tests: `tests/test_markdown_fence_md_restitch.py` (12) per the spec — roundtrip
complete + even parity under markdown-it AND the regex fallback, the
campaign-signature assertion, the unterminated-outer EOF case, four-backtick
untouched, non-md odd-parity untouched, extend-never-replace. Main suite 1944
green.

RESIDUE: the §3.10 contamination note for the v1.2 sweep stands — scores are
not revised. The three-arm build-backend string remains a separate open signal.

---

## 21. Truncation audit — "scope, don't truncate" violations (2026-08-01)

**Status:** CLOSED 2026-08-02/03 (epoch batch W1, `9b0fed5` + `0a46155`).
Every Tier A site fixed: rewrite got the symbol-menu drill-down
(offer_context_menu → fetch_symbol_body, cap 3 picks) + AST-derived import
deps + signature blocks; the interact tester map renders complete entries via
`render_data_file` (skeleton fallback); command-vocab loads full; the swarm
diagnose-worker symbol-scopes around implicated line numbers; the seam
directive caps per-problem (regressions first). Tier A′ head-cuts adopted
`_cap_diagnostic` (traceback-aware tail-keep) at all five sites. Original
audit preserved below as the map of the class.

Triggered by the laguna Q6_K degeneration study: `prepare_context`
byte-cuts related files at 3,000 chars, which left `UI.prompt` (byte 9,634 of
ui.py) INVISIBLE to a rewrite whose entire deliberation hinged on that
signature — 4 long-cycle orbits, ~150k wasted tokens.

**ADOPTED DESIGN (operator, 2026-08-01):** the Ouroboros standard is SCOPE,
DON'T TRUNCATE. When a model selects (or is handed) a file larger than the
context limit, bring it ONE MENU DEEPER: present the file's symbol table and
let it select FULL symbols from within. Byte-truncation wrecks the workflow;
symbol selection preserves it. The good patterns already in-tree:
`data_trace.extract_data_skeleton` (schema skeleton instead of byte-cut),
repomap (a map is scoped by construction), `analysis_types` stub-fallback,
`format_session_tail` (tail-keep for terminals).

### Tier A — model-facing evidence cut mid-body (the dangerous shape)

| site | what gets cut | measured bite |
|---|---|---|
| `flows/code_core/prepare_context.cue:73` (max_chars 3000) | related files in rewrite/fix context | THE case above. Fix = symbol-menu drill-down. |
| `agent/projections.py:441` (max_chars 4000) | **the play-tester's world map** — `data_file_contents` in interaction_context | REAL artifacts exceed it: arm01 world.yaml 13,727B (tester saw ~29%), Q6_K 7,477B (~54%). The interact-phase tester navigates and grades with a PARTIAL world. Plausibly affects functional-test coverage and goal counters. Fix = data skeleton + drill-down (the data_trace pattern). |
| `agent/projections.py:451` (max_chars 3000) | parser modules read for command-vocabulary hints | verb tables past 3KB never contribute to the vocabulary shown to testers |
| `agent/actions/contract_swarm_actions.py:2238` | diagnose-worker sees `content[:6000]` — the file it is diagnosing, head only | same shape as prepare_context; plus `output[:1200]`, `directive[:1500]` |
| `agent/actions/mission_actions.py:2331` | seam-gate directive evidence `seams[:800]` TOTAL across all problems | multi-problem gates truncate the later problems out of the fix directive |

### Tier A′ — HEAD-cut on stderr/tracebacks (wrong END kept)

`batch_structural_actions.py:768,772,834,847` (`[:500]`), `:951`
(`terminal_output[:1000]`): Python tracebacks put the exception LAST, so a
head-keep can retain the frame list and drop the error line. The tree already
has the right convention in `format_session_tail` (tail-keep) — these sites
contradict it. Fix = tail-keep for anything that can contain a traceback.

### Tier B — correct scoping, keep as the house patterns

data_trace skeletons · repomap budgets (design_and_plan/replan/ingest 4000) ·
analysis_types stubs · session_tail tail-keep · ops terminal_output tail
formatters · all logger/observation `[:60]`-class display cuts.

### Notes-system inventory (the broader look, same session)

Written categories: failure_analysis (14 sites), codebase_observation (3),
lint_warning (2), general (2), architecture_blueprint (2). Read filters cover
failure_analysis / architecture_blueprint / codebase_observation /
approach_rejected.

RESOLVED 2026-08-02 (epoch batch W6): `lint_warning` WIRED — both writers now
tag the target file, so the existing per-file filter surfaces them in the
last-5 window (the channel was write-only). `task_learning` and
`dependency_identified` PRUNED from the enum with a before-validator mapping
retired/unknown categories to "general" so archived mission.json files still
load. CORRECTION to the original audit: `requirement_discovered` is NOT
writer-less — `mission create --task` writes it (ouroboros.py:188); the
channel stays.

## 22. Testing paradigms for functional goals (operator, 2026-08-07)

The acceptance-check machinery was conceived as a REPLAY GUARD — after a
goal's first genuine pass, pin that session's durable end-state so a later
edit that breaks it reopens the goal. On the hy3 run it quietly deviated
into a quasi testing standard: derived checks became the de-facto
definition of the goal (session-fingerprint checks like
`current_room_id=='room4' and inventory==['sword']` vetoed every
legitimate future pass), and each veto bounced through a full
diagnose_issue that investigated nothing.

Patched tactically (2026-08-07): derive rule 8 (invariants, never
fingerprints), and vetoed rounds now dispatch a direct retest instead of a
diagnosis (`DirectiveReport.acceptance_vetoed`). Both keep the replay
guard a replay guard.

THE OPEN QUESTION is the strategic one: what testing paradigm should
functional goals actually carry? Constraints that make this tricky:

- Models typically write TESTS worse than they write code — a
  model-authored test suite becomes its own defect surface (the rigged
  checks catalog in derive rules 3-5 exists because every one of those
  patterns was observed).
- Must stay LANGUAGE-AGNOSTIC: the framework cannot assume pytest or any
  per-language harness; today's floor is `/bin/sh -c` + exit codes.
- The behavioural evaluator (PTY session + LLM judge) is the primary
  verifier; anything added must complement it, not compete (the double-veto
  incident: two signals judging the same question drove a churn loop).
- Deterministic replay of an INTERACTIVE program is inherently fragile —
  world state moves, routes change, output wording shifts. The stable
  substrate is invariants over durable artifacts, which is thin coverage.

Directions worth evaluating (none committed):
- Goal-scoped invariant contracts derived at DESIGN time (when the canon
  is authored) instead of post-pass — checks born from intent, not from a
  session's incidentals.
- Scripted-replay checks that drive the program's stdin with the passing
  session's command sequence and assert on coarse outcomes (exit code,
  final-state keys) — replay as data, not as re-derived assertions.
- Property-style checks over data files (schema/shape validation via the
  data_shapes exemplar machinery, which already exists and is checkable).
- Accepting the thin floor: evaluator-primary with structural-only checks,
  and investing instead in evaluator reliability (the picky-personality
  fix class).

**ANSWERED IN PART (2026-08-09) — the TDD repair loop shipped.** The
direction taken is none of the four above: author the test at the point of
REPAIR, in the diagnosis session, because that is the only moment a
NEGATIVE CONTROL exists (the code is still broken, so a test can be watched
failing for the right reason). `flows/code_core/diagnose_issue.cue` gained
`author_test_gate` → `author_test` after `systemic_scan`, with exactly one
exit to `end_session` — the arm can never delay or block the fix.
`agent/actions/authored_test_actions.py` keeps a candidate only if four
MECHANICAL controls pass: classified red (rc 1 + named failing nodes +
clean collection — an import error is red forever and would immortalize the
goal), a cold-workspace flush before the probe, a double run whose second
is deliberately warm (order-dependent tests drop out), and leftover/speed
gates. On success the authored check is stored FIRST and every derived
replay check is demoted to `required: False` — which is the direct
mechanical cure for the reopen class, since `action_regression_sweep`
filters on `required` in both directions. `authored_tests: "auto"|"on"|"off"`
in MissionConfig is the kill switch.

Constraints honoured / broken, honestly: language-agnosticism is BROKEN in
v1 — red is classifiable only through pytest, so the arm authors Python or
nothing. The evaluator stays primary (a green authored test cannot certify,
only veto — `parse_evaluation` still requires `goal_met==true`). The
model-writes-bad-tests risk is handled by the controls plus a quarantine:
an authored test is never disarmed, but at 3 behavioural contradictions it
is demoted to advisory and raises a `WarningRecord` — bounding that mistake
at 3 rounds against the 51 the derived check cost.

STILL OPEN: deterministic-mode goals are gated out (the acceptance rung
sits only on interact's exploratory arm); certification by a green test;
whether `derive_goal_acceptance` should be retired once authored coverage
is broad; and non-Python artifacts. Live watchlist for the next long run —
authored/eligible rate, the drop histogram (a `broken`-dominated histogram
is the kill criterion), max retest_count against the 5-and-51 baseline, and
finally: read a finished artifact's `tests/` and ask whether a human would
keep them.

## 23. The exemplar diff in quality_gate — evicted; the seam story needs a redesign (operator, 2026-08-08)

**What existed:** `data_shape_check` ran `validate_data_shapes` inside
quality_gate (since `dec2bde`, 2026-06-10): a deterministic path-by-path
diff of each data file against `DataShapeContract.example` — a minimal
literal instance authored ONCE by the design inference. Its structured
issues merged directly into fix tasks with exact signatures (bypassing the
prose layer, which had once paraphrased them into a 52-round noise loop).

**What it attempted:** kill the dict-vs-list loader/data mismatch class at
the source — real bugs in the era it shipped.

**How it went wrong (hy3, 2026-08-07/08):** the exemplar is a frozen
day-one sketch and the diff was symmetric, so two days of legitimate data
evolution (defense_bonus, heals_for, is_weakness, is_light, is_moonstone,
monsters' recoils_on_light/hidden, boss phase keys) produced 15 of the 27
gate goals as pure noise across two harvest rounds. Compounding defects:

- The one-element-per-list exemplar convention cannot express
  heterogeneous typed lists — every item diffed against the WEAPON
  exemplar, so "declared key absent" findings were FALSE for salves and
  shields, even though the structure PROSE said "plus type-specific
  fields".
- The exemplar is architecture METADATA no fix flow can write (the
  transient_files category error again), and finding text reads as an
  indictment of the FILE — so fixes appeased the checker by mutating
  world.json: `attack_bonus: 0` junk on every non-weapon item, a spurious
  `is_weakness` on the lantern (which then spawned its own coverage
  finding — the checker generating work for the checker).
- Zero of the 27 gate goals added product richness (26 verify-only; the
  one with fixes built checker-serving inspection plumbing whose engine
  rewrite reopened ten goals).

**Ruling:** evicted from the gate (not ported to structural — shape
conformance is a SEAM concern and the seam machinery is itself due a
holistic redesign; see also §5/§18/§19's seam-gate defects). The
`validate_data_shapes` action and `_shape_diff` remain in the codebase,
dormant. When the seam story is readdressed, the requirements this history
teaches: exemplars must refresh from reality (observation-is-declaration),
extra keys are advisory (richness ≠ defect), heterogeneous lists need
per-variant semantics, and any contract a checker enforces must be
WRITABLE by some repair path.

## 24. Claims-vs-behaviour seams are invisible to every current verifier (2026-08-09)

**The evidence.** An informal blind flight (hy3[g] vs the frozen Frontier
anchor — run NOT recorded as a judgement per operator ruling; the run was a
framework testbed with 30 live bounces) measured a 46/47 vs 47/47 checklist
gap alongside a 9-of-10-axes play gap. The delta between those two numbers
is a direct measurement of what our verification does not see. The decisive
defects, none of which any phase flagged:

- **The amulet seam.** `world.json` declares the boss weakness as an item
  whose `type` no equipment slot accepts; both NPCs tell the player to get
  it; the real gate is a different item. Functional testing exercised
  `equip` and saw a clean refusal — a PASS. The seam is between what the
  game SAYS and what the code ACCEPTS, and every probe we run tests the
  code alone. The only probe that catches it: *follow the game's own advice
  to win*. Nobody ever plays the game the way its NPCs describe.
- **`flee` = victory.** A reward-shaped bug: the verifier watches flee
  produce a victory screen and marks flee working. Needs a should-this-
  have-succeeded judgement, which no deterministic check carries and the
  evaluator is never prompted to make.
- **The save round-trip** — the one decisive defect the gate DID file,
  correctly and repeatedly, while it was structurally untestable (relaunch
  unreachable until the close-notice arc landed in the run's final hours).

**The lever, in order:** (1) the capability-checklist brief (§22's deferred
companion) should include the artifact's own claims — NPC advice, help
text, README assertions — as items to EXERCISE AS STATED, not merely as
features to touch; (2) the relaunch arc (landed, validated) makes multi-run
items testable at last; (3) `no_repro_policy: "strict"` should follow (1).

Also from the same flight, cheap and mechanical: the artifact shipped a
one-shot self-patch script, 413 lines of abandoned parallel implementation
nothing imports, and invented `ruff.toml`/`pyproject.toml` keys. A
dead-file/dead-config sweep at quality_gate (files no import reaches,
config keys the tool would reject) is deterministic and needs no LLM.
