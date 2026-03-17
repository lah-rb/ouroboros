# Ouroboros vs. Other Coding Agents — Architecture Comparison

*Generated from live run analysis (March 2026) and architectural review.*

---

## 1. Live Run Summary

**Mission:** "Build a Python URL shortener service" (URL model, JSON storage, CLI)

| Metric | Value |
|---|---|
| Agent cycles | 13 |
| Tasks planned & completed | 5/5 |
| Inference calls | 37 |
| Total response text | ~27K chars |
| Wall-clock time | ~4.5 minutes |
| Files created | 5 (`models/url.py`, `storage/json_storage.py`, `cli.py`, `requirements.txt`, `README.LLM.md`) |
| Learnings captured | 8 notes (lint warnings, codebase observations) |
| Validation checks | syntax compile, import check, ruff lint per file |
| Cost | ~$0 (local inference on M1 Ultra) |

**Code quality:** Docstrings on all public methods, type hints, Pydantic models with validators, argparse CLI with subcommands, proper error handling, deterministic short-code generation via SHA-256.

---

## 2. Agent Landscape

### 2.1 Agents Compared

| Agent | Developer | Model | Architecture Style |
|---|---|---|---|
| **Ouroboros** | This project | Local (Qwen 3.5 122B via LLMVP) | Declarative flow graphs + tail calls |
| **Devin** | Cognition | Claude/GPT-4 | Planner + shell-in-sandbox |
| **SWE-agent** | Princeton | Any (GPT-4, Claude) | ACI (Agent-Computer Interface) + turn loop |
| **OpenHands (ex-OpenDevin)** | UIUC/AllHands | Any | Event-stream runtime + sandboxed Docker |
| **Aider** | Paul Gauthier | Any | Edit-format chat loop + git integration |
| **Claude Code (Cline)** | Anthropic | Claude 3.5/4 | Tool-use agent loop + IDE integration |
| **Cursor Agent** | Cursor | Claude/GPT-4 | IDE-embedded + Apply model |
| **AutoCodeRover** | NUS | GPT-4 | AST-aware search → patch loop |
| **Codex CLI** | OpenAI | Codex/o3 | Sandboxed shell + multifile edits |

---

## 3. Architectural Comparison

### 3.1 Control Flow Model

| Agent | Control Flow | How Decisions Are Made |
|---|---|---|
| **Ouroboros** | **Declarative YAML flow graphs** with typed steps, rule-based and LLM-menu resolvers, tail calls for cycling. No external agent loop. | Each step has an explicit resolver (rule or LLM menu). Transitions are edges in a graph. |
| **SWE-agent** | **Imperative turn loop** — model sees observation, produces action, repeat. Fixed ACI commands (open, edit, search). | Model decides everything each turn — which command to run, when to submit. |
| **OpenHands** | **Event-stream architecture** — events trigger handlers, agents process observations and emit actions. | Agent controller manages event queue; model decides actions per observation. |
| **Aider** | **Chat loop** — user/model take turns. Model proposes edits in a structured format (unified diff, whole file, etc.). | Model proposes; Aider applies. User can accept/reject. Mostly single-shot per edit. |
| **Devin** | **Planner + executor** — high-level plan decomposed into shell commands in a sandboxed VM. | Planner creates steps; executor runs them; planner can revise on failure. |
| **Claude Code** | **Tool-use loop** — model calls tools (read_file, write_file, execute_command) in response to user task. | Model decides tool calls reactively. IDE provides tool results. |

**Ouroboros's unique position:** It's the only agent where the control flow is **data, not code**. Every other agent has its decision logic embedded in Python/TypeScript — the model is called in a loop, and the loop structure is hardcoded. Ouroboros expresses the procedure as YAML graphs that are inspectable, serializable, and eventually agent-authorable.

### 3.2 Inference Backend

| Agent | Inference | Cost Model |
|---|---|---|
| **Ouroboros** | **Local** (LLMVP → llama.cpp, Qwen 3.5 122B MoE) | Near-zero marginal cost. Hardware amortized. |
| **SWE-agent** | API (OpenAI, Anthropic) | Per-token. $0.50-15 per task depending on difficulty. |
| **OpenHands** | API (any LiteLLM-compatible) | Per-token. |
| **Aider** | API (any litellm) | Per-token. Aider tracks cost per session. |
| **Devin** | Proprietary (cloud) | Subscription ($500/mo). |
| **Claude Code** | API (Anthropic) | Per-token via API key. |
| **Cursor** | Proprietary + API | Subscription + API usage. |

**Ouroboros's unique position:** Only agent designed ground-up for **local inference**. The GraphQL separation means the inference backend is completely swappable. The flow engine's structured prompting (short, focused prompts per step) is designed for models with smaller context windows than Claude/GPT-4.

### 3.3 Task Decomposition

| Agent | How Tasks Are Broken Down |
|---|---|
| **Ouroboros** | LLM generates a structured plan (JSON array of tasks with flows, dependencies, priorities). Plan is persisted. Tasks dispatched one-by-one via tail calls. **Frustration system** gates escalation. |
| **SWE-agent** | No explicit decomposition. Model works on one issue at a time, deciding its own sub-steps turn by turn. |
| **OpenHands** | Agent can create sub-tasks. Browsing agent, coding agent, and micro-agents specialize. |
| **Aider** | No decomposition. One edit at a time, user-directed. Architect mode can plan then hand off to editor model. |
| **Devin** | Planner creates a step-by-step plan. Can revise plan on failure. |
| **AutoCodeRover** | Iterative: localize → generate patch → validate → retry. Fixed pipeline. |

**Ouroboros's unique position:** The **frustration counter** is novel — a per-task integer that gates escalation permissions. Other agents either always have access to expensive tools or never do. Ouroboros forces cheap retries first, escalation only after repeated failure.

### 3.4 Context Management

| Agent | Context Strategy |
|---|---|
| **Ouroboros** | **Context accumulator** with explicit `publishes`/`required`/`optional` declarations per step. Each step only sees what it declared. Overflow: split → reorganize → summarize (never truncate). |
| **SWE-agent** | Sliding window with custom ACI. Model sees last N observations. File content shown in 100-line windows. |
| **OpenHands** | Condensation — periodically summarizes conversation history to fit context. |
| **Aider** | **Repo map** (tree-sitter AST) for codebase overview. Selected files added to context. Smart token counting. |
| **Devin** | Full conversation in VM. Long-term memory via knowledge base. |
| **Claude Code** | Full conversation + tool results. Compaction when context fills. |

**Ouroboros's unique position:** The **per-step context scoping** is architecturally distinct. Other agents pass the entire conversation to the model. Ouroboros treats each step as a function with typed inputs — the step literally cannot see data it didn't declare interest in. This keeps prompts small and focused, critical for local models with smaller context windows.

### 3.5 Validation & Error Recovery

| Agent | Validation | Error Recovery |
|---|---|---|
| **Ouroboros** | **LLM-planned validation** — model decides which checks to run (syntax, import, lint, tests). Shared `validate_output` sub-flow. Notes lint warnings as persistent learnings. | Regenerate loop (up to 3 attempts), then abandon with frustration increment. Eventually escalate to senior dev (Claude API). |
| **SWE-agent** | Runs tests after edit. If fail, model sees error and retries. | Model decides whether to retry or give up. No structured escalation. |
| **OpenHands** | Sandboxed execution. Tests can be run. | Agent can retry. No formal escalation. |
| **Aider** | Runs linter after edits. Auto-fixes lint errors. Can run tests. | Lint auto-fix loop. User decides on test failures. |
| **AutoCodeRover** | Runs test suite after each patch. | Regenerate patch on failure. Fixed retry count. |

**Ouroboros's unique position:** The **escalation protocol** is unique — a structured bundle format (`EscalationBundle`) that packages context for a "senior developer" (Claude API). The frustration-gated permissions mean escalation is earned, not automatic.

### 3.6 Safety & Sandboxing

| Agent | Safety Model |
|---|---|
| **Ouroboros** | **Effects interface** — all side effects through a swappable protocol. `LocalEffects` for production, `MockEffects` for testing, `GitManagedEffects` for auto-commit safety. Path scoping prevents escape. Self-modification has a dedicated lifecycle flow (duplicate → modify → validate → promote/rollback). |
| **SWE-agent** | Docker container sandbox. |
| **OpenHands** | Docker sandbox with runtime. Network isolation configurable. |
| **Devin** | Full VM sandbox (cloud). |
| **Aider** | Runs on host. Git integration provides rollback. `--no-auto-commits` available. |
| **Claude Code** | Runs on host. Permission prompts for dangerous operations. |
| **Codex CLI** | Sandboxed with network disabled by default. |

**Ouroboros's unique position:** The **Effects Protocol** is the cleanest abstraction — it's not just sandboxing, it's a complete inversion of control for side effects. Actions never directly touch anything. This enables dry-run previews, full audit trails, and test mocking without any action code changes.

### 3.7 Persistence & Memory

| Agent | Persistence |
|---|---|
| **Ouroboros** | **File-backed JSON** in `.agent/` directory. Mission state, task records, event queue, flow artifacts, persistent notes. Agent can read its own state. Schema-versioned with migrations. |
| **SWE-agent** | Conversation trajectory saved. No persistent cross-session memory. |
| **OpenHands** | Event stream persisted. Can resume sessions. |
| **Aider** | Chat history in `.aider.chat.history.md`. Persistent linter settings. |
| **Devin** | Cloud-persisted sessions. Knowledge base across sessions. |
| **Claude Code** | `CLAUDE.md` project memory. Session history. |

**Ouroboros's unique position:** The **learning capture system** is distinctive — every task completion triggers a reflection step that extracts and persists observations, lint patterns, and approach notes. These notes are fed to future tasks as `relevant_notes`, creating genuine cross-task learning within a mission.

---

## 4. Key Differentiators

### What Ouroboros Does Differently

1. **Flow-as-data, not flow-as-code.** The entire agent procedure is inspectable YAML. Other agents embed their logic in Python loops.

2. **Local-first economics.** Near-zero marginal cost enables 24/7 continuous operation, aggressive retry loops, and speculative work that would be prohibitively expensive with API agents.

3. **Typed context scoping.** Steps declare their data dependencies. The runtime enforces this. Other agents dump everything into one big prompt.

4. **Frustration-gated escalation.** A graduated cost ladder — cheap retries first, expensive API calls only after repeated failure. No other agent has this economic model.

5. **Tail-call cycling.** No external agent loop. The flow graph IS the agent loop. Crash recovery follows the normal code path.

6. **Effects protocol.** Complete side-effect isolation. Not just sandboxing — a protocol that enables dry-run, mock, git-managed, and real modes with zero action code changes.

7. **Self-reflective learning.** Persistent notes from every task feed into future tasks. The agent accumulates project-specific knowledge.

### Where Ouroboros Trades Off

1. **Model capability.** Local Qwen 3.5 122B is strong but not Claude 3.5 Sonnet-level for complex reasoning. The architecture compensates with structured prompting and small focused steps.

2. **Context window.** Local models have smaller context than frontier APIs. The per-step scoping is a feature born of necessity.

3. **No interactive debugging.** SWE-agent and OpenHands can interactively explore code. Ouroboros follows predefined flow paths.

4. **Startup complexity.** Requires LLMVP server, model loading. API agents just need a key.

5. **Single-machine.** Currently runs on one Apple Silicon machine. API agents scale to cloud.

---

## 5. Competitive Assessment

### For the Target Use Case (24/7 autonomous coding on Apple Silicon)

| Capability | Ouroboros | Best Alternative |
|---|---|---|
| Cost for 24/7 operation | **~$0/month** (electricity only) | Aider: ~$300-1000/mo API costs |
| Autonomous mission execution | **Yes** (flow-driven) | Devin: Yes ($500/mo) |
| Self-healing on failure | **Yes** (frustration + escalation) | SWE-agent: retry loop only |
| Code quality assurance | **Yes** (LLM-planned validation) | Aider: lint auto-fix |
| Cross-task learning | **Yes** (persistent notes) | None comparable |
| Reproducibility | **Yes** (deterministic flows + effects logs) | None comparable |
| Sandboxed by design | **Yes** (Effects protocol) | Codex CLI, OpenHands |

### Bottom Line

Ouroboros occupies a unique niche: **a fully autonomous, flow-driven agent optimized for local inference economics.** No other agent combines declarative flow graphs, typed context management, frustration-gated escalation, and zero-cost continuous operation. The closest alternatives — SWE-agent and OpenHands — are API-dependent and lack Ouroboros's structured decomposition and learning systems. Devin is the closest in autonomy but is cloud-only and expensive.

The trade-off is clear: Ouroboros sacrifices raw model capability for economic sustainability, compensating with architectural rigor that keeps the local model operating within its competence zone.
