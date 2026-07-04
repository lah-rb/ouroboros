# Escalation primitive — one recovery shape for every devolution branch

Status: design brief (2026-07-03, from the B.5 retest post-mortem discussion).
Not scheduled; queued behind the ops write_file action and Phase C.

## Problem

Flows accumulate bespoke "something went wrong mid-flow" branches, each with a
hardcoded single fallback chosen at the time the branch was written. These age
badly and devolve:

- `file_ops.self_correct` → **whole-file rewrite** (chosen just after symbol
  edits landed, when rewrite was the only tool; burned 2×102s regenerating a
  19KB qdp.py chasing a pre-existing smoke failure, and the regeneration is
  where the py3.9-breaking annotation entered).
- `design_gate` 3-strikes → **terminal mission failure** (killed the langcodes
  mission at 175s with 1025s of budget left, before the goals-present rule).
- ops `retry_setup` → controller re-loop (removed in the ops pruning).
- The deterministic-startup blind-diagnose trap (memory: terminal-less
  diagnose/rewrite loop with no route back to live execution).
- Parse-floor / anti-gut rejections → refuse the write and hope the invoking
  turn does better next time (no active recovery at all).

Each is the same event — *the deterministic pipeline hit a wall* — answered
with a different, fixed, often-stale reflex.

## The primitive

One shared **escalation flow**, invoked at any devolution point, with a typed
return contract that REJOINS the invoking flow's normal progression — the
defining constraint that separates it from "just another flow":

```
escalate(input):
  invoking_flow, step        # where the wall was hit
  failure_evidence           # check output, rejection reason, error text
  expected_outputs           # the contract the invoker needs satisfied
  budget                     # turns + wall-clock slice
→ returns ONE of:
  resolved(outputs)          # deliver the invoker's expected outputs; invoker
                             # continues as if its own step had succeeded
  amended(restart)           # something was changed (env, file, config);
                             # restart the invoking flow fresh
  deferred(report)           # couldn't fix within budget; a structured report
                             # flows to mission_control (never a dead mission)
```

Three hook-back modes, matching the contract: flow back to mission_control
(deferred), deliver the invoker's outputs (resolved), or change-and-restart
(amended). The invoker's cue declares which modes it accepts.

## Body: a bounded REACT loop over a small tool menu

The proven shape already exists — `diagnose_issue`'s trace-and-conclude loop
(memoryful session, compound menu, turn budget, typed conclusion) is a REACT
solver in house dress. The escalation body generalizes it:

- **Menu** (per-site subset, declared in the invoking cue): read_file,
  run_command, trace_symbol, guarded write_file (anti-gut + parse floors
  apply), exa web_search (the stuck-search one-shot discipline), consult
  (second-opinion model — the curator's gemma pattern), conclude(resolved |
  amended | deferred).
- **Seed**: the failure evidence + the invoker's expected-output contract —
  facts only, diagnose-seed style.
- **Budget**: hard turn cap + the one-shot rules (one web search, one consult)
  so escalation can't itself devolve; on cap → deferred, never a loop.

## Design rules (from the B.5 lessons)

1. **Block verdicts, not attempts.** Stand-down guards (parse floor, smoke
   baseline, collection floor) stop false blame; escalation is where blocked
   ACTIONS go to get retried differently. No escalation exit may be a dead
   mission — worst case is `deferred` + report.
2. **Baseline-aware evidence.** The seed must say when a failing signal
   pre-dates the attempt (the unbuilt-astropy class) so the solver doesn't
   appease unappeasable checks — the exact failure the CoT showed ("package
   not built; we cannot build extensions" … proceeds to rewrite anyway).
3. **Tools are the existing guarded primitives**, never raw power: writes go
   through guarded_write_file, searches through the exa gate, terminal through
   the PTY session machinery.

## First adoption sites (in order of measured pain)

1. `file_ops.self_correct` — replace the rewrite-flow reflex entirely.
2. Parse-floor / anti-gut / TOML rejections — today they only refuse; escalate
   with the rejection as evidence.
3. `design_gate` repeated rejection (residual paths).
4. ops `judge`-fail loops (partial — ops already has tiered anti-give-up; the
   escalation would replace the tiers' bespoke plumbing eventually).

## Non-goals

- Not a replacement for diagnose_issue (that's goal-level work planning;
  escalation is step-level unblocking with a return contract).
- Not autonomous scope expansion: the menu is site-declared and small.
- No new engine machinery expected: flow-invoke + returns already carry
  sub-flow contracts; `amended(restart)` needs the most care (idempotency of
  the invoking flow's entry).
