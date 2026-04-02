#!/usr/bin/env python3
"""Ouroboros CLI — mission management and agent execution.

Usage:
    uv run ouroboros.py mission create --mission_config test        # from test.yaml
    uv run ouroboros.py mission create --objective "..." [options]   # from CLI flags
    uv run ouroboros.py mission status [--working-dir /path]
    uv run ouroboros.py mission pause [--working-dir /path]
    uv run ouroboros.py mission resume [--working-dir /path]
    uv run ouroboros.py mission abort [--working-dir /path]
    uv run ouroboros.py mission message "text" [--working-dir /path]
    uv run ouroboros.py mission history [--working-dir /path]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from agent.persistence.manager import PersistenceManager
from agent.persistence.models import (
    Event,
    MissionConfig,
    MissionState,
    NoteRecord,
    TaskRecord,
)


def cmd_mission_create(args: argparse.Namespace) -> None:
    """Create a new mission.

    Supports two modes:
    1. CLI flags: --objective, --principles, --tasks, etc.
    2. YAML config: --mission_config <name_or_path>

    When --mission_config is used, YAML values are loaded first, then
    any explicit CLI flags override them.
    """
    # ── Load from YAML config if provided ─────────────────────
    yaml_config = None
    if args.mission_config:
        from agent.mission_config import (
            load_mission_config,
            run_lifecycle_commands,
        )

        try:
            yaml_config = load_mission_config(args.mission_config)
        except FileNotFoundError as e:
            print(f"Error: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"Error loading mission config: {e}")
            sys.exit(1)

        # Run pre_create commands before mission creation
        if yaml_config.pre_create:
            dry_run = (
                args.effects_profile == "dry_run"
                or yaml_config.effects_profile == "dry_run"
            )
            try:
                run_lifecycle_commands(
                    yaml_config.pre_create,
                    phase="pre_create",
                    dry_run=dry_run,
                )
            except RuntimeError as e:
                print(f"Error: {e}")
                sys.exit(1)

    # ── Resolve parameters (CLI flags override YAML) ──────────
    objective = args.objective or (yaml_config.objective if yaml_config else None)
    if not objective:
        print("Error: --objective is required (via CLI flag or YAML config)")
        sys.exit(1)

    working_dir_raw = (
        args.working_dir
        or (yaml_config.working_dir if yaml_config else None)
        or os.getcwd()
    )
    working_dir = os.path.realpath(working_dir_raw)

    if not os.path.isdir(working_dir):
        print(f"Error: Working directory does not exist: {working_dir}")
        sys.exit(1)

    effects_profile = (
        args.effects_profile
        or (yaml_config.effects_profile if yaml_config else None)
        or "local"
    )

    llmvp_endpoint = (
        args.llmvp_endpoint
        or (yaml_config.llmvp_endpoint if yaml_config else None)
        or "http://localhost:8000/graphql"
    )

    principles = (
        args.principles or (yaml_config.principles if yaml_config else None) or []
    )

    tasks_list = args.tasks or (yaml_config.tasks if yaml_config else None) or []

    # ── Create the mission ────────────────────────────────────
    pm = PersistenceManager(working_dir)

    if pm.mission_exists():
        print(f"Error: Mission already exists at {pm.agent_dir}/mission.json")
        print("  Use 'mission abort' first, or delete .agent/ to start fresh.")
        sys.exit(1)

    pm.init_agent_dir()

    config = MissionConfig(
        working_directory=working_dir,
        effects_profile=effects_profile,
        llmvp_endpoint=llmvp_endpoint,
    )

    mission = MissionState(objective=objective, principles=principles, config=config)

    # Add initial tasks if provided
    if tasks_list:
        from agent.actions.mission_actions import (
            _infer_flow_from_description,
            _derive_source_for_tests,
        )

        for i, task_desc in enumerate(tasks_list):
            inferred_flow = _infer_flow_from_description(task_desc)

            # Extract target_file_path from description
            import re as _re

            file_match = _re.search(
                r"(?:in|for|to|create|modify|update|fix)\s+[`'\"]*([a-zA-Z0-9_/.-]+\.(?:py|js|ts|yaml|yml|md|toml|json|rs|cfg|txt))[`'\"]*",
                task_desc,
                _re.IGNORECASE,
            )
            target_file = file_match.group(1) if file_match else ""

            task_inputs = {
                "reason": task_desc,
                "target_file_path": target_file,
            }

            # For create_tests: target = source file, test_file_path = output
            if inferred_flow == "create_tests" and target_file:
                source_file = _derive_source_for_tests(target_file, task_desc)
                task_inputs["target_file_path"] = source_file
                task_inputs["test_file_path"] = target_file

            task = TaskRecord(
                description=task_desc,
                flow=inferred_flow,
                priority=i,
                inputs=task_inputs,
            )
            mission.plan.append(task)

    pm.save_mission(mission)

    source = "YAML config" if yaml_config else "CLI flags"
    print(f"✅ Mission created: {mission.id} (from {source})")
    print(f"   Objective: {mission.objective}")
    print(f"   Working dir: {working_dir}")
    print(f"   Effects: {config.effects_profile}")
    if principles:
        print(f"   Principles: {', '.join(principles)}")
    if mission.plan:
        print(f"   Tasks: {len(mission.plan)}")
        for t in mission.plan:
            print(f"     - [{t.status}] {t.description}")
    print(f"   State: {pm.agent_dir}/mission.json")

    # ── Run post_create commands after mission creation ───────
    if yaml_config and yaml_config.post_create:
        from agent.mission_config import run_lifecycle_commands

        print()
        dry_run = effects_profile == "dry_run"
        try:
            run_lifecycle_commands(
                yaml_config.post_create,
                phase="post_create",
                dry_run=dry_run,
                stream_output=True,
            )
        except RuntimeError as e:
            print(f"Error: {e}")
            sys.exit(1)


def cmd_mission_status(args: argparse.Namespace) -> None:
    """Show mission status."""
    working_dir = os.path.realpath(args.working_dir or os.getcwd())
    pm = PersistenceManager(working_dir)

    mission = pm.load_mission()
    if mission is None:
        print("No active mission found.")
        print(f"  (looked in {pm.agent_dir}/)")
        sys.exit(1)

    print(f"Mission: {mission.id}")
    print(f"  Status: {mission.status}")
    print(f"  Objective: {mission.objective}")
    if mission.principles:
        print(f"  Principles: {', '.join(mission.principles)}")
    print(f"  Created: {mission.created_at}")
    print(f"  Updated: {mission.updated_at}")
    print(f"  Effects: {mission.config.effects_profile}")
    print(f"  LLMVP: {mission.config.llmvp_endpoint}")

    if mission.plan:
        print(f"\n  Tasks ({len(mission.plan)}):")
        for t in mission.plan:
            frustration_str = (
                f" [frustration: {t.frustration}]" if t.frustration > 0 else ""
            )
            print(f"    [{t.status:12s}] {t.description}{frustration_str}")
            if t.summary:
                print(f"                  Summary: {t.summary}")

    events = pm.read_events()
    if events:
        print(f"\n  Pending events: {len(events)}")
        for e in events:
            print(f"    [{e.type}] {e.payload.get('message', e.payload)}")

    artifacts = pm.list_artifacts()
    if artifacts:
        print(f"\n  Artifacts: {len(artifacts)} in history")


def cmd_mission_pause(args: argparse.Namespace) -> None:
    """Pause the mission."""
    working_dir = os.path.realpath(args.working_dir or os.getcwd())
    pm = PersistenceManager(working_dir)
    mission = pm.load_mission()
    if mission is None:
        print("No active mission found.")
        sys.exit(1)

    if mission.status != "active":
        print(f"Mission is '{mission.status}', not active. Cannot pause.")
        sys.exit(1)

    event = Event(type="pause", payload={"reason": "User requested pause via CLI"})
    pm.push_event(event)
    print(f"⏸  Pause event pushed. Mission will pause at next cycle.")


def cmd_mission_resume(args: argparse.Namespace) -> None:
    """Resume a paused mission."""
    working_dir = os.path.realpath(args.working_dir or os.getcwd())
    pm = PersistenceManager(working_dir)
    mission = pm.load_mission()
    if mission is None:
        print("No active mission found.")
        sys.exit(1)

    if mission.status == "paused":
        mission.status = "active"
        pm.save_mission(mission)
        event = Event(type="resume", payload={"reason": "User resumed via CLI"})
        pm.push_event(event)
        print(f"▶  Mission resumed.")
    elif mission.status == "active":
        print("Mission is already active.")
    else:
        print(f"Mission is '{mission.status}'. Cannot resume.")
        sys.exit(1)


def cmd_mission_abort(args: argparse.Namespace) -> None:
    """Abort the mission."""
    working_dir = os.path.realpath(args.working_dir or os.getcwd())
    pm = PersistenceManager(working_dir)
    mission = pm.load_mission()
    if mission is None:
        print("No active mission found.")
        sys.exit(1)

    event = Event(type="abort", payload={"reason": "User aborted via CLI"})
    pm.push_event(event)
    print(f"🛑 Abort event pushed. Mission will abort at next cycle.")


def cmd_mission_message(args: argparse.Namespace) -> None:
    """Send a message to the agent."""
    working_dir = os.path.realpath(args.working_dir or os.getcwd())
    pm = PersistenceManager(working_dir)
    mission = pm.load_mission()
    if mission is None:
        print("No active mission found.")
        sys.exit(1)

    message = args.message
    event = Event(type="user_message", payload={"message": message})
    pm.push_event(event)

    # Also add as a note to mission state
    note = NoteRecord(content=message, source="user")
    mission.notes.append(note)
    pm.save_mission(mission)

    print(f"💬 Message sent: {message}")


def cmd_mission_history(args: argparse.Namespace) -> None:
    """Show mission history (flow artifacts)."""
    working_dir = os.path.realpath(args.working_dir or os.getcwd())
    pm = PersistenceManager(working_dir)
    mission = pm.load_mission()
    if mission is None:
        print("No active mission found.")
        sys.exit(1)

    artifacts = pm.list_artifacts()
    if not artifacts:
        print("No artifacts in history yet.")
        return

    print(f"Flow history ({len(artifacts)} artifacts):")
    for filename in artifacts:
        # Try to load and show summary
        task_id = filename.rsplit("_", 1)[-1].replace(".json", "")
        artifact = pm.load_artifact(task_id)
        if artifact:
            print(
                f"  [{artifact.status:10s}] {artifact.flow_name} (task: {artifact.task_id})"
            )
            print(f"               Steps: {' → '.join(artifact.steps_executed)}")
            print(f"               Time: {artifact.timestamp}")
        else:
            print(f"  {filename}")


def cmd_visualize(args: argparse.Namespace) -> None:
    """Visualize flow definitions as Mermaid or DOT diagrams."""
    from agent.loader import load_all_flows
    from agent.visualize import (
        flow_to_mermaid,
        flow_to_dot,
        all_flows_to_mermaid,
        all_flows_to_dot,
        render_to_svg,
    )

    flows_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flows")
    registry = load_all_flows(flows_dir)

    fmt = args.format or "mermaid"
    flow_name = args.flow_name

    if flow_name and flow_name != "--all":
        # Single flow
        if flow_name not in registry:
            print(f"Error: Flow '{flow_name}' not found.")
            print(f"Available flows: {', '.join(sorted(registry.keys()))}")
            sys.exit(1)
        flow_def = registry[flow_name]
        if fmt == "dot":
            output = flow_to_dot(flow_def)
        else:
            output = flow_to_mermaid(flow_def)
    else:
        # All flows (system view)
        if fmt == "dot":
            output = all_flows_to_dot(registry)
        else:
            output = all_flows_to_mermaid(registry, show_internal_steps=args.detailed)

    # ── SVG export ────────────────────────────────────────────
    svg_path = args.svg
    # Auto-detect: if --output ends in .svg, treat it as SVG export
    if not svg_path and args.output and args.output.endswith(".svg"):
        svg_path = args.output

    if svg_path:
        try:
            render_to_svg(output, source_format=fmt, output_path=svg_path)
            print(f"✅ Rendered SVG to {svg_path}")
        except RuntimeError as e:
            print(f"Error: {e}")
            sys.exit(1)
        return

    # ── Text output (Mermaid / DOT source) ────────────────────
    if args.output:
        with open(args.output, "w") as f:
            if fmt == "mermaid" and args.output.endswith(".md"):
                f.write(f"# Ouroboros Flow Diagrams\n\n")
                if flow_name and flow_name != "--all":
                    f.write(f"## {flow_name}\n\n")
                else:
                    f.write(f"## System View\n\n")
                f.write(f"```mermaid\n{output}\n```\n")
            else:
                f.write(output)
        print(f"✅ Wrote {fmt} diagram to {args.output}")
    else:
        print(output)


def cmd_lint(args: argparse.Namespace) -> None:
    """Run flow contract validation."""
    from agent.blueprint.analyzer import analyze
    from agent.blueprint.lint import lint_flows

    try:
        ir = analyze()
    except Exception as e:
        print(f"Error analyzing flows: {e}")
        sys.exit(1)

    results = lint_flows(ir, verbose=args.verbose)

    # Filter by flow if specified
    if args.flow:
        results = [r for r in results if r.flow == args.flow]

    for r in results:
        print(str(r))

    errors = [r for r in results if r.level == "ERROR"]
    warnings = [r for r in results if r.level == "WARNING"]
    infos = [r for r in results if r.level == "INFO"]

    print(f"\n{len(errors)} errors, {len(warnings)} warnings", end="")
    if args.verbose:
        print(f", {len(infos)} info")
    else:
        print()

    if errors:
        sys.exit(1)


def cmd_start(args: argparse.Namespace) -> None:
    """Start the agent on an active mission."""
    working_dir = os.path.realpath(args.working_dir or os.getcwd())
    pm = PersistenceManager(working_dir)

    mission = pm.load_mission()
    if mission is None:
        print("No active mission found.")
        print(f"  (looked in {pm.agent_dir}/)")
        print("  Use 'ouroboros.py mission create' first.")
        sys.exit(1)

    if mission.status not in ("active", "paused"):
        print(f"Mission is '{mission.status}'. Cannot start.")
        sys.exit(1)

    if mission.status == "paused":
        mission.status = "active"
        pm.save_mission(mission)

    # Set up logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)-5s | %(message)s",
    )

    print(f"🚀 Starting Ouroboros agent")
    print(f"   Mission: {mission.id}")
    print(f"   Objective: {mission.objective}")
    print(f"   Working dir: {working_dir}")
    print(f"   LLMVP: {mission.config.llmvp_endpoint}")
    print()

    # Build effects
    from agent.effects.local import LocalEffects

    effects = LocalEffects(
        working_directory=working_dir,
        llmvp_endpoint=mission.config.llmvp_endpoint,
        trace_thinking=getattr(args, "trace_thinking", False),
        trace_prompts=getattr(args, "trace_prompts", False),
    )

    # Resolve flows directory
    flows_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flows")
    prompts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

    # Run the agent loop
    from agent.loop import run_agent

    try:
        result = asyncio.run(
            run_agent(
                mission_id=mission.id,
                effects=effects,
                flows_dir=flows_dir,
                prompts_dir=prompts_dir,
                max_cycles=args.max_cycles,
            )
        )
        print()
        print(f"{'=' * 60}")
        print(f"Agent terminated: {result.status}")
        print(f"Steps: {' → '.join(result.steps_executed)}")
        if result.observations:
            print(f"Observations:")
            for obs in result.observations[-5:]:
                print(f"  {obs}")
        print(f"{'=' * 60}")
    except KeyboardInterrupt:
        print("\n⏹  Agent interrupted by user.")
    except Exception as e:
        print(f"\n❌ Agent error: {e}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)


def cmd_cue_compile(args: argparse.Namespace) -> None:
    """Validate CUE schemas and compile flows to compiled.json.

    Runs ``cue vet`` then ``cue export`` from within Python, reading
    the JSON output directly and writing flows/compiled.json.
    """
    import subprocess

    project_root = os.path.dirname(os.path.abspath(__file__))
    cue_dir = os.path.join(project_root, "flows", "cue")
    compiled_path = os.path.join(project_root, "flows", "compiled.json")

    if not os.path.isdir(cue_dir):
        print(f"Error: CUE directory not found: {cue_dir}")
        sys.exit(1)

    # Locate cue binary
    cue_bin = None
    for candidate in ("cue", os.path.join(project_root, "cue")):
        try:
            subprocess.run(
                [candidate, "version"],
                capture_output=True, check=True,
            )
            cue_bin = candidate
            break
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue

    if cue_bin is None:
        print("Error: 'cue' not found. Install from https://cuelang.org/docs/install/")
        sys.exit(1)

    # Step 1: Validate
    print("Validating CUE schemas...")
    result = subprocess.run(
        [cue_bin, "vet", "."],
        capture_output=True, text=True, cwd=cue_dir,
    )
    if result.returncode != 0:
        print(f"CUE validation failed:\n{result.stderr}")
        sys.exit(1)

    # Step 2: Export to JSON
    print("Exporting flows to JSON...")
    result = subprocess.run(
        [cue_bin, "export", ".", "--out", "json"],
        capture_output=True, text=True, cwd=cue_dir,
    )
    if result.returncode != 0:
        print(f"CUE export failed:\n{result.stderr}")
        sys.exit(1)

    # Step 3: Parse, validate, and write
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"CUE export produced invalid JSON: {e}")
        sys.exit(1)

    flow_count = sum(1 for v in data.values() if isinstance(v, dict) and "flow" in v)

    with open(compiled_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Done. {compiled_path} generated.")
    print(f"Flow count: {flow_count}")

    # Step 4: Quick structural sanity checks
    errors = []
    for name, flow_def in data.items():
        if not isinstance(flow_def, dict) or "flow" not in flow_def:
            continue
        entry = flow_def.get("entry", "")
        steps = flow_def.get("steps", {})
        if entry and entry not in steps:
            errors.append(f"  {name}: entry step '{entry}' not in steps")
        for step_name, step_def in steps.items():
            if not isinstance(step_def, dict):
                continue
            resolver = step_def.get("resolver", {})
            if resolver.get("type") == "rule":
                for rule in resolver.get("rules", []):
                    target = rule.get("transition", "")
                    if target and target not in steps:
                        errors.append(
                            f"  {name}.{step_name}: transition '{target}' not in steps"
                        )

    if errors:
        print(f"\n⚠️  Structural issues found:")
        for e in errors:
            print(e)
        sys.exit(1)


def cmd_lint_flows(args: argparse.Namespace) -> None:
    """Run the comprehensive flow context linter (dev/lint_flows.py)."""
    import subprocess

    project_root = os.path.dirname(os.path.abspath(__file__))
    lint_script = os.path.join(project_root, "dev", "lint_flows.py")

    if not os.path.exists(lint_script):
        print(f"Error: lint script not found: {lint_script}")
        sys.exit(1)

    compiled_path = (
        args.compiled
        if hasattr(args, "compiled") and args.compiled
        else os.path.join(project_root, "flows", "compiled.json")
    )
    if not os.path.exists(compiled_path):
        print(f"Error: {compiled_path} not found. Run 'ouroboros.py cue-compile' first.")
        sys.exit(1)

    cmd = [sys.executable, lint_script]
    if args.verbose:
        cmd.append("--verbose")
    if hasattr(args, "compiled") and args.compiled:
        cmd.extend(["--compiled", args.compiled])

    env = os.environ.copy()
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(cmd, cwd=project_root, env=env)
    sys.exit(result.returncode)


def cmd_smoke(args: argparse.Namespace) -> None:
    """Run the smoke test suite against compiled.json."""
    import subprocess

    project_root = os.path.dirname(os.path.abspath(__file__))
    smoke_script = os.path.join(project_root, "dev", "smoke_test.py")

    if not os.path.exists(smoke_script):
        print(f"Error: smoke test not found: {smoke_script}")
        sys.exit(1)

    compiled_path = os.path.join(project_root, "flows", "compiled.json")
    if not os.path.exists(compiled_path):
        print(f"Error: {compiled_path} not found. Run 'ouroboros.py cue-compile' first.")
        sys.exit(1)

    env = os.environ.copy()
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([sys.executable, smoke_script], cwd=project_root, env=env)
    sys.exit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ouroboros",
        description="Ouroboros — flow-driven autonomous coding agent",
    )
    subparsers = parser.add_subparsers(dest="command")

    # ── start subcommand ──────────────────────────────────────────
    start_p = subparsers.add_parser("start", help="Start the agent on a mission")
    start_p.add_argument("--working-dir", help="Working directory (default: cwd)")
    start_p.add_argument(
        "--max-cycles", type=int, default=50, help="Max flow cycles (default: 50)"
    )
    start_p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    start_p.add_argument(
        "--trace-thinking",
        action="store_true",
        help="Capture chain-of-thought from LLMVP thinking endpoint in trace events",
    )
    start_p.add_argument(
        "--trace-prompts",
        action="store_true",
        help="Capture full rendered prompts and raw model responses in trace events",
    )

    # ── blueprint subcommand ──────────────────────────────────────
    bp_p = subparsers.add_parser("blueprint", help="Generate architectural blueprint")
    bp_p.add_argument(
        "--format",
        choices=["pdf", "md"],
        default=None,
        help="Output format (default: both PDF and Markdown)",
    )
    bp_p.add_argument(
        "--output",
        help="Output directory (default: current working directory)",
    )

    # ── trace subcommand ──────────────────────────────────────────
    trace_p = subparsers.add_parser("trace", help="View runtime trace summaries")
    trace_p.add_argument("--mission", help="Filter by mission ID")
    trace_p.add_argument(
        "--format",
        choices=["summary", "detail"],
        default="summary",
        help="Output format (default: summary)",
    )
    trace_p.add_argument(
        "--output",
        help="Output file path (default: trace_{mission_id}.md in cwd)",
    )
    trace_p.add_argument("--working-dir", help="Working directory (default: cwd)")

    # ── lint subcommand ───────────────────────────────────────────
    lint_p = subparsers.add_parser("lint", help="Run flow contract validation")
    lint_p.add_argument("--flow", help="Lint a specific flow (default: all)")
    lint_p.add_argument(
        "--verbose", action="store_true", help="Show all checks, not just warnings"
    )
    lint_p.set_defaults(func=cmd_lint)

    # ── cue-compile subcommand ────────────────────────────────────
    cue_p = subparsers.add_parser(
        "cue-compile", help="Validate CUE schemas and compile flows to compiled.json"
    )

    # ── lint-flows subcommand ─────────────────────────────────────
    lf_p = subparsers.add_parser(
        "lint-flows", help="Run comprehensive flow context linter"
    )
    lf_p.add_argument(
        "--verbose", action="store_true", help="Show all checks"
    )
    lf_p.add_argument(
        "--compiled", help="Path to compiled.json (default: flows/compiled.json)"
    )

    # ── smoke subcommand ──────────────────────────────────────────
    smoke_p = subparsers.add_parser(
        "smoke", help="Run smoke test suite against compiled flows"
    )

    # ── visualize subcommand ──────────────────────────────────────
    viz_p = subparsers.add_parser("visualize", help="Visualize flow definitions")
    viz_p.add_argument("flow_name", nargs="?", help="Flow name (omit for all flows)")
    viz_p.add_argument(
        "--format", choices=["mermaid", "dot"], default="mermaid", help="Output format"
    )
    viz_p.add_argument("--output", help="Write to file instead of stdout")
    viz_p.add_argument(
        "--svg",
        help="Export as SVG image (requires mmdc for Mermaid, dot for Graphviz)",
    )
    viz_p.add_argument(
        "--detailed", action="store_true", help="Show internal steps in system view"
    )

    # ── mission subcommand ────────────────────────────────────────
    mission_parser = subparsers.add_parser("mission", help="Mission management")
    mission_sub = mission_parser.add_subparsers(dest="mission_command")

    # mission create
    create_p = mission_sub.add_parser("create", help="Create a new mission")
    create_p.add_argument(
        "--mission_config",
        help="Load mission from YAML config (name or path, e.g. 'test' loads test.yaml)",
    )
    create_p.add_argument(
        "--objective",
        help="Mission objective (required unless provided by --mission_config)",
    )
    create_p.add_argument("--working-dir", help="Working directory (default: cwd)")
    create_p.add_argument("--principles", nargs="*", help="Guiding principles")
    create_p.add_argument(
        "--effects-profile", choices=["local", "git_managed", "dry_run"]
    )
    create_p.add_argument("--llmvp-endpoint", help="LLMVP GraphQL endpoint URL")
    create_p.add_argument("--tasks", nargs="*", help="Initial task descriptions")

    # mission status
    status_p = mission_sub.add_parser("status", help="Show mission status")
    status_p.add_argument("--working-dir", help="Working directory (default: cwd)")

    # mission pause
    pause_p = mission_sub.add_parser("pause", help="Pause the mission")
    pause_p.add_argument("--working-dir", help="Working directory (default: cwd)")

    # mission resume
    resume_p = mission_sub.add_parser("resume", help="Resume a paused mission")
    resume_p.add_argument("--working-dir", help="Working directory (default: cwd)")

    # mission abort
    abort_p = mission_sub.add_parser("abort", help="Abort the mission")
    abort_p.add_argument("--working-dir", help="Working directory (default: cwd)")

    # mission message
    msg_p = mission_sub.add_parser("message", help="Send a message to the agent")
    msg_p.add_argument("message", help="Message text")
    msg_p.add_argument("--working-dir", help="Working directory (default: cwd)")

    # mission history
    hist_p = mission_sub.add_parser("history", help="Show flow execution history")
    hist_p.add_argument("--working-dir", help="Working directory (default: cwd)")

    args = parser.parse_args()

    if args.command == "start":
        cmd_start(args)
    elif args.command == "blueprint":
        from agent.blueprint.cli import cmd_blueprint

        cmd_blueprint(args)
    elif args.command == "trace":
        from agent.trace_cli import cmd_trace

        cmd_trace(args)
    elif args.command == "lint":
        cmd_lint(args)
    elif args.command == "cue-compile":
        cmd_cue_compile(args)
    elif args.command == "lint-flows":
        cmd_lint_flows(args)
    elif args.command == "smoke":
        cmd_smoke(args)
    elif args.command == "visualize":
        cmd_visualize(args)
    elif args.command == "mission":
        dispatch = {
            "create": cmd_mission_create,
            "status": cmd_mission_status,
            "pause": cmd_mission_pause,
            "resume": cmd_mission_resume,
            "abort": cmd_mission_abort,
            "message": cmd_mission_message,
            "history": cmd_mission_history,
        }
        handler = dispatch.get(args.mission_command)
        if handler:
            handler(args)
        else:
            mission_parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
