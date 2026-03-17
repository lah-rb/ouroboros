# Ouroboros — Flow Gap Analysis & Implementation Guide

*A principled analysis of missing flows in the Ouroboros agent system, informed by the rising-star developer archetype and software engineering literature. This document identifies capability gaps, provides detailed flow designs, and proposes an implementation ordering.*

*Reference reading: See `ouroboros_reading_list.md` for the books that informed this analysis.*

---

## 1. The Developer Model

Ouroboros models a rising-star junior developer — talented, disciplined, learns fast, and knows when to seek guidance. The flow system is a logical map of this developer's work life. Every flow represents a cognitive activity a developer performs, and the flow graph represents how those activities compose into productive work.

The current 15 flows model:

| Activity | Flows | Status |
|---|---|---|
| Planning & Orientation | mission_control, create_plan, revise_plan | Working |
| Implementation | create_file, modify_file, setup_project | Working |
| Quality & Verification | validate_output, quality_gate, create_tests | Working |
| Context & Research | prepare_context, research_context | Working |
| Meta-cognition & Learning | capture_learnings | Working |
| Internal Testing | test_simple, test_branching, test_inference | Working |

The gap analysis below identifies activities a rising-star developer performs that are not yet modeled.

---

## 2. Identified Flow Gaps

### Group 1: "Think Before You Act" — Diagnosis and Investigation

These fill the gap between "I have a task" and "I'm writing code." This is where weaker models fail hardest — they jump to code changes without understanding the problem. Separating thinking from acting is the highest-leverage improvement for output quality.

**`diagnose_issue`** — Methodical debugging separated from fix-and-retry. Reads errors, traces execution mentally, forms hypotheses, evaluates them against the code, and produces a structured diagnosis with fix recommendations. Does NOT modify code.

- Invoked by: `modify_file` (on validation failure), `mission_control` (for "fix bug" tasks)
- Composes: `prepare_context`, `research_context`
- Terminal statuses: `success` (confident diagnosis), `escalate_recommended` (intractable), `failed`
- Key insight: separate "understand" → "hypothesize" → "evaluate" → "recommend" as distinct steps with different temperature settings

**`explore_spike`** — Time-boxed investigation that produces knowledge, not code. Plans what to investigate, scans broadly, reads deeply, analyzes patterns, and synthesizes a structured findings document.

- Invoked by: `mission_control` (for investigation tasks), `create_plan` (when objective involves unfamiliar codebase)
- Composes: `prepare_context`, `research_context`, `capture_learnings`
- Terminal statuses: `success` (findings produced)
- Key insight: investigation plan first (strategic reading, not random browsing), two-tier reading (broad scan then deep read), bounded by step count

### Group 2: "Work at a Higher Level" — Systems Thinking

These are what separate writing files from building software. They model architectural thinking, quality discipline, and communication.

**`integrate_modules`** — Analyzes a multi-file project holistically, identifies disconnected components, missing imports, absent entry points, and produces the glue code that makes individual files into a working system.

- Invoked by: `mission_control` (after batch of create_file tasks), `quality_gate` (when modules are disconnected)
- Composes: `prepare_context`, `validate_output`, `diagnose_issue`, `capture_learnings`
- Terminal statuses: `success`, `already_integrated`, `nothing_to_integrate`, `escalate_recommended`, `failed`
- Key insight: reads ALL project files (not targeted scanning), produces multi-file changes in one pass, validates the integrated system end-to-end
- New action needed: `apply_multi_file_changes` — parses multi-file output format and writes each file through effects (~50 lines, reusable)

**`refactor`** — Deliberate structural improvement without behavior change. Identifies code smells using Fowler's vocabulary, proposes named refactoring operations, applies them one at a time, and verifies tests pass after each change.

- Invoked by: `mission_control` (periodic maintenance), `quality_gate` (when smells flagged), as prep before complex `modify_file`
- Composes: `prepare_context`, `validate_output`, `capture_learnings`
- Terminal statuses: `success`, `code_is_clean`, `too_risky`, `needs_tests_first`, `blocked` (tests already failing), `failed`
- Key insight: green baseline required (won't refactor with failing tests), one refactoring per pass with verification, two paths based on test coverage (full refactoring with tests vs safe-only without tests), rollback mechanism for failed refactorings, budget cap on operations per pass

**`document_project`** — Produces or updates README, module docstrings, and architecture notes. Reads the actual code and produces documentation that reflects the current state, not aspirations.

- Invoked by: `mission_control` (final mission task or periodic maintenance), `integrate_modules` (after integration)
- Composes: `prepare_context`, `validate_output` (for docstring changes), `capture_learnings`
- Terminal statuses: `success`, `documentation_adequate`, `failed`
- Key insight: assessment-first (survey what exists before generating), three distinct branches (README, docstrings, architecture), docstring changes get behavior verification, documentation grounded in actual code

### Group 3: "Self-Awareness and Growth" — Meta-Cognition

These make Ouroboros improve over time. They close the loop between capturing observations and actually changing behavior.

**`retrospective`** — Periodic self-assessment. Reviews completed work, analyzes patterns in successes and failures, evaluates effort distribution, and produces actionable recommendations that modify the mission plan.

- Invoked by: `mission_control` (every N tasks, at milestones, when frustration patterns are concerning)
- Composes: `capture_learnings`
- Terminal statuses: `success`, `too_early` (not enough data), `no_changes_needed`
- Key insight: data-driven (loads concrete metrics, not vibes), trigger_reason shapes analysis focus, recommendations are typed (add_task, reprioritize, revise_plan, note_for_knowledge_base) and applied as mission state changes, can flag concerns for the shop director via events
- Produces: `mission_health` assessment ("on_track" | "at_risk" | "needs_intervention")

**`request_review`** — Proactive code review submission. The agent is confident in completed work but seeks verification, feedback, and learning from the senior dev. Distinct from escalation in framing, purpose, and response handling.

- Invoked by: `mission_control` (for high-priority tasks, based on review_policy config)
- Composes: `prepare_context`, `capture_learnings`
- Terminal statuses: `success` (approved), routes to `mission_control` for changes_needed or major_rework
- Key insight: different system prompt framing than escalation ("reviewing completed work" not "helping with a problem"), self-assessment included in the request, feedback categorized by urgency (must-fix, should-fix, consider-later, positive), review unavailability is success not failure (review is enhancement, not gate)
- **Requires Phase 6 (escalation) for the API call.** Can be designed now, implemented after escalation exists.

### Group 4: Research Expansion — Specialized Research Sub-Flows

These expand `research_context` from a single web search into a router that dispatches to specialized research strategies.

**`research_context` v2** — Becomes a dispatcher. Classifies the research query (web search, local library, codebase history, technical literature) and routes to the appropriate sub-flow. The interface to callers stays identical.

**`research_local_library`** — RAG against a local collection of reference materials (books, documentation, project notes). Vector similarity search against indexed chunks with source attribution.
- Requires: RAG infrastructure (vector store, embedding model, indexing pipeline)
- Fallback: web search if library unavailable

**`research_codebase_history`** — Git archaeology. Plans git commands based on the research question, executes them, and synthesizes the history into an answer.
- Requires: working directory is a git repo, `effects.run_command()` with git
- Immediately implementable with existing infrastructure

**`research_technical`** — Specialized web search targeting authoritative technical sources (official docs, academic papers, authoritative tutorials). Filters results for technical quality.
- Immediately implementable as a specialized web search with source filtering

---

## 3. Implementation Ordering

The ordering optimizes for: what unblocks the most capability, what builds on existing infrastructure, and what has the fewest external dependencies.

### Wave 1: Foundation Improvements (Highest Leverage)

These improve every downstream flow and have zero external dependencies.

| Order | Flow | Rationale | Dependencies | New Actions |
|---|---|---|---|---|
| 1.1 | `diagnose_issue` | Immediately improves `modify_file` success rate. Separating diagnosis from fixing is the highest-leverage change for code quality on weaker models. | `prepare_context` (exists) | `compile_diagnosis` |
| 1.2 | `explore_spike` | Feeds better context into planning and modification. Makes the agent effective on unfamiliar codebases. | `prepare_context`, `research_context`, `capture_learnings` (all exist) | `read_investigation_targets` |

**Wiring changes:**
- `modify_file` gains a new transition: validation failure with non-trivial error → `diagnose_issue` sub-flow → retry with diagnosis as context
- `mission_control` gains dispatch capability for investigation-typed tasks → `explore_spike`
- `create_plan` can invoke `explore_spike` as a sub-flow when the objective involves an unfamiliar codebase

**Verification:**
- Give the agent a task with a subtle bug. Verify it diagnoses before fixing, and that the diagnosis-informed fix succeeds where blind retry would fail.
- Give the agent an exploration task on an existing codebase. Verify it produces a useful findings document that improves subsequent planning.

### Wave 2: Systems-Level Capabilities

These address the gap between "files exist" and "project works."

| Order | Flow | Rationale | Dependencies | New Actions |
|---|---|---|---|---|
| 2.1 | `integrate_modules` | The most visible quality problem — files that don't reference each other. | `prepare_context`, `validate_output`, `diagnose_issue` (Wave 1) | `apply_multi_file_changes` |
| 2.2 | `refactor` | Enables the agent to improve its own output. The "sweep the floor" behavior. | `prepare_context`, `validate_output`, `capture_learnings` | `check_remaining_smells`, `restore_file_from_context` |
| 2.3 | `document_project` | Important for mission completion quality. | `prepare_context`, `validate_output`, `capture_learnings` | `check_remaining_doc_tasks` (uses `apply_multi_file_changes` from 2.1) |

**Wiring changes:**
- `mission_control` gains dispatch capability for integration, refactoring, and documentation tasks
- `mission_control` or `create_plan` should insert integration tasks after batches of creation tasks
- `quality_gate` gains transitions to `integrate_modules` (when modules are disconnected) and `refactor` (when smells are flagged)
- `refactor` can be invoked as a preparatory step before complex `modify_file` tasks

**Verification:**
- Create a mission that builds a multi-file project. Verify `integrate_modules` produces working imports, entry points, and __init__.py files.
- Give the agent a file with known code smells. Verify `refactor` identifies them by name, applies fixes one at a time, and rolls back any that break tests.
- Verify `document_project` produces a README that accurately describes the actual project, not aspirational content.

### Wave 3: Meta-Cognitive Capabilities

These close the learning loop and build trust.

| Order | Flow | Rationale | Dependencies | New Actions |
|---|---|---|---|---|
| 3.1 | `retrospective` | Closes the loop between capturing learnings and changing behavior. Without this, accumulated learnings are dead data. | `capture_learnings` (exists) | `load_retrospective_data`, `apply_retrospective_recommendations`, `compose_director_report` |
| 3.2 | `request_review` | Builds trust, catches issues the agent can't self-detect. | `prepare_context`, `capture_learnings`, **Phase 6 escalation** | `submit_review_to_api` |

**Wiring changes:**
- `mission_control` gains periodic retrospective triggering (every N tasks or at milestones)
- `mission_control` gains review dispatch for high-priority tasks (gated on `review_policy` in mission config)
- `retrospective` recommendations feed back into mission state: new tasks, reprioritized plan, notes

**Verification:**
- Run a mission with several tasks (some succeeding, some failing). Verify `retrospective` produces data-driven analysis with actionable recommendations.
- Verify recommendations actually modify mission state (new tasks appear, priorities shift).
- For `request_review` (after Phase 6): submit completed work, verify the review feedback is categorized and actionable.

### Wave 4: Research Expansion

These deepen research capabilities incrementally.

| Order | Flow | Rationale | Dependencies | New Actions |
|---|---|---|---|---|
| 4.1 | `research_codebase_history` | Immediately implementable with existing git + effects.run_command(). No new infrastructure. | `effects.run_command()` (exists) | `run_git_investigation` |
| 4.2 | `research_technical` | Immediately implementable as specialized web search. No new infrastructure. | `web_search` action (exists) | None new |
| 4.3 | `research_context` v2 | Refactor into a dispatcher that routes to sub-flows based on query classification. | 4.1, 4.2 | None (restructure, not new) |
| 4.4 | `research_local_library` | Requires RAG infrastructure: vector store, embedding model, indexing pipeline. Build when the infrastructure is justified. | Vector store, embedding model, indexing CLI | `check_rag_index`, `rag_retrieve`, `format_rag_results` |

**Wiring changes:**
- `research_context` evolves from a single flow to a dispatcher. Callers (`prepare_context`, `explore_spike`) are unaffected — same interface, better routing internally.
- New CLI command: `uv run ouroboros.py library index --source /path/to/book.pdf --name "pragmatic_programmer"` (for 4.4)

**Verification:**
- Point `research_codebase_history` at a git repo and ask "why was this function changed recently?" Verify it runs appropriate git commands and synthesizes a useful answer.
- Verify `research_context` v2 correctly classifies queries and routes to the right sub-flow with graceful fallback to web search.

---

## 4. New Actions Summary

Actions needed across all waves, listed by wave:

### Wave 1
- `compile_diagnosis` — Assembles diagnosis fields (root cause, affected files, selected fix, rejected alternatives, confidence) from inference outputs into a structured `Diagnosis` model. ~40 lines.
- `read_investigation_targets` — Reads full content of files identified in an investigation plan. Uses `effects.read_file()` for each target, respects max_files and max_bytes limits. ~50 lines.

### Wave 2
- `apply_multi_file_changes` — Parses `=== FILE: path ===` delimited multi-file output from inference, writes each file through `effects.write_file()`. Returns success/failure per file. ~60 lines. **Reused by `integrate_modules` and `document_project`.**
- `check_remaining_smells` — Compares the smell analysis list against applied refactorings, returns count of remaining actionable items. ~20 lines.
- `restore_file_from_context` — Restores a file to a previous version stored in the context accumulator. Used for refactoring rollback. ~30 lines.
- `check_remaining_doc_tasks` — Compares the documentation assessment against completed doc tasks, returns remaining count. ~20 lines.
- `run_validation` — May already exist as part of `validate_output`. If not, wraps `effects.run_command()` for test execution with structured result parsing. ~40 lines.

### Wave 3
- `load_retrospective_data` — Loads mission history, task outcomes, timing data, and accumulated learnings from persistence. Structured aggregation for analysis. ~60 lines.
- `apply_retrospective_recommendations` — Translates typed recommendations into mission state changes: creates new TaskRecords, updates priorities, appends notes. ~50 lines.
- `compose_director_report` — Formats retrospective findings as a human-readable report and pushes as an event. ~30 lines.
- `submit_review_to_api` — Wraps `effects.escalate_to_api()` with review-specific system prompt and response parsing. Blocked on Phase 6. ~40 lines.

### Wave 4
- `run_git_investigation` — Executes planned git commands via `effects.run_command()`, collects and formats output. ~40 lines.
- `check_rag_index` — Verifies local library index exists and is queryable. ~15 lines.
- `rag_retrieve` — Embeds query, performs vector similarity search, returns ranked passages. Requires vector store integration. ~60 lines.
- `format_rag_results` — Formats retrieved passages with source attribution. ~20 lines.

**Total new actions: ~16, ranging from 15-60 lines each.**

---

## 5. Updated Flow Graph

After all waves, the system grows from 15 to ~25 flows. The composition patterns:

**Shared infrastructure sub-flows (used by 5+ parent flows):**
- `prepare_context` — context gathering
- `validate_output` — quality verification
- `capture_learnings` — knowledge persistence
- `research_context` — research routing

**Group composition patterns:**
- Group 2 flows compose Group 1 flows: `integrate_modules` → `diagnose_issue`, `refactor` → `validate_output`
- Group 3 flows observe the whole system: `retrospective` reads all artifacts, `request_review` submits to external
- Group 4 flows are composed by Groups 1-2 via `research_context`: `explore_spike` → `research_context` → sub-flows

**Dependency direction (no circular dependencies across groups):**
```
Group 4 (Research) ← composed by ← Group 1 (Diagnosis/Investigation)
Group 1 (Diagnosis) ← composed by ← Group 2 (Systems)
Group 2 (Systems) ← observed by ← Group 3 (Meta-cognition)
All groups ← orchestrated by ← mission_control
```

---

## 6. Changes to mission_control

`mission_control` needs the following enhancements to orchestrate the new flows:

**Dispatch expansion:** The `prepare_dispatch` step must map task types to appropriate flows:
- Task type "create" → `create_file`
- Task type "modify" or "fix" → `modify_file`
- Task type "investigate" → `explore_spike`
- Task type "integrate" → `integrate_modules`
- Task type "refactor" → `refactor`
- Task type "document" → `document_project`
- Task type "test" → `create_tests`
- Task type "setup" → `setup_project`

**Periodic triggers:** The `assess` step should check for periodic maintenance:
- Every N completed tasks → trigger `retrospective`
- After a batch of creation tasks with no integration task → insert integration task
- When accumulated AGENT-TODO count exceeds threshold → insert refactoring task
- When quality_gate flags documentation gaps → insert documentation task

**Review gating:** After a task completes with `success` status:
- Check mission config `review_policy`
- If policy requires review for this task's priority level → dispatch `request_review` before moving to next task

**Retrospective integration:** When `retrospective` returns with `changes_applied`:
- New tasks from recommendations are already in mission state
- Next `assess` cycle picks them up naturally via the existing task selection logic

---

## 7. Design Principles Applied

These flows were designed following principles from the reference reading list:

**From SICP — Abstraction Barriers:**
Every shared sub-flow has a documented contract. Parent flows depend on the contract (inputs, terminal statuses, published keys), not on internal implementation. `research_context` v2 is the clearest example: callers see the same interface, but the internal routing changes entirely.

**From Professor Frisby — Composition as Primary Design Tool:**
New flows compose existing flows rather than reimplementing. `integrate_modules` composes `prepare_context` + `validate_output` + `diagnose_issue` + `capture_learnings`. No new infrastructure is needed — just new compositions of existing pieces.

**From The Pragmatic Programmer — Tracer Bullets and Deliberate Practice:**
`integrate_modules` is the tracer bullet — scaffold the integration first. `refactor` is deliberate practice — improve structure as a distinct activity. `explore_spike` is "Know Your Limitations" — investigate before committing.

**From Apprenticeship Patterns — Growth Model:**
`retrospective` is "Record What You Learn" made actionable. `explore_spike` is "Confront Your Ignorance." `request_review` is "Expose Your Ignorance." `refactor` with safe-only mode (no tests) is "Retreat into Competence."

**From Refactoring (Fowler) — Named Operations:**
`refactor` uses Fowler's smell vocabulary in prompts and requires the model to name its refactoring operations. This makes output more predictable, reviewable, and learnable.

**From Continuous Delivery — Pipeline and Feedback Loops:**
The flow chain (create → validate → quality_gate → integrate → document) is a pipeline. Each stage gates progression on quality. `retrospective` shortens the feedback loop from "learnings accumulate until someone reads them" to "learnings are reviewed every N tasks."

**From AntiPatterns — Defensive Design:**
`refactor`'s green baseline requirement prevents the Lava Flow antipattern (fear of touching code). The frustration cap prevents infinite retry loops (a form of Analysis Paralysis). `integrate_modules`'s explicit disconnection detection prevents the project from becoming a collection of isolated files (a form of Stovepipe System).

**From The Clean Coder — Professional Responsibility:**
`request_review` models proactive quality assurance. `document_project` models documentation as a responsibility. `retrospective`'s "flag for director" path models honest status communication.

**From A Philosophy of Software Design — Deep Modules:**
Shared sub-flows (`prepare_context`, `validate_output`) are deep: simple interface, significant internal complexity. The flow count grows but the composition surface stays narrow.

---

## Appendix A: Flow YAML Templates

*These templates were designed in conversation without access to the full Ouroboros codebase. They represent the intended structure, step composition, prompt strategy, and resolver logic for each flow. Adaptation will be required when implementing — particularly around action names, context key names, and integration points with existing flows. Treat these as detailed blueprints, not copy-paste implementations.*

*Known areas likely to need adjustment:*
- *Action names must match the registered action registry in the actual codebase*
- *Context keys published by existing flows (prepare_context, validate_output, etc.) may use different names than assumed here*
- *Prompt templates reference context fields that depend on upstream action output shapes*
- *The `effects` blocks in step definitions use a declarative syntax that may need to be translated to the actual effects invocation pattern used by the runtime*
- *Tail-call input_map templates assume specific field names in the context accumulator*

---

### A.1 — `diagnose_issue`

```yaml
flow: diagnose_issue
version: 1
description: >
  Methodical diagnosis of a code issue. Reads error output, gathers context,
  forms hypotheses, gathers evidence, and produces a structured diagnosis
  with root cause analysis and fix recommendations. Does NOT modify code.

input:
  required:
    - error_description
    - target_file_path
  optional:
    - mission_id
    - task_id
    - error_output
    - previous_attempt
    - working_directory

defaults:
  config:
    temperature: "t*0.6"
    max_tokens: 4096

steps:

  gather_error_context:
    action: flow
    flow: prepare_context
    description: "Read the failing file and its immediate dependencies"
    input_map:
      target_file_path: "{{ input.target_file_path }}"
      working_directory: "{{ input.working_directory }}"
      depth: "imports"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: reproduce_mentally
        - condition: "result.status != 'success'"
          transition: diagnosis_failed
    publishes:
      - project_context
      - target_file

  reproduce_mentally:
    action: inference
    description: >
      Read the error output and the code. Trace the execution path mentally.
      Identify exactly where and why the failure occurs. Do NOT suggest fixes yet.
    context:
      required: [target_file, error_description]
      optional: [error_output, project_context, previous_attempt]
    prompt: |
      You are diagnosing a code issue. Your job is to UNDERSTAND the problem, not fix it yet.

      ## The Error
      {{ context.error_description }}

      {% if context.error_output %}
      ## Raw Error Output
      ```
      {{ context.error_output }}
      ```
      {% endif %}

      ## The File Where It Manifests
      **{{ context.target_file.path }}**
      ```python
      {{ context.target_file.content }}
      ```

      {% if context.project_context %}
      ## Related Files
      {% for filepath, summary in context.project_context.items() %}
      {% if filepath != context.target_file.path %}
      **{{ filepath }}** (first lines):
      ```
      {{ summary }}
      ```
      {% endif %}
      {% endfor %}
      {% endif %}

      {% if context.previous_attempt %}
      ## What Was Already Tried
      {{ context.previous_attempt }}
      (This approach did not resolve the issue.)
      {% endif %}

      ## Your Task
      1. Trace the execution path that leads to this error.
      2. Identify the EXACT line(s) where the problem originates (not just where it surfaces).
      3. Explain WHY the error occurs — what assumption is violated, what state is unexpected.
      4. Do NOT suggest fixes yet. Just understand the problem.

      Respond with:
      - **Error location**: file and line(s) where the root cause is
      - **Execution trace**: how control flow reaches the error
      - **Root cause**: the underlying reason, not just the symptom
      - **Violated assumption**: what the code expected vs what actually happened
    config:
      temperature: "t*0.4"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: form_hypotheses
    publishes:
      - error_analysis

  form_hypotheses:
    action: inference
    description: >
      Based on the error analysis, generate 2-3 hypotheses about what could
      fix the issue. Each hypothesis should be distinct and testable.
    context:
      required: [target_file, error_analysis, error_description]
      optional: [project_context, previous_attempt]
    prompt: |
      You diagnosed this error:

      ## Error Analysis
      {{ context.error_analysis }}

      {% if context.previous_attempt %}
      A previous fix attempt failed: {{ context.previous_attempt }}
      Do NOT re-suggest the same approach.
      {% endif %}

      ## Generate Fix Hypotheses
      Propose 2-3 DISTINCT approaches to fix this issue. For each:

      1. **Hypothesis**: What specific change would fix it
      2. **Reasoning**: Why this would address the root cause identified above
      3. **Risk**: What could go wrong or what else might break
      4. **Scope**: Which file(s) need to change and roughly how
      5. **Confidence**: low / medium / high

      Order them from most likely to succeed to least likely.
      Prefer minimal, focused changes over large refactors.

      Respond as a numbered list of hypotheses with the fields above.
    config:
      temperature: "t*0.8"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: evaluate_hypotheses
    publishes:
      - hypotheses

  evaluate_hypotheses:
    action: inference
    description: >
      Evaluate the hypotheses against the codebase context. Check whether
      each hypothesis is actually feasible given the code structure.
      Select the best approach and produce a structured diagnosis.
    context:
      required: [target_file, error_analysis, hypotheses]
      optional: [project_context]
    prompt: |
      You generated these fix hypotheses:

      {{ context.hypotheses }}

      Now evaluate each against the actual code:

      **{{ context.target_file.path }}**
      ```python
      {{ context.target_file.content }}
      ```

      {% if context.project_context %}
      Consider dependencies and interactions with:
      {% for filepath, summary in context.project_context.items() %}
      {% if filepath != context.target_file.path %}
      - {{ filepath }}
      {% endif %}
      {% endfor %}
      {% endif %}

      For each hypothesis:
      1. Is it actually implementable given the code structure?
      2. Would it introduce new issues?
      3. Does it address the ROOT cause or just the symptom?

      Then select the BEST hypothesis and explain why.

      Respond with:
      - **Selected approach**: which hypothesis number
      - **Justification**: why this one over the others
      - **Implementation plan**: specific lines to change and how
      - **Files to modify**: list of files that need changes
      - **Verification**: how to confirm the fix works
    config:
      temperature: "t*0.3"
    resolver:
      type: llm_menu
      prompt: "Is the diagnosis complete enough to act on?"
      options:
        complete:
          description: "The diagnosis is clear and actionable — ready to produce a fix recommendation"
        needs_more_context:
          description: "Need to read additional files to evaluate hypotheses properly"
        intractable:
          description: "The issue is too complex for confident diagnosis — should escalate"
    publishes:
      - evaluation

  gather_additional_context:
    action: flow
    flow: prepare_context
    description: "Read additional files identified during hypothesis evaluation"
    input_map:
      target_file_path: "{{ context.evaluation.additional_files[0] }}"
      working_directory: "{{ input.working_directory }}"
      depth: "imports"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: evaluate_hypotheses
    publishes:
      - project_context

  complete:
    action: compile_diagnosis
    description: "Assemble the final structured diagnosis document"
    context:
      required: [error_analysis, hypotheses, evaluation, error_description]
      optional: [target_file, previous_attempt]
    params:
      include_rejected_hypotheses: true
    terminal: true
    status: success
    publishes:
      - diagnosis

  intractable:
    action: compile_diagnosis
    description: "Issue is beyond confident diagnosis — package what we know for escalation"
    context:
      required: [error_analysis, hypotheses, error_description]
      optional: [evaluation]
    params:
      include_rejected_hypotheses: true
      mark_as_intractable: true
    terminal: true
    status: escalate_recommended
    publishes:
      - diagnosis

  diagnosis_failed:
    action: noop
    description: "Could not even gather context for the failing file"
    terminal: true
    status: failed
    publishes:
      - diagnosis

entry: gather_error_context

overflow:
  strategy: split
  fallback: reorganize
```

---

### A.2 — `explore_spike`

```yaml
flow: explore_spike
version: 1
description: >
  Time-boxed investigation of a codebase, module, pattern, or technology.
  Produces a structured findings document without modifying any code.
  Used to build understanding before planning or implementation.

input:
  required:
    - investigation_goal
  optional:
    - mission_id
    - task_id
    - scope_hint
    - time_budget_steps
    - working_directory
    - specific_questions

defaults:
  config:
    temperature: "t*0.5"
    max_tokens: 4096

steps:

  plan_investigation:
    action: inference
    description: >
      Given the investigation goal, produce a focused investigation plan:
      what to look at, in what order, and what specific questions to answer.
    context:
      required: [investigation_goal]
      optional: [scope_hint, specific_questions]
    prompt: |
      You are about to investigate a codebase to understand something before taking action.

      ## Investigation Goal
      {{ context.investigation_goal }}

      {% if context.scope_hint %}
      ## Starting Point
      Begin your investigation at: {{ context.scope_hint }}
      {% endif %}

      {% if context.specific_questions %}
      ## Specific Questions to Answer
      {% for q in context.specific_questions %}
      - {{ q }}
      {% endfor %}
      {% endif %}

      ## Plan Your Investigation
      Produce a focused investigation plan. You have a limited budget of exploration steps,
      so prioritize what's most important to understand.

      For each step:
      1. **What to examine**: specific file, directory, or concept
      2. **What to look for**: specific patterns, interfaces, dependencies, conventions
      3. **Why this matters**: how it connects to the investigation goal

      Order steps from most critical to least critical.
      Limit to 3-5 investigation steps.
    config:
      temperature: "t*0.6"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: scan_structure
    publishes:
      - investigation_plan

  scan_structure:
    action: flow
    flow: prepare_context
    description: "Scan project structure and read key files identified in the plan"
    input_map:
      target_file_path: "{{ input.scope_hint or '.' }}"
      working_directory: "{{ input.working_directory }}"
      depth: "full"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: deep_read
        - condition: "result.status != 'success'"
          transition: synthesize
    publishes:
      - project_context

  deep_read:
    action: read_investigation_targets
    description: >
      Read the specific files identified in the investigation plan.
      Not just first-N-lines summaries — full content of the most
      important files for understanding.
    context:
      required: [investigation_plan, project_context]
      optional: []
    params:
      max_files: 5
      max_bytes_per_file: 50000
    resolver:
      type: rule
      rules:
        - condition: "result.files_read > 0"
          transition: analyze
        - condition: "result.files_read == 0"
          transition: synthesize
    publishes:
      - deep_context

  analyze:
    action: inference
    description: >
      With the investigation plan and the gathered code, analyze patterns,
      architecture, conventions, dependencies, and risks. Answer the
      specific questions from the investigation goal.
    context:
      required: [investigation_goal, investigation_plan, deep_context]
      optional: [project_context, specific_questions]
    prompt: |
      You are analyzing a codebase to understand it. Here's what you planned to investigate
      and what you found.

      ## Investigation Goal
      {{ context.investigation_goal }}

      ## Investigation Plan
      {{ context.investigation_plan }}

      ## Code You've Read
      {% for filepath, content in context.deep_context.items() %}
      ### {{ filepath }}
      ```python
      {{ content }}
      ```
      {% endfor %}

      {% if context.project_context %}
      ## Project Structure Overview
      {% for filepath, summary in context.project_context.items() %}
      {% if filepath not in context.deep_context %}
      - **{{ filepath }}**: {{ summary[:100] }}
      {% endif %}
      {% endfor %}
      {% endif %}

      ## Analyze
      Address each point from your investigation plan. For each:
      1. **What you found**: concrete observations from the code
      2. **Patterns identified**: recurring conventions, architectural patterns, coding style
      3. **Dependencies**: what depends on what, what's tightly vs loosely coupled
      4. **Risks**: potential issues, fragile areas, technical debt you noticed
      5. **Conventions**: naming patterns, file organization, import style, testing patterns

      {% if context.specific_questions %}
      ## Also Answer These Specific Questions
      {% for q in context.specific_questions %}
      - {{ q }}
      {% endfor %}
      {% endif %}

      Be specific. Reference file names, function names, and line numbers.
      Distinguish between what you observed and what you're inferring.
    config:
      temperature: "t*0.4"
    resolver:
      type: llm_menu
      prompt: "Is the analysis sufficient to meet the investigation goal?"
      options:
        sufficient:
          description: "The analysis addresses the investigation goal adequately"
        need_deeper_look:
          description: "There's a specific area that needs closer examination"
        need_external_research:
          description: "The investigation requires external knowledge (documentation, articles, references)"
    publishes:
      - analysis

  deeper_look:
    action: inference
    description: >
      The initial analysis identified a specific area needing closer examination.
      Formulate what to look at next and why.
    context:
      required: [analysis, investigation_goal]
      optional: [deep_context, project_context]
    prompt: |
      Your analysis of the codebase identified areas needing deeper investigation:

      {{ context.analysis }}

      What specific file or code section needs closer examination?
      What specific question does examining it answer?

      Be precise: name the file and the function/class/section.
    config:
      temperature: "t*0.3"
    resolver:
      type: rule
      rules:
        - condition: "meta.attempt < 3"
          transition: deep_read
        - condition: "meta.attempt >= 3"
          transition: synthesize
    publishes:
      - investigation_plan

  external_research:
    action: flow
    flow: research_context
    description: "Research external sources for patterns, documentation, or references"
    input_map:
      research_query: "{{ context.analysis.research_needed }}"
      working_directory: "{{ input.working_directory }}"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: synthesize
        - condition: "result.status != 'success'"
          transition: synthesize
    publishes:
      - external_research

  synthesize:
    action: inference
    description: >
      Produce the final structured findings document. This is the deliverable
      of the spike — consumed by downstream flows as enriched context.
    context:
      required: [investigation_goal, analysis]
      optional: [project_context, deep_context, external_research, specific_questions]
    prompt: |
      You've completed an investigation. Synthesize your findings into a structured document.

      ## Investigation Goal
      {{ context.investigation_goal }}

      ## Your Analysis
      {{ context.analysis }}

      {% if context.external_research %}
      ## External Research
      {{ context.external_research }}
      {% endif %}

      ## Produce a Findings Document

      Structure your findings as:

      ### Summary
      2-3 sentence overview of what you learned.

      ### Architecture & Patterns
      How the code is structured, what patterns it uses, what conventions it follows.

      ### Key Interfaces
      The most important functions, classes, or modules — what they do and how they connect.

      ### Risks & Technical Debt
      Issues you identified that could cause problems.

      ### Recommendations
      Concrete suggestions for how to approach work in this codebase.
      What to be careful about. What patterns to follow. What to avoid.

      {% if context.specific_questions %}
      ### Answers to Specific Questions
      {% for q in context.specific_questions %}
      **Q: {{ q }}**
      A: [your answer]
      {% endfor %}
      {% endif %}

      ### Files Referenced
      List every file you examined with a one-line note about its role.

      Be concrete and actionable. This document will be used by other flows
      that need to understand this codebase before making changes.
    config:
      temperature: "t*0.3"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: complete
    publishes:
      - findings

  complete:
    action: flow
    flow: capture_learnings
    description: "Record investigation findings as a persistent learning"
    input_map:
      learning_type: "investigation"
      content: "{{ context.findings }}"
      mission_id: "{{ input.mission_id }}"
      task_id: "{{ input.task_id }}"
    terminal: true
    status: success
    publishes:
      - findings
      - investigation_summary

entry: plan_investigation

overflow:
  strategy: split
  fallback: reorganize
```

---

### A.3 — `integrate_modules`

```yaml
flow: integrate_modules
version: 1
description: >
  Analyzes a multi-file project holistically, identifies disconnected components,
  missing imports, absent entry points, and configuration gaps, then produces
  and applies the glue code that makes individual files into a working system.

input:
  required:
    - mission_id
    - mission_objective
  optional:
    - task_id
    - working_directory
    - expected_entry_point
    - integration_hints

defaults:
  config:
    temperature: "t*0.4"
    max_tokens: 8192

steps:

  inventory_project:
    action: flow
    flow: prepare_context
    description: "Read every file in the project — we need the complete picture"
    input_map:
      target_file_path: "."
      working_directory: "{{ input.working_directory }}"
      depth: "full"
      include_content: true
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success' and result.file_count > 1"
          transition: analyze_connections
        - condition: "result.status == 'success' and result.file_count <= 1"
          transition: nothing_to_integrate
        - condition: "result.status != 'success'"
          transition: failed
    publishes:
      - project_context
      - project_structure

  analyze_connections:
    action: inference
    description: >
      Map the dependency graph. Identify what imports what, what's disconnected,
      what interfaces are defined but never consumed, and what's missing
      for the system to work as a whole.
    context:
      required: [project_context, mission_objective]
      optional: [expected_entry_point, integration_hints, project_structure]
    prompt: |
      You are a systems integrator reviewing a multi-file Python project.
      Your job is to analyze how the pieces connect and identify what's missing.

      ## Project Objective
      {{ context.mission_objective }}

      {% if context.expected_entry_point %}
      ## Expected Entry Point
      {{ context.expected_entry_point }}
      {% endif %}

      {% if context.integration_hints %}
      ## Integration Notes from Planning
      {{ context.integration_hints }}
      {% endif %}

      ## Project Files
      {% for filepath, content in context.project_context.items() %}
      ### {{ filepath }}
      ```python
      {{ content }}
      ```

      {% endfor %}

      ## Analyze the Integration State

      Produce a structured analysis:

      ### 1. Dependency Map
      For each file, list:
      - What it imports (from stdlib, from third-party, from this project)
      - What it exports (public classes, functions, constants)
      - What project modules it SHOULD import based on its purpose but DOESN'T

      ### 2. Disconnected Components
      Which files exist in isolation? They define functionality that nothing else uses,
      or they reference functionality that doesn't exist yet.

      ### 3. Missing Glue
      What's needed to make this a working system:
      - Missing __init__.py files for packages?
      - Missing entry point / main script?
      - Missing configuration that connects components?
      - Missing import statements within existing files?
      - Missing adapter/wrapper code between incompatible interfaces?

      ### 4. Interface Mismatches
      Where do two modules THINK they connect but their interfaces don't actually match?
      (Function signatures, return types, expected data structures that differ.)

      ### 5. Execution Path
      If someone ran the entry point, what would happen step by step?
      Where would execution fail or reach a dead end?

      Be specific. Reference file names, function names, line numbers.
    config:
      temperature: "t*0.3"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: plan_integration
    publishes:
      - connection_analysis

  plan_integration:
    action: inference
    description: >
      From the connection analysis, produce an ordered list of specific
      integration tasks. Each task is a single file change.
    context:
      required: [connection_analysis, project_context, mission_objective]
      optional: [expected_entry_point]
    prompt: |
      Based on your integration analysis:

      {{ context.connection_analysis }}

      ## Plan the Integration

      Produce an ORDERED list of integration tasks. Each task should be one
      focused change to one file. Order matters — do foundational work
      (package __init__.py, base configuration) before wiring (imports, entry points).

      For each task:
      1. **File**: which file to create or modify
      2. **Action**: create / modify
      3. **Description**: what specifically to do
      4. **Depends on**: which earlier tasks in this list must complete first
      5. **Rationale**: why this integration step is needed

      Keep tasks minimal. An import fix is one task. An __init__.py is one task.
      An entry point script is one task. Don't bundle unrelated changes.

      After listing tasks, assess:
      - **Confidence**: How confident are you that completing all tasks will
        produce a working, integrated system?
      - **Remaining risks**: What might still not work even after all tasks complete?
    config:
      temperature: "t*0.3"
    resolver:
      type: llm_menu
      prompt: "Review the integration plan. Is it actionable?"
      options:
        execute:
          description: "Plan is clear and actionable — proceed with integration"
        too_complex:
          description: "Integration requires changes too complex for automated application — needs manual review"
        already_integrated:
          description: "The analysis shows the modules are already properly connected — no changes needed"
    publishes:
      - integration_plan

  execute_integration:
    action: inference
    description: >
      For each task in the integration plan, produce the file content.
      Creates new files or produces modified versions of existing files.
    context:
      required: [integration_plan, project_context]
      optional: [connection_analysis]
    prompt: |
      Execute this integration plan by producing the file changes needed.

      ## Integration Plan
      {{ context.integration_plan }}

      ## Current File Contents
      {% for filepath, content in context.project_context.items() %}
      ### {{ filepath }}
      ```python
      {{ content }}
      ```
      {% endfor %}

      ## Produce Changes

      For EACH task in the integration plan, produce the complete file content.

      For NEW files: provide the complete file.
      For MODIFIED files: provide the complete modified file (not a diff).

      Format your response as a series of file blocks:

      === FILE: path/to/file.py ===
      ```python
      [complete file content]
      ```

      === FILE: path/to/other.py ===
      ```python
      [complete file content]
      ```

      Include EVERY file that needs to change. Do not skip files.
      Do not include files that don't need changes.
    config:
      temperature: "t*0.2"
      max_tokens: 12288
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: apply_changes
    publishes:
      - integration_code

  apply_changes:
    action: apply_multi_file_changes
    description: >
      Parse the integration output and write each file.
    context:
      required: [integration_code]
      optional: [integration_plan]
    resolver:
      type: rule
      rules:
        - condition: "result.all_written == true"
          transition: validate
        - condition: "result.all_written == false"
          transition: failed
    publishes:
      - files_changed

  validate:
    action: flow
    flow: validate_output
    description: "Run validation on the integrated project"
    input_map:
      working_directory: "{{ input.working_directory }}"
      validation_scope: "project"
      entry_point: "{{ input.expected_entry_point }}"
      check_imports: true
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: complete
        - condition: "result.status == 'failed' and meta.attempt < 2"
          transition: diagnose_integration_failure
        - condition: "result.status == 'failed' and meta.attempt >= 2"
          transition: failed
    publishes:
      - validation_results

  diagnose_integration_failure:
    action: flow
    flow: diagnose_issue
    description: "Integration validation failed — diagnose before retrying"
    input_map:
      error_description: "Integration validation failed after applying changes"
      target_file_path: "{{ context.validation_results.failing_file or '.' }}"
      error_output: "{{ context.validation_results.error_output }}"
      working_directory: "{{ input.working_directory }}"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: execute_integration
        - condition: "result.status != 'success'"
          transition: failed
    publishes:
      - diagnosis

  complete:
    action: flow
    flow: capture_learnings
    description: "Record what integration work was needed and why"
    input_map:
      learning_type: "integration"
      content: "{{ context.connection_analysis }}"
      mission_id: "{{ input.mission_id }}"
    terminal: true
    status: success
    publishes:
      - integration_summary
      - files_changed

  already_integrated:
    action: noop
    description: "Modules are already properly connected"
    terminal: true
    status: success
    publishes:
      - integration_summary

  nothing_to_integrate:
    action: noop
    description: "Only one file in project — nothing to integrate"
    terminal: true
    status: success

  too_complex:
    action: noop
    description: "Integration requires changes too complex for automated application"
    terminal: true
    status: escalate_recommended
    publishes:
      - connection_analysis
      - integration_plan

  failed:
    action: noop
    terminal: true
    status: failed
    publishes:
      - connection_analysis
      - validation_results

entry: inventory_project

overflow:
  strategy: split
  fallback: reorganize
```

---

### A.4 — `refactor`

```yaml
flow: refactor
version: 1
description: >
  Deliberate structural improvement of existing code without changing behavior.
  Identifies code smells, proposes named refactorings, applies them one at a time,
  and verifies tests still pass after each change.

input:
  required:
    - target_file_path
  optional:
    - mission_id
    - task_id
    - working_directory
    - specific_smells
    - refactoring_budget
    - findings

defaults:
  config:
    temperature: "t*0.4"
    max_tokens: 8192

steps:

  gather_context:
    action: flow
    flow: prepare_context
    description: "Read the target file, its tests, and its immediate dependencies"
    input_map:
      target_file_path: "{{ input.target_file_path }}"
      working_directory: "{{ input.working_directory }}"
      depth: "imports_and_tests"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: baseline_tests
        - condition: "result.status != 'success'"
          transition: failed
    publishes:
      - target_file
      - project_context
      - test_files

  baseline_tests:
    action: run_validation
    description: >
      Run existing tests BEFORE any changes. Refactoring requires a green
      baseline — if tests are already failing, we can't refactor safely.
    context:
      required: [target_file]
      optional: [test_files]
    params:
      scope: "related"
      working_directory: "{{ input.working_directory }}"
    resolver:
      type: rule
      rules:
        - condition: "result.all_passing == true"
          transition: identify_smells
        - condition: "result.all_passing == false and result.no_tests == true"
          transition: identify_smells_no_tests
        - condition: "result.all_passing == false"
          transition: cannot_refactor
    publishes:
      - baseline_results

  identify_smells:
    action: inference
    description: >
      Analyze the code for structural issues using Fowler's smell vocabulary.
    context:
      required: [target_file]
      optional: [project_context, specific_smells, findings, test_files]
    prompt: |
      You are performing a code review focused on structural quality.
      Do NOT change behavior — only identify structural improvements.

      ## Target File
      **{{ context.target_file.path }}**
      ```python
      {{ context.target_file.content }}
      ```

      {% if context.test_files %}
      ## Related Tests
      {% for filepath, content in context.test_files.items() %}
      **{{ filepath }}**
      ```python
      {{ content }}
      ```
      {% endfor %}
      {% endif %}

      {% if context.specific_smells %}
      ## Known Issues to Address
      {% for smell in context.specific_smells %}
      - {{ smell }}
      {% endfor %}
      {% endif %}

      {% if context.findings %}
      ## Codebase Context
      {{ context.findings }}
      {% endif %}

      ## Identify Code Smells

      Review the code for these common issues (Fowler's catalog):
      - **Long Method**: functions over 25 lines that do multiple things
      - **Feature Envy**: methods that use another class's data more than their own
      - **Data Clumps**: groups of parameters that travel together
      - **Primitive Obsession**: using primitives where a domain type would be clearer
      - **Duplicate Code**: repeated logic that could be extracted
      - **Long Parameter List**: functions with more than 3-4 parameters
      - **Dead Code**: unused functions, unreachable branches, commented-out code
      - **Inappropriate Naming**: names that don't communicate intent

      Also check for:
      - Missing type hints
      - Missing or inadequate docstrings
      - Inconsistent code style
      - AGENT-TODO or AGENT-NOTE markers requesting attention

      For each smell found:
      1. **Smell name**: from the catalog above
      2. **Location**: file, function/class, approximate line
      3. **Description**: what's wrong and why it matters
      4. **Proposed refactoring**: name the refactoring operation (Extract Method, Rename Variable, etc.)
      5. **Risk**: low / medium / high
      6. **Impact**: low / medium / high

      Prioritize: high-impact, low-risk refactorings first.
      Limit to 5 most important findings.
    config:
      temperature: "t*0.5"
    resolver:
      type: llm_menu
      prompt: "Review the identified smells. How should we proceed?"
      options:
        proceed:
          description: "Found meaningful refactoring opportunities — proceed with changes"
        code_is_clean:
          description: "The code is already well-structured — no refactoring needed"
        too_risky:
          description: "The identified refactorings are too risky without better test coverage"
    publishes:
      - smell_analysis

  identify_smells_no_tests:
    action: inference
    description: >
      Same as identify_smells but with explicit awareness that there's no
      safety net. Only suggest low-risk refactorings.
    context:
      required: [target_file]
      optional: [project_context, specific_smells, findings]
    prompt: |
      You are performing a code review focused on structural quality.

      **IMPORTANT: This file has NO test coverage.** Only suggest refactorings
      where you are highly confident the behavior will not change.
      Limit suggestions to:
      - Rename operations (variables, functions, parameters)
      - Adding type hints and docstrings
      - Removing dead code (truly unreachable, not just unused)
      - Extracting constants from magic numbers/strings

      Do NOT suggest:
      - Restructuring logic flow
      - Extracting methods that change call signatures
      - Modifying class hierarchies
      - Any change where the before/after equivalence isn't obvious

      ## Target File
      **{{ context.target_file.path }}**
      ```python
      {{ context.target_file.content }}
      ```

      {% if context.specific_smells %}
      ## Known Issues
      {% for smell in context.specific_smells %}
      - {{ smell }}
      {% endfor %}
      {% endif %}

      For each safe refactoring found:
      1. **Smell name**
      2. **Location**
      3. **Proposed refactoring** (name the operation)
      4. **Why this is safe without tests**: explain the equivalence

      Limit to 3 most impactful safe refactorings.
    config:
      temperature: "t*0.3"
    resolver:
      type: llm_menu
      prompt: "Any safe refactorings to apply?"
      options:
        proceed:
          description: "Found safe refactoring opportunities"
        code_is_clean:
          description: "No safe refactorings to suggest"
        needs_tests_first:
          description: "Meaningful refactoring requires test coverage first — suggest writing tests"
    publishes:
      - smell_analysis

  apply_refactoring:
    action: inference
    description: >
      Apply the HIGHEST priority refactoring from the smell analysis.
      ONE refactoring per pass — verify tests after each one.
    context:
      required: [target_file, smell_analysis]
      optional: [project_context, previous_refactorings]
    prompt: |
      Apply the next refactoring from this analysis:

      {{ context.smell_analysis }}

      {% if context.previous_refactorings %}
      ## Already Applied
      {% for r in context.previous_refactorings %}
      - {{ r }}
      {% endfor %}
      Apply the NEXT refactoring that hasn't been done yet.
      {% else %}
      Apply the HIGHEST priority refactoring.
      {% endif %}

      ## Current File Content
      **{{ context.target_file.path }}**
      ```python
      {{ context.target_file.content }}
      ```

      ## Rules
      - Apply EXACTLY ONE refactoring operation.
      - The behavior MUST remain identical.
      - Name the refactoring you're applying (e.g., "Extract Method: _validate_input from process_data").
      - Explain the before/after equivalence in one sentence.

      Respond with:
      - **Refactoring applied**: name and description
      - **Equivalence argument**: why behavior is unchanged
      - Then the complete modified file in ```python ``` markers.
    config:
      temperature: "t*0.2"
    effects:
      - write_file:
          path: "{{ context.target_file.path }}"
          content: "{{ result.modified_content }}"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: verify_refactoring
    publishes:
      - refactoring_applied
      - target_file

  verify_refactoring:
    action: run_validation
    description: "Confirm tests still pass after the refactoring"
    context:
      required: [target_file, refactoring_applied]
      optional: [test_files, baseline_results]
    params:
      scope: "related"
      working_directory: "{{ input.working_directory }}"
    resolver:
      type: rule
      rules:
        - condition: "result.all_passing == true and meta.refactoring_count < (input.refactoring_budget or 3)"
          transition: check_more_refactorings
        - condition: "result.all_passing == true"
          transition: complete
        - condition: "result.all_passing == false"
          transition: rollback_refactoring
    publishes:
      - post_refactoring_results

  check_more_refactorings:
    action: check_remaining_smells
    description: "Are there more refactorings from the analysis to apply?"
    context:
      required: [smell_analysis, refactoring_applied]
      optional: [previous_refactorings]
    resolver:
      type: rule
      rules:
        - condition: "result.remaining > 0"
          transition: apply_refactoring
        - condition: "result.remaining == 0"
          transition: complete
    publishes:
      - previous_refactorings

  rollback_refactoring:
    action: restore_file_from_context
    description: >
      The last refactoring broke tests. Restore the file to its pre-refactoring
      state and record the failure.
    context:
      required: [target_file, refactoring_applied, post_refactoring_results]
    params:
      restore_key: "target_file_before_refactoring"
    resolver:
      type: rule
      rules:
        - condition: "result.restored == true and meta.refactoring_count < (input.refactoring_budget or 3)"
          transition: check_more_refactorings
        - condition: "true"
          transition: complete
    publishes:
      - target_file
      - failed_refactoring

  complete:
    action: flow
    flow: capture_learnings
    description: "Record what refactorings were applied and their outcomes"
    input_map:
      learning_type: "refactoring"
      content: "{{ context.previous_refactorings }}"
      mission_id: "{{ input.mission_id }}"
    terminal: true
    status: success
    publishes:
      - refactoring_summary
      - files_changed

  code_is_clean:
    action: noop
    description: "Code reviewed, no refactoring needed"
    terminal: true
    status: success
    publishes:
      - smell_analysis

  too_risky:
    action: noop
    description: "Refactorings identified but too risky without better test coverage"
    terminal: true
    status: deferred
    publishes:
      - smell_analysis

  needs_tests_first:
    action: noop
    description: "Meaningful refactoring blocked on test coverage"
    terminal: true
    status: deferred
    publishes:
      - smell_analysis

  cannot_refactor:
    action: noop
    description: "Tests already failing — can't refactor without a green baseline"
    terminal: true
    status: blocked
    publishes:
      - baseline_results

  failed:
    action: noop
    terminal: true
    status: failed

entry: gather_context

overflow:
  strategy: split
  fallback: reorganize
```

---

### A.5 — `document_project`

```yaml
flow: document_project
version: 1
description: >
  Produce or update project documentation: README, module docstrings,
  architecture notes, usage examples. Reads the actual code and produces
  documentation that accurately reflects the current state of the project.

input:
  required:
    - mission_id
    - mission_objective
  optional:
    - task_id
    - working_directory
    - doc_scope
    - existing_docs
    - findings

defaults:
  config:
    temperature: "t*0.5"
    max_tokens: 8192

steps:

  gather_context:
    action: flow
    flow: prepare_context
    description: "Read the full project to understand what to document"
    input_map:
      target_file_path: "."
      working_directory: "{{ input.working_directory }}"
      depth: "full"
      include_content: true
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: assess_documentation_state
        - condition: "result.status != 'success'"
          transition: failed
    publishes:
      - project_context
      - project_structure

  assess_documentation_state:
    action: inference
    description: "Survey existing documentation and identify gaps."
    context:
      required: [project_context, mission_objective]
      optional: [existing_docs, doc_scope, project_structure, findings]
    prompt: |
      You are a technical writer reviewing a Python project's documentation.

      ## Project Objective
      {{ context.mission_objective }}

      ## Project Files
      {% for filepath, content in context.project_context.items() %}
      ### {{ filepath }}
      ```
      {{ content[:500] }}{% if content|length > 500 %}...{% endif %}
      ```
      {% endfor %}

      {% if context.findings %}
      ## Architectural Understanding
      {{ context.findings }}
      {% endif %}

      ## Assess Documentation State

      Evaluate:
      1. **README**: Does one exist? Is it accurate and complete?
      2. **Module docstrings**: Do source files have module-level docstrings?
      3. **Function/class docstrings**: Are public interfaces documented?
      4. **Architecture notes**: Is the system design documented anywhere?
      5. **Usage examples**: Can someone figure out how to run/use this project?
      6. **Setup instructions**: Are dependencies and installation documented?

      For each category, rate: missing / incomplete / adequate / good.

      Then list the HIGHEST PRIORITY documentation tasks (max 4), ordered
      by impact on someone trying to understand and use this project.
    config:
      temperature: "t*0.3"
    resolver:
      type: llm_menu
      prompt: "What documentation work is most needed?"
      options:
        write_readme:
          description: "README is missing or inadequate — this is the highest priority"
        update_docstrings:
          description: "Source files lack proper docstrings — improve inline documentation"
        write_architecture:
          description: "System design needs documentation — write architecture notes"
        documentation_adequate:
          description: "Documentation is already adequate — no changes needed"
    publishes:
      - doc_assessment

  write_readme:
    action: inference
    description: "Produce a comprehensive README for the project"
    context:
      required: [project_context, mission_objective, doc_assessment]
      optional: [findings, project_structure]
    prompt: |
      Write a README.md for this Python project.

      ## Project Objective
      {{ context.mission_objective }}

      ## Documentation Assessment
      {{ context.doc_assessment }}

      ## Project Structure
      {% for filepath in context.project_context.keys() %}
      - {{ filepath }}
      {% endfor %}

      ## Project Code (for understanding)
      {% for filepath, content in context.project_context.items() %}
      ### {{ filepath }}
      ```python
      {{ content[:1000] }}{% if content|length > 1000 %}...{% endif %}
      ```
      {% endfor %}

      ## Write the README

      Include:
      - **Project title and description**: what it does, why it exists
      - **Features**: key capabilities
      - **Installation**: how to set up (dependencies, environment)
      - **Usage**: how to run it, with concrete examples
      - **Project structure**: brief description of each file/module's role
      - **Architecture**: high-level design (if the project is complex enough)
      - **Development**: how to run tests, contribute

      Write for someone who has never seen this project before.
      Be concrete — use actual command examples and file paths from the project.
      Don't document aspirational features — only what actually exists in the code.

      Respond with the complete README.md content in ```markdown ``` markers.
    config:
      temperature: "t*0.5"
    effects:
      - write_file:
          path: "README.md"
          content: "{{ result.readme_content }}"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: check_more_docs
    publishes:
      - readme_written

  update_docstrings:
    action: inference
    description: "Add or improve docstrings across the project's source files."
    context:
      required: [project_context, doc_assessment]
      optional: []
    prompt: |
      Add or improve docstrings in this project's Python files.
      Do NOT change any code logic — only add/improve documentation strings.

      {{ context.doc_assessment }}

      ## Files to Document
      {% for filepath, content in context.project_context.items() %}
      {% if filepath.endswith('.py') %}
      ### {{ filepath }}
      ```python
      {{ content }}
      ```
      {% endif %}
      {% endfor %}

      ## Rules
      - Add module-level docstrings if missing
      - Add class and function docstrings if missing (Google style)
      - Include type information in docstrings for parameters and returns
      - Keep docstrings concise but informative
      - Do NOT modify any code logic, only add/update docstring text

      Produce the modified files using the multi-file format:

      === FILE: path/to/file.py ===
      ```python
      [complete file with docstrings added]
      ```
    config:
      temperature: "t*0.3"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: apply_docstrings
    publishes:
      - docstring_changes

  apply_docstrings:
    action: apply_multi_file_changes
    description: "Write the docstring-improved files"
    context:
      required: [docstring_changes]
    resolver:
      type: rule
      rules:
        - condition: "result.all_written == true"
          transition: verify_no_behavior_change
        - condition: "result.all_written == false"
          transition: failed
    publishes:
      - files_changed

  verify_no_behavior_change:
    action: flow
    flow: validate_output
    description: "Verify adding docstrings didn't break anything"
    input_map:
      working_directory: "{{ input.working_directory }}"
      validation_scope: "project"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: check_more_docs
        - condition: "result.status == 'failed'"
          transition: failed
    publishes:
      - validation_results

  write_architecture:
    action: inference
    description: "Produce architecture documentation for the project"
    context:
      required: [project_context, mission_objective, doc_assessment]
      optional: [findings, project_structure]
    prompt: |
      Write an ARCHITECTURE.md for this project that explains the system design.

      ## Project
      {{ context.mission_objective }}

      ## Code
      {% for filepath, content in context.project_context.items() %}
      ### {{ filepath }}
      ```python
      {{ content[:800] }}{% if content|length > 800 %}...{% endif %}
      ```
      {% endfor %}

      ## Write ARCHITECTURE.md

      Include:
      - **Overview**: what the system does at a high level
      - **Component diagram**: describe the main modules and how they interact (text-based)
      - **Data flow**: how data moves through the system
      - **Key design decisions**: why things are structured this way
      - **Extension points**: where new functionality would be added
      - **Dependencies**: external libraries and what they're used for

      Write for a developer joining the project who needs to understand
      the architecture before contributing.

      Respond with the complete ARCHITECTURE.md content in ```markdown ``` markers.
    config:
      temperature: "t*0.5"
    effects:
      - write_file:
          path: "ARCHITECTURE.md"
          content: "{{ result.architecture_content }}"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: check_more_docs
    publishes:
      - architecture_written

  check_more_docs:
    action: check_remaining_doc_tasks
    description: "Are there more documentation tasks from the assessment?"
    context:
      required: [doc_assessment]
      optional: [readme_written, docstring_changes, architecture_written]
    resolver:
      type: rule
      rules:
        - condition: "result.remaining > 0 and meta.attempt < 3"
          transition: assess_documentation_state
        - condition: "true"
          transition: complete
    publishes:
      - docs_completed

  complete:
    action: flow
    flow: capture_learnings
    description: "Record what documentation was produced"
    input_map:
      learning_type: "documentation"
      content: "{{ context.docs_completed }}"
      mission_id: "{{ input.mission_id }}"
    terminal: true
    status: success
    publishes:
      - documentation_summary

  documentation_adequate:
    action: noop
    description: "Documentation reviewed and found adequate"
    terminal: true
    status: success

  failed:
    action: noop
    terminal: true
    status: failed

entry: gather_context

overflow:
  strategy: split
  fallback: reorganize
```

---

### A.6 — `retrospective`

```yaml
flow: retrospective
version: 1
description: >
  Periodic self-assessment of agent performance. Reviews completed work,
  analyzes patterns in successes and failures, evaluates time/effort distribution,
  and produces actionable recommendations for adjusting approach.

input:
  required:
    - mission_id
  optional:
    - task_id
    - working_directory
    - trigger_reason
    - scope

defaults:
  config:
    temperature: "t*0.6"
    max_tokens: 4096

steps:

  gather_history:
    action: load_retrospective_data
    description: >
      Load mission state, completed task records, frustration history,
      captured learnings, and flow artifacts.
    context:
      required: [mission_id]
      optional: [scope, trigger_reason]
    params:
      include_artifacts: true
      include_learnings: true
      include_timing: true
    resolver:
      type: rule
      rules:
        - condition: "result.completed_tasks > 0"
          transition: analyze_patterns
        - condition: "result.completed_tasks == 0"
          transition: too_early
    publishes:
      - mission_history
      - task_outcomes
      - learnings_archive
      - timing_data

  analyze_patterns:
    action: inference
    description: >
      Review the performance record and identify patterns.
    context:
      required: [mission_history, task_outcomes]
      optional: [learnings_archive, timing_data, trigger_reason]
    prompt: |
      You are reviewing your own performance as a developer on this mission.
      Be honest and specific — this analysis is for your own improvement.

      ## Mission
      {{ context.mission_history.objective }}

      ## Task Outcomes
      {% for task in context.task_outcomes %}
      - **{{ task.id }}** ({{ task.flow }}): {{ task.status }}
        Attempts: {{ task.attempts }} | Frustration: {{ task.frustration }}
        {% if task.summary %}Summary: {{ task.summary }}{% endif %}
        {% if task.failure_reason %}Failure: {{ task.failure_reason }}{% endif %}
      {% endfor %}

      {% if context.timing_data %}
      ## Time Distribution
      {% for flow_name, avg_duration in context.timing_data.items() %}
      - {{ flow_name }}: avg {{ avg_duration }}s per invocation
      {% endfor %}
      {% endif %}

      {% if context.learnings_archive %}
      ## Captured Learnings
      {% for learning in context.learnings_archive[-10:] %}
      - [{{ learning.type }}] {{ learning.content[:200] }}
      {% endfor %}
      {% endif %}

      {% if context.trigger_reason %}
      ## Why This Retrospective Was Triggered
      {{ context.trigger_reason }}
      {% endif %}

      ## Analyze

      ### Success Patterns
      What types of tasks consistently succeed? What do they have in common?

      ### Failure Patterns
      What types of tasks fail or require multiple attempts? What's the common thread?

      ### Effort Distribution
      Where is time being spent? Is the ratio of planning/coding/testing/debugging healthy?

      ### Trend Analysis
      Is performance improving, degrading, or flat over the course of the mission?

      ### Blind Spots
      What AREN'T you doing that you should be?

      Be specific. Reference task IDs, file names, and concrete observations.
    config:
      temperature: "t*0.6"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: generate_recommendations
    publishes:
      - performance_analysis

  generate_recommendations:
    action: inference
    description: >
      From the pattern analysis, produce specific, actionable recommendations.
    context:
      required: [performance_analysis, mission_history]
      optional: [task_outcomes, learnings_archive]
    prompt: |
      Based on your performance analysis:

      {{ context.performance_analysis }}

      ## Remaining Work
      {% for task in context.mission_history.plan %}
      {% if task.status == 'pending' %}
      - {{ task.id }}: {{ task.description }}
      {% endif %}
      {% endfor %}

      ## Generate Recommendations

      Produce 3-5 specific, actionable recommendations. For each:

      1. **Recommendation**: what to change
      2. **Based on**: which pattern from the analysis this addresses
      3. **Action type**: one of:
         - `adjust_approach` — change how remaining tasks are executed
         - `add_task` — add a new task to the plan
         - `reprioritize` — change the order of remaining tasks
         - `revise_plan` — fundamentally restructure remaining work
         - `note_for_knowledge_base` — a principle that should persist beyond this mission
      4. **Specifics**: exact details of what to do

      Be concrete. "Write more tests" is too vague. "Add unit tests for the
      validation logic in models/living_data.py that has failed twice during
      modification" is actionable.

      Also assess overall mission health:
      - **On track**: the mission is progressing well
      - **At risk**: patterns suggest the mission may not complete successfully
      - **Needs intervention**: systemic issues require the shop director's attention
    config:
      temperature: "t*0.5"
    resolver:
      type: llm_menu
      prompt: "How should these recommendations be applied?"
      options:
        apply_recommendations:
          description: "Recommendations are actionable — apply them to the mission"
        flag_for_director:
          description: "Issues found that need the shop director's attention"
        no_changes_needed:
          description: "Performance is healthy — no adjustments needed"
    publishes:
      - recommendations
      - mission_health

  apply_recommendations:
    action: apply_retrospective_recommendations
    description: >
      Translate recommendations into mission state changes.
    context:
      required: [recommendations, mission_history]
      optional: [performance_analysis]
    effects:
      - save_mission
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: complete
    publishes:
      - changes_applied

  flag_for_director:
    action: compose_director_report
    description: "Package findings into a report for the shop director."
    context:
      required: [performance_analysis, recommendations, mission_health]
      optional: [mission_history]
    effects:
      - push_event:
          type: "retrospective_report"
          payload: "{{ result.report }}"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: complete
    publishes:
      - director_report

  complete:
    action: flow
    flow: capture_learnings
    description: "Record the retrospective findings"
    input_map:
      learning_type: "retrospective"
      content: "{{ context.performance_analysis }}"
      mission_id: "{{ input.mission_id }}"
    terminal: true
    status: success
    publishes:
      - retrospective_summary
      - mission_health
      - changes_applied

  too_early:
    action: noop
    description: "Not enough completed work to retrospect on"
    terminal: true
    status: success

  no_changes_needed:
    action: flow
    flow: capture_learnings
    description: "Record that retrospective found no issues"
    input_map:
      learning_type: "retrospective"
      content: "Performance review: no adjustments needed"
      mission_id: "{{ input.mission_id }}"
    terminal: true
    status: success

entry: gather_history

overflow:
  strategy: split
  fallback: reorganize
```

---

### A.7 — `request_review`

*Note: This flow requires Phase 6 (escalation) for the `submit_review_to_api` action. The flow can be loaded and validated before Phase 6, but the API submission step will fail until the escalation effect is implemented.*

```yaml
flow: request_review
version: 1
description: >
  Proactively submit completed work for senior dev review. The agent is
  confident in the work but seeks verification, feedback, and learning.

input:
  required:
    - mission_id
    - task_id
    - work_summary
    - files_to_review
  optional:
    - working_directory
    - design_decisions
    - specific_concerns
    - quality_gate_observations

defaults:
  config:
    temperature: "t*0.3"
    max_tokens: 4096

steps:

  gather_review_context:
    action: flow
    flow: prepare_context
    description: "Read the files to be reviewed with full content"
    input_map:
      target_file_path: "{{ input.files_to_review[0] }}"
      working_directory: "{{ input.working_directory }}"
      depth: "full"
      additional_files: "{{ input.files_to_review }}"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: compose_review_request
        - condition: "result.status != 'success'"
          transition: failed
    publishes:
      - review_files

  compose_review_request:
    action: inference
    description: >
      Compose a clear, focused review request.
    context:
      required: [review_files, work_summary]
      optional: [design_decisions, specific_concerns, quality_gate_observations]
    prompt: |
      You completed a task and are requesting a code review from a senior developer.
      Compose a review request that is clear, focused, and respectful of their time.

      ## What You Did
      {{ context.work_summary }}

      {% if context.design_decisions %}
      ## Design Decisions Made
      {{ context.design_decisions }}
      {% endif %}

      {% if context.specific_concerns %}
      ## Areas Where You Want Particular Attention
      {% for concern in context.specific_concerns %}
      - {{ concern }}
      {% endfor %}
      {% endif %}

      {% if context.quality_gate_observations %}
      ## Quality Gate Notes
      {{ context.quality_gate_observations }}
      {% endif %}

      ## Files for Review
      {% for filepath, content in context.review_files.items() %}
      ### {{ filepath }}
      ```python
      {{ content }}
      ```
      {% endfor %}

      ## Compose the Review Request

      Write a concise review request that includes:

      1. **Summary**: 2-3 sentences on what was built and why
      2. **Key decisions**: the most important design choices and your reasoning
      3. **What to focus on**: specific areas where review would be most valuable
      4. **Self-assessment**: your honest evaluation of the work quality

      Be direct. Don't be falsely humble or falsely confident.
    config:
      temperature: "t*0.4"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: submit_review
    publishes:
      - review_request

  submit_review:
    action: submit_review_to_api
    description: "Send the review request to the senior dev via the escalation API."
    context:
      required: [review_request, review_files, work_summary]
      optional: [design_decisions]
    params:
      request_type: "code_review"
      system_prompt_override: |
        You are a senior developer reviewing work from a junior developer on your team.
        They are NOT stuck — they completed the work and are requesting feedback.

        Provide a code review that includes:
        1. Overall assessment: approve / request changes / reject
        2. Specific feedback on code quality, design decisions, and potential issues
        3. Suggestions for improvement (if any)
        4. What was done well (positive reinforcement helps learning)

        Be constructive and specific. Reference line numbers and function names.
        If the work is good, say so clearly — don't manufacture criticism.
    resolver:
      type: rule
      rules:
        - condition: "result.response_received == true"
          transition: process_review_feedback
        - condition: "result.response_received == false"
          transition: review_unavailable
    publishes:
      - review_response

  process_review_feedback:
    action: inference
    description: "Analyze the senior dev's review feedback."
    context:
      required: [review_response, review_files, work_summary]
      optional: [design_decisions]
    prompt: |
      You received this code review from the senior developer:

      {{ context.review_response }}

      ## Analyze the Feedback

      Categorize each piece of feedback:

      1. **Must fix**: Issues that need to be addressed before moving on
      2. **Should fix**: Improvements worth making but not blocking
      3. **Consider for future**: Good advice to remember but not actionable now
      4. **Positive feedback**: What was done well — record for reinforcement

      For each "must fix" and "should fix" item:
      - What specifically needs to change
      - In which file and approximately where
      - How confident are you in implementing the fix

      Also extract any general principles or lessons the senior dev communicated
      that should be remembered for future work.
    config:
      temperature: "t*0.3"
    resolver:
      type: llm_menu
      prompt: "How should the review feedback be handled?"
      options:
        approved:
          description: "Review approved — no changes needed or only minor suggestions"
        changes_needed:
          description: "Changes requested — need to modify code before proceeding"
        major_rework:
          description: "Significant issues found — task needs substantial rework"
    publishes:
      - feedback_analysis
      - required_changes
      - learnings_from_review

  apply_review_changes:
    action: tail_call
    description: "Route back to mission_control for modification tasks."
    context:
      required: [required_changes, review_files]
    tail_call:
      flow: mission_control
      input_map:
        mission_id: "{{ input.mission_id }}"
        last_status: "review_changes_needed"
        last_result:
          review_feedback: "{{ context.feedback_analysis }}"
          required_changes: "{{ context.required_changes }}"
          source_task_id: "{{ input.task_id }}"

  major_rework:
    action: tail_call
    description: "Review found significant issues — re-plan the task."
    context:
      required: [feedback_analysis, review_response]
    tail_call:
      flow: mission_control
      input_map:
        mission_id: "{{ input.mission_id }}"
        last_status: "review_rework_needed"
        last_result:
          review_feedback: "{{ context.feedback_analysis }}"
          rework_reason: "{{ context.review_response }}"
          source_task_id: "{{ input.task_id }}"

  approved:
    action: flow
    flow: capture_learnings
    description: "Record review feedback and learnings"
    input_map:
      learning_type: "code_review"
      content: "{{ context.feedback_analysis }}"
      mission_id: "{{ input.mission_id }}"
    terminal: true
    status: success
    publishes:
      - review_outcome
      - learnings_from_review

  review_unavailable:
    action: noop
    description: "Senior dev review not available — proceed without review"
    terminal: true
    status: success
    publishes:
      - review_outcome

  failed:
    action: noop
    terminal: true
    status: failed

entry: gather_review_context

overflow:
  strategy: split
  fallback: reorganize
```

---

### A.8 — `research_context` v2 (Dispatcher)

```yaml
flow: research_context
version: 2
description: >
  Routes research queries to specialized sub-flows based on the nature
  of the question. Acts as an abstraction barrier — callers request
  research, this flow determines how to conduct it.

input:
  required:
    - research_query
  optional:
    - working_directory
    - research_type_hint
    - source_preferences
    - max_results

defaults:
  config:
    temperature: "t*0.3"
    max_tokens: 2048

steps:

  classify_query:
    action: inference
    description: "Determine what type of research this query requires."
    context:
      required: [research_query]
      optional: [research_type_hint, source_preferences]
    prompt: |
      Classify this research query to determine the best research strategy:

      Query: {{ context.research_query }}

      {% if context.research_type_hint %}
      Suggested type: {{ context.research_type_hint }}
      {% endif %}

      Categories:
      1. **web_search**: General information, current events, API documentation,
         library usage, error messages, Stack Overflow-type questions
      2. **local_library**: Questions answerable from reference books, design patterns,
         established principles, software engineering concepts
      3. **codebase_history**: Understanding why code is the way it is, what changed
         recently, who changed what, evolution of a module over time
      4. **technical_literature**: Algorithms, data structures, academic concepts,
         formal specifications, mathematical foundations

      Which category best fits this query?
    config:
      temperature: "t*0.2"
    resolver:
      type: llm_menu
      prompt: "What type of research is needed?"
      options:
        web_search:
          description: "General web search"
        local_library:
          description: "Consult reference material"
        codebase_history:
          description: "Trace code history"
        technical_literature:
          description: "Research algorithms or formal concepts"
    publishes:
      - query_classification

  web_search:
    action: web_search
    description: "Search the web"
    context:
      required: [research_query]
      optional: [max_results]
    params:
      provider: "duckduckgo"
      max_results: "{{ input.max_results or 5 }}"
    resolver:
      type: rule
      rules:
        - condition: "result.results_found > 0"
          transition: synthesize_results
        - condition: "result.results_found == 0"
          transition: no_results
    publishes:
      - raw_results

  local_library:
    action: flow
    flow: research_local_library
    description: "Query local reference materials via RAG"
    input_map:
      query: "{{ context.research_query }}"
      source_preferences: "{{ input.source_preferences }}"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: synthesize_results
        - condition: "result.status != 'success'"
          transition: web_search
    publishes:
      - raw_results

  codebase_history:
    action: flow
    flow: research_codebase_history
    description: "Investigate code history via git"
    input_map:
      query: "{{ context.research_query }}"
      working_directory: "{{ input.working_directory }}"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: synthesize_results
        - condition: "result.status != 'success'"
          transition: no_results
    publishes:
      - raw_results

  technical_literature:
    action: flow
    flow: research_technical
    description: "Search technical/scholarly sources"
    input_map:
      query: "{{ context.research_query }}"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: synthesize_results
        - condition: "result.status != 'success'"
          transition: web_search
    publishes:
      - raw_results

  synthesize_results:
    action: inference
    description: "Synthesize raw research results into an actionable answer"
    context:
      required: [research_query, raw_results]
      optional: [query_classification]
    prompt: |
      You researched this question:
      {{ context.research_query }}

      ## Raw Results
      {{ context.raw_results }}

      ## Synthesize

      Produce a concise, actionable answer:
      1. **Direct answer**: address the research query specifically
      2. **Key findings**: the most relevant information discovered
      3. **Sources**: where the information came from
      4. **Confidence**: how reliable is this information
      5. **Caveats**: anything the reader should be cautious about
    config:
      temperature: "t*0.3"
    terminal: true
    status: success
    publishes:
      - research_findings

  no_results:
    action: noop
    terminal: true
    status: failed
    publishes:
      - research_findings

entry: classify_query

overflow:
  strategy: summarize
  fallback: reorganize
```

---

### A.9 — `research_codebase_history`

```yaml
flow: research_codebase_history
version: 1
description: >
  Investigate the history of code changes using git.

input:
  required:
    - query
  optional:
    - working_directory
    - target_file
    - time_range

steps:

  determine_git_commands:
    action: inference
    description: "Determine which git commands will answer the question."
    context:
      required: [query]
      optional: [target_file, time_range]
    prompt: |
      You need to investigate code history to answer this question:
      {{ context.query }}

      {% if context.target_file %}Target file: {{ context.target_file }}{% endif %}
      {% if context.time_range %}Time range: {{ context.time_range }}{% endif %}

      Which git commands would help? Choose from:
      - `git log --oneline -20 [file]`
      - `git log --oneline --since="2 weeks ago"`
      - `git blame [file]`
      - `git diff HEAD~5 -- [file]`
      - `git log --all --oneline --grep="keyword"`

      List 1-3 commands, most useful first.
    config:
      temperature: "t*0.2"
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: execute_git_commands
    publishes:
      - git_commands

  execute_git_commands:
    action: run_git_investigation
    description: "Execute the planned git commands and collect output"
    context:
      required: [git_commands]
    params:
      working_directory: "{{ input.working_directory }}"
      max_output_lines: 100
    resolver:
      type: rule
      rules:
        - condition: "result.any_output == true"
          transition: analyze_history
        - condition: "result.any_output == false"
          transition: no_history
    publishes:
      - git_output

  analyze_history:
    action: inference
    description: "Synthesize git output into an answer"
    context:
      required: [query, git_output]
    prompt: |
      You investigated git history to answer: {{ context.query }}

      ## Git Output
      {{ context.git_output }}

      Synthesize what the history tells you. Focus on:
      - What changed and when
      - Why it changed (from commit messages)
      - Patterns in the evolution
      - Relevant design decisions visible in the history
    config:
      temperature: "t*0.3"
    terminal: true
    status: success
    publishes:
      - raw_results

  no_history:
    action: noop
    description: "No git history available"
    terminal: true
    status: failed

entry: determine_git_commands
```

---

### A.10 — `research_local_library` (Stub)

*Requires RAG infrastructure. Designed interface only — action implementations are stubs.*

```yaml
flow: research_local_library
version: 1
description: >
  Query a local collection of reference materials using RAG.

input:
  required:
    - query
  optional:
    - source_preferences
    - max_passages

steps:

  check_library_available:
    action: check_rag_index
    description: "Verify the local library index exists and is queryable"
    resolver:
      type: rule
      rules:
        - condition: "result.available == true"
          transition: retrieve_passages
        - condition: "result.available == false"
          transition: library_unavailable
    publishes:
      - library_status

  retrieve_passages:
    action: rag_retrieve
    description: "Embed the query and retrieve relevant passages"
    context:
      required: [query]
      optional: [source_preferences, max_passages]
    params:
      index_path: ".agent/library/index"
      max_passages: "{{ input.max_passages or 3 }}"
      min_relevance: 0.7
      source_boost: "{{ input.source_preferences }}"
    resolver:
      type: rule
      rules:
        - condition: "result.passages_found > 0"
          transition: format_results
        - condition: "result.passages_found == 0"
          transition: library_unavailable
    publishes:
      - retrieved_passages

  format_results:
    action: format_rag_results
    description: "Format retrieved passages with source attribution"
    context:
      required: [retrieved_passages, query]
    terminal: true
    status: success
    publishes:
      - raw_results

  library_unavailable:
    action: noop
    terminal: true
    status: failed

entry: check_library_available
```

---

### A.11 — `research_technical`

```yaml
flow: research_technical
version: 1
description: >
  Research technical concepts from authoritative sources.

input:
  required:
    - query
  optional:
    - domain_hint

steps:

  search_technical_sources:
    action: web_search
    description: "Search with technical source bias"
    context:
      required: [query]
      optional: [domain_hint]
    params:
      provider: "duckduckgo"
      query_modifier: "{{ context.query }} site:docs.python.org OR site:arxiv.org OR site:en.wikipedia.org OR site:realpython.com"
      max_results: 5
    resolver:
      type: rule
      rules:
        - condition: "result.results_found > 0"
          transition: filter_and_format
        - condition: "result.results_found == 0"
          transition: no_results
    publishes:
      - raw_search_results

  filter_and_format:
    action: inference
    description: "Filter search results for technical quality and relevance"
    context:
      required: [query, raw_search_results]
      optional: [domain_hint]
    prompt: |
      You searched for technical information on: {{ context.query }}

      ## Search Results
      {{ context.raw_search_results }}

      Filter these results:
      1. Keep only results from authoritative technical sources
      2. Discard blog posts, forums, and opinion pieces unless they contain
         concrete technical content
      3. Prioritize: official documentation > academic papers > authoritative tutorials
      4. Summarize each kept result's relevant content

      Produce a focused technical answer to the query.
    config:
      temperature: "t*0.2"
    terminal: true
    status: success
    publishes:
      - raw_results

  no_results:
    action: noop
    terminal: true
    status: failed

entry: search_technical_sources
```