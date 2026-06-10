# Site #17 — `run_session.evaluate`

**Status:** Approved for Step C migration.
**Response shape:** `menu_compound` (two options + `reason` argument)
**Current file:** `flows/cue/run_session.cue` (lines 126-164)
**Current prompt:** `prompts/run_in_terminal/evaluate.yaml`
**Empirical (892 run):** 66 total inference calls across 22 step invocations (3.0 per step). 44 session_inferences + 22 menu_resolves. 2 compound-menu leaks in the prose-turn responses — `{"action": "close", "reason": "..."}` shape bleeding in from `plan_interaction`'s KV cache context.

---

## Turn definition

```cue
evaluate: #StepDefinition & {
    action: "inference"
    description: "Model evaluates whether to continue exploring or close the session"
    context: {
        required: ["mcp_session_id", "session_history"]
        optional: ["inference_session_id"]
    }
    turn: #Turn & {
        response_shape: "menu_compound"
        sections: [
            // In-session: renderer short-forms role already seeded by start_session.
            {type: "role",        template: "personas/run_session_operator"},  // short-formed
            // turn_info + last_output consolidate under evidence.
            {type: "evidence",    template: "run_in_terminal/evaluate_evidence"},
            {type: "instruction", template: "run_in_terminal/evaluate_rules"},
            {type: "options"},
            {type: "envelope"},
        ]
        response: {
            options: {
                continue_interaction: {
                    key:         "continue_interaction"
                    description: "CONTINUE — need to explore more. Choose when additional observations are needed, or when the first command was setup (cd, ls) and the actual program hasn't run yet."
                    arg: {name: "reason", description: "Brief explanation of why continuing (what you still need to observe)"}
                }
                close_session: {
                    key:         "close_session"
                    description: "CLOSE — done observing. Choose when the program ran and you observed its behavior, when you hit an error with enough info to report, when 5+ commands ran without progress, or when the goal has been achieved."
                    arg: {name: "reason", description: "Brief explanation of why closing (what you observed, or why stuck)"}
                }
            }
            publish_selection: "session_decision"
        }
        transitions: {
            options: {
                continue_interaction: "plan_interaction"
                close_session:        "close_session"
            }
            default:   "close_session"   // safety fallback if choice doesn't match options
            no_answer: "close_session"   // retries exhausted — close cleanly rather than fail
        }
        config: {temperature: "t*0.3"}
    }
    publishes: ["inference_response", "session_decision"]
}
```

---

## Decisions landed

### Two inference calls → one `menu_compound` turn

**Settled.** Today's site runs **two inference calls per invocation**: a session_inference requesting prose, then an `llm_menu_resolve` extracting the choice via a separate menu prompt. That's the split-flow pattern the schema is built to eliminate.

Collapses into one compound menu where `reason` (the prose) rides alongside `choice` (the control flow). The `reason` arg carries the observability value of the previous prose turn without a separate inference call.

**Empirical weight:** 66 total inferences across 22 step invocations today → ~22 total inferences after migration. A 3x reduction at this site alone. Over a mission with ~22 evaluate invocations, that's saving ~44 inference calls.

### `reason` argument kept (compound, not single)

**Settled.** Observability is the primary justification. When the model closes too early or fails to recognize the goal was met, the trace should show *why* the model made the decision — that's actionable debug data. The cost is minimal (~20-30 tokens), and the compound shape is already what we're using at the other investigation sites. Consistency is a secondary benefit.

Considered and rejected: dropping to `menu_single` for simplicity. Reason field is cheap and informative; removing it would save a tiny amount of output budget at the cost of every bad decision being mysterious in the trace.

### Section consolidation — `turn_info` + `last_output` → `evidence`

**Settled.** Current prompt has two separate sections for turn count and last command output. Schema allows one `evidence` section per turn. Template composes both: turn count first, then last command output. Same consolidation pattern as Sites #1, #2, and #5.

### Decision guidance migrates from instruction to option descriptions

**Settled.** Current prompt's `output_format` section mixes three things:
- Behavioral rules ("Your job is to RUN and OBSERVE, not to fix issues")
- Decision guidance ("CLOSE when... CONTINUE when...")
- Output format

Under the schema:
- **Behavioral rules** stay in `instruction` template — cross-cutting operational norms
- **Decision guidance** moves into each option's `description` — makes options self-documenting (a menu option's description IS the "when to pick this" text)
- **Output format** handled by envelope

This is a general pattern worth noting: when prompts today teach "pick option X when...", that guidance belongs in option descriptions, not instruction text. Keeps the instruction block focused on what doesn't vary across options.

### `persona` section merged into `role`

**Settled.** Current prompt has both `system_role` and `persona` as separate sections. They're duplicative — both tell the model who it is. Merge into one `role` section (`personas/run_session_operator`). Renderer short-forms this when the session is already seeded with the persona via `start_session`.

### `no_answer → close_session` (safe default)

**Settled.** If the menu retry budget exhausts without a parseable choice, close the session cleanly rather than failing. Matches current `default_transition: "close_session"` behavior. A session that can't decide is better closed cleanly than escalated to `close_failure`.

### Temperature `t*0.3` kept

**Settled.** Control-flow decision with brief articulated reasoning. Deterministic-leaning, but the compound `reason` field benefits from slight flexibility. `t*0.3` hits the right regime.

Deliberately different from Site #7's `t*0.4` bump. Site #7 had a documented failure mode (over-rigid charter adherence) that needed more interpretive flexibility. Site #17's two-option choice isn't susceptible to over-interpretation — the decision is concrete enough that low temperature serves it well.

### Options stay flow-specific, not stock

**Settled.** Neither `continue_interaction` nor `close_session` is promoted to the `_stock_options` catalog. Their semantics are specific to run_session:
- "continue" means "send another command to THIS PTY"
- "close_session" means "terminate THIS PTY session"

No other flow has these exact semantics. The stock catalog's growth discipline (2+ flows with identical semantics) doesn't trigger.

### Schema registry — not shared with Site #7

**Settled.** Site #7's `evaluation` schema (`{goal_met: bool, summary: string}`) is different from Site #17's response (`{choice: "...", reason: "..."}`). Site #7 is a judgment output; Site #17 is a control-flow decision with justification. The fields aren't interchangeable:
- `goal_met` judges success; `choice` picks a flow branch
- `summary` is a post-hoc assessment; `reason` is a pre-action justification

No registry entry needed for this site — the menu_compound response shape is defined by the options and args, not a json_document schema.

### Downstream unchanged

- `plan_interaction` (Site #8, next) — receives control when `continue_interaction` picked
- `close_session` — receives control when `close_session` picked or on default/no_answer
- `inference_session_id` preservation across the boundary — unchanged

---

## Templates to author at Step C

1. **`personas/run_session_operator.yaml`** — `---ACT AS---` block. Short; the session seed already carries the execution_persona from the caller (interact or quality_gate), so this persona is a lightweight supplement describing the "manage a terminal session" role on top of the interaction_persona.
2. **`run_in_terminal/evaluate_evidence.yaml`** — consolidated template rendering `Commands executed so far: {turn_count}` followed by `Last command output: {last_command_output}`. Both sub-blocks conditional — render only if the context key is non-empty.
3. **`run_in_terminal/evaluate_rules.yaml`** — behavioral rules only ("Your job is to RUN and OBSERVE, not to fix issues. Do NOT install packages, edit files, or attempt repairs."). Decision guidance removed — lives in option descriptions now.

---

## Architectural observation — the "reference clean menu" story revisited

Site #5's analysis cited `interact.run_session`'s menu as 22/22 clean and used that as motivation for Option C's unified compound pattern. Closer empirical inspection at Site #17 reveals this was *only the menu extraction turn*: 22 of 22 `llm_menu_resolve` calls returned clean choices. What wasn't counted in the original "22/22 clean" framing was the preceding **prose turn**, which had 2 of 44 responses leaking compound shape from `plan_interaction`'s KV cache context.

**The reference-clean pattern is the menu extraction call, not the step as a whole.** The prose turn preceding it has its own drift. Site #17's migration collapses both turns into one compound menu, which:

1. Eliminates the prose-turn drift by putting prose behind the compound arg (which carries its own mode-priming from the banner).
2. Reduces inference count at this site 3x.
3. Reinforces Site #5's Option C choice — the single-menu-loop pattern is right because it avoids exactly this kind of dual-turn drift.

This observation doesn't change any decision in Site #5's record. It strengthens the confidence in Option C.

---

## Cross-site follow-ups

- **The "decision guidance in option descriptions" pattern** — first explicit articulation. Applies to any site where the prompt today says "pick CONTINUE when... pick CLOSE when..." format. Watch for this at Sites #8 (plan_interaction) and #18 (run_session.plan_interaction — action envelope).
- **Site #8 (`interact.plan_interaction`) dependency** — this evaluate step's `continue_interaction` target routes back to plan_interaction when continuing. That step is next up for analysis.
- **Menu-extraction-is-clean-but-preceding-prose-is-not pattern** — if other sites have this shape (prose turn + menu extraction), they exhibit the same dual-turn drift. Site inventory should flag any similar structures for same treatment.
