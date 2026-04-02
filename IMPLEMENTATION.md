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

**Declarative flow definitions.** Flows are defined as CUE data files in `flows/cue/`,
compiled to JSON via `cue export` (`flows/compiled.json`). The graph structure (steps,
transitions, context requirements) is pure data with full type validation at the CUE layer.
Actions (what a step does) are registered Python callables referenced by name. This separates
"what is the procedure" from "what does this step actually do," making flows inspectable,
serializable, and eventually authorable by the agent itself.

**Pluggable transition resolution.** The flow engine doesn't decide how transitions work —
the resolver does. Different steps can use different resolver types (rule-based, LLM-driven)
within the same flow. The engine just calls the resolver and follows the result.

**Extensibility as a core requirement.** The engine makes no assumptions about constraint
level, resolver types, or action types. New resolvers, new action types, and new effects can
be added without modifying the engine.

#### 2.1.2 Flow Definition Format

Flows are CUE files in `flows/cue/` conforming to the `#FlowDefinition` schema defined in
`flows/cue/flow.cue`. The build pipeline is: `.cue` → `cue export --out json` →
`flows/compiled.json` → Python loader (`loader_v2.py`) resolves `$ref` values and assembles
prompts at runtime.

```cue
flow_name: #FlowDefinition & {
    flow:        "flow_name"
    version:     <int>
    description: "<string>"

    input: {
        required: ["<key>", ...]
        optional: ["<key>", ...]
    }

    defaults: config: temperature: <float|"t*N">

    steps: {
        <step_name>: #StepDefinition & {
            ...                          // See §2.1.3
        }
    }

    entry: "<step_name>"

    overflow: {
        strategy: "split"
        fallback: "reorganize"
    }
}
```

Structural values that depend on runtime data use **typed `$ref` references** instead of
string interpolation:

```cue
// Simple reference
mission_id: {$ref: "input.mission_id"}

// Reference with default
mode: {$ref: "input.mode", default: "fix"}

// Fallback chain — first non-null wins
observation: {$ref: "context.director_analysis", fallback: [
    {$ref: "context.dispatch_warning"},
    "Plan revision needed",
]}
```

The `$ref` system replaces Jinja2 `{{ }}` syntax in all structural fields (params,
input_map, tail_call). CUE validates structure and types; the Python runtime
(`loader_v2.py`) resolves references against live input/context/meta namespaces at
execution time.

#### 2.1.3 Step Definition Elements

Each step in the `steps` map has:

- **`action`**: Reference to a registered action callable, or special values: `inference`
  (inference call), `flow` (sub-flow invocation), `noop` (pass-through for routing).
- **`description`**: Human-readable purpose.
- **`context.required` / `context.optional`**: Which keys from the context accumulator
  this step needs. The runtime filters the accumulator to only these keys before invoking
  the action. Required keys are validated — missing keys cause a runtime error.
- **`params`**: Static parameters for the action. Values are literals or `$ref` references
  resolved at runtime against input/context/meta namespaces.
- **`prompt_template`**: For `action: inference` steps — a reference to an external
  section-based YAML prompt template in `prompts/<flow>/<step>.yaml`. Declares which
  `context_keys` and `input_keys` the template will reference (validated by the linter).
  See `PROMPTING_CONVENTIONS.md` for template standards.
- **`pre_compute`**: List of registered Python formatter functions that run before template
  rendering. Each formatter reads from input/context, produces a string, and injects it as
  a context key. Defined in `agent/formatters.py`.
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

```cue
resolver: {
    type: "rule"
    rules: [
        {condition: "result.file_found == true", transition: "plan_change"},
        {condition: "result.file_found == false", transition: "escalate"},
    ]
}
```

**LLM menu resolver (`type: llm_menu`):** Presents the model with a constrained set of
named options, each with a description. The model picks one. Costs one inference call.
Supports `publish_selection` to publish the selected option key to context as data,
enabling a single downstream step to read the selection rather than needing N transition
targets.

```cue
resolver: {
    type: "llm_menu"
    prompt: "Given your analysis, what should happen next?"
    options: {
        execute_change: {
            description: "High confidence — proceed with the change"
        }
        gather_more_context: {
            description: "Need more information before committing"
            target:      "gather_context"
        }
        abandon: {
            description: "This approach won't work"
            terminal:    true
            status:      "abandoned"
        }
    }
    publish_selection: "selected_action"  // optional: publish choice to context
}
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

### 2.2.1 Context Contract Architecture

Every flow declares a **contract** — what context tier it operates at, what it reads from
persistence, and what structured data it produces at termination. Together these form
auditable boundaries between flows.

**Context Tiers.** Each flow declares a `context_tier` that constrains what downward
context it operates with. The tier hierarchy narrows context at each level:

```
mission_objective  — Full mission picture (design_and_plan, quality_gate)
project_goal       — Which capability to advance (mission_control, revise_plan, retrospective)
flow_directive     — What to do right now (file_ops, interact, diagnose_issue, project_ops)
session_task       — Mechanical execution (create, patch, rewrite, run_commands, etc.)
```

CUE enforces tier constraints at compile time (e.g., `flow_directive` tier flows must
declare `flow_directive` as a required input). The Python runtime provides belt-and-suspenders
enforcement at dispatch time — logging warnings when tier boundaries are violated dynamically.

**Structured Returns.** Each flow declares a `returns` block specifying what data it
produces at termination:

```cue
returns: {
    target_file:   {type: "string", from: "input.target_file_path"}
    files_changed: {type: "list",   from: "context.files_changed", optional: true}
}
```

At terminal steps, the runtime resolves each field's `from` path against the accumulator,
validates required fields, and packages the result as a structured dict. This replaces the
old prose-formatted `last_result` mechanism — the director's prompt template formats the
structured data for display.

**State Reads.** Each flow declares `state_reads` — the persistence paths it loads at
runtime (e.g., `["mission.objective", "mission.architecture"]`). This is an auditability
declaration: it documents which persistent data a flow depends on, making data flow across
persistence boundaries traceable.

**Goals.** Project goals sit between the mission objective (too broad for tactical decisions)
and individual tasks (too narrow for strategic reasoning). Goals are derived by
`design_and_plan` in two passes: deterministic structural goals from architecture modules,
and inference-derived functional goals from the objective + architecture. The director
(`mission_control`) reasons at the goal level — which capability to advance, whether an
approach is working, when to redesign vs retry. Goals are stored as `GoalRecord` on
`MissionState` and inform all downstream dispatch.

### 2.3 Flow Composition

#### Tail Calls

Tail calls are how flows chain without nesting. A terminal step can include a `tail_call`
block that specifies the next flow to execute and what inputs to pass:

```cue
dispatch: #StepDefinition & {
    action: "noop"
    tail_call: {
        flow: {$ref: "context.dispatch_config.flow"}
        input_map: {
            mission_id: {$ref: "input.mission_id"}
            task_id:    {$ref: "context.dispatch_config.task_id"}
        }
    }
}
```

At tail-call time, the runtime assembles the flow's `returns` declaration into a structured
dict and passes it as `last_result` in the tail-call inputs. This replaces the old
`result_formatter`/`result_keys` mechanism — `last_result` is now structured data (not
prose), and the director's prompt template formats it for display.

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
- `read_file(path)`, `write_file(path, content)`, `list_directory(path, recursive)`,
  `search_files(pattern, path)`, `makedirs(path)`, `file_exists(path)` — filesystem
- `run_command(cmd, cwd)` — subprocess execution
- `start_terminal()`, `send_to_terminal(session_id, command)`,
  `close_terminal(session_id)` — persistent terminal sessions
- `run_inference(prompt, config)` — single LLMVP GraphQL call
- `start_inference_session(config)`, `session_inference(session_id, prompt)`,
  `end_inference_session(session_id)` — memoryful inference sessions
- `load_mission()`, `save_mission(state)` — mission state persistence
- `push_event(event)`, `read_events()`, `clear_events()` — event queue
- `save_artifact(artifact)`, `load_artifact(task_id)`, `list_artifacts(filter)` — artifact storage
- `read_state(key)`, `write_state(key, value)` — generic key-value state
- `emit_trace(event)`, `flush_traces()` — runtime tracing

**Implementations:**
- `LocalEffects` — real filesystem, real subprocess, real inference, real persistence.
  Production use.
- `MockEffects` — canned responses, call recording. Testing use.
- `DryRunEffects` — reads real, writes logged. Planned.
- `GitManagedEffects` — auto-branching, auto-commit, rollback. Planned.

### 2.5 The Agent Cycle — mission_control

`mission_control` (defined in `flows/cue/mission_control.cue`, version 5) is the hub flow
that orchestrates the entire agent lifecycle. It operates at the `project_goal` context
tier — reasoning about which capability to advance, not the full mission picture.

1. **Load state** — read mission state, event queue, and frustration map from persistence.
2. **Apply last result** — integrate the returning flow's structured result (goal completion
   checks, frustration resets, plan availability).
3. **Process events** — handle user messages, abort/pause signals.
4. **Start director session** — open a memoryful inference session for the reasoning cycle.
5. **Reason** — analyze mission state at the goal level with pre-computed context (goals,
   plan, architecture, frustration landscape, dispatch history, notes).
6. **Decide flow** — LLM menu selects the best action type (file_ops, diagnose_issue,
   interact, project_ops, design_and_plan, quality_checkpoint, quality_completion, deadlock).
   The selection is published to context via `publish_selection`.
7. **Select task** — pick a task from the plan (or compose a novel directive via inference).
8. **Resolve target** — determine target file for the dispatch.
9. **Record and dispatch** — record the dispatch decision, end the director session,
   tail-call to the selected flow with a structured `flow_directive`.

All child task flows tail-call back to `mission_control` on completion, creating the
continuous cycle.

### 2.6 Frustration System

The frustration system is a per-task counter that gates escalation permissions. It prevents
the agent from immediately reaching for expensive solutions and forces cheap retries first.

**Current behavior:**
- Each task has a frustration counter that increments on failure/retry.
- `select_task_for_dispatch` checks frustration levels when selecting the next task.
  Higher frustration influences temperature perturbation and research injection.
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

Reusable step configurations defined in `flows/cue/templates.cue`. Templates
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

All flows are defined as CUE files in `flows/cue/` and compiled to `flows/compiled.json`
via `uv run ouroboros.py cue-compile`. The flow set was consolidated during the CUE
migration — many single-purpose flows were absorbed into broader lifecycle flows.

### 3.1 Orchestrator Flows

| Flow | Version | Tier | Steps | Purpose |
|------|---------|------|-------|---------|
| `mission_control` | v5 | project_goal | 30 | Core director — load state, reason about goals, select task, dispatch |
| `design_and_plan` | v4 | mission_objective | 17 | Design/reconcile architecture, derive goals, generate plan |
| `revise_plan` | v3 | project_goal | 6 | Add/reorder/remove tasks based on observations |
| `retrospective` | v5 | project_goal | 5 | Capture learnings from frustration recovery |

### 3.2 Task Flows (dispatched by mission_control)

| Flow | Version | Tier | Steps | Purpose |
|------|---------|------|-------|---------|
| `file_ops` | v1 | flow_directive | 18 | File lifecycle — routes to create/patch/rewrite, validates, self-corrects (2 retries), diagnoses on failure |
| `diagnose_issue` | v4 | flow_directive | 9 | Deep issue diagnosis — traces error paths, generates hypotheses, creates fix tasks |
| `interact` | v2 | flow_directive | 7 | Run the software, test features, observe behavior via terminal sessions |
| `project_ops` | v4 | flow_directive | 7 | Initialize project tooling and structure — configs, directories, packages |

### 3.3 Sub-flows (invoked by parent flows via `action: flow`)

| Flow | Version | Tier | Steps | Invoked By | Purpose |
|------|---------|------|-------|------------|---------|
| `create` | v1 | session_task | 7 | `file_ops` | Generate content for a new source file |
| `patch` | v1 | session_task | 10 | `file_ops` | AST-parsed surgical symbol-level editing |
| `rewrite` | v1 | session_task | 6 | `file_ops` | Complete file replacement |
| `run_commands` | v1 | session_task | 4 | `quality_gate` | Batch command execution |
| `run_session` | v1 | session_task | 7 | `interact`, `quality_gate` | Multi-turn persistent terminal session |
| `prepare_context` | v3 | session_task | 8 | Most task flows | Scan workspace, build repo map, select relevant files |
| `quality_gate` | v5 | mission_objective | 12 | `mission_control` | Structural + behavioral quality validation |
| `research` | v2 | session_task | 6 | `design_and_plan`, `mission_control` | Search and summarize into actionable text |
| `capture_learnings` | v3 | session_task | 5 | `retrospective` | Reflect on completed work, persist observations |
| `set_env` | v2 | session_task | 5 | `file_ops` | Detect and persist project validation tooling |

### 3.4 Supporting Files

| File | Purpose |
|------|---------|
| `flows/cue/flow.cue` | CUE schema — `#FlowDefinition`, `#StepDefinition`, `#Ref`, `#Resolver`, `#ContextTier`, `#FlowReturns` |
| `flows/cue/templates.cue` | Reusable step templates (e.g., `load_mission`, `read_target_file`, `push_note`) |
| `flows/cue/prompt.cue` | Prompt template reference types and pre-compute formatter registry |
| `flows/cue/lint.cue` | CUE-level lint constraints for flow validation |
| `flows/compiled.json` | Build artifact — all flows compiled from CUE (do not edit directly) |

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
| 5 — Tail Calls & mission_control | `loop.py`, `tail_call.py`, `mission_control` flow. Full agent cycle with continuous tail-call operation. |

**Post-Phase 5 — CUE Migration & Flow Consolidation** ✅

Major restructuring: 14 task flows + 11 sub-flows → 4 task flows + 10 sub-flows.

- `loader_v2.py` — CUE/JSON flow loading with `$ref` resolution, replacing Jinja2 in structural fields
- Section-based prompt templates in `prompts/` with pre-compute formatters (`formatters.py`)
- `markdown_fence.py` — robust CommonMark-based code block extraction from LLM responses
- `schema_extract.py` — lightweight structural summaries for LLM context
- Frustration system with threshold-gated dispatch configuration
- Quality gate with retry cycles
- Plan revision and extension checking
- Retrospective flow with periodic triggers
- AST-based repo map with tree-sitter and PageRank
- Flow visualizer with Mermaid/Graphviz output
- Mission YAML config with lifecycle commands (`pre_create`/`post_create`)
- Step templates for reusable step configuration via CUE unification

**Post-Phase 5 — Blueprint System** ✅

`ouroboros.py blueprint` command producing a comprehensive plan set in both
Markdown (for AI developer context) and PDF via WeasyPrint (for human architectural review).
Implementation in `agent/blueprint/` with IR schema, flow analyzer, linter, Mermaid
diagram generation, and custom Egyptian hieroglyphic symbology.

**Post-Phase 5 — Runtime Tracing** ✅

Lightweight always-on trace instrumentation producing structured JSONL trace events.
8 event types (CycleStart, CycleEnd, StepStart, StepEnd, InferenceCall, FlowInvoke,
FlowReturn, base TraceEvent). Token counting, wall-clock timing, resolver decisions.
`ouroboros.py trace` CLI for post-run analysis. Supports `--trace-thinking` for
chain-of-thought capture and `--trace-prompts` for full prompt/response recording.

**Post-Phase 5 — Context Contract Architecture** ✅

Context tier system (mission_objective → project_goal → flow_directive → session_task)
with compile-time CUE enforcement and runtime belt-and-suspenders validation. Structured
`returns` declarations replacing prose-formatted result strings. `state_reads` for
persistence auditability. Goal derivation (structural + functional) with goal-aware
director reasoning. See §2.2.1 for full documentation.

### Active Work

Current focus areas (see issue registry for prioritized backlog):
- LLMVP Harmony parser integration (B2/B4 — highest impact bug fix)
- Dependency coverage validation (A1 — blocks live testing)
- Cross-file integration validation (A2/A3 — runtime correctness)
- Early smoke testing after file creation (A5 — efficiency)

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
| Flow definitions | Declarative CUE with typed `$ref` resolution, compiled to JSON. Code hooks via action registry. Prompts in separate section-based YAML templates. | Type-safe schema validation at build time. Separates procedure (data) from behavior (code) from prompts (content). Flows inspectable, serializable, eventually agent-authorable. |
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
