# Site #12 — `project_ops.plan_setup`

**Status:** Approved for Step C migration.
**Response shape:** `code` (multi-fence, one-to-many files per invocation)
**Current file:** `flows/cue/project_ops.cue` (lines 62-86)
**Current prompt:** `prompts/project_ops/plan.yaml`
**Empirical (892 run):** n=1, in=403 tokens, out=305 tokens. One-shot per goal; succeeded.

---

## Turn definition

```cue
plan_setup: #StepDefinition & {
    action: "inference"
    description: "Determine what setup actions are needed"
    context: optional: ["project_manifest", "repo_map_formatted"]
    turn: #Turn & {
        response_shape: "code"
        sections: [
            {type: "role",          template: "personas/project_ops_setup"},
            {type: "problem",       template: "project_ops/task_with_focus"},  // merged task + focus
            {type: "context_files", ref: {$ref: "context.project_file_list"}},
            {type: "dependencies",  ref: {$ref: "context.setup_brief"}},  // category stretch, see Cross-site note
            {type: "instruction",   template: "project_ops/plan_setup_instruction"},
            {type: "envelope"},
        ]
        response: {
            // Multi-file output: the instruction specifies each file's
            // language in its fence tag. The schema's `language` field
            // represents the expected output, but for multi-file sites
            // the instruction's per-file fence tags are authoritative.
            language: ""
        }
        transitions: {
            default:   "write_files"
            no_answer: "build_report_failure"
        }
        config: {temperature: "t*0.4"}  // bumped from t*0.3 — code-generation regime
    }
    pre_compute: [
        // Unchanged from current flow
        {formatter: "render_project_setup_context", output_key: "setup_brief"
            params: {source: {$ref: "input.project_setup_context"}}},
        {formatter: "format_project_file_list", output_key: "project_file_list"
            params: {source: {$ref: "context.project_manifest"}}},
    ]
    publishes: ["inference_response"]
}
```

---

## Decisions landed

### Response shape — `code` with multi-fence protocol

**Settled.** The schema's `code` shape uses the same fence-with-path-comment protocol established at Site #1, extended for the multi-file case this site requires. Each file emerges as a separate fenced block whose first comment line is `# === FILE: path ===`:

````
```toml
# === FILE: pyproject.toml ===
[project]
...
```

```markdown
# === FILE: README.md ===
# Project Name
...
```
````

**Framing clarification per your observation:** Site #1 is single-file *today* because file_ops.create produces one file per invocation. The protocol itself is multi-file capable. N=1 is a degenerate case of N=N, not a distinct shape. Site #12 exercises the full capability. This means:

- **No new schema shape** — `code` covers both single-file and multi-file outputs.
- **Extractor is unified** — scans for `# === FILE: path ===` markers anywhere in the response, doesn't care about fence count.
- **Future-proof** — when file_ops.create gains multi-file capability, no schema migration needed. Just an instruction template revision.

### `language: ""` as multi-fence sentinel

**Settled.** For multi-file sites where each fence carries its own language tag (`toml`, `markdown`, `python`, `gitignore`, etc.), the schema's top-level `language` field is an empty string. The instruction template tells the model which language to use per file; the envelope renders `=== CODE EDITOR ===` without committing to a specific fence tag.

Schema commentary at Step C should document this: `language: ""` means "multi-language multi-fence, instruction is authoritative on per-fence language tags."

### Fence-with-path-comment protocol adopted (matches Site #1)

**Settled.** The current `=== FILE: path ===` top-level marker (outside any fence) migrates to `# === FILE: path ===` as the first comment line *inside* each fence. Same decision as Site #1. Consistent protocol across all code-producing sites.

### `task` + `focus` merge under `problem`

**Settled.** Current prompt has `task` (mandatory `## Task` + flow_directive) and `focus` (conditional `Focus: ...` line). Both are the problem statement; the conditional `focus` is just an optional qualifier. One template renders both — Task first, Focus appended if present.

### `architecture` section → `dependencies` (category stretch, third instance)

**Settled with caveat — parked for consolidation review.** The current `architecture` section renders `setup_brief` (formatted project setup context: language, dependency file names, etc.). Same category-stretch flagged at Site #8 (`interact.plan_interaction`'s `interaction_brief`).

Both are "reference material the output must be grounded in" rather than "signatures the output must honor." The fit under `dependencies` is imperfect but usable. Third occurrence may warrant a new section type, but naming it requires reviewing what functionality these stretches share — which we'll do after Site #13's analysis.

### Temperature — `t*0.4`

**Settled.** Bumped from current `t*0.3`. Code-generation regime (Sites #1, #10a, #16 cluster). Task is generating multiple config files with various languages — needs the same synthesis flexibility as other code sites.

### Drop negations from prompt (banner-handled)

**Settled.** Current prompt has multiple negations ("do not include text outside the file blocks", "no explanation outside the file blocks", "NOTHING outside the file blocks") and ✅/❌ format examples. All handled by the `=== CODE EDITOR ===` banner + SOUL primer. Same pattern as Sites #1, #2, #7, #8.

### Extractor reuse

**Settled.** Same extractor as Sites #1 and #16 (both migrating to the fence-with-path-comment protocol at Step C). Single implementation handles one-fence or many-fence cases by scanning for `# === FILE: path ===` markers.

### Unchanged

- Pre-compute formatters (`render_project_setup_context`, `format_project_file_list`) — unchanged
- Step-level rule resolver for write_files / report_failure branching — turn handles inference outcome; step handles flow routing via `default` / `no_answer` transitions directly
- `publishes: ["inference_response"]` — unchanged

---

## Templates to author at Step C

1. **`personas/project_ops_setup.yaml`** — `---ACT AS---` block. "Project setup engineer producing config files and scaffolding."
2. **`project_ops/task_with_focus.yaml`** — merged task + focus. Renders `## Task\n{flow_directive}`, appends `Focus: {setup_focus}` conditionally.
3. **`project_ops/plan_setup_instruction.yaml`** — the "Generate ALL configuration and setup files" instruction block. Includes the list of expected file types (pyproject.toml, __init__.py, README.md, .gitignore, etc.) and the "DO NOT create source code files" guardrail.

---

## Cross-site follow-ups

- **Category-stretch of `dependencies`** — third instance (Sites #8, #12, and likely #13). After Site #13 we'll review the shared functionality across these stretches to decide whether to promote a new `notes` or `reference` section type. Naming should reflect what these sites' data actually *is*, not just that they're "not quite dependencies."
- **Multi-file protocol now canonical** — Site #1's single-file case and Site #12's multi-file case both use the same underlying protocol. Step C extractor is one implementation; prompts differ by what they ask the model to produce. Worth noting explicitly in the Site #1 record via appendix.
- **`language: ""` for mixed-language output** — first site to formally use this. Document as supported pattern in schema commentary.
