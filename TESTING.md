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
- **Shared doubles live in `conftest.py`** — both suites now have one
  (`tests/conftest.py` 920f28f, `llmvp/tests/conftest.py` a3e0f28); that is
  why `ScriptedInferenceEffects` no longer exists in six copies. New shared
  fixtures: add to conftest, not to your file. **But a double's ABSENT
  methods can be load-bearing**: `agent/trace.py:19` gates all emission on
  `hasattr(effects, "emit_trace")`, so a double without it deliberately
  exercises the no-trace branch. Adding a method to a shared double to
  absorb one more call site can silently move every existing user onto a
  different production path, suite fully green. Widen by SUBCLASS, not by
  editing the base.
- **A table row must assert something.** Subset-comparison columns
  (`expected.items() <= out.result.items()`) are the right way to let rows
  name different result keys — but `{}.items() <= anything` is `True`, so a
  row that forgets its expectation passes vacuously. Guard in the body:
  `assert expected_result, f"{case_id}: row must name at least one key"`.
  Same rule as the production gates: fail on zero checkable items.
- **Incident narrative survives parametrization, or the test stays a
  function.** If a docstring's "why" (the run it came from, the failure it
  pins) cannot be carried by a case id plus a table comment, that test is
  not a row. Losing the narrative costs more than the duplication saved.
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

## Reduce-while-broadening roadmap

### Measuring it — the unit is FUNCTIONS and LINES, not reported tests

The original roadmap promised a "~140-test reduction". **That number was
never reachable by the means it prescribed, and its own exemplar disproves
it**: `test_markdown_fence_parser.py` is now **3 functions reporting 18
tests** — function count fell 16→3 while the reported count ROSE 16→18.
pytest reports every parametrized case, so table-driving trades functions
and lines for cases, and a faithful execution of this roadmap ends with
MORE reported tests than it started with.

Measure the thing that actually moves. Baseline (2026-07-25):

| Suite | functions | reported | lines | files |
|---|---|---|---|---|
| `tests/` | 1440 | 1591 | 33,072 | 146 |
| `llmvp/tests/` | 311 | 330 | 7,002 | 24 |

A consolidation pass is going well when functions and lines fall, reported
cases hold or rise, and coverage does not regress. Anyone reporting
"−140 tests" is reporting the wrong number.

### Proving a consolidation did not weaken a test

Green is not evidence here — a table that dropped an assertion is green,
and so is one whose rows assert nothing. Three mechanics, in cost order:

1. **Assertion ledger.** Count `assert` statements before. Every one maps
   to a table column, a retained in-body assert, or a deletion you justify
   in the commit message. The ledger goes IN the commit message.
2. **Coverage missing-line subset.** `--cov=<module> --cov-branch
   --cov-report=term-missing` before and after; the after missing-set must
   be a SUBSET of the before set. A percentage that holds while different
   lines go dark is a regression wearing a good number.
3. **Mutation spot-check** (~60s each). Break the one production line the
   test pins, run only that file, confirm exactly the expected tests fail,
   then restore. This is the only mechanic that catches a test that lost its
   teeth without changing which lines execute.

   Three rules learned the hard way on 2026-07-25, when this caught four real
   problems — twice in tests the author had just written and believed:

   - **`assert` that the pattern matched** (`assert src.count(old) == 1`)
     before writing the mutant. A mutation that silently fails to apply
     reports "all green", which reads exactly like confirmation. This
     produced five false confirmations in one run before the guard was added.
   - **Restore from a COPY, never `git checkout --`.** Checkout discards
     uncommitted work — including the fix you are validating. `cp` the file
     aside first and `cp` it back.
   - **Verify the tree is clean between mutations.** One restore that did not
     land left two mutations stacked, and the second one's result was
     attributed to the wrong guard.

   When a mutation fails NOTHING, that is a finding, not a dull result: the
   test cannot prove its own name. Usually the fixture never reaches the
   branch (see the anti-pattern below).

- **A test whose fixture never reaches the branch.** The commonest way a
  guard test is unfailable, and it looks perfectly reasonable on the page.
  Real examples, all fixed 2026-07-25: patterns that match nothing in the
  listing they filter; a tool left unconfigured so the guarded call never
  runs; a step name never passed, so the router cannot match any rule; a
  double so permissive the function returns at its first precondition. Before
  trusting a guard test, ask what the fixture makes REACHABLE — not what the
  assertion says.

### Status

**Executed:** markdown_fence consolidation (exemplar), module-import smoke,
`run_command` timeout/group-kill tests, drain-refresh state-machine tests,
`test_mock.py` deleted, both conftests introduced (920f28f, a3e0f28).

| Target | Now | Move |
|---|---|---|
| test_turn_renderer.py | 44 fns, ~70 prose asserts | 7 tables → ~25 fns. Do LAST — biggest file, 3 fns already hide internal loops. Parametrize and prose→structure are SEPARATE commits |
| test_turn_models.py | 31 fns | validate-table by shape → ~9. Reject-table needs a model column (3 models); assert `errors()[0]["loc"]/["type"]`, never pydantic's rendered message |
| test_oracle_rung.py / test_output_format_oracle.py | 24 / 23 fns | (rung, artifact, verdict) tables → ~8 / ~11. `_apply_format_checks` block is the cleanest target in the suite — start there. Its gate/store pairs are cross-action SEQUENCES, not rows |
| test_data_ops.py / test_schema_registry.py / test_frame_editor.py | 28 / 26 / 22 | format/loader/declaration tables. Deferred until the four above land — seven parametrization files in one pass is where quality drops |
| llmvp: API layer | graphql_api 65%, rest_api 61%, `api/main.py` **0%** | The 65% is INFLATED — ~20 strawberry dataclass decls (lines 52-338) execute at import; resolver bodies are the gap. Highest value: `rest_api` `/chat/completions` (118-148, the Terminus path, entirely uncovered) and `Mutation.swap_model` (mutates the `_session_manager` global, 3 outcomes, tested nowhere — `test_model_swap.py` tests `core/model_swap.py`, not the resolver) |
| llmvp: reasoning-strip orchestration | `_maybe_strip_reasoning` (session_manager.py:849-908) — 4 untested guards | Direct guard tests + one enabled-path e2e. The empty-content guard at :859-865 is the prize: its comment records a real incident (stripping there writes an answerless assistant turn into the KV, compounding across turns) |

**Declined, with reasons** (a declined row is worth more than a permanent todo):

- **migration family render-tables** (39 tests / 4 files). The conftest half
  shipped; the rest is declined. These are the suite's highest-value
  integration tests (the only place runtime + compiled flows + renderer +
  actions run end to end), the available reduction is ~9 functions, and
  prose→structure here is RE-SPECIFICATION, not refactoring — the natural
  bad outcome (`assert prompt` replacing `assert "X" in prompt`) is
  invisible to all three mechanics above. The `emit_trace` trap lives here.
- **`_si` / `_mission` builders** (48 files each, ~94 definitions). Biggest
  raw count, worst payoff: they are helpers, so merging them reduces test
  functions by exactly ZERO. Only ~10 are byte-identical; `flow_name` takes
  33 distinct values and `effects=` is load-bearing (`MockEffects()` vs
  `MockEffects(mission=mission)` decides whether mission persistence is
  wired — `agent/effects/mock.py:53,634,639`). The moment a shared builder
  gives `flow_name` a default, ~40 tests silently assert against a flow
  they don't belong to, with nothing going red.
- **The two `CountingEffects`** (`test_runtime_turn_integration.py:795,865`)
  — identical except one implements `session_inference` and the other
  `run_inference`. That difference IS the test (`runtime.py:1239` +
  `:1070`); they assert opposite emit counts.
- **`prompts_dir`** — same fixture name in two files, disjoint template
  sets, one monkeypatches the runtime singleton. Hoisting either under the
  shared name swaps templates under the other file.

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
