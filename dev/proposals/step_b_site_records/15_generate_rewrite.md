# Site #15 — `rewrite.generate_rewrite`

**Status:** Approved for Step C migration.
**Response shape:** `code` (single fence, fence-with-path-comment protocol)
**Current file:** `flows/cue/rewrite.cue` (lines 54-84)
**Current prompt:** `prompts/modify_file/full_rewrite.yaml`
**Empirical (892 run):** n=1, in=3675 tokens, out=427 tokens. One-shot; succeeded.

Note: inventory's Site #15/#16 numbering reflects two use cases of the same inference step (fresh rewrites vs. validation-error-triggered). One record; both cases covered by the same migration.

---

## Turn definition

```cue
generate_rewrite: #StepDefinition & {
    action: "inference"
    description: "Generate complete file replacement"
    context: optional: ["project_manifest", "repo_map_formatted"]
    turn: #Turn & {
        response_shape: "code"
        sections: [
            {type: "role",          template: "personas/code_author"},  // shared with Sites #1, #10a
            {type: "problem",       template: "rewrite/task_with_validation_errors"},  // merged task + validation_errors
            {type: "target_entity",
             ref: {$ref: "context.target_file_content"},
             title: "Current File: {input.target_file_path}"},
            {type: "dependencies",  template: "rewrite/project_and_architecture"},  // merged file_excerpts + architecture_spec
            {type: "instruction",   template: "rewrite/generate_rewrite_instruction"},
            {type: "envelope"},
        ]
        response: {
            language: "python"  // parked: dynamic per language when agent expands beyond Python
        }
        transitions: {
            default:   "write_file"
            no_answer: "failed"
        }
        config: {temperature: "t*0.4"}  // bumped from t*0.3 — matches code-gen cluster
        retries: 3
    }
    pre_compute: [
        // Unchanged from current flow
        {formatter: "render_file_context", output_key: "architecture_spec"
            params: {source: {$ref: "input.file_context"}}},
        {formatter: "render_dependency_excerpts", output_key: "file_excerpts"
            params: {source: {$ref: "input.file_context"}}},
        {formatter: "render_data_contracts", output_key: "data_contract_block"
            params: {source: {$ref: "input.file_context"}}},
        {formatter: "extract_field", output_key: "target_file_content"
            params: {source: {$ref: "input.file_context"}, field: "target_content"}},
    ]
    publishes: ["inference_response"]
}
```

---

## Decisions landed

### Response shape — `code` (single-fence case of the multi-file protocol)

**Settled.** Under the multi-fence-capable framing established at Site #12, this site is the N=1 case. Same extractor, same fence-with-path-comment protocol (`# === FILE: path ===` as first comment line inside the fence), just always produces exactly one fenced block.

Consistent across all code-producing sites:
- Site #1 (`create.generate_content`) — N=1 today
- Site #12 (`project_ops.plan_setup`) — N>1
- Site #15 (`rewrite.generate_rewrite`) — N=1

No schema divergence.

### `related_files` + `architecture` → consolidated `dependencies`

**Settled.** Two current sections (`related_files` carrying `file_excerpts`, `architecture` carrying `architecture_spec`) consolidate under one `dependencies` section rendered by a single template with two sub-blocks.

**This is the authentic `dependencies` category** — symbol signatures and import conventions the output must honor. Unlike Sites #8 and #12 where content was stretched into dependencies because the fit was imperfect, Site #15's content is genuinely what the section type was designed for. The preservation of unrelated code, correct imports, and compatible interfaces is exactly "signatures the output must honor."

### `task` + `validation_errors` → consolidated `problem`

**Settled.** When the rewrite is triggered by validation errors, those errors frame *what problem* the rewrite is solving. They're part of the problem statement, not separate evidence or context. One template renders the task (required) followed by validation errors (conditional).

Same consolidation pattern as Sites #1 (data_contracts + file_excerpts), #8 (project_files + repo_map), #17 (turn_count + last_command).

### `target_entity` with dynamic title override

**Settled.** Uses the `target_entity` section type (renamed at Site #10 from `target_file`) with `title: "Current File: {input.target_file_path}"` — the title-override feature established at Site #10 enables per-invocation title composition.

### Temperature — `t*0.4`

**Settled.** Bumped from current `t*0.3`. Rationale under the Site #14 corrected calibration:

- `t*0.3` is the menu-selection regime (Sites #4, #5, #9, #17)
- `t*0.4` is the code-generation regime (Sites #1, #10a, #12)
- This site's work is code generation, not menu selection

Strong preservation constraint ("preserve all functionality not related to the modification") argues for restrictive temperature. `t*0.4` is the right balance — enough flexibility to synthesize a coherent modified file, restrictive enough to avoid the "creative rewrite that refactors unrelated code" failure mode the prompt explicitly fights against.

Not bumping to `t*0.6` (Site #14's regime) because this task is more constrained than synthesis-into-prose. Specific change, leave rest alone — restricted code-gen territory, not loosened default-adjacent territory.

### Drop format negations from prompt

**Settled.** Current prompt has multiple negations ("Do not include text outside the file block", "NOTHING before the file block or after it", ✅/❌ examples). All banner-handled under `=== CODE EDITOR ===`.

Preservation rules stay in instruction (content discipline, not format discipline):
- "Preserve all functionality not related to the modification"
- "Do NOT remove functions, classes, or methods that are not related to the fix"
- "The output file MUST be similar in size to the current file"

### Language tag — `"python"` (parked for multi-language future)

**Settled.** Hardcoded `python` language tag on the fence. Parked consideration: dynamic language selection when the agent expands beyond Python. Same language-awareness cross-cutting work as Site #10a, `build_repomap`, and the removed `__grep__` tool.

Not blocking — Python-only is the current scope.

### Unchanged

- Pre-compute formatter list (all four current formatters)
- Step-level routing via turn's default/no_answer
- `publishes: ["inference_response"]`
- Flow-graph position

---

## Templates to author at Step C

1. **`personas/code_author.yaml`** — shared with Sites #1, #10a. Check alignment during Step C authoring.
2. **`rewrite/task_with_validation_errors.yaml`** — merged problem template. Renders `## Task\n{flow_directive}`, conditionally appends `## Validation Errors to Fix\n{validation_errors}`.
3. **`rewrite/project_and_architecture.yaml`** — merged dependencies template. Two sub-blocks: `## Project Structure` (file_excerpts) + `## Architecture & Import Conventions` (architecture_spec). Includes the preservation reminder: "Preserve compatibility with these when rewriting."
4. **`rewrite/generate_rewrite_instruction.yaml`** — trimmed instruction with preservation rules only (no format discipline, handled by banner).

---

## Cross-site follow-ups

- **`personas/code_author` convergence point** — Sites #1, #10a, #15 all share this persona. Step C authoring should produce one canonical template they all reference.
- **Extractor convergence** — Sites #1, #12, #15 all use the same `# === FILE: path ===` extractor. Single implementation handles all three at Step C.
- **Language-awareness parked work** — `rewrite.generate_rewrite`, `patch.rewrite_symbol`, `create.generate_content`, `build_repomap`, and future grep replacement all need language-aware adaptations when the agent expands beyond Python. Deferred to a post-Step-C pass.
- **Authentic `dependencies` vs. stretched `dependencies`** — Site #15's `related_files` + `architecture` consolidation is the canonical use. Sites #8 and #12 stretch the category. Supports the view that those stretches may warrant a new section type (decision deferred until all Step B sites reviewed).
