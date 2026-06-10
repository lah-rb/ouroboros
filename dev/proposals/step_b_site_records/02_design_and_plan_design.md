# Site #2 — `design_and_plan.design_initial` / `design_and_plan.design_reconcile`

**Status:** Approved for Step C migration.
**Response shape:** `json_document`
**Current file:** `flows/cue/design_and_plan.cue` (lines 139-166)
**Empirical (892 run):** n=1, in=705 tokens, out=504 tokens — one-shot per mission.

---

## Turn definition

```cue
_design_turn_shape: #Turn & {
    response_shape: "json_document"
    sections: [
        {type: "role",          template: "personas/software_architect"},
        {type: "problem",       template: "design_and_plan/objective"},      // ## Mission Objective
        // `evidence` section is present only in the reconcile variant —
        // design_initial omits it (empty section is omitted by the renderer).
        {type: "evidence",      ref: {$ref: "context.existing_architecture"}},
        {type: "context_files", template: "design_and_plan/context_files"},  // repo_map + project_file_list
        {type: "instruction",   template: "design_and_plan/architecture_brief"},
        {type: "envelope"},
    ]
    response: {
        schema_id: "architecture_plan"  // registered in the shared schema registry
    }
    transitions: {
        default:   string  // different per variant — see below
        no_answer: "failed"
    }
    config: {temperature: "t*0.2"}
}

design_initial: #StepDefinition & {
    action: "inference"
    description: "Design project architecture from scratch"
    turn: _design_turn_shape & {
        transitions: {
            default:   "parse_architecture"
            no_answer: "failed"
        }
    }
    // pre_compute remains as in current flow (format_mission_meta,
    // format_project_file_list)
    publishes: ["inference_response"]
}

design_reconcile: #StepDefinition & {
    action: "inference"
    description: "Reconcile architecture with drifted codebase"
    turn: _design_turn_shape & {
        transitions: {
            default:   "parse_architecture_reconcile"
            no_answer: "failed"
        }
    }
    // pre_compute adds format_existing_architecture on top of base precompute
    publishes: ["inference_response"]
}
```

(Step C chooses the CUE idiom for the shared shape — likely `let` binding
with field overrides, mirroring the pattern from Site #1.)

---

## Decisions landed

### One-shot — kept

**Settled:** keep `design_initial` as a single inference call producing the full architecture JSON. Not reshaping to iterative per-module generation.

**Rationale:** the architecture fields are cross-referential. Modules' `imports_from` refers to other modules; `creation_order` sorts the module list; `interfaces` cross-reference caller/callee pairs. Producing them in one coherent object means the model reasons about the whole shape together — splitting across turns risks drift where a later turn forgets (or re-invents) facts from an earlier one. One-shot guarantees internal consistency.

**Empirical support:** this flow has not given us problems. The low-temperature one-shot is doing actual work — it's correct to let it keep doing it.

**If this ever fails on larger projects:** the right mitigation is richer pre-compute (filtered/clustered manifests, staged inputs), not turn-splitting. The schema supports arbitrarily rich `pre_compute`; we have runway.

### `existing_architecture` section — maps to `evidence` (for now)

**Settled:** reconcile's "current architecture" block goes in the `evidence` section. It's structured data the model reasons from to determine what changed — category-compatible with terminal output, test results, and other "artifact the model inspects."

**Promotion condition:** if a second site emerges with a "prior state" concept (e.g., prior goals during re-derivation, file-before-edit for rewrite), promote to a new `prior_state` section type at that time. The schema vocabulary grows only when a pattern recurs.

### `repo_map` + `project_file_list` — merge under `context_files`

**Settled:** same consolidation pattern as Site #1. The current prompt has two separate sections (`repo_map`, `existing_files`) — both are "here's what's on disk." Under the schema's single-per-type rule, they compose into one `context_files` template that renders AST map first, then file list, in consistent order.

Pre-compute formatters (`format_project_file_list`, plus the build_repomap step's `repo_map_formatted`) remain unchanged. The merge happens at the template layer.

### DRY — `design_initial` / `design_reconcile` share one shape

**Settled:** two steps sharing one turn shape, differing in:
- Whether `evidence` section is populated (initial has nothing to render; reconcile has `existing_architecture`)
- Different `default` transition target (different parse step per variant)
- Different pre-compute list (reconcile adds `format_existing_architecture`)

Same pattern as Site #1. Schema preserves the shared structure cleanly.

### Temperature — `t*0.2` kept

**Settled:** low temperature for architecture. Different task than code generation — architecture requires internal cross-referential consistency across ~7 nested fields in one pass, where a sampling divergence can produce "module listed in `modules` but missing from `creation_order`" type errors that cascade downstream.

Discussed: consistency with code's `t*0.4` across sites has pull as a uniform project default, but task-fit wins here. Low temperature is doing work; flow has not given us problems; don't change it.

Implication for the per-site temp catalog: architecture and code are genuinely different temperature regimes, and that's acceptable. The site records will note the temp choice on each site rather than a one-size-fits-all default.

### `output_format` section — removed at this site

**Settled:** same pattern as Site #1. The ✅/❌ examples move into SOUL.md primer as part of the envelope/banner priming. One-time startup cost; per-site format guidance redundant.

### Unchanged at this site

- `load_mission`, `scan_workspace`, `build_repomap`, `check_drift` — non-inference preparatory steps. Schema doesn't touch them.
- `parse_architecture`, `parse_architecture_reconcile` — non-inference parse steps. Unchanged.
- `domain_research`, `save_research`, `derive_goals`, `complete`, `failed` — non-inference or rule-based. Unchanged.
- `publishes: ["inference_response"]` unchanged.
- `context` requirements unchanged.

---

## Templates to author at Step C

1. **`personas/software_architect.yaml`** — `---ACT AS---` block for the software-architect role.
2. **`design_and_plan/objective.yaml`** — `## Mission Objective` + `{context.mission_objective}`.
3. **`design_and_plan/context_files.yaml`** — merged repo_map + project_file_list rendering. Consumes existing pre-compute outputs.
4. **`design_and_plan/architecture_brief.yaml`** — the 7-point architecture instruction block (from current `instructions` section).

## Schemas to author at Step C

1. **`architecture_plan`** schema entry in the shared schema registry — encoding the expected JSON shape: `execution{}`, `modules[]`, `interfaces[]`, `data_shapes[]`, `creation_order[]`, `notes`. The schema registry is one of the new Step C modules.

---

## Cross-site follow-ups

- **Temperature policy** — record the divergence here. Code (Site #1) uses `t*0.4`; architecture (Site #2) uses `t*0.2`. Site records note the temp choice explicitly rather than assuming a project-wide default. No flow-level defaults get overridden here; the per-turn `config: {temperature: "t*0.2"}` is canonical.
- **`personas/software_architect`** persona template — first appearance. May be reused by future high-level planning sites; check alignment if another site calls for an architect persona.
- **`prior_state` section type** — parked until a second site needs it. Watchlist during remaining Step B analyses.
- **Schema registry** — Site #2 is the first site to need the registry. Step C implementation plan should treat this as the forcing function for creating it, not a separate future project.
