# Ouroboros — Runtime Prompt Conventions

Standards for the section-based YAML prompt templates referenced by CUE flow definitions. These conventions exist because the local model is more sensitive to prompt structure than frontier models — clear framing, explicit constraints, and concrete examples dramatically reduce extraction failures and off-task responses.

---

## 1. Prompt Architecture

### Context Layers

Every inference call in Ouroboros is composed from three layers:

**Layer 1 — Soul (static, always present).** The agent's persistent identity, defined in `SOUL.md`. Pre-tokenized into a binary buffer and memory-mapped by LLMVP at server startup. Prepended to every inference call at zero marginal cost. Contains behavioral philosophy, correction discipline, coding principles, and universal output conventions. Changes rarely — only when the agent's character or fundamental operating principles change.

**Layer 2 — Step prompt (dynamic, per-inference).** Section-based YAML templates in `prompts/<flow>/<step>.yaml`, composed at inference time by the runtime. Contains the role framing, task materials, project context, and output format specification for the current step. Changes whenever flow logic or prompt quality is iterated.

**Layer 3 — Pre-computed context (dynamic, per-inference).** Python formatters that run before template rendering, injecting computed strings into the context namespace. Handles complex data formatting (goal listings, plan summaries, repo maps, file excerpts) that would be unwieldy as template logic. Changes when the data model or formatting needs change.

The soul establishes *who the agent is*. The step prompt establishes *what to do right now*. Pre-computed context provides *the materials to do it with*. When writing or modifying prompts, keep this separation in mind — behavioral principles belong in the soul, task-specific format instructions belong in the step template, and data formatting belongs in pre-compute formatters.

### Template Format

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

The output format section is the highest-leverage part of the prompt. For prompts whose output is machine-parsed (JSON, file blocks, code extraction), it MUST include:
1. An explicit statement of what format to return
2. At least one ✅ CORRECT example
3. At least one ❌ WRONG example showing the most common failure mode
4. A final reinforcement line restating the core constraint

For free-text prompts (analysis, reflections, directives) where output is not machine-parsed, ✅/❌ examples are optional. Over-constraining format for reasoning and analysis prompts can reduce output quality — the model focuses on matching the example rather than thinking through the problem. Use format guidance (length, structure, focus) without rigid examples unless you're seeing a specific failure mode you need to correct.

---

## 3. Structured Output (JSON) Prompts

When the model must return parseable JSON (plans, file selections, validation strategies):

```yaml
  - id: output_format
    content: |
      Return a JSON object inside a fenced code block:

      ✅ CORRECT:
      ```json
      [{"field": "value", "other": "value"}]
      ```

      ❌ WRONG — do not add explanation before or after:
      Here is the plan:
      ```json
      [{"field": "value"}]
      ```

      Return ONLY the fenced JSON.
```

### Key Rules for JSON Prompts

- **ALWAYS show the exact JSON schema** with field names, types, and a complete example
- **ALWAYS use fenced code blocks** (```` ```json ````) — fenced JSON is the standard output protocol. The runtime extracts content from markdown fences via `strip_markdown_wrapper()` and `markdown-it-py`. This aligns JSON output with the same fencing convention used for code output, and matches the model's natural behavior.
- **Show the ❌ explanation-wrapping failure** — prose before/after the fenced JSON is the #1 extraction failure mode
- **ALWAYS end with a one-line reinforcement** ("Return ONLY the fenced JSON")
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

For prompts where the model should produce unstructured observations (capture_learnings, diagnostics, retrospective, director analysis):

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

These prompts are lower-stakes (output isn't parsed by regex) and ✅/❌ examples are optional. They're helpful when the model produces a specific undesirable pattern (bullet lists, headers, verbose restatements) but should be omitted when the prompt is open-ended analysis or reasoning. For director reasoning, diagnostic analysis, and session summaries, length and focus guidance is sufficient without rigid format examples — constraining the shape of analysis constrains the analysis itself.

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

## 7. Role / Persona Crafting Prompts

When a prompt must produce a *role description* for another model to inhabit (e.g., `interact/plan` crafting an `execution_persona` for `run_session`), the output quality depends on techniques borrowed from the roleplay/character-card community. These techniques are empirically validated on local models in the 7B-120B range — the same class Ouroboros targets via LLMVP.

These same principles inform the agent's own persistent identity in `SOUL.md` — author framing, positive instructions, concrete behavioral grounding. The key difference: the soul is model-agnostic (it goes through different chat template wrappers per model) and persistent (pre-tokenized into the static knowledge buffer), while per-session personas are ephemeral and can be model-specific.

### Design Principles

**Author framing over character possession.** A model told "you ARE a beta tester" produces flat, mechanical responses. A model told "you are acting as a meticulous beta tester who explores corners" produces more dynamic, exploratory behavior. The framing "acting as" or "giving voice to" preserves the model's flexibility to adapt to unexpected situations.

**Show, don't tell — include a concrete first action.** The model mimics the style and specificity of what it sees. A persona that says "Start by running `uv run python main.py`, then type 'look' to see your surroundings" will produce a model that types those commands and then *continues exploring in that style*. A persona that says "test the project" will produce "I ran the project. It works."

**PList-style trait blocks for role definition.** The bracket format `[Role: X; Approach: Y; Focus: Z]` is token-efficient and models parse it reliably. Use this for establishing identity and behavioral constraints. Follow it with natural language for the action plan.

**Under 300 tokens total.** LLMs deprioritize instructions buried in long prompts. A 150-token persona with specific commands beats a 500-token one covering every edge case. The persona is injected into every turn of a memoryful session — bloat compounds across turns.

**Most important instruction goes last.** In PList-style blocks, traits at the end carry more weight. In the overall persona, the focus/findings instruction should be the final element — it's closest to where generation begins and most likely to be followed.

**Positive instructions over negative.** "Explore naturally and report what you find" works better than "Do NOT install packages or fix code." Tell the model what TO do, not what to avoid. Reserve negative constraints for the system prompt wrapping the session, not the persona itself.

### Persona Structure

A well-formed execution persona follows this layout:

```
[Role: {identity and approach};
 Behavior: {how they act — concrete verbs, not abstract traits};
 Focus: {what to report, what counts as a finding}]

{1-2 sentences: how to launch the project — exact command}
{1-2 sentences: how to interact — concrete commands/inputs to try}
{1 sentence: what to focus on — specific things that count as findings}
```

### Example

```
[Role: meticulous beta tester exploring a CLI text adventure;
 Approach: tries commands naturally, explores corners, notices inconsistencies;
 Focus: broken dialogue, unreachable rooms, commands that crash]

Start by running `uv run python main.py`. When you see the game prompt,
type 'look' to survey the area. Move with 'go north/south/east/west'.
Talk to NPCs with 'talk [name]' and try different dialogue choices.
Report any commands that crash, dialogue that cuts off abruptly,
rooms described but unreachable, or items that can't be used.
```

This is ~100 tokens, self-contained, and produces a model that explores methodically rather than running one command and stopping.

### The Planning Prompt

The prompt that *generates* a persona (e.g., `interact/plan.yaml`) must:

1. **Provide project context** — repo map, file list, architecture notes — so the planning model can determine how to launch and interact
2. **Request a single string output** — not JSON, not structured data. The persona is a prompt fragment, not a data object
3. **Include a concrete example** of a good persona — models produce better personas when they can see the expected style
4. **Specify the structure** — role block, launch command, interaction commands, focus. Don't leave it open-ended

### Anti-Patterns

- **Vague personas:** "Test the project and report issues" → model runs one command and says "it works"
- **Overly technical personas:** "Execute the main module via the uv package runner and validate standard output against expected behavioral specifications" → model gets confused by formality
- **Separate context fields:** Splitting persona and technical context into two inputs dilutes both — the persona should be self-contained
- **JSON-wrapped personas:** Requiring JSON output adds a parsing step and the model focuses on format compliance instead of persona quality

### Persona Injection Protocol

Flow personas are defined in `flows/cue/personas.cue` using PList-style format and injected into prompts via two standard blocks:

**`---ACT AS---`** — The current flow's persona. Injected when a flow declares `flow_persona` in its CUE definition. Tells the model what role it's playing for this task.

**`---PEERS---`** — Peer flow personas. Injected when a flow declares `known_personas: ["flow_a", "flow_b"]`. Tells the model what downstream roles will consume its output, so it can produce output they can act on directly.

Implementation:
- Persona definitions live in `flows/cue/personas.cue` as `_personas` (hidden, not exported)
- Flows reference them: `flow_persona: _personas.file_ops`
- Pre-compute formatters `format_flow_persona` and `format_known_personas` render the blocks
- Prompt templates include conditional sections gated on `context.flow_persona` / `context.peer_personas`

When adding personas to a new flow, add the definition to `personas.cue`, declare `flow_persona` and/or `known_personas` on the flow, add a pre-compute entry to the inference step, and add conditional template sections.

---

## 8. Section-Based Template Patterns

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

## 9. Temperature Guidelines

All temperature settings use relative `t*` multipliers for cross-model portability. The `t*` system resolves as `model_default_temperature × multiplier` at inference time.

**Model default range:** LLMVP model configs should set the default temperature between **0.5 and 1.0** for predictable behavior with the multipliers below. Below 0.5, low-end multipliers collapse to near-zero (greedy decoding). Above 1.0, high-end multipliers produce incoherent output. Vendor-recommended defaults outside this range (e.g., Mistral Small 4's recommended 0.1) should be overridden in the LLMVP model config.

| Task Type | Multiplier | Effective Range (0.7–1.0 default) | Rationale |
|-----------|------------|-----------------------------------|-----------|
| Structured output (JSON) | `t*0.0` – `t*0.2` | 0.0 – 0.2 | Format adherence critical. Creativity risks parse failures. Community consensus: 0.0–0.2 for structured output. |
| Code generation (new files) | `t*0.3` – `t*0.4` | 0.2 – 0.4 | Slight creativity within patterns. Community consensus: 0.0–0.3 optimal, but new file creation benefits from more exploration than modification. |
| Code modification (rewrites) | `t*0.3` | 0.2 – 0.3 | Lower than creation — preserving existing code demands focus. |
| Planning / analysis | `t*0.4` – `t*0.6` | 0.3 – 0.6 | Balanced exploration. Director reasoning and hypothesis generation need room to consider alternatives. |
| Persona crafting | `t*0.4` | 0.3 – 0.4 | Creative enough for engaging personas, structured enough to follow PList format. |
| Reflection / learning | `t*0.3` – `t*0.5` | 0.2 – 0.5 | Focused observations. Too high produces verbose, unfocused reflections. |
| Terminal command planning | `t*0.6` | 0.4 – 0.6 | Some exploration for commands — needs to try different approaches. |
| Terminal evaluation | `t*0.3` | 0.2 – 0.3 | Continue/close decision — needs consistency. |
| Retry after failure | Lower than original | — | Tighten focus after drift. |

**Key findings from community research:**
- Prompt engineering beats parameter tuning. Well-structured prompts with ✅/❌ examples yield larger gains than temperature optimization.
- Optimal settings are model-dependent. The same temperature produces different behavior across model families. The `t*` system mitigates this.
- Temperature above `t*1.2` rarely produces usable results. The added randomness does not translate into genuine creativity.
- Temperature 0.0 does not guarantee determinism. Hardware concurrency and floating-point precision can introduce tiny variations.

---

## 10. Extraction Pipeline

Fenced code blocks are the universal output protocol for all structured content. Prompts should instruct models to produce fenced output — the extractors are built for it and models naturally produce it.

### JSON Extraction
All JSON extraction calls `strip_markdown_wrapper()` which handles ```` ```json ````, ```` ```python ````, and plain ```` ``` ```` wrappers using `markdown-it-py` (CommonMark-compliant) with regex fallback. Prompts should request fenced JSON (```` ```json ````) — this aligns with the same convention used for code output and matches natural model behavior. Unfenced JSON is handled for robustness but should not be requested.

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

## 11. Config Values Are Static

The `config:` block in step definitions is **NOT template-rendered**. Values are passed directly to the inference engine.

```cue
config: temperature: 0.7           // absolute
config: temperature: "t*0.8"       // relative multiplier
```

The `t*` multiplier format is handled by `resolve_temperature()` in `agent/effects/inference.py`.

---

## 12. Retry-with-Limit via `meta.attempt`

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

## 13. Prompt Maintenance Checklist

When adding or modifying a prompt template, verify:

- [ ] **Role section present** — 1-3 sentences establishing identity and constraints
- [ ] **Output format section at the end** — with ✅/❌ example pair for machine-parsed outputs (JSON, file blocks, code). Optional for free-text analysis prompts (see §5).
- [ ] **Critical information at edges** — target file and task near the top; format spec at the bottom
- [ ] **Optional sections use `when:`** — for conditional context inclusion
- [ ] **Single output per prompt** — one file, one JSON object, or one reflection
- [ ] **Final reinforcement line** — "Return ONLY..." as the last line for parsed outputs
- [ ] **Fenced output** — JSON and code output use markdown fences (see §10)
- [ ] **Temperature set appropriately** — per the guidelines table above
- [ ] **Persona prompts follow section 7** — PList traits, concrete first action, under 300 tokens, focus last
- [ ] **Pre-computed keys documented** — comment header listing what formatters provide
- [ ] **Consistent with the soul** — step prompt reinforces (not contradicts) SOUL.md principles
