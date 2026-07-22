# Oracle improvements — implementation plan

> **STATUS 2026-07-22 — SHIPPED, plus canary residue.** Delta-audit against the
> live tree: every tier below is implemented — Tier 1 statics (PERSISTENCE
> clause in plan_interaction_rules, close-option rewording, charter solvability
> line) + the gated exa probe; Tier 2 reground_criteria trio (the blind early
> derivation was REMOVED — reground is the only pass); Tier 3 fabrication check
> in verify_completion + action-JSON derive guards in BOTH parsers
> (operations_actions._parse_completion_criteria / _parse_output_format_spec).
> The 2026-07-21 canary (adaptive 2/8) ran WITH all of this live; its near
> misses exposed two residual classes, both fixed 2026-07-22:
> 1. **Answer-profile routing trap** (chess-best-move, reproduced on a clean
>    rerun): the router chose code_core WITH profile=answer — a one-answer task
>    decomposed into a 4-goal pipeline, burned its ~14-min task budget
>    mid-build, and forfeited this entire oracle chain (which only runs in
>    ops). Fix: deterministic answer+code_core → ops downgrade in
>    conclude_route + rubric guidance (router_actions.py).
> 2. **Selection-answer false-done** (mteb-retrieve, persistent 1/2): an
>    honestly-computed-WRONG value ("5th highest cosine similarity" → wrong
>    doc) sailed through the fabrication check (the value WAS in the
>    transcript). Fix: verify_completion now requires the ordered candidates +
>    the selection rule applied to be visible for rank/criterion answers.
> Remaining known-unaddressed: path-tracing-class numeric-fidelity misses
> (4/5 at 98% similarity — capability/iteration depth, no oracle rung helps)
> and wall-bound builds that never reach close (pace, not gating).

## Evidence (what the runs showed)
TB2 (87 fails) + the fix-canary tell a sharper story than "missing artifact":
- **Give-up on solvable tasks.** `sqlite-db-truncate`: agent probed, declared *"recovery is impossible — no valid SQLite header,"* `close`d, never wrote `recover.json` → `FileNotFoundError`. 13/87 ended in `close`. The completion loop *did* re-engage, but the agent kept reaching the same dead end — **looping isn't enough; it must retry DIFFERENTLY.**
- **Wrong value, right artifact.** `count-dataset-tokens`: wrote `/app/answer.txt`, value wrong (`Expected 10994372`). Correctness miss — only a self-check helps.
- **Blind-early derivation.** `derive_output_format` / `derive_completion_criteria` run pre-exploration; the format derive sometimes emits **action JSON** (`{"action":"none"}`) instead of a spec (~69% empty).
- **Fabrication.** `mteb-leaderboard`: the judge invented a fake model name rather than reading one.

## What already exists (don't rebuild)
The completion pipeline is mature and the new rungs slot into it idiomatically:
```
plan_interaction(close) → close_session → run_checks → check_sanity → sanity_plausibility
  → profile_oracle → gate_reground → check_format → probe_gate → judge_step
  → reprobe_completion → [verify_completion → record_completion_verify] → decide → success|loop
```
Each oracle rung **appends a REQUIRED fail to `validation_results`**; `judge`/`decide` loop on survivors. New rungs follow that exact pattern (template: `check_sanity` → `record_sanity`, ops_task.cue:204-254). **`exa_search` exists** (`action_exa_search`, registered; `~/.exa_key` present) but isn't wired into ops.

## Convention conformance (PROMPTING_CONVENTIONS.md)
Current oracle prompts are compliant (role/instructions/output, `cache:true` heads, ✅/❌, fenced JSON). The new prompts must respect:
- **§5** — creativity/anti-give-up prompts are *free-text reasoning*; DON'T over-constrain with rigid ✅/❌ (it makes the model match the example instead of thinking). Length/focus guidance only.
- **§9** — author-framing ("acting as a resourceful engineer who…"), **positive instructions over negative**, **show-don't-tell** (concrete reframes), most-important-last.
- **§8** — exa results are a tool return → frame with `Observation:` … `(End of observation.)`, not bare context.
- **§10** — any cycle-reissued prompt orders `cache:true` head + dynamic tail last.
- **§14** — gate the stuck-breaker on `meta.attempt` for retry limits.
- **§6** — keep the close menu description to one framing sentence.

---

## The rungs (prioritized)

### TIER 1 — Anti-give-up: STATIC prompt-engineering + gated exa  *(highest leverage; the `sqlite` case)*
**Operator adjustment:** the creativity triggers are **static prompt language** (always-present prompt engineering), NOT a turn-to-turn charter injection — the charter must not shift under the agent. The *only* dynamic mechanism is the **exa search**, justified because its results are genuinely NEW information that can redirect the fix; tool-result delivery (§8), not a creativity nudge.

1. **Static anti-give-up language** baked into the always-present prompts (prompt engineering, no detection, no injection):
   - **`prompts/run_in_terminal/plan_interaction_rules.yaml`** (the per-turn rules the executing agent reads, where `close` is chosen) — add a tight PERSISTENCE clause (author-framed §9, positive, free-text §5): *the task is solvable by design; before closing as stuck, restate the objective differently, enumerate every factor/resource/exact-error, and try one untried approach; never close with the required output unwritten.*
   - **`flows/shared/run_session.cue`** close option (§6) — drop *"stuck after 3+ failed attempts"* (it licenses abandonment); reword to end only on genuine completion or after distinct approaches AND a reframe.
   - **`prompts/ops/charter_accomplish.yaml`** instruction head (`cache:true`) — one persistence line so the brief itself assumes solvability and pushes through obstacles.

2. **Gated `exa_probe` rung** (the ONLY dynamic part) in `ops_task.cue`: when a cycle ends in give-up (close reason matches `impossible|can't|cannot|stuck|unable|not possible|no valid`) AND `meta.attempt` within a cap, derive a focused query from the objective + obstacle, call the existing `exa_search`, and surface the hits to the NEXT cycle as **new information** — framed `Observation:` (§8) via a `when:`-gated `search_findings` charter section (dynamic *info*, distinct from the static *triggers*). No-ops gracefully on empty results; capped per task for cost.

**Files:** `prompts/run_in_terminal/plan_interaction_rules.yaml` + `prompts/ops/charter_accomplish.yaml` + `flows/shared/run_session.cue` (static language); `flows/ops/ops_task.cue` (the `exa_probe` rung + give-up gate + `search_findings` section wiring), small `detect_giveup` + query-derive action, reuse `action_exa_search`.

### TIER 2 — reground_criteria + block-close-without-artifact
5. **`reground_completion_criteria`** — the exact parallel to the shipped `reground_output_format`: a gated late re-derivation of `completion_criteria`, grounded in `project_manifest` + session tail, so the definition-of-done actually *requires the produced artifact*. Mirror the shipped rung trio (`gate_reground_criteria` → `reground_criteria` → `store_reground_criteria`), one-shot via a `criteria_grounded` flag. New prompt `prompts/ops/reground_completion_criteria.yaml` mirroring `derive_completion_criteria` (JSON, ✅/❌, ROBUST-PREDICATES + NEVER-RE-DERIVE rules) plus the `workspace_context` grounding section.
6. **Block-close-without-artifact** — once criteria are grounded, `run_checks` + `check_format` already append a required fail on a missing artifact; the gap is the agent *closing anyway*. Tighten: in `record_completion_verify`/`decide`, if a required artifact is absent, force `task_done=false` with **directive** feedback (*"You have NOT written `<path>` — produce it before closing"*), not just generic feedback.

### TIER 3 — anti-fabrication + derive reliability + self-check
7. **Anti-fabrication** — strengthen `verify_completion.yaml`: add an explicit check that the claimed answer is **grounded in the transcript** (the value/string must appear in observed command output or a file read), refuting invented values. Free-text §5; one added instruction + one ❌ example (the mteb fake-model case).
8. **Derive reliability** — guard `_parse_output_format_spec` / criteria parse to **reject action-shaped JSON** (`{"action": …}` with no `checks`) as a non-spec, and add a one-line negative to the derive prompt ("this is NOT an action — never output `{\"action\": …}`"). The late reground is the safety net; this fixes the source.
9. **Self-check (correctness)** — the regrounded criteria already run in `run_checks`; the win is *executing them as a pre-submission gate* on the agent's claimed-done, which Tier-2 grounding enables. No new rung — it falls out of reground_criteria.

---

## Sequencing
1. **Tier 1** (stuck-breaker + exa + close wording) — biggest lever (give-ups), mostly control-flow + one free-text charter section.
2. **Tier 2** (reground_criteria + block-close) — copy the proven `reground_output_format` pattern.
3. **Tier 3** (anti-fabrication, derive guard, self-check) — robustness polish.

## Validation
- **Unit:** `detect_giveup` (give-up reason / attempt gating), the reground_criteria gate (one-shot, empty-criteria), the action-JSON parser guard, the anti-fabrication instruction (snapshot). Mirror the existing `test_output_format_oracle.py` style + add the regression that the **gate fires with no terminal_output** (the inert-port lesson).
- **CUE:** recompile + `dev/lint_flows.py` (0 errors) + the `test_compiled_ops_wiring` update for the new rungs.
- **Canary (fresh process, the real test):** re-run `sqlite-db-truncate` (give-up) + a missing-artifact task → confirm the stuck-breaker fires, the charter gets the creativity triggers + exa Observation, and the agent retries differently / writes the artifact. (The shipped reground still needs a live empty-spec canary too.)

## Prereqs / risks
- `~/.exa_key` present ✓. Forced search adds latency + tokens — gate it tightly (`meta.attempt`, once/twice per task).
- Anti-give-up must not cause the *opposite* failure (thrashing forever) — keep the §14 attempt cap so the loop still terminates.
- Don't regress the convention that reasoning/free-text prompts stay un-over-constrained (§5).
