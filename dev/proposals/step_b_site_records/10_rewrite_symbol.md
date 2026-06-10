# Site #10 — `patch.rewrite_symbol` + `patch.capture_bail_reason`

**Status:** Approved for Step C migration.
**Response shapes:** `code` (rewrite_symbol) + `prose` (capture_bail_reason)
**Current file:** `flows/cue/patch.cue` (lines 99-115 and 148-161)
**Current action code:** `agent/actions/ast_actions.py::rewrite_symbol_turn` — one action serving both steps via `bail_prompt` parameter branch (~500 lines including validation helpers)
**Empirical (892 run):** rewrite_symbol n=5, in=138, out=188 (max 276). Small sample, no documented leaks.

---

## Structural observation

The current `rewrite_symbol_turn` action is doing double duty: it's called from both `rewrite_symbol` (produces code) and `capture_bail_reason` (produces prose), with a `bail_prompt: true` param flag toggling between two genuinely different turns. Under the schema, these split cleanly into separate `#Turn`s with different response shapes.

There's also kind-aware branching inside the rewrite path (class vs function) that drives two distinct instruction templates. This motivated a small schema primitive extension — `#Section.template: string | #Ref` — to allow dynamic template selection.

A third concern is domain-specific validation: the current action validates that the rewritten code's top-level AST node type matches the original kind (class → class, function → function) and re-prompts with a targeted correction on mismatch. This stays in the action wrapper — the schema's generic `retries` handles parse failures, but kind-validation is a separate post-inference check.

---

## Schema primitive changes

### Promoted `target_file` → `target_entity`

Rename-only change in `flows/cue/turn.cue`. The `target_entity` section type covers "the specific entity under consideration" — file, symbol, or similar. Rendered title is site-contextual via the existing `title?: string` field.

Applied to: schema primitive, self-test, existing site records (#1, #5), primitives proposal, SOUL primer.

Rationale: `target_file` holding a symbol body at this site would confuse anyone reading the flow CUE. Clearer vocabulary up front is cheaper than retrofitting later.

### Extended `#Section.template: string | #Ref`

Minor extension. Section templates can now be dynamic references, not just static strings. Enables kind-aware instruction selection at this site. Future sites may use it for other forms of runtime template selection.

```cue
#Section: {
    type: #SectionType
    ref?:      #Ref
    template?: string | #Ref    // Extended: can be ref to a pre-computed template ID
    literal?:  string
    // ...
}
```

Step C renderer: when `template` is a `#Ref`, resolve to a template ID at render time, then load. Single resolution step.

---

## Site #10a — `patch.rewrite_symbol`

### Turn definition

```cue
rewrite_symbol: #StepDefinition & {
    action: "rewrite_symbol_turn"  // wrapper: turn + kind validation + retry
    description: "Model produces complete rewritten symbol body"
    context: {
        required: ["edit_session_id", "current_symbol"]
        optional: ["rewrite_queue", "file_content", "file_content_updated", "file_path", "mode"]
    }
    turn: #Turn & {
        response_shape: "code"
        sections: [
            {type: "role", template: "personas/code_author"},  // shared with Site #1
            {type: "target_entity",
             ref: {$ref: "context.current_symbol"},
             title: "Symbol to rewrite"},
            {type: "instruction",
             template: {$ref: "derived.kind_instruction_template"}},  // dynamic template ref
            {type: "envelope"},
        ]
        response: {
            language: "python"  // TODO: should this be {$ref: ...} from current_symbol.language?
                                // Parked: today symbols are Python-only; widens when we add other languages.
        }
        transitions: {
            // The turn's own transitions aren't used here — the step's
            // rule resolver branches on action-wrapper results
            // (rewrite_success, has_next) to decide loop vs finalize.
            default:   "finalize"
            no_answer: "finalize"
        }
        config: {temperature: "t*0.4"}
    }
    pre_compute: [
        // Resolves kind_instruction_template from current_symbol.kind
        {formatter: "select_rewrite_instruction_template",
         output_key: "kind_instruction_template",
         params: {source: {$ref: "context.current_symbol"}}},
    ]
    resolver: {
        // Rule resolver unchanged — branches on action wrapper's result flags
        type: "rule"
        rules: [
            {condition: "result.rewrite_success == true and result.has_next == true", transition: "rewrite_symbol"},
            {condition: "result.rewrite_success == true", transition: "finalize"},
            {condition: "true", transition: "finalize"},
        ]
    }
    publishes: ["current_symbol", "rewrite_queue", "file_content_updated"]
}
```

### Decisions landed

**Response shape — `code`.** Same regime as Sites #1 and #16. Required markdown fence. Language tag `python` (parked consideration: dynamic per symbol language when the agent expands beyond Python).

**No `=== FILE: path ===` inside-fence protocol at this site.** Unlike Site #1 (file creation) or Site #16 (full file rewrite), rewrite_symbol produces a symbol body only. The splicer knows where to insert via AST position; no path metadata needed in the output.

**Kind-aware instruction templates — dynamic template reference.** Two templates authored at Step C:
- `patch/rewrite_class_instruction.yaml` — class-specific guidance
- `patch/rewrite_function_instruction.yaml` — function-specific guidance

A pre-compute formatter (`select_rewrite_instruction_template`) reads `current_symbol.kind` and emits the appropriate template ID to context. The section's `template: {$ref: "derived.kind_instruction_template"}` resolves to that ID at render time.

**Kind-validation + re-prompt stays in action wrapper.** Schema's `retries` handles parse failures (no response, invalid fence). Kind-mismatch validation (Python produced a function where a class was expected) is post-inference validation with a domain-specific correction prompt — different concern, different layer. Action wrapper invokes the turn, extracts code, validates kind, optionally re-invokes with correction, then returns result flags to the step's rule resolver.

The wrapper is smaller than today: prompt construction moves to templates, validation logic is preserved verbatim.

**Temperature — `t*0.4`.** Matches code-generation regime (Sites #1, #16). Current action uses literal `0.3`, which at flow default 0.7 is roughly `t*0.43` — `t*0.4` is the calibrated equivalent.

**Session-turn short-forming.** Patch session seeded by `start_edit_session` with file context and persona. Role section short-forms. `target_entity`, `instruction`, `envelope` render in full. The symbol body renders each turn because which symbol is under rewrite varies by iteration.

**Transitions — step-level rule resolver preserved.** The turn's default/no_answer both route to `finalize` as a fallback; the real branching happens on the step's rule resolver reading action-wrapper result flags. This is the right division — turn handles inference outcome, step handles flow-control branching on business logic.

### Templates to author at Step C

1. **`personas/code_author.yaml`** — shared with Site #1 (check alignment during migration).
2. **`patch/rewrite_class_instruction.yaml`** — "Rewrite this class. Include all decorators, the class statement, and the full body..." etc.
3. **`patch/rewrite_function_instruction.yaml`** — "Rewrite this function. Do not include anything outside the function body..." etc.

### Formatters to register at Step C

- **`select_rewrite_instruction_template`** — reads `current_symbol.kind`, returns template ID string (`patch/rewrite_class_instruction` or `patch/rewrite_function_instruction`).

---

## Site #10b — `patch.capture_bail_reason`

### Turn definition

```cue
capture_bail_reason: #StepDefinition & {
    action: "capture_bail_turn"  // new narrow action, replaces bail-branch of rewrite_symbol_turn
    description: "Capture model's reasoning for bailing from the edit"
    context: {
        required: ["edit_session_id"]
        optional: ["file_content", "file_path", "current_symbol", "mode", "rewrite_queue"]
    }
    turn: #Turn & {
        response_shape: "prose"
        sections: [
            {type: "role",        template: "personas/code_author"},  // short-formed in session
            {type: "problem",     template: "patch/bail_capture_context"},
            {type: "instruction", template: "patch/bail_capture_instruction"},
            {type: "envelope"},
        ]
        response: {}  // prose has no declared shape
        transitions: {
            default:   "close_bail"
            no_answer: "close_bail"  // session closes cleanly even without a reason
        }
        config: {temperature: "t*0.4"}
    }
    publishes: ["bail_reason"]
}
```

### Decisions landed

**Response shape — `prose`.** ~1-3 sentences explaining why the model bailed. No envelope, no JSON, just the writing. `=== WRITING ===` banner.

**Temperature — `t*0.4`.** Same regime as Site #17's compound reason argument and Site #15's research summary. Interpretive writing about what went wrong.

**`no_answer → close_bail` (not failed).** Missing a bail reason isn't worth failing the session on. The session closes cleanly regardless; the bail reason is observability data, not control flow. Matches current behavior.

**New narrow action `capture_bail_turn`** — replaces the `bail_prompt: true` branch of `rewrite_symbol_turn`. Simpler: invoke turn, extract response text, publish as `bail_reason`. No code extraction, no validation.

### Templates to author at Step C

1. **`patch/bail_capture_context.yaml`** — short `problem` section explaining "you requested to bail from this edit; capture why for the mission record."
2. **`patch/bail_capture_instruction.yaml`** — "In 1-2 sentences, explain why this task cannot be completed."

---

## Cross-site follow-ups

- **`personas/code_author` template** — first shared across Sites #1, #10a, #16 (rewrite). Check alignment during Step C authoring.
- **Dynamic template reference (`#Section.template: string | #Ref`)** — first use. If other sites reveal similar kind-aware or mode-aware instruction branching, the pattern applies. Currently: only patch.rewrite_symbol.
- **Language tag on `code` shape** — parked: currently `"python"` is hardcoded. When the agent expands to other languages, this becomes dynamic (ref to symbol language or architecture language). Not blocking now; file a follow-up for language-awareness work (which also touches `build_repomap` and future `__grep__` replacement work).
- **Action-wrapper pattern for post-validation** — this site keeps a wrapper action (`rewrite_symbol_turn`) that invokes a `#Turn` then does kind-validation. If a second site needs post-inference domain validation with targeted correction, we have a pattern. Candidates to watch: Site #16 (rewrite.generate_rewrite) may need syntax validation; Site #2's parse_architecture could in principle use it though today it's a separate step.
- **Bail reason pattern** — this is the first `prose` site producing a short "why" explanation. Site #17's compound reason argument is analogous. If a third reason-producing site emerges, consider whether bail_reason / session_decision reason should share a template convention.
