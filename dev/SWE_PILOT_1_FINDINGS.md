# SWE-bench Verified pilot 1 — 2026-07-04

First official-harness result. 12 gold-oracle-verified instances (9 small-repo
+ 3 django/sympy scouts), code_core repair, gpt-oss-120b medium, 20-min /
40-cycle budget each, sequential. `runs/swe/swe-pilot-1/`.

## Result: 1/12 resolved (8.3%) — pallets/flask-5014

Clean number: every instance's gold patch was pre-verified to resolve on this
machine, so a non-resolve is an AGENT failure, not a harness/grader problem.
Re-graded after the patch-hygiene fix (below) — still 1/12, so hygiene was not
masking correct fixes.

## Two distinct problem classes

**1. Patch hygiene (FIXED — swe_adapter/patch.py).** `git add -A` swept
non-solution paths into every model_patch: `.agent/env.json` (set_env writes it
RELATIVE → ContainerEffects routes it into the container /testbed) leaked into
ALL 12; requests-1142 additionally captured 66 `build/lib/**` artifacts
(882KB patch, 74 files → 8 real). Fix: `rm -rf .agent` + git pathspec excludes
(.agent, build, dist, *.egg-info, __pycache__, .pytest_cache). Re-grade with
cleaned patches: unchanged (1/12) — the leak was cosmetically wrong and broke
requests' `git apply`, but the underlying fixes were wrong regardless. Keep the
fix: honest, human-readable patches; no 882KB junk at scale.

**2. The repo-scale wall (the real capability gap).** Per-instance taxonomy:
- **10/12 PAUSED at the wall clock** — only pylint-4604 and sympy-11618
  completed. The agent runs out of budget mid-repair on real repos.
- **The one solve (flask-5014) is the smallest surface**: 1-file patch, single
  goal. sympy-11618 completed cleanly (1 goal, 14 cyc, 5-file patch) but its
  fix was wrong — completing ≠ correct.
- **Over-broad, non-surgical edits**: django-10880 touched 21 clean files for
  one bug (0/2 goals, paused at 23 cyc); pytest-10081 10 files. The agent
  spreads edits instead of localizing — the diffuse-edit signature of weak
  localization on a large tree.
- **Inference volume varies wildly** under the same budget: pytest-10051 burned
  134 inferences (0/2 goals), flask-5014 83 (and resolved). High-volume thrash
  vs. convergence.

## Read (CORRECTED after CoT/trace deep-dive)

The first-pass "repo-scale localization wall" read was WRONG — the traces
overturn it. Source localization mostly WORKS: **8/12 missions edited the
correct gold file** (only 4 genuinely missed). The failures are downstream of
finding the file, and the dominant cause is **over-scoped work plans**, not
localization and not raw inference speed:

- **The brownfield replan/decompose turns a surgical bug-fix into a multi-goal
  BUILD.** astropy-12907 (gold = ~15 lines in one function) decomposed into
  goals like "a helper function that recursively flattens nested
  CompoundModel", "update the module docstring and add inline comments", plus
  several "fix failing test" goals → +107 lines added, 16 dispatches (all
  *succeeding*), then PAUSED. It wasn't stuck; it built a small project for a
  3-line task.
- **Self-authored tests + scaffolding churn burn the budget.** django-10554
  spent SEVEN file_ops writing its own test file
  (`django/tests/queryset_union_ordering.py`) — the grader supplies the test —
  then fixed the two gold files. Nearly every mission also edited
  README/requirements/pyproject/ruff.toml (non-fix scaffolding). Pure wasted
  throughput + patch pollution.
- **Over-broad, non-minimal edits** (+57..+107 lines vs ~15 gold) follow
  directly from the over-scoped goals — and add their own failure surface.
- **Verification/test-selection gap** (sympy-11618, COMPLETED but wrong): the
  repair-test selection picked `test_args.py`/`test_line.py`, NOT
  `test_point.py` where the regression lives — verified green against
  irrelevant tests, self-certified done, and shipped a fix with a **missing
  `zip_longest` import** (NameError under the real test). The fix LOGIC was
  arguably better than gold; it failed on an import + wrong-test verification.
- **Genuine source-localization miss: 4/12** (astropy-13033, django-10880,
  pylint-4551, pylint-4604) — real, but the minority.

**Speed vs capability vs localization:** 10/12 paused, but the pause is
LARGELY SELF-INFLICTED — the agent spends 3-5× the necessary work per task
(helper/docstring/test goals, scaffolding edits, wholesale rewrites). The
hardware isn't the wall; the work plan is. Tightening scope recovers the
throughput without faster inference.

## Next-phase levers (RE-RANKED by the CoT evidence)

1. **Repair-scope discipline (the #1 lever).** Brownfield repair must be a
   MINIMAL diff that makes the failing test pass — not a decomposed build.
   The replan/decompose phase is generating helper/docstring/refactor goals
   and self-authored-test goals for what is usually a one-symbol patch. Fixing
   this directly removes both the over-broad edits AND most of the wall-clock
   pauses. (This is a repair-profile scoping constraint, not new machinery.)
2. **Don't author tests; don't touch scaffolding.** The grader supplies tests;
   an agent writing its own (django: 7 cycles) is waste + pollution. Repair
   missions should be barred from creating test files / editing
   README/requirements/pyproject.
3. **Test-selection correctness (sympy).** Select the actual regression test
   (include the problem statement's own repro / the file whose name matches
   the changed module) — the witness rule needs the RIGHT test, not just a
   baseline-failing one.
4. **Minimal-edit discipline.** +107 lines for a 15-line fix; resist wholesale
   function rewrites.
5. **Issue-guided retrieval** — still worth it for the 4 real localization
   misses, but demoted: it is NOT the dominant failure.

## Non-goals confirmed

Not a hygiene problem (proven by re-grade); not a harness problem (gold 12/12);
NOT primarily a localization wall (8/12 found the file); NOT primarily a
hardware-speed wall (the pauses are self-inflicted by over-scoped work). The
lever is repair-scope discipline.
