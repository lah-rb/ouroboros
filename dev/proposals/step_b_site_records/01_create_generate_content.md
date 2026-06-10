# Site #1 — `create.generate_content` / `create.generate_tests`

**Status:** Approved for Step C migration.
**Response shape:** `code`
**Current file:** `flows/cue/create.cue` (lines 89-97)
**Empirical (892 run):** n=8, avg_in=1665 tokens, avg_out=489 tokens, max_out=755

---

## Turn definition

```cue
_generate_turn_shape: #Turn & {
    response_shape: "code"
    sections: [
        {type: "role",          template: "personas/code_author"},
        {type: "problem",       template: "create_file/task"},  // ## Task + flow_directive
        {type: "target_entity", ref: {$ref: "input.target_file_path"}, title: "Target file"},
        {type: "dependencies",  template: "create_file/dependencies"},  // data contracts + excerpts
        {type: "context_files", ref: {$ref: "context.architecture_spec"}},
        {type: "instruction",   template: string},  // varies by variant
        {type: "envelope"},
    ]
    response: {
        language: "python"
    }
    transitions: {
        default:   "write_files"
        no_answer: "failed"
    }
    config: {temperature: "t*0.4"}
}

generate_content: #StepDefinition & {
    action: "inference"
    description: "Generate file content"
    turn: _generate_turn_shape & {
        sections: [...] & [
            ..., // same as shape above except:
            {type: "instruction", template: "create_file/generate_content_instruction"},
            ...,
        ]
    }
    publishes: ["inference_response"]
}

generate_tests: #StepDefinition & {
    action: "inference"
    description: "Generate test file content"
    turn: _generate_turn_shape & {
        sections: [...] & [
            ..., // same as shape above except:
            {type: "instruction", template: "create_file/generate_tests_instruction"},
            ...,
        ]
    }
    publishes: ["inference_response"]
}
```

(The exact CUE ergonomics for "shared turn shape with one field override" will
be sorted during Step C implementation — the intent is DRY, the mechanism is
flexible.)

---

## Decisions landed

### Output protocol — migrate to fence-with-path-comment (option B)

**Settled:** move from the current custom frame to a markdown fence with path metadata as a comment:

````
```python
# === FILE: engine.py ===
def ...
```
````

**Rationale:**
- One `=== ... ===` vocabulary per turn. The banner at the top is the only triple-equals line; the model sees banner → prime → fenced code as a clean sequence.
- Markdown fences are the project's preferred envelope everywhere else.
- **Secondary benefit:** the fence-with-path-comment protocol acts as an auto-ground for `patch` and `rewrite` flows, which also produce code artifacts and will benefit from the same protocol. Adopting it here establishes the pattern.

**Extractor change required:** the existing `action_handler.write_files` parser reads `=== FILE: path ===` followed by a fenced block. Step C rewrites this to parse a fenced block whose first line is a `# === FILE: path ===` comment.

### `dependencies` section consolidation — APPROVED

The current `data_contracts` and `existing_files` prompt sections both map to schema `dependencies`. The schema allows each section type at most once. Consolidate into one `dependencies` template that renders data contracts first, then file excerpts, in consistent order.

Pre-compute formatters stay the same (`render_dependency_excerpts`, `render_data_contracts`); the output is composed by one template instead of two.

### `output_format` prompt section — removed at this site

The ✅/❌ examples currently in `output_format` move into SOUL.md as part of the banner/envelope primer. One-time processing cost at startup; not re-paid per turn. Per-site format guidance is then redundant.

### DRY — `generate_content` and `generate_tests` share one shape

Both steps declare the same turn shape, differing only in the `instruction` section's template reference. Step C implementation chooses the CUE idiom for sharing (`let` binding plus override, or embedded struct, etc.) — either way, the two steps do not duplicate the turn definition.

### Temperature — unchanged

`temperature: "t*0.4"`. Top of the project's recommended range for code generation (t = 0.2 to t = 0.4). Leaving as-is.

### Unchanged at this site

- `select_prompt` rule-resolver step stays as-is. Non-inference; schema doesn't touch it.
- `write_files` and `done` / `failed` terminal steps unchanged.
- Publishes `inference_response` unchanged.

---

## Templates to author at Step C

Three new prompt template partials (replacing the current single `create_file/generate_content.yaml`):

1. **`personas/code_author.yaml`** — `---ACT AS---` block for the code-authoring role. May be reused by `patch.rewrite_symbol` and `rewrite.generate_rewrite` — check alignment at those sites during their Step B analysis.
2. **`create_file/task.yaml`** — `## Task` header + `{input.flow_directive}`.
3. **`create_file/dependencies.yaml`** — merged data contracts + file excerpts rendering. Consumes the existing pre-compute outputs (`data_contract_block`, `file_excerpts`).
4. **`create_file/generate_content_instruction.yaml`** — the "what you need to do" for content generation.
5. **`create_file/generate_tests_instruction.yaml`** — the "what you need to do" for test generation.

---

## Cross-site follow-ups

- **`personas/code_author`** persona template — check alignment when we reach `patch.rewrite_symbol` (Site #11) and `rewrite.generate_rewrite` (Site #16).
- **Fence-with-path-comment output protocol** — should be adopted by `rewrite.generate_rewrite` as well, since it produces a full-file replacement. `patch.rewrite_symbol` produces a symbol body only, so the path comment wouldn't apply there — but fenced code with language tag is still the right shape.
- **Extractor module rewrite** — one change to `action_handler.write_files` (or equivalent) at Step C migration time, serving all three sites that produce code.

---

## Appendix — multi-file protocol clarification (from Site #12 analysis)

Site #12 (`project_ops.plan_setup`) uses the same protocol to produce **multiple files per call**. The schema's `code` shape turns out to be naturally multi-file capable: the fence-with-path-comment protocol scales from one fence to N fences without any schema change. Each fenced block declares its own file via `# === FILE: path ===` as its first comment line.

Site #1 is single-file *today* because `file_ops.create` produces one file per invocation by design. When the create flow is eventually extended to produce multi-file outputs, **no schema change is needed** — only the instruction template changes to ask for multiple files. The extractor (unified across Sites #1, #12, #16) already scans for all `# === FILE: path ===` markers regardless of fence count.

This reframes Site #1 as a specialization of a multi-file capable pattern rather than a distinct single-file pattern. N=1 is the current use case; the capability supports arbitrary N.
