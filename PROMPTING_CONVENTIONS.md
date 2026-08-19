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

### Cache lifetimes — what content belongs where

The KV the server computes for a prompt is reused at one of four **lifetimes**. Placing a fact at the right one is a quiet but real efficiency lever: put it at the *shallowest lifetime whose scope it actually needs*. Too deep and it gets re-computed needlessly every call; too shallow and it wastes KV on every unrelated call (and, for the permanent layer, can't change without a server-side rebuild). This is gentle guidance, not a hard rule — but across a long run it adds up, and it maps cleanly onto the context layers above.

| Lifetime | Where it lives | Reused by | Put here only… |
|---|---|---|---|
| **Permanent** | the Soul / static buffer (Layer 1) | **every** call, every flow, for the whole server run | …**generalizable** identity + principles true for *all* flows — behavioral philosophy, universal output conventions. Nothing flow- or task-specific. |
| **Semi-permanent · flow** | the `cache: true` static head of a step template | every task & cycle of that one `(flow, step)` | …the **task-invariant** per-flow framing — role, instructions, output-format. Must read identically for every task (no `{input.*}`/`{context.*}` refs) or it won't cache. |
| **Semi-permanent · session** | a memoryful session's accumulated turns | every later turn *of the same session* | …**persistent details later turns reference** — the investigation transcript, plan state, prior answers, a per-session persona. |
| **Single-turn** | the dynamic tail of the prompt | nothing — computed once, then discarded | …only what is **specific to the exact task at hand** — the target file, the latest feedback, this cycle's state. |

In practice:
- **Generalizable → permanent.** If it holds for the create_file flow *and* diagnose *and* ops, it belongs in `SOUL.md` once, not repeated in each step prompt.
- **Per-flow invariant → flow head.** Role framing and output-format identical for every task this step runs go in the leading `cache: true` sections (§10). Keep them free of task variables so the head stays byte-identical and shareable.
- **Carries across turns → session.** If a fact must survive into the *next* turn of the same multi-turn loop, let the session hold it — don't re-send it each turn. The session-injection queue (§7) adds to it without a wasted inference.
- **Just this task → tail.** Everything keyed to the specific input — file contents, feedback, per-cycle results — is the dynamic tail. It comes last (§10) and is the only part re-prefilled each call.

The runtime mechanics behind these lifetimes (resident in-context sequences vs the legacy `save_state` path, eviction bounds, the SWA `swa_full` requirement) live in [dev/archive/docs/CACHE_STATE.md](dev/archive/docs/CACHE_STATE.md). As a prompt author you only need the placement guidance above plus the ordering rule in §10.

### Template Format

Prompts live as YAML files in `prompts/<flow>/<step>.yaml` in two shapes. The PRIMARY shape (turn-based steps, the migration target — ~2/3 of files) is **single-`content` fragments** composed by the CUE step's `turn.sections: [{type, template}]` list — each fragment holds one section's text and the CUE declaration owns the assembly order. The legacy shape is a **section-based file** carrying its own `sections:` list, referenced via `prompt_template.template`. In both shapes, complex data formatting is handled by **pre-compute formatters** (Python functions) that run before template rendering — the template itself only does simple variable substitution.

```yaml
# Legacy section-based shape (fragment example: prompts/create/task.yaml
# plus flows/code_core/create.cue's turn.sections list):
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

**CRITICAL:** The output format section must include at least one concrete example of the expected format. It should sit near the end — *except* for prompts re-issued every cycle with only a small varying tail (planners, judges), where it belongs in the static head so the whole head can be KV-cached and only the dynamic tail (latest feedback/state) trails it. See §10 "Cache-aware ordering." Why this is safe: Ouroboros runs at well under 5% of the model's context window, where mid-prompt position is negligible — the format spec does not need to be last to be followed.

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

### Declaring a free-text contract

`check_prompt_conventions` enforces the ✅/❌ rule above, and it cannot tell by reading a prompt whether its output gets parsed. Left to guess, it asked every free-text prompt for examples the paragraph above says not to add — so those findings sat unresolved and unresolvable, which is how a real signal becomes noise.

A prompt whose output is genuinely free-text declares it, as a top-level key beside `id:`:

```yaml
id: quality_gate/evaluate_ux_session
# output_contract: free_text — a UX ASSESSMENT published verbatim as
# ux_session_assessment. Judgement prose; §5's warning applies directly.
output_contract: free_text
```

The key is read by the linter only — no loader or renderer touches it, so it is inert at runtime. **Undeclared defaults to parsed**, the stricter reading: a new prompt must earn its exemption rather than get one by omission. Declare it only when nothing regex- or JSON-parses the output; if a parser reads the result, the prompt needs the examples, not the exemption.

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

For prompts where the model should produce unstructured observations (diagnostic summaries, director analysis, session summaries):

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

## 7. Session Inference Shapes

Memoryful sessions are the primary mechanism for multi-turn reasoning in Ouroboros — symbol selection, file investigation, editor loops. Every call into a session has one of three shapes, and confusing them is one of the easiest ways to write a prompt that silently corrupts reasoning-model outputs.

### The three shapes

**1. Ask — direct inference with an expected answer.** The caller has a question and wants a text response. Free-form natural language, a plan, a diagnosis, a generated file. The response is *consumed by the caller*. Budget the tokens the answer might need.

```python
result = await effects.session_inference(session_id, prompt, {"temperature": 0.3})
answer = result.text.strip()
# use `answer`
```

**2. Select — menu selection with a bounded choice.** The caller needs one of a small enumerated set of outcomes. The response format is structured (JSON object or named token) and naturally self-terminating — the model writes `}` and stops. Menu selection is a specialization of "ask" that never needs a `max_tokens` cap because the format bounds the response.

```python
prompt = build_menu_prompt(options, instruction="What next?", style="direct")
result = await effects.session_inference(session_id, prompt, {"temperature": 0.1})
choice = extract_choice(result.text, list(options.keys()))
```

**3. Inject — context update with no response.** The caller has information the model needs (seed context, a correction notice, tool output), but nothing useful to do with a generated response. The naïve implementation — `session_inference(...)` with a small `max_tokens` cap to minimize wasted compute — is a trap on reasoning-model families. On Nemotron-3-super, `max_tokens=20` cut the model mid-reasoning, producing history fragments like `"We need to respond with 'eady' as per instr"` that poisoned every subsequent turn.

Use `agent.session_injections` for this shape. The injection is queued in context and prepended to the next real inference's prompt. No wasted inference calls, no `max_tokens` cap, identical behaviour for thinking and non-thinking models.

### The queue/consume pattern

```python
from agent.session_injections import consume, queue

# Producer — a step that wants to add context without generating:
updates: dict[str, Any] = {...}
queue(updates, step_input.context, "File X could not be read — select another")
return StepOutput(..., context_updates=updates)

# Consumer — every step that calls session_inference:
prompt, clears = consume(step_input.context, my_prompt)
result = await effects.session_inference(session_id, prompt, params)
return StepOutput(
    ...,
    context_updates={**clears, ...other updates...},
)
```

### The rules

- **Every `session_inference` call site consumes pending injections first.** This is the single rule that makes the pattern safe: any step anywhere can queue context for the next inference without coordinating with the consumer. Forget this and the queue leaks into later turns, or worse, gets replayed in unrelated flows.
- **Never use `max_tokens` as cost control.** If you care about the response, budget enough tokens for the answer (thinking models need thousands). If you don't care about the response, you don't want an inference call — use injection.
- **Never fire-and-forget `session_inference`.** "Run the call and ignore the result" is always wrong. Either the response matters (shape: ask) or it doesn't (shape: inject). The `max_tokens=20` ack pattern straddles both incorrectly and produces the reasoning-model pathology above.
- **Combine shapes freely on one call.** A consumer can also be a producer — `pick_suspect_file` consumes pending injections, presents a menu, and queues a correction message if the response doesn't parse. The next menu turn sees the correction prepended.

### Anti-pattern to avoid

```python
# WRONG — fire-and-forget "injection" via inference
await effects.session_inference(
    session_id,
    "Investigation note: could not read file",
    {"max_tokens": 30},  # <— reasoning-model trap
)
```

```python
# RIGHT — queue the injection for the next real inference
from agent.session_injections import queue
queue(updates, step_input.context, "Investigation note: could not read file")
```

---

## 8. Tool Result Delivery

When an injection is delivering the result of a tool call (a trace, a file read, an external query), it needs explicit framing. Bare context — "here are some lines from a file" — is interpreted by the model as ambient project background, not as the return value of its previous request. The model then re-requests the tool, sees the same content arrive, and re-requests again. Across six diagnose-heavy runs, **46-79% of trace requests in failing sessions were re-requests of already-traced symbols.** The model's CoT during these spins consistently said either "we need to actually trace X" (model can't see the result) or "format is wrong, retry the JSON" (model invented a rejection narrative because there was no acceptance feedback). The lower-spin runs were also the higher-success runs, with the most successful run (505) showing the lowest spin rate.

This convention defines the framing for tool-result injections so the model recognizes them as such.

### The two signals

**Tool result framing — what the tool returned.** Wraps the actual returned content with explicit boundary markers and tool identification. Queued by the action that executed the tool (e.g. `execute_symbol_trace` queues this with the rendered trace body):

```
Observation (from your trace of `parser.py:parse_command`):

def parse_command(raw: str) -> Command:
    """Parse a raw user input line into a Command instance."""
    ...

(End of observation.)
```

**Acceptance signal — what the system did with the model's last reply.** A brief acknowledgment that the model's previous menu choice was parsed and executed. Queued universally at the menu-choice publish point in `_execute_inference_action` for any successfully-extracted choice on a session-bearing menu turn:

```
[Your previous selection of 'trace' with argument 'parser.py:parse_command' was accepted and executed.]
```

The two signals stack cleanly when both fire. Order at the start of the next prompt:

```
[Your previous selection of 'trace' with argument 'parser.py:parse_command' was accepted and executed.]

Observation (from your trace of `parser.py:parse_command`):

def parse_command(raw: str) -> Command:
    ...

(End of observation.)

[menu envelope follows]
```

The acceptance signal addresses **format paranoia** (~74% of observed spin events): the model invents a "rejection" narrative because it has no signal that its previous JSON was good. The tool result framing addresses **result invisibility** (~24% of observed spin events): the model has the trace body in its prompt but doesn't recognize it as a tool return because the framing is ambiguous.

### Why this format

The `Observation:` prefix is the dominant cross-framework convention for tool returns. The original ReAct paper (Yao et al., 2022) introduced it; HotpotQA few-shot trajectories use it; LangChain's standard agent patterns use it; agent-tutorial blog posts the model has seen extensively use it. It is the single label that has the strongest training-data anchor for "this is what your tool returned" across the open-source LLM corpus. Anthropic's `tool_result` content blocks, OpenAI's `role: "tool"` messages, and Harmony's `Role.TOOL` author all serve the same purpose at the API level — but those mechanisms are below the LLMVP API surface and not directly available to us. The textual `Observation:` prefix is the closest plain-text approximation that reaches the same priors.

The bracket convention `[...]` is reserved for procedural meta-signals — acceptance confirmations, system status messages, anything that's about the conversation rather than substantive content. This keeps brackets distinct from `Observation:` (which labels substantive content) and from harmony-style `<|tokens|>` (which the model could mistakenly treat as openable). When the model sees `[...]`, it should read it as system narration about the turn boundary; when it sees `Observation:`, it should read what follows as tool data.

The closing marker `(End of observation.)` provides an explicit boundary so the model knows where its tool's data ends and the next turn's prompt begins. This matters when the menu envelope or other rendered turn content follows directly — without a boundary, the model can blur the result with the next instructions.

### What to strip as noise

Line-range annotations like `(lines 67-165)` in body headers add visual variation without semantic content. The body itself communicates what's there. Line ranges shift between cycles when files are edited, making the same logical symbol look like a different result on each delivery — encouraging the model to re-request even when the underlying code is unchanged. Strip them from:

- Trace target body headers
- Same-file reference body headers
- Class-level data block headers

Keep line numbers where they identify a **specific actionable point**, not a body range — such as upstream call sites in the "Called by" section, where `loader.py:142` tells the model exactly where the call lives in the caller's file.

Within the framed observation, use `--- Subsection ---` rather than `=== Subsection ===` for internal dividers. Strong markers (`===`) are reserved for the framing itself; weaker ones for the structure inside it. This way the model's attention anchors on the framing as the outermost structure.

### Anti-patterns

```
=== parser.py: parse_command (lines 67-165) ===
def parse_command(raw: str) -> Command:
    ...
```

This is what failed in e39, f3d, 6c2, 88c, 902. The leading `=== file: symbol ===` header looks the same as project-context headers used elsewhere in prompts. Without an `Observation:` label, the model interprets the body as ambient context and re-requests the trace. Without a closing boundary, the body bleeds into whatever comes next.

```
Here is the result of your trace:
def parse_command(...):
    ...
```

Better than no framing, but still weaker than the standard. "Result of your trace" doesn't have the same training-data weight as `Observation:`. Models trained on agent corpora respond more reliably to the established label.

```
[Trace returned 142 lines:]
... body ...
```

Brackets for substantive content blurs the meta vs substance distinction. The model then has weaker priors about which bracketed content is system narration (skip in CoT) versus tool data (reason about). Reserve brackets for procedural messages.

### Cross-reference

This convention layers on top of Section 7's queue/consume mechanism. The queue/consume pattern is the *plumbing*; the framing here is the *shape* of what flows through. Any action that delivers tool output via `session_injections.queue` should produce content shaped like this — both the `Observation:`-framed tool result and (where applicable) the bracketed acceptance signal.

---

## 9. Role / Persona Crafting Prompts

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

### Named Block Protocol (`---BLOCK---` pattern)

Named blocks use `---NAME---` delimiters to visually separate critical information that the model must attend to. The delimiter pattern creates strong attention boundaries in the prompt — the model treats block content as higher-priority than surrounding context.

Use named blocks for information that is:
- **Mandatory** — the model must conform to it, not just consider it
- **Structurally distinct** — it's a different kind of content from the surrounding prompt
- **High-consequence** — ignoring it causes failures that are expensive to fix

Standard named blocks:

**`---ACT AS---`** — The current flow's persona. Injected when a flow declares `flow_persona` in its CUE definition. Tells the model what role it's playing for this task.

**`---PEERS---`** — Peer flow personas. Injected when a flow declares `known_personas: ["flow_a", "flow_b"]`. Tells the model what downstream roles will consume its output, so it can produce output they can act on directly.

**`---DATA CONTRACTS (MANDATORY)---`** — Data format contracts from the architecture. Injected when the target file produces or consumes data files. The model MUST use the exact key names and structure specified. Rendered by `render_data_contracts` from the `file_context` projection's `data_shapes`.

### Persona Implementation

- Persona definitions live in `flows/code_core/personas.cue` as `_personas` (hidden, not exported)
- Flows reference them: `flow_persona: _personas.file_ops`
- Persona text is lazy-loaded from compiled.json (`agent/formatters.py` `_persona_cache`) and rendered into the persona sections
- Prompt templates include conditional sections gated on `context.flow_persona` / `context.peer_personas`

When adding personas to a new flow, add the definition to `personas.cue`, declare `flow_persona` and/or `known_personas` on the flow, add a pre-compute entry to the inference step, and add conditional template sections.

---

## 10. Section-Based Template Patterns

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

### Cache-aware ordering (static-first / dynamic-last)

For a prompt that is **re-issued every cycle with only a small varying tail** — ops `plan_provision` / `charter_accomplish` / the judge, the file-ops planners — order the sections **static-first, dynamic-last** and mark the leading invariant run `cache: true`:

```yaml
  - id: system_role        # ┐
    cache: true            # │
  - id: task               # │ invariant head — role · task · instructions ·
    cache: true            # │ output-format. Pinned in the per-flow KV cache
  - id: instructions       # │ (config.model.flow_kv_cache) so it is prefilled
    cache: true            # │ ONCE per (flow, task) and reused every cycle.
  - id: output_format      # ┘
    cache: true
  - id: feedback           # the ONLY dynamic section — comes LAST, ends the
    when: context.feedback_block   # cached prefix, and lands in the recency slot.
    content: "{context.feedback_block}"
```

How it works (`PromptRenderer.render_with_cache_split` → `runtime` → `effects.run_inference(static_prefix, flow_key)` → llmvp): the renderer splits the **leading contiguous run** of `cache: true` sections from the rest, reconstructing the full prompt verbatim (output-neutral). Only a *leading prefix* caches, so **any dynamic section ends the run** — put every varying section (feedback, latest state, per-cycle results) at the bottom. If a template's only dynamic section is conditionally absent (e.g. feedback on cycle 0), the whole prompt is static, the tail is empty, and the runtime sends it normally (no cache that cycle).

Why static-first/dynamic-last is safe (and slightly preferable) here, despite the classic "important content last" guidance:
- **It's a relative-length effect.** "Lost in the middle" (Liu et al., TACL 2024) and positional bias bite when the input fills **≳25–50%** of the model's context window (COLM 2025, arXiv 2508.07479), not at any absolute length. Ouroboros operates at **~2–5k tokens on ≥128k windows (<5% fill)** — far below that, so the whole prompt sits in the high-attention zone and mid-prompt position is within noise.
- **The two privileged edges still get used.** A mild causal-mask **primacy** bias favors the front → good home for static context; **recency** favors the end → the per-cycle feedback (the freshest, most actionable signal) goes there.
- **No "restate at the end" insurance needed** at this context fraction. The one positional effect that survives short context is *few-shot label-order* bias — so if a prompt ever stacks labeled in-context examples, balance/shuffle them; our `✅/❌` blocks are format exemplars, not labeled demonstrations, and are immune.

This is the ONLY place the "output-format last" rule (§2) is overridden. Single-shot prompts (no per-cycle re-issue) keep format-near-the-end and need no `cache:` markers.

---

## 11. Temperature Guidelines

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

## 12. Extraction Pipeline

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

## 13. Config Values Are Static

The `config:` block in step definitions is **NOT template-rendered**. Values are passed directly to the inference engine.

```cue
config: temperature: 0.7           // absolute
config: temperature: "t*0.8"       // relative multiplier
```

The `t*` multiplier format is handled by `resolve_temperature()` in `agent/effects/inference.py`.

---

## 14. Retry-with-Limit via `meta.attempt`

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

## 15. No Firm Numeric Constraints

**Never give a bare count the task can contradict.** `(2-4 steps)`,
`under 200 words`, `at most 6 symbols` — a number is sticky to attention in
a way the prose beside it is not, so it wins even when the work needs
otherwise, and the model optimises the count instead of the job.

This is not hypothetical. A charter prompt said `TEST STEPS ... (2-4 steps)`
and, one line below, already carried the escape hatch: *"if reaching the
target genuinely requires movement, write the exact route as explicit
steps."* Given a game whose win is seven steps from the start, a captured
reasoning trace shows the model spending its ENTIRE turn on the number —

> *"That's 7 steps, exceeds limit."* … *"rule says 2-4 steps ... Must
> adhere."* … *"Maybe we can **cheat**: use 'use crystal_key' immediately
> after entering boss chamber."* … *"Maybe we can start at boss chamber by
> launching program with a saved state? Not allowed."* … *"I'll produce a
> brief with maybe 6 steps, hoping it's acceptable."*

— cycling through shortcuts, rejecting one as not allowed, and violating
the budget anyway. The qualifier directly beneath the number never got a
vote. Downstream, that same pressure applied to a goal that could not be
satisfied honestly produced four artifacts written to satisfy a checker
rather than a player, including a `suicide` command added purely to reach
an unreachable defeat screen.

**Write the intent instead.**

| ❌ firm count | ✅ intent |
|---|---|
| `A numbered short list (2-4 steps)` | `A numbered list, as short as the job allows` |
| `Target length: under 200 words` | `Keep it brief — a tester should take it in at a glance` |
| `Emit 2-4 NEW queries` | `Emit a handful of NEW queries` |

**When the number exists to mean "too big", make the model REPORT the
overflow rather than trim to fit.** Trimming destroys the signal the bound
existed to raise:

```
❌  List at most 6 symbols — if more would need to change, the refactor
    is too large to land in one patch.
✅  List every symbol that must change with it — do NOT truncate the list
    to keep it short. If it runs long, that IS the finding: the refactor
    is too large for one patch, and a silently shortened list hides that.
```

**THE ANNOUNCEMENT SUBTYPE — a count that restates a list it sits above.**
Every example so far is a BUDGET: a number constraining how much output to
produce. The other shape is a number that merely announces an enumeration
which then follows literally — `The charter must contain five labeled
parts:` above a list of six. It cannot constrain the job, because the list
underneath is the authority; it can only be correct and redundant, or wrong
and expensive. `interact/charter_function` carried exactly that for as long
as its CONSTRAINT part existed, and a live CoT shows the model paying for it
on every charter turn:

> *"Potential issue: The user said 'The charter must contain five labeled
> parts' but listed six. We include six. Good."*

This is the rule's stated failure mode — a number the model must reconcile
with the work — in its mildest and most durable form: nobody re-counts the
list when a part is added, so the number silently drifts out of agreement.
Two reviewers (and two editing passes on that same file) read past it.
**Write `these labeled parts, in order:` and let the list speak.** When
auditing for §15, grep for spelled-out counts in front of enumerations
(`\b(one|two|three|four|five|six|seven|eight) (labeled|numbered)\b`), not
just for parenthesised ranges.

A hard bound is legitimate only where the number is a REAL external limit
(a context window, an API page size, a protocol field), not a stylistic
preference. If violating it would merely make the output longer than you
would like, it is a preference — say so in prose.

**This governs prompt TEXT the model reads, not control flow.** The
`meta.attempt` retry bounds in §14 are resolver guards the engine enforces;
the model never sees them and cannot try to satisfy them. Keep those. The
failure mode here is specifically a number placed in front of the model as
a requirement it must reconcile with the work.

## 16. Prompt Maintenance Checklist

When adding or modifying a prompt template, verify:

- [ ] **Role section present** — 1-3 sentences establishing identity and constraints
- [ ] **Output format section near the end** — with ✅/❌ example pair for machine-parsed outputs (JSON, file blocks, code). Optional for free-text analysis prompts (see §5). EXCEPTION: cycle-re-issued prompts put it in the `cache: true` static head with the dynamic tail last (§10 "Cache-aware ordering").
- [ ] **Critical information at edges** — target file and task near the top; format spec at the bottom
- [ ] **Optional sections use `when:`** — for conditional context inclusion
- [ ] **Single output per prompt** — one file, one JSON object, or one reflection
- [ ] **Final reinforcement line** — "Return ONLY..." as the last line for parsed outputs
- [ ] **Fenced output** — JSON and code output use markdown fences (see §12)
- [ ] **Temperature set appropriately** — per the guidelines table above
- [ ] **Persona prompts follow section 9** — PList traits, concrete first action, under 300 tokens, focus last
- [ ] **Pre-computed keys documented** — comment header listing what formatters provide
- [ ] **Consistent with the soul** — step prompt reinforces (not contradicts) SOUL.md principles
- [ ] **No firm numeric constraints** — no bare count the task could contradict (§15); write the intent, and where a bound means "too big", have the model report the overflow instead of trimming to fit
