# Ouroboros — Runtime Prompt Conventions

Standards for the section-based YAML prompt templates referenced by CUE flow definitions. These conventions exist because the local model is more sensitive to prompt structure than frontier models — clear framing, explicit constraints, and concrete examples dramatically reduce extraction failures and off-task responses.

---

## 1. Prompt Architecture

Prompts are **section-based YAML files** in `prompts/<flow>/<step>.yaml`, referenced by CUE flow definitions via `prompt_template.template`. Complex data formatting is handled by **pre-compute formatters** (Python functions) that run before template rendering — the template itself only does simple variable substitution.

```yaml
# prompts/create_file/generate_content.yaml
id: create_file/generate_content
description: "Generate complete file content for a new source file"

sections:
  - id: system_role
    content: |
      You are a code generation module in an automated pipeline.
      ...

  - id: task
    content: |
      ## Task
      {input.task_description}

  - id: existing_files
    when: context.file_excerpts        # conditional section
    content: |
      ## Existing Files
      {context.file_excerpts}

  - id: output_format
    content: |
      ... format spec with ✅/❌ examples ...
```

Variable syntax: `{input.X}`, `{context.X}`, `{meta.X}`. Resolved via `string.format_map()` — no expressions, no filters, no method calls. Missing values render as empty string.

---

## 2. Prompt Structure: The Three-Section Pattern

Every inference prompt MUST follow this structure:

```
[ROLE + CONTEXT]     — Who you are, what system you're in, what constraints apply
[TASK + MATERIALS]   — What to do, with all relevant data inline
[OUTPUT FORMAT]      — Exactly what to return, with examples of right and wrong
```

**CRITICAL:** The output format section must appear last (closest to where generation begins) and must include at least one concrete example of the expected format.

### Role Section

Every prompt begins with a brief identity and constraint frame. 1-3 sentences. It must convey:
- What the model is (a module in a pipeline, not a chatbot)
- What happens to its output (parsed automatically, not read by humans)
- The single most important constraint (usually: no prose outside the required format)

```yaml
  - id: system_role
    content: |
      You are a code generation module in an automated pipeline.
      Your output will be extracted by a parser and written directly to a file.
      Do NOT include explanatory prose or commentary outside code blocks.
```

### Task Section

The task section provides all materials inline — file contents, project context, error output. Use `when:` conditionals on sections to include only what's available.

**IMPORTANT:** Place the most critical information (the target file, the specific task) before supplementary context (related files, notes). The model weighs content near the beginning and end of prompts more heavily.

### Output Format Section

The output format section is the highest-leverage part of the prompt. It MUST include:
1. An explicit statement of what format to return
2. At least one ✅ CORRECT example
3. At least one ❌ WRONG example showing the most common failure mode
4. A final reinforcement line restating the core constraint

---

## 3. Structured Output (JSON) Prompts

When the model must return parseable JSON (plans, file selections, validation strategies):

```yaml
  - id: output_format
    content: |
      Return a JSON object in this exact format:

      ✅ CORRECT:
      ```json
      [{"field": "value", "other": "value"}]
      ```

      ❌ WRONG — do not add explanation before or after:
      Here is the plan:
      ```json
      [{"field": "value"}]
      ```

      Return ONLY the JSON.
```

### Key Rules for JSON Prompts

- **ALWAYS show the exact JSON schema** with field names, types, and a complete example
- **Use fenced code blocks** (```` ```json ````) in the ✅ example — the runtime robustly extracts content from markdown fences via `strip_markdown_wrapper()` and `markdown-it-py`
- **Show the ❌ explanation-wrapping failure** — prose before/after JSON is the #1 extraction failure mode
- **ALWAYS end with a one-line reinforcement** ("Return ONLY the JSON")
- **NEVER use `true/false` as placeholder values** in examples — use actual plausible values

---

## 4. Code Generation Prompts

When the model must return file content for extraction and writing to disk:

```yaml
  - id: output_format
    content: |
      Write the complete file content for {input.target_file_path}.

      ✅ CORRECT format:
      === FILE: {input.target_file_path} ===
      ```python
      # your complete file content here
      def example():
          pass
      ```

      ❌ WRONG — no explanation outside the file block:
      Here is the implementation:
      === FILE: main.py ===
      ```python
      def example():
          pass
      ```
      I added the function because...

      Return ONLY the file block, nothing else.
```

### Key Rules for Code Prompts

- **ALWAYS request the COMPLETE file** — not a diff, not a partial update
- **Use `=== FILE: path ===` markers** followed by fenced code blocks — this is the format `parse_file_blocks()` extracts
- **Fenced code blocks are fine** — ```` ```python ````, ```` ```yaml ````, or plain ```` ``` ```` are all robustly handled by the extraction pipeline (`markdown_fence.py`)
- **End with format reinforcement** — "Return ONLY the file block, nothing else"

---

## 5. Reflection / Free-Text Prompts

For prompts where the model should produce unstructured observations (capture_learnings, diagnostics, retrospective):

```yaml
  - id: output_format
    content: |
      Write a concise observation in 2-4 sentences of plain prose.
      Focus on facts that would help a future task working on this code.
      Do NOT use bullet points or headers. Do NOT restate the task description.

      ✅ CORRECT — plain prose, specific:
      The Todo model needed explicit __eq__ to work in sets. The existing tests import from todo.models, so new files must follow that path.

      ❌ WRONG — generic advice with formatting:
      ## Learnings
      - Always check imports
      - Consider edge cases

      Write plain prose only.
```

These prompts are lower-stakes (output isn't parsed by regex) but still benefit from length and format constraints to prevent verbose responses that waste context budget.

---

## 6. LLM Menu Resolver Prompts

The `resolver.prompt` field for `type: llm_menu` provides brief context before the menu options. Keep it to one sentence that frames the decision:

```cue
resolver: {
    type: "llm_menu"
    prompt: "Based on your confidence in the change plan, what should happen next?"
    options: {
        execute_change: {
            description: "Confidence is high — proceed with implementing the change"
        }
        abandon: {
            description: "The approach is flawed — return to mission_control"
        }
    }
}
```

The resolver system (`agent/resolvers/llm_menu.py`) automatically appends option listing and selection instructions. Do NOT duplicate this in the resolver prompt.

---

## 7. Section-Based Template Patterns

### Conditional Sections

Use `when:` to include sections only when data is available:

```yaml
  - id: architecture
    when: input.relevant_notes
    content: |
      ## Architecture & Import Conventions
      {input.relevant_notes}
```

### Pre-Computed Context

Complex data formatting is handled by registered Python formatters declared in the CUE step definition's `pre_compute` block. The formatter runs before template rendering and injects a string value into the context namespace:

```cue
pre_compute: [{
    formatter:  "format_file_excerpts"
    output_key: "file_excerpts"
    params: {
        source: {$ref: "context.context_bundle.files"}
        exclude: {$ref: "input.target_file_path"}
        max_chars: 1500
    }
}]
```

The template then references the pre-computed key as `{context.file_excerpts}`.

### Variable References

All references use simple dotted paths:
- `{input.X}` — flow input values
- `{context.X}` — context accumulator values (including pre-computed)
- `{meta.X}` — flow execution metadata

No expressions, no filters, no method calls. If a value is None or missing, it renders as empty string.

---

## 8. Temperature Guidelines

| Prompt Type | Temperature | Rationale |
|-------------|-------------|-----------|
| Structured output (JSON) | `0.0` – `0.1` | Deterministic format adherence |
| Code generation | `t*0.8` (relative) | Slight creativity within patterns |
| Planning / analysis | `0.3` – `0.4` | Balanced exploration |
| Reflection / learning | `0.2` – `0.4` | Mild variety in observations |
| Retry after failure | Lower than original | Tighten focus after drift |
| Terminal command planning | `t*0.6` | Some exploration for commands |

Use relative temperature (`t*0.5`, `t*1.2`) for cross-model portability. Use absolute values only for format-critical outputs (JSON extraction, menu choices).

---

## 9. Extraction Pipeline

The runtime handles LLM response extraction robustly. You do NOT need to tell models to avoid fences — fences are the expected format.

### JSON Extraction
All JSON extraction calls `strip_markdown_wrapper()` which handles ```` ```json ````, ```` ```python ````, and plain ```` ``` ```` wrappers using `markdown-it-py` (CommonMark-compliant) with regex fallback.

### Code Extraction
`extract_code_from_response()` tries multiple strategies:
1. Single fenced block → extract content
2. Multiple fenced blocks → use the largest
3. Remove obvious non-code lines (explanations, commentary)
4. Fall back to raw response

### Multi-File Extraction
`parse_file_blocks()` splits on `=== FILE: path ===` markers, then extracts fenced content from each section.

**Key:** JSON regex MUST be **greedy** (`[\s\S]*`) not non-greedy (`[\s\S]*?`). Non-greedy matches inner arrays instead of the full outer array.

---

## 10. Config Values Are Static

The `config:` block in step definitions is **NOT template-rendered**. Values are passed directly to the inference engine.

```cue
config: temperature: 0.7           // absolute
config: temperature: "t*0.8"       // relative multiplier
```

The `t*` multiplier format is handled by `resolve_temperature()` in `agent/effects/inference.py`.

---

## 11. Retry-with-Limit via `meta.attempt`

The runtime tracks step visit counts. Use `meta.attempt` in resolver conditions:

```cue
resolver: {
    type: "rule"
    rules: [
        {condition: "result.plan_created == true", transition: "complete"},
        {condition: "meta.attempt < 3", transition: "retry_plan"},
        {condition: "true", transition: "failed"},
    ]
}
```

`meta.attempt` starts at 1, increments each revisit.

---

## 12. Prompt Maintenance Checklist

When adding or modifying a prompt template, verify:

- [ ] **Role section present** — 1-3 sentences establishing identity and constraints
- [ ] **Output format section at the end** — with at least one ✅/❌ example pair
- [ ] **Critical information at edges** — target file and task near the top; format spec at the bottom
- [ ] **Optional sections use `when:`** — for conditional context inclusion
- [ ] **Single output per prompt** — one file, one JSON object, or one reflection
- [ ] **Final reinforcement line** — "Return ONLY..." as the last line
- [ ] **Temperature set appropriately** — per the guidelines table above
- [ ] **Pre-computed keys documented** — comment header listing what formatters provide
