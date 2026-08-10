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
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.models import FlowDefinition
from agent.actions.registry import build_action_registry
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.persistence.models import (
    MissionState,
    MissionConfig,
    GoalRecord,
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
        mission.goals = [
            GoalRecord(
                id="goal-001",
                description="Create main.py with basic TODO class",
                type="structural",
                status="incomplete",
                associated_files=["main.py"],
            ),
            GoalRecord(
                id="goal-002",
                description="Create test_main.py",
                type="structural",
                status="incomplete",
                associated_files=["test_main.py"],
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
            "[]",  # empty check list
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
    # Contract-swarm orchestrator (controller copy; same shape)
    "mission_control_swarm": {
        "mission_id": "test-mission-001",
    },
    # batch_contracted orchestrator (controller copy; same shape)
    "mission_control_contracted": {
        "mission_id": "test-mission-001",
    },
    # integrated orchestrator (controller copy; same shape)
    "mission_control_integrated": {
        "mission_id": "test-mission-001",
    },
    # Session structural mode (one file per turn, checked between turns)
    "build_structure_session": {
        "mission_id": "test-mission-001",
        "goal_id": "",
        "working_directory": "/tmp/smoke",
        "flow_directive": "build everything",
    },
    # Contract-swarm structural flow
    "build_contracts": {
        "mission_id": "test-mission-001",
        "goal_id": "",
        "working_directory": "/tmp/smoke",
        "flow_directive": "build everything",
    },
    # batch_contracted structural flow (contracts front + single-completion back)
    "build_structure_contracted": {
        "mission_id": "test-mission-001",
        "goal_id": "",
        "working_directory": "/tmp/smoke",
        "flow_directive": "build everything",
    },
    # integrated structural flow (swarm fan-out front + single integrator back)
    "build_structure_integrated": {
        "mission_id": "test-mission-001",
        "goal_id": "",
        "working_directory": "/tmp/smoke",
        "flow_directive": "build everything",
    },
    # Planning
    "design_and_plan": {
        "mission_id": "test-mission-001",
    },
    # Brownfield adoption — read a foreign workspace into mission.architecture
    "ingest_workspace": {
        "mission_id": "test-mission-001",
    },
    "revise_plan": {
        "mission_id": "test-mission-001",
        "observation": "Need to add database support",
    },
    # File operations (Context Contract Architecture — require flow_directive)
    "file_ops": {
        "mission_id": "test-mission-001",
        "goal_id": "goal-001",
        "target_file_path": "main.py",
        "flow_directive": "Create the main TODO application entry point with a Todo class that supports add, remove, and list operations.",
        "working_directory": "/tmp/test-project",
    },
    "create": {
        "mission_id": "test-mission-001",
        "goal_id": "goal-001",
        "target_file_path": "main.py",
        "flow_directive": "Create main.py with a Todo class supporting CRUD operations.",
        "working_directory": "/tmp/test-project",
    },
    "rewrite": {
        "mission_id": "test-mission-001",
        "goal_id": "goal-001",
        "target_file_path": "main.py",
        "flow_directive": "Rewrite main.py to add save/load methods to the Todo class.",
        "working_directory": "/tmp/test-project",
    },
    "patch_module": {
        "target_file_path": "main.py",
        "file_content": "import os\n\n\nclass Todo:\n    def save(self):\n        pass\n",
        "flow_directive": "Add the missing module-level line `import json` to this file.",
        "module_directive": "Add the missing module-level line `import json` to this file.",
    },
    "data_patch": {
        "target_file_path": "config.yaml",
        "file_content": "name: app\nversion: 1\n",
        "flow_directive": "Set version to 2 in config.yaml.",
    },
    "deep_search": {
        "brief": "What is the standard library way to parse TOML in Python 3.11+?",
    },
    "deep_research": {
        "brief": "Survey the current approaches to structured output in LLMs.",
    },
    "create_content_batch": {
        "mission_id": "test-mission-001",
        "flow_directive": "Generate every missing data file concurrently.",
    },
    "diagnose_batch": {
        "mission_id": "test-mission-001",
        "flow_directive": "Triage every gate-failed goal concurrently.",
    },
    "patch": {
        "file_path": "main.py",
        "file_content": "class Todo:\n    def save(self):\n        pass\n",
        "symbol_table": [
            {
                "name": "Todo.save",
                "kind": "method",
                "signature": "def save(self)",
                "line": 2,
                "end_line": 3,
                "body": "    def save(self):\n        pass\n",
                "parent": "Todo",
            }
        ],
        "target_symbol": "Todo.save",
        "change_spec": "Write the Todo instance state to a JSON file at self.path.",
        "flow_directive": "Implement the save() method.",
        "working_directory": "/tmp/test-project",
    },
    # Phase D (patch redesign) — new sub-flow for inserting symbols
    # that diagnose names but the file's AST doesn't contain yet.
    "add_symbol": {
        "file_path": "main.py",
        "file_content": "class Todo:\n    pass\n",
        "symbol_table": [
            {
                "name": "Todo",
                "kind": "class",
                "signature": "class Todo",
                "line": 1,
                "end_line": 2,
                "body": "class Todo:\n    pass\n",
                "parent": "",
            }
        ],
        "flow_directive": "Add a save() method to the Todo class.",
        "target_symbol": "Todo.save",
        "change_spec": "Write the Todo instance to a JSON file at self.path.",
        "working_directory": "/tmp/test-project",
    },
    # Parallel structural mode — one-shot batch creation
    "build_structure": {
        "mission_id": "test-mission-001",
        "goal_id": "",
        "flow_directive": "Create all architecture files in one batch generation.",
        "working_directory": "/tmp/test-project",
    },
    # Diagnostics
    "diagnose_issue": {
        "mission_id": "test-mission-001",
        "goal_id": "goal-001",
        "flow_directive": "Investigate why importing main.py fails with ModuleNotFoundError.",
        "target_file_path": "main.py",
        "error_output": "ModuleNotFoundError: No module named 'todo'",
        "working_directory": "/tmp/test-project",
        # Projection stubs — smoke bypasses loop.py's _materialize_projections,
        # so any projection the flow consumes must be pre-seeded here.
        "pick_file_menu": [
            {"id": "main.py", "description": "main.py — entry point [defines: main]"},
            {"id": "todo.py", "description": "todo.py — Todo model [defines: Todo]"},
        ],
    },
    # Project infrastructure
    "project_ops": {
        "mission_id": "test-mission-001",
        "goal_id": "goal-001",
        "flow_directive": "Set up project structure with pyproject.toml and install dependencies.",
        "working_directory": "/tmp/test-project",
    },
    # Interactive testing
    "interact": {
        "mission_id": "test-mission-001",
        "goal_id": "goal-001",
        "flow_directive": "Run the TODO app and verify that adding and listing items works correctly.",
        "working_directory": "/tmp/test-project",
    },
    # Extractor flow set (scraper v2)
    "extract_control": {
        "mission_id": "test-mission-001",
    },
    "extract_pdfs": {
        "mission_id": "test-mission-001",
        "goal_id": "goal-001",
        "flow_directive": "Extract markdown and figures from 1 PDF.",
        "paper_keys": ["doi_10.1000_x.1"],
        "working_directory": "/tmp/test-project",
    },
    "extract_gate": {
        "mission_id": "test-mission-001",
        "working_directory": "/tmp/test-project",
    },
    # Curator flow set (corpus stage 3)
    "curate_control": {
        "mission_id": "test-mission-001",
    },
    "fig_review": {
        "mission_id": "test-mission-001",
        "goal_id": "goal-001",
        "flow_directive": "VLM figure readings for 1 paper.",
        "paper_keys": ["doi_10.1000_x.1"],
        "working_directory": "/tmp/test-project",
    },
    "curate_paper": {
        "mission_id": "test-mission-001",
        "goal_id": "goal-001",
        "flow_directive": "Review and pack paper doi_10.1000_x.1",
        "paper_key": "doi_10.1000_x.1",
        "working_directory": "/tmp/test-project",
    },
    "curate_gate": {
        "mission_id": "test-mission-001",
        "working_directory": "/tmp/test-project",
    },
    # Ops flow set
    "ops_control": {
        "mission_id": "test-mission-001",
    },
    "ops_task": {
        "mission_id": "test-mission-001",
        "working_directory": "/tmp/test-project",
    },
    # Scraper flow set
    "research_control": {
        "mission_id": "test-mission-001",
    },
    "plan_research": {
        "mission_id": "test-mission-001",
    },
    "discover": {
        "mission_id": "test-mission-001",
        "goal_id": "goal-001",
        "flow_directive": "Find candidate papers for the aspect 'grain boundaries'.",
        "aspect_name": "grain boundaries",
        "seed_queries": ["grain boundary segregation"],
        "working_directory": "/tmp/test-project",
    },
    "acquire_catalog": {
        "mission_id": "test-mission-001",
        "goal_id": "goal-001",
        "flow_directive": "Acquire and catalog 1 paper from the candidate worklist.",
        "paper_keys": ["doi_10.1000_x"],
        "working_directory": "/tmp/test-project",
    },
    "research_gate": {
        "mission_id": "test-mission-001",
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
        "goal_id": "goal-001",
        "trigger_reason": "Task completed after overcoming difficulty",
    },
    "capture_learnings": {
        "task_description": "Created main.py with TODO class",
        "target_file_path": "main.py",
    },
    # Escalation layer (shared recovery primitive)
    "escalate": {
        "mission_id": "test-mission-001",
        "failure_evidence": "[FAIL] syntax: main.py\n  stderr: SyntaxError",
        "expected_outcome": "The validation checks pass for main.py.",
        "target_file_path": "main.py",
        "working_directory": "/tmp/test-project",
        "invoking_flow": "file_ops",
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
FLOW_MAX_STEPS = {}

# Flows where hitting max_steps is expected with mock effects
# (exploratory loops that need real LLM menu responses to terminate).
# ops_task is an honestly-long straight DAG (~25 steps, no internal loop)
# that also EMBEDS run_session as a subflow — under mocks it exhausts any
# budget inside that loop, exactly like run_session itself.
EXPECTED_MAX_STEPS = {"run_session", "ops_task"}


async def smoke_test_flow(flow_name, flow_def, registry, all_flows, max_steps=15):
    """Try to execute a flow for up to max_steps steps.

    Returns (success: bool, error: str | None, steps_executed: list)
    """
    inputs = FLOW_INPUTS.get(flow_name, {"mission_id": "test-mission-001"})
    # mission_control: use a FRESH mission (no goals, no architecture) so the
    # phase machine routes to plan → dispatch_planning (a terminal tail-call
    # the smoke can observe). The default fixture (goals but no architecture)
    # is a brownfield state that now correctly routes to the structural sweep
    # — which can't progress under mock effects and reads as a loop.
    if flow_name in (
        "mission_control",
        "mission_control_swarm",
        "mission_control_contracted",
        "mission_control_integrated",
    ):
        effects = make_effects(mission=make_mock_mission(with_plan=False))
    else:
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
            return True, None, ["(loop hit max_steps — expected with mocks)"]
        return False, f"{type(e).__name__}: {e}", []


async def main():
    # Load compiled.json
    compiled_path = Path("flows/compiled.json")
    if not compiled_path.exists():
        print(
            "ERROR: flows/compiled.json not found. Run 'ouroboros.py cue-compile' first."
        )
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
        print("\nFailures:")
        for name, err in errors:
            print(f"  {name}: {err}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
