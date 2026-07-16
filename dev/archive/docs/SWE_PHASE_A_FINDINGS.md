# SWE-bench Phase A findings — 2026-07-03

> **STATUS: BANKED 2026-07 — Phase A findings folded into the program plan; first solve landed (langcodes). Program continues; see memory: swe-bench-phase-a-first-solve.**

Re-baseline of the TB1 SWE quartet at HEAD (clobber fix + ops pruning + the
five code_core ports). Analysis: `dev/swe_taxonomy.py runs/hh-ops runs/hh-code_core`;
June baselines preserved at `runs/hh-{ops,code_core}-20260623`.

## Grader ceiling (A1)

`runs/oracle-swe4`: **4/4 gold solutions resolve** (astropy-1, astropy-2,
fsspec, langcodes). Every failure below is on the agent. (First attempt died
on a transient Docker-registry stall overnight — not reproducible.)

## Results (A2)

| task | ops 06-23 | ops HEAD | code_core 06-23 | code_core HEAD |
|---|---|---|---|---|
| langcodes | timeout ✗ | **RESOLVED ✓** (730s, 41 inf) | clobber parse_error | ✗ tests ran, no patch (175s, 8 inf) |
| fsspec | timeout ✗ | timeout ✗ (101 inf) | timeout ✗ | parse_error (agent corrupted pyproject.toml) |
| astropy-2 | parse_error | timeout ✗ (83 inf) | parse_error | timeout ✗ (40 inf) |

## Retest (post seal + dynamic stop + brownfield fixes 2–4), 2026-07-03 PM

Baselines preserved: `runs/hh-{ops,code_core}-2026{0623,0703}`.

| task | ops RETEST | code_core RETEST |
|---|---|---|
| langcodes | ✗ timeout (117 inf) — nondet flip | **RESOLVED ✓** (886s, 64 inf) |
| fsspec | ✗ timeout (81 inf) | ✗ timeout (63 inf) — parse_error GONE |
| astropy-2 | parse_error (astropy conftest) | parse_error (astropy conftest) |

**Headline: code_core's FIRST SWE-bench solve (langcodes).** Mission trace
proves the brownfield path end-to-end: interact → diagnose_issue →
file_ops on `langcodes/__init__.py` (the REAL source) → interact re-test,
structural goal complete, `test__hash__ PASSED`. No design-gate trap (fix 2),
no clobber, no arch-parse death (fix 3). The 07-03 early-exit (8 inf / 175s)
is closed.

**fsspec: parse_error → clean timeout on BOTH arms.** The scaffold parse floor
+ protect_existing (fix 4) held — the agent no longer corrupts pyproject.toml;
it now simply runs out of time. Remaining blocker is repo-scale localization
(Phase B.5), not scaffolding.

**astropy-2 parse_error is NOT our regression.** Cause:
`AttributeError: module 'builtins' has no attribute '_xdg_config_home_orig'`
in astropy's OWN `conftest.py::pytest_unconfigure` — its teardown hook crashes
when a test session is interrupted before setup completed. Astropy is the
hardest task (big repo, py3.9 source build) and is localization-bound anyway;
the gold patch resolves clean (oracle 4/4), so this is an interrupted-run
teardown artifact, not agent damage.

**ops/langcodes regression = nondeterminism, confirmed.** seed=-1; the 07-03
solve was itself a marginal timeout-boundary pass (729s, flagged
agent_timeout). Retest turns are clean (117 turns, mean 283 tok, ZERO
zero/sub-5-token turns) and NO degeneration aborts fired in the window — so
the seal/stop did not truncate anything. The seal is extraction-only and the
stop only ends generation EARLIER, so neither can raise inference count; this
is the model taking a longer unsuccessful path. ops does not get the
brownfield fixes.

**Scorecard:** code_core 0/3 (all broken by clobber/trap/corruption) → 1/3
clean solve + 2 legitimate non-regression failures. Small-repo editing
competence proven on both arms; the wall is repo-scale localization.

**Telemetry gap found:** `gen_end_reason = "final_channel_close"` is
overwritten by `"completed"` before logging, so the dynamic stop's firing
isn't observable in the server log (0 hits despite clean turns). One-line fix
pending (needs a restart to take effect).

**First SWE-bench resolution ever** (ops/langcodes — `test__hash__ PASSED`).

## What improved

- **Clobber-regen is gone.** code_core/langcodes: the grader ran the REAL
  repo test against the REAL pyproject (June: agent-regenerated
  pyproject-0.1.0 with cov addopts choked pytest). The brownfield gate works.
- **Cache economics transformed.** Hit rates 0.03–0.22 → 0.72–0.90 across
  every task; ops/langcodes inference count 158 → 41.

## New defect list (Phase B priorities, in order)

1. **[CORRECTED 2026-07-03] "Degeneration" = post-answer rambling, not model
   failure — and it was on astropy-2, not langcodes.** Deep-dive (runaway
   capture `llmvp/logs/runaway_captures/20260703T152322_106343.json`): in a
   diagnose menu turn the model produced a clean CoT ("Now inspect
   _line_type…") and a COMPLETE, correct final channel
   (`{"choice": "trace", "symbol_ref": "astropy/io/ascii/qdp.py:_line_type"}<|end|>`)
   — then generation was allowed to continue (Harmony stops are only
   `<|return|>` + fake-user opener, by design — see renderer.py stop_tokens
   design note re the 91% analysis→final reopen pattern), chained an empty
   analysis + a second final, began HALLUCINATING the observation it
   expected next (the `_line_type` docstring), and looped on the RST
   ``!``-style double-backticks inside it (token 26178 = '``', run 48).
   The guard then errored the WHOLE turn — discarding the perfect answer
   already in the buffer. Server-side session recovery was clean (purged
   turn span back to pos 6766); server temp floor was active (0.35 over the
   client's 0.0). The agent loop did NOT die — diagnose treated it as a
   junk turn and concluded with an empty target.
   Fixes (LLMVP, not the agent loop):
   a. **Salvage on guard abort**: when the repetition guard fires, run FSM
      extraction over the captured text; a complete final channel = return
      it as a successful turn (log the anomaly). Zero-risk net.
   b. **FSM-driven session stop**: stop generation when the FIRST final
      channel closes (`final … <|end|>`) in session mode — stateful stop at
      the labeller layer, NOT a `<|start|>assistant` substring stop (that
      was the e75 46%-empty regression). Saves the wasted decode and the
      self-poisoning ramble entirely.
   c. (Defensive, agent) retry a menu turn once on an inference error.
2. **Brownfield still re-designs — root is the ingest parse failure (#3).**
   Chain on langcodes: extract_architecture parse DIED on the pydantic
   error → no adopted architecture stored → phase machine saw greenfield →
   design_and_plan produced a near-empty blueprint ("Modules list empty,
   files exist on disk the blueprint does not declare") → design_gate
   correctly rejected 3× → terminal exit at 175s with 1025s left. Fix both
   ends: (a) coercion so ingest parse succeeds (#3), (b) structurally,
   a brownfield mission (pending_directive set / ingest entry) must never
   fall into blueprint design+gate — retry ingest or dispatch goals
   architecture-less.
3. **extract_architecture type coercion.** Pydantic:
   `InterfaceContract.symbol — Input should be a valid string, got list`
   (model emitted a list of exported symbols). Coerce list → joined string
   (and similar lenient shapes) instead of failing the ingest.
4. **Scaffolding-edit floor.** fsspec: an in-place pyproject.toml edit left
   invalid TOML (line 56) → grader pip install died → parse_error. After any
   edit to a parseable config (toml/yaml/json/cfg), a deterministic
   parse-check rung must fail the write (same family as the boot-liveness
   floor). The June regen-clobber and this are the same lesson: repo
   scaffolding is load-bearing for the grader.
5. **run_session inference volume** (ops arm): 66–83 turns on astropy/fsspec
   before timeout. Repo-scale tasks need the localization work (Phase B.1)
   plus per-turn economy; the 0.9 cache hit rate means these turns are cheap
   per-token now — the cost is decode volume and turn latency.

## Notes

- `dev/datacollect_tb1.sh` does `rm -rf` on its run dir — relocate prior runs
  before re-running an existing run-id (June baselines were moved to
  `runs/hh-*-20260623` seconds before deletion). Consider a timestamp suffix.
- ops/langcodes shows `failure_mode=agent_timeout` yet `is_resolved=True` —
  the wall-clock cap expired mid-run but the container state already passed;
  timeout is not synonymous with loss.
