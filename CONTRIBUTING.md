# Ouroboros — Contributing Guide

*Patterns and recipes for adding new capabilities to Ouroboros. For architectural
overview, see `IMPLEMENTATION.md`. For operational guidance, see `AGENT.md`.*

---

## Adding a New Task Flow

1. **Create the CUE file** in the flow's set directory — `flows/code_core/<flow_name>.cue` for the code pipeline, `flows/shared/<flow_name>.cue` only if the flow is set-agnostic (usable by any mission type).
2. Define the flow using `#FlowDefinition &` schema (see `flows/shared/flow.cue` and
   `IMPLEMENTATION.md` §2.1.2).
3. Every flow must have:
   - `flow`, `version`, `description`, `input`, `entry` fields.
   - At least one terminal step with `terminal: true` and `status`.
   - A tail-call back to `mission_control` on terminal steps.
4. Create prompt templates in `prompts/<flow_name>/<step>.yaml` for inference steps.
5. Rebuild: `uv run ouroboros.py cue-compile` (validates CUE and exports `flows/compiled.json`).
7. Ensure `design_and_plan`'s planning prompt knows about the new flow (it selects flows by name).
8. Add tests in `tests/` covering the flow's key paths.

**Template:** Copy an existing task flow that's similar in structure. `diagnose_issue.cue`
is a good starting point for investigation flows. `file_ops.cue` shows the full
create/modify lifecycle pattern.

---

## Adding a New Action

1. **Create the action function** in the appropriate `agent/actions/<category>_actions.py`
   file, or create a new category file.
2. Signature must be: `async def action_name(step_input: StepInput) -> StepOutput`.
3. All side effects go through `step_input.effects` — never direct file I/O, subprocess,
   or network calls.
4. **Register** in `agent/actions/registry.py` → `build_action_registry()`.
5. Add tests in `tests/` using `MockEffects`.

**Naming convention:** Action names in the registry are snake_case and should match the
function name minus the `action_` prefix. E.g., `action_read_files` → registered as
`read_files`.

---

## Adding a New Shared Sub-flow

1. **Create the CUE file** in the flow's set directory — `flows/code_core/<flow_name>.cue` for the code pipeline, `flows/shared/<flow_name>.cue` only if the flow is set-agnostic (usable by any mission type).
2. Shared sub-flows are invoked via `action: flow` from parent steps.
3. They should be focused and reusable — one clear responsibility.
4. Document inputs/outputs clearly since multiple parent flows will depend on the contract.
5. Rebuild: `uv run ouroboros.py cue-compile`.

---

## Adding a New Resolver Type

1. **Create** `agent/resolvers/<type_name>.py` with a `resolve()` function.
2. **Register** in `agent/resolvers/__init__.py` dispatcher.
3. **Update** `agent/loader.py` validation to accept the new resolver type.
4. Add tests in `tests/`.

The resolver receives the step output, context accumulator, resolver definition, and
effects interface. It returns the name of the next step (transition target).

---

## Adding a New Effect

1. **Extend** `agent/effects/protocol.py` with the new method signature.
2. **Implement** in `agent/effects/local.py` (production behavior).
3. **Implement** in `agent/effects/mock.py` (test behavior — canned responses + recording).
4. Update any other effects implementations (`DryRunEffects`, etc.) if they exist.
5. Add tests verifying both real and mock behavior.

---

## Modifying Prompts

**ALWAYS consult `PROMPTING_CONVENTIONS.md` before modifying any prompt.**

Key rules:
- Follow the three-section pattern: Role + Context → Task + Materials → Output Format.
- Output format section must appear last with ✅ CORRECT and ❌ WRONG examples.
- Use `t*` temperature specifiers: `t*0.5` for deterministic, `t*1` for balanced,
  `t*1.2` for creative.
- Test prompt changes with live inference (`--mission_config test_config`), not just
  unit tests.

---

## Step Templates

Reusable step configurations live in `flows/shared/templates.cue`. To use a
template in a flow step:

```cue
steps: {
    my_step: #StepDefinition & _templates.template_name & {
        // Any fields here override the template defaults
        description: "Custom description"
    }
}
```

The loader merges template fields with step-level overrides at load time. Step-level
values always win. See `agent/loader.py` for merge logic.

---

## Development Cycle

**See `AGENT.md` for the full development cycle.** In summary: code → format with
`black` → lint with `ruff check` (auto-fix with `--fix`) → test where tests exist →
`cue-compile` on CUE changes → `ouroboros.py smoke` → `ouroboros.py lint-flows` →
live verification via `mission create --mission_config <test_config>`.

Live verification is the only real test for feature work touching flows, actions, or
prompts — smoke tests load flows and begin execution without inference, which catches
structural regressions but not semantic ones.

---

## Output Preservation Policy

**Never truncate, cap, or slice content that will be parsed downstream.**

This applies to any data flowing into `json.loads`, `json_repair.loads`, structured extraction, or any parser. Truncated JSON is unparseable JSON, and the silent fallbacks that follow (default-to-true verification, empty plan arrays, skipped goals) cause cascading failures across the mission lifecycle.

**Never ask the model for bare text responses that will be parsed programmatically.**

All machine-parsed model output must use fenced JSON (```` ```json ```` blocks) or the `{"choice": "..."}` menu format. Use `parse_llm_json()` from `agent/llm_json.py` for all extraction. Bare text prompts ("respond with the file path, e.g.: engine.py") fail at ~50% rates on sparse MoE models due to EOS token leakage, JSON wrapping, and chat template bleed. If you need a string value from the model, wrap it in a JSON field: `{"file": "engine.py"}` not `engine.py`.

Specific anti-patterns to avoid:
- `response[:N]` before JSON parsing
- `max_tokens` set too low on inference calls that produce structured output
- `content[:500]` on error output that feeds into diagnosis or rework directives
- `text[:200]` on verification evidence that determines pass/fail

If you encounter existing truncation in a code path that parses the result, **report it as a bug**. Display-only truncation (trace logs, observation strings, UI summaries) is fine — the test is whether anything downstream will try to parse the truncated content as structured data.

---

## Naming Conventions

| Entity | Convention | Example |
|--------|-----------|---------|
| Flow names | snake_case | `rewrite`, `research_context` |
| Step names | snake_case | `gather_context`, `plan_change` |
| Action names | snake_case | `read_files`, `load_mission_state` |
| Context keys | snake_case | `target_file`, `mission`, `flow_directive` |
| Python modules | snake_case | `terminal_actions.py`, `mission_config.py` |
| Test files | `test_<module>.py` | `test_flow_loader.py` |
| Pydantic models | PascalCase | `FlowDefinition`, `StepInput`, `MissionState` |

---

## Package Management

**This project uses `uv` exclusively.** Never call `python`, `pip`, or `pip install`
directly. See `AGENT.md` for the full `uv` command reference.
