# Site #4 — `diagnose_issue.pick_file`

**Status:** Approved for Step C migration.
**Response shape:** `menu_single` (projection-sourced options + stock options)
**Current file:** `flows/cue/diagnose_issue.cue` (lines 103-122)
**Current action code:** `agent/actions/diagnosis_session_actions.py::action_pick_suspect_file` (lines 236-400+)
**Empirical (892 run):** n=22, avg_in=706 tokens, avg_out=2 tokens (max 10). 22/22 successful after recent fixes.

---

## Turn definition

```cue
pick_file: #StepDefinition & {
    action: "inference"
    description: "Model picks the file most likely responsible for the error"
    context: {
        required: ["diagnosis_session_id"]
        optional: ["investigation_turn", "file_context"]
    }
    turn: #Turn & {
        response_shape: "menu_single"
        sections: [
            // In-session: renderer short-forms already-seeded sections.
            // Only instruction and options render in full.
            {type: "role",        template: "personas/diagnose_issue"},  // short-formed in session
            {type: "instruction", template: "diagnose_issue/pick_file_instruction"},
            {type: "options"},
            {type: "envelope"},
        ]
        response: {
            options_from: {
                source:     "projection"
                projection: "file_context"
                // The renderer composes each option's description from the
                // projection's raw data (import_deps[].responsibility, and
                // data_shapes[].consumed_by). The projection stays pure data;
                // the template layer handles per-option description rendering.
                description_from: "file_context"
            }
            stock: [
                _stock_options.__run_command__,   // compound — takes `command` arg
                _stock_options.__conclude__,
            ]
            publish_selection: "suspect_file"
        }
        transitions: {
            options: {
                "__run_command__": "run_command"
                "__conclude__":    "end_session_failure"
            }
            default:   "trace_symbols"
            no_answer: "pick_file_fallback"
        }
        config: {temperature: "t*0.3"}
    }
    publishes: ["suspect_file", "investigation_turn"]
}

// New step — observable first-file fallback for parse-exhausted case.
// Replaces the hand-rolled default in action_pick_suspect_file.
pick_file_fallback: #StepDefinition & {
    action:      "pick_file_default_first"
    description: "Parse-exhausted fallback: select first architecture module, queue correction"
    context: {
        required: ["diagnosis_session_id", "file_context"]
        optional: ["investigation_turn"]
    }
    resolver: {
        type: "rule"
        rules: [{condition: "true", transition: "trace_symbols"}]
    }
    publishes: ["suspect_file", "investigation_turn"]
}
```

---

## Decisions landed

### Compound `__run_command__` at pick_file

**Settled:** replace the current no-arg `run_command` option with the stock compound `__run_command__`. Model emits `{"choice": "__run_command__", "command": "..."}` in one turn; the command flows into context and Site #6 (`run_command` step) receives it pre-filled.

Eliminates the split "pick option → transition → ask for command" pattern that was a documented leakage source in the 892 trace.

### Description sourcing — template renders from raw projection data

**Settled:** projection stays pure data (`import_deps`, `data_shapes`). The option-description template composes presentation strings from projection fields (e.g., `"{file} — {responsibility}"` or `"{file} — data file consumed by {consumed_by}"`). Presentation concerns live in the template layer, not in the projection.

### Session-turn rendering policy — assume session-aware renderer

**Settled for this site, deferred as a cross-cutting Step C decision:** session turns declare the full section stack, but the renderer short-forms sections whose content already lives in the session seed (role, context, etc.). Only `instruction`, `options`, and `envelope` render in full each turn.

This gives consistent structural framing (the mode banner always appears in the same position) without wasting tokens re-emitting seed content.

Watchlist: validate this policy against Sites #5, #6, and #17 (other session-based sites) as we reach them. If any site reveals a gap, reopen.

### Custom action collapses into a standard `#Turn`

**Settled:** `action_pick_suspect_file` is deleted. Its responsibilities redistribute:
- **Inference call + retry** → runtime handles via `#Turn.retries` (default 3)
- **Menu extraction** → runtime's menu resolver
- **Option rendering** → renderer using projection + description template
- **First-file fallback** → new observable step `pick_file_fallback`

Code removed: ~150 lines of hand-rolled menu logic. Lint now sees the menu declaration in CUE and can validate option sources, stock option usage, and transition completeness.

### Parse-exhausted fallback — observable recovery (Option B + named step)

**Settled:** `no_answer` transitions to a new `pick_file_fallback` step that selects the first architecture module, queues the existing correction injection, and continues to `trace_symbols`. `trace_symbols`'s existing `wrong_file` branch provides the recovery path.

**Rationale for this over `end_session_failure`:**

- **Recovery path already exists and was designed for this case.** `trace_symbols`' `wrong_file` → `pick_file` loop was built specifically for "we picked the wrong file, try another" and routes back into investigation without losing evidence.
- **Whole-cycle retry has strictly worse failure math when the cause persists.** If the parse failure is about prompt shape or a specific file name the model can't emit, re-running `diagnose_issue` from scratch hits the same wall. Fallback at least gets signal from `trace_symbols`.
- **Observable.** Today's fallback is silent (a `logger.warning` + injection inside action code). The new step has a name in the flow graph, emits trace events, shows up in cycle stats. If the fallback triggers more than 5% of `pick_file` invocations, we have data to reopen the design with evidence.

**Reopen conditions:** fallback trigger rate >5% of pick_file calls across missions, OR trace evidence that fallback → trace_symbols → wrong_file → pick_file loops don't converge within 2 iterations. Either signal means the menu shape itself is the problem and whole-cycle retry becomes worth reconsidering.

### Transitions

| Outcome | Target |
|---|---|
| Any file choice (project file key) | `trace_symbols` (default) |
| `__run_command__` (compound, carries command) | `run_command` |
| `__conclude__` | `end_session_failure` (pick_file can't produce diagnosis) |
| Parse exhausted after retries | `pick_file_fallback` (no_answer) |

### Temperature — `t*0.3`

**Settled (revised).** Initially drafted at `t*0.1` to mirror the current action code's literal 0.1. Revised during Site #9's calibration pass: `t*0.1` resolves to 0.07 @ t=0.7 or 0.05 @ t=0.5 — essentially argmax. Too restrictive for menu selection where multiple options may be close in relevance and the model benefits from modest judgment flexibility.

Bumped to `t*0.3`, matching Site #9's calibration and joining the menu-site cluster (Sites #4, #5, #9, #17 all at `t*0.3`).

### Unchanged at this site

- Session lifecycle (`start_diagnosis_session` upstream, `end_session` downstream).
- Context contract — still consumes `diagnosis_session_id`, `investigation_turn`, `file_context`; still publishes `suspect_file`, `investigation_turn`.
- Session injections — continue to work via the `session_injections` ambient whitelist fix. The fallback step still queues corrections the same way.

---

## New action required at Step C

`pick_file_default_first` — a narrow action that:
1. Reads `file_context` from context.
2. Selects the first module file (`file_context.import_deps[0].file`).
3. Publishes `suspect_file` + `investigation_turn`.
4. Queues the "defaulted to X, choose 'examine another file' after tracing" injection via `agent/session_injections.py::queue`.
5. Emits a trace event tagged `fallback_first_file` for observability.

Simple enough that it doesn't warrant a dedicated module. Lives in `diagnosis_session_actions.py` alongside the existing diagnosis actions.

---

## Templates to author at Step C

1. **`personas/diagnose_issue.yaml`** — `---ACT AS---` persona block. Probably already exists in some form (current seed uses `_personas.diagnose_issue`); confirm alignment.
2. **`diagnose_issue/pick_file_instruction.yaml`** — "Which file is most likely responsible for the error? Select the file to examine."
3. **`diagnose_issue/file_option_description.yaml`** (or handled inline) — the per-option description renderer. Takes `import_deps[].file`, `import_deps[].responsibility`, `data_shapes[].file`, `data_shapes[].consumed_by`. Produces the final option description string.

## Projection slot updates

`file_context` projection already exists and produces the right raw data (`import_deps`, `data_shapes`). No projection change required — just add the `description_from: "file_context"` declaration to the turn, and have the renderer consume the raw fields.

If the renderer needs more structured description data than the projection currently returns, we add it to the projection at Step C. But from reading the current action code, `import_deps[].responsibility` and `data_shapes[].consumed_by` are already there.

---

## Cross-site follow-ups

- **Session-turn rendering policy** — validate against Sites #5, #6, #17 as we reach them. Decide finally in the Step C renderer implementation if no site reopens it earlier.
- **Fallback-step observability pattern** — first appearance. If other sites reveal similar "silent parse-fallback in action code" patterns, convert them to named steps the same way.
- **Compound `__run_command__`** — first use of a stock compound option in Step B analysis. Keep an eye on the ergonomics: if the option description in the menu needs to convey "provide the command here", the stock description may need revisiting.
- **`personas/diagnose_issue` persona** — confirm alignment with current `_personas.diagnose_issue` at Step C time.
