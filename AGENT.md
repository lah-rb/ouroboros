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
- **YAML for flow logic, Python for behavior** — Flow structure (steps, transitions, context) lives in YAML. Action implementation lives in Python. Never mix.
- **Use `black` for formatting** — Run `uv run black .` before every test run.

---

## Quick Reference — What to Touch

| If the task involves... | Start here | Also check |
|------------------------|------------|------------|
| Flow logic (step order, transitions, routing) | `flows/*.yaml`, `flows/**/*.yaml` | `tests/test_runtime.py` |
| New action behavior | `agent/actions/` | Register in `agent/actions/registry.py`, test in `tests/test_new_actions.py` |
| Prompt wording for local model | `flows/**/*.yaml` prompt blocks | `PROMPTING_CONVENTIONS.md` for standards |
| Step templates (reusable step configs) | `flows/shared/step_templates.yaml` | `agent/loader.py` (merge logic), `tests/test_templates.py` |
| Data models or schemas | `agent/models.py` or `agent/persistence/models.py` | `tests/test_models.py`, `tests/test_persistence.py` |
| How flows are loaded/validated | `agent/loader.py` | `tests/test_loader.py` |
| Template rendering (Jinja2) | `agent/template.py` | `tests/test_template.py` |
| LLMVP inference integration | `agent/effects/inference.py` | `tests/test_inference.py` |
| Mission state / persistence | `agent/persistence/` | `tests/test_persistence.py` |
| Resolver logic (rule or LLM menu) | `agent/resolvers/` | `tests/test_resolvers.py`, `tests/test_llm_menu.py` |
| The agent loop / tail calls | `agent/loop.py`, `agent/tail_call.py` | `tests/test_runtime.py` |
| Mission CLI commands | `ouroboros.py` | — |
| Mission YAML config | `agent/mission_config.py` | `ouroboros.py`, `tests/test_mission_config.py` |
| Architecture / design decisions | `IMPLEMENTATION.md` | `REFINEMENT.md`, `greenfield.md` |
| Prompt quality / conventions | `PROMPTING_CONVENTIONS.md` | `claude-skill-writing-patterns.md` |

---

## Mission YAML Config (`--mission_config`)

Instead of passing many CLI flags, you can declare a mission in a YAML file and load it with `--mission_config`:

```bash
# Load test.yaml from the current directory
uv run ouroboros.py mission create --mission_config test

# Load from an explicit path
uv run ouroboros.py mission create --mission_config ./configs/deploy.yaml

# CLI flags override YAML values
uv run ouroboros.py mission create --mission_config test --working-dir /other/path
```

### YAML Schema

```yaml
# ── Required ──────────────────────────────────────────────
objective: "Build a REST API for user management"

# ── Optional settings ─────────────────────────────────────
working_dir: "."                                    # default: current directory
effects_profile: local                              # local | git_managed | dry_run
llmvp_endpoint: "http://localhost:8000/graphql"     # default LLMVP endpoint

principles:                                         # guiding principles for the agent
  - "Keep functions small and focused"
  - "Write tests for all public APIs"

tasks:                                              # initial task descriptions
  - "Create user model in models/user.py"
  - "Add CRUD endpoints in routes/users.py"

# ── Lifecycle commands ────────────────────────────────────
# Both run in the invoking cwd (not working_dir).
# Fail-fast: if any command exits non-zero, the process aborts.

pre_create:                                         # before mission creation
  - "rm -rf /tmp/project && mkdir /tmp/project"     #   wipe & recreate workspace
  - "rm -f llmvp/logs/interactions.jsonl"           #   clean inference logs

post_create:                                        # after mission creation
  - "uv run ouroboros.py start --working-dir /tmp/project"  # start the agent
```

### Field Reference

| Field | Type | Required | Default | CLI equivalent |
|-------|------|----------|---------|----------------|
| `objective` | string | **yes** | — | `--objective` |
| `working_dir` | string | no | `"."` | `--working-dir` |
| `effects_profile` | `local` \| `git_managed` \| `dry_run` | no | `local` | `--effects-profile` |
| `llmvp_endpoint` | string | no | `http://localhost:8000/graphql` | `--llmvp-endpoint` |
| `principles` | list[string] | no | `[]` | `--principles` |
| `tasks` | list[string] | no | `[]` | `--tasks` |
| `pre_create` | list[string] | no | `[]` | *(YAML only)* |
| `post_create` | list[string] | no | `[]` | *(YAML only)* |

### Resolution Order

When `--mission_config` is combined with CLI flags, CLI flags win:
1. Load YAML config → provides base values
2. Apply CLI overrides → any explicit flag replaces the YAML value
3. Apply defaults → fill remaining gaps with built-in defaults

### Lifecycle Commands

Two command phases bracket mission creation:

**`pre_create`** — Runs **before** mission creation
- Typical use: wipe working directory, clean logs, prepare workspace
- Runs in the **invoking cwd** (not `working_dir` — it may not exist yet)
- **Fail-fast**: first non-zero exit aborts `mission create`
- Stdout captured and displayed inline

**`post_create`** — Runs **after** mission creation
- Typical use: start the agent, run setup scripts that need mission.json
- Runs in the **invoking cwd**
- Output streams directly to terminal (supports long-running processes)
- **Fail-fast**: first non-zero exit stops remaining commands

Both phases:
- Executed via shell (`subprocess.run(cmd, shell=True)`)
- When `effects_profile: dry_run`, commands are printed but not executed

### Implementation

- **Model**: `agent/mission_config.py` → `MissionYAMLConfig` (Pydantic v2)
- **Loader**: `agent/mission_config.py` → `load_mission_config()`, `resolve_config_path()`
- **Lifecycle**: `agent/mission_config.py` → `run_lifecycle_commands()`
- **CLI integration**: `ouroboros.py` → `cmd_mission_create()`
- **Tests**: `tests/test_mission_config.py`

---

## Package Management

**CRITICAL: This project uses `uv` exclusively.**

❌ WRONG:
```bash
pip install httpx
python -m pytest tests/
python ouroboros.py start
```

✅ CORRECT:
```bash
uv add httpx              # add a dependency
uv remove httpx           # remove a dependency
uv run pytest tests/ -v   # run tests
uv run ouroboros.py start  # run the agent
uv run black .            # format code
```

`uv` manages the virtual environment, dependency resolution, and Python version. Direct `pip`/`python` calls bypass this and create broken environments.

---

## Development Cycle

**ALWAYS follow this sequence when making changes:**
1. **Test** → `uv run pytest tests/ -v`
   - Run the full suite first. Only narrow with `-k "test_name"` after full suite passes.
   - CRITICAL: If any tests fail, stop the current object, investigate the issues, then plan and execute the fix. Only after all the tests pass should the origina objective be resumed.

2. **Code**

3. **Format** → `uv run black .`
   - ⚠️ Do NOT skip this — Black reformatting can change line numbers that affect test assertions.

4. **Test** → `uv run pytest tests/ -v`
   - Run the full suite first. Only narrow with `-k "test_name"` after full suite passes.
   - Specific file: `uv run pytest tests/test_runtime.py -v`
   - Specific test: `uv run pytest tests/ -v -k "test_name"`

5. **Verify live** → `uv run ouroboros.py mission create --mission_config test_config`
   - Run the actual product for feature additions and possible breaks.
   - The above command is an all in one. It cleans, creates, and starts the test. It requires no additional commands to deliver test the agent live.

6. **If tests fail** → Read the error output, fix the specific failure, return to step 1.

**IMPORTANT:** Step 3 is not optional for feature work. Unit tests passing does not guarantee the agent cycle works end-to-end.

---

## LLMVP Backend Server

Ouroboros requires a running LLMVP server for inference. The LLMVP project lives in `../llmvp/` (gitignored from this repo).

### Starting the Backend
```bash
cd ouroboros/llmvp
uv run llmvp.py --backend     # GraphQL API — waits for pool ready
```

You will see `✅ Server started in background` and `✅ Llama pool ready (2 instances)` when ready.

### Stopping the Backend
```bash
cd ouroboros/llmvp
uv run llmvp.py --stop
```

### Health Check
```bash
curl -X POST http://localhost:8000/graphql \
  -H "Content-Type: application/json" \
  -d '{"query": "query { health { status poolSize availableInstances } }"}'
```

### Test Completion
```bash
curl -X POST http://localhost:8000/graphql \
  -H "Content-Type: application/json" \
  -d '{
    "query": "query { completion(request: { prompt: \"Hello!\", maxTokens: 50, temperature: 0.7 }) { text tokensGenerated finished } }"
  }'
```

### Model Context
**IMPORTANT** While context is dependant to the specific running model, modern local models frequently support 128k, 256k and 1M context lengths natively. This being said, context management is still important as large contexts can slow models and long contexts often have poorer understanding for both local and API models.

---

## Architecture Quick Reference

For full architectural design, see `IMPLEMENTATION.md`. Key concepts:

- **Flow engine** — Declarative YAML graphs. Steps have typed I/O, explicit transitions, and context scoping.
- **Context accumulator** — Data flows between steps via `publishes` + `context.required/optional`. Each step only sees what it declared.
- **Resolvers** — Rule-based (restricted `eval()`, no builtins) or LLM menu (constrained choice from named options).
- **Tail calls** — Replace an external agent loop. Child flows tail-call back to `mission_control`, creating a continuous cycle.
- **Effects interface** — All side effects (file I/O, subprocess, inference, persistence) go through a swappable protocol. `LocalEffects` for production, `MockEffects` for testing.
- **Frustration system** — Per-task counter that gates escalation. Cheap retries first, expensive escalation only after repeated failure.
- **Template interpolation** — Jinja2 syntax: `{{ input.x }}`, `{{ context.y }}`, `{{ meta.flow_name }}`.

---

## Development Conventions
- **temperature** when making new flow prefer "t*" specifiers like "t*0.5" for deterministic flows, "t*1" for most tasks (optimized creativity and determinism), or "t*1.2" for creative tasks.
- **Pydantic v2** with strict validation for all models
- **Declarative YAML** for flow definitions — never Python code for flow structure
- **Async callables** with signature `(StepInput) -> StepOutput` for all actions
- **Effects interface** for all side effects — actions never directly touch filesystem/network
- **Jinja2 templates** for prompt and param interpolation
- **Restricted `eval()`** for resolver conditions — no builtins, only context namespace
- **Prompt conventions** — See `PROMPTING_CONVENTIONS.md` for runtime prompt standards
---ALWAYS CONSULT PROMPTING_CONVENTIONS.md WHEN ADDING OR MODIFYING ANY PROMPT INSTRUCTIONS---

> **CRITICAL: Read this file completely before making any changes to the codebase.**
> This file MUST remain in context at all times. Do not compact or purge it.
