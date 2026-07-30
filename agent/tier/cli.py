"""`ouroboros.py tier` — start, watch and steer a tier batch.

    tier run --models glm-4.7-flash,gemma-4-26b-a4b,laguna-xs-2.1
    tier status
    tier skip | stop | force-stop

`run` DETACHES by default. A tier batch outlives the session that started it —
the 2026-07-29 batch ran eleven hours — and a background job started from an
interactive session dies with that session. `subprocess` with
`start_new_session=True` is the portable form of what `setsid` would do;
the `setsid` BINARY DOES NOT EXIST ON MACOS and a shell script calling it fails
silently, leaving the job in the session's process group to die exactly as
described. `--foreground` opts out for testing.

The control verbs write a single word into the run's CONTROL file, which the
runner consumes on its next poll (within POLL_S). They are not signals: a
sentinel survives a runner that is mid-arm and busy, and it can be issued from
any shell without knowing a pid.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from agent.tier.runner import RUNS, TierRun

CONTROL_HELP = {
    "pause": "park the current arm cleanly and free the machine (resumable)",
    "skip": "abandon the current arm, stage whatever exists, continue to the next",
    "stop": "let the current arm finish, then end the chain",
    "force-stop": "abandon the current arm AND end the chain",
}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, TypeError):
        return False


def _run_dirs() -> list[Path]:
    """Run DIRECTORIES, newest first. The is_dir filter is load-bearing:
    `tier_*` also matches sidecar logs like `tier_driver_<stamp>.log`, and
    without it `status` reported a log file as the most recent run."""
    return sorted((p for p in RUNS.glob("tier_*") if p.is_dir()), reverse=True)


def _active_run() -> Optional[Path]:
    """Newest run whose worker is still alive. Finished runs are skipped so a
    control verb can never land on yesterday's directory."""
    for base in _run_dirs():
        state = base / "STATE.json"
        if not state.exists():
            continue
        try:
            data = json.loads(state.read_text())
        except json.JSONDecodeError:
            continue
        if data.get("finished"):
            continue
        if _alive(data.get("pid", -1)):
            return base
    return None


def _paused_runs() -> list[tuple[Path, dict]]:
    """Runs parked by `tier pause`, newest first.

    A paused run is neither active (its worker exited) nor finished, which is
    exactly how it stays findable without taking control verbs meant for a live
    batch."""
    out = []
    for base in _run_dirs():
        state = base / "STATE.json"
        if not state.exists():
            continue
        try:
            data = json.loads(state.read_text())
        except json.JSONDecodeError:
            continue
        if data.get("paused") and not data.get("finished"):
            out.append((base, data))
    return out


def cmd_tier(args) -> None:
    if args.tier_command == "run":
        _run(args)
    elif args.tier_command == "status":
        _status()
    elif args.tier_command == "resume":
        _resume(args)
    elif args.tier_command == "list":
        _list()
    elif args.tier_command in CONTROL_HELP:
        _control(args.tier_command)
    else:
        print(
            "usage: ouroboros.py tier "
            "{run,status,list,pause,resume,skip,stop,force-stop}"
        )


def _list() -> None:
    dirs = _run_dirs()
    if not dirs:
        print("no tier runs found")
        return
    active = _active_run()
    print(f"{'run':<28}{'state':<12}{'detail'}")
    for base in dirs[:15]:
        state = base / "STATE.json"
        if not state.exists():
            print(f"{base.name:<28}{'legacy':<12}(pre-STATE.json; shell runner)")
            continue
        try:
            data = json.loads(state.read_text())
        except json.JSONDecodeError:
            print(f"{base.name:<28}{'corrupt':<12}STATE.json unreadable")
            continue
        if base == active:
            what = f"arm {data.get('index')} — {data.get('current_arm')}"
            print(f"{base.name:<28}{'RUNNING':<12}{what}")
        elif data.get("paused"):
            arm = (data.get("paused_arm") or {}).get("config", "?")
            queue = data.get("remaining_arms") or []
            print(
                f"{base.name:<28}{'PAUSED':<12}{arm} + {len(queue)-1 if queue else 0} queued"
                f"  -> ouroboros.py tier resume"
            )
        else:
            done = sum(1 for r in data.get("results", []) if r.get("staged"))
            print(f"{base.name:<28}{'finished':<12}{done} staged")


def _resume(args) -> None:
    paused = _paused_runs()
    if not paused:
        print("no paused tier batch to resume")
        raise SystemExit(1)
    if (existing := _active_run()) is not None:
        print(f"a tier batch is already running: {existing}")
        raise SystemExit(1)

    base, data = paused[0]
    arm = data["paused_arm"]
    queue = data.get("remaining_arms") or [arm["config"]]
    consumed_min = int(arm["consumed_s"] / 60)
    print(f"resuming {base.name}")
    print(f"  arm    : {arm['config']} ({consumed_min}min already spent)")
    print(f"  queue  : {', '.join(queue)}")

    # Continue INTO THE SAME run directory: one batch, one record, so the
    # manifest and logs stay contiguous across the interruption.
    with open(base / "driver.log", "a") as fh:
        proc = subprocess.Popen(
            [
                sys.executable,
                "ouroboros.py",
                "tier",
                "run",
                "--_worker",
                "--models",
                ",".join(queue),
                "--base",
                str(base),
                "--mission",
                data.get("mission", "game_challenge_tier"),
                "--wall",
                data.get("wall", "2h"),
                "--top-phase",
                getattr(args, "top_phase", None) or "quality",
                "--budget-h",
                str(getattr(args, "budget_h", None) or 11.0),
                "--resume-consumed-s",
                str(arm["consumed_s"]),
            ],
            cwd=Path(__file__).resolve().parents[2],
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"\nresumed (pid {proc.pid}, detached) — ouroboros.py tier status")


def _run(args) -> None:
    arms = [m.strip() for m in args.models.split(",") if m.strip()]
    if not arms:
        print("no models given — use --models a,b,c")
        raise SystemExit(2)

    if getattr(args, "worker", False):
        consumed = float(getattr(args, "resume_consumed_s", 0) or 0)
        resume = None
        if consumed > 0:
            # The head of the queue is the arm that was paused; its workspace is
            # keyed by config name, the same as when it started.
            resume = {
                "config": arms[0],
                "work": str(Path("/tmp/tier") / arms[0]),
                "consumed_s": consumed,
            }
        run = TierRun(
            arms=arms,
            mission=args.mission,
            wall=args.wall,
            top_phase=args.top_phase,
            budget_h=args.budget_h,
            leave_server_up=args.leave_server_up,
            base=Path(args.base),
            resume_from=resume,
        )
        raise SystemExit(run.execute())

    if (existing := _active_run()) is not None:
        print(f"a tier batch is already running: {existing}")
        print("  end it first:  ouroboros.py tier stop")
        raise SystemExit(1)

    base = RUNS / f"tier_{time.strftime('%Y%m%d-%H%M%S')}"
    base.mkdir(parents=True, exist_ok=True)
    if args.foreground:
        run = TierRun(
            arms=arms,
            mission=args.mission,
            wall=args.wall,
            top_phase=args.top_phase,
            budget_h=args.budget_h,
            leave_server_up=args.leave_server_up,
            base=base,
        )
        raise SystemExit(run.execute())

    driver = base / "driver.log"
    with open(driver, "w") as fh:
        proc = subprocess.Popen(
            [
                sys.executable,
                "ouroboros.py",
                "tier",
                "run",
                "--_worker",
                "--models",
                ",".join(arms),
                "--base",
                str(base),
                "--mission",
                args.mission,
                "--wall",
                args.wall,
                "--top-phase",
                args.top_phase,
                "--budget-h",
                str(args.budget_h),
            ]
            + (["--leave-server-up"] if args.leave_server_up else []),
            cwd=Path(__file__).resolve().parents[2],
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    print(f"tier batch started (pid {proc.pid}, detached)")
    print(f"  arms   : {len(arms)} — {', '.join(arms)}")
    print(f"  each   : {args.wall}   budget: {args.budget_h}h")
    print(f"  base   : {base}")
    print(
        f"  server : {'left UP after the chain' if args.leave_server_up else 'DOWN after the chain'}"
    )
    print("\n  ouroboros.py tier status        # progress")
    for verb, why in CONTROL_HELP.items():
        print(f"  ouroboros.py tier {verb:<11} # {why}")


def _status() -> None:
    base = _active_run()
    if base is None:
        finished = _run_dirs()
        if not finished:
            print("no tier runs found")
            return
        print(f"no tier batch running. most recent: {finished[0]}")
        man = finished[0] / "MANIFEST.txt"
        if man.exists():
            print("\n" + man.read_text().rstrip())
        return
    print(f"tier batch running: {base}\n")
    hb = base / "HEARTBEAT.txt"
    print(hb.read_text().rstrip() if hb.exists() else "(no heartbeat yet)")
    man = base / "MANIFEST.txt"
    if man.exists() and man.read_text().strip():
        print("\n--- staged so far ---\n" + man.read_text().rstrip())


def _control(word: str) -> None:
    base = _active_run()
    if base is None:
        print("no tier batch running")
        raise SystemExit(1)
    (base / "CONTROL").write_text(word)
    print(f"{word} requested — {CONTROL_HELP[word]}")
    print(f"  the runner picks this up within ~30s ({base})")
    if word in ("skip", "force-stop"):
        print(
            "  note: the server may need to finish an abandoned generation "
            "before it releases; the runner escalates to SIGKILL if it stalls."
        )
