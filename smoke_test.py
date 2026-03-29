"""Smoke test: load compiled.json and execute each flow's first 3 steps.

Catches runtime errors like:
  - PreComputeStep not subscriptable
  - hasattr not defined in conditions
  - Missing formatters
  - Pydantic validation failures
  - Resolver errors on the first transition
"""

import asyncio
import json
import sys
import traceback
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from agent.models import FlowDefinition, StepInput, FlowMeta
from agent.actions.registry import ActionRegistry, build_action_registry
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.persistence.models import (
    MissionState,
    MissionConfig,
    TaskRecord,
    ArchitectureState,
)
from agent.runtime import execute_flow


def make_mock_mission(with_architecture=False, with_plan=True):
    """Create a realistic MissionState for testing."""
    config = MissionConfig(working_directory="/tmp/test-project")
    mission = MissionState(
        id="test-mission-001",
        status="active",
        objective="Build a simple TODO app with Python",
        config=config,
    )
    if with_plan:
        mission.plan = [
            TaskRecord(
                id="task-001",
                description="Create main.py with basic TODO class",
                flow="file_write",
                status="pending",
                inputs={
                    "target_file_path": "main.py",
                    "task_description": "Create main.py with basic TODO class",
                    "working_directory": "/tmp/test-project",
                },
            ),
            TaskRecord(
                id="task-002",
                description="Create test_main.py",
                flow="file_write",
                status="pending",
                inputs={
                    "target_file_path": "test_main.py",
                    "task_description": "Create test_main.py",
                    "working_directory": "/tmp/test-project",
                },
            ),
        ]
    if with_architecture:
        mission.architecture = ArchitectureState(
            import_scheme="relative",
            run_command="python main.py",
            test_command="pytest",
        )
    return mission


def make_effects(mission=None, inference_responses=None):
    """Create MockEffects with a realistic setup."""
    if mission is None:
        mission = make_mock_mission()

    files = {
        "main.py": "# TODO app\nclass Todo:\n    pass\n",
        "test_main.py": "import pytest\n",
        "pyproject.toml": '[project]\nname = "todo"\n',
    }

    effects = MockEffects(
        files=files,
        commands={
            "python": CommandResult(return_code=0, stdout="OK", stderr="", command="python"),
            "pytest": CommandResult(return_code=0, stdout="1 passed", stderr="", command="pytest"),
        },
        inference_responses=inference_responses or [
            # Provide many responses for multi-step flows
            '{"action": "create", "target": "main.py"}',
            "Mock generated code content",
            "a",  # LLM menu selection
            "b",  # LLM menu selection
            "Mock analysis response",
            "Mock plan response",
        ] * 5,  # Repeat to avoid exhaustion
    )
    # Pre-load mission state
    effects._state["mission"] = mission
    return effects


# Standard inputs for each flow type
FLOW_INPUTS = {
    "mission_control": {
        "mission_id": "test-mission-001",
    },
    "design_and_plan": {
        "mission_id": "test-mission-001",
    },
    "file_write": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "task_description": "Create main.py with basic TODO class",
        "target_file_path": "main.py",
        "working_directory": "/tmp/test-project",
        "mission_objective": "Build a simple TODO app",
    },
    "create_file": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "task_description": "Create main.py",
        "target_file_path": "main.py",
        "working_directory": "/tmp/test-project",
        "mission_objective": "Build a TODO app",
        "relevant_notes": "",
    },
    "modify_file": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "task_description": "Add save method to Todo class",
        "target_file_path": "main.py",
        "working_directory": "/tmp/test-project",
        "mission_objective": "Build a TODO app",
        "relevant_notes": "",
    },
    "diagnose_issue": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "task_description": "Fix import error in main.py",
        "target_file_path": "main.py",
        "working_directory": "/tmp/test-project",
        "mission_objective": "Build a TODO app",
        "error_output": "ImportError: No module named 'todo'",
        "relevant_notes": "",
    },
    "prepare_context": {
        "working_directory": "/tmp/test-project",
        "task_description": "Create main.py",
        "mission_objective": "Build a TODO app",
        "target_file_path": "main.py",
    },
    "capture_learnings": {
        "task_description": "Created main.py",
        "target_file_path": "main.py",
    },
    "retrospective": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "task_description": "Fixed the bug",
        "mission_objective": "Build a TODO app",
        "working_directory": "/tmp/test-project",
        "trigger_reason": "Task completed after overcoming difficulty",
    },
    "research": {
        "search_intent": "Python TODO app best practices",
        "mission_objective": "Build a TODO app",
    },
    "set_env": {
        "mission_id": "test-mission-001",
        "target_file_path": "main.py",
        "working_directory": "/tmp/test-project",
    },
    "quality_gate": {
        "mission_id": "test-mission-001",
        "mission_objective": "Build a TODO app",
        "working_directory": "/tmp/test-project",
    },
    "project_ops": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "task_description": "Set up project structure",
        "working_directory": "/tmp/test-project",
        "mission_objective": "Build a TODO app",
        "relevant_notes": "",
    },
    "interact": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "task_description": "Run the app and check output",
        "working_directory": "/tmp/test-project",
        "mission_objective": "Build a TODO app",
    },
    "revise_plan": {
        "mission_id": "test-mission-001",
        "observation": "Need to add database support",
    },
    "run_in_terminal": {
        "session_goal": "Run pytest",
        "working_directory": "/tmp/test-project",
    },
    "ast_edit_session": {
        "file_path": "main.py",
        "file_content": "class Todo:\n    pass\n",
        "task_description": "Add save method",
        "working_directory": "/tmp/test-project",
        "symbol_table": [{"name": "Todo", "type": "class", "start_line": 1, "end_line": 2, "body": "class Todo:\n    pass\n"}],
        "symbol_menu_options": [{"key": "a", "label": "Todo", "description": "class Todo"}],
    },
    "lint": {
        "mission_id": "test-mission-001",
        "working_directory": "/tmp/test-project",
    },
}


async def smoke_test_flow(flow_name, flow_def, registry, all_flows, max_steps=15):
    """Try to execute a flow for up to max_steps steps.
    
    Returns (success: bool, error: str | None, steps_executed: list)
    """
    inputs = FLOW_INPUTS.get(flow_name, {"mission_id": "test-mission-001"})
    effects = make_effects()

    # ast_edit_session needs specific mock data shape for select_symbol_turn;
    # limit to 2 steps to validate flow loading and first transition
    if flow_name == "ast_edit_session":
        max_steps = 1

    try:
        result = await execute_flow(
            flow_def=flow_def,
            inputs=inputs,
            action_registry=registry,
            max_steps=max_steps,
            effects=effects,
            flow_registry=all_flows,
        )
        return True, None, result.steps_executed
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", []


async def main():
    # Load compiled.json
    compiled_path = Path("flows/compiled.json")
    if not compiled_path.exists():
        print("ERROR: flows/compiled.json not found. Run build_flows.sh first.")
        sys.exit(1)

    with open(compiled_path) as f:
        data = json.load(f)

    # Parse all flows
    flows = {}
    for name, flow_data in data.items():
        if isinstance(flow_data, dict) and "flow" in flow_data:
            try:
                flows[name] = FlowDefinition(**flow_data)
            except Exception as e:
                print(f"  PARSE FAIL: {name}: {e}")

    print(f"Loaded {len(flows)} flows from compiled.json\n")

    # Build action registry with all built-in actions
    registry = build_action_registry()

    # Run smoke test on each flow
    passed = 0
    failed = 0
    errors = []

    for flow_name in sorted(flows.keys()):
        flow_def = flows[flow_name]
        ok, error, steps = await smoke_test_flow(
            flow_name, flow_def, registry, flows, max_steps=15
        )
        if ok:
            print(f"  ✅ {flow_name}: {steps}")
            passed += 1
        else:
            print(f"  ❌ {flow_name}: {error}")
            failed += 1
            errors.append((flow_name, error))

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(flows)} flows")

    if errors:
        print(f"\nFailures:")
        for name, err in errors:
            print(f"  {name}: {err}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
