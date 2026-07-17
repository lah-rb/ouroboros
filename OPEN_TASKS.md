# Open Tasks — Delegation Brief

*Deferred work with enough context to pick up any item cold. Written
2026-07-16 at the end of the great cleanup week. Ordering reflects the
agreed timing: gates first, score levers next, soak-gated and background
items last. Update this file as items land or close.*

## Standing constraints (read before touching anything)

- Explicit `git add <files>` only — never `-A`; commits end with the
  Co-Authored-By trailer of whoever/whatever authored them.
- NEVER bare `uv sync` (prunes out-of-band packages: terminal_bench,
  torch, mlx, the editable ~/Repos/tau-bench install). `uv pip install`
  or `uv sync --inexact`.
- llmvp tests run under llmvp's OWN venv: `cd llmvp && .venv/bin/python
  -m pytest tests/`. Main suite: `uv run pytest tests/` (1390 green as of
  writing; llmvp 228).
- Verification fence per change: both suites + `ouroboros.py lint-flows`
  (0 errors / 1 standing advisory) + `smoke` (38/38) + `cli-smoke`
  (16/16) + black/ruff clean. `flows/compiled.json` commits force all
  touched .cue sources into the same commit.
- After ANY benchmark/server work: restore the production LLMVP config
  (`llmvp/active_config.txt` → `gpt-oss-120b-a5`) and restart.
- Test-writing rules: `TESTING.md`. Repo layout: `CONTRIBUTING.md`.
- Server restarts are cheap and pre-approved when a soured/wedged server
  is burning hours (SIGSTOP mission processes → SIGTERM server →
  relaunch → SIGCONT; the agent retry loops ride through).

## 1. Boss-game A/B analysis — ROUND 1 CALLED 2026-07-17, ROUND 2 RUNNING

Round 1 (`/tmp/gameab/bossgame_{adaptive,baseline}`, 32.4h each, PARKED
and preserved) is NOT a clean adaptive-thinking A/B: postmortem (see
commit 73084a6) found both arms ran the entire time on pre-fix code
(process start predates 351092e/3f2686f; 854/854 fix prompts had empty
error slots) and each wedged in a structural trap — adaptive on the
lint-E402 vs fossil-import two-gate conflict on engine.py (433 reports),
baseline on world.yaml exit-reciprocity whack-a-mole (one defect visible
per prompt). Round-1 data remains useful for TOKEN accounting only
(adaptive 1.63M gen / 3,071 cycles vs baseline 1.33M / 3,528 — same
outage-window caveats as before).

Round 2 launched 2026-07-17 ~08:20 in `/tmp/gameab/bossgame2_*` on code
73084a6 (gate-output threading, sibling-goal context, create-loophole
backfill all live; cmd_start now logs the code SHA). Deliverable when it
finishes: prototype quality head-to-head, tokens by arm/step/reasoning
level, cycles, wall; plus the round-1 vs round-2 wedge comparison (did
sibling context kill the oscillation; did threaded gate output break the
two-gate loops). Afterwards: restore production a5 config; update the
adaptive-reasoning memory; consider merging this branch.

## 2. Drain-refresh follow-through

Context: `llmvp` timed context auto-refresh with drain
(`context_refresh_drain_s`, commit `8073353`; incident history in the
staleness memory). The FIRST drain firing (2026-07-16 18:12) threw
somewhere after phase-1 and died silently; the loop is now hardened to
log the traceback and continue (commit "proactive-refresh loop must
survive"). Work:
(a) When the traceback appears in `llmvp/logs/server_stdout.log`
    (grep "refresh attempt failed"), diagnose and fix the root cause.
    Prime suspects: `engine.pause()` timeout on the parked decode
    thread, or `_rebuild_batched_context` under the new seq/token
    refactors.
(b) Tier-2 (ONLY if telemetry shows drains force-expiring sessions —
    if phase-1 keeps clearing, skip): server-side session replay across
    refresh, rebuilding live sessions from token history onto fresh
    seats (the snapshot cold-tier pattern) instead of expiring them.

## 3. Powered TB canary with the adaptive config (+ context-budget A/B)

Run the 8-task TB2 canary (`dev/canary_tb2_gptoss.sh`) with
`OURO_ADAPTIVE_REASONING=1` against the production a5 config
(reasoning_head_swap is enabled there) vs a kill-switch baseline arm.
This is the shipped router's first benchmark measurement. Fold in the
deferred context-budget experiment: a third arm (or follow-up pair) with
`prepare_context` budgets reduced for fix-mode rewrites — the 26k-token
rewrite prompts (19k file + 112k chars packed context) were the KV-
collision trigger; Luke wants budget reduction validated carefully, not
assumed. Compare pass rate + decode tokens + prompt sizes.

## 4. SWE-bench: issue-guided retrieval (the big score lever)

Pilot verdict (memory: swe-bench-verified-pilot): 1/12, with 10/12
failures wall-bound on repo-scale localization — the agent burns its
budget finding WHERE to edit. Build issue-guided retrieval: use the
problem statement to rank/prefetch candidate files+symbols (repomap
PageRank + lexical/embedding match against the issue text) and seed the
diagnose/ingest path with them. Evidence: `runs/swe/` per-instance
traces; `dev/swe_taxonomy.py` classifies failures. Success = localization
time collapses on the pilot set; rerun the 12-instance pilot
(`dev/swe_eval.sh`, predictions archive in `dev/archive/swe_reports/`).

## 5. TB2 oracle improvements (chunkable)

`dev/ORACLE_IMPROVEMENTS_PLAN.md` — evidence-backed items from the 87
TB2 failures: (a) retry-DIFFERENTLY on give-up loops (same dead end
re-reached is the signature); (b) self-check for wrong-value-right-
artifact; (c) blind-early derivation fixes (derive_output_format emits
action JSON ~69% empty); (d) fabrication guard on the judge. Each is
independent; good interleave work between item-4 milestones.

## 6. TRAP_BRIEF re-validation (cheap; do before any investment)

`dev/TRAP_BRIEF.md` documents the deterministic-startup-fail
blind-diagnose trap. ALL its evidence predates the featurizer bare-<
fix, which was the actual root cause of several "trapped" runs. Rerun
one canonical trap task (thompson-nfa-regex-engine or bplus-tree
greenfield mission) post-fix. If the trap no longer reproduces, stamp
the doc CLOSED and archive it; if it does, the fix is wiring the stalled
fix-loop detector to the (now shipped) escalate flow.

## 7. Test-suite consolidation roadmap (background)

`TESTING.md` bottom table (~140-test reduction while broadening).
Priority order: introduce `tests/conftest.py` + collapse the migration
family (4 files, 6 copies of ScriptedInferenceEffects, 12 dup fixtures);
llmvp API-layer tests (graphql/rest at 0%); the reasoning-strip test
that every session test currently disables (`LLMVP_THINK_STRIP=0`);
then the parametrize tables (turn_renderer, turn_models, oracle files).
One dedicated short session for the first two; the rest opportunistic.

## 8. generate_stream_sync request-prep extraction (SOAK-GATED)

The last "giants" item: shared `_prepare_stream_request()` for the
static/dynamic split + stop defaulting + kv_base + tracker.start that
`generate_stream_sync` and `_batched_stream` both do. DO NOT start until
the token-pipeline + seq-layout + drain-refresh changes have soaked
under real load for several days without incident — that file absorbed
four surgeries in one week.

## 9. Small items (grab-bag)

- Batched-engine watchdog-cancel seat leak (memory: debate-vs-cot,
  "watchdog cancel leaks server-side seats") — reproduce via a cancelled
  session turn, check seat accounting.
- Agent-side identical-retry backoff: the KV-eviction and anti-gut loops
  both retried the same dispatch unchanged for hours. Auto-refresh bounds
  the souring case; a dispatch-level "same goal+flow failed N× in a row →
  backoff/escalate" guard bounds the class.
- Standing lint advisory: `add_symbol.generate_new_symbol` string-match
  condition (arguably a linter over-trigger — either exempt emptiness
  checks in flow_lint or convert the step).
- mission.json archival (memory: memory-hardening-audit, still open).
- Qwen3-Next resident-cache validation + windowing follow-ups (memory:
  resident-seq-cache-implemented).

## 10. Parked until triggered (do NOT start unprompted)

- 615-turn quarantine panel (K=7 highs + splits) under
  `dev/JUDGE_STANDARD.md` + the 250-turn post-featurizer-fix
  regeneration calibration. Trigger: wanting to grow/re-train the
  reasoning router (provenance chain preserved in dev/ for exactly this).
- Context-refresh stub-rate auto-trigger (the timed cap + drain covers
  the operational need; a signal-based trigger is a refinement).
