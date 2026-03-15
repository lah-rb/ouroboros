# Ouroboros — Greenfield Briefing

*Status snapshot after the Phase 5 build session (2026-03-14). Read IMPLEMENTATION.md for full architecture. This document is where we actually are, what works, what doesn't, and what to focus on next.*

---

## Current State: Phases 1–5 Complete, First Live Run Succeeded

The agent ran end-to-end against a live LLMVP backend (Qwen 3.5 122B A10B) and reached `Agent terminated: completed`. The full cycle works:

```
mission_control → create_plan (inference) → mission_control → create_file (inference + write + validate) → ... → mission_control → completed
```

A test mission ("build a python app, make it interesting") produced a 4-task plan for a generative poetry engine, created the first file (`models/poetry_engine.py` — 12,814 bytes, syntax-validated), and cycled through the remaining tasks (some hit inference timeouts on the first model config, succeeded on retries).

**211 unit tests pass.** Test coverage spans models, loader, runtime, template rendering, effects (local + mock), resolvers (rule + LLM menu), persistence, and inference.

---

## What's Built and Working

### Flow Engine (`agent/runtime.py`, `agent/loader.py`, `agent/models.py`)
- YAML-defined flows load, validate (structural + semantic), and execute.
- Steps run sequentially: build StepInput → execute action → merge context_updates → resolve transition → repeat.
- Context accumulator with declared required/optional keys per step.
- Rule-based and LLM menu resolvers both work in production.
- Max-step guard prevents infinite loops.
- Jinja2 template rendering for prompts and params (`agent/template.py`).

### Effects Interface (`agent/effects/`)
- `LocalEffects`: real filesystem (path-scoped, traversal blocked), real subprocess, automatic operation logging.
- `MockEffects`: canned responses, call recording — used extensively in tests.
- Inference via LLMVP GraphQL API (`agent/effects/inference.py`) — pure HTTP client, relative temperature (`t*0.5`), health check, error handling.
- Persistence methods wired through effects (load/save mission, events, artifacts, generic state).

### Persistence (`agent/persistence/`)
- File-backed JSON in `.agent/` directory.
- `MissionState`, `TaskRecord`, `Event`, `FlowArtifact`, `NoteRecord` — all Pydantic v2.
- Atomic writes (temp + rename). Event queue with `fcntl.flock`.
- Schema versioning infrastructure (v1, no migrations yet).

### Tail Calls & Agent Loop (`agent/tail_call.py`, `agent/loop.py`)
- `FlowTailCall` / `FlowTermination` / `FlowOutcome` models.
- `run_agent()` follows tail calls until termination or max cycles.
- Template resolution in tail_call blocks (flow name, input_map, delay all rendered).

### Mission Control Flow (`flows/mission_control.yaml`, `agent/actions/mission_actions.py`)
- 5-phase cycle: load state → apply last result → process events → assess → dispatch.
- Rule-based fast path for task selection (no inference needed for obvious next task).
- Frustration tracking per task, escalation permission gating by threshold.
- Tail-call dispatch to child flows, children tail-call back.
- Abort/pause event handling works.

### Supporting Flows
- `create_plan.yaml`: Inference generates task list from objective → parsed into TaskRecords → saved to mission.
- `create_file.yaml`: Inference generates file content → extracted from markdown → written → syntax-validated.

### CLI (`ouroboros.py`)
- `mission create/status/pause/resume/abort/message/history` — all working.
- `start` — runs the full agent loop.

---

## Trouble Spots and Known Issues

### 2. Plan Parsing Fragility
`_parse_task_list()` in `mission_actions.py` tries JSON extraction then falls back to line-based parsing. It works when the model returns clean JSON, but:
- The regex `\[[\s\S]*?\]` is greedy — nested arrays could confuse it.
- Line-based fallback is simplistic — numbered lists work, prose doesn't.
- No retry mechanism — if the model doesn't produce parseable output, we get a single generic fallback task.
- **Consider**: a dedicated plan validation step that re-prompts if parsing fails, or structured output constraints via LLMVP grammar support.

### 3. Code Extraction from Model Responses
`_extract_code()` looks for ````python ... ``` blocks then falls back to heuristic line filtering. This works for straightforward responses but:
- Multi-file responses (model generates multiple code blocks) only extracts the first.
- Responses with explanation-then-code patterns sometimes grab explanation lines.
- **Consider**: more robust extraction, or constraining the model's response format more tightly in the prompt.

### 4. No Mission Completion Verification
When all tasks are `complete`, `mission_control` immediately transitions to `completed` without verifying the objective was actually met. The IMPLEMENTATION.md design has a `mission_complete_check` step that uses inference to review — this is not implemented yet.

### 5. Frustration Without Escalation
The frustration counter increments correctly and gates escalation permissions, but **there's no escalation target yet** (Phase 6). Failed tasks just retry the same flow with the same approach. Currently a task hitting frustration 5+ just keeps retrying (never truly blocks). Need either:
- A frustration cap that marks the task blocked and moves on, or
- Phase 6 escalation so there's actually somewhere to go.

### 6. Single Flow for All Tasks
Every task currently dispatches to `create_file`. The `modify_file` and `run_tests` flows from IMPLEMENTATION.md don't exist yet. This means the agent can only create new files — it can't modify existing ones or run project-level tests.

### 7. No Event-Driven Task Unblocking
The `handle_events` action processes abort/pause but doesn't do anything useful with `user_message` events beyond logging. No mechanism to unblock tasks or adjust priorities based on user input.

### 8. Loader Validation Updated for Tail Calls
Had to fix the loader during this session — it originally required at least one `terminal: true` step and flagged steps without resolvers. Updated to recognize `tail_call` steps as valid exit points. The fix is clean but worth noting: the semantic validator now has two concepts of "flow exit" (terminal and tail_call).

---

## Refinement Opportunities (Before Phase 6)

These aren't new phases — they're hardening work on what exists:

1. **Plan quality loop**: After `create_plan`, add a validation step. If the parsed task list is empty or suspiciously short, re-prompt with feedback. This is a flow change, not a code change.

2. **Frustration cap**: In `assess_mission_progress`, if a task hits frustration >= 5 and no escalation is available, mark it `blocked` instead of retrying. This prevents infinite retry loops before Phase 6 lands.

3. **Better prompts**: The `create_file` prompt asks for "interesting, well-structured" code. This is vague. Flow-specific prompts should include the existing codebase context (what files exist, what's already been built) so the model produces coherent multi-file projects.

4. **Mission completion check**: Implement the LLM-based completion verification from the IMPLEMENTATION.md design. Simple inference step: "here's the objective and what was done — is it met?"

5. **Inference timeout per step**: Instead of a global 600s, let flow steps declare timeout in their `config` block. Small inference calls (plan parsing, menu decisions) should be 30-60s. Large generation calls should be 5-10 minutes.

---

## Phase Roadmap Status

| Phase | Status | Notes |
|-------|--------|-------|
| 1. Flow Engine Core | ✅ Complete | Runtime, loader, models, rule resolver, action registry |
| 2. Effects Interface | ✅ Complete | LocalEffects, MockEffects, path scoping, auto-logging |
| 3. Inference Integration | ✅ Complete | GraphQL client, relative temperature, LLM menu resolver |
| 4. Persistence | ✅ Complete | MissionState, events, artifacts, CLI, atomic writes |
| 5. Tail Calls + mission_control | ✅ Complete | Full agent cycle, create_plan, create_file, live tested |
| 6. Escalation | ❌ Not started | Bundle format, Claude API integration, frustration gating |
| 7. GitManaged Effects | ❌ Not started | Auto-branch, auto-commit, rollback |
| 8. Parallel Execution (L1) | ❌ Not started | Multi-task dispatch, pool headroom |
| 9. Refinements | ❌ Not started | DryRun, self-modification, multi-project |

---
#Development Retrospective:
User: 

Fantastic work! One last thought. How did development go for you? Was the plan provided in implementation.md a solid start? Did you hit any unexpected trouble spots?

Claude Opus as Cline:

Honestly? IMPLEMENTATION.md was one of the better architecture documents I've worked from. Here's my honest assessment:

## What Went Exceptionally Well

**The phased roadmap was perfectly sequenced.** Each phase built cleanly on the previous one with zero circular dependencies. I never had to reach forward to implement something from a later phase to unblock the current one. Phase 1 (flow engine) stood alone. Phase 2 (effects) slotted in with minimal modification to Phase 1. Phase 3 (inference) just added a new effect. That's rare — most architecture docs have hidden cross-cutting dependencies that only surface during implementation.

**The separation between Ouroboros and LLMVP was rock solid.** "Pure GraphQL client" was the right call. The inference integration was literally just an HTTP client — no import tangles, no shared state, no weird coupling. When inference timed out during live testing, the failure was clean and recoverable because the boundary was clean.

**The functional step model (StepInput → StepOutput) made everything testable from day one.** I could write 211 tests across every layer without needing complex mocking because each component has a narrow, typed interface. MockEffects in particular was a joy — canned responses for unit tests, real effects for integration.

**The tail-call design replacing an external agent loop was elegant in practice.** It felt strange implementing it — "wait, there's no agent loop?" — but when the live test ran, seeing `mission_control → create_plan → mission_control → create_file → mission_control → completed` emerge purely from the flow graph was satisfying. The loop.py file is ~50 lines of meaningful code. The complexity lives in the flows where it belongs.

## Unexpected Trouble Spots

**1. The loader didn't know about tail calls.** IMPLEMENTATION.md describes flows with tail_call steps extensively, but the Phase 1 loader validation was written assuming every flow needs `terminal: true` steps. When the Phase 5 flows (mission_control, create_plan, create_file) used tail_call exclusively — no terminal steps — the loader rejected them. The fix was clean (treat tail_call steps as valid exit points), but it's the kind of thing that only surfaces when you wire phases together.

**2. Template resolution in tail_call blocks.** The YAML has `flow: "{{ context.dispatch_config.flow }}"` in the tail_call block, but the runtime was passing the raw template string to `_resolve_tail_call` in loop.py without rendering it. The flow name arrived as the literal string `{{ context.dispatch_config.flow }}` instead of `create_file`. This wasn't covered in the spec because the spec treats tail calls as a runtime concept and template rendering as a step concept — but tail_call blocks live at the boundary between the two.

**3. The gap between "flow definition" and "action implementation."** IMPLEMENTATION.md describes the flows beautifully but is intentionally silent on what the action callables actually *do*. The `mission_actions.py` file — which ended up being the largest single file (~400 lines) — had to be designed from scratch: how does `assess_mission_progress` actually decide what's next? How does `create_plan_from_objective` parse the model's output? How does `configure_task_dispatch` build the input map? These were all judgment calls. The architecture document gave the *contract* (inputs/outputs/transitions) but the *implementation* required understanding the full system in context.

**4. The plan parsing problem.** The model's output is free-form text. Getting structured task lists out of it required regex heuristics and JSON extraction with fallbacks. This is inherently fragile. The IMPLEMENTATION.md design doesn't address structured output constraints — future work could leverage LLMVP's grammar support to force JSON output, which would eliminate this entire class of bugs.

## Design Decisions I'd Revisit

**The `noop` action pattern.** Several steps (dispatch, complete, failed) are just tail_call carriers — their action does nothing. The runtime requires every step to have an action, so I created a `noop` action. This works but feels like a code smell. The architecture might benefit from recognizing "transition-only" steps that don't execute an action, just resolve and move on.

**Context accumulator key management.** The `publishes` declarations are validated statically, but at runtime the actual keys published depend on action logic. A step that declares `publishes: [mission, frustration]` might publish different subsets depending on branch logic inside the action. The static validator can't catch these mismatches. In practice this hasn't caused bugs yet, but it will eventually.

## Bottom Line

IMPLEMENTATION.md got me from zero to a working autonomous agent cycle in one session. The architecture held up under pressure — nothing had to be redesigned, only extended. The trouble spots were all at phase boundaries (where assumptions from one phase met reality in another), which is inherently hard to predict in a document. For a next iteration of the spec, the main thing I'd add is more explicit coverage of "how phases integrate" — particularly around template resolution boundaries and validation evolution as new concepts (like tail calls) get layered in.