# Ops quality_gate port — grounded output-format re-assessment

> **STATUS: SHIPPED 2026-07 — per-goal grounded acceptance checks (gate/derive/store/run) live in the interact flow; vacuous-verification guard held. Hardened 2026-07-16 (gate output threaded into fix prompts).**

## Why
TB2 datacollect (89 tasks, gpt-oss-120b): **2 pass, 87 fail** — and the dominant
failure is NOT timeout. Primary verifier failure across the 87:
- **63% MISSING ARTIFACT** — required output file never written
- 15% wrong-content, 22% other/infra

Root cause is a **deterministic pipeline pathway**, not model capability:
- `derive_output_format` runs **once, blind, in ops_control — before cycle 1**, with
  only `mission.objective` + `working_directory` (no filesystem, no terminal). Its
  prompt is (correctly) conservative: "if the task doesn't *state* a concrete output
  format, return `{checks:[]}`." But TB output paths are usually a **convention**
  (`/app/result.txt`, `/app/output_data/plan_b1.jsonl`) the bare task never states.
- Result measured on the 87: **69% empty/nonsense spec → 63% missing artifact.**
  The agent reasons fine in-chat (e.g. mteb-leaderboard *understood* it needed an
  org/model name) but never writes the file the verifier reads.

The intended design (per the operator) was the oracle as a quality_gate analog:
catch bogus inputs EARLY, then **re-assess LATE** like code_core's `quality_gate`.
The early-catch survived; the late re-assess was dropped. The spec is frozen from
the blind early pass and reused verbatim every cycle. **Context is inverted:** the
step that needs ground truth (derivation) runs starved; the step that has it
(`check_format`, live filesystem) just enforces the bad spec.

## The port (surgical — reuses existing enforcement)
`check_format` already flags a missing artifact (when the spec has an `exists` check)
and `decide` already blocks `task_done` on surviving required-fails. The ONLY broken
link is the spec. So we add a **grounded late re-derivation**, gated to fire once,
positioned where live terminal context exists — and the existing machinery does the
rest.

New steps in `flows/ops/ops_task.cue`, spliced into the check chain
`… profile_oracle → check_format …`:

```
profile_oracle → gate_reground → [reground_output_format → store_reground_format →] check_format
```

- **`gate_reground`** (`gate_reground_output_format`, deterministic): fires the
  reground only when the early pass left **no usable spec** AND we now have terminal
  context AND it hasn't run yet (`output_format_grounded`). Keeps the conservative
  early pass; targets only the ~69% empty-spec tasks; zero cost when a good early
  spec exists.
- **`reground_output_format`** (inference, `prompts/ops/reground_output_format`):
  re-derives the spec grounded in `task_spec` + `working_directory` + `session_tail`
  (the live exploration — stub files, expected paths, "no such file" hints). Can now
  name the conventional artifact the blind pass couldn't.
- **`store_reground_format`** (`store_reground_output_format`): parse + store
  (reuses the conservative parser) and set `output_format_grounded=True` — one-shot
  guard even when it still finds nothing.

Model: `TaskDefinition.output_format_grounded: bool = False`
(`agent/persistence/models.py`).

## What it does / doesn't change
- **Conservatism preserved:** still SHAPE-only, still `{checks:[]}` when no artifact
  is identifiable; the gate never re-derives a *good* early spec. So it cannot newly
  false-fail a correct answer beyond the existing `exists`-check semantics.
- **Enforcement unchanged:** `check_format` + `decide` already loop on a missing
  required artifact; we just give them a spec that actually names it.
- **Cost:** at most ONE extra inference per mission, and only for empty-spec tasks.

## Out of scope (follow-ups)
- In-session anchoring (surface the grounded required-artifact into the charter so
  the agent prioritizes producing it) — helps the timeout subset; bigger change.
- Always-reground (refine even non-empty early specs) — v2 if measured to help.
- Re-grounding `completion_criteria` (also blind-early) — same pattern, later.

## Validation
- Server-free now: unit tests (gate fires on empty spec / skips when grounded /
  skips when usable / one-shot store) + CUE recompile + full suite green.
- **DEFERRED (needs the server, which TB1 is using):** canary on a handful of the
  missing-artifact tasks (e.g. mteb-leaderboard, llm-inference-batching-scheduler)
  to confirm the grounded spec → artifact written → pass. Must NOT interfere with
  the TB1 adaptive-reasoning data-collection run.
