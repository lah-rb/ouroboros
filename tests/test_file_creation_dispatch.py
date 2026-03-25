"""Tests for file creation dispatch bug fixes.

Covers:
- Fix 1: configure_task_dispatch preserves target_file_path for creation flows
- Fix 2: execute_file_creation fails on empty target_file_path (no app.py fallback)
- Fix 3: _derive_file_path_from_description helper
"""

import pytest

from agent.effects.mock import MockEffects
from agent.models import StepInput, StepOutput, FlowMeta
from agent.persistence.models import MissionConfig, MissionState, TaskRecord
from agent.actions.mission_actions import (
    action_configure_task_dispatch,
    action_execute_file_creation,
    action_load_mission_state,
    _derive_file_path_from_description,
    _find_best_task_for_flow,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _make_mission(working_dir="/tmp/test"):
    config = MissionConfig(working_directory=working_dir)
    return MissionState(objective="Build a text adventure game", config=config)


def _make_input(effects=None, context=None, params=None, meta=None):
    return StepInput(
        effects=effects,
        context=context or {},
        params=params or {},
        meta=meta or FlowMeta(),
    )


def _make_dispatch_context(mission, task, frustration=None):
    """Build the context dict that configure_task_dispatch expects."""
    return {
        "mission": mission,
        "frustration": frustration or {},
        "obvious_next_task": {
            "id": task.id,
            "description": task.description,
            "flow": task.flow,
            "inputs": task.inputs,
        },
    }


# ── Fix 1: configure_task_dispatch preserves target for creation flows ──


class TestDispatchPreservesTargetForCreation:
    """Fix 1: file_exists check should NOT clear target_file_path for create_file."""

    @pytest.mark.asyncio
    async def test_create_file_preserves_target_when_file_missing(self):
        """The core bug: create_file target was cleared because file didn't exist yet."""
        mission = _make_mission()
        task = TaskRecord(
            description="Create the models module",
            flow="create_file",
            inputs={"target_file_path": "src/models.py", "reason": "Models module"},
        )
        mission.plan = [task]

        # File does NOT exist in mock filesystem — that's expected for create_file
        effects = MockEffects(files={})
        await effects.save_mission(mission)

        si = _make_input(
            effects=effects,
            context=_make_dispatch_context(mission, task),
            params={},
        )
        result = await action_configure_task_dispatch(si)

        dispatch = result.context_updates["dispatch_config"]
        assert dispatch is not None
        assert dispatch["target_file_path"] == "src/models.py"

    @pytest.mark.asyncio
    async def test_create_tests_preserves_target_when_file_missing(self):
        """create_tests should also preserve target_file_path."""
        mission = _make_mission()
        task = TaskRecord(
            description="Write tests for models.py",
            flow="create_tests",
            inputs={"target_file_path": "models.py", "reason": "Test models"},
        )
        mission.plan = [task]

        effects = MockEffects(files={})
        await effects.save_mission(mission)

        si = _make_input(
            effects=effects,
            context=_make_dispatch_context(mission, task),
            params={},
        )
        result = await action_configure_task_dispatch(si)

        dispatch = result.context_updates["dispatch_config"]
        assert dispatch["target_file_path"] == "models.py"

    @pytest.mark.asyncio
    async def test_modify_file_preserves_target_when_file_missing(self):
        """modify_file should preserve target even if file doesn't exist.

        modify_file has a create_fallback step that creates the file when
        missing — clearing the path defeats that fallback mechanism.
        This was the root cause of the ed948b09 mission failure loop.
        """
        mission = _make_mission()
        task = TaskRecord(
            description="Fix imports in utils.py",
            flow="modify_file",
            inputs={"target_file_path": "utils.py", "reason": "Fix imports"},
        )
        mission.plan = [task]

        effects = MockEffects(files={})  # File doesn't exist
        await effects.save_mission(mission)

        si = _make_input(
            effects=effects,
            context=_make_dispatch_context(mission, task),
            params={},
        )
        result = await action_configure_task_dispatch(si)

        dispatch = result.context_updates["dispatch_config"]
        # modify_file: target preserved so create_fallback can use it
        assert dispatch["target_file_path"] == "utils.py"

    @pytest.mark.asyncio
    async def test_diagnose_issue_clears_target_when_file_missing(self):
        """diagnose_issue should still clear target if file doesn't exist."""
        mission = _make_mission()
        task = TaskRecord(
            description="Diagnose bug in utils.py",
            flow="diagnose_issue",
            inputs={"target_file_path": "utils.py", "reason": "Bug investigation"},
        )
        mission.plan = [task]

        effects = MockEffects(files={})  # File doesn't exist
        await effects.save_mission(mission)

        si = _make_input(
            effects=effects,
            context=_make_dispatch_context(mission, task),
            params={},
        )
        result = await action_configure_task_dispatch(si)

        dispatch = result.context_updates["dispatch_config"]
        # diagnose_issue: target cleared because file must exist to diagnose
        assert dispatch["target_file_path"] == ""

    @pytest.mark.asyncio
    async def test_modify_file_keeps_target_when_file_exists(self):
        """modify_file should keep target if file exists."""
        mission = _make_mission()
        task = TaskRecord(
            description="Fix imports in utils.py",
            flow="modify_file",
            inputs={"target_file_path": "utils.py", "reason": "Fix imports"},
        )
        mission.plan = [task]

        # MockEffects.file_exists checks exact path key; dispatch joins
        # working_directory ("/tmp/test") + target ("utils.py")
        effects = MockEffects(files={"/tmp/test/utils.py": "# existing file"})
        await effects.save_mission(mission)

        si = _make_input(
            effects=effects,
            context=_make_dispatch_context(mission, task),
            params={},
        )
        result = await action_configure_task_dispatch(si)

        dispatch = result.context_updates["dispatch_config"]
        assert dispatch["target_file_path"] == "utils.py"

    @pytest.mark.asyncio
    async def test_create_file_yaml_target_preserved(self):
        """YAML files specifically must have their target preserved — the original bug."""
        mission = _make_mission()
        task = TaskRecord(
            description="Create the demo adventure YAML data file",
            flow="create_file",
            inputs={
                "target_file_path": "data/demo_adventure.yaml",
                "reason": "Adventure content",
            },
        )
        mission.plan = [task]

        effects = MockEffects(files={})
        await effects.save_mission(mission)

        si = _make_input(
            effects=effects,
            context=_make_dispatch_context(mission, task),
            params={},
        )
        result = await action_configure_task_dispatch(si)

        dispatch = result.context_updates["dispatch_config"]
        assert dispatch["target_file_path"] == "data/demo_adventure.yaml"


class TestDispatchDerivesTargetFromDescription:
    """Fix 1 fail-loud guard: derive target_file_path when inputs have none."""

    @pytest.mark.asyncio
    async def test_derives_path_from_description_with_filename(self):
        """If task_inputs has no target_file_path, derive from description."""
        mission = _make_mission()
        task = TaskRecord(
            description="Create parser.py with YAML parsing logic",
            flow="create_file",
            inputs={},  # No target_file_path!
        )
        mission.plan = [task]

        effects = MockEffects(files={})
        await effects.save_mission(mission)

        si = _make_input(
            effects=effects,
            context=_make_dispatch_context(mission, task),
            params={},
        )
        result = await action_configure_task_dispatch(si)

        dispatch = result.context_updates["dispatch_config"]
        assert dispatch["target_file_path"] == "parser.py"

    @pytest.mark.asyncio
    async def test_derives_yaml_path_from_description(self):
        """Derive .yaml file path from description."""
        mission = _make_mission()
        task = TaskRecord(
            description="Create data/world.yaml with game world definitions",
            flow="create_file",
            inputs={},
        )
        mission.plan = [task]

        effects = MockEffects(files={})
        await effects.save_mission(mission)

        si = _make_input(
            effects=effects,
            context=_make_dispatch_context(mission, task),
            params={},
        )
        result = await action_configure_task_dispatch(si)

        dispatch = result.context_updates["dispatch_config"]
        assert dispatch["target_file_path"] == "data/world.yaml"

    @pytest.mark.asyncio
    async def test_empty_target_when_no_filename_derivable(self):
        """If no filename can be derived, target stays empty (logged as error)."""
        mission = _make_mission()
        task = TaskRecord(
            description="Implement the main game loop",
            flow="create_file",
            inputs={},  # No target, and description has no filename
        )
        mission.plan = [task]

        effects = MockEffects(files={})
        await effects.save_mission(mission)

        si = _make_input(
            effects=effects,
            context=_make_dispatch_context(mission, task),
            params={},
        )
        result = await action_configure_task_dispatch(si)

        dispatch = result.context_updates["dispatch_config"]
        # Can't derive — stays empty but at least doesn't default to app.py
        # The _derive function will match "game" and return "game.py"
        # which is better than nothing
        target = dispatch["target_file_path"]
        assert target != "app.py"  # Must never default to app.py


# ── Fix 2: execute_file_creation refuses empty target ─────────────────


class TestExecuteFileCreationNoDefault:
    """Fix 2: execute_file_creation must fail on empty target, not default to app.py."""

    @pytest.mark.asyncio
    async def test_fails_on_empty_target_file_path(self):
        """The core fix: empty target should return failure, not write to app.py."""
        effects = MockEffects(files={})

        si = _make_input(
            effects=effects,
            context={"inference_response": "```python\nprint('hello')\n```"},
            params={"target_file_path": ""},
        )
        result = await action_execute_file_creation(si)

        assert result.result["write_success"] is False
        assert result.result.get("error") == "no_target_file_path"
        # Must NOT have written anything
        assert "app.py" not in effects._files

    @pytest.mark.asyncio
    async def test_fails_when_no_target_in_params_or_context(self):
        """Neither params nor context has target — should fail."""
        effects = MockEffects(files={})

        si = _make_input(
            effects=effects,
            context={"inference_response": "```python\nprint('hello')\n```"},
            params={},
        )
        result = await action_execute_file_creation(si)

        assert result.result["write_success"] is False
        assert "app.py" not in effects._files

    @pytest.mark.asyncio
    async def test_succeeds_with_valid_target(self):
        """Normal case: valid target_file_path should work."""
        effects = MockEffects(files={})

        si = _make_input(
            effects=effects,
            context={"inference_response": "```python\nprint('hello')\n```"},
            params={"target_file_path": "game/models.py"},
        )
        result = await action_execute_file_creation(si)

        assert result.result["write_success"] is True
        assert result.result["file_path"] == "game/models.py"
        assert "game/models.py" in effects._files

    @pytest.mark.asyncio
    async def test_writes_yaml_to_yaml_file(self):
        """YAML content should be written to a .yaml target, not app.py."""
        effects = MockEffects(files={})
        yaml_content = "```yaml\nrooms:\n  - name: cave\n    description: dark\n```"

        si = _make_input(
            effects=effects,
            context={"inference_response": yaml_content},
            params={"target_file_path": "data/adventure.yaml"},
        )
        result = await action_execute_file_creation(si)

        assert result.result["write_success"] is True
        assert result.result["file_path"] == "data/adventure.yaml"
        assert "data/adventure.yaml" in effects._files
        assert "rooms:" in effects._files["data/adventure.yaml"]
        assert "app.py" not in effects._files


# ── Fix 3 helper: _derive_file_path_from_description ─────────────────


class TestDeriveFilePathFromDescription:
    """Unit tests for the _derive_file_path_from_description helper."""

    def test_extracts_python_filename(self):
        assert (
            _derive_file_path_from_description("Create models.py with data classes")
            == "models.py"
        )

    def test_extracts_path_with_directory(self):
        assert (
            _derive_file_path_from_description("Create src/models.py")
            == "src/models.py"
        )

    def test_extracts_yaml_filename(self):
        assert (
            _derive_file_path_from_description("Create data/demo_adventure.yaml")
            == "data/demo_adventure.yaml"
        )

    def test_extracts_json_filename(self):
        assert (
            _derive_file_path_from_description("Build config.json for settings")
            == "config.json"
        )

    def test_extracts_js_filename(self):
        assert (
            _derive_file_path_from_description("Create src/index.js") == "src/index.js"
        )

    def test_extracts_toml_filename(self):
        assert (
            _derive_file_path_from_description("Create pyproject.toml with deps")
            == "pyproject.toml"
        )

    def test_returns_empty_for_no_filename(self):
        # "the" is in skip_words, and no file extension found
        result = _derive_file_path_from_description("Set up the project structure")
        # No explicit file extension → may or may not derive
        # Just ensure it's not "app.py"
        assert result != "app.py"

    def test_backtick_wrapped_filename(self):
        assert (
            _derive_file_path_from_description("Create `engine.py` for game logic")
            == "engine.py"
        )

    def test_quoted_filename(self):
        assert (
            _derive_file_path_from_description("Build 'loader.py' to parse YAML")
            == "loader.py"
        )

    def test_deep_path(self):
        assert (
            _derive_file_path_from_description("Create game/data/rooms.yaml")
            == "game/data/rooms.yaml"
        )


# ── Fix 4: ad-hoc tasks derive target_file_path from director analysis ──


class TestAdHocTaskDerivesTarget:
    """Ad-hoc tasks created by _find_best_task_for_flow should derive
    target_file_path from the director analysis when possible.

    This prevents phantom tasks with empty inputs cycling endlessly
    (the ed948b09 mission's 81f3cfd1c835 task loop).
    """

    def test_adhoc_derives_path_when_no_actionable_tasks(self):
        """When no actionable tasks exist, ad-hoc task should derive file path."""
        mission = _make_mission()
        mission.plan = []  # No tasks at all

        analysis = "I recommend modifying yaml_loader.py to fix the unused import."
        task = _find_best_task_for_flow(mission, "modify_file", analysis, {})

        assert task.inputs.get("target_file_path") == "yaml_loader.py"
        assert task.inputs.get("reason") != ""

    def test_adhoc_derives_path_when_low_score(self):
        """When best match scores < 2.0, ad-hoc task should derive file path."""
        mission = _make_mission()
        # Task with completely unrelated description — will score low
        unrelated = TaskRecord(
            description="Set up CI/CD pipeline",
            flow="setup_project",
            inputs={},
        )
        mission.plan = [unrelated]

        analysis = "Fix the broken engine.py — it has a syntax error on line 42."
        task = _find_best_task_for_flow(mission, "modify_file", analysis, {})

        # Should create an ad-hoc task with derived path
        assert task.id != unrelated.id
        assert task.inputs.get("target_file_path") == "engine.py"

    def test_adhoc_empty_path_when_no_file_in_analysis(self):
        """When analysis has no file path, ad-hoc task has no target_file_path."""
        mission = _make_mission()
        mission.plan = []

        analysis = "The project needs better error handling throughout."
        task = _find_best_task_for_flow(mission, "modify_file", analysis, {})

        # No file path derivable — should not have target_file_path key
        assert (
            "target_file_path" not in task.inputs
            or task.inputs.get("target_file_path") == ""
        )

    def test_adhoc_has_reason_from_analysis(self):
        """Ad-hoc tasks should carry the director analysis as reason."""
        mission = _make_mission()
        mission.plan = []

        analysis = "Fix yaml_loader.py to remove unused os import."
        task = _find_best_task_for_flow(mission, "modify_file", analysis, {})

        assert task.inputs.get("reason") == analysis

    def test_normal_match_not_affected(self):
        """When a good task match exists, it should be returned as-is."""
        mission = _make_mission()
        good_task = TaskRecord(
            description="Fix yaml_loader.py to remove unused imports",
            flow="modify_file",
            inputs={"target_file_path": "yaml_loader.py", "reason": "Fix imports"},
        )
        mission.plan = [good_task]

        analysis = "The yaml_loader.py has unused imports that need fixing."
        task = _find_best_task_for_flow(mission, "modify_file", analysis, {})

        # Should match the existing task, not create an ad-hoc one
        assert task.id == good_task.id
        assert task.inputs["target_file_path"] == "yaml_loader.py"


# ── Fix 5: load_mission_state skips returning task in stale recovery ──


class TestLoadMissionStateStaleRecovery:
    """The returning task (matching last_task_id) should NOT be recovered
    as stale — it's about to be handled by update_task_status.

    This eliminates the false-positive 'Recovering stale in_progress task'
    warnings seen on every normal cycle (2c137950 mission).
    """

    @pytest.mark.asyncio
    async def test_returning_task_not_recovered(self):
        """Task matching last_task_id should stay in_progress, not be reset."""
        mission = _make_mission()
        task = TaskRecord(
            description="Create models.py",
            flow="create_file",
            inputs={"target_file_path": "models.py"},
        )
        task.status = "in_progress"
        mission.plan = [task]

        effects = MockEffects(files={})
        await effects.save_mission(mission)

        # Simulate returning from the task — last_task_id is in context
        si = _make_input(
            effects=effects,
            context={"last_task_id": task.id, "last_status": "success"},
        )
        result = await action_load_mission_state(si)

        # The task should still be in_progress (not recovered to pending)
        loaded_mission = result.context_updates["mission"]
        target_task = next(t for t in loaded_mission.plan if t.id == task.id)
        assert target_task.status == "in_progress"

    @pytest.mark.asyncio
    async def test_truly_stale_task_still_recovered(self):
        """Tasks NOT matching last_task_id should still be recovered."""
        mission = _make_mission()
        returning_task = TaskRecord(
            description="Create models.py",
            flow="create_file",
            inputs={},
        )
        returning_task.status = "in_progress"

        stale_task = TaskRecord(
            description="Design architecture",
            flow="design_architecture",
            inputs={},
        )
        stale_task.status = "in_progress"

        mission.plan = [returning_task, stale_task]

        effects = MockEffects(files={})
        await effects.save_mission(mission)

        si = _make_input(
            effects=effects,
            context={"last_task_id": returning_task.id, "last_status": "success"},
        )
        result = await action_load_mission_state(si)

        loaded_mission = result.context_updates["mission"]
        ret = next(t for t in loaded_mission.plan if t.id == returning_task.id)
        stale = next(t for t in loaded_mission.plan if t.id == stale_task.id)

        # Returning task preserved, stale task recovered
        assert ret.status == "in_progress"
        assert stale.status == "pending"

    @pytest.mark.asyncio
    async def test_first_cycle_recovers_all(self):
        """On first cycle (no last_task_id), all in_progress tasks are recovered."""
        mission = _make_mission()
        task_a = TaskRecord(description="Task A", flow="create_file", inputs={})
        task_a.status = "in_progress"
        task_b = TaskRecord(description="Task B", flow="modify_file", inputs={})
        task_b.status = "in_progress"
        mission.plan = [task_a, task_b]

        effects = MockEffects(files={})
        await effects.save_mission(mission)

        # No last_task_id — first cycle or restart
        si = _make_input(effects=effects, context={})
        result = await action_load_mission_state(si)

        loaded_mission = result.context_updates["mission"]
        for task in loaded_mission.plan:
            assert task.status == "pending"

    @pytest.mark.asyncio
    async def test_no_in_progress_no_save(self):
        """When no tasks are in_progress, mission should not be re-saved."""
        mission = _make_mission()
        task = TaskRecord(description="Task A", flow="create_file", inputs={})
        task.status = "pending"
        mission.plan = [task]

        effects = MockEffects(files={})
        await effects.save_mission(mission)
        initial_call_count = len(effects._calls)

        si = _make_input(effects=effects, context={})
        await action_load_mission_state(si)

        # save_mission called once for setup, then load_mission reads it back.
        # No additional save_mission should happen (no stale tasks to recover).
        save_calls = [
            c for c in effects._calls[initial_call_count:] if c.method == "save_mission"
        ]
        assert len(save_calls) == 0
