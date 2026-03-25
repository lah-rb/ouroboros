# Ouroboros — Architecture & Implementation Guide

*A flow-driven autonomous coding agent backed by LLMVP local inference.*

*This document is the authoritative architectural reference for Ouroboros. It describes
what the system IS — its components, contracts, data flows, and design decisions. For
operational guidance (how to develop, test, and run), see `AGENT.md`. For contributor
patterns (how to add new flows, actions, etc.), see `CONTRIBUTING.md`.*

---

## 1. Project Identity & Relationship to LLMVP

### 1.1 What Ouroboros Is

Ouroboros is an autonomous, flow-driven coding agent. It operates continuously with minimal
human supervision, executing missions (user-defined objectives) by breaking them into tasks,
selecting and executing appropriate workflows, and managing escalation when it gets stuck.

### 1.2 The Programming Shop Metaphor

The system is modeled after a programming shop with three roles:

- **Shop Director (the user):** Sets missions, checks in periodically, adjusts direction.
  Interacts through CLI commands and mission configuration.
- **Senior Developer (external API — design pending):** Consulted when the junior dev gets
  stuck. Provides code reviews, architectural guidance, direct fixes, and decisions. Expensive
  per interaction — used sparingly. *Note: escalation integration is a future phase. The base
  agent must prove capable before designing the interaction model.*
- **Junior Developer (local model via LLMVP):** Does the actual work. Runs continuously for
  near-zero cost. Follows established flows, makes tactical decisions, and knows when to ask
  for help.

### 1.3 Separation from LLMVP

**Ouroboros is a pure GraphQL client of LLMVP.** It is a separate project with its own
repository, its own `pyproject.toml`, its own CLI entry point. It does not import any Python
modules from LLMVP. All inference requests go through LLMVP's GraphQL API over HTTP.

This separation means:
- Ouroboros can run as a separate process, potentially on a different machine.
- LLMVP doesn't know its client is an autonomous agent — it just serves inference requests.
- The inference backend could be swapped for any GraphQL-compatible server without changing Ouroboros.
- Each project maintains independent development velocity and release cycles.

### 1.4 Target Hardware & Model

Primary target: Apple Silicon M1 Ultra with 128 GB unified memory, running Qwen 3.5 122B
(A10B active, sparse MoE). This model provides:

- High coding competence (top-ranked on HuggingFace arena for code)
- 1 response at ~35 tokens/sec, or up to 8 parallel agents at ~5 tokens/sec each
- 2-3 pool instances for the standard operating mode (triage + surgeon patterns)
- Near-zero electricity cost for continuous 24/7 operation

### 1.5 Multi-Project Future

The architecture supports running multiple missions on separate projects simultaneously,
sharing the LLMVP inference pool. Each mission has its own working directory, its own
`.agent/` state, and its own effects profile. This is a future capability that the
architecture accommodates without redesign.

---

## 2. Core Architecture

### 2.1 The Flow Engine

The flow engine is the backbone of Ouroboros. Everything the agent does is expressed as a
flow — a directed graph of steps with typed inputs, typed outputs, and explicit transition
logic.

#### 2.1.1 Design Principles

**Functional model.** Every step is a pure function: immutable input in, immutable output
out. Side effects (file I/O, inference calls, API requests) happen through an effects
interface that the step receives but does not own. This gives reproducibility (same input →
same output), composability (steps don't know about each other), serializability (every
state transition is a logged event), and testability (swap the effects interface for mocks).

**Declarative flow definitions.** Flows are defined as YAML data files, not Python code.
The graph structure (steps, transitions, context requirements) is pure data. Actions (what a
step does) are registered Python callables referenced by name. This separates "what is the
procedure" from "what does this step actually do," making flows inspectable, serializable,
and eventually authorable by the agent itself.

**Pluggable transition resolution.** The flow engine doesn't decide how transitions work —
the resolver does. Different steps can use different resolver types (rule-based, LLM-driven)
within the same flow. The engine just calls the resolver and follows the result.

**Extensibility as a core requirement.** The engine makes no assumptions about constraint
level, resolver types, or action types. New resolvers, new action types, and new effects can
be added without modifying the engine.

#### 2.1.2 Flow Definition Format

Flows are YAML files with the following top-level structure:

```yaml
flow: <name>                    # Unique flow identifier
version: <int>                  # Schema version
description: <string>           # Human-readable purpose

input:
  required: [<key>, ...]        # Flow cannot execute without these
  optional: [<key>, ...]        # Enriches execution but not required

defaults:
  config:
    temperature: <float|str>    # Absolute (0.2) or relative ("t*0.5")
    max_tokens: <int>

steps:
  <step_name>:                  # Map of step definitions (see §2.1.3)
    ...

entry: <step_name>              # Where execution begins

overflow:
  strategy: split               # Context overflow handling
  fallback: reorganize
```

#### 2.1.3 Step Definition Elements

Each step in the `steps` map has:

- **`action`**: Reference to a registered action callable, or special values: `inference`
  (inference call), `flow` (sub-flow invocation), `noop` (pass-through for routing).
- **`description`**: Human-readable purpose.
- **`context.required` / `context.optional`**: Which keys from the context accumulator
  this step needs. The runtime filters the accumulator to only these keys before invoking
  the action. Required keys are validated — missing keys cause a runtime error.
- **`params`**: Static parameters for the action. Supports `{{ }}` Jinja2 template
  interpolation against flow inputs and context.
- **`prompt`**: For `action: inference` steps — the Jinja2 template rendered against
  context and passed to the model. See `PROMPTING_CONVENTIONS.md` for standards.
- **`config`**: Generation parameter overrides. Merged with flow-level defaults. Supports
  both absolute values (`temperature: 0.1`) and relative values (`temperature: "t*0.5"`).
- **`resolver`**: How the next step is chosen. See §2.1.4.
- **`publishes`**: List of context keys this step adds to the accumulator.
- **`effects`**: Declared side effects the step performs.
- **`terminal`**: If true, this step ends the flow. Must include a `status` value.
- **`tail_call`**: If present on a terminal step, triggers a tail call instead of
  returning to caller. See §2.3.

#### 2.1.4 Transition Resolvers

**Rule-based resolver (`type: rule`):** Evaluates conditions against the step's output,
the context accumulator, and execution metadata. No inference call needed. Conditions are
Python expressions evaluated with restricted `eval()` — the context dict is the only
namespace, no builtins. Rules evaluated in order; first match wins.

```yaml
resolver:
  type: rule
  rules:
    - condition: "result.file_found == true"
      transition: plan_change
    - condition: "result.file_found == false"
      transition: escalate
```

**LLM menu resolver (`type: llm_menu`):** Presents the model with a constrained set of
named options, each with a description. The model picks one. Costs one inference call.

```yaml
resolver:
  type: llm_menu
  prompt: "Given your analysis, what should happen next?"
  options:
    execute_change:
      description: "High confidence — proceed with the change"
    gather_more_context:
      description: "Need more information before committing"
      target: gather_context
    abandon:
      description: "This approach won't work"
      terminal: true
      status: abandoned
```

### 2.2 Context Model

The context accumulator is the shared data store for a flow execution. It follows a
publish/subscribe pattern with explicit declarations:

- Steps declare what they **consume** (`context.required`, `context.optional`).
- Steps declare what they **produce** (`publishes`).
- The runtime validates required keys are present before executing a step.
- Each step only sees the keys it declared — no ambient access to the full accumulator.

This per-step context scoping is architecturally critical for local models with smaller
context windows. It keeps prompts focused and prevents context bloat that degrades model
performance.

**Context overflow strategy:** split → reorganize → summarize. Truncation is never used
because it causes unpredictable model degradation. Split preserves everything by dividing
work. Reorganize lets the parent flow reassess scope. Summarize is lossy but coherent.

### 2.3 Flow Composition

#### Tail Calls

Tail calls are how flows chain without nesting. A terminal step can include a `tail_call`
block that specifies the next flow to execute and what inputs to pass:

```yaml
dispatch:
  action: noop
  tail_call:
    flow: "{{ context.dispatch_config.flow }}"
    input_map:
      mission_id: "{{ input.mission_id }}"
      task_id: "{{ context.dispatch_config.task_id }}"
```

The agent's continuous operation emerges from this: `mission_control` dispatches a task
flow → task flow completes and tail-calls back to `mission_control` → `mission_control`
dispatches the next task. There is no external loop managing this cycle — it's entirely
expressed in the flow graph.

#### Sub-flows

Steps with `action: flow` invoke a child flow synchronously. The child runs to completion
and its result is available to the parent's resolver. Sub-flows are black boxes — the
parent doesn't know or care about the child's internal steps. This is the same contract
as a function call.

### 2.4 Effects Interface

All side effects go through a swappable protocol. Actions never directly touch filesystem,
network, or subprocess.

**Protocol methods (current):**
- `read_file(path)`, `write_file(path, content)`, `list_directory(path)`
- `run_command(cmd, cwd)` — subprocess execution
- `run_inference(prompt, config)` — LLMVP GraphQL call
- `load_mission(id)`, `save_mission(state)` — persistence
- `push_event(event)`, `read_events()` — event queue

**Implementations:**
- `LocalEffects` — real filesystem, real subprocess, real inference, real persistence.
  Production use.
- `MockEffects` — canned responses, call recording. Testing use.
- `DryRunEffects` — reads real, writes logged. Planned.
- `GitManagedEffects` — auto-branching, auto-commit, rollback. Planned.

### 2.5 The Agent Cycle — mission_control

`mission_control.yaml` is the hub flow that orchestrates the entire agent lifecycle:

1. **Load state** — read mission state and event queue from persistence.
2. **Apply last result** — integrate the previous flow's outcome (task completion,
   abandonment, frustration updates).
3. **Check retrospective** — trigger periodic retrospective every 5 completed tasks,
   when frustration exceeds thresholds, or when multiple tasks are blocked/failed.
4. **Process events** — handle user messages, abort/pause signals.
5. **Check extension** — evaluate whether the mission plan needs additional tasks
   based on progress and objective coverage.
6. **Assess** — determine what to work on next. Find the highest-priority unblocked task.
7. **Quality gate** — when all tasks complete, run project-wide quality check before
   declaring mission complete. Up to 2 retry cycles on failure.
8. **Dispatch** — configure inputs for the selected task and tail-call to its flow.

All child task flows tail-call back to `mission_control` on completion, creating the
continuous cycle.

### 2.6 Frustration System

The frustration system is a per-task counter that gates escalation permissions. It prevents
the agent from immediately reaching for expensive solutions and forces cheap retries first.

**Current behavior:**
- Each task has a frustration counter that increments on failure/retry.
- `configure_task_dispatch` checks frustration against thresholds defined in its params:
  - `review: 2` — at frustration 2+, eligible for review escalation.
  - `instructions: 4` — at frustration 4+, eligible for instructions escalation.
  - `direct_fix: 5` — at frustration 5+, eligible for direct fix escalation.
- Temperature perturbation: at frustration 2+, the temperature is offset by 0.15-0.4 to
  encourage different model outputs.
- Research injection: at frustration 3+, additional research context is gathered.

**Escalation integration is pending** — the thresholds exist but no external API call is
wired. The frustration system currently influences dispatch configuration (temperature,
research) but does not escalate to an external model. See §4 Future Directions.

### 2.7 Persistence

Mission state, event queues, and flow artifacts are file-backed JSON in `.agent/`:

```
.agent/
├── mission.json          # MissionState: objective, plan, config, notes
├── events.json           # Event queue for user messages, signals
├── history/              # Completed flow artifacts
└── repo_map.json         # Cached AST-based repository map
```

- **Atomic writes** via temp file + rename. No partial state on crash.
- **Single-threaded access** guaranteed by the tail-call execution model.
- **Event queue** uses `fcntl.flock` for safe concurrent access (CLI → agent).

### 2.8 Step Templates

Reusable step configurations defined in `flows/shared/step_templates.yaml`. Templates
provide default action, params, config, and resolver settings that individual flow steps
inherit and can override. The loader merges templates at load time — the runtime sees
fully resolved step definitions.

### 2.9 AST-Based Repository Map

`agent/repomap.py` provides structural code awareness via tree-sitter AST parsing:

- Extracts function/class definitions and their references across files.
- Builds a networkx graph with PageRank ranking to surface the most important files.
- Token-budgeted map formatting for inclusion in prompts without context bloat.
- Cached in `.agent/repo_map.json` with mtime-based invalidation.
- Falls back to regex extraction for unsupported languages.

---

## 3. Flow Inventory

### 3.1 Task Flows (`flows/tasks/`)

| Flow | Purpose |
|------|---------|
| `create_file` | Create a new file from a task description |
| `create_tests` | Generate test files for existing code |
| `design_architecture` | Produce architectural design for a component |
| `diagnose_issue` | Investigate and diagnose a failing test or error |
| `document_project` | Generate project documentation |
| `explore_spike` | Exploratory investigation of an approach |
| `integrate_modules` | Wire modules together and verify integration |
| `manage_packages` | Add/remove/update package dependencies |
| `modify_file` | Modify an existing file to address a specific issue |
| `refactor` | Restructure code without changing behavior |
| `request_review` | Request review of completed work |
| `retrospective` | Periodic analysis of mission progress and patterns |
| `setup_project` | Initialize project structure and boilerplate |
| `validate_behavior` | Verify that implemented behavior matches requirements |

### 3.2 Shared Sub-flows (`flows/shared/`)

| Flow | Purpose |
|------|---------|
| `capture_learnings` | Extract and persist observations from task execution |
| `prepare_context` | Gather files, repo map, and notes for a task |
| `quality_gate` | Project-wide validation before mission completion |
| `research_codebase_history` | Investigate version history for context |
| `research_context` | Broad research gathering for under-informed tasks |
| `research_repomap` | Generate/refresh the AST-based repository map |
| `research_technical` | Look up technical approaches and patterns |
| `revise_plan` | Add or modify tasks in the mission plan |
| `run_in_terminal` | Execute shell commands with output capture |
| `step_templates` | Reusable step configuration templates |
| `validate_output` | Per-file validation (syntax, lint, import check) |

### 3.3 Control Flows (`flows/`)

| Flow | Purpose |
|------|---------|
| `mission_control` | Top-level agent routing and orchestration hub |
| `create_plan` | Generate initial mission plan from objective |
| `registry` | Flow registry with metadata and categorization |

---

## 4. Future Directions

These are design areas that have been identified but not yet implemented. Each requires
a dedicated design phase before implementation. They are listed here for architectural
awareness, not as commitments.

### 4.1 Escalation Integration

The escalation protocol — how the local agent consults an external, more capable model —
is the largest open design question. Several interaction models are under consideration:

- **API calls**: Direct Claude API calls with structured escalation bundles.
- **Cline CLI integration**: Leverage Cline's existing interface for focused fixes.
- **Claude Code as provider**: Use Claude Code with subscription rate limiting as a
  safety guard while Claude runs Ouroboros tasks.
- **Focused prompting**: Claude Code fixes specific issues with tightly scoped prompts.

The right answer depends on observed failure modes from real missions. The runtime tracing
system (Phase 2) will provide the data needed to make this design decision. Key questions:
where does the local model actually get stuck, what's the pattern of those failures, and
how much context needs to be conveyed to a senior model?

The escalation bundle format and response model from the original design (see Appendix A)
remain a reasonable starting point but should be reassessed against real trace data.

### 4.2 Parallel Execution

When `mission_control` identifies multiple independent tasks and pool capacity is available,
it could dispatch them simultaneously. The architecture supports this without redesign — each
flow execution is independent with its own context accumulator. Key considerations:

- Pool headroom reservation (don't use all instances for parallel tasks).
- LLM-based safety check for task independence (two "independent" tasks may modify the
  same file).
- Fan-out/fan-in in the runtime for sub-step parallelism.

### 4.3 Self-Modification Lifecycle

When Ouroboros modifies its own codebase, a strict lifecycle flow applies: duplicate →
modify → validate → promote/rollback → stress test → archive. This is implemented as a
dedicated flow, not baked into the effects interface. Safety comes from procedure.

### 4.4 WASM-Based Execution Sandbox

The effects interface's `run_command` currently executes real subprocesses with path scoping
as the only safety mechanism. A WASM sandbox would provide proper process isolation. Deferred
until the agent begins working on real external projects.

### 4.5 Prompt Profiles & Effectiveness Tracking

Track which prompt formulations work best based on empirical data. Prompt tiers
(default → escalated), success/fail tracking per model-prompt combination. Deferred until
sufficient execution history exists for data-driven decisions.

---

## 5. Development Phases

### Completed Phases (Development History)

**Phases 1-5** built the core system from scratch:

| Phase | Delivered |
|-------|-----------|
| 1 — Flow Engine Core | `models.py`, `loader.py`, `runtime.py`, `resolvers/rule.py`, `actions/registry.py`. YAML flow loading, step execution, rule-based transitions, context accumulator. |
| 2 — Effects Interface | `effects/protocol.py`, `effects/local.py`, `effects/mock.py`. Swappable side-effect protocol. Actions decoupled from direct I/O. |
| 3 — Inference Integration | `effects/inference.py`, `resolvers/llm_menu.py`, `template.py`. LLMVP GraphQL inference, Jinja2 prompt rendering, LLM menu resolvers. |
| 4 — Persistence | `persistence/models.py`, `persistence/manager.py`, `persistence/migrations.py`. Mission state, event queue, artifact storage. CLI commands. |
| 5 — Tail Calls & mission_control | `loop.py`, `tail_call.py`, `mission_control.yaml`. Full agent cycle with continuous tail-call operation. |

**Post-Phase 5 additions** (implemented but not part of original roadmap):
- 14 task flows and 11 shared sub-flows (full flow inventory in §3)
- Frustration system with threshold-gated dispatch configuration
- Quality gate with retry cycles
- Plan revision and extension checking
- Retrospective flow with periodic triggers
- AST-based repo map with tree-sitter and PageRank
- Flow visualizer with Mermaid/Graphviz output
- Mission YAML config with lifecycle commands (`pre_create`/`post_create`)
- Step templates for reusable step configuration

### Active Phases

**Phase 1 — Blueprint System (Static Analysis & Documentation)** ✅

Deliverable: `ouroboros.py blueprint` command producing a comprehensive plan set in both
Markdown (for AI developer context) and PDF via WeasyPrint (for human architectural review).

See `BLUEPRINT_DESIGN.md` for full specification including:
- Intermediate representation (IR) schema
- Sheet structure (cover, system context, lifecycle, flow catalog, context dictionary,
  sub-flow details, action registry)
- Custom symbology set with Egyptian hieroglyphic effect symbols
- Analyzer, renderer, and CLI integration design

**Phase 2 — Runtime Tracing (Default Level)** ✅

Deliverable: Lightweight always-on trace instrumentation producing structured trace events
for post-run analysis. Token counting (whitespace-split), wall-clock timing, resolver
decisions, flow/step lifecycle events.

Implementation:
- `agent/trace.py` — TraceEvent dataclass family (8 event types: CycleStart, CycleEnd,
  StepStart, StepEnd, InferenceCall, FlowInvoke, FlowReturn, base TraceEvent).
  `count_tokens()` for client-side whitespace-split approximation.
- Effects protocol extension — `emit_trace()` and `flush_traces()` added to protocol,
  `LocalEffects` (buffer + JSONL flush), and `MockEffects` (public list for assertions).
- Instrumentation in `runtime.py` — StepStart/StepEnd around every step, InferenceCall
  around inference actions, FlowInvoke/FlowReturn around sub-flow invocations.
- Instrumentation in `loop.py` — CycleStart/CycleEnd at cycle boundaries, flush_traces
  before following tail calls. `_trace_cycle` synthetic input for cycle propagation.
- Instrumentation in `resolvers/llm_menu.py` — InferenceCall with purpose="llm_menu_resolve".
- `agent/trace_cli.py` — `ouroboros.py trace` command with summary and detail formats.
  Summary shows flow breakdown, token totals, resolver decisions, and audit warnings.
- JSONL trace files in `.agent/traces/{mission_id}_{timestamp}.jsonl`.
- Tests in `tests/test_trace.py` — 22 tests covering serialization, token counting,
  MockEffects tracing, LocalEffects flush to disk, and runtime instrumentation.

**Phase 3+ — To Be Determined After Phase 2**

Candidates (to be prioritized based on Phase 2 trace data):
- Frustration system enhancement with path-history-aware resolvers
- Escalation integration design
- Full context debugging traces (LLMVP chain-of-thought extraction)
- GitManagedEffects
- Parallel execution

---

## Appendix A: Escalation Bundle Format (Original Design)

*Retained for reference. To be reassessed against real trace data before implementation.*

```python
class EscalationBundle(BaseModel):
    # What are you working on?
    mission_objective: str
    current_task: str

    # What did you try?
    files: dict[str, str]         # filename → content (relevant files only)
    plan: str | None
    actions_taken: list[str]       # from effects log

    # What went wrong?
    problem_type: Literal["review", "diagnosis", "capability", "ambiguity"]
    description: str
    error_output: str | None
    attempts: int

    # What do you need from me?
    request_type: Literal["instructions", "direct_fix", "decision", "code_review"]
    specific_question: str | None
    files_to_return: list[str]
```

```python
class EscalationResponse(BaseModel):
    type: Literal["instructions", "direct_fix", "decision", "approval", "rejection"]
    content: str
    files: dict[str, str] | None
    reasoning: str | None
    follow_up: str | None
```

## Appendix B: Design Decisions Log

| Decision | Choice | Rationale |
|---|---|---|
| Flow model | Functional (pure data in/out, effects behind interface) | Reproducibility, composability, testability. Side effects isolated and swappable. |
| Flow definitions | Declarative YAML with code hooks via action registry | Separates procedure (data) from behavior (code). Flows inspectable, serializable, eventually agent-authorable. |
| Composition model | Black box sub-flows | Clean boundaries. Child internals can change without breaking parents. Same contract as function calls. |
| Agent loop | Tail-call via mission_control flow (no external loop) | Uniform execution model. No special runtime. State guaranteed valid by flow contracts. |
| Transition resolution | Pluggable resolvers per step | Different steps need different constraint levels. Rule-based for mechanical, LLM-driven for judgment. |
| Context overflow | Split → reorganize → summarize (no truncation) | Truncation causes unpredictable model degradation. |
| Escalation gating | Frustration counter with thresholds | Cost-escalation ladder. Cheap solutions first, expensive tools only after repeated failure. |
| Persistence | File-backed JSON in .agent/ | Single-threaded access. No relational needs. Agent can read own state. Atomic writes. |
| LLMVP relationship | Pure GraphQL client | Clean separation. Swappable backends. LLMVP unaware of agent. |
| Control paradigm | Flow-as-controller (not LLM-as-controller) | Local models lack reasoning bandwidth for full ReAct loops. Structured workflows maximize quality. |
| Context awareness | AST repo map with PageRank | Structural awareness without full-file token cost. |
| Blueprint symbology | Egyptian hieroglyphs for effects + Unicode geometric for data flow | Visually distinct, printable, unique project identity. Consistent across all documentation. |
| Runtime tracing | Effects interface extension (emit_trace/flush_traces) | Follows existing convention — all side effects through effects protocol. MockEffects captures for test assertions. |
| Token counting | Client-side whitespace split | No LLMVP changes needed. Precise enough for context bloat/starvation detection. |
