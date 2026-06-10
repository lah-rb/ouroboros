# Site #8 — `interact.plan_interaction`

**Status:** Approved for Step C migration.
**Response shape:** `prose`
**Current file:** `flows/cue/interact.cue` (lines 139-163)
**Current prompt:** `prompts/interact/plan.yaml`
**Empirical (892 run):** n=11, avg_in=1673 tokens, avg_out=130 tokens (max 175). One-shot per session, no documented leak pattern.

---

## Turn definition

```cue
plan_interaction: #StepDefinition & {
    action: "inference"
    description: "Craft a test charter for the run_session sub-flow"
    context: optional: ["project_manifest", "repo_map_formatted"]
    turn: #Turn & {
        response_shape: "prose"
        sections: [
            {type: "role",          template: "personas/charter_author"},
            {type: "problem",       template: "interact/test_objective_bounded"},  // shared with Site #7
            {type: "context_files", template: "interact/project_and_code_structure"},  // merged project_files + repo_map
            {type: "dependencies",  ref: {$ref: "context.interaction_brief"}},
            {type: "instruction",   template: "interact/charter_specification"},
            {type: "envelope"},
        ]
        response: {}  // prose has no declared shape
        transitions: {
            default:   "run_session"
            no_answer: "failed"
        }
        config: {temperature: "t*0.4"}
    }
    pre_compute: [
        // Unchanged from current flow
        {formatter: "render_interaction_context", output_key: "interaction_brief"
            params: {source: {$ref: "input.interaction_context"}}},
        {formatter: "format_project_file_list", output_key: "project_file_list"
            params: {source: {$ref: "context.project_manifest"}}},
    ]
    publishes: ["execution_persona"]
}
```

---

## Decisions landed

### Response shape — `prose`

**Settled.** First site in the Step B pass classified as `prose`. Output is a charter document — a self-contained narrative ~250 words long, explicitly not JSON or structured data. The five internal sections (ROLE, LAUNCH, TEST STEPS, PASS CRITERIA, FAIL CRITERIA) are author-guidance for the downstream run_session tester, not a parsing contract.

The `=== WRITING ===` banner primes the model for prose output.

### `interaction_brief` section → `dependencies` (with caveat)

**Settled with caveat.** The `interaction_brief` section renders rich product knowledge from the `render_interaction_context` formatter: launch command hints, world data file contents, command vocabulary. This isn't code structure, nor file contents in the usual dependency sense — it's domain knowledge the charter must be grounded in.

Stretching `dependencies` here feels slightly awkward. The fit isn't as clean as at Site #1 (data contracts + file excerpts) where both components were genuinely dependency-shaped. This is more "author's reference material" than "contracts the output must honor."

**Reopen condition:** if a second site emerges with an analogous "domain knowledge / reference material" need that also feels awkward under `dependencies`, introduce a new `notes` (or `reference`) section type at that point. Until then, stretch `dependencies` rather than grow the vocabulary on a single-site need. Consistent with the "promote when recurring, not on first sighting" discipline.

Watchlist: Site #13 (`project_ops.plan_setup`) might have similar reference-material inputs. Check at that site.

### `context_files` consolidation — project_file_list + repo_map_formatted

**Settled.** Same pattern as Sites #1, #2, #5, #17. Two source sections (`project_context` and `repo_map` in current template) compose into one `context_files` rendering via template: project files first, then code structure.

### Drop "don't produce JSON/markdown/structured data" negations from the prompt

**Settled.** Current prompt has three explicit negations:
- "Do NOT produce JSON, markdown, or structured data"
- "Write ONLY the charter text. No preamble, no explanation, no JSON."
- (implicit: keep it prose)

The `=== WRITING ===` banner handles all three via positive priming — it tells the model "prose mode" rather than fighting against JSON/markdown priors through repeated denials. Dropping the negations makes the prompt shorter and more focused on what the charter should contain.

General pattern: negation-heavy prompts fighting model priors are a signal that banner priming isn't doing its job (or hasn't been introduced yet). Post-migration, lint could flag "Do NOT X" instruction patterns for review.

### Length guidance — "close to 250 words" (not "under 250")

**Settled.** Current prompt says "under 250 words." Revise to "close to 250 words." Models interpret "under" asymmetrically — aggressive undercutting, producing charters at ~180 words that short-change the downstream tester. "Close to" centers the target and yields charters clustering closer to the intended ~230-240 word range.

This is a prompt-text tweak, not a schema feature. No length primitive added to the schema — a single-site length constraint doesn't earn it.

General pattern worth noting: **when a target length matters, frame it as "close to N" rather than "under N."** Applies to any prose-shape site with length guidance. Add to SOUL.md primer? Probably not — it's a prompt-author discipline, not model-facing guidance.

### Temperature — `t*0.4` kept

**Settled.** Prose production requiring synthesis across rich inputs (project structure, world data, test objective). Matches code generation's regime. No reshape candidate here.

### Sharing `interact/test_objective_bounded` template with Site #7

**Settled.** Site #7 already registered this template for the `---TEST OBJECTIVE---\n{input.flow_directive}\n---END TEST OBJECTIVE---` bounded rendering. Site #8 uses the identical block. One template, two consumers. First concrete template-reuse across site records.

### Transitions — trivial mapping

**Settled.**
- Current `tokens_generated > 0` → `run_session` becomes `default: "run_session"`
- Current fallback → `failed` becomes `no_answer: "failed"`

No reshape.

### Unchanged

- Pre-compute list (both formatters) — carried through unchanged
- `publishes: ["execution_persona"]` — unchanged
- Context requirements — unchanged

---

## Templates to author at Step C

1. **`personas/charter_author.yaml`** — `---ACT AS---` block for the charter-authoring role. "QA test lead writing a self-contained tester brief." Lightweight.
2. **`interact/project_and_code_structure.yaml`** — merged `project_files` + `repo_map` rendering. Each sub-block renders conditionally (omit if its source context key is empty).
3. **`interact/charter_specification.yaml`** — the five-part charter spec (ROLE, LAUNCH, TEST STEPS, PASS CRITERIA, FAIL CRITERIA). Revised to drop negations and update "under 250" to "close to 250".

---

## Cross-site follow-ups

- **`notes` / reference-material section type** — park the idea. Reopen if Site #13 (`project_ops.plan_setup`) or any later site exhibits the same "awkward-under-dependencies" pattern.
- **Template reuse — `test_objective_bounded`** — shared between Site #7 and Site #8. First concrete reuse in Step B. Evidence that template modularity pays.
- **Negation-pattern-in-prompts** — general observation: when a prompt has 3+ "do NOT X" phrasings, banner priming is likely absent or weak. Post-migration, lint could flag this pattern. Noted for Step C linter.
- **"close to N" vs. "under N" length framing** — general prompt-author discipline. May be worth a short line in developer docs, not user-facing SOUL.md.
