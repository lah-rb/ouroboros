# Ouroboros — Guide for AI Developers and Agents

> **CRITICAL: Read this file completely before making any changes to the codebase.**
> This file MUST remain in context at all times. Do not compact or purge it.

Ouroboros is a flow-driven autonomous coding agent backed by LLMVP local inference.
It is a **pure GraphQL client** of LLMVP — a separate project with its own repository.
All inference requests go through LLMVP's GraphQL API over HTTP.

---

## Critical Rules

- **Pure GraphQL client** — Ouroboros NEVER imports Python modules from LLMVP. All inference goes through HTTP to `localhost:8000/graphql`. If you feel tempted to `from llmvp import ...`, stop — that is always wrong.
- **uv for everything** — NEVER call `python`, `pip`, or `pip install` directly. Always prefix with `uv run` or use `uv add`.
- **Effects interface for all side effects** — Actions never directly touch filesystem, network, or subprocess. Always go through the `effects` parameter on `StepInput`.
- **Pydantic v2 strict validation** — All data models use Pydantic v2. No raw dicts for structured data crossing boundaries.
- **Async callables** — All actions have signature `async (StepInput) -> StepOutput`. No exceptions.
- **CUE for flow logic, Python for behavior** — Flow structure (steps, transitions, context) lives in CUE. Action implementation lives in Python. Never mix.
- **Use `black` for formatting** — Run `uv run black .` before every test run.

---

## Quick Reference — What to Touch

| If the task involves... | Start here | Also check |
|------------------------|------------|------------|
| Flow logic (step order, transitions, routing) | `flows/cue/*.cue` | Rebuild with `uv run ouroboros.py cue-compile` |
| New action behavior | `agent/actions/` | Register in `agent/actions/registry.py` |
| Prompt wording for local model | `prompts/<flow>/<step>.yaml` | `PROMPTING_CONVENTIONS.md` for standards |
| Step templates (reusable step configs) | `flows/cue/templates.cue` | `agent/loader_v2.py` (merge logic) |
| Data models or schemas | `agent/models.py` or `agent/persistence/models.py` | |
| How flows are loaded/validated | `agent/loader_v2.py` | |
| Template rendering (Jinja2 for prompts) | `agent/template.py` | |
| `$ref` resolution (structural fields) | `agent/loader_v2.py` | `flows/cue/flow.cue` (`#Ref` schema) |
| Pre-compute formatters / result formatters | `agent/formatters.py` | `flows/cue/prompt.cue` (formatter registry) |
| LLMVP inference integration | `agent/effects/inference.py` | |
| Mission state / persistence | `agent/persistence/` | |
| Resolver logic (rule or LLM menu) | `agent/resolvers/` | |
| The agent loop / tail calls | `agent/loop.py`, `agent/tail_call.py` | |
| Mission CLI commands | `ouroboros.py` | |
| Mission YAML config | `agent/mission_config.py` | `ouroboros.py` |
| Runtime tracing / trace events | `agent/trace.py`, `agent/trace_cli.py` | |
| Architecture / design decisions | `IMPLEMENTATION.md` | |
| Prompt quality / conventions | `PROMPTING_CONVENTIONS.md` | `dev/claude-skill-writing-patterns.md` |
| Static analysis of flow contracts | `dev/lint_flows.py` | `uv run ouroboros.py lint-flows` |
| Tests | `tests/test_contracts.py` | `dev/smoke_test.py` via `uv run ouroboros.py smoke` |

---

## CLI Reference (`ouroboros.py`)

The CLI is the single entry point for all operations. Always invoke via `uv run ouroboros.py <command>`.

### Agent Execution

```bash
# Start the agent on an existing mission
uv run ouroboros.py start --working-dir /path [--max-cycles 50] [-v]
                         [--trace-thinking] [--trace-prompts]

# All-in-one: load YAML config, run pre_create hooks, create mission, run post_create hooks
uv run ouroboros.py mission create --mission_config game_challenge
```

| Flag | Description |
|------|-------------|
| `--working-dir` | Project working directory (default: cwd) |
| `--max-cycles` | Safety limit on flow executions (default: 50) |
| `-v` / `--verbose` | Debug-level logging |
| `--trace-thinking` | Capture chain-of-thought from LLMVP thinking endpoint |
| `--trace-prompts` | Capture full rendered prompts and raw responses in traces |

### Mission Management

```bash
uv run ouroboros.py mission create --mission_config <n>   # from YAML config
uv run ouroboros.py mission create --objective "..." [opts]   # from CLI flags
uv run ouroboros.py mission status  [--working-dir /path]
uv run ouroboros.py mission pause   [--working-dir /path]
uv run ouroboros.py mission resume  [--working-dir /path]
uv run ouroboros.py mission abort   [--working-dir /path]
uv run ouroboros.py mission message "text" [--working-dir /path]
uv run ouroboros.py mission history [--working-dir /path]
```

**`mission create` flags:**

| Flag | Description |
|------|-------------|
| `--mission_config` | YAML config name or path (e.g. `test` loads `test.yaml`) |
| `--objective` | Mission objective (required unless in YAML config) |
| `--working-dir` | Project working directory |
| `--principles` | Guiding principles (space-separated) |
| `--tasks` | Initial task descriptions (space-separated) |
| `--effects-profile` | `local`, `git_managed`, or `dry_run` |
| `--llmvp-endpoint` | LLMVP GraphQL endpoint URL |

When `--mission_config` is combined with CLI flags, CLI flags win.

### Development Tools

```bash
# Validate CUE schemas and compile flows to flows/compiled.json
uv run ouroboros.py cue-compile

# Structural lint (entry points, transition targets)
uv run ouroboros.py lint [--flow <n>] [--verbose]

# Comprehensive flow contract linter (action contracts, publish/consume, cycles)
uv run ouroboros.py lint-flows [--verbose] [--compiled <path>]

# Smoke test: load all flows, execute first 3 steps with MockEffects
uv run ouroboros.py smoke

# View runtime trace summaries
uv run ouroboros.py trace [--mission <id>] [--format summary|detail]
                         [--output <path>] [--working-dir /path]

# Generate architectural blueprint (Markdown and/or PDF)
uv run ouroboros.py blueprint [--format pdf|md] [--output <dir>]

# Visualize flow definitions as diagrams
uv run ouroboros.py visualize [flow_name] [--format mermaid|dot]
                              [--output <file>] [--svg <file>] [--detailed]
```

---

## Mission YAML Config (`--mission_config`)

Instead of passing many CLI flags, declare a mission in a YAML file:

```bash
uv run ouroboros.py mission create --mission_config test            # loads test.yaml
uv run ouroboros.py mission create --mission_config ./configs/deploy.yaml
uv run ouroboros.py mission create --mission_config test --working-dir /other  # CLI overrides YAML
```

### YAML Schema

```yaml
objective: "Build a REST API for user management"   # required

working_dir: "."                                     # default: cwd
effects_profile: local                               # local | git_managed | dry_run
llmvp_endpoint: "http://localhost:8000/graphql"

principles:
  - "Keep functions small and focused"
tasks:
  - "Create user model in models/user.py"

pre_create:                                          # shell commands before mission creation
  - "rm -rf /tmp/project && mkdir /tmp/project"
post_create:                                         # shell commands after mission creation
  - "uv run ouroboros.py start --working-dir /tmp/project"
```

Both `pre_create` and `post_create` run in the invoking cwd (not `working_dir`), fail-fast on non-zero exit, and are printed-but-not-executed when `effects_profile: dry_run`.

**Implementation:** `agent/mission_config.py` (`MissionYAMLConfig`, `load_mission_config()`, `run_lifecycle_commands()`).

---

## Package Management

**CRITICAL: This project uses `uv` exclusively.**

```bash
# ❌ WRONG                          # ✅ CORRECT
pip install httpx                    uv add httpx
python -m pytest tests/              uv run pytest tests/ -v
python ouroboros.py start            uv run ouroboros.py start
```

---

## Development Cycle

**ALWAYS follow this sequence when making changes:**

1. **Test** → `uv run pytest tests/ -v`
   - Run the full suite first. Only narrow with `-k "test_name"` after full suite passes.
   - CRITICAL: If any tests fail, stop, investigate, fix, then resume the original objective.

2. **Code**

3. **Format** → `uv run black .`
   - Do NOT skip — Black reformatting can change line numbers that affect test assertions.

4. **Test** → `uv run pytest tests/ -v`

5. **Compile flows** → `uv run ouroboros.py cue-compile` (if CUE files changed)

6. **Smoke test** → `uv run ouroboros.py smoke` (for flow/action changes)

7. **Verify live** → `uv run ouroboros.py mission create --mission_config <test_config>`
   - The above command is all-in-one: cleans, creates, and starts the agent.
   - Requires a running LLMVP server.

8. **If tests fail** → Read the error output, fix the specific failure, return to step 1.

**IMPORTANT:** Steps 5-7 are not optional for feature work. Unit tests passing does not guarantee the agent cycle works end-to-end.

---

## LLMVP Backend Server

Ouroboros requires a running LLMVP server for live testing. The LLMVP project lives in `llmvp/` within this repo.

### Starting / Stopping

```bash
cd ouroboros/llmvp
uv run llmvp.py --backend     # starts GraphQL API, waits for pool ready
uv run llmvp.py --stop        # stops the backend
```

### Health Check / Test Completion

```bash
curl -X POST http://localhost:8000/graphql \
  -H "Content-Type: application/json" \
  -d '{"query": "query { health { status poolSize availableInstances } }"}'

curl -X POST http://localhost:8000/graphql \
  -H "Content-Type: application/json" \
  -d '{"query": "query { completion(request: { prompt: \"Hello!\", maxTokens: 50, temperature: 0.7 }) { text tokensGenerated finished } }"}'
```

### Static Knowledge (Universal Context)

LLMVP pre-tokenizes a knowledge base file into a binary token buffer (`data/<model>.tokens.bin`) at preprocessing time. At server startup, this buffer is memory-mapped and prepended to every inference call as a static prefix. The model evaluates these tokens once on first use, and the KV cache snapshot is reused for all subsequent calls — making the universal context effectively free at inference time.

The knowledge base is composed from a model-specific wrapper template (`llmvp/knowledge/<model>-wrapper.txt`) that includes `SOUL.md` via `{{ouroboros_universal_prompt.md}}` placeholder substitution. To update the universal context after editing the soul or knowledge files:

```bash
cd ouroboros/llmvp
uv run python preprocessing/cli.py    # re-tokenize knowledge base
uv run llmvp.py --stop && uv run llmvp.py --backend   # restart to reload
```

This architecture means the soul is always present in every inference call with zero marginal latency or token cost. Per-step prompt templates compose the dynamic portion of each call on top of this static prefix.

---

## Architecture Quick Reference

For full architectural design, see `IMPLEMENTATION.md`. Key concepts:

- **Flow engine** — Declarative CUE flow definitions compiled to JSON. Steps have typed I/O, explicit transitions, and context scoping.
- **Context Contract Architecture** — Every flow declares a `context_tier` (mission_objective → project_goal → flow_directive → session_task), `returns` (structured output at termination), and `state_reads` (persistence paths loaded). The runtime enforces tier boundaries at dispatch time.
- **Context accumulator** — Data flows between steps via `publishes` + `context.required/optional`. Each step only sees what it declared.
- **Goals** — Project-level objectives derived from architecture. The director reasons at the goal level, not the task level.
- **Resolvers** — Rule-based (restricted `eval()`, no builtins) or LLM menu (GBNF grammar-constrained choice, supports `publish_selection`).
- **Tail calls** — Replace an external agent loop. Child flows tail-call back to `mission_control`, creating a continuous cycle. Structured returns (not prose) flow back as `last_result`.
- **Effects interface** — All side effects (file I/O, subprocess, inference, persistence, terminal sessions, tracing) go through a swappable protocol. `LocalEffects` for production, `MockEffects` for testing.
- **Frustration system** — Per-task counter that gates escalation. Cheap retries first, expensive escalation only after repeated failure.
- **`$ref` resolution** — Typed references in structural fields (params, input_map, tail_call) resolved at runtime by `loader_v2.py`. Replaces Jinja2 `{{ }}` in flow definitions.
- **Prompt templates** — Section-based YAML files in `prompts/` referenced by CUE flows via `prompt_template`. Pre-compute formatters handle complex data formatting.

### Flow Inventory (18 flows, 169 steps, 61 actions)

**Orchestrator / Planning:**

| Flow | Tier | Steps | Purpose |
|------|------|-------|---------|
| `mission_control` v5 | project_goal | 30 | Core director — load state → integrate result → reason → select task → dispatch |
| `design_and_plan` v4 | mission_objective | 17 | Design/reconcile architecture, derive goals, generate plan |
| `revise_plan` v3 | project_goal | 6 | Add/reorder/remove tasks based on new observations |
| `retrospective` v5 | project_goal | 5 | Capture learnings from frustration recovery |

**Task Flows (dispatched by mission_control):**

| Flow | Tier | Steps | Purpose |
|------|------|-------|---------|
| `file_ops` v1 | flow_directive | 18 | File lifecycle — routes to create/patch/rewrite, validates, self-corrects |
| `diagnose_issue` v4 | flow_directive | 9 | Deep issue investigation without modifying files |
| `interact` v2 | flow_directive | 7 | Run the software, test features, observe behavior |
| `project_ops` v4 | flow_directive | 7 | Project infrastructure — deps, config, directory structure |

**Sub-flows (invoked via `action: flow`):**

| Flow | Tier | Steps | Invoked By |
|------|------|-------|------------|
| `create` v1 | session_task | 7 | `file_ops` — generate new file |
| `patch` v1 | session_task | 10 | `file_ops` — AST-parsed surgical symbol editing |
| `rewrite` v1 | session_task | 6 | `file_ops` — complete file replacement |
| `run_commands` v1 | session_task | 4 | `quality_gate` — batch command execution |
| `run_session` v1 | session_task | 7 | `interact`, `quality_gate` — multi-turn terminal session |
| `prepare_context` v3 | session_task | 8 | Most task flows — scan workspace, build repo map |
| `quality_gate` v5 | mission_objective | 12 | `mission_control` — structural + behavioral validation |
| `research` v2 | session_task | 6 | `design_and_plan`, `mission_control` — search and summarize |
| `capture_learnings` v3 | session_task | 5 | `retrospective` — reflect and persist observations |
| `set_env` v2 | session_task | 5 | `file_ops` — detect validation tooling |

---

## Development Conventions

- **temperature** — prefer `t*` specifiers: `t*0.5` for deterministic, `t*1` for balanced, `t*1.2` for creative.
- **Pydantic v2** with strict validation for all models.
- **Declarative CUE** for flow definitions — never Python code for flow structure. Compile with `uv run ouroboros.py cue-compile`.
- **Async callables** with signature `(StepInput) -> StepOutput` for all actions.
- **Effects interface** for all side effects — actions never directly touch filesystem/network.
- **`$ref` references** for structural data plumbing in flows; **Jinja2** only in `agent/template.py` for prompt rendering.
- **Restricted `eval()`** for resolver conditions — no builtins, only context namespace.
- **Prompt conventions** — See `PROMPTING_CONVENTIONS.md` for runtime prompt standards.

---ALWAYS CONSULT PROMPTING_CONVENTIONS.md WHEN ADDING OR MODIFYING ANY PROMPT INSTRUCTIONS---

> **CRITICAL: Read this file completely before making any changes to the codebase.**
> This file MUST remain in context at all times. Do not compact or purge it.
