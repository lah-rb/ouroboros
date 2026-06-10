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

**Declarative flow definitions.** Flows are defined as CUE data files in `flows/<set>/` directories,
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

Flows are CUE files in `flows/shared/` (set-agnostic layer) and per-set directories like
`flows/code_core/`, conforming to the `#FlowDefinition` schema defined in `flows/shared/flow.cue`. The build pipeline is: `.cue` → `cue export --out json` →
`flows/compiled.json` → Python loader (`loader.py`) resolves `$ref` values and assembles
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
(`loader.py`) resolves references against live input/context/meta namespaces at
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
project_goal       — Which capability to advance (mission_control)
flow_directive     — What to do right now (file_ops, interact, diagnose_issue, project_ops)
session_task       — Mechanical execution (create, patch, rewrite, run_commands,
                      run_session, prepare_context, research, set_env)
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
dict and passes it as `last_result` in the tail-call inputs. `last_result` is structured
data (not prose), and the director's prompt template formats it for display.

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

`mission_control` (defined in `flows/code_core/mission_control.cue`) is the hub flow that
orchestrates the entire agent lifecycle. It operates at the `project_goal` context tier —
reasoning about which capability to advance, not the full mission picture.

`mission_control` is a **deterministic pipeline**: it computes the current mission phase
from goal statuses and dispatches the appropriate work without LLM routing. The phases
are ordered and each has a clear entry condition based on what goals exist, what their
states are, and what the last returning flow reported:

- **Plan** — no architecture yet, or architecture drift detected → tail-call
  `design_and_plan`.
- **Structural sweep** — structural goals (file creation) have incomplete members →
  dispatch the next one to `file_ops`.
- **Environment** — source files exist but project tooling is missing → dispatch to
  `project_ops` or `set_env`.
- **Functional sweep** — structural goals complete, functional goals incomplete →
  dispatch to `interact` for behavioral testing, or `diagnose_issue` when tests fail.
- **Quality gate** — all goals provisionally complete → dispatch to `quality_gate` for
  final validation before mission completion.

At each cycle, `mission_control` loads mission state, integrates the `last_result` from
the returning flow (goal completion, dispatch history updates), processes any user
events, and then computes the phase. The dispatch step tail-calls the selected task
flow with a structured `flow_directive` input. All task flows tail-call back to
`mission_control` on completion, creating the continuous cycle with no external loop.

The deterministic-pipeline model replaces an earlier LLM-routing approach. Removing
LLM calls from the dispatch decision eliminated hallucination-driven failures (dispatching
tasks that weren't ready, skipping tasks the model didn't "see") and made the cycle
cheaper and more reproducible. The LLM is still consulted during task *execution* —
just not during task *selection*.

### 2.6 Frustration System (Deprecated)

The per-task frustration counter has been deprecated. It was removed along with the
`TaskRecord` class when the persistence model moved to a goal-centric plan (`GoalRecord`
with accumulated `DirectiveReport`s; tasks no longer exist as persisted entities).

**Why it was removed:** In practice, the frustration filter (`frustration < 5`) silently
removed tasks from the actionable pool without informing the director. This caused two
systemic failures: (1) tasks that succeeded on disk but failed verification were permanently
filtered out, blocking all downstream dependencies; (2) with actionable tasks removed, the
agent was funneled into inappropriate work (running tests before code exists, replanning
when implementation was needed). The existing circuit breakers — dispatch repeat detection,
completion gate rework budgets, quality gate exhaustion, deadlock detection, and the cycle
budget — provide more targeted protection without these side effects.

See §4.6 for the planned replacement: a unified circuit breaker layer that gives the
director visibility into task health rather than silently filtering.

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

Reusable step configurations defined in `flows/shared/templates.cue`. Templates
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

## 3. Flow Organization

All flows are defined as CUE files under `flows/<set>/` and compiled to `flows/compiled.json`
via `uv run ouroboros.py cue-compile`. The authoritative flow list is the CUE source —
this document describes how the set is *organized*, not what flows currently exist.

### 3.1 Flow Categories

Flows are organized by their role in the agent cycle, which aligns with their declared
`context_tier`:

**Orchestrator flows** (`project_goal` and `mission_objective` tiers). These shape the
mission itself — designing architecture, selecting goals, reasoning about what to
advance. `mission_control` is the always-present hub; other orchestrators run at mission
start or checkpoint boundaries. They tail-call into task flows and receive structured
reports back.

**Flow-directive flows** (`flow_directive` tier). The dispatchable units of work that
`mission_control` routes specific directives to. Each handles one class of work (source
files, project infrastructure, investigation, behavioral testing) and produces a
`DirectiveReport` before tail-calling back. This is the layer where inference-heavy
reasoning about project changes happens.

**Sub-flows** (`session_task` tier). Mechanical execution units invoked synchronously via
`action: flow`. They have no agency — a specific job, structured data back. Sub-flows
are the shared building blocks: modification modes (`create`, `rewrite`, `patch`),
context assembly (`prepare_context`, `research`), terminal interaction (`run_commands`,
`run_session`), environment probing (`set_env`). Parent flows treat them as black boxes.

### 3.2 Supporting Files

| File | Purpose |
|------|---------|
| `flows/shared/flow.cue` | CUE schema — `#FlowDefinition`, `#StepDefinition`, `#Ref`, `#Resolver`, `#ContextTier`, `#FlowReturns` |
| `flows/shared/templates.cue` | Reusable step templates (inherited via CUE unification) |
| `flows/shared/prompt.cue` | Prompt template reference types and pre-compute formatter registry |
| `flows/shared/lint.cue` | CUE-level lint constraints for flow validation |
| `flows/compiled.json` | Build artifact — all flows compiled from CUE (do not edit directly) |

For the current flow inventory, inspect the `flows/` set directories directly or run
`uv run ouroboros.py cue-compile` then read `flows/compiled.json`.

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

The right answer depends on observed failure modes from real missions. The runtime
tracing system will provide the data needed to make this design decision. Key questions:
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

### 4.6 Unified Circuit Breaker Layer (Subsumes Retrospective Redesign)

Replace the deprecated per-task frustration filter (§2.6) with a circuit breaker system
that the director can reason about. The frustration counter was a blunt safety valve —
it silently removed tasks after N failures without diagnostic context. The replacement
should unify the existing circuit breakers (dispatch repeat detection, rework budgets,
quality gate exhaustion, deadlock detection, cycle budget) under a single coherent
framework that:

- Surfaces task health to the director as structured context (attempt count, failure
  patterns, last error category) rather than silently filtering.
- Distinguishes failure modes: "verification parse error" vs "code genuinely wrong" vs
  "prerequisite missing" vs "environment broken" — each warrants a different response.
- Allows the director to explicitly decide: retry with different approach, skip and
  revisit, redesign the task, or escalate — rather than having the system decide
  by removing the task from view.
- Integrates with the escalation protocol (§4.1) when it arrives — the circuit breaker
  is the natural trigger point for consulting a senior model.

**Retrospective subsystem.** The original `retrospective` / `capture_learnings` flows
(since removed) coupled observation-capture to frustration thresholds: when a task hit
the frustration cap, the retrospective flow would run to surface learnings before
giving up. With frustration deprecated and the flows removed, there is no
learning-capture mechanism. The circuit breaker work above is the natural place to
reintroduce one — when a breaker trips, the director has concrete failure data worth
persisting as a learning. This avoids the original coupling (retrospective triggered
only on frustration) and makes learning-capture a first-class part of circuit-breaker
handling rather than a separate flow to invoke.

### 4.7 Step-Context Plumbing for Effects

Most effects methods are called with just their data arguments — the effect has no
knowledge of which flow, step, or goal the call originates from. `push_note` was the
first case where that missing context became interesting: a `source_flow` string is
passed per-call, every caller has to remember to pass it, and only one field in one
data model actually carries the value. Other effects have the same latent need —
`emit_trace` currently gets flow/step context from its payload rather than the effect
knowing inherently; `save_artifact` and `run_inference` would benefit from the same
correlation data if downstream consumers ever want to slice by step or goal.

Two plumbing shapes are worth considering when a concrete need arises:

**Context-manager approach.** `effects.in_step_context(meta)` sets a per-call context
variable around the step's execution. Any effect method can read it to enrich its
writes (`push_note` auto-injects `goal_id`; `emit_trace` auto-tags events; etc.).
The runtime wraps each action call with the context manager; effects read from it
opportunistically. Works across nested effect calls in a single step. Cost: a context
var, enter/exit bookkeeping in the runtime, and a small amount of discipline about
re-entrancy.

**Bind-on-build approach.** Just before handing `effects` to the action, the runtime
returns a lightweight wrapper (`effects.bind(meta)`) that knows the step's flow/step/
goal. The wrapper forwards every method, but methods that want step context read it
from the wrapper's bound meta. Zero changes to the action signatures; effects methods
opt in individually. Cost: a wrapper class and a small runtime change at the
`_build_step_input` call site.

**Candidate methods that could benefit.** Only pursue when there is a concrete consumer
for the enrichment:

- `push_note` — auto-inject `source_goal_id` so projections can slice notes per goal.
- `emit_trace` — auto-tag trace events with `goal_id` for per-goal timeline analysis.
- `save_artifact` — artifacts currently carry `task_id` from the action; `goal_id`
  would enable grouping artifacts under their owning goal.
- `run_inference` — prompt-observability tooling could tag inference calls with the
  step that produced them without every action threading it manually.

Either approach is a small project — the right moment to build it is when the first
consumer (likely the circuit-breaker work in §4.6, which will want per-goal failure
timelines) creates a concrete requirement. Until then, per-call arguments remain the
simpler choice.

### 4.8 Self-Hosted Search Stack

The `research` flow currently consumes Exa via the hosted `exa-mcp-server` (see
AGENT.md §"External Services"). Exa is inexpensive at expected volume and its
bundled search+content returns match the "search with follow-up fetch" shape the
flow needs. It is the right default today.

A self-hosted alternative is worth reaching for when one of these triggers fires:
research call volume sustainably exceeds Exa's free tier in a way that matters, Exa
quality or reliability regresses materially on coding queries, or the project
commits to operating without any paid external services.

**Proposed architecture.** A new `mcp_servers/search/` package mirroring
`mcp_servers/terminal/` in shape — FastMCP over stdio, same consumption pattern
from the effects layer. Tools would be `search(query, n)`, `fetch(url)`, and
`search_and_fetch(query, n)` to preserve the current research-flow contract.

**Backends.**
- *Primary:* SearXNG running as a local container. Open-source metasearch,
  aggregates DuckDuckGo, Brave, Wikipedia, Stack Overflow, GitHub, arXiv, and
  others. No API keys. Quality is respectable on coding queries thanks to the
  domain mix; inherits upstream rate limits but is resilient to any single
  engine's bad day.
- *Fallback:* Google Programmable Search Engine (CSE). 100 queries/day free, then
  $5/1,000. Invoked when SearXNG returns thin or irrelevant results. Highest-
  quality index available; 10 results per request ceiling is fine for our use
  case.
- *Extraction:* `trafilatura` for HTML→markdown on follow-up fetches.
  Article-quality on the major coding sources (Stack Overflow, GitHub README/wiki,
  MDN, Python/Rust/language docs).

**Why not now.** The current research volume is roughly one invocation per mission
from `design_and_plan`. Exa is free at that scale and the self-hosted path adds
ops surface area (a SearXNG container to keep updated, CSE billing to monitor,
extractor tweaks when upstream sites change). The payoff doesn't materialize
until volume grows or Exa stops being a fit. Deferring this is the "consolidation
over proliferation" call — ship research using the service that already works,
replace when the trigger fires.

---

## 5. System Capabilities

The system delivers the following capabilities in working form. This section describes
*what the system can do*, not the sequence of development phases that produced each
capability — phase numbering drifts over time and the current state is what matters.

**Flow engine.** Declarative CUE flow definitions compiled to JSON. Steps with typed I/O,
explicit transitions, context scoping. Rule-based and LLM-menu resolvers. Sub-flow
invocation and tail-call chaining. Context Contract Architecture enforces tier
boundaries and structured returns.

**Effects interface.** All side effects routed through a swappable protocol. `LocalEffects`
for production (real filesystem, subprocess, LLMVP inference, JSON persistence,
terminal sessions via MCP, runtime tracing). `MockEffects` for testing (canned
responses, call recording).

**Inference integration.** LLMVP GraphQL client with section-based YAML prompt templates,
pre-compute formatters, Jinja2 rendering, and memoryful inference sessions for
multi-turn reasoning. Robust JSON extraction via `llm_json.py` wrapping `json_repair`
for malformed LLM output.

**Persistence.** File-backed JSON state in `.agent/` (mission, events, artifacts, repo
map cache). Atomic writes via temp+rename. Single-threaded access enforced by the
tail-call execution model.

**Goal-driven planning.** `design_and_plan` derives project goals from the mission
objective and architecture in two passes (deterministic structural + inference-derived
functional). The director reasons at the goal level; goals are the plan.

**Runtime tracing.** Always-on structured JSONL trace events (cycle/step/inference/
flow-invoke events). `ouroboros.py trace` CLI for post-run analysis. Optional
`--trace-thinking` for chain-of-thought capture and `--trace-prompts` for full
prompt/response recording.

**Blueprint generation.** `ouroboros.py blueprint` produces Markdown and PDF architectural
documentation from the compiled flows. Custom symbology, Mermaid diagrams, flow-level
cross-references.

**AST-based repo map.** Structural code awareness via tree-sitter parsing. Directed
reference graph with PageRank to surface the most important files. Token-budgeted
formatting for inclusion in prompts.

**Mission management.** YAML-configured missions with pre/post-create lifecycle commands.
CLI for create, start, pause, resume, abort, status, history, and interactive messaging.

### Active Work

Active work items live in the project's issue registry, not in this document. This
section intentionally does not enumerate them — any such list goes stale quickly and
the issue registry is the source of truth.

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
