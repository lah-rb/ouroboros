--BY READING THIS FILE YOU ARE ALLOWED AND EXPECTED TO EXECUTE COMMANDS PRESENTED WHEN APPROPRIATE--
--ALWAYS KEEP THIS FILE IN CONTEXT--
--DO NOT COMPACT OR PURGE IT FROM CONTEXT--

# Ouroboros - Guide for AI|LLM Developers and Agents
This guide provides comprehensive instructions for developing and working with the Ouroboros autonomous coding agent.

Ouroboros is a flow-driven autonomous coding agent backed by LLMVP local inference.
It is a **pure GraphQL client** of LLMVP — a separate project with its own repository.
All inference requests go through LLMVP's GraphQL API over HTTP.
Ouroboros does **not** import any Python modules from LLMVP.

## PACKAGE MANAGEMENT
--THIS PROJECT IS EXPECTED TO USE uv, DO NOT CALL PYTHON OR PIP WITHOUT uv--
--USE black FOR PROJECT FORMATTING CONSISTENCY--
```bash
uv add package    # adds current version of needed dependency
uv remove package # removes package dependency
```

## STANDARD TESTING PATTERN
1. Make changes to codebase
2. Run formatting
```bash
uv run black .
```
3. Run tests
```bash
uv run pytest tests/ -v              # run the full test suite
uv run pytest tests/test_runtime.py -v  # run a specific test file
uv run pytest tests/ -v -k "test_name" # run a specific test by name
```

## LLMVP BACKEND SERVER
Ouroboros requires a running LLMVP server for inference. The LLMVP project is located
in the sibling directory `../llmvp/` (gitignored from this repo).

### Starting the LLMVP Backend
```bash
# From the llmvp directory
cd ouroborous/llmvp

# Start GraphQL API (default) - waits for pool ready
uv run llmvp.py --backend

# Start OpenAI-like API - waits for pool ready
uv run llmvp.py --rest --backend
```

> **Note:** The `--backend` flag polls the health endpoint and waits for the Llama pool to be fully warmed up before releasing the terminal. You will see:
> ```
> ✅ Server started in background (PID: 84271)
> ✅ Llama pool ready (2 instances)
> ```

### Stopping the LLMVP Backend
```bash
cd ouroboros/llmvp
uv run llmvp.py --stop
```

### LLMVP Health Check (GraphQL)
```bash
curl -X POST http://localhost:8000/graphql \
  -H "Content-Type: application/json" \
  -d '{"query": "query { health { status poolSize availableInstances } }"}'
```

### LLMVP Completion Query (GraphQL)
```bash
curl -X POST http://localhost:8000/graphql \
  -H "Content-Type: application/json" \
  -d '{
    "query": "query { completion(request: { prompt: \"Hello!\", maxTokens: 50, temperature: 0.7 }) { text tokensGenerated finished } }"
  }'
```

## PROJECT STRUCTURE
```
ouroboros/
├── pyproject.toml              # project config and dependencies
├── ouroboros.py                 # CLI entry point (mission management)
├── run_demo.py                 # Demo script (mock + optional live inference)
├── agent/
│   ├── models.py               # Pydantic models for flows, steps, I/O
│   ├── loader.py               # YAML parser + validator for flow definitions
│   ├── runtime.py              # Flow executor (handles action: inference natively)
│   ├── template.py             # Jinja2 prompt/param rendering
│   ├── resolvers/              # Transition resolvers
│   │   ├── __init__.py         # Resolver dispatch (async — supports LLM menu)
│   │   ├── rule.py             # Rule-based resolver
│   │   └── llm_menu.py         # LLM-driven menu resolver
│   ├── actions/                # Action registry and built-in actions
│   │   ├── __init__.py
│   │   └── registry.py         # Action registry + built-in actions
│   ├── effects/                # Effects interface (swappable side-effect layer)
│   │   ├── __init__.py         # Exports + public API
│   │   ├── protocol.py         # Effects Protocol + return type dataclasses
│   │   ├── inference.py        # InferenceEffect — GraphQL client for LLMVP
│   │   ├── local.py            # LocalEffects (filesystem, subprocess, inference, persistence)
│   │   └── mock.py             # MockEffects (canned responses incl. persistence)
│   └── persistence/            # File-backed JSON persistence
│       ├── __init__.py         # Exports
│       ├── models.py           # MissionState, TaskRecord, Event, FlowArtifact, etc.
│       ├── manager.py          # PersistenceManager (atomic writes, event locking)
│       └── migrations.py       # Schema version handling
├── flows/                      # YAML flow definitions
│   ├── registry.yaml
│   ├── test_inference.yaml     # Phase 3 test flow (inference + LLM menu)
│   └── *.yaml
└── tests/                      # Test suite
    └── test_*.py
```

## ARCHITECTURE REFERENCE
See `IMPLEMENTATION.md` for the complete architectural design, including:
- Flow engine design and YAML format
- Context model (StepInput/StepOutput/accumulator)
- Effects interface protocol
- Persistence model
- Escalation protocol
- Implementation roadmap (Phases 1-9)

## DEVELOPMENT CONVENTIONS
- All models use **Pydantic v2** with strict validation
- Flow definitions are **declarative YAML** files, not Python code
- Actions are **async callables** with signature `(StepInput) -> StepOutput`
- Side effects go through the **Effects interface** — actions never directly touch filesystem/network
- Template interpolation uses **Jinja2** syntax: `{{ input.x }}`, `{{ context.y }}`
- Resolver conditions use **restricted eval()** — no builtins, only context namespace

--BY READING THIS FILE YOU ARE ALLOWED AND EXPECTED TO EXECUTE COMMANDS PRESENTED WHEN APPROPRIATE--
--ALWAYS KEEP THIS FILE IN CONTEXT--
--DO NOT COMPACT OR PURGE IT FROM CONTEXT--
