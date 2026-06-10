# Site #11 — `patch.select_symbols`

**Status:** Approved for Step C migration.
**Response shape:** `menu_single` (Pattern B + stock options)
**Current file:** `flows/cue/patch.cue` (lines 64-83)
**Current action code:** `agent/actions/ast_actions.py::action_select_symbol_turn` (lines 339-595, ~260 lines)
**Upstream provider:** `agent/actions/ast_actions.py::extract_symbol_bodies` — publishes `symbol_menu_options`
**Empirical (892 run):** n=19, avg_in=325 tokens, avg_out=2 tokens (max 29). Originally 9 empty responses before the LLMVP session-cap fix; currently clean.

---

## Turn definition

```cue
select_symbols: #StepDefinition & {
    action: "select_symbol_turn"  // wrapper: turn + runaway-prevention guards
    description: "Present symbol menu — model picks next target or signals done"
    context: {
        required: ["edit_session_id", "symbol_menu_options"]
        optional: ["selected_symbols", "selection_turn"]
    }
    turn: #Turn & {
        response_shape: "menu_single"
        sections: [
            {type: "role",        template: "personas/code_author"},  // short-formed in session
            {type: "evidence",    template: "patch/selection_state"},  // running "selected so far" list
            {type: "instruction", template: "patch/select_symbols_instruction"},
            {type: "options"},
            {type: "envelope"},
        ]
        response: {
            options_from: {
                source:      "context"
                context_key: "symbol_menu_options"
            }
            stock: [
                _stock_options.__full_rewrite__,  // terminal, escape to full file rewrite
                _stock_options.__bail__,          // terminal, file does not need changes
                _stock_options.__done__,          // terminal, finish selection and proceed
            ]
            publish_selection: "selection_choice"
        }
        transitions: {
            options: {
                "__full_rewrite__": "close_full_rewrite"
                "__bail__":         "capture_bail_reason"
                "__done__":         "select_symbols_complete"  // new helper — publishes final flags
            }
            default:   "select_symbols"         // loop: a symbol was picked, iterate
            no_answer: "close_full_rewrite"     // retries exhausted → fall back to broader edit
        }
        config: {temperature: "t*0.3"}
        retries: 3  // keeps current behavior; empirical recovery on turn 3 for menu-site
    }
    pre_compute: [
        // Renders the running selected-so-far list as evidence content.
        {formatter: "format_selection_state",
         output_key: "selection_state_rendered",
         params: {selected: {$ref: "context.selected_symbols"}}},
    ]
    resolver: {
        // Rule resolver preserved — action wrapper flags drive flow control,
        // same pattern as Site #10's rewrite_symbol.
        type: "rule"
        rules: [
            {condition: "result.selection_complete == true and result.symbols_selected > 0", transition: "begin_rewrites"},
            {condition: "result.selection_complete == true and result.symbols_selected == 0", transition: "no_changes_needed"},
            {condition: "result.full_rewrite_requested == true", transition: "close_full_rewrite"},
            {condition: "result.bail_requested == true", transition: "capture_bail_reason"},
            {condition: "result.symbol_selected == true", transition: "select_symbols"},
            {condition: "true", transition: "begin_rewrites"},
        ]
    }
    publishes: ["selected_symbols", "selection_turn"]
}
```

---

## Decisions landed

### Wrapper action preserved — keeps runaway-prevention guards

**Settled.** Same pattern as Site #10 (`rewrite_symbol_turn`): the `#Turn` handles inference + option extraction; the action wrapper handles pre-inference policy guards and post-inference state accumulation.

Three guards to preserve in the wrapper:

1. **Auto-complete short-circuit** — if all selectable symbols already in `selected`, skip inference and return complete. Cheap optimization, not reachable from schema declarations.
2. **50% cap → full_rewrite** — if selected count ≥ half the selectable symbols, unilaterally route to full_rewrite. Runaway prevention: the model trying to rewrite most of the file should do full rewrite, not one-by-one symbol editing.
3. **Max-turn cap** (`selectable_count + 4` turns) — hard stop on selection loops.

None of these are schema-declarable policy. They're behavioral safeguards that must run outside the inference turn.

### `__full_rewrite__`, `__bail__`, `__done__` as explicit stock options

**Settled.** Currently these are mixed into `symbol_menu_options` by the upstream `extract_symbol_bodies` action (`__full_rewrite__`, `__bail__`) and inline-appended by `select_symbol_turn` at render time (`__done__`). Under the schema, all three are declared as stock options on the turn itself.

**Benefits:**
- Flow CUE directly shows what escape hatches are available — no hunt through action code to find them.
- `_stock_options` catalog provides the canonical descriptions; lint enforces consistency.
- Upstream `extract_symbol_bodies` action simplifies: returns only actual symbol entries. No more manual escape-hatch appending.
- The `select_symbol_turn` action stops rendering `__done__` inline.

### `no_answer → close_full_rewrite` with retries

**Settled.** The current action's parse-failure path silently routes to "done with 0 selected" → `no_changes_needed`. This is the silent-empty hazard Site #4's record established — parse failure resolving to a default flow target is exactly what `no_answer` was designed to make impossible.

Under the schema:
- **`retries: 3`** — first-line defense. Most parse failures recover within two retries (empirically observed in `run_session` patterns, Site #17). Keeping the 3-retry default honors the documented recovery pattern.
- **`no_answer → close_full_rewrite`** — only fires when retries exhaust. Consistent with the 50%-cap guard's philosophy: "something's off with selection, fall back to broader edit." The file still gets edited; we just use the less-surgical mechanism.

**Rejected alternatives:**
- `no_answer → no_changes_needed` — literal port of current behavior. Preserves the bug class. Not acceptable.
- `no_answer → capture_bail_reason` — asks model to justify non-response. But the model has already failed to respond meaningfully three times; asking it to explain the confusion is unlikely to produce useful signal. `capture_bail_reason` is for when the *model* chose to bail, not as a framework-level recovery path.

### Running selection state as `evidence` section

**Settled.** The current action renders "Selected so far: foo, bar" inline in the menu prompt. Under the schema this becomes an `evidence` section populated by a pre-compute formatter (`format_selection_state`) that reads `context.selected_symbols` and emits a short string.

Each turn re-renders from the latest selection. Clean separation: instruction template stays constant across turns; evidence shows per-turn state progression.

The "## Selected so far" rendering (or similar) gives the model a persistent view of what it's committed to — useful context as the selection accumulates.

### Context-key sourcing (Pattern B) stays until observations land

**Settled.** `symbol_menu_options` is built by the upstream `extract_symbol_bodies` action, which runs tree-sitter on the current file. Effect-sourced, session-scoped — same observation-system-candidate profile as Site #5's `symbol_table`.

Until the parked observations system lands, stay Pattern B. When observations land, this site and Sites #4 + #5 migrate together.

### Temperature — `t*0.3`

**Settled.** Matches the menu-site cluster (Sites #4, #5, #9, #17). Revised from current `0.1` literal (argmax-adjacent under `t*N` resolution at 0.7 default).

### Publish-selection key

**Settled.** `publish_selection: "selection_choice"` — records which option the model picked in context. Different from `selected_symbols` (the accumulating list maintained by the wrapper action). The `selection_choice` is per-turn; `selected_symbols` is the running list.

### Session-turn rendering

**Settled.** Patch session seeded by `start_edit_session`. Role short-forms. `evidence` (running state), `instruction`, `options`, `envelope` render in full each turn.

### Flow graph — one new helper step, no deletions

**Settled.** `__done__` transitions to a new small step `select_symbols_complete` that publishes the final `selection_complete: true` flags. This avoids conflating the turn's `__done__` option (which the runtime's menu resolver handles) with the wrapper action's post-inference state (which the rule resolver reads).

Alternatively: let the wrapper action detect `selection_choice == "__done__"` and set `selection_complete: true` itself. Simpler. I'll go with that — no new step needed. The option's transition becomes `default` (falls back to the rule resolver, which reads the wrapper's result flags).

Revised transition table:
```cue
transitions: {
    options: {
        "__full_rewrite__": "close_full_rewrite"
        "__bail__":         "capture_bail_reason"
        // __done__ has no explicit transition — falls to default, where
        // the wrapper action's result flags drive the rule resolver.
    }
    default:   "select_symbols"         // loop OR fall through to rule resolver
    no_answer: "close_full_rewrite"
}
```

Wait — this creates ambiguity. If `default` is "select_symbols" (loop back), but `__done__` hasn't been explicitly handled, the flow would loop forever on done. The action wrapper solves this: when it sees `__done__` in the response, it sets `selection_complete: true` and the *step's* rule resolver (not the turn's transitions) routes to `begin_rewrites` or `no_changes_needed` based on count.

The turn's transitions section handles menu-routing for the runtime-resolvable cases (`__full_rewrite__`, `__bail__`). Everything else falls through to `default` which routes back to `select_symbols` — but `select_symbols`'s own step-level rule resolver reads the wrapper's flags first and can terminate the loop via `begin_rewrites` / `no_changes_needed`.

This is the same two-layer pattern from Site #10: turn handles inference-result routing where trivial, step-level rule resolver handles the business-logic branching based on wrapper flags. Clean.

---

## Templates to author at Step C

1. **`personas/code_author.yaml`** — shared with Sites #1, #10 (check alignment during authoring).
2. **`patch/select_symbols_instruction.yaml`** — "Pick the next symbol to modify, or select '__done__' to finish selection."
3. **`patch/selection_state.yaml`** — evidence template rendering the running "Selected so far: ..." list. Omits section entirely when empty (schema default).

## Formatters to register at Step C

- **`format_selection_state`** — reads `selected_symbols` list, emits human-readable string for the evidence section.

## Upstream action simplification

`extract_symbol_bodies` (in `ast_actions.py`) loses the escape-hatch appends. Current code appends `__full_rewrite__` and `__bail__` entries manually to `symbol_menu_options`. After migration, it returns only actual symbol entries; the turn declaration carries the escape hatches. Simpler output shape, cleaner responsibility.

---

## Cross-site follow-ups

- **Two-layer transition pattern** — Site #10 and Site #11 both use "turn routes stock/trivial options; step rule resolver branches on wrapper flags for business logic." Pattern: `options: {trivially_routable}`, `default: /* conservative fall-through */`, followed by step-level rule resolver reading action flags. Worth documenting at Step C as the standard pattern for action-wrapped turns.
- **Pattern B → observation migration (future)** — Sites #4 (`file_context` already is a projection), #5 (`symbol_table`), #11 (`symbol_menu_options`) will all migrate to the observations system when it lands. Unified migration pass.
- **Extract_symbol_bodies cleanup** — the upstream action simplifies as part of this migration. Small change, couples to this site's Step C work.
- **Stock option semantic drift** — `__bail__` on this site routes to `capture_bail_reason`. Same stock option used at Site #5 routes to `end_session_failure`. That's fine — stock options have consistent *semantics* ("abandon this task"), not identical *targets* (each flow's abandonment path differs). Flag pattern: stock options declare meaning; per-site transitions declare where that meaning takes you.
