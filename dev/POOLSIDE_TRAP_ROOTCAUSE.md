# Poolside run: root cause of the blind-diagnose fall (2026-07-27)

Aborted at ~68 min of a 2h backstop. 20 authored files (83 KB) kept; the
remaining time was being spent re-diagnosing one error.

## The chain

1. **A venv was created but never populated.** The three arms that survived all
   emitted the identical command; poolside emitted **no install command at all**:

   | arm | install command | venv site-packages | outcome |
   |---|---|---|---|
   | gemma | `uv pip install -r pyproject.toml` | `yaml`, `pyyaml-6.0.3.dist-info` | ran |
   | gpt-oss | `uv pip install -r pyproject.toml` | populated | ran |
   | devstral | `uv pip install -r pyproject.toml` | populated | ran |
   | unsloth laguna | *(never created a venv)* | n/a | ran |
   | **poolside** | **none** | **EMPTY** | trapped |

2. **The empty venv SHADOWS a working system python.** yaml is importable
   system-wide — poolside's game runs fine under `python3` right now. The only
   broken thing is the venv.

3. **Startup check fails -> fix-mode.** `ModuleNotFoundError: No module named
   'yaml'`.

4. **The remedy cannot work.** It wrote `fix_env.sh`:

       cd /private/tmp/tier/poolside-laguna
       pip install pyyaml

   **uv-created venvs contain no `pip` binary.** So the command either resolves
   to system pip (installing outside the venv) or fails; the venv stays empty
   either way.

5. **No route back to execution.** 26 `diagnose_issue` cycles, **45 PTY sessions
   every one of which closed after 1 turn.** It never held a terminal open,
   never ran its own game, and re-derived the same conclusion every ~5s:

       [17:04:30] The trace shows load_world imports yaml... ModuleNotFoundError
       [17:04:35] The trace confirms loader.py does import yaml at line 7...
       [17:04:40] pyproject.toml declares PyYAML>=6.0, so the project correctly...

This is the **deterministic-startup-fail blind-diagnose trap** already on record:
a failed startup check flips the goal to fix-mode with no path back to live
execution, so diagnosis proceeds blind. The known predictor (investigate >>
interaction) holds exactly.

## Comparative signature

| arm | PTY sessions by turn count | diagnose_issue |
|---|---|---|
| gemma (panel winner) | 8x1, 4x5, 4x4, 3x2 | **9** |
| devstral | 14x1, 6x3, 6x2, 3x7 | 13 |
| unsloth laguna | 18x1, 9x2, 3x7, 3x3 | 17 |
| gpt-oss | 28x1, 8x7, 8x2, 5x4 | 27 |
| **poolside** | **45x1 and NOTHING else** | 26 @ 63min (~50 pace) |

Every other arm drove multi-turn sessions. Poolside drove none. That is the
cleanest single indicator of the trap — not the diagnose count (gpt-oss ran 27
and still placed 2nd), but the **complete absence of multi-turn sessions**.

## What is a model failure and what is a framework failure

**Model:** it did not run the install step the other three ran, and when
recovering it reached for `pip` — which does not exist in a uv venv. One run, so
this could be variance rather than a trait.

**Framework, and the more actionable half:**

1. **Nothing verifies a created venv is usable.** An empty venv shadowing a
   working interpreter is worse than no venv — proven here, since the arm with
   NO venv ran fine. Bootstrap should either populate it or not create it.
2. **The fix path does not know its own tooling.** The project is uv-managed and
   uv venvs have no pip; the repair prompt should name `uv pip install`.
3. **The diagnose loop has no escape hatch.** 26 identical diagnoses with no
   strategy change. A repeat-diagnosis counter should escalate to the
   PTY/charter path (the remedy already identified for this trap) rather than
   re-deriving the same root cause indefinitely.
4. **A working system interpreter is a viable fallback that is never tried.**
   The game runs under `python3` today.

## What this does NOT show

Nothing about poolside's *quality*. The artifact is 83 KB across 20 files — the
largest of any arm — with 10/10 in a single batch slice, zero parse failures,
zero tool calls, and zero pure-CoT responses. It fell to environment plumbing,
not to code generation, and the fall is a framework trap that any model can hit.

A rerun with the venv pre-provisioned (or bootstrap hardened) would measure what
this run was meant to measure.
