#!/usr/bin/env python3
"""Ouroboros CLI — mission management and agent execution.

Usage:
    uv run ouroboros.py mission create --mission_config game_challenge  # missions/game_challenge.yaml
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
import logging
import os
import sys

from agent.persistence.manager import PersistenceManager
from agent.persistence.models import (
    Event,
    MissionConfig,
    MissionState,
    NoteRecord,
)


def _load_mission_or_exit(args: argparse.Namespace):
    """Open the mission at --working-dir (or cwd); exit 1 when none exists.

    The shared front door for every mission-management subcommand —
    previously seven near-identical open blocks.
    """
    working_dir = os.path.realpath(args.working_dir or os.getcwd())
    pm = PersistenceManager(working_dir)
    mission = pm.load_mission()
    if mission is None:
        print("No active mission found.")
        print(f"  (looked in {pm.agent_dir}/)")
        sys.exit(1)
    return pm, mission


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
        or "http://localhost:8008/graphql"
    )

    # Precedence: OURO_FLOW_SET env (hard override) > CLI flag > YAML > "auto".
    # "auto" (the full-local default) routes in-graph via the `classify` flow
    # (LLM picks flow_set + profile); any concrete value SKIPS routing — the
    # override path for human/API-managed runs.
    flow_set = (
        os.environ.get("OURO_FLOW_SET")
        or getattr(args, "flow_set", None)
        or (yaml_config.flow_set if yaml_config else None)
        or "auto"
    )
    from agent.flow_sets import FLOW_SETS

    if flow_set not in FLOW_SETS:
        print(f"Error: unknown flow set '{flow_set}' — known: {sorted(FLOW_SETS)}")
        sys.exit(1)

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

    structural_mode = (
        getattr(args, "structural_mode", None)
        or (yaml_config.structural_mode if yaml_config else None)
        or "parallel"
    )

    config = MissionConfig(
        working_directory=working_dir,
        effects_profile=effects_profile,
        llmvp_endpoint=llmvp_endpoint,
        flow_set=flow_set,
        structural_mode=structural_mode,
        run_until=(yaml_config.run_until if yaml_config else "cycle_budget"),
        max_cycles=(yaml_config.max_cycles if yaml_config else None),
        max_wall_clock_s=(yaml_config.max_wall_clock if yaml_config else None),
    )

    mission = MissionState(objective=objective, principles=principles, config=config)

    # Add initial task descriptions as notes (goals are derived by the agent)
    if tasks_list:
        for task_desc in tasks_list:
            mission.notes.append(
                NoteRecord(
                    content=task_desc,
                    category="requirement_discovered",
                    source_flow="cli_create",
                )
            )

    pm.save_mission(mission)

    source = "YAML config" if yaml_config else "CLI flags"
    print(f"✅ Mission created: {mission.id} (from {source})")
    print(f"   Objective: {mission.objective}")
    print(f"   Working dir: {working_dir}")
    print(f"   Effects: {config.effects_profile}")
    print(f"   Flow set: {config.flow_set}")
    if principles:
        print(f"   Principles: {', '.join(principles)}")
    if tasks_list:
        print(f"   Notes from tasks: {len(tasks_list)}")
        for desc in tasks_list:
            print(f"     - {desc}")
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
    pm, mission = _load_mission_or_exit(args)

    print(f"Mission: {mission.id}")
    print(f"  Status: {mission.status}")
    print(f"  Objective: {mission.objective}")
    if mission.principles:
        print(f"  Principles: {', '.join(mission.principles)}")
    print(f"  Created: {mission.created_at}")
    print(f"  Updated: {mission.updated_at}")
    print(f"  Effects: {mission.config.effects_profile}")
    print(f"  LLMVP: {mission.config.llmvp_endpoint}")

    if mission.goals:
        complete = sum(1 for g in mission.goals if g.status == "complete")
        print(f"\n  Goals ({complete}/{len(mission.goals)} complete):")
        for g in mission.goals:
            type_tag = f" [{g.type}]" if g.type == "functional" else ""
            files_str = (
                f" → {', '.join(g.associated_files)}" if g.associated_files else ""
            )
            print(f"    [{g.status:10s}] {g.description}{type_tag}{files_str}")
            if g.reports:
                last = g.reports[-1]
                print(
                    f"                  Last report: {last.flow} ({last.status}) — {last.summary[:80]}"
                )

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
    pm, mission = _load_mission_or_exit(args)

    if mission.status != "active":
        print(f"Mission is '{mission.status}', not active. Cannot pause.")
        sys.exit(1)

    event = Event(type="pause", payload={"reason": "User requested pause via CLI"})
    pm.push_event(event)
    print("⏸  Pause event pushed. Mission will pause at next cycle.")


def cmd_mission_resume(args: argparse.Namespace) -> None:
    """Resume a paused mission."""
    pm, mission = _load_mission_or_exit(args)

    if mission.status == "paused":
        mission.status = "active"
        pm.save_mission(mission)
        event = Event(type="resume", payload={"reason": "User resumed via CLI"})
        pm.push_event(event)
        print("▶  Mission resumed.")
    elif mission.status == "active":
        print("Mission is already active.")
    else:
        print(f"Mission is '{mission.status}'. Cannot resume.")
        sys.exit(1)


def cmd_mission_abort(args: argparse.Namespace) -> None:
    """Abort the mission."""
    pm, mission = _load_mission_or_exit(args)

    event = Event(type="abort", payload={"reason": "User aborted via CLI"})
    pm.push_event(event)
    print("🛑 Abort event pushed. Mission will abort at next cycle.")


def cmd_mission_reopen(args: argparse.Namespace) -> None:
    """Reopen a finished mission so `start` can run it again.

    A completed (or aborted) mission can't be restarted — its status is
    terminal. Reopen flips it back to 'active'. Phase is a pure function of
    goal state (see action_check_pipeline_phase), so what runs next follows
    automatically: with every goal already complete the agent goes straight to
    the quality gate (a clean re-run); add new scope first (Phase 2) and it
    works those goals before re-gating. Use `mission resume` for a *paused*
    mission — this is only for terminal states.
    """
    pm, mission = _load_mission_or_exit(args)

    if mission.status == "active":
        print("Mission is already active — just run `start`.")
        return
    if mission.status == "paused":
        print(
            "Mission is paused. Use `mission resume` (reopen is for finished missions)."
        )
        sys.exit(1)
    if mission.status not in ("completed", "aborted"):
        print(f"Mission is '{mission.status}'. Nothing to reopen.")
        sys.exit(1)

    prior = mission.status
    mission.status = "active"
    mission.reopen_count += 1

    # ── New scope (Phase 2) ───────────────────────────────────────────
    # Two ways to add direction on reopen (the rest re-gate as before):
    #   --add-goal "..."  append a concrete goal directly (simple extension)
    #   --directive "..." queue a high-level direction for the planning pass
    #                     to decompose into goals (brownfield design step)
    from agent.persistence.models import GoalRecord

    added = 0
    existing = {g.description.strip().lower() for g in mission.goals}
    for desc in getattr(args, "add_goal", None) or []:
        desc = desc.strip()
        if not desc:
            continue
        if desc.lower() in existing:
            print(f"   (skipped duplicate goal: {desc[:60]})")
            continue
        mission.goals.append(
            GoalRecord(
                description=desc,
                type="functional",
                status="incomplete",
                origin="directive",
            )
        )
        existing.add(desc.lower())
        added += 1

    directive = (getattr(args, "directive", None) or "").strip()
    if directive:
        mission.pending_directive = directive

    pm.save_mission(mission)
    pm.push_event(
        Event(
            type="reopen",
            payload={
                "from_status": prior,
                "reopen_count": mission.reopen_count,
                "goals_added": added,
                "directive": bool(directive),
            },
        )
    )

    incomplete = [g for g in mission.goals if g.status == "incomplete"]
    print(f"♻  Mission reopened (was '{prior}', reopen #{mission.reopen_count}).")
    if added:
        print(f"   +{added} goal(s) added directly — `start` will work them.")
    if directive:
        print(f'   Directive queued: "{directive[:70]}"')
        print(
            "   `start` will decompose it into goals via the planning pass "
            "before working them."
        )
    if not added and not directive:
        if incomplete:
            print(
                f"   {len(incomplete)} incomplete goal(s) — `start` will work "
                f"them, then re-gate."
            )
        else:
            print("   All goals complete — `start` will re-run the quality gate.")
    print("   Run: ouroboros.py start --working-dir " + os.path.dirname(pm.agent_dir))


def cmd_mission_message(args: argparse.Namespace) -> None:
    """Send a message to the agent."""
    pm, mission = _load_mission_or_exit(args)

    message = args.message
    event = Event(type="user_message", payload={"message": message})
    pm.push_event(event)

    # Also add as a note to mission state
    note = NoteRecord(content=message, source_flow="user_message")
    mission.notes.append(note)
    pm.save_mission(mission)

    print(f"💬 Message sent: {message}")


def cmd_mission_history(args: argparse.Namespace) -> None:
    """Show mission history (flow artifacts)."""
    pm, mission = _load_mission_or_exit(args)

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

    print("🚀 Starting Ouroboros agent")
    print(f"   Mission: {mission.id}")
    print(f"   Objective: {mission.objective}")
    print(f"   Working dir: {working_dir}")
    print(f"   LLMVP: {mission.config.llmvp_endpoint}")
    # Code-liveness stamp: a long-running agent process keeps the code it
    # started with. Recording the SHA here makes "were the fixes actually
    # in that run?" answerable from the log instead of from memory (the
    # 2026-07-16 bossgame A/B ran 32h on pre-fix code, undetected).
    try:
        import subprocess as _sp

        repo = os.path.dirname(os.path.abspath(__file__))
        sha = _sp.run(
            ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = bool(
            _sp.run(
                ["git", "-C", repo, "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        )
        if sha:
            print(f"   Agent code: {sha}{' (dirty)' if dirty else ''}")
    except Exception:
        pass
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

    # Entry flow derives from the mission's flow set (registry, not
    # persisted — getattr keeps pre-flow-set mission.json files working).
    from agent.flow_sets import get_flow_set

    flow_set = get_flow_set(getattr(mission.config, "flow_set", "code_core"))
    print(f"   Flow set: {flow_set.name} (entry: {flow_set.entry_flow})")

    # Termination policy: CLI flags beat mission config beats defaults.
    from agent.mission_config import resolve_run_policy

    try:
        max_cycles, max_wall_clock_s = resolve_run_policy(
            mission.config,
            cli_max_cycles=args.max_cycles,
            cli_max_wall_clock=getattr(args, "max_wall_clock", None),
        )
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    policy = "until completed" if max_cycles is None else f"≤{max_cycles} cycles"
    if max_wall_clock_s:
        policy += f", ≤{max_wall_clock_s / 3600:.1f}h wall clock"
    print(f"   Run: {policy}")

    # Run the agent loop on the shared isolated harness (dedicated thread +
    # event loop + drain; see agent/mission_runner.py).
    from agent.mission_runner import run_mission_isolated

    try:
        outcome = run_mission_isolated(
            effects,
            mission_id=mission.id,
            entry_flow=flow_set.entry_flow,
            max_cycles=max_cycles,
            max_wall_clock_s=max_wall_clock_s,
            flows_dir=flows_dir,
            prompts_dir=prompts_dir,
        )
        if outcome.parked:
            # Budget stop, not a crash — the mission is parked as paused and
            # `mission resume` / `start` continues it.
            print(f"\n⏸  {outcome.park_message}")
            return
        if outcome.error is not None:
            raise outcome.error
        result = outcome.result
        print()
        print(f"{'=' * 60}")
        print(f"Agent terminated: {result.status}")
        print(f"Steps: {' → '.join(result.steps_executed)}")
        if result.observations:
            print("Observations:")
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
    """Validate CUE schemas and compile all flow sets to compiled.json.

    Thin shell over agent.flow_compile.compile_flows (see its module
    docstring for the pipeline).
    """
    from agent.flow_compile import FlowCompileError, compile_flows

    flows_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flows")
    try:
        compiled_path, flow_count = compile_flows(flows_dir)
    except FlowCompileError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"Done. {compiled_path} generated.")
    print(f"Flow count: {flow_count}")


def cmd_lint_flows(args: argparse.Namespace) -> None:
    """Run the comprehensive flow context linter (agent/flow_lint.py)."""
    from agent.flow_lint import lint

    project_root = os.path.dirname(os.path.abspath(__file__))
    compiled_path = (
        args.compiled
        if getattr(args, "compiled", None)
        else os.path.join(project_root, "flows", "compiled.json")
    )
    if not os.path.exists(compiled_path):
        print(
            f"Error: {compiled_path} not found. Run 'ouroboros.py cue-compile' first."
        )
        sys.exit(1)

    results = lint(compiled_path=compiled_path, verbose=args.verbose)
    shown = [r for r in results if args.verbose or r.level in ("ERROR", "WARNING")]
    for r in shown:
        print(str(r))
    errors = sum(1 for r in results if r.level == "ERROR")
    warnings = sum(1 for r in results if r.level == "WARNING")
    print(f"\n{errors} errors, {warnings} warnings")
    if errors:
        sys.exit(1)


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
        print(
            f"Error: {compiled_path} not found. Run 'ouroboros.py cue-compile' first."
        )
        sys.exit(1)

    env = os.environ.copy()
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([sys.executable, smoke_script], cwd=project_root, env=env)
    sys.exit(result.returncode)


def cmd_cli_smoke(args: argparse.Namespace) -> None:
    """Exercise --help on every subcommand; catches import-chain rot."""
    import subprocess

    project_root = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(project_root, "dev", "cli_smoke.py")

    if not os.path.exists(script):
        print(f"Error: CLI smoke script not found: {script}")
        sys.exit(1)

    env = os.environ.copy()
    env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([sys.executable, script], cwd=project_root, env=env)
    sys.exit(result.returncode)


def _llmvp_graphql(endpoint: str, query: str, variables: dict | None = None) -> dict:
    """POST one GraphQL request to LLMVP; exits with the error on failure.

    No client timeout — a model swap legitimately holds the request open
    for minutes while weights load."""
    import httpx

    try:
        resp = httpx.post(
            endpoint,
            json={"query": query, "variables": variables or {}},
            timeout=None,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        print(f"Error: cannot reach LLMVP at {endpoint}: {exc}")
        sys.exit(1)
    if data.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in data["errors"])
        print(f"Error from LLMVP: {msgs}")
        sys.exit(1)
    return data["data"]


def cmd_llmvp_models(args: argparse.Namespace) -> None:
    """List the server's swappable model configs."""
    data = _llmvp_graphql(
        args.endpoint,
        "{ models { name family ggufSizeGb weightsPresent active provider error } }",
    )
    for m in data["models"]:
        marker = "→" if m["active"] else " "
        note = (
            "MISSING WEIGHTS"
            if not m["weightsPresent"] and not m["error"]
            else (m["error"] or "")
        )
        size = (
            f"{m['ggufSizeGb']:7.1f} GB"
            if m["provider"] == "local_llama"
            else f"{'remote':>10s}"
        )
        print(f"{marker} {m['name']:32s} {m['family']:9s} {size}  {note}")


def cmd_llmvp_swap(args: argparse.Namespace) -> None:
    """Hotswap the served model to a named config (waits for completion)."""
    print(f"Swapping LLMVP to {args.name!r} (drain {args.drain_s:.0f}s) …")
    data = _llmvp_graphql(
        args.endpoint,
        """
        mutation($name: String!, $drainS: Float!) {
          swapModel(name: $name, drainS: $drainS) {
            ok name previous noop rolledBack drainForced
            expiredSessions evictedStreams teardownMs loadMs totalMs error
          }
        }
        """,
        {"name": args.name, "drainS": args.drain_s},
    )
    r = data["swapModel"]
    if r["noop"]:
        print(f"Already serving {r['name']} — nothing to do.")
        return
    if r["ok"]:
        print(
            f"✅ {r['previous']} → {r['name']} in {r['totalMs'] / 1000:.1f}s "
            f"(teardown {r['teardownMs'] / 1000:.1f}s, load {r['loadMs'] / 1000:.1f}s)"
        )
        if r["drainForced"]:
            print(
                f"   drain forced: {r['expiredSessions']} sessions expired, "
                f"{r['evictedStreams']} streams evicted"
            )
    else:
        state = "rolled back to previous model" if r["rolledBack"] else "MODELLESS"
        print(f"❌ swap failed ({state}): {r['error']}")
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
        "--max-cycles",
        type=int,
        default=None,
        help="Max work-flow cycles (default: mission config, else 50; "
        "missions with run_until: completed run unbounded unless capped)",
    )
    start_p.add_argument(
        "--max-wall-clock",
        default=None,
        help="Park the mission as paused after this much wall time "
        '("3h", "90m", "1h30m", or seconds; default: mission config)',
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

    # ── cue-compile subcommand ────────────────────────────────────
    subparsers.add_parser(
        "cue-compile", help="Validate CUE schemas and compile flows to compiled.json"
    )

    # ── lint-flows subcommand ─────────────────────────────────────
    lf_p = subparsers.add_parser(
        "lint-flows", help="Run comprehensive flow context linter"
    )
    lf_p.add_argument("--verbose", action="store_true", help="Show all checks")
    lf_p.add_argument(
        "--compiled", help="Path to compiled.json (default: flows/compiled.json)"
    )

    # ── smoke subcommand ──────────────────────────────────────────
    subparsers.add_parser("smoke", help="Run smoke test suite against compiled flows")

    # ── cli-smoke subcommand ──────────────────────────────────────
    subparsers.add_parser(
        "cli-smoke",
        help="Exercise --help on every subcommand (catches import rot)",
    )

    # ── mission subcommand ────────────────────────────────────────
    mission_parser = subparsers.add_parser("mission", help="Mission management")
    mission_sub = mission_parser.add_subparsers(dest="mission_command")

    # mission create
    create_p = mission_sub.add_parser("create", help="Create a new mission")
    create_p.add_argument(
        "--mission_config",
        help="Load mission from YAML config (name or path; bare names resolve in cwd then missions/)",
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
    create_p.add_argument(
        "--flow-set",
        help="Flow set to run the mission with (default: 'auto' — the LLM routes "
        "ops vs code_core in-graph; name a set to skip routing)",
    )
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

    # mission reopen
    reopen_p = mission_sub.add_parser(
        "reopen",
        help="Reopen a finished (completed/aborted) mission so `start` can run again",
    )
    reopen_p.add_argument("--working-dir", help="Working directory (default: cwd)")
    reopen_p.add_argument(
        "--add-goal",
        action="append",
        metavar="DESCRIPTION",
        help="Append a concrete goal directly (repeatable). For simple "
        "extensions that need no planning. Duplicates are skipped.",
    )
    reopen_p.add_argument(
        "--directive",
        help="Queue a high-level direction for the planning pass to decompose "
        "into structural/functional goals (brownfield design step).",
    )

    # mission message
    msg_p = mission_sub.add_parser("message", help="Send a message to the agent")
    msg_p.add_argument("message", help="Message text")
    msg_p.add_argument("--working-dir", help="Working directory (default: cwd)")

    # mission history
    hist_p = mission_sub.add_parser("history", help="Show flow execution history")
    hist_p.add_argument("--working-dir", help="Working directory (default: cwd)")

    # ── llmvp subcommand ──────────────────────────────────────────
    llmvp_parser = subparsers.add_parser(
        "llmvp", help="LLMVP server operations (model catalog, hotswap)"
    )
    llmvp_sub = llmvp_parser.add_subparsers(dest="llmvp_command")
    _default_endpoint = "http://localhost:8008/graphql"

    models_p = llmvp_sub.add_parser("models", help="List swappable model configs")
    models_p.add_argument("--endpoint", default=_default_endpoint)

    swap_p = llmvp_sub.add_parser(
        "swap", help="Hotswap the served model to a named config"
    )
    swap_p.add_argument("name", help="Config name (llmvp/configs/{name}.yaml)")
    swap_p.add_argument("--endpoint", default=_default_endpoint)
    swap_p.add_argument(
        "--drain-s",
        type=float,
        default=60.0,
        help="Finish window for in-flight work before force-clear (default 60)",
    )

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
    elif args.command == "cli-smoke":
        cmd_cli_smoke(args)
    elif args.command == "mission":
        dispatch = {
            "create": cmd_mission_create,
            "status": cmd_mission_status,
            "pause": cmd_mission_pause,
            "resume": cmd_mission_resume,
            "abort": cmd_mission_abort,
            "reopen": cmd_mission_reopen,
            "message": cmd_mission_message,
            "history": cmd_mission_history,
        }
        handler = dispatch.get(args.mission_command)
        if handler:
            handler(args)
        else:
            mission_parser.print_help()
    elif args.command == "llmvp":
        dispatch = {
            "models": cmd_llmvp_models,
            "swap": cmd_llmvp_swap,
        }
        handler = dispatch.get(args.llmvp_command)
        if handler:
            handler(args)
        else:
            llmvp_parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
