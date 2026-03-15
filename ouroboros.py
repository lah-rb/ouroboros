#!/usr/bin/env python3
"""Ouroboros CLI — mission management and agent execution.

Usage:
    uv run ouroboros.py mission create --objective "..." --working-dir /path [options]
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
    """Create a new mission."""
    working_dir = os.path.realpath(args.working_dir or os.getcwd())
    if not os.path.isdir(working_dir):
        print(f"Error: Working directory does not exist: {working_dir}")
        sys.exit(1)

    pm = PersistenceManager(working_dir)

    if pm.mission_exists():
        print(f"Error: Mission already exists at {pm.agent_dir}/mission.json")
        print("  Use 'mission abort' first, or delete .agent/ to start fresh.")
        sys.exit(1)

    pm.init_agent_dir()

    config = MissionConfig(
        working_directory=working_dir,
        effects_profile=args.effects_profile or "local",
        llmvp_endpoint=args.llmvp_endpoint or "http://localhost:8000/graphql",
    )

    principles = args.principles or []
    mission = MissionState(
        objective=args.objective, principles=principles, config=config
    )

    # Add initial tasks if provided
    if args.tasks:
        for i, task_desc in enumerate(args.tasks):
            task = TaskRecord(
                description=task_desc,
                flow="modify_file",  # default flow, can be changed later
                priority=i,
            )
            mission.plan.append(task)

    pm.save_mission(mission)

    print(f"✅ Mission created: {mission.id}")
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
    )

    # Resolve flows directory
    flows_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flows")

    # Run the agent loop
    from agent.loop import run_agent

    try:
        result = asyncio.run(
            run_agent(
                mission_id=mission.id,
                effects=effects,
                flows_dir=flows_dir,
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

    # ── mission subcommand ────────────────────────────────────────
    mission_parser = subparsers.add_parser("mission", help="Mission management")
    mission_sub = mission_parser.add_subparsers(dest="mission_command")

    # mission create
    create_p = mission_sub.add_parser("create", help="Create a new mission")
    create_p.add_argument("--objective", required=True, help="Mission objective")
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
