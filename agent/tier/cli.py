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

from agent.tier import runner as tier_runner
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
    elif args.tier_command == "extend":
        _extend(args)
    elif args.tier_command in CONTROL_HELP:
        _control(args.tier_command)
    else:
        print(
            "usage: ouroboros.py tier "
            "{run,status,list,extend,pause,resume,skip,stop,force-stop}"
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
            short = sum(
                1
                for c in tier_runner.extend_candidates(base)
                if c["verdict"] == "eligible"
            )
            hint = (
                f", {short} extendable  -> ouroboros.py tier extend "
                f"--run {base.name}"
                if short
                else ""
            )
            print(f"{base.name:<28}{'finished':<12}{done} staged{hint}")


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
                "--resume-prior-elapsed-s",
                str(arm.get("prior_elapsed_s", 0.0) or 0.0),
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

    if not getattr(args, "worker", False):
        # League:both models (hy3) run in BOTH leagues — expand to two
        # labeled arms here, in the NON-worker branch, so STATE.json's arm
        # list, the resume queue, workspaces and logs all carry the labels
        # natively (the worker and `tier resume` re-emit them verbatim).
        from agent.tier.runner import config_league

        expanded: list[str] = []
        for m in arms:
            if m.endswith(("[c]", "[g]")):
                expanded.append(m)
            elif config_league(m) == "both":
                expanded.extend([f"{m}[c]", f"{m}[g]"])
            else:
                expanded.append(m)
        arms = expanded

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
                "prior_elapsed_s": float(
                    getattr(args, "resume_prior_elapsed_s", 0.0) or 0.0
                ),
            }
        elif getattr(args, "extend", False):
            # Extending a short arm: same re-entry mechanics as a resume, but
            # consumed_s is 0 ON PURPOSE. For a contemplator to park short the
            # WALL is what bound it, so carrying elapsed (~4h) would leave
            # _remaining_wall_s at its 60-second floor — a 20-minute model boot
            # for one minute of work. The cycle cap is the real budget; the
            # fresh wall is only jam protection. Prior elapsed rides separately
            # so ArmResult.minutes (and therefore cyc/h) stays honest.
            label = arms[0]
            base_dir = Path(args.base)
            prior = next(
                (
                    c
                    for c in tier_runner.extend_candidates(base_dir)
                    if c["arm"] == label
                ),
                {},
            )
            resume = {
                "config": label,
                "work": str(Path("/tmp/tier") / label),
                "consumed_s": 0.0,
                "prior_elapsed_s": float(prior.get("minutes", 0) or 0) * 60.0,
                "mission_id": tier_runner.snapshot_mission_id(base_dir, label),
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


def _resolve_extend_run(args) -> Optional[Path]:
    """The run to extend: --run, else the newest finished one with a candidate."""
    if getattr(args, "run", None):
        base = Path(args.run)
        if not base.is_dir():
            base = RUNS / args.run
        if not base.is_dir():
            print(f"no such run: {args.run}")
            return None
        return base
    for base in _run_dirs():
        try:
            data = json.loads((base / "STATE.json").read_text())
        except Exception:  # noqa: BLE001
            continue
        if not data.get("finished"):
            continue
        if any(c["verdict"] == "eligible" for c in tier_runner.extend_candidates(base)):
            print(f"(newest finished run with an extendable arm: {base.name})")
            return base
    print("no finished run has an extendable arm — `tier extend --list` for why")
    return None


def _print_candidates(base: Path) -> list[dict]:
    rows = tier_runner.extend_candidates(base)
    print(f"\n{base.name}")
    if not rows:
        print("  (no arms recorded — STATE.json unreadable or pre-dates the field)")
        return rows
    for c in rows:
        mark = "ELIGIBLE" if c["verdict"] == "eligible" else c["verdict"]
        detail = tier_runner.VERDICT_HELP.get(c["verdict"], "")
        if c["verdict"] == "eligible":
            detail = (
                f"{c['cycles']}/{tier_runner.CONTEMPLATOR_CYCLES} cycles, "
                f"slot arm{c['slot']:02d}"
                if c["slot"]
                else f"{c['cycles']} cycles"
            )
        print(f"  {c['arm']:<30} {mark:<18} {detail}")
    return rows


def _extend(args) -> None:
    """Give a short contemplator arm the cycle budget it never spent.

    The counterpart to `tier resume`, which only finds a batch parked by the
    explicit `pause` verb. An arm whose mission parked on its wall inside a
    FINISHED batch is in exactly the same resumable state — `status: paused`
    with `cycles_consumed` on disk — but had no way back.
    """
    if getattr(args, "list", False):
        dirs = _run_dirs()[:15]
        if not dirs:
            print("no tier runs found")
            return
        for base in dirs:
            _print_candidates(base)
        return

    if (existing := _active_run()) is not None:
        # Global on purpose: the inference server is single-tenant, so a live
        # batch anywhere blocks, not just one in this directory.
        print(f"a tier batch is already running: {existing}")
        raise SystemExit(1)

    base = _resolve_extend_run(args)
    if base is None:
        raise SystemExit(1)

    try:
        state = json.loads((base / "STATE.json").read_text())
    except Exception:  # noqa: BLE001
        print(f"{base.name}: STATE.json unreadable")
        raise SystemExit(1)
    if not state.get("finished"):
        verb = "resume" if state.get("paused") else "wait for it"
        print(f"{base.name} is not finished — {verb}")
        raise SystemExit(1)

    rows = tier_runner.extend_candidates(base)
    by_arm = {c["arm"]: c for c in rows}
    eligible = [c for c in rows if c["verdict"] == "eligible"]

    if getattr(args, "arm", None):
        cand = by_arm.get(args.arm)
        if cand is None:
            print(f"{base.name} has no arm {args.arm!r}. Arms:")
            _print_candidates(base)
            raise SystemExit(1)
        if cand["verdict"] != "eligible":
            _refuse(base, cand)
            raise SystemExit(1)
    elif len(eligible) == 1:
        cand = eligible[0]
    elif not eligible:
        print(f"{base.name}: no extendable arm.")
        _print_candidates(base)
        raise SystemExit(1)
    else:
        # Never guess which model gets four hours of the machine.
        print(f"{base.name} has {len(eligible)} extendable arms — name one with --arm:")
        _print_candidates(base)
        raise SystemExit(2)

    slot = cand["slot"]
    if slot is not None and not getattr(args, "replace_judged", False):
        cite = f"{base.name}/staged/arm{slot:02d}"
        ladder = tier_runner.ROOT / "dev/blind_panel/LADDER.md"
        try:
            if cite in ladder.read_text():
                print(
                    f"REFUSED: staged/arm{slot:02d} is cited in LADDER.md as {cite}.\n"
                    f"  Extending REPLACES that artifact, and it has already been "
                    f"judged and ranked.\n"
                    f"  Pass --replace-judged if that is what you want."
                )
                raise SystemExit(1)
        except OSError:
            pass

    target = (
        f"REPLACES staged/arm{slot:02d}" if slot is not None else "stages a new slot"
    )
    print(f"extending {base.name}")
    print(f"  arm    : {cand['arm']}   ({cand['league']})")
    print(
        f"  cycles : {cand['cycles']}/{tier_runner.CONTEMPLATOR_CYCLES} "
        f"-> {tier_runner.CONTEMPLATOR_CYCLES - cand['cycles']} more, "
        f"{tier_runner.CONTEMPLATOR_SAFETY_WALL} safety wall"
    )
    print(f"  prior  : {cand['minutes']}min (carried into the record; wall is fresh)")
    print(f"  stages : {target}")

    cmd = [
        sys.executable,
        "ouroboros.py",
        "tier",
        "run",
        "--_worker",
        "--_extend",
        "--models",
        cand["arm"],
        "--base",
        str(base),
        "--mission",
        state.get("mission", "game_challenge_tier"),
        "--wall",
        state.get("wall", "2h"),
    ]
    root = Path(__file__).resolve().parents[2]
    if getattr(args, "foreground", False):
        raise SystemExit(subprocess.run(cmd, cwd=root).returncode)
    with open(base / "driver.log", "a") as fh:
        proc = subprocess.Popen(
            cmd, cwd=root, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True
        )
    print(f"\nextending (pid {proc.pid}, detached) — ouroboros.py tier status")


def _refuse(base: Path, cand: dict) -> None:
    """Say why, and name the remedy that actually applies."""
    v = cand["verdict"]
    if v == "grinder":
        cfg = cand["config"]
        print(
            f"REFUSED: {cand['arm']} ran GRINDER (llmvp/configs/{cfg}.yaml "
            f"tier.league).\n"
            f"  A grinder reaching its wall is the league contract working, not a\n"
            f"  shortfall — there is no cycle budget left unspent to extend into.\n\n"
            f"  If the wall was the wrong instrument for this model, re-league it:\n"
            f"      llmvp/configs/{cfg}.yaml:  tier.league: contemplator\n"
            f"  then `tier extend` becomes available on this run (it re-reads the\n"
            f"  config), or re-run clean for a single-session artifact."
        )
    elif v == "workspace_reused":
        print(
            f"REFUSED: {cand['arm']}'s workspace now holds a DIFFERENT mission.\n"
            f"    this run staged : {cand.get('snapshot_id')}\n"
            f"    /tmp/tier holds : {cand.get('live_id')}\n"
            f"  /tmp/tier/<label> is keyed by model name alone, so a later batch\n"
            f"  running the same model overwrote it. Extending would resume that\n"
            f"  stranger's mission and stage it over this run's artifact.\n"
            f"  Nothing to do — re-run the model fresh if you want more cycles."
        )
    elif v.startswith("mission_"):
        status = v.removeprefix("mission_")
        extra = {
            "completed": "it finished inside its budget; there is nothing to extend",
            "active": "it never parked, so its cycle count is stale and "
            "30-N would be arithmetic on a number that is not true",
        }.get(status, "only a paused mission can be extended")
        print(f"REFUSED: {cand['arm']} mission is {status!r} — {extra}.")
    else:
        print(f"REFUSED: {cand['arm']} — " f"{tier_runner.VERDICT_HELP.get(v, v)}.")
