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


---

# FIXED — 2026-07-27

Every link in the chain now has a break, and **A and B independently prevent the
original failure**. Verified end-to-end against a rebuilt fixture (empty `.venv`
+ `pyproject.toml` declaring PyYAML + code importing yaml): the venv is no
longer created, no longer activated when present, and the program runs.

| # | fix | commit |
|---|---|---|
| A | `uv venv` gated on a real Python install, not on a `py` section existing | `8280fa9` |
| C | loud warning when a Python project declares no install command | `8280fa9` |
| B | venv activation requires >=1 installed distribution | `8280fa9` |
| E | `FailedAttempt` recorded on the project_ops route | `d5ba47d` |
| D | failed install escalates to a command-capable loop (OPEN_TASKS §2) | `e61fdc0` |
| F | dead `run_setup_commands` step removed | `264adc0` |
| G | `verify_project_env` — project_ops verifies its own work | `1a09700` |

**33 new tests, every one mutation-verified.** Agent suite 1695, llmvp 418.

## What each actually changed

**A** — `_uvize_install_commands` keyed venv creation on a `py` SECTION, and
`syntax` is required for every detected extension, so every Python project
always had one. Zero install commands still returned
`["uv venv --allow-existing --python 3.x"]`. Now gated on a Python install
actually being present; `python -m pip install …` is also recognised, which was
previously neither rewritten nor counted.

**B** — `venv_env_overrides` accepted any `.venv/bin/python` as proof of
usability. Activation now requires an installed distribution. The predicate was
chosen deliberately: activating a venv with zero distributions can never help —
neutral for a stdlib-only project, harmful when the deps live in the ambient
interpreter. Proven by the arm that survived *because* it had no venv.

**E** — the most instructive. Three mechanisms (`## Prior attempts`, the
repeat-target CRITICAL warning, the web-search gate) were already built and
simply starved of input, because attempts were recorded on the file_ops route
only. After three env cycles they now all fire. **This reframes the 26 identical
diagnoses as the framework's repetition, not the model's** — its `root_cause`
was correct every cycle; it was re-reading a byte-identical seed.

**D** — closes `OPEN_TASKS` §2. Note `TRAP_BRIEF.md` §7 supersedes its own
"escalate to the PTY" recommendation with A′, on the grounds that ground truth
was present and IGNORED. That is correct for thompson-nfa (static tracing
mis-localised to the wrong package) and does **not** describe this failure: here
localisation was right every cycle and the remedy was inexpressible. Different
sub-case; an action space is exactly what it needs.

**G** — verifies DECLARED distributions via `importlib.metadata`, deliberately
by distribution name rather than module name (PyYAML->yaml, beautifulsoup4->bs4;
a manifest declares the former). A probe that cannot run reports unverified,
never a pass. `escalate_env`'s "resolved" is re-verified rather than trusted —
the same class of self-claim that produced this trap — via a separate step whose
failure route terminates, so a persistently-missing dependency cannot cycle.

## Still open

- The **laguna JSON-extraction failure** that started the chain is a model-format
  issue, tracked in `dev/laguna/FINDINGS.md`. Deliberately not "fixed" here: any
  model can return a malformed response, and the framework must not convert one
  into a silent 50-minute loop. It no longer does.
- `test_install_command` is absent from `schemas/validation_env_config.json` yet
  prompted for and consumed, and is not uv-rewritten — so its own prompt example
  (`pip install -e '.[test]'`) runs bare `pip` in a pip-less uv venv.
- `action_persist_validation_env` uses a shallow `dict.update`, so a later
  detection omitting `install_command` silently erases a working one.
- A rerun of the poolside 2h mission would measure what that run was meant to
  measure. Success looks like: `Collected install_command` present, multi-turn
  PTY sessions (it had 45, all 1-turn), `diagnose_issue` in single digits.
