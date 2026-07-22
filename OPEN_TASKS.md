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
  -m pytest tests/`. Main suite: `uv run pytest tests/` (1518 green as of
  2026-07-21; llmvp 265).
- Verification fence per change: both suites + `ouroboros.py lint-flows`
  (0 errors / 1 standing advisory) + `smoke` (44/44) + `cli-smoke`
  (19/19) + black/ruff clean. `flows/compiled.json` commits force all
  touched .cue sources into the same commit.
- After ANY benchmark/server work: restore the production LLMVP config
  (`llmvp/active_config.txt` → `gpt-oss-120b-a5`) and restart.
- Test-writing rules: `TESTING.md`. Repo layout: `CONTRIBUTING.md`.
- Server restarts are cheap and pre-approved when a soured/wedged server
  is burning hours (SIGSTOP mission processes → SIGTERM server →
  relaunch → SIGCONT; the agent retry loops ride through).

## 1. Boss-game A/B analysis — CLOSED 2026-07-21 (round 2 complete)

Both arms COMPLETED (baseline 28/28 goals, adaptive 73/73). Blind
pinned-Opus panel on the finished games: **adaptive 16/25 (judge completed
a genuine winning run; robust, save/load + dialogue + equip all real) vs
baseline 8/25 (unwinnable — boss subsystem disconnected from data; crashes
on bad input)**. Router at scale: 33% of 11,085 calls routed low, high on
14 deliberations only, mean decode/call 15.6s vs 17.6s on a harder
mission; per-goal cost ~identical (75k vs 79k OUT/goal). VERDICT: adaptive
does not cost quality vs flat medium → **adaptive is the default for
further testing.** Full numbers in the reasoning-injection memory. a5
production config restored + server restarted (tracker fix deployed)
2026-07-21. Branch merge decision still open.

## OLD-1 (superseded) — ROUND 1 CALLED 2026-07-17

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

## 2. Drain-refresh follow-through — CLOSED 2026-07-21 (by evidence)

Zero "refresh attempt failed" since the hardening; 27h+ uptime through the
heaviest workload to date (n=symbols fan-outs, capacity benches, config
swaps) with proactive-timed-drain cycling cleanly every ~30 min. Tier-2
(session replay across refresh) SKIPPED per its own gate — phase-1 keeps
clearing.

## OLD-2 (superseded)

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

## 3. Powered TB canary with the adaptive config — COMPLETE 2026-07-21

**Adaptive 2/8 (first-ever winning-avg-corewars pass) vs flat 1/8.**
Near-miss autopsy 2026-07-22: path-tracing 4/5 (98%-similarity fingertip,
capability), mteb 1/2 (wrong-value false-done → verify_completion
sharpened), chess-best-move 0/1 ×2 (answer-profile routing trap → router
override shipped, see item 5), portfolio 1/4 (wall-bound C-extension
build). The context-budget arm remains DEFERRED. Next decision: full-89
run (adaptive single-arm) vs the 18.7% Terminus baseline — worth doing
after the routing/oracle fixes get a mini-canary.

## OLD-3 (original brief)

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

## 5. TB2 oracle improvements — CLOSED 2026-07-22 (shipped + canary residue fixed)

Delta-audit found all four plan items (a-d) already implemented (see the
STATUS block atop `dev/ORACLE_IMPROVEMENTS_PLAN.md`). The 2026-07-21
canary near-misses exposed two residual classes, both fixed:
**answer-profile routing trap** (chess-best-move ×2: profile=answer routed
code_core → burned the ~14-min budget mid-pipeline, forfeited the ops-only
oracle chain; now deterministically downgraded to ops in conclude_route)
and **selection-answer false-done** (mteb: computed-wrong "5th highest"
value passed the fabrication check; verify_completion now demands the
visible ranking + selection rule). Validation still owed: rerun
chess-best-move + mteb-retrieve once the server is free (expect chess to
route ops now). Known-unaddressed: path-tracing numeric fidelity
(capability), wall-bound builds (pace).

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
