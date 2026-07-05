# SWE-bench repair scope discipline — pilot 1 → 4 (2026-07-05)

Same 12 gold-verified Verified instances, same gpt-oss-120b-a5 server, same
1200s/20-cycle budget every run. Only agent-side logic changed between pilots,
so the deltas are clean.

## Headline: 1/12 → 5/12

| metric            | pilot-1 | pilot-2 | pilot-3 | pilot-4 |
|-------------------|:-------:|:-------:|:-------:|:-------:|
| **resolved**      | 1/12    | 0/12    | 1/12    | **5/12**|
| completed         | 2       | 5       | 4       | 5       |
| gold-file hit     | 8       | 2       | ~4      | 8       |
| empty patches     | 0       | 5       | 4       | 1       |
| Σ goals (all 12)  | 57      | 42      | ~40     | **12**  |
| Σ +lines          | 19,128  | —       | —       | **756** |

pilot-4 resolves: django-10880, flask-5014, requests-1142, pytest-10081,
sympy-11618 — and every one is SURGICAL: requests f1/+12 (was f8/+16396),
django-10880 f2/+41 (was f21/+476), pytest-10081 f1/+6 (was f10/+286),
sympy f1/+8. Localization never regressed (8/12 hit throughout); the change
converted those hits from over-broad WRONG fixes into surgical CORRECT ones.

## The iteration (each pilot removed exactly one failure class)

1. **pilot-1 (1/12):** baseline. First-pass "localization wall" read was wrong
   — 8/12 found the gold file. Real failure = over-scoped work plans.
2. **Scope discipline v1** (repair decompose prompt + capability_absent=False
   flip + write-guard + module-match test selection). **pilot-2 REGRESSED to
   0/12:** the flip alone routed repair goals to the sweep's default "verify it
   works" interact, which no-ops on SWE-bench's held-out test → 5 empty patches.
   BUT write-guard (scaffold 27→0) and module-match (sympy picks test_point)
   proved correct.
3. **Diagnose-first fix** (a repair fix-goal is a confirmed defect → route to
   diagnose_issue from the problem statement, not verify-interact). **pilot-3
   (1/12):** recovered; flask resolved VIA the new branch. But it only fired
   3/12 — the other 9 were hijacked by two mechanisms treating baseline-failing
   tests as ground truth.
4. **held_out_tests** (the deeper root): in SWE-bench the regression test is
   HELD OUT, so every baseline-failing test is a pre-existing red-herring.
   Fixed the witness rule (a bare nonzero rc is not a witness) and gated the
   test-suite-gate harvest + repair-test loop behind a `held_out_tests` flag
   the adapter sets. **pilot-4: 5/12.** diagnose-first now fires 12/12; goal
   explosion gone (astropy 1 real goal was becoming 9 phantom "fix failing
   test" goals); empties 5→1.

## What each lever did (all landed on main)

- **Repair decompose** (prompt + capability_absent=False): 1 fix goal, not a
  helper/docstring/integration split. Σgoals 57→12.
- **Diagnose-first routing**: repair fix-goal → diagnose_issue from the problem
  statement (confirmed-defect polarity). Restored acting + localization.
- **held_out_tests**: baseline-failing tests are NOT the bug's ground truth in
  SWE-bench. Killed the phantom-goal harvest and the spurious-witness no-op.
- **Write-guard**: no new test/scaffolding files on a repair mission. Scaffold
  files in patches 27→0.
- **Module-match test selection**: prefer test_<module>.py (sympy).

## Remaining (next-look, do NOT block the win)

- **astropy-13033: catastrophic mass-deletion patch** — 20MB, 1264 files
  deleted (the whole astropy package: CITATION, __init__.py, …). A destructive
  op (or corrupted working tree) captured by `git add -A`. Distinct bug class
  (patch sanity, not scope). Lever: a patch-sanity guard that rejects/flags a
  diff deleting a large fraction of tracked files + investigate what the agent
  ran.
- **pylint-4604: the 1 remaining empty patch** (regressed from pilot-1's
  completed). Diagnose-first produced no edit; needs a trace read.
- **astropy-12907: still over-broad** (f7/+313, paused) — hit the file but the
  fix sprawled; the one case where scope discipline didn't fully bite.
- 7/12 still paused at the wall clock — but now paused mid-SURGICAL-fix, not
  mid-sprawl. Budget/localization depth is the next frontier, not scope.
