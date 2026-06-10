# Site #6 — `diagnose_issue.run_command` — ELIMINATED

**Status:** Approved for Step C migration. **Step is eliminated, not ported.**
**Current file:** `flows/cue/diagnose_issue.cue` (lines 153-170)
**Current action code:** `agent/actions/diagnosis_session_actions.py::action_run_investigation_command` (lines 738-943, ~200 lines)
**Empirical (892 run):** n=35 inference calls across 10 step invocations (3.50 inferences per step — highest cost-per-invocation in the flow). 9 compound-response leaks — highest leak rate in the trace.

---

## Decision — eliminate the step entirely

The current `run_command` step hides **three inference calls** with shape transitions between them in a single shared session — the architectural worst case for KV-cache drift. Under the new schema, all three of its model-facing responsibilities are absorbed elsewhere:

- **Tool selection** (Menu 1: grep / run_command / read_data_file / conclude) → becomes the stock compound options already declared at Site #5's `pick_action` (and Site #4's `pick_file` carries `__run_command__` too).
- **Argument collection** (Inference 2: JSON doc with pattern/command/file) → compound argument on the stock option carries the value directly.
- **Post-execution decision** (Menu 3: conclude / another_command / examine_file) → next iteration of Site #5's `pick_action` loop asks the same question.

The only remaining work is tool execution, which is deterministic and doesn't need an inference call. That becomes a new narrow step, `execute_investigation_tool`.

**Net result:** 3 inferences per invocation → 0.

---

## The replacement — `execute_investigation_tool`

```cue
execute_investigation_tool: #StepDefinition & {
    action:      "execute_investigation_tool"
    description: "Run command collected from __run_command__ compound option; queue observation as session injection"
    context: {
        required: ["diagnosis_session_id", "pick_action_choice"]
        optional: ["suspect_file", "investigation_turn"]
    }
    resolver: {
        type: "rule"
        rules: [
            // Route back to the step that requested the command.
            // When suspect_file is in context (set by pick_file before pick_action
            // was reached), we're in Phase 2 — return to pick_action.
            // Otherwise (pick_file's pre-file __run_command__ escape) return to pick_file.
            {condition: "context.get('suspect_file')", transition: "pick_action"},
            {condition: "true",                         transition: "pick_file"},
        ]
    }
    publishes: ["investigation_turn"]
}
```

The step:
1. Reads the command string from `pick_action_choice` (or an equivalent context key that the compound menu resolver publishes from the `command` arg).
2. Executes via the existing `_tool_run_command` helper (`agent/actions/diagnosis_session_actions.py` — keep this; drop `_tool_grep_project` and `_tool_read_file`).
3. Queues the observation (`$ <cmd>\nReturn code: N\n<output>`) as a session injection so the next inference turn sees it.
4. Rule resolver routes back: `pick_action` if suspect_file was set (normal Phase 2 flow), `pick_file` if not (pre-file escape from pick_file).

No inference. Pure execution + injection + route.

---

## Decisions landed

### Collapse `run_command` step entirely

**Settled.** The step is deleted. Its work redistributes as described above. Transitions that previously targeted `run_command` (from Sites #4 and #5) now route through the `__run_command__` compound option at the source menu, with execution happening in `execute_investigation_tool`.

### `pick_action` stock options — trimmed to `[__all_symbols__, __run_command__, __conclude__]`

**Settled.** Three stock options, not five. `__grep__` and `__read_file__` are both removed from consideration — see next section for reasoning.

### `__grep__` and `__read_file__` removed from the `_stock_options` catalog

**Settled.** The schema primitives file (`flows/cue/turn.cue`) gets these two entries deleted during Step C.

**Reasoning:**

The argument for `__grep__` was "output hygiene" — preset `--include` filters keep noise out of session injections. But the current implementation hardcodes `.py, .yaml, .json, .yml`. For anything outside the Python family, `__grep__` silently misses matches. The model gets "no matches found" and has no signal that the filter is the reason.

A language-biased tool masquerading as general-purpose is worse than no tool — false-clean results erode the investigation. Two alternatives were considered:

- **Language-aware grep** (read architecture, populate `--include` dynamically) — couples an investigation tool to mission state. Over-engineered.
- **Broader `--include` default list** covering many languages — list-maintenance burden. New language → forgotten-to-add bug.

Cleanest option is to drop `__grep__` and let the model use `__run_command__` with flags appropriate to the project. Output hygiene moves to the prompt/SOUL.md layer: teach the useful `grep` incantations rather than hide them behind a tool that's wrong for most projects.

`__read_file__` removal is simpler — `cat path` via `__run_command__` covers it with no meaningful loss. The catalog shrinks by two entries.

### SOUL.md primer addition

**Settled for Step C:** the primer's Response Environments section (drafted in `dev/proposals/turn_schema_soul_primer.md`) gains a short paragraph on investigation command hygiene when `__run_command__` is being used for search:

> When using `__run_command__` for cross-file search, scope the grep:
> `grep -rn --include='*.<ext>' pattern <dir>` and exclude noisy dirs like
> `.git`, `node_modules`, `.venv`, `__pycache__`. Pipe to `head -N` if you
> expect many matches. A well-scoped search returns 10-50 useful lines;
> an unscoped one floods the session with irrelevant matches.

Voice-matched to the existing primer prose. Not a formal option catalog entry — just guidance in the environment section.

### Retroactive updates to Site #5's record

**Required:** Site #5's pick_action stock list was drafted with five entries including `__grep__` and `__read_file__`. Update to the trimmed three:

```cue
stock: [
    _stock_options.__all_symbols__,
    _stock_options.__run_command__,  // compound, takes command (covers grep, cat, head, ls, etc.)
    _stock_options.__conclude__,
]
```

Transitions map trims correspondingly — only `__run_command__` routes to `execute_investigation_tool`; no `__grep__` or `__read_file__` options exist.

**Action:** add an appendix to Site #5's record noting these trims with a back-reference to this record. Deferred to next turn so we don't break continuity here.

### `execute_investigation_tool` routes back based on `suspect_file` presence

**Settled.** Single step, rule resolver. If `suspect_file` is in context (Phase 2 — pick_action called), return to `pick_action`. Otherwise (Phase 1 — pick_file's pre-file escape), return to `pick_file`. Handles both use sites cleanly.

### Tool execution helpers — simplified

**Settled:** keep `_tool_run_command`. Drop `_tool_grep_project` and `_tool_read_file` since no code path calls them after the collapse. Net removal: ~60 lines of tool-helper Python.

---

## Total code removed at this site

- `action_run_investigation_command` — ~200 lines.
- `_tool_grep_project` — ~35 lines.
- `_tool_read_file` — ~25 lines.
- Three inline menu prompts (`cmd_options`, three `arg_prompts` variants, `post_options`) and their `build_menu_prompt` calls.

**Total: ~260 lines of Python eliminated.** Replaced by a ~30-line `execute_investigation_tool` action.

---

## Cross-site follow-ups

- **Site #5's stock options + transitions** — retroactive update required next turn. Record the trim with back-reference to this record's catalog decision.
- **Schema primitive file change** — `flows/cue/turn.cue` loses `__grep__` and `__read_file__` from `_stock_options`. This is the first in-flight modification to the primitive file itself (every prior site record was additive). Step C migration must include this edit.
- **`compound_arg` consumption pattern** — Site #5's `pick_action` uses compound options for `__run_command__`. The arg (`command` string) must be published into context so `execute_investigation_tool` can read it. This is a new requirement on the menu resolver: when a compound option fires, the arg value publishes into context under a known key. Needs design at Step C renderer time. Mechanism candidates: publish under `pick_action_choice_arg`, or a general `<publish_selection>_arg` companion key.
- **Project-language awareness — cross-cutting observation** — `build_repomap` in `flows/cue/design_and_plan.cue` uses a similar hardcoded extension list (`*.py, *.js, *.ts, *.rs, *.yaml, *.yml`). Better than grep's but still hardcoded. Not part of Step B schema work; parked under the "observation-system + language-awareness" bucket for a later pass. Note attached to Site #2's record on next revision.
- **Site #3 dependency** — Site #3 (classify_fix_type elimination) folded `recommended_flow` into the CONCLUDE schema. The CONCLUDE turn isn't this site; it's the `force_conclude` step created in Site #5. That step's `#Turn` definition (authored at Step C) includes `recommended_flow` in its json_document schema. Three-way cross-reference: Site #3 decided the field belongs on CONCLUDE; Site #5 owns the CONCLUDE step; Site #6 is unaffected by the field itself but eliminates the step whose responsibility it was.

---

## Migration ordering (Step C)

Because Sites #3, #4, #5, and #6 all touch the diagnose_issue flow interconnectedly, they must migrate together in one change (per AGENT.md). Order within the change:

1. Author new `execute_investigation_tool`, `execute_traces`, `extract_symbols`, `pick_file_fallback`, `force_conclude` action code.
2. Author `pick_action` turn in `diagnose_issue.cue`.
3. Author `pick_file` turn in `diagnose_issue.cue` with trimmed stock + compound run_command.
4. Update CONCLUDE turn (owned by force_conclude step) to include `recommended_flow` field in its json_document schema.
5. Remove `__grep__` and `__read_file__` from `_stock_options` in `turn.cue`.
6. Delete `classify_fix_type`, `run_command`, `trace_symbols` steps from `diagnose_issue.cue`.
7. Delete `action_pick_suspect_file`, `action_select_and_trace_symbols`, `_present_after_trace_menu`, `action_run_investigation_command`, `_tool_grep_project`, `_tool_read_file` from action code.
8. Add SOUL.md primer addition for grep hygiene.

All ships together. Single change, clean break.
