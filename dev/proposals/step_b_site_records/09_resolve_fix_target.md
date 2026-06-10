# Site #9 — `mission_control.resolve_fix_target`

**Status:** Approved for Step C migration.
**Response shape:** `menu_single` (projection-sourced options, Pattern C)
**Current file:** `flows/cue/mission_control.cue` (lines 268-284)
**Current action code:** `agent/actions/mission_actions.py::action_build_fix_target_menu` (lines 1411-1491, ~80 lines — eliminated)
**Empirical (892 run):** n=14, avg_in=230 tokens, avg_out=2 tokens (max 4). **14/14 clean** modulo fence wrapping. Cleanest menu site in the codebase after `run_session.evaluate`.

---

## Turn definition

```cue
resolve_fix_target: #StepDefinition & {
    action: "inference"
    description: "Model selects which file to fix based on diagnosis"
    context: {
        required: ["mission"]
        optional: ["dispatch_config"]
    }
    turn: #Turn & {
        response_shape: "menu_single"
        sections: [
            {type: "role",        template: "personas/fix_target_selector"},
            {type: "evidence",    ref: {$ref: "context.dispatch_config.diagnosis_summary"}},
            {type: "instruction", template: "mission_control/resolve_fix_target_instruction"},
            {type: "options"},
            {type: "envelope"},
        ]
        response: {
            options_from: {
                source:     "projection"
                projection: "fix_target_menu"
                // Projection produces pre-composed description strings.
                // See schema comment about Pattern C variants.
            }
            publish_selection: "selected_fix_target"
        }
        transitions: {
            default:   "apply_fix_target"
            no_answer: "check_phase"
        }
        config: {temperature: "t*0.3"}
    }
    publishes: ["selected_fix_target"]
}
```

---

## Decisions landed

### Promotion to Pattern C (projection-sourced)

**Settled.** Today's `build_fix_target_menu` action computes `fix_target_options` as a pure function of `mission.architecture` (modules + data_shapes) — that's projection-shaped work wearing a Pattern B costume because the projection system postdates this flow.

**Change:**
- Delete `build_fix_target_menu` step and `action_build_fix_target_menu` (~80 lines).
- Add `fix_target_menu` projection slot to `mission_control`'s `projections:` list.
- `resolve_fix_target` reads directly via `options_from: {source: "projection", projection: "fix_target_menu"}`.

**Benefits:**
- Pure projection — snapshot-reproducible, testable with a hand-built MissionState.
- One fewer step in the flow graph.
- Audit story simplified: projection slot is authoritative; no context-key indirection.
- Aligns with Site #4's principle that file-menu-from-architecture is Pattern C territory.

`diagnosis_context` (the current action's other output) is just `dispatch_config.diagnosis_summary`. Read directly in the turn's `evidence` section via `$ref: "context.dispatch_config.diagnosis_summary"`. No helper step required.

### Projection produces pre-composed description strings (Pattern C variant)

**Settled.** Unlike Site #4 where the projection returns raw data and the template composes descriptions, this site's description logic is complex enough that splitting is awkward:

```
"engine.py — Game loop and room navigation [defines: GameEngine, Room, Parser (+2 more)]"
```

The `defines` truncation (cap at 6, `(+N more)`) belongs with the composition. Projections produce display-ready strings; the options renderer just lays them out.

**Both Pattern C variants are valid:**
- Projection returns raw data → template composes (Site #4)
- Projection returns pre-composed descriptions → template renders as-is (Site #9)

Step C's schema documentation should note both variants. Which is right depends on the complexity of the composition logic — simple concat can live in templates; truncation/sorting/conditional-labeling belongs in the projection function.

### Temperature — `t*0.3`

**Settled.** Lower than flow default (`t*0.5`) but not as restrictive as architecture's `t*0.2`. Menu selection with multiple relevant-looking options benefits from modest flexibility — too low and the model argmaxes on marginal edges; too high and diagnosis weighing drifts.

**Retroactive calibration:** this decision prompted a review of Sites #4 and #5 (`pick_file` and `pick_action`), which were drafted at `t*0.1`. Reasoning: at `model_default=0.7`, `t*0.1` resolves to 0.07; at 0.5, 0.05 — essentially argmax. Bumped both sites to `t*0.3` for consistency with this site. See appendices to Site #4 and Site #5 records.

### `evidence` section — diagnosis context rendered directly from `dispatch_config`

**Settled.** Current prompt inlines `Diagnosis: {{context.diagnosis_context}}` in the prompt string. Under the schema, `evidence` section renders from `context.dispatch_config.diagnosis_summary` directly via `$ref`. No pre-compute formatter needed — the value is already a string in dispatch_config.

### Transitions — `no_answer → check_phase` (short-circuits apply_fix_target stub path)

**Settled.** Currently when the menu fails to extract a choice, it routes to `default_transition: "apply_fix_target"` with an empty selection, and `apply_fix_target` itself returns `target_applied: false` → `check_phase`. The schema's explicit `no_answer` routes directly to `check_phase`, eliminating one pointless step in the failure path.

### Banner replaces no existing transition marker

**Observation, not a decision.** Unlike Sites #7 and #17, this site's prompt has no existing `---BLOCK---` mode-shift marker. The `=== MENU CHOICE ===` banner is a pure addition, providing priming where there was none.

### Stateless turn — full section rendering, no session short-form

**Settled.** `mission_control` runs fresh each invocation (no memoryful session). All sections render in full. No persona-already-in-seed optimization applies.

### Templates to author at Step C

1. **`personas/fix_target_selector.yaml`** — `---ACT AS---` block. Short; role is "reviewer picking which file to fix based on diagnosis." First-class role rather than relying on whatever implicit role the current bare prompt conveys.
2. **`mission_control/resolve_fix_target_instruction.yaml`** — "Based on the diagnosis, which project file most likely needs to be fixed?" + "Select the file to fix." Guidance consolidates from the current inline prompt.

### New projection slot

`fix_target_menu` — added to `_projections` catalog. Computed from `mission.architecture.modules` and `mission.architecture.data_shapes`, returns a list of `{id: str, description: str}` entries with pre-composed display strings.

---

## Cross-site follow-ups

- **Pattern C variant documentation** — schema comments should explicitly describe both variants (raw data + template compose vs. projection pre-composes). First site to require the distinction.
- **Sites #4 and #5 temperature retroactive** — appended to those records. Both bumped from `t*0.1` to `t*0.3` by inference from the calibration done here.
- **`fix_target_menu` projection** — new projection slot required at Step C. May share implementation details with `file_context` (Site #4's file-picker projection), though the consumers are different and descriptions differ enough that they're separate slots.

---

## Temperature calibration — site summary

This site triggered an explicit calibration pass. Recording for future reference:

| Temperature | Resolves at t=0.7 | Resolves at t=0.5 | Regime |
|---|---|---|---|
| `t*0.1` | 0.07 | 0.05 | Argmax-adjacent — avoid |
| `t*0.2` | 0.14 | 0.10 | Tight determinism (architecture) |
| `t*0.3` | 0.21 | 0.15 | Menu selection / control-flow decisions |
| `t*0.4` | 0.28 | 0.20 | Prose + code generation / interpretive flexibility |
| `t*0.5` | 0.35 | 0.25 | Flow default (mission_control) |
| `t*0.6` | 0.42 | 0.30 | General flow default, creative work |

Post-migration site temperatures cluster at `t*0.3` for menus, `t*0.4` for generation, `t*0.2` for internal-consistency-critical tasks. No site below `t*0.2` after the calibration.
