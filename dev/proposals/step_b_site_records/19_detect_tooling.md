# Site #19 — `set_env.detect_tooling`

**Status:** Approved for Step C migration.
**Response shape:** `json_document` with `validation_env_config` schema
**Current file:** `flows/cue/set_env.cue` (lines 44-66)
**Current prompt:** `prompts/set_env/detect_tooling.yaml`
**Empirical (892 run):** n=3 inferences (1 empty from the LLMVP session-cap bug now fixed; 2 successful). Responses produced clean JSON.

---

## Turn definition

```cue
detect_tooling: #StepDefinition & {
    action: "inference"
    description: "Infer validation commands for this project's languages"
    context: required: ["project_manifest"]
    turn: #Turn & {
        response_shape: "json_document"
        sections: [
            {type: "role",        template: "personas/env_detector"},
            {type: "evidence",    template: "set_env/project_scan"},  // working_directory + project_file_list
            {type: "problem",     ref: {$ref: "input.target_file_path"}},  // conditional
            {type: "instruction", template: "set_env/detect_tooling_rules"},
            {type: "envelope"},
        ]
        response: {
            schema_id: "validation_env_config"
        }
        transitions: {
            default:   "persist_env"
            no_answer: "failed"
        }
        config: {temperature: "t*0.0"}  // deterministic lookup — see Decisions
        retries: 3
    }
    pre_compute: [
        {formatter: "format_project_file_list", output_key: "project_file_list"
            params: {source: {$ref: "context.project_manifest"}}},
    ]
    publishes: ["inference_response"]
}
```

---

## Decisions landed

### Response shape — `json_document` with registered schema

**Settled.** Output is a fenced JSON object with a hybrid root:
- Optional top-level `interactive_prompt: string`
- Dynamic keys for each detected file extension (`py`, `js`, `rs`, etc.)
- Each extension maps to a command-config object with `syntax` required, other commands optional

**Schema registry — `validation_env_config`:**

```
validation_env_config:
  interactive_prompt?: string
  <extension>:  # dynamic keys for present-in-project languages
    install_command?: list<string>
    formatter?:       list<string>
    syntax:           list<string>   # required
    import?:          list<string>
    cross_import?:    list<string>
    lint?:            list<string>
```

Fourth registry entry after `architecture_plan`, `evaluation`, and `research_queries`. The dynamic-key pattern is unusual but legitimate; schema validators handle it via pattern properties.

### Temperature — `t*0.0` (keep current)

**Settled.** The only site running at literal zero temperature. Resolves to `model_default * 0.0 = 0.0` — pure argmax, fully deterministic.

Task is explicitly deterministic lookup: same project → same `env.json`. Creative synthesis is not wanted; the model either knows standard validation commands or doesn't. Literal zero is the honest signal.

**Retry-variance trade-off acknowledged:** `retries: 3` can't produce different output at `t*0.0` because sampling is deterministic. If the first inference produces malformed JSON, retries reproduce the same error and the step terminates via `no_answer → failed`. That's acceptable behavior — the determinism guarantee is worth more than the retry recovery path for this specific task.

**Empirical evidence:** this setting has never failed in documented runs (per confirmation). The rate of `no_answer` termination is effectively zero.

### Calibration table addition

`t*0.0` — full argmax / deterministic lookup. First site outside the `t*0.1`–`t*1.0` range. Reserved for genuine lookup-like tasks where same-input-same-output is a feature.

### Section mapping

**Settled.** Standard cleanup:
- `context` (working_directory + project_file_list) consolidates into one `evidence` section via template — same pattern as Sites #1, #2, #8, #17
- `task` (conditional target_file_path) becomes `problem` (conditional)
- `output_format`'s ✅/❌ examples absorb into envelope + SOUL primer
- "Return ONLY the fenced JSON" absorbs into envelope
- Content rules (6 command-type definitions, placeholder semantics, extension-presence requirement) stay in instruction

### `no_answer → failed`

**Settled.** Retry exhaustion terminates as failed. `persist_env` downstream cannot proceed without inference_response; there's no meaningful fallback for this step.

### Session — not session-based

**Settled.** One-shot turn from mission-level dispatch. Full section rendering, no short-forming.

### Unchanged

- Pre-compute formatter (`format_project_file_list`)
- Context contract (`project_manifest` required)
- Downstream `persist_env` step
- Flow-graph position

---

## Templates to author at Step C

1. **`personas/env_detector.yaml`** — `---ACT AS---` block. "Project environment detection module; output is parsed by a JSON extractor and persisted as the project's validation config."
2. **`set_env/project_scan.yaml`** — evidence template rendering working directory + project file list.
3. **`set_env/detect_tooling_rules.yaml`** — trimmed instruction. Contains:
   - 6 command-type definitions (install_command, formatter, syntax, import, cross_import, lint)
   - Placeholder semantics (`{file}`, `{module}`)
   - `interactive_prompt` detection guidance (look for `input("> ")`, `input(">>> ")` patterns)
   - `## Rules` heading (replacing "Rules:" plain prefix, per the all-caps discipline from Site #18) — only-include-present-extensions, syntax-is-required, placeholder usage

## Schema registry entry

`validation_env_config` added to the registry at Step C. Hybrid shape with dynamic keys — document the pattern in schema registry commentary.

---

## Cross-site follow-ups

- **Fourth schema registry entry** — registry now has `architecture_plan`, `evaluation`, `research_queries`, `validation_env_config`. Step C implementation defines the registry loader and schema validation.
- **`t*0.0` as a legitimate calibration value** — first site using it. Document in calibration notes as reserved for deterministic lookup tasks (not a general option).
- **Dynamic-key JSON shapes in the registry** — `validation_env_config` uses pattern properties. Schema validator at Step C must handle this; most JSON Schema validators support it via `patternProperties`.
