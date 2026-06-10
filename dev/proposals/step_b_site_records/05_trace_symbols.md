# Site #5 — `diagnose_issue.trace_symbols`

**Status:** Approved for Step C migration. **Structural reshape: Option C.**
**Response shape:** `menu_compound` (single unified investigation menu, invoked in a loop)
**Current file:** `flows/cue/diagnose_issue.cue` (lines 128-147)
**Current action code:** `agent/actions/diagnosis_session_actions.py::action_select_and_trace_symbols` (lines 419-645) + `_present_after_trace_menu` (lines 648-730)
**Empirical (892 run):** n=38 inference calls across 15 step invocations (2.53 inferences per step). 18 compound-response leaks. Highest-frequency inference site in the diagnose flow.

---

## Decision — Option C (unified compound menu)

The current `trace_symbols` step hides two inference calls and two distinct menus inside one action. Option C collapses them into a single menu shape invoked in a loop — replicating the pattern from `interact.run_session`, which was 22/22 clean in the 892 trace (the most reliable menu in the codebase).

### Rationale

The two menus in the current structure are the same underlying decision ("what's my next investigation action?") asked at different points in an investigation. Making them one menu honors that. The pattern matches `run_session`'s uniform option-shape-per-turn discipline, which is empirically the most reliable menu pattern we have.

### Flow graph — after migration

```
(upstream) → extract_symbols → pick_action ⇄ execute_traces → (downstream)
                                     ↓
                                  (escape hatches:
                                    conclude → end_session
                                    examine_another_file → pick_file
                                    __run_command__ → run_command)
```

The self-loop `pick_action → execute_traces → pick_action` carries each investigation iteration. Model sees queued trace evidence as session injection on each iteration.

### Why C over B

- `interact.run_session`'s uniform compound menu was the most reliable menu in the entire 892 trace (22/22, zero leaks).
- The two current menus are the same underlying question ("what's your next investigation action?") at different investigation states.
- Fewer step definitions, one option shape to maintain, one template to render.
- Session injection protocol already in place — trace evidence rides the injection just like terminal output rides it in run_session.

### Risk acknowledgement

**The initial turn and iterating turns are not as symmetric as run_session's turns.** In run_session, every turn takes terminal output + makes a command decision — consistently. In diagnosis:
- Turn 1: no evidence yet → "which symbol looks suspect at a glance"
- Turn N: accumulated trace evidence → "given what I've seen, which symbol to dig into next"

These are genuinely different decisions presented through the same menu. Smart models will handle it. If KV cache drift amplifies the asymmetry, we'll see it.

**Observability commitment:** the trace includes an iteration counter on every `pick_action` invocation. If traces show first-iteration behavior diverging from later iterations (unusual conclude rate on turn 1, shape leaks concentrated at specific iteration boundaries), we have concrete signal that the asymmetry matters.

**Reopen conditions for considering Option B:**
1. First-iteration `__conclude__` rate > 2x later-iteration rate (model giving up before investigating).
2. Shape-leak rate on `pick_action` turns stays above 5% after Step C ships (banners + uniform shape should have fixed leakage; if it hasn't, the asymmetry may be the cause).
3. Investigation-turns-per-mission trends higher than current ~3 (model needing more iterations to converge through a uniform-but-awkward menu).

**Option B as escape hatch** — see "Deferred Option B" section at end of this record. Full specification preserved so the reshape is a mechanical switch if C underperforms.

---

## Turn definitions

```cue
extract_symbols: #StepDefinition & {
    action:      "extract_symbols_from_suspect"
    description: "Read suspect file, parse AST, publish symbol_table + target_file"
    context: {
        required: ["suspect_file", "diagnosis_session_id"]
        optional: ["investigation_turn"]
    }
    resolver: {
        type: "rule"
        rules: [
            // Normal case: file read, symbols extracted.
            {condition: "result.symbol_table_nonempty == true", transition: "pick_action"},
            // File read but no parseable symbols: queue full content, go to menu anyway.
            {condition: "result.read_ok == true", transition: "pick_action"},
            // File read failed: queue injection, bounce back to file selection.
            {condition: "true", transition: "pick_file"},
        ]
    }
    publishes: ["symbol_table", "target_file", "investigation_turn"]
}

pick_action: #StepDefinition & {
    action: "inference"
    description: "Model picks next investigation action: trace a symbol, conclude, escape"
    context: {
        required: ["diagnosis_session_id", "symbol_table", "suspect_file"]
        optional: ["investigation_turn", "file_context"]
    }
    turn: #Turn & {
        response_shape: "menu_compound"
        sections: [
            {type: "role",        template: "personas/diagnose_issue"},      // short-formed in-session
            {type: "target_entity", ref: {$ref: "context.suspect_file"}, title: "Target file"},
            {type: "instruction", template: "diagnose_issue/pick_action_instruction"},
            {type: "options"},
            {type: "envelope"},
        ]
        response: {
            // Symbol options come from context-key (action-published — future
            // observation-system target per Site #4's pattern).
            options_from: {
                source:      "context"
                context_key: "symbol_table"
            }
            stock: [
                _stock_options.__all_symbols__,    // no-arg: trace all symbols
                _stock_options.__run_command__,    // compound: takes command arg (covers grep, cat, head, ls, etc.)
                _stock_options.__conclude__,       // terminal
            ]
            // Flow-specific options (not stock — specific to diagnose investigation).
            options: {
                examine_another_file: {
                    key:         "examine_another_file"
                    description: "This file isn't the right one — switch to a different file"
                }
            }
            publish_selection: "pick_action_choice"
        }
        transitions: {
            options: {
                "__run_command__":      "execute_investigation_tool"
                "__conclude__":         "end_session"
                "examine_another_file": "pick_file"
            }
            default:   "execute_traces"   // any symbol name OR __all_symbols__
            no_answer: "force_conclude"   // retries exhausted → produce diagnosis from whatever evidence is queued
        }
        config: {temperature: "t*0.3"}
    }
    publishes: ["investigation_turn", "pick_action_choice"]
}

execute_traces: #StepDefinition & {
    action:      "execute_symbol_traces"
    description: "Trace selected symbol(s) cross-file, queue evidence as injection"
    context: {
        required: ["diagnosis_session_id", "symbol_table", "target_file", "pick_action_choice"]
        optional: ["investigation_turn", "file_context"]
    }
    resolver: {
        type: "rule"
        rules: [{condition: "true", transition: "pick_action"}]  // always loop back
    }
    publishes: ["investigation_turn", "traces_executed"]
}

force_conclude: #StepDefinition & {
    action:      "force_diagnosis_conclude"
    description: "Fallback: produce diagnosis from queued evidence when menu couldn't resolve"
    context: required: ["diagnosis_session_id"]
    resolver: {
        type: "rule"
        rules: [{condition: "true", transition: "end_session"}]
    }
    publishes: ["diagnosis_text", "hypotheses", "error_analysis",
                "investigation_turn", "investigation_complete", "concluded"]
}
```

---

## Decisions landed

### Structural: Option C

Settled above. Two inference calls → one turn invoked in a loop. Matches run_session's empirically-best pattern.

### Symbol list via context-key (Pattern B)

**Settled:** `extract_symbols` action runs tree-sitter, publishes `symbol_table` to context. `pick_action`'s turn sources options via `source: "context", context_key: "symbol_table"`.

Future: migrate to observation when the observations system lands. Same pattern noted at Site #4.

### Trace execution as a dedicated step

**Settled:** `execute_traces` is its own named step, not a pre-compute. Step gives it trace-event observability (duration, symbol count, failure) without pre-compute indirection.

### Option composition at `pick_action`

Unified option set across all invocations. All options present every turn — first-turn has `__run_command__` and `examine_another_file` available even though they're more useful later. Matches run_session's uniform-option-shape discipline.

**Per-turn options:**
- Every symbol in `symbol_table` (dynamic, from context-key)
- `__all_symbols__` — stock, no arg, trace everything
- `__run_command__` — stock, compound, takes `command` arg
- `__conclude__` — stock, terminal, status `concluded`
- `examine_another_file` — flow-specific, routes to `pick_file`

Option-set growth-on-iteration concern (raised in discussion) resolved by including all options on turn 1 — they're present but not useful if the model hasn't seen evidence yet. Model has always been smart enough to ignore irrelevant options.

### No-symbols fallback

**Settled:** if tree-sitter returns no parseable symbols, `extract_symbols` queues the full file content as a session injection and transitions to `pick_action` anyway. Model sees the file content and picks an action based on that. Preserves current graceful fallback.

Within `pick_action`, the `__all_symbols__` option is still meaningful but `symbol_table` is empty — the menu renders with just stock and flow-specific options. Viable.

### File-read-fail fallback

**Settled:** `extract_symbols` rule resolver routes to `pick_file` with a queued injection explaining the failure. Matches current action behavior. Observable in the flow graph.

### `no_answer` at `pick_action` → `force_conclude`

**Settled:** when retries exhaust at `pick_action`, route to `force_conclude`. Different from Site #4's fallback (first-file pick) because trace_symbols' recovery path is genuinely different: if the model can't pick an investigation action, giving it a random symbol to trace wastes effort. Forcing a diagnosis from whatever evidence is already queued is higher-value.

This is an observable named step, not silent default behavior. Trace events show when force_conclude fires.

### Temperature — `t*0.3` on `pick_action`

**Settled (revised).** Initially drafted at `t*0.1` to match the current action's 0.1. Revised during Site #9's calibration: `t*0.1` is argmax-adjacent and too restrictive for investigation decisions where the model weighs symbol relevance against accumulated evidence.

Bumped to `t*0.3`. Joins the menu-site cluster (Sites #4, #5, #9, #17). For this site specifically, the slight flexibility is more important than elsewhere — `pick_action` runs many iterations per mission, and `t*0.3` lets the model meaningfully re-evaluate on new evidence rather than locking into a symbol-picking pattern across 30+ invocations.

### Trace observability — iteration counter

**Settled for Step C:** each `pick_action` trace event includes `investigation_turn` number. Downstream analysis can slice behavior by iteration, validating (or falsifying) the C-vs-B decision empirically.

### Stock option reuse

`__all_symbols__`, `__run_command__`, `__conclude__` — all already in `_stock_options` catalog. Clean reuse, no catalog additions.

`examine_another_file` stays flow-specific. Specific to diagnose investigation; not generalizable across flows.

### Custom action decomposes

`action_select_and_trace_symbols` + `_present_after_trace_menu` (~300 lines) decomposes into:
- `extract_symbols_from_suspect` (AST + read — one responsibility)
- `execute_symbol_traces` (trace execution — one responsibility)
- `force_diagnosis_conclude` (CONCLUDE — delegates to existing `_force_conclude` helper)
- **The menu logic is gone from Python entirely.** Schema + renderer handle it.

---

## Templates to author at Step C

1. **`diagnose_issue/pick_action_instruction.yaml`** — "What's your next investigation action?" with brief guidance (model picks a symbol to trace, all symbols, conclude, run a command, or switch files).
2. **`diagnose_issue/symbol_option_description.yaml`** — per-option rendering for symbols from `symbol_table`: `"{kind} (lines {line}-{end_line}): {signature_truncated}"` pattern from current action.
3. The no-symbols injection message template — `"=== {suspect_file} (no parseable symbols — showing full content) ===\n{content}"` (exact match to current).

---

## Cross-site follow-ups

- **Observations system target** — `symbol_table` is a clean future migration from context-key to observation. Noted at Site #4; Site #5 is the second site with this pattern. When observations land, this and Site #4 migrate together.
- **Iteration-counter trace events** — first appearance. Pattern may generalize to other loop-structured flows (run_session itself already has `session_turn_count`). Consider formalizing at Step C.
- **`force_conclude` step pattern** — new named-fallback step for session-ending-on-failure. May be the cross-cutting pattern for session-loop flows when `no_answer` resolves.
- **Site #6 dependency** — `__run_command__` compound option from `pick_action` routes to the `run_command` step. Site #6's reshape must accept a pre-filled `command` context key from this compound arg. Cross-site contract.

---

## Appendix — updates from Site #6 decisions

After Site #6's analysis, the stock options on `pick_action` were trimmed from five entries to three. Site #6 eliminated `run_command` as a step entirely and removed `__grep__` and `__read_file__` from the schema's `_stock_options` catalog.

**Updated stock list** (reflected in the Turn definition above):
- `__all_symbols__` — no-arg, trace all symbols
- `__run_command__` — compound, takes `command` arg (covers grep, cat, head, ls, etc.)
- `__conclude__` — terminal

**Updated transition target** for `__run_command__`:
- Was: `run_command` (step to be ported)
- Is: `execute_investigation_tool` (new deterministic step authored in Site #6; no inference, executes the pre-filled command, queues observation, routes back to `pick_action`)

**Rationale recap:** `__grep__`'s preset `--include` filters assumed a Python project and silently misrouted searches in projects using other languages. `__read_file__` was covered by `__run_command__` with `cat`. Both removed from the catalog. Search hygiene moves to SOUL.md primer guidance.

See `06_run_command_eliminated.md` for the full Site #6 record.

---

## Deferred Option B — preserved as escape hatch

If Option C underperforms against the reopen conditions (first-iteration conclude rate spike, post-migration shape-leak rate, investigation-turn count), reshape to B:

### Option B flow graph

```
extract_symbols → select_symbols → execute_traces → after_trace_decision
                                                          ↓
                                                    (escape hatches
                                                     identical to C's pick_action)
```

### Key differences B vs. C

- **Two turns** instead of one loop — `select_symbols` (first-time picker) and `after_trace_decision` (iterating picker).
- **`select_symbols`** — `menu_compound`, options = symbols + `__all_symbols__` + `__run_command__` + `__conclude__`. No `examine_another_file` (can't switch files before tracing anything).
- **`after_trace_decision`** — `menu_compound`, options = `conclude` (stock) + `examine_another_file` + `select_more_symbols` (routes to `select_symbols`) + `__run_command__`. No symbol options — symbols are picked in the dedicated selector step.
- **`after_trace_decision → select_more_symbols → select_symbols`** creates a three-step loop instead of C's two-step loop.

### Migration from C to B if reopen triggers

1. Split `pick_action` into `select_symbols` and `after_trace_decision` with their distinct option sets.
2. Add `select_more_symbols` flow-specific option to `after_trace_decision` routing back to `select_symbols`.
3. Retarget `execute_traces`'s default from `pick_action` to `after_trace_decision`.
4. Update templates to reflect narrower per-turn option sets.
5. Preserve `force_conclude` and `extract_symbols` unchanged.

Mechanical reshape. One step becomes two; transition graph gets one extra node. No action code changes.
