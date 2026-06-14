# State-management experiment: full-replay vs save/load sessions

**Date:** 2026-06-13 · **Model under test:** gpt-oss-120b-a5 (plain transformer, sparse MoE)

## Question

LLMVP runs *memoryful* inference sessions (the gate's behavioral playthroughs,
the interact tests). Two ways to carry KV state across turns:

- **save/load** — `save_state`/`load_state` splice the KV per turn (+ tail
  `seq_rm`). Incrementally cheap: each turn prefills only the new tokens.
- **full-replay** (`session_full_replay`) — restore the pristine static
  snapshot each turn and **re-prefill the whole token history**. The one
  rollback every architecture supports; never serializes KV state.

full-replay was added as the Qwen3-Next (hybrid/recurrent) mitigation, where
save/load is *unsound*. The question here: **what does full-replay cost a plain
transformer**, and is it worth making the default?

## How we got here (the chain)

1. **SOUL.md anti-placeholder nudge.** A 4-sentence "Real over placeholder"
   rule was added to the persona after gpt-oss was caught shipping placeholder
   command handlers (`use X → "nothing happens"`) that its own quality gate
   rubber-stamped (commit `7159779`).
2. **Reopen test.** The completed game_challenge mission (36/36) was reopened
   under the new persona. The gate that previously **passed** now **failed**,
   harvested the real command-recognition defects, the agent **fixed them for
   real** (0 reopened across re-gates), and it converged to a genuine
   `gate_pass` at 43/43. Verified by hand: `use key` now *unlocks a door*
   instead of "nothing happens". → `data/ab/replay-point/` is that patched game;
   `data/reopen_run.log` is the run.
3. **The bug surfaced.** During the reopen the gate's deep UX-eval inferences
   threw `SystemError: Negative size passed to PyBytes_FromStringAndSize`,
   inside `session_manager.session_turn → llama_cpp.save_state`. That motivated
   this A/B.

## A/B method

Both arms start from the **identical** archived replay-point (the 43/43 patched
game) and get the **same** injected directive — *"add a room with a boss gated
behind a puzzle"* — differing only in `session_full_replay`. Driver:
`dev/ab_session_test.sh` (restore replay-point → toggle flag → restart server →
inject directive + reopen → run with traces under 100-cycle / 2h caps → archive).
Measured by `dev/ab_trace_compare.py` from the runtime traces. Full table:
`ab_comparison.txt`. Both arms **capped at ~2h churning the boss-room task**
(gpt-oss kept re-planning it; neither completed it) — so this is a
*cost-under-equivalent-work* comparison, not time-to-ship.

## Results

| metric | save/load (A) | full-replay (B) | Δ |
|---|---|---|---|
| total wall | 7,445 s | 7,240 s | −2.8% (both capped) |
| wall / cycle | 46.8 s | 45.0 s | −4% (equivalent) |
| session-inf wall (mean) | 7,712 ms | 8,827 ms | **+14.4%** |
| session-inf prefill (mean) | 732 tok | 748 tok | +2% (flat) |
| session-inf prefill (**P90**) | 1,407 tok | 2,032 tok | **+44%** |
| `save_state` overflow errors | **1** | **0** | reliability |

## Conclusions

1. **Wall-clock neutral.** Total time and per-cycle time were equivalent
   (full-replay marginally faster here). The scary cycle-median gap (58 ms vs
   14 s) was a *median artifact* — both arms split ~50/50 fast-routing /
   slow-inference cycles with near-identical slow-cycle medians (61 s vs 65 s).
2. **Cost is real but small and tail-concentrated.** full-replay re-prefills
   the transcript, so per *session* inference it's ~14% slower on average and
   **+44% prefill at P90** (deep multi-turn turns). The mean is flat — shallow /
   single-turn inference (the bulk of the framework) pays nothing. The slower
   inferences just mean fewer run in the same window, so total work self-balances.
   Cost scales with session depth; a deeper-session workload would widen the gap.
3. **Reliability win.** full-replay never calls `save_state`, so it cannot hit
   the overflow.

### The overflow, explained

`save_state` serializes the session's **used** KV cache; size grows with depth.
On gpt-oss-120B the serialized blob crosses an **~2 GB integer boundary** in the
llama-cpp-python save path around **~40 turns** of this game's context. It's a
knife-edge, not "deep = doomed": the failing UX session was **41 turns**; a
sibling at **39 turns succeeded**. The deepest session is both the most likely
to overflow and the most redundant.

### Why the failing round wasn't wasted (gate robustness)

The gate is a multi-signal pipeline, and finding-synthesis is *decoupled* from
the live session:

- The 4 behavioral sessions per the experiment run **near-identical explore
  charters** — redundant coverage, no unique detail in any one.
- `evaluate_ux_session` is a turn *inside* the session (calls `save_state` →
  overflowed). But the step that actually feeds the verdict, **`summarize`**, is
  a **standalone inference over the recorded play observations** — it never
  touches `save_state`. It produced the findings *with repro steps* fine. The
  failed eval was a lost second opinion, not lost data.
- Worst case the gate records unverified aspects as explicit `untested:`
  findings → goals. It degrades into honest coverage gaps, never a false pass.

### Recovery cost of an error

Graceful degradation, **not** a retry loop: the poisoned session is force-closed
and the gate proceeds. Time sunk = the wasted generation(s) only — ~9–11 s per
error (1 wasted session-inference), **<1% of a 2 h run** (reopen: ~31 s over 3
errors). The real cost is the *lost eval signal* (quality), which the redundancy
above absorbed.

## Decision

**`session_full_replay` is now the default (`True`)** for safety — it sidesteps
both the recurrent-model unsoundness and the `save_state` overflow at ~free
wall-clock cost. **save/load is the opt-in fast path** (`session_full_replay:
false`) for plain transformers running shallow sessions where the minor speed
gain matters. (`llmvp/core/config.py`, `session_manager.py`.)

## Contents

```
ab_comparison.txt                 full ab_trace_compare output
data/  (gitignored — local archive)
  ab/replay-point/                the 43/43 patched game (reopen result) + .agent traces
  ab/arm-save_load/               save/load arm: boss-room game + trace + run.log + meta
  ab/arm-full_replay/             full-replay arm: same
  ab/orchestrator.log             A/B driver log
  reopen_run.log                  the SOUL-nudge reopen run (full agent log)
```

Tooling: `dev/ab_session_test.sh`, `dev/ab_set_replay.py`,
`dev/ab_inject_directive.py`, `dev/ab_trace_compare.py`.
