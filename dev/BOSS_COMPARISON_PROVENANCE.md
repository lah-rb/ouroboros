# Boss-run comparison — provenance and what is actually comparable

Three 7h `game_challenge_boss` runs, quality top phase, intended as a
largest-MoE vs largest-dense comparison. **They did not all run the same
prompt.** Read this before comparing anything.

| run | started (local) | model | goal-derivation prompt |
|---|---|---|---|
| `step37_boss` | 2026-07-25 00:36 | step-3.7-flash 196B MoE | hardcoded goal-count band |
| `step37_boss_adaptive` | 2026-07-25 10:55 | step-3.7-flash 196B MoE | hardcoded goal-count band |
| `mistral_boss2` | 2026-07-26 02:35 | mistral-medium 128B dense | COVERAGE contract |

`8a695eb` ("functional goals are set by the objective's scope, not a hardcoded
band") landed **2026-07-25 14:40**, i.e. *after* both step37 arms and *before*
mistral.

## What this breaks

**Goal counts are not comparable across models here.** mistral derived 8
structural + 26 functional; the step37 arms derived ~5-6 structural + 8
functional. That gap is the prompt change, not a model difference — the old
prompt anchored the count, the new one asks for coverage at sensible
granularity. Any "N/M goals complete" comparison between these runs is
measuring the prompt.

## What still holds

**The blind panel is unaffected.** It plays the finished game and scores the
artifact; it never sees the goal ledger. Since the established lesson is
already that goal counters are not the instrument (they inverted against blind
play in earlier rounds, and overstated the gap even when directionally right
in the 07-25 panel), the comparison Luke actually wants — "compare the
products of the largest MoE and largest dense models" — survives intact.

**Use the panel. Do not quote the counters.**

## If a strict A/B is wanted later

Re-run step37 under the current prompt. Until then this is a
model-plus-prompt comparison on the artifact axis only, and honest reporting
should say so.

## Other per-run caveats worth carrying

- `step37_boss_adaptive` lost ~50 min of its 7h to probe SIGSTOPs (the clock
  keeps running while the process is frozen), landing in its functional phase.
- `mistral_boss2` is the SECOND attempt; the first died at minute 5 on a
  transient "busy" (OPEN_TASKS §10) and produced nothing.
- mistral prefill measured ~30 tok/s vs gpt-oss's 113 tok/s aggregate decode
  on the same box — the dense model is prefill-bound, which is the concrete
  form of "hampered by the framework".
