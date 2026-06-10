# Site #7 — `interact.evaluate_outcome`

**Status:** Approved for Step C migration.
**Response shape:** `json_document`
**Current file:** `flows/cue/interact.cue` (lines 197-217)
**Current prompt:** `prompts/interact/evaluate_session.yaml`
**Empirical (892 run):** n=22, avg_in=287 tokens, avg_out=32 tokens (max 45). Apparently clean — no documented leak pattern.

---

## Turn definition

```cue
evaluate_outcome: #StepDefinition & {
    action: "inference"
    description: "Evaluate whether the product interaction achieved its goal"
    context: {
        optional: ["terminal_output", "inference_session_id"]
    }
    turn: #Turn & {
        response_shape: "json_document"
        sections: [
            // In-session: renderer short-forms already-seeded sections.
            // The session's run_session persona is still alive in KV cache.
            {type: "role",        template: "personas/interact_evaluator"},  // short-formed in session
            // Evidence renders conditionally — fallback if session context is lost.
            {type: "evidence",    ref: {$ref: "context.terminal_output"}},
            // Problem section uses the ---TEST OBJECTIVE--- bounded form for
            // attention-boundary. Template carries the markers; the schema
            // section type is just `problem`.
            {type: "problem",     template: "interact/test_objective_bounded"},
            {type: "instruction", template: "interact/evaluate_rules"},
            {type: "envelope"},
        ]
        response: {
            schema_id: "evaluation"
        }
        transitions: {
            default:   "parse_evaluation"
            no_answer: "compile_report_failure"
        }
        config: {temperature: "t*0.4"}  // bumped from t*0.2 — see Decisions
    }
    publishes: ["inference_response"]
}
```

---

## Decisions landed

### Response shape — `json_document`

**Settled.** Two-field structured output: `{goal_met: bool, summary: string}`. Not a menu; the model is producing a typed judgment. Schema registered as `evaluation` in the shared schema registry.

### Temperature — bump from `t*0.2` to `t*0.4`

**Settled.** Motivated by a documented failure mode: the evaluator follows the test charter too rigorously and fails goals that have good passing evidence. Concretely — charter outlines steps 1-2-3, session hits the real test objective on step 1, session ends. Model sees "incomplete checklist" and emits `goal_met: false` despite evidence satisfying the objective.

The current prompt explicitly warns against this (*"The test charter was a GUIDE for the session, not a checklist. If the session ended before every planned step was executed, judge whether what you observed is sufficient evidence..."*), but at `t*0.2` the rule isn't strong enough to override the model's natural pattern-matching toward the visible checklist.

`t*0.4` gives room for the model to synthesize across competing signals — to weight "stated objective was observed" above "not every planned step was executed" when those conflict. Matches the regime we use for code generation and is the "needs flexibility but not creativity" default.

Pushed back against going higher (`t*0.5+`) — evaluation benefits from anchoring to the rule set, and too-creative interpretation risks the opposite failure (generous pass on ambiguous evidence).

### `---TEST OBJECTIVE---` bounded rendering — template-layer concern

**Settled.** The existing `---TEST OBJECTIVE---` / `---END TEST OBJECTIVE---` attention-boundary markers remain in the prompt. They're part of the `problem` section's template content, not a new schema feature. Schema section type stays `problem`; renderer emits whatever the template provides. The bounded-content convention is shared with `---ACT AS---` / `---PEERS---` / `---TERMINAL CLOSED---` — all fall under "template-level content, not schema-level structure."

### `---TERMINAL CLOSED---` transition marker — removed

**Settled.** The current prompt's `transition` section (`---TERMINAL CLOSED---`) signals mode shift from interactive testing to assessment. Under the new schema, the `=== JSON DOCUMENT ===` banner at the top of the prompt carries the same signal more strongly. The triple-dash transition marker becomes structurally redundant.

This is the first concrete example of a banner replacing existing in-flow transition prose. Pattern generalizes: wherever the current codebase uses `---BLOCK---` to signal mode shifts, the banner replaces them.

### `evidence` section renders conditionally

**Settled.** The current `terminal_output_fallback` section renders `when: context.terminal_output` — defensive, handles the case where inference session was lost. Schema's `ref: {$ref: "context.terminal_output"}` with implicit omit-when-empty behavior preserves this exactly. No explicit `when:` clause needed — the schema's default is "omit section if source is empty."

### Schema registry entry — `evaluation`

**Settled.** Short shape (2 required fields), but earns a registry entry. Shapes are:

```
evaluation:
  goal_met: bool (required)
  summary:  string (required)
```

Named `evaluation` per your instruction (not `interaction_evaluation` — less verbose, no namespace needed if the registry shapes are flat).

Cross-site precedent: Site #17 (`run_session.evaluate`) might want to share this schema. Decision deferred to Site #17's analysis — next in queue.

### Session-turn rendering

**Settled (inherited from Site #4's policy):** `role` section declared but short-formed by the renderer since the session seed (run_session's `execution_persona`) already loaded the persona. `instruction`, `problem`, `evidence`, `envelope` render in full.

### Downstream unchanged

- `parse_evaluation` step (rule-based branching on `goal_met`) unchanged.
- `end_eval_session_success` / `end_eval_session_failure` — unchanged.
- `compile_report_success` / `compile_report_failure` — unchanged.
- `publishes: ["inference_response"]` unchanged.

---

## Templates to author at Step C

1. **`personas/interact_evaluator.yaml`** — `---ACT AS---` block. Likely lightweight since the session already carries the tester persona; this adds only the "you're now evaluating" shift.
2. **`interact/test_objective_bounded.yaml`** — renders `---TEST OBJECTIVE---\n{input.flow_directive}\n---END TEST OBJECTIVE---`. The bounded form preserved from current template.
3. **`interact/evaluate_rules.yaml`** — the current `instructions` section's rules block. Lightly reworded (remove the inline schema example since the envelope carries that now; retain the behavioral rules).

## Schema registry entry

`evaluation` — added to the schema registry at Step C. This is the second entry after `architecture_plan` (Site #2). `diagnosis_document` (Site #5's force_conclude, with Site #3's `recommended_flow` field) will be the third.

---

## Cross-site follow-ups

- **Site #17 schema sharing** — next in queue. Decide whether `run_session.evaluate`'s output shape coincides with `evaluation` schema or is genuinely different. Flag for analysis on Site #17.
- **Banner-replaces-transition-block pattern** — `---TERMINAL CLOSED---` → `=== JSON DOCUMENT ===` is the first instance. Watch for similar patterns at other sites: any in-prompt mode-shift marker (`---ASSESSMENT MODE---`, `---DIAGNOSIS---`, `---PRODUCTION---`, etc.) should collapse into its corresponding banner.
- **Template rule "don't include inline schema examples"** — enforceable lint: after migration, schema-example blocks inside instruction templates are redundant (the envelope carries the example). Could be a lint warning at Step C.
