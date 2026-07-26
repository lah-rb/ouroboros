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

## 2. TRAP_BRIEF re-validation (cheap; do before any investment)

`dev/TRAP_BRIEF.md` documents the deterministic-startup-fail
blind-diagnose trap. ALL its evidence predates the featurizer bare-<
fix, which was the actual root cause of several "trapped" runs. Rerun
one canonical trap task (thompson-nfa-regex-engine or bplus-tree
greenfield mission) post-fix. If the trap no longer reproduces, stamp
the doc CLOSED and archive it; if it does, the fix is wiring the stalled
fix-loop detector to the (now shipped) escalate flow.

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

Next actions live in that file's §8. Cheapest first rung: **run the owed
pairwise panels on the quarantined highs** — `dev/label_quarantine_v1.json`
holds **84** of them (52 unanimous 3/3, 31 split, 1 short-votes), not the 13
of the early pilot. Certifying even the 52 unanimous ones breaks the two-class
ceiling before any new collection.

**BLOCKED on a corpus re-run (2026-07-26):** the pairwise protocol needs
final-channel actions, and the stored candidates were mutilated by the
extraction bug fixed in 59a6d65 — 93% of quarantined highs have no final
channel at all. The regeneration proved the fix (4,107/4,107 usable) but its
LEVEL dimension is void: rawCompletion silently dropped `reasoning` (fixed in
0297fa1), so all three "levels" were identical generations. Re-run
`dev/cf_regen_swarm.py --run` now that the field is honored, THEN panel.

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

## 11. Shared prefix cache — the biggest untapped serving lever

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
speed one). `dev/prefill_ceiling/bench.py` already measures cold prefill and
asserts `cachedPrefixTokens ~= 0`, so it is the natural base to extend.

### 11b. Revisit flow_kv_cache — built before we understood seq shifting

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
