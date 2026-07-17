# Ouroboros — Testing Guidelines

*How we test, what earns a test, and what a test must never be. Written from
the 2026-07-16 suite-quality evaluation (130 files / ~1270 tests in `tests/`,
16 files / ~228 in `llmvp/tests/`), graded against the three real bugs that
shipped past the suite that same week. For contribution mechanics see
`CONTRIBUTING.md`; run commands live in `AGENT.md`.*

---

## The empirical rule this document exists for

Every escaped incident of 2026-07-16 came from a module at 0–27% coverage
(the TB agents at 0% shipped a live AttributeError; `effects/local.py` at
27% hid a 72-minute hang in `run_command`'s untested timeout branch). The
44 modules at ≥90% produced zero incidents. **Coverage of load-bearing
seams is not a vanity metric here — it is the observed incident boundary.**

The inverse also held: the suite's *mass* sat where assertions were
thinnest. 44 tests pinned exact prose in one renderer file while the
highest-risk effect had none. Value density beats count.

## What earns a test

1. **A behavior contract** — what a caller may rely on. Not the wording of
   a log line, not the phrasing of a prompt.
2. **An incident** — every real bug gets a regression test that pins the
   *class*, not just the instance. Exemplar: `tests/test_tb_base.py`
   AST-walks both TB agents so *any* `self._x()` without a matching
   definition fails — born from one deleted-method bug, it now catches the
   whole category. Write the narrative into the docstring (see
   `test_swe_adapter.py`, `test_pool_scaling.py` — incident-tied tests age
   best because the "why" survives).
3. **An extracted unit** — when logic is pulled out of a giant (the
   token pipeline, the seq-band planner), it gets direct unit tests in the
   same commit. `llmvp/tests/test_token_pipeline.py` and
   `test_seq_layout.py` are the models: small, fast, model-free, pinning
   exact semantics of a clean seam.
4. **A guard invariant** — ceilings respected, markers present, output
   never truncated-then-parsed (`test_prompt_backstop.py` style).

## What a test must never be

- **A prose pin.** `assert "sceptical materials-science data curator" in
  prompt` breaks on every benign reword and catches no behavior change.
  Assert STRUCTURE instead: keys present, sections ordered, placeholders
  resolved, contracts parseable. (~200+ prose assertions remain across
  ~20 files — see the roadmap.) If prose genuinely matters (an output
  format the model must reproduce), derive offsets/anchors from the text
  (`.index()` style, as in `test_final_channel_stop.py`) rather than
  duplicating literals.
- **`assert True`** or a constructor-sets-attribute tautology.
- **Coupled to internal apparatus.** No imports of `dev/` scripts (a test
  died this way when the bake-off apparatus was deleted); no
  monkeypatching of private module globals (`runtime._turn_renderer`,
  whole-module `time` replacement) when a public seam exists. Dynamic
  imports of non-package tools are allowed ONLY for load-bearing
  production tools (then a missing tool SHOULD fail the suite, loudly).
- **A silent skip.** Heavy external deps (terminal_bench, harbor, docker,
  tau_bench) are guarded with `pytest.skip` naming the dep — never a bare
  try/except that passes vacuously.

## Structure rules

- **Table-driven by default** for literal-varied behavior. One
  parametrized test with named case ids replaces N copy-pasted functions;
  each case stays individually reported and individually extendable.
  Exemplar: `tests/test_markdown_fence_parser.py` (16 functions → 1 table
  + 2 specials, same cases, −40% code).
- **Shared doubles live in `conftest.py`** (currently being introduced —
  neither suite had one, which is why `ScriptedInferenceEffects` existed
  in six copies and every llmvp file re-invented `FakeLlama`). New shared
  fixtures: add to conftest, not to your file.
- **Builders over inline literals** for domain records: `goal_record()`,
  `make_config()`, `make_backend()` with keyword overrides. Six files
  hand-build `GoalRecord(...)` a dozen times each today.
- **Module-import smoke** (`tests/test_module_imports.py`) walks every
  production package — the floor under the deleted-symbol bug class. Keep
  the optional-dep table there current when adding an adapter.
- **Async machinery is testable without a model.** The llmvp pattern:
  real backend/engine classes driven by scripted fakes with tiny timeouts
  (`test_pool_scaling.py`, `test_batched_engine.py` — the latter runs the
  real decode thread). Timing knobs used by drains/settles should be
  instance attributes so tests can shrink them (`_refresh_settle_s`).
- **One behavior, one home.** Before writing a test, grep for the module's
  existing test file; 130 files exist partly because behaviors were
  re-pinned in new files instead of extended in place.

## Reduce-while-broadening roadmap (from the 2026-07-16 eval)

Executed: markdown_fence consolidation (exemplar), module-import smoke,
`run_command` timeout/group-kill tests, drain-refresh state-machine tests,
`test_mock.py` deleted.

Remaining, ranked (est. ~140-test / thousands-of-lines reduction while
adding coverage):

| Target | Now | Move |
|---|---|---|
| migration family (create/rewrite/set_env/research) | 40 tests, 4 copies of ScriptedInferenceEffects + 12 dup fixtures | conftest fixtures + per-file render tables; replace prose asserts with structure asserts |
| test_turn_renderer.py | 44 tests, 71 prose asserts | parametrize the 4 literal clusters; structure-assert the rest |
| test_turn_models.py | 31 | validate-table by shape |
| test_oracle_rung.py / test_output_format_oracle.py | 24 / 23 | (rung, artifact, verdict) tables |
| test_data_ops.py / test_schema_registry.py / test_frame_editor.py | 28 / 26 / 22 | format/loader/declaration tables |
| llmvp: API layer (graphql_api, rest_api) | 0 tests | resolver-shaping + error-mapping units with fake manager |
| llmvp: reasoning-strip orchestration | disabled in every session test via LLMVP_THINK_STRIP=0 | one enabled-path test with the recording-ctx fake |
| llmvp conftest | fakes duplicated ×4-5 | make_config / make_backend / RecordingCtx / FakeLlama |

Deliberately untested: `agent/blueprint/*` (documentation tooling, ~1.5k
stmts at 0% — accepted), anything requiring a live model (that is what the
`dev/` operator acceptance scripts are for: batched_parity, duo_soak,
snapshot_stress).

## Coverage practice

`uv run pytest tests/ --cov=agent --cov=adapters` (main),
`cd llmvp && .venv/bin/python -m pytest tests/ --cov=core --cov=inference --cov=api`.
Coverage is advisory, but a change touching a sub-50% module should leave
it better than it found it — that band is where all the incidents live.
Current floors (2026-07-16): main 60%, llmvp 47%.
