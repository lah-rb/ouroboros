# Ouroboros — Contributing Guide

*Patterns and recipes for adding new capabilities to Ouroboros. For architectural
overview, see `IMPLEMENTATION.md`. For operational guidance, see `AGENT.md`.*

---

## Adding a New Task Flow

1. **Create the YAML** in `flows/tasks/<flow_name>.yaml`.
2. Follow the flow definition format (see `IMPLEMENTATION.md` §2.1.2).
3. Every flow must have:
   - `flow`, `version`, `description`, `input`, `entry` fields.
   - At least one terminal step with `terminal: true` and `status`.
   - A tail-call back to `mission_control` on terminal steps.
4. Register the flow in `flows/registry.yaml` with its metadata.
5. Ensure `create_plan.yaml`'s prompt knows about the new flow (it selects flows by name).
6. Add tests in `tests/` covering the flow's key paths.

**Template:** Copy an existing task flow that's similar in structure. `modify_file.yaml`
is a good general-purpose starting point. `diagnose_issue.yaml` is good for investigation
flows.

---

## Adding a New Action

1. **Create the action function** in the appropriate `agent/actions/<category>_actions.py`
   file, or create a new category file.
2. Signature must be: `async def action_name(step_input: StepInput) -> StepOutput`.
3. All side effects go through `step_input.effects` — never direct file I/O, subprocess,
   or network calls.
4. **Register** in `agent/actions/registry.py` → `build_action_registry()`.
5. Add tests in `tests/test_new_actions.py` using `MockEffects`.

**Naming convention:** Action names in the registry are snake_case and should match the
function name minus the `action_` prefix. E.g., `action_read_files` → registered as
`read_files`.

---

## Adding a New Shared Sub-flow

1. **Create the YAML** in `flows/shared/<flow_name>.yaml`.
2. Shared sub-flows are invoked via `action: flow` from parent steps.
3. They should be focused and reusable — one clear responsibility.
4. Document inputs/outputs clearly since multiple parent flows will depend on the contract.
5. Add to the shared flow inventory in `IMPLEMENTATION.md` §3.2.

---

## Adding a New Resolver Type

1. **Create** `agent/resolvers/<type_name>.py` with a `resolve()` function.
2. **Register** in `agent/resolvers/__init__.py` dispatcher.
3. **Update** `agent/loader.py` validation to accept the new resolver type.
4. Add tests in `tests/test_resolvers.py`.

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

Reusable step configurations live in `flows/shared/step_templates.yaml`. To use a
template in a flow step:

```yaml
steps:
  my_step:
    template: template_name
    # Any fields here override the template defaults
    description: "Custom description"
```

The loader merges template fields with step-level overrides at load time. Step-level
values always win. See `agent/loader.py` for merge logic.

---

## Development Cycle

**ALWAYS follow this sequence (also documented in `AGENT.md`):**

1. `uv run pytest tests/ -v` — run full test suite first.
2. Make code changes.
3. `uv run black .` — format (Black can change line numbers affecting assertions).
4. `uv run pytest tests/ -v` — verify changes pass.
5. `uv run ouroboros.py mission create --mission_config test_config` — live verification.
6. If tests fail → investigate → fix → return to step 1.

Step 5 is NOT optional for feature work.

---

## Naming Conventions

| Entity | Convention | Example |
|--------|-----------|---------|
| Flow names | snake_case | `modify_file`, `research_context` |
| Step names | snake_case | `gather_context`, `plan_change` |
| Action names | snake_case | `read_files`, `load_mission_state` |
| Context keys | snake_case | `target_file`, `mission`, `frustration` |
| Python modules | snake_case | `terminal_actions.py`, `mission_config.py` |
| Test files | `test_<module>.py` | `test_runtime.py`, `test_effects.py` |
| Pydantic models | PascalCase | `FlowDefinition`, `StepInput`, `MissionState` |

---

## Package Management

**This project uses `uv` exclusively.** Never call `python`, `pip`, or `pip install`
directly. See `AGENT.md` for the full `uv` command reference.
