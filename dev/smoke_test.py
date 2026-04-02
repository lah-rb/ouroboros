"""Smoke test: load compiled.json and execute each flow's first steps.

Catches runtime errors like:
  - PreComputeStep not subscriptable
  - hasattr not defined in conditions
  - Missing formatters
  - Pydantic validation failures
  - Resolver errors on the first transition

Updated for Context Contract Architecture (flow_directive inputs,
current flow names, proper mock data shapes).
"""

import asyncio
import json
import sys
import traceback
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

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
                flow="create",
                status="pending",
                inputs={
                    "target_file_path": "main.py",
                    "reason": "Create main.py with basic TODO class",
                },
            ),
            TaskRecord(
                id="task-002",
                description="Create test_main.py",
                flow="create",
                status="pending",
                inputs={
                    "target_file_path": "test_main.py",
                    "reason": "Create test_main.py",
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
        "main.py": "# TODO app\nimport json\n\nclass Todo:\n    pass\n",
        "test_main.py": "import pytest\nfrom main import Todo\n",
        "pyproject.toml": '[project]\nname = "todo"\ndependencies = ["pytest"]\n',
    }

    default_cmd = CommandResult(
        return_code=0, stdout="OK", stderr="", command="default"
    )

    effects = MockEffects(
        files=files,
        commands={
            "python": default_cmd,
            "pytest": CommandResult(
                return_code=0, stdout="1 passed", stderr="", command="pytest"
            ),
            "echo": default_cmd,
        },
        inference_responses=inference_responses
        or [
            # Provide a mix of responses for different inference contexts
            '{"action": "create", "target": "main.py"}',
            "Mock generated code content",
            "a",  # LLM menu selection
            "b",  # LLM menu selection
            "Mock analysis response",
            '{"revision_needed": false}',
            '[]',  # empty check list
            '{"verdict": "pass", "blocking_issues": [], "summary": "All good"}',
            '{"missing_dependencies": []}',
            "Mock plan response",
        ]
        * 5,  # Repeat to avoid exhaustion
    )
    # Pre-load mission state
    effects._state["mission"] = mission
    return effects


# ── Standard inputs for each flow ────────────────────────────────────
# Keys match current compiled.json flow names and required inputs.

FLOW_INPUTS = {
    # Orchestrator
    "mission_control": {
        "mission_id": "test-mission-001",
    },

    # Planning
    "design_and_plan": {
        "mission_id": "test-mission-001",
    },
    "revise_plan": {
        "mission_id": "test-mission-001",
        "observation": "Need to add database support",
    },

    # File operations (Context Contract Architecture — require flow_directive)
    "file_ops": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "target_file_path": "main.py",
        "flow_directive": "Create the main TODO application entry point with a Todo class that supports add, remove, and list operations.",
        "working_directory": "/tmp/test-project",
    },
    "create": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "target_file_path": "main.py",
        "flow_directive": "Create main.py with a Todo class supporting CRUD operations.",
        "working_directory": "/tmp/test-project",
    },
    "rewrite": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "target_file_path": "main.py",
        "flow_directive": "Rewrite main.py to add save/load methods to the Todo class.",
        "working_directory": "/tmp/test-project",
    },
    "patch": {
        "file_path": "main.py",
        "file_content": "class Todo:\n    pass\n",
        "symbol_table": [
            {
                "name": "Todo",
                "type": "class",
                "start_line": 1,
                "end_line": 2,
                "body": "class Todo:\n    pass\n",
            }
        ],
        "symbol_menu_options": [
            {"id": "Todo", "description": "class Todo"},
            {"id": "__done__", "description": "DONE — finish selection"},
            {"id": "__full_rewrite__", "description": "Full rewrite instead of patch"},
        ],
        "flow_directive": "Add a save() method to the Todo class.",
        "working_directory": "/tmp/test-project",
    },

    # Diagnostics
    "diagnose_issue": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "flow_directive": "Investigate why importing main.py fails with ModuleNotFoundError.",
        "target_file_path": "main.py",
        "error_output": "ModuleNotFoundError: No module named 'todo'",
        "working_directory": "/tmp/test-project",
    },

    # Project infrastructure
    "project_ops": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "flow_directive": "Set up project structure with pyproject.toml and install dependencies.",
        "working_directory": "/tmp/test-project",
    },

    # Interactive testing
    "interact": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "flow_directive": "Run the TODO app and verify that adding and listing items works correctly.",
        "working_directory": "/tmp/test-project",
    },

    # Quality and validation
    "quality_gate": {
        "mission_id": "test-mission-001",
        "mission_objective": "Build a simple TODO app with Python",
        "working_directory": "/tmp/test-project",
        "mode": "checkpoint",
    },
    "set_env": {
        "mission_id": "test-mission-001",
        "target_file_path": "main.py",
        "working_directory": "/tmp/test-project",
    },

    # Context and research
    "prepare_context": {
        "working_directory": "/tmp/test-project",
        "task_description": "Create main.py",
        "target_file_path": "main.py",
    },
    "research": {
        "research_query": "Python TODO app best practices",
        "research_context": "Building a simple TODO app",
    },

    # Retrospective and learnings
    "retrospective": {
        "mission_id": "test-mission-001",
        "task_id": "task-001",
        "trigger_reason": "Task completed after overcoming difficulty",
    },
    "capture_learnings": {
        "task_description": "Created main.py with TODO class",
        "target_file_path": "main.py",
    },

    # Terminal sub-flows
    "run_commands": {
        "commands": ["echo 'hello'"],
        "working_directory": "/tmp/test-project",
    },
    "run_session": {
        "execution_persona": "You are a QA tester. Run the app and check basic functionality.",
        "working_directory": "/tmp/test-project",
    },
}

# Flows that need special max_steps limits (complex sub-flow invocations
# or mock data shape limitations)
FLOW_MAX_STEPS = {
    "patch": 5,          # select_symbol_turn with limited mock session
}

# Flows where hitting max_steps is expected with mock effects
# (exploratory loops that need real LLM menu responses to terminate)
EXPECTED_MAX_STEPS = {"run_session"}


async def smoke_test_flow(flow_name, flow_def, registry, all_flows, max_steps=15):
    """Try to execute a flow for up to max_steps steps.

    Returns (success: bool, error: str | None, steps_executed: list)
    """
    inputs = FLOW_INPUTS.get(flow_name, {"mission_id": "test-mission-001"})
    effects = make_effects()

    actual_max = FLOW_MAX_STEPS.get(flow_name, max_steps)

    try:
        result = await execute_flow(
            flow_def=flow_def,
            inputs=inputs,
            action_registry=registry,
            max_steps=actual_max,
            effects=effects,
            flow_registry=all_flows,
        )
        return True, None, result.steps_executed
    except Exception as e:
        # MaxStepsExceeded is expected for exploratory loop flows
        # when running with mock effects (no real LLM to pick "done")
        if flow_name in EXPECTED_MAX_STEPS and "MaxStepsExceeded" in type(e).__name__:
            return True, None, [f"(loop hit max_steps — expected with mocks)"]
        return False, f"{type(e).__name__}: {e}", []


async def main():
    # Load compiled.json
    compiled_path = Path("flows/compiled.json")
    if not compiled_path.exists():
        print("ERROR: flows/compiled.json not found. Run 'ouroboros.py cue-compile' first.")
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
