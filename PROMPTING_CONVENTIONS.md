# Ouroboros — Runtime Prompt Conventions

Standards for the Jinja2 prompt templates in YAML flow files that are sent to the local Qwen model during execution. These conventions exist because the local model is more sensitive to prompt structure than frontier models — clear framing, explicit constraints, and concrete examples dramatically reduce extraction failures and off-task responses.

---

## 1. Prompt Structure: The Three-Section Pattern

Every inference prompt MUST follow this structure:

```
[ROLE + CONTEXT]     — Who you are, what system you're in, what constraints apply
[TASK + MATERIALS]   — What to do, with all relevant data inline
[OUTPUT FORMAT]      — Exactly what to return, with examples of right and wrong
```

**CRITICAL:** The output format section must appear last (closest to where generation begins) and must include at least one concrete example of the expected format.

### Role Section

Every prompt begins with a brief identity and constraint frame. This is not decorative — it prevents the model from adopting unhelpful personas or adding unwanted commentary.

```yaml
prompt: |
  You are a code generation module in an automated pipeline.
  Your output will be extracted by a parser and written directly to a file.
  Do NOT include explanatory prose, commentary, or markdown outside code markers.
```

The role section should be 1-3 sentences. It must convey:
- What the model is (a module in a pipeline, not a chatbot)
- What happens to its output (parsed automatically, not read by humans)
- The single most important constraint (usually: no prose outside the required format)

### Task Section

The task section provides all materials inline — file contents, project context, error output, previous attempts. Use Jinja2 conditionals to include only what's available.

**IMPORTANT:** Place the most critical information (the target file, the specific task) before supplementary context (related files, notes, research). The model weighs content near the beginning and end of prompts more heavily than content in the middle.

### Output Format Section

The output format section is the highest-leverage part of the prompt. It MUST include:
1. An explicit statement of what format to return
2. At least one ✅ CORRECT example
3. At least one ❌ WRONG example showing the most common failure mode
4. A final reinforcement line restating the core constraint

---

## 2. Structured Output (JSON) Prompts

When the model must return parseable JSON (plans, file selections, validation strategies), use this pattern:

```yaml
prompt: |
  ... [role + task sections] ...

  Return ONLY a JSON array in this exact format — no markdown, no explanation, no wrapping:

  ✅ CORRECT output (raw JSON, nothing else):
  [
    {"field": "value", "other": "value"}
  ]

  ❌ WRONG — do not wrap in markdown code blocks:
  ```json
  [{"field": "value"}]
  ```

  ❌ WRONG — do not add explanation before or after:
  Here is the plan:
  [{"field": "value"}]

  Return ONLY the raw JSON.
```

### Key Rules for JSON Prompts

- **ALWAYS show the exact JSON schema** with field names, types, and a complete example
- **ALWAYS show the ❌ markdown-wrapping failure** — this is the #1 extraction failure mode
- **ALWAYS end with a one-line reinforcement** ("Return ONLY the raw JSON")
- **NEVER use `true/false` as placeholder values** in examples — use actual plausible values so the model distinguishes the schema from a boolean instruction

---

## 3. Code Generation Prompts

When the model must return file content for extraction and writing to disk:

```yaml
prompt: |
  ... [role + task sections] ...

  Write the complete file content for {{ input.target_file_path }}.

  Respond with ONLY the code inside triple-backtick markers.
  Do NOT include any text before or after the code block.

  ✅ CORRECT format:
  ```
  # your complete file content here
  def example():
      pass
  ```

  ❌ WRONG — no explanation outside the markers:
  Here is the implementation:
  ```
  def example():
      pass
  ```
  I added the function because...

  Return ONLY the code block, nothing else.
```

### Key Rules for Code Prompts

- **ALWAYS request the COMPLETE file** — not a diff, not a partial update, the entire file content
- **Use plain triple-backtick markers** (not ` ```python `) — the extraction regex is simpler and more reliable with plain markers
- **NEVER ask for multiple code blocks** — the extractor takes the first one. One file per inference call.
- **End with format reinforcement** — "Return ONLY the code block, nothing else"

---

## 4. Reflection / Free-Text Prompts

For prompts where the model should produce unstructured observations (capture_learnings, diagnostics):

```yaml
prompt: |
  ... [role + task sections] ...

  Be concise — 2-4 sentences maximum.
  Focus on facts that would help a future task working on this code.
  Do NOT restate the task description. Do NOT use bullet points or headers.
  Write plain prose.
```

These prompts are lower-stakes (output isn't parsed by regex) but still benefit from length and format constraints to prevent verbose responses that waste context budget.

---

## 5. LLM Menu Resolver Prompts

The `resolver.prompt` field for `type: llm_menu` provides brief context before the menu options. Keep it to one sentence that frames the decision:

```yaml
resolver:
  type: llm_menu
  prompt: "Based on your confidence in the change plan, what should happen next?"
  options:
    execute_change:
      description: "Confidence is high — proceed with implementing the change"
    abandon:
      description: "The approach is flawed — return to mission_control"
```

The resolver system (`agent/resolvers/llm_menu.py`) automatically appends "Choose exactly ONE of the following options by responding with just the option name" and lists the options. Do NOT duplicate this instruction in the resolver prompt.

---

## 6. Jinja2 Template Patterns

### Conditional Context Blocks

Always guard optional context with `{% if %}` to avoid rendering empty sections:

```yaml
prompt: |
  {% if context.context_bundle and context.context_bundle.files %}
  Existing project files for reference:
  {% for file in context.context_bundle.files %}
  === {{ file.path }} ===
  {{ file.content }}
  {% endfor %}
  {% endif %}
```

### Frustration-Aware Prompts

When frustration history is available, include it with explicit framing that steers the model away from repeating the same approach:

```yaml
  {% if input.frustration_history %}
  ⚠️ Previous attempts at this task FAILED:
  {{ input.frustration_history }}
  You MUST take a different approach than what was tried before.
  {% endif %}
```

### Default Values

Use Jinja2 `| default()` for optional values that need sensible fallbacks:

```yaml
  Budget: at most {{ input.context_budget | default(8) }} files.
```

---

## 7. Temperature Guidelines

| Prompt Type | Temperature | Rationale |
|-------------|-------------|-----------|
| Structured output (JSON) | `0.0` – `0.1` | Deterministic format adherence |
| Code generation | `t*0.8` (relative) | Slight creativity within patterns |
| Planning / analysis | `0.3` – `0.4` | Balanced exploration |
| Reflection / learning | `0.2` | Mild variety in observations |
| Retry after failure | Lower than original | Tighten focus after drift |
| Frustration retry | Perturbed (see §7 in REFINEMENT.md) | Explore different sampling trajectories |

Use relative temperature (`t*0.5`, `t*1.2`) for cross-model portability. Use absolute values only for format-critical outputs (JSON extraction, menu choices).

---

## 8. Strict Block Messages for Code Output

In addition to ❌/✅ teaching patterns, code generation prompts MUST end with strict block directives. These are proven effective at curtailing fenced code block wrapping even on stubborn models:

```yaml
prompt: |
  ... [role + task + format examples] ...

  ---DO NOT GENERATE EXTRA CHARACTERS---
  ---DO NOT USE FENCED CODE BLOCKS (```)---
  ---END RESPONSE IMMEDIATELY AFTER CODE---
  ---ONLY CODE EXECUTABLE SYNTAX BELOW---
```

**IMPORTANT:** These directives complement the ❌/✅ examples — do NOT replace the examples. Repetition across different framing styles (examples, imperatives, block directives) is intentional and significantly improves compliance. Place block directives as the absolute last content before the `config:` key.

---

## 9. Config Values Are Static — No Jinja2 Templates

The `config:` block in step definitions is **NOT template-rendered**. Values are passed directly to the inference engine.

```yaml
# ✅ CORRECT — static values only
config:
  temperature: 0.7
  max_tokens: 4096

# ✅ CORRECT — relative temperature multiplier (t* prefix)
config:
  temperature: "t*0.8"

# ❌ WRONG — Jinja2 templates are NOT rendered in config
config:
  temperature: "t*{{ input.temperature_multiplier | default(1.0) }}"
```

If you need dynamic temperature (e.g. frustration-based perturbation), set it programmatically in the dispatch action (`action_configure_task_dispatch`) and pass it as a flow input. The `t*` multiplier format is handled by `resolve_temperature()` in `agent/effects/inference.py`.

---

## 10. Defensive JSON Parsing — `strip_markdown_wrapper`

All JSON extraction functions MUST call `strip_markdown_wrapper()` before regex extraction. Despite explicit instructions, many models wrap JSON/code in ` ```json ` blocks.

```python
from agent.actions.refinement_actions import strip_markdown_wrapper

response = strip_markdown_wrapper(response)
json_match = re.search(r"\[[\s\S]*\]", response)  # greedy
```

**CRITICAL:** JSON array regex MUST be **greedy** (`[\s\S]*`) not non-greedy (`[\s\S]*?`). Non-greedy matches inner arrays (like `depends_on: []`) instead of the full outer array, causing silent extraction failures.

---

## 11. Retry-with-Limit via `meta.step_visits`

The runtime tracks how many times each step has been visited within a single flow execution. Use `meta.attempt` (= visit count for the current step) or `meta.step_visits` (dict of all step visit counts) in resolver conditions:

```yaml
resolver:
  type: rule
  rules:
    - condition: "result.plan_created == true and result.task_count >= 2"
      transition: complete
    - condition: "meta.attempt < 3"
      transition: retry_plan
    - condition: "true"
      transition: failed
```

`meta.attempt` is the visit count for the current step (starts at 1, increments each revisit). `meta.step_visits` is the full dict if you need cross-step awareness.

---

## 12. Research Availability — Frustration-Graduated

Research via `research_context` sub-flow is always available, with urgency graduated by frustration level:

| Frustration | Research Behavior |
|-------------|------------------|
| 0-2 (low) | **Optional** — LLM decides whether to research |
| 3-5 (medium) | **Recommended** — prompt emphasizes research is strongly advised |
| 6+ (high) | **Mandatory** — rule-based, always researches before proceeding |

This is implemented in `prepare_context.yaml` via the `check_research_needed` → `decide_research_optional` / `decide_research_recommended` / `research` branching.

---

## 13. Prompt Maintenance Checklist

When adding or modifying a prompt in a YAML flow file, verify:

- [ ] **Role section present** — 1-3 sentences establishing identity and constraints
- [ ] **Output format section at the end** — with at least one ✅/❌ example pair
- [ ] **Critical information at edges** — target file and task near the top; format spec at the bottom
- [ ] **Optional context guarded** — all `{% if %}` blocks for optional Jinja2 variables
- [ ] **Single output per prompt** — one file, one JSON object, or one reflection. Never multiple.
- [ ] **Final reinforcement line** — "Return ONLY..." as the last line
- [ ] **Temperature set appropriately** — per the guidelines table above
- [ ] **No duplicate instructions** — don't restate what the resolver or runtime already handles
