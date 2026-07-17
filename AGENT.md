# Ouroboros — Guide for AI Developers and Agents

> **CRITICAL: Read this file completely before making any changes to the codebase.**
> This file MUST remain in context at all times. Do not compact or purge it.

Ouroboros is a flow-driven autonomous coding agent backed by LLMVP local inference.
It is a **pure GraphQL client** of LLMVP — a separate project with its own repository.
All inference requests go through LLMVP's GraphQL API over HTTP.

---

## Critical Rules

- **Pure GraphQL client** — Ouroboros NEVER imports Python modules from LLMVP. All inference goes through HTTP to `localhost:8008/graphql`. If you feel tempted to `from llmvp import ...`, stop — that is always wrong.
- **uv for everything** — NEVER call `python`, `pip`, or `pip install` directly. Always prefix with `uv run` or use `uv add`.
- **Effects interface for all side effects** — Actions never directly touch filesystem, network, or subprocess. Always go through the `effects` parameter on `StepInput`.
- **Pydantic v2 strict validation** — All data models use Pydantic v2. No raw dicts for structured data crossing boundaries.
- **Async callables** — All actions have signature `async (StepInput) -> StepOutput`. No exceptions.
- **CUE for flow logic, Python for behavior** — Flow structure (steps, transitions, context) lives in CUE. Action implementation lives in Python. Never mix.
- **Use `black` for formatting** — Run `uv run black .` before every test run.
- **Use `ruff` for linting** — Run `uv run ruff check .` alongside `black`. Auto-fix the mechanical categories with `uv run ruff check --fix`. The rules `F401` (unused imports), `F821` (undefined names), `F841` (unused locals), `E402` (import order), and `E741` (ambiguous names) catch real wiring bugs, not just style issues — do not disable them locally to silence a warning, fix the underlying issue.
- **Never truncate or cap LLM output** — Do NOT apply character limits, line caps, `[:N]` slicing, or `max_tokens` ceilings to content that will be parsed downstream (JSON responses, verification results, plan arrays, goal lists). Truncated JSON is unparseable JSON. If you encounter existing truncation in a parsing path, report it as a bug. Display-only contexts (trace logs, observation strings, UI summaries) may use truncation for readability, but anything that feeds into `json.loads`, `json_repair`, or structured extraction must receive the complete response.
- **Never ask for bare text responses** — All machine-parsed model output must use fenced JSON (```` ```json ```` blocks) or the `{"choice": "..."}` menu format. Never prompt the model to respond with a bare string, file path, or unstructured text that will be parsed programmatically. Small/sparse models fail at bare text compliance ~50% of the time. Use `parse_llm_json()` from `agent/llm_json.py` for all extraction. See `PROMPTING_CONVENTIONS.md` §3 for the fenced JSON protocol.

---

## Project Character

Ouroboros is **unpublished**. There are no downstream users, no compatibility guarantees, and no migration windows to honor. This has direct implications for how changes should be made:

- **Prefer clean-break, big-bang transitions over gradual migrations.** When a concept is being replaced, delete the old version in the same change that introduces the new one. Do not leave "kept for backward compatibility" shims, dual-path code, or feature flags guarding deprecated behavior — if the old path is dead, remove it fully. Comments marking code as "legacy" or "for migration" are an anti-pattern here; they become lies the moment the migration completes and noise that obscures what the code actually does.
- **Expect cleanup as part of every feature change.** A feature change is not complete until its footprint is clean: unused imports are removed, superseded code paths are deleted, stale documentation is updated, orphan prompts/flows/actions are pruned, and lint categories pass cleanly. Lint, smoke, and the CLI smoke are the verification fence — if any of them regress, the change isn't done. The lint tooling (`ouroboros.py lint` and `ouroboros.py lint-flows`) is calibrated to surface this kind of drift; treat new warnings as defects to resolve, not noise to tolerate.
- **Favor consolidation over proliferation.** When two pieces of code do similar things, collapse them rather than adding a third. When a concept has grown three call sites, extract it. When a helper is only used once and the caller is clear, inline it. The goal is a codebase that stays small enough for a single reader to hold in their head.

These norms apply to AI-directed changes as much as to human ones. If a change appears to be "too large" because it spans feature code, flow definitions, prompts, and docs — that is usually the right size, not a red flag.

---

## Quick Reference — What to Touch

| If the task involves... | Start here | Also check |
|------------------------|------------|------------|
| Flow logic (step order, transitions, routing) | `flows/<set>/*.cue` (`shared/` + one dir per flow set, e.g. `code_core/`) | Rebuild with `uv run ouroboros.py cue-compile` |
| Mission types / flow sets (entry flow, phase order) | `agent/flow_sets.py` (registry) | IMPLEMENTATION.md §3.2 "Flow Sets" |
| New action behavior | `agent/actions/` | Register in `agent/actions/registry.py` |
| Prompt wording for local model | `prompts/<flow>/<step>.yaml` | `PROMPTING_CONVENTIONS.md` for standards |
| Step templates (reusable step configs) | `flows/shared/templates.cue` | `agent/loader.py` (merge logic) |
| Data models or schemas | `agent/models.py` or `agent/persistence/models.py` | |
| How flows are loaded/validated | `agent/loader.py` | |
| Template rendering (Jinja2 for prompts) | `agent/template.py` | |
| `$ref` resolution (structural fields) | `agent/loader.py` | `flows/shared/flow.cue` (`#Ref` schema) |
| Pre-compute formatters / result formatters | `agent/formatters.py` | `flows/shared/prompt.cue` (formatter registry) |
| LLMVP inference integration | `agent/effects/inference.py` | |
| Mission state / persistence | `agent/persistence/` | |
| Resolver logic (rule or LLM menu) | `agent/resolvers/` | |
| The agent loop / tail calls | `agent/loop.py`, `agent/tail_call.py` | |
| Mission CLI commands | `ouroboros.py` | |
| Mission YAML config | `agent/mission_config.py` | `ouroboros.py` |
| Runtime tracing / trace events | `agent/trace.py`, `agent/trace_cli.py` | |
| Architecture / design decisions | `IMPLEMENTATION.md` | |
| Prompt quality / conventions | `PROMPTING_CONVENTIONS.md` | |
| Static analysis of flow contracts | `agent/flow_lint.py` | `uv run ouroboros.py lint-flows` |
| Smoke testing | `dev/smoke_test.py` | `uv run ouroboros.py smoke` |
| CLI import-rot gate | `dev/cli_smoke.py` | `uv run ouroboros.py cli-smoke` |
| Test philosophy / structure rules | `TESTING.md` | |
| Deferred-work delegation brief | `OPEN_TASKS.md` | |

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
uv run ouroboros.py mission reopen  [--working-dir /path]  # re-activate a completed/aborted mission
uv run ouroboros.py mission abort   [--working-dir /path]
uv run ouroboros.py mission message "text" [--working-dir /path]
uv run ouroboros.py mission history [--working-dir /path]
```

**`mission create` flags:**

| Flag | Description |
|------|-------------|
| `--mission_config` | YAML config name or path (bare names resolve in cwd, then `missions/`) |
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
```

---

## Mission YAML Config (`--mission_config`)

Instead of passing many CLI flags, declare a mission in a YAML file:

```bash
uv run ouroboros.py mission create --mission_config game_challenge   # missions/game_challenge.yaml
uv run ouroboros.py mission create --mission_config ./missions/ops_demo.yaml
uv run ouroboros.py mission create --mission_config game_challenge --working-dir /other  # CLI overrides YAML
```

### YAML Schema

```yaml
objective: "Build a REST API for user management"   # required

working_dir: "."                                     # default: cwd
effects_profile: local                               # local | git_managed | dry_run
llmvp_endpoint: "http://localhost:8008/graphql"

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

## External Services

Ouroboros composes with external services through the MCP (Model Context Protocol) pattern. Each service is a subprocess launched on demand and spoken to via stdio. Services are declared in `agent/effects/local.py`'s `_MCP_SERVERS` registry.

### Registry shape

Each entry is a dict with at least `command` (the subprocess argv) and optionally `env_from_file` (a mapping from environment variable name to the path of a file whose contents become that variable's value):

```python
_MCP_SERVERS: dict[str, dict[str, Any]] = {
    "terminal": {
        "command": [sys.executable, "-m", "mcp_servers.terminal"],
    },
    "exa": {
        "command": ["npx", "-y", "exa-mcp-server",
                    "--tools=web_search_exa,get_code_context_exa"],
        "env_from_file": {"EXA_API_KEY": "~/.exa_key"},
    },
}
```

`env_from_file` values may use `~` for the user home directory. Files are read once at connect time, stripped of surrounding whitespace, and merged into the subprocess environment. If any declared file is missing or empty, `mcp_connect` raises immediately with a clear message naming the missing key — research calls that depend on the service will structurally fall through to the `no_results` branch rather than burning retries against a broken server.

### Current services

| Service | Purpose | Requires |
|---------|---------|----------|
| `terminal` | PTY sessions for interactive program testing | nothing (in-tree `mcp_servers.terminal`) |
| `exa` | Web search + content fetch for the `research` flow | `~/.exa_key` containing an Exa API key, and `npx` on PATH |

To add a new service, drop an entry into `_MCP_SERVERS` and write an action that calls `effects.mcp_connect(name)` + `effects.mcp_call_tool(conn_id, tool, args)`. See `agent/actions/interactive_actions.py` for the established consumption pattern.

---

## Development Cycle

**ALWAYS follow this sequence when making changes:**

1. **Code**

2. **Format** → `uv run black .`
   - Do NOT skip — Black reformatting can change line numbers that affect any test assertions and downstream diffs.

3. **Lint** → `uv run ruff check .`
   - Auto-fix what's safe: `uv run ruff check --fix`.
   - Investigate every remaining warning — F841 in particular surfaces dropped wiring, not stylistic noise.

4. **Test** → `uv run pytest tests/ -v` if tests exist for the touched code (conventions: `TESTING.md`).

5. **Compile flows** → `uv run ouroboros.py cue-compile` (if CUE files changed)
   - `flows/compiled.json` is a build artifact regenerated from `flows/<set>/*.cue` — it is committed (tests read it directly), so rebuild and include it whenever CUE sources change.

6. **Smoke test** → `uv run ouroboros.py smoke` (for flow/action changes)

7. **Flow linter** → `uv run ouroboros.py lint-flows` (for flow/action changes)

8. **Verify live** → `uv run ouroboros.py mission create --mission_config <test_config>`
   - The above command is all-in-one: cleans, creates, and starts the agent.
   - Requires a running LLMVP server.

Smoke (`uv run ouroboros.py smoke`) is the fastest signal that the full flow set still loads and begins execution cleanly. It catches most structural regressions without needing a live LLMVP server.

Unit tests passing (when they exist) does not guarantee the agent cycle works end-to-end. Live verification is the only real test for feature work touching flows, actions, or prompts.

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
curl -X POST http://localhost:8008/graphql \
  -H "Content-Type: application/json" \
  -d '{"query": "query { health { status poolSize availableInstances } }"}'

curl -X POST http://localhost:8008/graphql \
  -H "Content-Type: application/json" \
  -d '{"query": "query { completion(request: { prompt: \"Hello!\", maxTokens: 50, temperature: 0.7 }) { text tokensGenerated finished } }"}'
```

### Static Knowledge (Universal Context)

LLMVP pre-tokenizes a knowledge base file into a binary token buffer (`data/<model>.tokens.bin`) at preprocessing time. At server startup, this buffer is memory-mapped and prepended to every inference call as a static prefix. The model evaluates these tokens once on first use, and the KV cache snapshot is reused for all subsequent calls — making the universal context effectively free at inference time.

The static stream is composed per PERSONA (llmvp/preprocessing/builder.py): the persona file (`config.prompt.persona_file`, e.g. `llmvp/knowledge/SOUL.md`; alternate personas via the `personas:` map) plus the `llmvp/knowledge/` documents, tokenized into `config.knowledge.tokens_bin`. The bin auto-rebuilds when missing or stale. To force a rebuild after editing the soul or knowledge files:

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
- **Circuit breakers** — Multiple targeted safety valves (dispatch-repeat detection, rework budgets, quality-gate exhaustion, cycle budget) limit runaway behavior. The per-task frustration counter has been deprecated; see `IMPLEMENTATION.md` §2.6 and §4.6 for the planned unified replacement.
- **`$ref` resolution** — Typed references in structural fields (params, input_map, tail_call) resolved at runtime by `loader.py`. Replaces Jinja2 `{{ }}` in flow definitions.
- **Prompt templates** — Section-based YAML files in `prompts/` referenced by CUE flows via `prompt_template`. Pre-compute formatters handle complex data formatting.

### Flow Organization

Flows are grouped by role in the agent cycle. For the current flow set, run `uv run ouroboros.py cue-compile` and inspect `flows/compiled.json`, or read the CUE source in `flows/shared/` and `flows/code_core/`.

**Orchestrator flows** operate above any single task. They shape the mission — designing architecture, selecting goals, reasoning about what to work on next. `mission_control` is the always-present hub; `design_and_plan` runs at mission start and when architecture drift is detected.

**Flow-directive flows** are the dispatchable units of work. They receive a specific directive (target file + intent) from `mission_control` and execute it, reporting back a structured `DirectiveReport`. Each handles one class of work: source files, project infrastructure, investigation, behavioral testing. They are the layer where inference-heavy reasoning happens and where diagnosis-on-failure originates.

**Sub-flows** are mechanical execution units invoked synchronously via `action: flow`. They have no agency — they do a specific job and return structured data. `create`/`rewrite`/`patch` are the three modes of file modification; `prepare_context` builds the workspace view most flows need; `run_commands` and `run_session` wrap terminal interaction; `quality_gate` performs structural and behavioral validation.

The full inventory is derivable from the `flows/` set directories at any time. Referring to it in this document would invite drift — the CUE source is the source of truth.

---

## Development Conventions

- **temperature** — prefer `t*` specifiers (multipliers on the model default): `t*0.0`–`t*0.2` deterministic/JSON extraction, `t*0.4`–`t*0.6` balanced planning/editing, `t*0.8` exploratory (see PROMPTING_CONVENTIONS §11 for the full table).
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
