# SWE-bench Phase A findings — 2026-07-03

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

**First SWE-bench resolution ever** (ops/langcodes — `test__hash__ PASSED`).

## What improved

- **Clobber-regen is gone.** code_core/langcodes: the grader ran the REAL
  repo test against the REAL pyproject (June: agent-regenerated
  pyproject-0.1.0 with cov addopts choked pytest). The brownfield gate works.
- **Cache economics transformed.** Hit rates 0.03–0.22 → 0.72–0.90 across
  every task; ops/langcodes inference count 158 → 41.

## New defect list (Phase B priorities, in order)

1. **Agent loop dies on a degenerate inference.** gpt-oss hit the repetition
   guard ("GraphQL inference errors: run-length 48 of token 26178") on
   code_core/langcodes and the loop EXITED at 175s with 1025s of budget left
   and the correct functional goal already derived. The loop must treat a
   degeneration abort as a retryable turn (re-roll / temperature nudge /
   session refresh), never a run-ender. Cheapest, highest-leverage fix —
   this task was one patch away from a likely second solve.
2. **Brownfield still re-designs.** After ingest + replan derived the right
   goal, design_and_plan ran (6 inferences) and design_gate rejected
   blueprints twice ("Import scheme 'flat' conflicts…"). Brownfield missions
   should skip blueprint derivation/gating for the adopted architecture —
   dispatch the functional goal directly.
3. **extract_architecture type coercion.** Pydantic:
   `InterfaceContract — Input should be a valid string, got list`. The
   brownfield architecture parse needs lenient coercion (list → joined
   string) instead of a validation error mid-ingest.
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
