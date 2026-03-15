# Ouroboros — Architecture & Implementation Guide

*A flow-driven autonomous coding agent backed by LLMVP local inference.*

*This document captures the complete architectural design for Ouroboros, produced through iterative discussion. It is intended to be self-contained: an implementer working in a separate coding context should be able to build the system from this document alone without needing access to the original conversation.*

---

## 1. Project Identity & Relationship to LLMVP

### 1.1 What Ouroboros Is

Ouroboros is an autonomous, flow-driven coding agent. It operates continuously with minimal human supervision, executing missions (user-defined objectives) by breaking them into tasks, selecting and executing appropriate workflows, and escalating to a more capable external model when it gets stuck.

### 1.2 The Programming Shop Metaphor

The system is modeled after a programming shop with three roles:

- **Shop Director (the user):** Sets missions, checks in periodically, adjusts direction. Interacts through CLI commands and mission configuration.
- **Senior Developer (Claude API):** Consulted when the junior dev gets stuck. Provides code reviews, architectural guidance, direct fixes, and decisions. Expensive per interaction — used sparingly.
- **Junior Developer (local model via LLMVP):** Does the actual work. Runs continuously for near-zero cost. Follows established flows, makes tactical decisions, and knows when to ask for help.

### 1.3 Separation from LLMVP

**Ouroboros is a pure GraphQL client of LLMVP.** It is a separate project with its own repository, its own `pyproject.toml`, its own CLI entry point. It does not import any Python modules from LLMVP. All inference requests go through LLMVP's GraphQL API over HTTP.

This separation means:
- Ouroboros can run as a separate process, potentially on a different machine.
- LLMVP doesn't know its client is an autonomous agent — it just serves inference requests.
- The inference backend could be swapped for any GraphQL-compatible server without changing Ouroboros.
- Each project maintains independent development velocity and release cycles.

### 1.4 Target Hardware & Model

Primary target: Apple Silicon M1 Ultra with 128 GB unified memory, running Qwen 3.5 122B (A10B active, sparse MoE). This model provides:

- High coding competence (top-ranked on HuggingFace arena for code)
- 1 response at ~35 tokens/sec, or up to 8 parallel agents at ~5 tokens/sec each
- 2-3 pool instances for the standard operating mode (triage + surgeon patterns)
- Near-zero electricity cost for continuous 24/7 operation

### 1.5 Multi-Project Future

The architecture supports running multiple missions on separate projects simultaneously, sharing the LLMVP inference pool. Each mission has its own working directory, its own `.agent/` state, and its own effects profile. Pool capacity is shared across missions. This is a future capability that the architecture accommodates without redesign — not part of the initial implementation.

---

## 2. Core Architecture

### 2.1 The Flow Engine

The flow engine is the backbone of Ouroboros. Everything the agent does is expressed as a flow — a directed graph of steps with typed inputs, typed outputs, and explicit transition logic.

#### 2.1.1 Design Principles

**Functional model.** Every step is a pure function: immutable input in, immutable output out. Side effects (file I/O, inference calls, API requests) happen through an effects interface that the step receives but does not own. This gives reproducibility (same input → same output), composability (steps don't know about each other), serializability (every state transition is a logged event), and testability (swap the effects interface for mocks).

**Declarative flow definitions.** Flows are defined as YAML data files, not Python code. The graph structure (steps, transitions, context requirements) is pure data. Actions (what a step does) are registered Python callables referenced by name. This separates "what is the procedure" from "what does this step actually do," making flows inspectable, serializable, and eventually authorable by the agent itself.

**Pluggable transition resolution.** The flow engine doesn't decide how transitions work — the resolver does. Different steps can use different resolver types (rule-based, LLM-driven) within the same flow. The engine just calls the resolver and follows the result.

**Extensibility as a core requirement.** The engine makes no assumptions about constraint level, resolver types, or action types. New resolvers, new action types, and new effects can be added without modifying the engine.

#### 2.1.2 Flow Definition Format

Flows are YAML files with the following structure:

```yaml
flow: modify_file
version: 1
description: "Modify an existing file to address a specific issue"

input:
  required:
    - target_file_path
    - reason
  optional:
    - mission_excerpt
    - related_files_hint

defaults:
  config:
    temperature: 0.2
    max_tokens: 4096

steps:
  gather_context:
    action: read_files
    description: "Read target file and discover related files"
    context:
      required: []
      optional: [related_files_hint]
    params:
      target: "{{ input.target_file_path }}"
      discover_imports: true
    config:
      temperature: 0.0
    resolver:
      type: rule
      rules:
        - condition: "result.file_found == true"
          transition: plan_change
        - condition: "result.file_found == false"
          transition: escalate
    publishes:
      - target_file
      - related_files

  plan_change:
    action: inference
    description: "Analyze the issue and produce a change plan"
    context:
      required: [target_file, reason]
      optional: [related_files, mission_excerpt]
    prompt: |
      You are analyzing a file to plan a specific change.

      File: {{ context.target_file.path }}
      Content:
      {{ context.target_file.content }}

      Issue: {{ context.reason }}

      Produce a change plan. Describe what to change, where, and why.
      Assess your confidence and the risk level.
    config:
      temperature: "t*1.2"
    resolver:
      type: llm_menu
      prompt: "Given your plan, what should happen next?"
      options:
        execute_change:
          description: "Confidence is high, proceed with the change"
        gather_more_context:
          description: "Need to see more files or context before committing"
          target: gather_context
        request_review:
          description: "Uncertain — escalate plan to senior dev for review"
        abandon:
          description: "This approach won't work, return to parent flow"
          terminal: true
          status: abandoned
    publishes:
      - plan
      - risk_assessment

  # ... additional steps ...

entry: gather_context

overflow:
  strategy: split
  fallback: reorganize
```

#### 2.1.3 Key Flow Definition Elements

**`input`**: Declares what the flow requires and optionally accepts from its caller. The runtime validates required inputs before execution.

**`steps`**: A map of step definitions. Each step has:

- **`action`**: Reference to a registered action callable, or the special values `inference` (inference call), `flow` (sub-flow invocation), or `tail_call` (tail-call to another flow).
- **`description`**: Human-readable purpose of the step.
- **`context.required` / `context.optional`**: Which keys from the context accumulator this step needs. The runtime filters the accumulator to only these keys before invoking the action. Required keys are validated — missing keys cause a runtime error.
- **`params`**: Static parameters for the action. Supports `{{ }}` Jinja2 template interpolation against flow inputs and context.
- **`prompt`**: For `action: inference` steps — the Jinja2 template rendered against context and passed to the model.
- **`config`**: Generation parameter overrides for this step. Merged with flow-level defaults. Supports both absolute values (`temperature: 0.1`) and relative values (`temperature: "t*0.5"`).
- **`resolver`**: How the next step is chosen. See §2.1.4.
- **`publishes`**: List of context keys this step adds to the accumulator. The step's `context_updates` output must include these keys.
- **`effects`**: Declared side effects the step performs (file writes, commands, etc.).
- **`terminal`**: If true, this step ends the flow. Must include a `status` value.
- **`tail_call`**: If present on a terminal step, triggers a tail call instead of returning to caller. See §2.3.

**`entry`**: The step ID where execution begins.

**`overflow`**: Default context overflow strategy for all steps. Individual steps can override.

**`defaults.config`**: Flow-level generation parameter defaults.

#### 2.1.4 Transition Resolvers

**Rule-based resolver (`type: rule`):** Evaluates conditions against the step's output, the context accumulator, and execution metadata. No inference call needed. Conditions are Python expressions evaluated with restricted `eval()` — the context dict is the only namespace, no builtins. Flow authors are trusted, so this is acceptable.

```yaml
resolver:
  type: rule
  rules:
    - condition: "result.file_found == true"
      transition: plan_change
    - condition: "result.file_found == false"
      transition: escalate
```

Rules are evaluated in order; the first matching condition wins.

**LLM menu resolver (`type: llm_menu`):** Presents the model with a constrained set of named options, each with a description. The model picks one. Costs one inference call.

```yaml
resolver:
  type: llm_menu
  prompt: "Given your plan, what should happen next?"
  options:
    execute_change:
      description: "Confidence is high, proceed with the change"
    request_review:
      description: "Uncertain — escalate plan to senior dev for review"
    abandon:
      description: "This approach won't work"
```

The resolver constructs a prompt listing the options, calls `effects.run_inference()` with low temperature and short max_tokens, parses the response to extract the option name, and validates it. Invalid responses get one retry with a more constrained prompt, then fall back to a default if defined.

**Dynamic options (`options_from`):** For cases where the option set is generated from context (e.g., selecting from a list of ready tasks), the resolver reads the option list from a context key:

```yaml
resolver:
  type: llm_menu
  options_from: "context.assessment.ready_tasks"
```

**Additional resolver types** can be added by registering new resolver functions. The engine dispatches based on the `type` field.

#### 2.1.5 Relative Temperature

Flow steps can specify temperature as a multiplier of the model's configured default, providing cross-model portability. A model that runs best at 0.7 and one that runs best at 0.4 both respond correctly to `t*0.5` (halving for precision) or `t*1.2` (slight increase for creativity).

```yaml
config:
  temperature: 0.1        # absolute: always 0.1
  temperature: "t*0.5"    # relative: half of model default
  temperature: "t*1.2"    # relative: 20% above model default
```

The flow runtime parses this at step execution time, resolves `t` from the active model configuration, and passes the computed value to the inference effect.

### 2.2 Context Model

#### 2.2.1 StepInput

Every step receives a single `StepInput` containing:

- **`task`**: The step's description string — makes the step self-contained even in isolation.
- **`context`**: A dictionary of named data bundles from previous steps, filtered to only the keys this step declared interest in (required + optional). Values are structured data (dicts, Pydantic models), not raw strings.
- **`config`**: Merged generation parameters for this step (flow defaults + step overrides, with relative temperatures resolved).
- **`budget`**: Token budget information — static knowledge tokens consumed, context size, generation headroom remaining.
- **`meta`**: Flow metadata — flow name, step id, attempt number, mission id, task id, frustration level, escalation permissions.

#### 2.2.2 StepOutput

Every step produces a single `StepOutput` containing:

- **`result`**: The primary output — structured data typed per action.
- **`observations`**: Free-form notes from the action/model. Useful for downstream steps and debugging.
- **`context_updates`**: Dictionary of named data bundles to add/overwrite in the context accumulator. This is how data flows between steps.
- **`transition_hint`**: Optional recommendation for which transition to take. Resolvers may use or ignore this.
- **`effects_log`**: Automatically populated record of side effects performed, captured by the effects interface wrapper.

#### 2.2.3 Context Accumulator

The runtime maintains a context accumulator for each flow execution. It starts with the flow's initial inputs and grows as steps add `context_updates`.

When preparing `StepInput` for the next step:
1. Take the current accumulated context.
2. Filter to only the keys the step declares (required + optional).
3. Validate that all required keys are present.
4. Attach task description, config overrides, budget, and meta.

This filtering keeps each step's context window tight. Steps only see what they've declared interest in.

#### 2.2.4 Context Overflow Strategies

When context exceeds the model's token budget, three strategies are available (specified per-flow with per-step override):

1. **Split (primary strategy):** The surgeon/triage pattern — dedicate one pool instance to full messy context for analysis, produce a clean brief, hand the brief to another instance for execution. If no second instance is available, the same instance drops its context, picks up the surgical brief, and continues sequentially. No deadlock.
2. **Reorganize (escape hatch):** Kick the job back to the parent flow for reassessment. The parent can re-scope, split the task, or escalate.
3. **Summarize (lossy fallback):** Run a quick inference pass to compress verbose context into a summary. Loses detail but preserves coherence — preferable to truncation.

**Truncation is explicitly excluded.** LLMs degrade unpredictably when context is silently truncated — the model doesn't know what it's missing.

### 2.3 Flow Composition

#### 2.3.1 Sub-Flow Invocation (Black Box)

Flows can invoke other flows as child flows. The composition model is **black box**: the parent passes inputs, the child runs to completion (or failure), and the parent receives the terminal output. The parent has no visibility into the child's internal steps.

```yaml
  attempt_fix:
    action: flow
    flow: modify_file
    input_map:
      target_file_path: "{{ context.target_file_path }}"
      reason: "{{ context.reason }}"
      mission_excerpt: "{{ context.mission_excerpt }}"
    resolver:
      type: rule
      rules:
        - condition: "result.status == 'success'"
          transition: report_fix
        - condition: "result.status == 'escalated'"
          transition: handle_escalation
        - condition: "result.status == 'abandoned'"
          transition: reassess
    publishes:
      - fix_result
```

The parent's contract with a child flow: "I give you these inputs, you give me a terminal status and output." The child never sees the parent's context — only what the parent explicitly passes through `input_map`.

Terminal states declare a status (`success`, `abandoned`, `escalated`), and the parent branches on this status.

#### 2.3.2 Tail Calls

A tail call is a special terminal state that, instead of returning to a caller, triggers a new flow execution. The current flow's context is fully released — no stack accumulation.

```yaml
  complete:
    action: log_completion
    terminal: true
    status: success
    publishes:
      - summary
    tail_call:
      flow: mission_control
      input_map:
        mission_id: "{{ meta.mission_id }}"
        last_result: "{{ result.summary }}"
        last_status: "success"
        last_task_id: "{{ meta.task_id }}"
```

Tail calls are the mechanism that replaces an external agent loop. Child flows tail-call back to `mission_control` on completion, creating a continuous cycle driven entirely by the flow graph.

Terminal steps **without** `tail_call` produce true termination — the outermost process loop (`loop.py`) receives the result and stops.

Tail calls can include a `delay` field for polling scenarios (e.g., the idle state waiting for events).

#### 2.3.3 Flow Directory Structure

Start flat — organize when the number of flows demands it:

```
flows/
├── registry.yaml              # lists all flows with metadata
├── mission_control.yaml
├── modify_file.yaml
├── create_file.yaml
├── run_tests.yaml
├── fix_bug.yaml
├── create_plan.yaml
└── ...
```

`registry.yaml` is the discovery mechanism. It lists each flow with its name, description, input requirements, and possible terminal statuses. The runtime loads it at startup and validates that all referenced flow files exist and parse correctly.

### 2.4 Effects Interface

#### 2.4.1 Design Principles

Actions request effects through a swappable interface. The action never directly touches the filesystem, runs a process, or makes a network call. This means implementations can be swapped (real, sandboxed, mocked, dry-run) without changing any action or flow logic.

#### 2.4.2 The Effects Protocol

```python
class Effects(Protocol):
    # File operations
    async def read_file(self, path: str) -> FileContent: ...
    async def write_file(self, path: str, content: str) -> WriteResult: ...
    async def list_directory(self, path: str, recursive: bool = False) -> DirListing: ...
    async def search_files(self, pattern: str, content_pattern: str | None = None) -> SearchResults: ...
    async def file_exists(self, path: str) -> bool: ...

    # Process execution
    async def run_command(self, command: str, working_dir: str | None = None, timeout: int = 30) -> CommandResult: ...

    # Inference (via LLMVP GraphQL API)
    async def run_inference(self, prompt: str, config_overrides: dict | None = None) -> InferenceResult: ...

    # Escalation (via Claude API)
    async def escalate_to_api(self, bundle: EscalationBundle) -> EscalationResponse: ...

    # Persistence
    async def load_mission(self) -> MissionState: ...
    async def save_mission(self, state: MissionState) -> bool: ...
    async def read_events(self) -> list[Event]: ...
    async def push_event(self, event: Event) -> bool: ...
    async def clear_events(self) -> bool: ...
    async def save_artifact(self, flow_name: str, task_id: str, artifact: FlowArtifact) -> bool: ...
    async def load_artifact(self, task_id: str) -> FlowArtifact | None: ...
    async def list_artifacts(self, filter: str | None = None) -> list[str]: ...
    async def read_state(self, key: str) -> Any: ...
    async def write_state(self, key: str, value: Any) -> bool: ...
```

#### 2.4.3 Implementations

**`LocalEffects`**: The real implementation. File operations hit the actual filesystem. `run_command` runs real subprocesses. `run_inference` makes GraphQL requests to the LLMVP endpoint. All paths are resolved relative to the mission's working directory; path traversal above the working directory is blocked.

**`GitManagedEffects`**: Wraps `LocalEffects`. Every `write_file` auto-commits to a branch. `run_command` operates in a git worktree. Rollback is `git reset`. Safety net for real project work.

**`DryRunEffects`**: Reads are real, writes are logged but not executed. Commands log what would run. For "what would the agent do?" previews.

**`MockEffects`**: Returns canned data from a preconfigured dict. For unit testing flows and actions.

#### 2.4.4 Effects Profiles

Configured at mission level with flow-level override:

```yaml
# Mission config
effects_profile: git_managed    # default for all flows in this mission

# Flow-level override
flow: modify_own_code
effects_profile: git_managed    # always version-controlled, regardless of mission default
```

Most flows use `effects_profile: inherit` to take whatever the mission specifies.

#### 2.4.5 Automatic Logging

Every effect method call is automatically logged by a wrapper — method name, arguments (content truncated for large files), result summary, timestamp, duration. These entries accumulate as the `effects_log` on StepOutput. Actions don't do this logging — it's transparent. This provides a complete audit trail and is included in escalation bundles.

#### 2.4.6 Path Scoping

Every mission has a `working_directory`. All file paths in effects resolve relative to this directory. The effects interface refuses paths that escape the working directory (resolves absolute path, verifies it starts with the working directory prefix). For self-modification missions, the working directory is the Ouroboros project root itself.

### 2.5 The Agent Cycle — mission_control

#### 2.5.1 No External Loop

There is no special agent loop runtime. The agent cycle is itself a flow — `mission_control` — invoked via tail calls. The cycling behavior emerges from the flow graph: `mission_control` dispatches a task flow, the task flow completes and tail-calls back to `mission_control`, which dispatches the next task.

The only "loop" is a thin outer process (`loop.py`) that follows tail calls:

```python
async def run_agent(mission_id: str, flows_dir: str, effects: Effects):
    registry = load_all_flows(flows_dir)
    actions = build_action_registry()
    current_flow = "mission_control"
    current_inputs = {"mission_id": mission_id}

    while True:
        flow_def = registry[current_flow]
        outcome = await execute_flow(flow_def, current_inputs, actions, effects)

        match outcome:
            case FlowTermination(result=result):
                log.info(f"Agent terminated: {result.status}")
                return result
            case FlowTailCall(target_flow=target, inputs=inputs, delay_seconds=delay):
                if delay:
                    await asyncio.sleep(delay)
                current_flow = target
                current_inputs = inputs
```

#### 2.5.2 mission_control Flow Structure

The `mission_control` flow has five phases:

**Phase 1 — Load State:** Read mission state and event queue from persistence. Branch on mission status (active → continue, paused → idle, completed/aborted → terminate).

**Phase 2 — Integrate Previous Result:** Apply the previous flow's outcome to mission state. On success: mark task complete, reset frustration. On escalated: mark task blocked, increment frustration. On abandoned: mark task failed, increment frustration, add failure notes. Save updated state.

**Phase 3 — Process Events:** Read and consume pending events — user messages, escalation responses, abort/pause signals. Apply changes to mission state.

**Phase 4 — Assess and Select:** Determine what to work on next. Fast path (rule-based): if there's an obvious next pending task with met dependencies, take it. Slow path (LLM): if multiple tasks are ready and prioritization is ambiguous, ask the model to choose. The model sees each task's frustration level when prioritizing.

**Phase 5 — Configure and Dispatch:** Build the input map for the selected task's flow. Compute escalation permissions based on frustration level. Tail-call to the selected flow.

**Terminal/Parking States:** `completed` (all tasks done, objective met), `aborted` (user abort), `idle` (all tasks blocked, delayed tail-call back to self for polling).

See Appendix A for the complete `mission_control` flow definition.

#### 2.5.3 The Frustration System

Frustration is a per-task persistent integer counter that drives escalation behavior — a cost-escalation ladder forcing the agent to try cheap solutions before reaching for expensive tools.

**Mechanics:**
- Starts at 0 for each task.
- Incremented when a dispatched flow returns `escalated` or `abandoned`.
- Reset to 0 on `success`.
- Persisted in mission state.

**Frustration gates escalation permissions:**

| Frustration | Unlocked Escalation |
|---|---|
| 0-1 | None — normal operation only |
| 2-3 | Can escalate for review (cheap — senior dev reads and reacts) |
| 4 | Can escalate for instructions (moderate — senior dev writes guidance) |
| 5+ | Can escalate for direct fix (expensive — senior dev writes code) |

These thresholds are configurable in the `prepare_dispatch` step's params.

Escalation permissions are passed to child flows via `meta.escalation_permissions`. A child flow's escalation steps check these permissions before offering escalation as a resolver option. At frustration 0, the "request review" option literally doesn't appear in the LLM menu.

The `prioritize` step sees frustration when the LLM chooses between tasks — a task at frustration 4 might be deprioritized or specifically chosen because escalation is now available.

### 2.6 Persistence

#### 2.6.1 Storage Model

File-backed JSON in a `.agent/` directory within the mission's working directory.

The agent runs one cycle at a time (the tail-call model guarantees this). No concurrent write contention. Data is structured but not relational. Total volume is modest. JSON has the advantage that the agent can read and reason about its own state files directly through the effects interface.

```
.agent/
├── mission.json              # current mission state
├── events.json               # pending event queue
├── history/                  # completed flow artifacts
│   ├── 2026-03-13T10-00-00_implement-flow-engine.json
│   └── ...
├── snapshots/                # context snapshot metadata
└── config.json               # agent-level config
```

#### 2.6.2 Mission State Schema

```python
class MissionState(BaseModel):
    id: str
    status: Literal["active", "paused", "completed", "aborted"]
    objective: str
    principles: list[str] = []
    plan: list[TaskRecord] = []
    notes: list[NoteRecord] = []
    created_at: str
    updated_at: str
    config: MissionConfig
    schema_version: int = 1

class TaskRecord(BaseModel):
    id: str
    description: str
    flow: str
    inputs: dict
    status: Literal["pending", "in_progress", "complete", "failed", "blocked"]
    depends_on: list[str] = []
    priority: int = 0
    frustration: int = 0
    attempts: list[AttemptRecord] = []
    summary: str | None = None
    escalation_bundle: dict | None = None

class MissionConfig(BaseModel):
    working_directory: str
    effects_profile: Literal["local", "git_managed", "dry_run"] = "local"
    escalation_budget_usd: float | None = None
    escalation_tokens_used: int = 0
    llmvp_endpoint: str = "http://localhost:8000/graphql"
```

#### 2.6.3 Atomic Writes

`save_mission` writes to a temp file, then renames over `mission.json`. Rename is atomic on POSIX. A crash mid-write leaves the previous valid state intact.

#### 2.6.4 Event Queue

`events.json` is the mailbox. External actors (user CLI, async escalation responses) write events; `mission_control` reads and consumes them. Concurrent access is protected by `fcntl.flock` — contention is extremely low.

```python
class Event(BaseModel):
    id: str
    type: Literal["user_message", "escalation_response", "priority_change", "abort", "pause", "resume"]
    timestamp: str
    payload: dict
```

#### 2.6.5 Schema Versioning

Every persisted JSON file includes `schema_version`. When models change, the version bumps and a migration function is added. `load_mission()` checks the version and runs migrations if needed. Infrastructure is in place from the start — one version, no migrations initially.

#### 2.6.6 Crash Recovery

1. Server restarts, LLMVP backend initializes.
2. Ouroboros startup checks for `.agent/mission.json` in the configured working directory.
3. If found with `status: active`, invokes `mission_control` with `mission_id` and no `last_result`/`last_status`.
4. `mission_control` loads state, sees an `in_progress` task with no active flow.
5. Treats it as interrupted — restarts the task's flow from scratch or marks for reassessment.
6. Normal operation continues.

Recovery follows the exact same code path as normal operation.

### 2.7 Escalation Protocol

#### 2.7.1 Escalation Bundle Format

The bundle answers four questions for the senior dev:

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

#### 2.7.2 Problem Types

- **`review`**: Agent produced a plan/change but isn't confident. Cheapest — senior dev just reads and reacts.
- **`diagnosis`**: Something failed and the agent can't figure out why after multiple attempts.
- **`capability`**: Agent recognizes the task exceeds its ability (architecture, complex refactors).
- **`ambiguity`**: Agent needs a decision it can't make from principles and mission alone.

#### 2.7.3 Response Format

```python
class EscalationResponse(BaseModel):
    type: Literal["instructions", "direct_fix", "decision", "approval", "rejection"]
    content: str
    files: dict[str, str] | None       # modified file contents for direct_fix
    reasoning: str | None               # for decisions
    follow_up: str | None
```

#### 2.7.4 API Call Mechanics

The `escalate_to_api` effect:
1. Constructs a tight system prompt framing the senior dev role.
2. Constructs the user message from the bundle — structured, not raw-dumped.
3. Calls the Claude API (one-shot, no back-and-forth).
4. Parses the response into `EscalationResponse`.

The system prompt is tightly written to avoid wasted tokens:
```
You are a senior developer supervising an autonomous coding agent.
The agent is working on: {{ bundle.mission_objective }}
It has escalated because: {{ bundle.problem_type }}

Respond ONLY with the requested format. Do not restate the problem.
Do not ask clarifying questions.
```

#### 2.7.5 Cost Management

- Mission-level budget cap (`escalation_budget_usd`) checked before each API call.
- Running token count tracked in mission config (`escalation_tokens_used`).
- Budget exhaustion returns an error — the flow handles it (probably pauses the mission and notifies the user).

#### 2.7.6 Blocking vs Async

Initial implementation: escalation blocks. The flow calls the API, waits, and continues with the response.

Future optimization: escalation fires the API call, the flow terminates with `status: escalated`, `mission_control` picks up a different non-blocked task, and the response arrives as an event.

### 2.8 Parallel Execution

#### 2.8.1 Level 1: Parallel Tasks (Primary Implementation)

When `mission_control` identifies multiple independent tasks and pool capacity is available, it dispatches them simultaneously on separate pool instances.

```yaml
  parallel_dispatch:
    action: parallel_tail_call
    parallel:
      items: "{{ context.independent_tasks }}"
      max_workers: "{{ context.available_pool_instances }}"
      flow: "{{ item.flow }}"
      input_map: "{{ item.inputs }}"
    join:
      tail_call:
        flow: mission_control
        input_map:
          mission_id: "{{ input.mission_id }}"
          last_result: "{{ parallel_results }}"
          last_status: "combined"
```

Individual flows don't know they're running in parallel — the parallelism is purely orchestration.

**Pool headroom reservation:** Don't use all instances for parallel tasks. Reserve 2 instances for sub-work (retry loops, surgeon patterns) to prevent deadlock:

```yaml
max_parallel_tasks: "{{ pool_size - 2 }}"
```

**Safety:** The LLM decides whether parallel execution is actually safe — it can identify that two "independent" tasks both modify the same file and choose serial instead.

#### 2.8.2 Level 2: Surgeon/Triage Split (Deferred)

Within a single step, one instance handles messy context and produces a clean brief, another executes with surgical precision. If no second instance is available, the same instance drops context and continues sequentially — no deadlock.

Deferred until a specific flow demonstrates clear need.

#### 2.8.3 Level 3: Parallel Sub-Steps (Deferred)

Multiple independent inference calls within a single step. Deferred — Levels 1 and 2 cover the important cases.

### 2.9 Self-Modification Lifecycle

When Ouroboros modifies its own codebase, a strict lifecycle flow applies:

1. **Duplicate:** Create a complete copy of the project.
2. **Modify:** Work on the duplicate — all changes go here.
3. **Transition:** Start a transition state machine.
4. **Validate:** Run the full test suite on the duplicate.
5. **Promote or rollback:** On success, replace the original with the duplicate. On failure, discard the duplicate and rollback.
6. **Stress test:** Run extended validation on the promoted version.
7. **Archive:** On success, archive the previous version. On failure, restore from archive.

This is implemented as a dedicated flow (or flow chain), not baked into the effects interface. The effects interface stays simple; the safety comes from the procedure.

---

## 3. Project Structure

```
ouroboros/
├── pyproject.toml              # separate project, own dependencies
├── ouroboros.py                # CLI entry point
├── agent/
│   ├── __init__.py
│   ├── models.py              # Pydantic models: FlowDefinition, StepInput/Output, FlowResult, etc.
│   ├── loader.py              # YAML parser + validator for flow definitions
│   ├── runtime.py             # Flow executor
│   ├── loop.py                # Thin outer process: bootstrap mission_control, follow tail calls
│   ├── tail_call.py           # Tail-call mechanics (FlowTailCall, FlowTermination, FlowOutcome)
│   ├── template.py            # Jinja2 prompt rendering for flow steps
│   ├── resolvers/
│   │   ├── __init__.py        # Resolver dispatch
│   │   ├── rule.py            # Rule-based resolver (restricted eval)
│   │   └── llm_menu.py        # LLM-driven menu resolver
│   ├── actions/
│   │   ├── __init__.py
│   │   └── registry.py        # Action registry (register, lookup, execute)
│   ├── effects/
│   │   ├── __init__.py        # Exports Effects protocol + factory
│   │   ├── protocol.py        # Effects Protocol definition
│   │   ├── local.py           # LocalEffects (real filesystem, real subprocess)
│   │   ├── git_managed.py     # GitManagedEffects (auto-commit, branching, rollback)
│   │   ├── dry_run.py         # DryRunEffects (reads real, writes logged)
│   │   ├── mock.py            # MockEffects (canned responses for testing)
│   │   ├── inference.py       # Inference via LLMVP GraphQL API
│   │   └── persistence.py     # Persistence methods wired to PersistenceManager
│   ├── persistence/
│   │   ├── __init__.py
│   │   ├── models.py          # MissionState, TaskRecord, Event, FlowArtifact, etc.
│   │   ├── manager.py         # File-backed JSON read/write on .agent/ directory
│   │   └── migrations.py      # Schema version handling
│   └── escalation.py          # Bundle construction, API call, response parsing
├── flows/
│   ├── registry.yaml
│   ├── mission_control.yaml
│   ├── modify_file.yaml
│   ├── create_file.yaml
│   ├── run_tests.yaml
│   ├── fix_bug.yaml
│   └── ...
└── tests/
    ├── test_runtime.py
    ├── test_loader.py
    ├── test_resolvers.py
    ├── test_effects.py
    └── test_persistence.py
```

### 3.1 CLI Interface

```bash
# Mission management
uv run ouroboros.py mission create \
    --objective "Expand LLMVP's agent capabilities" \
    --working-dir /path/to/project \
    --principles "DRY" "separation of concerns" \
    --effects-profile git_managed

uv run ouroboros.py mission status
uv run ouroboros.py mission pause
uv run ouroboros.py mission resume
uv run ouroboros.py mission abort
uv run ouroboros.py mission message "Prioritize persistence layer"
uv run ouroboros.py mission history

# Agent execution
uv run ouroboros.py start                     # start on active mission
uv run ouroboros.py start --mission-id xyz    # start specific mission
```

The `start` command reads mission config, constructs the effects instance (with the LLMVP endpoint from mission config), loads the flow registry, and calls `run_agent()`. It assumes LLMVP is already running.

---

## 4. Implementation Roadmap

### Phase 1: Flow Engine Core

**Goal:** A flow runtime that loads YAML definitions, executes steps sequentially with rule-based transitions, manages the context accumulator, and reaches terminal states.

**What to build:**
- `agent/models.py` — Pydantic models: `FlowDefinition`, `StepDefinition`, `ResolverDefinition`, `StepInput`, `StepOutput`, `FlowExecution`, `FlowResult`.
- `agent/loader.py` — YAML parser producing validated `FlowDefinition` objects. Semantic validation: transition targets exist, required context keys are published upstream, entry step exists, reachable terminal states exist.
- `agent/runtime.py` — `execute_flow()`: validate inputs → initialize accumulator → loop (build StepInput → execute action → merge context_updates → resolve transition → repeat until terminal) → return FlowResult. Max step count guard against infinite loops.
- `agent/resolvers/rule.py` — Restricted `eval()` against context dict. Rules evaluated in order, first match wins.
- `agent/actions/registry.py` — Action registry: register, lookup. Actions are async callables with signature `(StepInput) -> StepOutput`.

**What to skip:** Tail calls, LLM resolvers, inference, persistence, effects interface, parallel execution.

**Test actions:** `read_files` (direct file I/O for now), `transform` (passthrough), `log_completion` (terminal).

**Verification:** A trivial test flow (read file → check condition → branch → terminal) executes correctly. Malformed flow definitions fail at load time.

**Files modified in existing code:** None. Entirely new code.

### Phase 2: Effects Interface

**Goal:** Actions interact with the outside world through a swappable interface.

**What to build:**
- `agent/effects/protocol.py` — `Effects` Protocol class with file, process, and placeholder methods.
- `agent/effects/local.py` — `LocalEffects`: real filesystem (path-scoped), real subprocess (`asyncio.create_subprocess_exec`, no shell), automatic logging wrapper.
- `agent/effects/mock.py` — `MockEffects`: canned responses from a preconfigured dict, call recording for assertions.
- Return type models in `agent/models.py`: `FileContent`, `WriteResult`, `DirListing`, `SearchResults`, `CommandResult`.

**What to skip:** `GitManagedEffects`, `DryRunEffects`, inference effects, escalation effects, persistence effects.

**Modifications to Phase 1:**
- `runtime.py` takes `effects` parameter (required). Effects instance attached to `StepInput`.
- Test actions rewritten to use `effects.read_file()` instead of direct I/O.

**Verification:** Flow reads/writes real files through effects. Same flow runs with `MockEffects`. Effects log captures all operations. Path traversal is blocked.

### Phase 3: Inference Integration

**Goal:** Flow steps can make the model think. LLM menu resolvers work.

**What to build:**
- `agent/effects/inference.py` — `InferenceEffectImpl`: constructs GraphQL queries, sends HTTP requests to LLMVP endpoint via `httpx.AsyncClient`, parses responses. Relative temperature resolution (`t*{float}` parsing).
- `agent/resolvers/llm_menu.py` — Constructs option prompt, calls `effects.run_inference()` with low temperature/short max_tokens, parses response, validates against options, retries once on invalid response.
- `agent/template.py` — Jinja2 rendering of step `prompt` fields against context dict. Separate from LLMVP's `template_engine.py` (that handles chat message formatting; this handles flow prompt rendering).
- `inference` action type: registered action that reads prompt from step definition, renders template, calls inference effect, wraps response in StepOutput.

**What to skip:** Surgeon/split execution mode, tool-augmented inference within flows.

**Modifications:**
- `runtime.py` — resolver dispatch routes to `llm_menu.resolve()` for `type: llm_menu`.
- `effects/local.py` — gains `InferenceEffectImpl` (takes LLMVP endpoint URL at construction).

**Key design note:** The inference effect is a pure HTTP client. It constructs GraphQL queries:
```python
query = """
    query Completion($request: CompletionRequest!) {
        completion(request: $request) { text tokenCount }
    }
"""
```
Ouroboros does not import anything from LLMVP.

**Verification:** A flow with an inference step gives the model a file and asks it to summarize. LLM menu resolver presents options, model picks one, flow transitions correctly. Run against live LLMVP backend.

### Phase 4: Persistence

**Goal:** Mission state, event queues, and flow artifacts survive restarts.

**What to build:**
- `agent/persistence/models.py` — `MissionState`, `TaskRecord`, `AttemptRecord`, `MissionConfig`, `Event`, `EventQueue`, `FlowArtifact`, `NoteRecord`.
- `agent/persistence/manager.py` — `PersistenceManager`: `init_agent_dir()`, `load_mission()`, `save_mission()` (atomic write via temp+rename), `read_events()`, `push_event()` (with `fcntl.flock`), `clear_events()`, `save_artifact()`, `load_artifact()`, `list_artifacts()`.
- `agent/persistence/migrations.py` — Version checking infrastructure, no migrations yet.
- `agent/effects/persistence.py` — Wires persistence methods into effects protocol.
- CLI commands: `mission create`, `mission status`, `mission pause/resume/abort`, `mission message`, `mission history`.

**Modifications:**
- `effects/protocol.py` — Persistence placeholders become real method signatures.
- `effects/local.py` — Takes `PersistenceManager`, delegates persistence calls.
- `effects/mock.py` — In-memory dict for persistence.

**Verification:** Create mission via CLI → verify `.agent/mission.json`. Push event via CLI → verify `events.json`. Flow loads/modifies/saves state → verify file reflects changes. Kill process → restart → verify state intact.

### Phase 5: Tail Calls and mission_control

**Goal:** The full agent cycle works. Mission → task selection → flow execution → tail-call → next task → completion.

**What to build:**
- `agent/tail_call.py` — `FlowTailCall`, `FlowTermination`, `FlowOutcome` models. Tail-call semantics in runtime.
- `agent/loop.py` — Thin outer process: load flows, build effects, follow tail calls until termination.
- `flows/mission_control.yaml` — Simplified initial version: load state, apply last result, process events (abort/pause only), assess (rule-based fast path), prepare dispatch, dispatch via tail-call. Plus idle and terminal states.
- `flows/modify_file.yaml` — Simplified: gather context → plan (inference + LLM menu) → execute change (inference + write) → validate (run tests) → complete/abandon. No escalation, no retry loop.
- `flows/run_tests.yaml` — Run a test command, report results.
- `ouroboros.py` — CLI entry point with `start` command.

**Modifications:**
- `runtime.py` — Returns `FlowOutcome` instead of `FlowResult`. Handles `tail_call` blocks on terminal steps.
- `models.py` — `FlowTailCall`, `FlowOutcome`, `tail_call` field on `StepDefinition`.
- `loader.py` — Validates tail-call blocks.

**Verification — the end-to-end proof:**
1. Start LLMVP with loaded model.
2. Create mission with a task.
3. `uv run ouroboros.py start`.
4. Watch: `mission_control` → loads state → finds task → dispatches `modify_file` → model reads file → model plans → model writes fix → tests run → complete → tail-call → `mission_control` → no more tasks → mission complete → agent terminates.
5. Verify file modified, `.agent/mission.json` shows task complete, artifact in `.agent/history/`.

### Phase 6: Escalation

Build escalation bundle format, `escalate_to_api` effect, system prompt construction, response parsing, frustration-gated permissions. Start with blocking calls, instructions-only responses.

### Phase 7: GitManaged Effects

`GitManagedEffects` — auto-branching, auto-commit, rollback. Working directory scoping. Point agent at a real project.

### Phase 8: Parallel Execution (Level 1)

Parallel task dispatch in `mission_control`, fan-out/fan-in in runtime, pool headroom reservation.

### Phase 9: Refinements

Full event system, GraphQL observability, idle/polling, `DryRunEffects`, self-modification lifecycle flow, multi-project support.

---

## Appendix A: mission_control Flow Definition

```yaml
flow: mission_control
version: 1
description: >
  Top-level agent routing flow. Loads mission state, processes events,
  assesses progress, selects next task, dispatches to appropriate flow.
  All child flows tail-call back here on completion.

input:
  required:
    - mission_id
  optional:
    - last_result
    - last_status
    - last_task_id

defaults:
  config:
    temperature: 0.1

steps:

  # ── Phase 1: Load persistent state ──────────────────────────

  load_state:
    action: load_mission_state
    description: "Read mission state and event queue from persistence"
    params:
      mission_id: "{{ input.mission_id }}"
    resolver:
      type: rule
      rules:
        - condition: "result.mission.status == 'active'"
          transition: apply_last_result
        - condition: "result.mission.status == 'paused'"
          transition: idle
        - condition: "result.mission.status == 'completed'"
          transition: completed
        - condition: "result.mission.status == 'aborted'"
          transition: aborted
    publishes:
      - mission
      - events
      - frustration

  # ── Phase 2: Integrate results from previous cycle ──────────

  apply_last_result:
    action: update_task_status
    description: "Apply the previous flow's outcome to mission state"
    context:
      required: [mission, frustration]
      optional: [last_result, last_status, last_task_id]
    params:
      skip_if_no_result: true
    effects:
      - save_mission
    resolver:
      type: rule
      rules:
        - condition: "result.events_pending == true"
          transition: process_events
        - condition: "result.events_pending == false"
          transition: assess
    publishes:
      - mission
      - frustration

  # ── Phase 3: Process external events ────────────────────────

  process_events:
    action: handle_events
    description: "Process user messages, escalation responses, priority changes"
    context:
      required: [mission, events]
      optional: [frustration]
    effects:
      - save_mission
      - clear_events
    resolver:
      type: rule
      rules:
        - condition: "result.abort_requested == true"
          transition: aborted
        - condition: "result.pause_requested == true"
          transition: idle
        - condition: "result.task_unblocked == true"
          transition: assess
        - condition: "true"
          transition: assess
    publishes:
      - mission
      - unblocked_tasks

  # ── Phase 4: Assess and select ──────────────────────────────

  assess:
    action: assess_mission_progress
    description: "Determine what to work on next"
    context:
      required: [mission, frustration]
      optional: [unblocked_tasks]
    resolver:
      type: rule
      rules:
        - condition: "context.unblocked_tasks and len(context.unblocked_tasks) > 0"
          transition: select_unblocked
        - condition: "result.obvious_next_task != null"
          transition: prepare_dispatch
        - condition: "result.all_tasks_complete == true"
          transition: mission_complete_check
        - condition: "result.all_remaining_blocked == true"
          transition: idle
        - condition: "true"
          transition: prioritize
    publishes:
      - assessment
      - obvious_next_task

  select_unblocked:
    action: select_from_list
    description: "Pick highest priority unblocked task"
    context:
      required: [unblocked_tasks, mission]
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: prepare_dispatch
    publishes:
      - selected_task

  prioritize:
    action: inference
    description: "LLM evaluates competing tasks and selects one"
    context:
      required: [mission, assessment, frustration]
    config:
      temperature: "t*0.8"
    prompt: |
      You are managing a coding project. Here is the current state:

      Objective: {{ context.mission.objective }}
      Principles: {{ context.mission.principles | join(', ') }}

      Progress:
      {{ context.assessment.summary }}

      Ready tasks (dependencies met, not blocked):
      {% for task in context.assessment.ready_tasks %}
      - {{ task.id }}: {{ task.description }}
        Frustration: {{ context.frustration.get(task.id, 0) }}/5
        {% if task.previous_attempts %}Last attempt: {{ task.previous_attempts[-1].summary }}{% endif %}
      {% endfor %}

      Select the task that will make the most progress toward the objective.
      Consider: dependencies unblocked by completing it, risk, frustration level.
    resolver:
      type: llm_menu
      prompt: "Which task should we work on next?"
      options_from: "context.assessment.ready_tasks"
    publishes:
      - selected_task

  # ── Phase 5: Configure and dispatch ─────────────────────────

  prepare_dispatch:
    action: configure_task_dispatch
    description: "Build input map and determine flow config for selected task"
    context:
      required: [selected_task, mission, frustration]
    params:
      frustration_thresholds:
        review: 2
        instructions: 4
        direct_fix: 5
    effects:
      - save_mission
    resolver:
      type: rule
      rules:
        - condition: "true"
          transition: dispatch
    publishes:
      - dispatch_config

  dispatch:
    action: tail_call
    description: "Hand off to the selected flow"
    context:
      required: [dispatch_config, mission]
    tail_call:
      flow: "{{ context.dispatch_config.flow }}"
      input_map: "{{ context.dispatch_config.input_map }}"
      meta:
        mission_id: "{{ input.mission_id }}"
        task_id: "{{ context.dispatch_config.task_id }}"
        frustration: "{{ context.frustration[context.dispatch_config.task_id] }}"
        escalation_permissions: "{{ context.dispatch_config.escalation_permissions }}"

  # ── Terminal / parking states ───────────────────────────────

  mission_complete_check:
    action: inference
    description: "All tasks done — verify mission objective is met"
    context:
      required: [mission, assessment]
    prompt: |
      All planned tasks are complete. Review whether the mission objective
      has been fully met, or if additional tasks should be created.

      Objective: {{ context.mission.objective }}
      Completed tasks:
      {% for task in context.mission.plan %}
      - {{ task.id }}: {{ task.summary }}
      {% endfor %}
    resolver:
      type: llm_menu
      options:
        completed:
          description: "Mission objective is fully met"
        extend:
          description: "Additional tasks needed — create a new plan"
    publishes:
      - completion_assessment

  completed:
    action: finalize_mission
    description: "Mark mission complete, generate final report"
    context:
      required: [mission, completion_assessment]
    effects:
      - save_mission
      - push_event:
          type: mission_complete
    terminal: true
    status: completed

  extend:
    action: tail_call
    description: "Mission needs more work — tail-call to planning"
    context:
      required: [mission, completion_assessment]
    tail_call:
      flow: create_plan
      input_map:
        mission_id: "{{ input.mission_id }}"
        existing_progress: "{{ context.completion_assessment }}"

  idle:
    action: enter_idle
    description: "Nothing to do — wait for events"
    params:
      poll_interval_seconds: 30
    tail_call:
      flow: mission_control
      input_map:
        mission_id: "{{ input.mission_id }}"
      delay: "{{ params.poll_interval_seconds }}"

  aborted:
    action: finalize_mission
    description: "Mission aborted by user"
    effects:
      - save_mission
    terminal: true
    status: aborted

entry: load_state

overflow:
  strategy: split
  fallback: reorganize
```

---

## Appendix B: modify_file Flow Definition

```yaml
flow: modify_file
version: 1
description: "Modify an existing file to address a specific issue"

input:
  required:
    - target_file_path
    - reason
  optional:
    - mission_excerpt
    - related_files_hint

defaults:
  config:
    temperature: 0.2
    max_tokens: 4096

steps:
  gather_context:
    action: read_files
    description: "Read target file and discover related files"
    context:
      required: []
      optional: [related_files_hint]
    params:
      target: "{{ input.target_file_path }}"
      discover_imports: true
    config:
      temperature: 0.0
    resolver:
      type: rule
      rules:
        - condition: "result.file_found == true"
          transition: plan_change
        - condition: "result.file_found == false"
          transition: escalate
    publishes:
      - target_file
      - related_files

  plan_change:
    action: inference
    description: "Analyze the issue and produce a change plan"
    context:
      required: [target_file, reason]
      optional: [related_files, mission_excerpt]
    prompt: |
      You are analyzing a file to plan a specific change.

      File: {{ context.target_file.path }}
      Content:
      {{ context.target_file.content }}

      Issue: {{ context.reason }}

      Produce a change plan. Describe what to change, where, and why.
      Assess your confidence and the risk level.
    config:
      temperature: "t*1.2"
    resolver:
      type: llm_menu
      prompt: "Given your plan, what should happen next?"
      options:
        execute_change:
          description: "Confidence is high, proceed with the change"
        gather_more_context:
          description: "Need to see more files or context before committing"
          target: gather_context
        request_review:
          description: "Uncertain — escalate plan to senior dev for review"
        abandon:
          description: "This approach won't work, return to parent flow"
          terminal: true
          status: abandoned
    publishes:
      - plan
      - risk_assessment

  execute_change:
    action: inference
    description: "Produce the modified file content"
    context:
      required: [target_file, plan]
      optional: [related_files]
    prompt: |
      Apply the following change plan to this file.
      Return the complete modified file content.

      Plan: {{ context.plan.description }}

      Original file:
      {{ context.target_file.content }}
    config:
      temperature: "t*0.3"
    effects:
      - write_file:
          path: "{{ input.target_file_path }}"
          content: "{{ result.modified_content }}"
    resolver:
      type: rule
      rules:
        - condition: "effects.write_file.success == true"
          transition: validate
        - condition: "effects.write_file.success == false"
          transition: escalate
    publishes:
      - modified_content
      - diff_summary

  validate:
    action: run_tests
    description: "Run tests to verify the change"
    context:
      required: [modified_content]
      optional: []
    params:
      scope: related
    resolver:
      type: rule
      rules:
        - condition: "result.all_passing == true"
          transition: complete
        - condition: "result.all_passing == false and meta.attempt < 3"
          transition: diagnose_failure
        - condition: "result.all_passing == false and meta.attempt >= 3"
          transition: escalate
    publishes:
      - test_results

  diagnose_failure:
    action: inference
    description: "Analyze test failure and revise the plan"
    context:
      required: [target_file, plan, test_results, modified_content]
      optional: [related_files]
    prompt: |
      The change you made caused test failures.

      Original plan: {{ context.plan.description }}
      Test output: {{ context.test_results.stdout }}

      Analyze what went wrong and produce a revised plan.
    resolver:
      type: llm_menu
      options:
        execute_change:
          description: "I see the issue, revised plan is ready"
        request_review:
          description: "This failure is beyond my ability to diagnose"
        abandon:
          description: "The original approach is flawed"
          terminal: true
          status: abandoned
    publishes:
      - plan
      - diagnosis

  escalate:
    action: build_escalation_bundle
    description: "Package context for senior dev review"
    context:
      required: [target_file, reason]
      optional: [plan, test_results, diagnosis, related_files]
    params:
      escalation_type: "{{ 'review' if context.plan else 'help' }}"
    terminal: true
    status: escalated
    publishes:
      - escalation_bundle

  complete:
    action: log_completion
    description: "Record successful modification"
    context:
      required: [modified_content, diff_summary, test_results]
    terminal: true
    status: success
    publishes:
      - summary

entry: gather_context

overflow:
  strategy: split
  fallback: reorganize
```

---

## Appendix C: Design Decisions Log

A record of every key architectural decision made during the design process, with rationale.

| Decision | Choice | Rationale |
|---|---|---|
| Flow model | Functional (pure data in/out, effects behind interface) | Reproducibility, composability, testability. Side effects isolated and swappable. |
| Flow definitions | Declarative YAML with code hooks via action registry | Separates procedure (data) from behavior (code). Flows inspectable, serializable, eventually agent-authorable. |
| Composition model | Black box sub-flows | Clean boundaries. Child internals can change without breaking parents. Same contract as function calls. |
| Agent loop | Tail-call via mission_control flow (no external loop) | Uniform execution model. No special runtime. State guaranteed valid by flow contracts. Crash recovery follows normal code path. |
| Transition resolution | Pluggable resolvers per step | Different steps need different constraint levels. Rule-based for mechanical decisions, LLM-driven for judgment calls. |
| Context overflow | Split → reorganize → summarize (no truncation) | Truncation causes unpredictable model degradation. Split preserves everything. Reorganize lets parent reassess scope. Summarize is lossy but coherent. |
| Escalation gating | Frustration counter with thresholds | Cost-escalation ladder. Cheap solutions first, expensive tools only after repeated failure. Emergent from task retry count. |
| Persistence | File-backed JSON in .agent/ directory | Single-threaded access (tail-call model). No relational needs. Agent can read own state. Atomic writes via rename. |
| LLMVP relationship | Ouroboros as pure GraphQL client | Clean separation. Separate projects. Swappable backends. LLMVP unaware of agent. |
| Language | Python | Existing ecosystem (Pydantic, asyncio, httpx). I/O-bound workload. Flow engine not performance-critical. Strict Pydantic validation substitutes for compiler. |
| Temperature | Relative (t*multiplier) and absolute supported | Cross-model portability. Flow intent (more/less creative) decoupled from model baseline. |
| Condition evaluation | Restricted eval() | Flow authors are trusted. Simple, expressive. Revisit when agent-authored flows are possible. |
| Self-modification | Dedicated lifecycle flow (duplicate → modify → validate → promote/rollback) | Safety from procedure, not from effects interface restrictions. Effects interface stays simple. |
```