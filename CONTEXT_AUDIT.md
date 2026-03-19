# Context & Structure Audit — Findings and Implemented Fixes

## Executive Summary

Ouroboros has two systemic weaknesses for multi-file systems development:

1. **Tree-sitter context is underutilized** — The `repomap.py` module (AST parsing + PageRank ranking) existed but was only used in the `research_repomap` sub-flow, which is an *optional* research path rarely invoked during normal task execution. The core file selection and code generation pipelines never saw the AST dependency map.

2. **No holistic design step** — The agent creates files one at a time with no upfront architectural plan. Each `create_file` invocation makes independent structural choices (flat vs nested, naming, interfaces), leading to the "duplicate module structure" problem observed in the challenge assessment.

Both issues have been fixed. Below are the detailed findings and the seven recommendations implemented.

---

## Weakness 1: Tree-Sitter Context Not Utilized in Core Flows

### Finding

The `repomap.py` module provides three critical capabilities:
- **AST-based symbol extraction** — Uses tree-sitter to parse every source file and extract classes, functions, methods, imports
- **PageRank-weighted ranking** — Builds a reference graph and ranks files by structural importance
- **Related file discovery** — `get_related_files(target)` returns files most connected to a given target by actual symbol references

However, these capabilities were only accessible through `research_repomap.yaml`, which is dispatched by `research_context.yaml` — a sub-flow that `prepare_context` only invokes when the model's frustration level is elevated. **In normal operation (frustration=0), the repomap was never consulted.**

### Where Context Was Missing

| Flow | Step | What it saw | What it needed |
|------|------|-------------|----------------|
| `prepare_context` | `select_relevant` | Flat file manifest (path + signature) | AST dependency map showing actual imports/references |
| `prepare_context` | `load_selected` | Only LLM-selected files | Deterministic dependency-based files |
| `create_file` | `generate_content` | Context bundle (files) | Repomap + related files for interface matching |
| `create_tests` | `generate_tests` | Target file + context bundle | Repomap for correct import path derivation |
| `integrate_modules` | `analyze_connections` | File manifest only | AST dependency graph with actual symbol references |
| `modify_file` | `plan_change` | Target file + context bundle | Related files by reference graph for impact analysis |
| `create_plan` | `generate_plan` | File manifest only | Structural overview for dependency ordering |
| `revise_plan` | `assess_revision` | Notes only | File manifest + workspace reality |
| `quality_gate` | `plan_checks` / `summarize` | Validation results only | Structural consistency analysis |

### Impact (from Challenge Assessment)

- **`create_tests` failed 3 times** (frustration=4) — Model generated test files with wrong import paths because it couldn't see the actual project structure
- **Duplicate module structure** — Agent created both `command_parser.py` and `src/command_parser.py` because no structural context constrained its choices
- **`integrate_modules` hit frustration=5** — Without seeing the actual dependency graph, the model couldn't identify which files were disconnected

---

## Weakness 2: No Holistic Design Opportunities

### Finding

The agent's execution pipeline is:
```
create_plan → [create_file × N] → integrate_modules → validate_behavior
```

Each `create_file` invocation operates in isolation:
1. It sees the files that already exist (via `prepare_context`)
2. It generates code for one file
3. It validates syntax
4. It returns to `mission_control`

**There is no step where the model considers the entire project design holistically.** Specifically:

- **No directory layout decision** — Each file creation independently decides where to put things
- **No interface contract specification** — Module A defines `parse_command(input)` but Module B calls `parse(raw_text)` because they were generated in separate inference calls
- **No creation order enforcement** — The plan has `depends_on` but no mechanism to ensure interfaces match across dependency boundaries
- **`revise_plan` operates blind** — When plan revision was triggered, it had no access to the actual workspace state, only mission notes. It hallucinated fix targets for files that didn't exist.

### Impact (from Challenge Assessment)

- **Duplicate `src/` and top-level modules** — Two separate `create_file` calls made different layout decisions
- **Integration failures** — `integrate_modules` found disconnected components because files were designed independently
- **Hallucinated plan revisions** — `revise_plan` added tasks for `clients/api.py` and `app.py` which didn't exist

---

## Implemented Fixes

### R1: Integrate Repomap into `prepare_context` (Core Pipeline)

**Files changed:** `flows/shared/prepare_context.yaml`

Added `build_repomap` step between `scan_workspace` and `check_research_needed`. This runs on every context-gathering invocation, not just when research is triggered. The repomap now publishes:
- `repo_map_formatted` — PageRank-ranked AST map (token-budgeted for prompts)
- `related_files` — Files most connected to the target by reference graph

Both are propagated through the `gather_project_context` step template to all consuming flows.

### R2: New `design_architecture` Flow

**Files created:** `flows/tasks/design_architecture.yaml`
**Files changed:** `flows/registry.yaml`, `flows/create_plan.yaml`, `agent/actions/mission_actions.py`

A dedicated flow that runs early in the mission (before file creation) and produces a structured JSON blueprint defining:
1. Directory layout (flat vs nested — one authoritative choice)
2. Module responsibilities (what each file defines and exports)
3. Interface contracts (caller → callee, exact function signatures)
4. Creation order (dependency-sorted)
5. Entry point wiring (every import the main module needs)

The blueprint is persisted as a mission note with category `architecture_blueprint`.

`create_plan` now instructs the model to use `design_architecture` as the first task for any multi-file project.

### R3: Fix `revise_plan` Blindness

**Files changed:** `flows/shared/revise_plan.yaml`

Added `scan_workspace` step before the LLM assessment. The revision prompt now includes:
- `project_manifest` — actual files on disk with signatures
- Explicit instruction: "ONLY reference files that exist in the manifest above"

This prevents hallucinated fix targets.

### R4: Cross-File Validation in Quality Gate

**Files created:** `action_validate_cross_file_consistency` in `agent/actions/research_actions.py`
**Files changed:** `flows/shared/quality_gate.yaml`, `agent/actions/registry.py`

A deterministic (no LLM needed) validation step that runs tree-sitter analysis and checks for:
- **Duplicate definitions** — Same symbol defined in multiple files
- **Unresolved references** — File A references symbol X but no file defines it
- **Orphan files** — Files that define symbols but have no cross-file connections

Results are injected into the `summarize` step prompt so the LLM creates targeted fix tasks.

### R5: Hybrid File Selection

**Files changed:** `agent/actions/refinement_actions.py`

The `load_file_contents` action now uses a two-phase selection strategy:
1. **Phase 1 (deterministic):** Include `related_files` from the repomap — these are actual dependencies by reference graph
2. **Phase 2 (LLM-augmented):** Fill remaining budget with LLM-selected files

This ensures the context bundle always includes true dependencies, even if the LLM's selection misses them.

### R6: Repomap in `create_tests`

**Files changed:** `flows/tasks/create_tests.yaml`

The `generate_tests` step now sees `repo_map_formatted` with an explicit instruction:
> "Use the EXACT module paths shown above for imports in your tests. Convert file paths to Python module names: app/main.py → app.main. Do NOT guess import paths."

This directly addresses the challenge assessment finding that `create_tests` failed due to wrong import paths.

### R7: Blueprint Persistence & Retrieval

**Files changed:** `agent/actions/mission_actions.py`

The `relevant_notes` assembly in `configure_task_dispatch` now partitions notes:
- **Architecture blueprint notes are always included** (regardless of recency)
- Remaining budget filled with most recent other notes

This ensures every `create_file`, `modify_file`, and `integrate_modules` invocation sees the blueprint, even late in the mission when many other notes exist.

---

## Verification

All 676 existing tests pass after these changes. No test modifications were needed except updating the flow count assertion (28 → 29) for the new `design_architecture` flow.

---

## Remaining Opportunities (Not Yet Implemented)

1. **Interface Contract Validation** — A dedicated action that parses the blueprint JSON and validates actual function signatures against the contract. Currently the blueprint is advisory text; it could be machine-checked.

2. **Creation Order Enforcement** — The `assess_mission_progress` action could enforce that `design_architecture` completes before any `create_file` task is dispatched, rather than relying on `depends_on` resolution.

3. **Incremental Repomap Updates** — Currently `build_repomap` rebuilds from scratch each time. For large projects, caching the AST parse and only re-parsing changed files would save time.

4. **Blueprint Diffing** — When `revise_plan` modifies the task list, it could diff the new plan against the blueprint and flag structural conflicts (e.g., adding a file not in the layout).

5. **Cross-File Validation in validate_output** — Currently `validate_output` only checks the single file being written. It could run `validate_cross_file_consistency` after each file creation to catch interface mismatches early, not just at the quality gate.
