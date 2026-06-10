# Observations — a complementary system to projections

**Status:** Proposal, parked for after the turn schema work.
**Related:** Chat threads on menu schema design, `#Turn` primitive design
(`dev/proposals/turn_schema_primitives.md` when drafted).

---

## The gap this fills

The agent has one mature read-view mechanism — **projections**. A projection is
a pure function `(MissionState, params) → dict`. Projections materialize
composed read views from durable mission state so steps receive exactly the
context they need. They are:

- **Pure.** No effects. Same inputs produce same output.
- **Mission-sourced.** Input is the mission record; that's it.
- **Snapshot-reproducible.** Given a mission snapshot, any projection can be
  replayed bit-identically. This is central to the audit story.
- **Testable.** Tests pass a hand-built `MissionState`, assert the output
  dict.

Projections cover a lot of ground. They do not cover the shape of data the
agent genuinely needs that is **not in the mission record** but *is*
predictably computable from the world:

- The current list of source files on disk (changes as `file_ops` edits)
- AST symbols extracted from a file at read time
- The current working directory's layout (for `project_ops` setup)
- The running process table during an `interact` session
- Git state (branch, dirty files, last commit) when relevant

Today these show up as **ad-hoc action-published context keys**. An action
runs an effect, publishes the result into the accumulator, a downstream step
reads it. This works but has no conceptual spine:

- No declared shape — every site invents its own context-key schema.
- No audit story — "what did the step see?" requires reading the producing
  action's code to know what fields exist in the dict.
- No cache story — if three steps need the same observation, three actions
  run the same effect or the flow has to manually thread a single
  observation through each step's publishes/context.optional.
- No temporal guarantee — two steps in the same flow might observe the
  filesystem at different moments and disagree, silently.
- No declarative visibility — reading the flow CUE, you can't tell which
  steps depend on which observations of the world.

The menu schema work surfaced this gap directly. When a menu asks for
`options_from: "scanned_files"`, the source of that data (and its audit
story) is materially different from a menu that asks for
`options_from: "mission_file_list"` (a projection slot). Both deserve
first-class treatment, but they have different computational shapes and
different audit guarantees, so they should be different systems.

## The proposal: observations

An **observation** is declared like a projection but with explicit effect
dependencies and an explicit staleness contract.

```cue
#ObservationDef: {
    name:        string         // e.g. "project_file_list"
    effects:     [...string]    // Which effect calls this observation uses
    cache_scope: "step" | "flow" | "mission"  // When re-run is allowed
    pure_on:     [...string]    // Inputs (from context or params) that
                                // determine the observation's value — if
                                // these don't change, the cached result
                                // is valid
}
```

A flow declares observations it needs in the same way it declares
context:

```cue
my_step: #StepDefinition & {
    observations: required: ["project_file_list", "git_state"]
    ...
}
```

The runtime materializes the declared observations before the step runs,
runs the effect calls, writes the result into the step's input alongside
context. Trace events record:

- The observation name
- What effects were called and their results
- The wall-clock timestamp of the observation
- Whether the result came from cache or a fresh call

## Contrast with projections

| Axis | Projections | Observations |
|---|---|---|
| Purity | Pure function | Effectful |
| Input | `MissionState` + params | Effects + params |
| Reproducibility | Bit-identical from mission snapshot | Requires timestamp + effect log |
| Caching | N/A — re-run is free | Cache scope is part of the declaration |
| Audit story | Replay against mission | Trace records observation call + result |
| Test story | Hand-build MissionState, assert output | Mock effects, assert the observation's compiled sequence |
| Typical lifetime | Any (mission-durable data) | Step → Flow → Mission (declared) |

Both feed menus/turns through the same declarative channel. The turn
schema's `options_from` / `description_from` can resolve to **either** a
projection slot or an observation — same call site, the resolution
mechanism figures out which.

## Cache scope

Three levels, chosen by the observation author based on the data's
volatility and cost of the effect call:

- **step** — run once per step invocation. The next step in the flow
  re-runs. Use for cheap observations that can change between steps
  (e.g., line count of a file being actively edited).
- **flow** — run once per flow invocation. All steps in the flow see the
  same snapshot. Re-runs on next flow call. Use for observations that
  are expensive or whose stability across the flow matters (e.g., the
  list of project files during a single `file_ops` invocation).
- **mission** — run once per mission lifetime (or until invalidated).
  Use for observations that are effectively immutable during the
  mission (e.g., git repo root path).

Cache scope is declared, not inferred. The runtime honors it and records
cache hits vs. refreshes in the trace.

## What it does *not* do

- **Not a general cache.** Observations are specifically for
  effect-sourced read views of the world. Caching arbitrary computation
  belongs elsewhere if we ever need it.
- **Not a write channel.** Observations are read-only. Writes go through
  actions and effects as today.
- **Not a subscription mechanism.** No reactive updates, no watchers.
  Observations are pull-based, lazy, and cached by scope.
- **Not a replacement for projections.** Mission state stays in
  projections. Observations complement, don't compete.

## Worked example — `scanned_files` today vs. under observations

**Today (ad hoc):**

```cue
scan_project: #StepDefinition & {
    action: "scan_project"     // effect call, publishes scanned_files
    publishes: ["project_manifest", "scanned_files"]
}
pick_file: #StepDefinition & {
    context: required: ["scanned_files"]
    ...
}
```

Problems: `scanned_files` shape is invented by the `scan_project` action.
No lint check that the shape matches what `pick_file` expects. If
`pick_file` is called from a different flow that forgot to scan first,
the error surfaces at runtime. Trace shows `scan_project` ran, but the
causal link between "we scanned" and "we used scanned data in the menu"
is implicit in flow ordering, not explicit in the declarations.

**Under observations:**

```cue
// Declared once, in a shared observations registry
project_file_list: #ObservationDef & {
    name: "project_file_list"
    effects: ["list_directory"]
    cache_scope: "flow"
    pure_on: ["working_directory"]
}

// Used by any step that needs it
pick_file: #StepDefinition & {
    observations: required: ["project_file_list"]
    ...
}
```

Advantages:
- No explicit `scan_project` step needed — the observation materializes
  on demand.
- Shape is declared centrally; lint enforces that consumers match.
- Reusable across flows without re-scanning.
- Trace explicitly logs the observation call and cache behavior.
- Works seamlessly with `options_from: "project_file_list"` in a turn
  schema.

## Scope estimate

Not small, but self-contained. Rough inventory of where observations
would replace ad-hoc shapes (from today's flows):

- `project_manifest` / `scanned_files` (scan_project action → multiple
  consumers in `file_ops`, `mission_control`, `design_and_plan`)
- `symbol_menu_options` (extract_symbols action → `patch`)
- `file_content` (read_file action → `patch`, `rewrite`)
- `validation_results` (run_validation_checks_from_env → `file_ops`,
  `quality_gate`)
- `terminal_output` sampling (multiple sites)

A fair estimate: 5–10 observations, a new runtime phase (observation
materialization before step input assembly), a `#ObservationDef` CUE
primitive, a registry module, lint checks, tests. Comparable in scope
to the projections infrastructure itself.

## Why park this

- The turn schema work will surface which observations are actually
  needed. Going into schema design with "the menu schema might call
  into observations someday" is good; going in with a built-but-empty
  observations system is premature.
- The turn-schema per-site analysis pass will show us which
  context-key-publishing actions are doing observation-shaped work. We
  learn the real catalog from that pass.
- Observations intersect with runtime infrastructure in ways that need
  care (materialization phase, trace integration, cache invalidation
  semantics). That's a project-sized change on its own.

## Next-actions checklist when we pick this up

1. Review the list of ad-hoc context keys that turned up during the
   turn-schema per-site analysis — which ones are observation-shaped?
2. Draft `#ObservationDef` in CUE, sized against the actual catalog we
   find.
3. Design the runtime materialization phase and its ordering vs. step
   input assembly.
4. Design the cache-scope implementation (per-step / per-flow /
   per-mission) with explicit invalidation hooks.
5. Integrate with the turn schema's `options_from` / `description_from`
   resolver so menus draw from observations as cleanly as from
   projections.
6. Migrate the catalog sites in one clean-break change, per the
   project's norm.

---

*Written during menu-schema design discussions. Not a finalized design,
a placeholder for the shape of the follow-up.*
