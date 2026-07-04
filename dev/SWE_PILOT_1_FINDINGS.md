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

## Read

code_core's small-surface editing competence is real and now has an official
data point (flask). Everything past a handful of files hits the localization +
throughput wall we consciously parked after Phase B — confirmed here with clean
attribution rather than inferred. The pilot did its job: it isolated hygiene
(fixed) from capability (the next phase) and gave a real baseline (8.3%) to
move from.

## Next-phase levers (ranked by the taxonomy)

1. **Repo-scale localization** — the dominant failure. The diffuse 21-file
   edits + wall-clock pauses say the agent can't cheaply find + bound the
   change site on a large tree. This is the Phase B.5 hypothesis that the
   head-to-head disproved for SMALL repos but is clearly live at Verified
   scale. Issue-guided retrieval (repomap query + grep from the problem
   statement) feeding a FOCUSED file_context, so diagnose/patch don't wander.
2. **Inference economy / budget** — 10/12 pausing means the 20-min cap is
   binding; either faster convergence (fewer, better-targeted edits) or a
   larger budget for the scouts. Measure inferences-to-first-edit.
3. **Surgical-edit discipline** — a 21-file diff for one issue is almost never
   right; the escalation/repair loop should resist broadening.

## Non-goals confirmed

Not a hygiene problem (proven by re-grade); not a harness problem (gold 12/12);
not the small-surface editing path (flask works). The wall is localization +
throughput at repo scale — the next build.
