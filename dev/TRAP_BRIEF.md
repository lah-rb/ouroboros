# Trap Brief — the deterministic-startup-fail blind-diagnose trap

*For deciding the fix. Date: 2026-06-30. Evidence: thompson-nfa-regex-engine, bplus-tree, cost-based-query-planner traces + the purge-restart A/B.*

## 1. Nature (one line)
A mission whose **deterministic startup check fails** flips its goal into fix-mode and gets **trapped in a terminal-less `diagnose → rewrite → diagnose` loop with no route back to live execution** — so it rewrites *blind*, never running the code to see whether a fix worked, until the wall-clock backstop.

## 2. Evidence (it's real, specific, and server-independent)
- **thompson-nfa (definitive):** 699 `investigate` + 78 `conclude` + 116 rewrites, **0 `plan_interaction`**, **0 `create_session` after cycle 0** (env setup). 74/78 conclusions were "won't import." It re-traced `parser.py:parse` **108×**, rewrote `cli.py` **55×** + `parser.py` **30×** — each fix introducing the next import error, cycle 9→93, **never once executing the program**. Paused 6/14.
- **bplus-tree, cost-based-query-planner:** same shape (`pi=0`, investigate 97–142). In the v3 cleanup all three contributed **0 labelable turns** — the trap self-excludes from the dataset.
- **Server-restart A/B (decisive):** re-ran bplus-tree on a **purged + cold-loaded** server → trapped *again* (`pi=0`, `create_session=0`). **Server-state-independent → it's a flow bug**, not memory/Metal. Consistent with the process-level-rot proof.
- **Frequency:** ~2 of every ~4–6 runs (thompson-nfa, bplus-tree) vs the converging runs (pratt 17/17, buffer-pool 13/13, lsm-tree 19/21, hindley-milner 14/17) that all live-tested.

## 3. Trigger + mechanism
**Trigger:** the greenfield build chose a `src/`-layout package (`src/regex_engine/…`) but the test invocation expects a top-level `regex_engine` import → `ModuleNotFoundError`.

**Mechanism — two compounding gates:**
1. The goal's `interaction_mode == 'deterministic'`, so `flows/code_core/interact.cue` `check_mode` (lines 70–77) routes to **`run_deterministic`** (zero-inference command run), NOT the exploratory `gather_context → choose_charter → plan_interaction → run_session` (charter/PTY live-test) branch. **Live-testing is structurally unreachable for that goal.**
2. The deterministic startup check failed → goal flips to fix-mode → `mission_control` `functional_sweep_next` then chooses `dispatch_functional_fix` for *all* remaining cycles, **never re-dispatching a functional test**. The fix path routes only to **`diagnose_issue`** — which has **no terminal execution**.

Net: the agent can only *statically trace symbols and guess* at the import error, rewrite, re-trace — **diagnosing blind, with zero ground-truth between attempts.** Converging runs (pratt) had goals dispatched **exploratory** → `plan_interaction` → real `run_session` PTY tests → the feedback loop the trapped runs are denied.

## 4. Correction options (with trade-offs)

| | Option | What | Pro | Con | Effort |
|---|---|---|---|---|---|
| **A** *(root fix)* | **Escalate stalled deterministic fix-loops to the PTY/charter path** | After N consecutive `dispatch_functional_fix` without the deterministic check passing, route the next attempt through `interact`/`run_session` so the agent **runs the code and sees if the fix landed** | Directly breaks the trap; restores the ground-truth feedback loop | Flow-routing change (`functional_sweep_next` + a stall counter); adds PTY inference cost to the fix path | Medium |
| **B** *(trigger-side)* | **Layout/import sanity gate at build time** | After greenfield build, verify the package imports the way the test invokes it; fix layout *before* functional tests | Prevents the most common trigger (src-vs-top-level); cheap | Doesn't fix the deeper bug — any *other* deterministic startup failure can still trap | Low |
| **C** *(backstop)* | **Diagnose-budget cap** | Cap consecutive `investigate` cycles without goal progress before forcing a live-test | General guard against any blind-diagnose sink | Heuristic cap; may cut legitimate deep diagnosis; treats the symptom | Low-Med |

## 5. Recommendation
**A + B.** A is the root fix (give the stalled fix-loop ground-truth via the PTY path); B cheaply reduces how often the trigger fires. C only if A's "stalled" detector proves to miss cases.

## 6. Detection (monitoring + dataset hygiene)
The **refined predictor**: trapped ⇔ `plan_interaction` stays ~0 **while `investigate` climbs and goals stall**. NOT `pi=0` alone — early `pi=0` is the normal deterministic-basics phase; a healthy run *transitions* to live-testing once basics pass (hindley-milner: pi 0→438). Wire `investigate : plan_interaction : goal-progress` into marathon health for live flagging + optional auto-skip.

## 7. UPDATE — deeper analysis: it was CHASING ITS TAIL, and the ground-truth existed
A close read of thompson-nfa's CoT + edit diffs (2026-06-30) overturns the "blind, lacking ground-truth" framing:
- **Ground-truth was emitted 117×.** The startup stderr was byte-for-byte identical from t+12m→t+177m and NAMED the broken file: `ImportError: cannot import name 'parse' from 'regex_engine.parser' (.../regex_engine/parser.py)`.
- **The named file was never edited** (0 of 116 edits). It was a 75-byte CORRUPTED STUB — a tool-call blob `{"action":"run","command":"sed -n '1,200p' src/regex_engine/parser.py"}` written as file content (a `generate_rewrite` stub corrupted it ~40 min in; thompson-nfa stub-rate 0.11).
- **116/116 edits hit the WRONG package** (`src/regex_engine/{cli,parser}.py`, 77 edits) which the failing `python -m regex_engine.cli` never imports. It diagnosed by STATIC symbol-tracing, never reading the runtime error, so it mis-localized.
- **Oscillation:** `src/cli.py` import strategy went TRY/EXCEPT → LAZY (80 min) → back to TRY/EXCEPT; `parse`/`parse_regex` alias added+removed on alternating cycles. Net progress = ZERO (113 identical failures).
- **Verdict: chasing its tail.** The trapped runs are genuinely wasted (their data correctly auto-excluded in cleanup).

**Refined fix (supersedes §5's A+B):**
- **A′ (root) — ground the diagnose→fix loop in the deterministic check's RUNTIME ERROR.** Parse the traceback `file:line` and force localization to the file the error names, instead of static symbol-tracing to a different package. (Old "escalate to PTY" gives MORE ground-truth, but the existing ground-truth was already ignored — so it wouldn't fix this.)
- **B′ (trigger) — file-write stub-guard.** Reject writing a tool-call JSON blob as file content (what corrupted parser.py), reusing `contam_forensics.is_stub`.
- Relevant: `flows/code_core/diagnose_issue.cue` `execute_symbol_trace` (the static-trace step that ignores the runtime error); the file-write path (for B′).

## Key files
- `flows/code_core/interact.cue` — `check_mode` (lines 70–77): the deterministic-vs-exploratory routing gate
- `flows/code_core/mission_control*` — `functional_sweep_next` (test-vs-fix dispatch), the gate that stops re-dispatching tests
- `flows/code_core/diagnose_issue.cue` — the terminal-less fix path the trap lives in
- `flows/shared/run_session.cue` — the PTY/charter live-test path (option A's escalation target)
