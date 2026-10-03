#!/usr/bin/env python3
"""Move the 3060 box between OCR duty and repack duty, mission and all.

The box (192.168.1.76) serves ONE primary model in 12 GB: paddle for the OCR
lanes, or gemma-4-12b for the remote repack lane -- never both. Switching is a
fixed sequence that is easy to get wrong by hand (a stale pause event, an OCR
request asking a gemma box to load paddle, orphaned tool processes), so it lives
here (operator ruling 2026-10-01: schedule the swap; it falls late at night).

    tools/transition_3060.py to repack [--wait-ocr-drained] [--bench-triage]
    tools/transition_3060.py to ocr [--wait-repack-idle]
    tools/transition_3060.py to repack --quiet-local   (free this box's GPUs)

  repack: box -> gemma-4-12b-3060; mission lanes: ocr OFF, repack_r1 ON
  ocr:    box -> paddle-ocr-vl-3060; mission lanes: ocr + ocr2 ON, repack OFF

--wait-ocr-drained polls until nothing is pending OCR (the agent's own
_extraction_pending) and no OCR tool runs for the open workspace.
--wait-repack-idle polls the newest mission log until the repack lane has
logged REPACK_LANE_IDLE three rounds running (no pack owed, no missed window
owed that it may take), so the box returns to OCR when the lane runs dry.
--bench-triage runs dev/bench_triage_judge.py's gemma arm on the idle box
BETWEEN the swap and the repack lane's start (operator ruling: bench it in the
OCR -> packing transition); the mission's muse lanes keep running meanwhile.

The box is driven over its GraphQL API (swapModel), so no SSH is needed; a
swap persists the box's active_config.txt, so a reboot agrees with it. If the
swap fails, the mission is restarted on the profile it came from.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
WORKDIR = os.path.expanduser("~/corpora/ouroboros-spectra")
BOX = "http://192.168.1.76:8008/graphql"
PY = str(REPO / ".venv" / "bin" / "python")

COMMON_ENV = {
    "OUROBOROS_REMOTE_CURATE_LANES": "0",
    "OUROBOROS_FIGTEXT_FIGS": "12",
    "OUROBOROS_CURATE_PAPERS": "1",
    "OUROBOROS_SCRAPER_OVERLAP_PDFS": "4",
    "OUROBOROS_CURATE_SEAT_TOKENS": "131072",
}
#: Lanes switched off on EVERY profile (operator ruling 2026-10-03: translate
#: down to one lane during the curation finale -- 18 accepted lingual papers
#: wait, none await review, and curate lanes were refused seats while four
#: translate lanes held them). Merged into each start's DISABLE_LANES so a
#: profile switch does not bring the lanes back.
STANDING_OFF = ("translate2", "translate3", "translate4")

PROFILES = {
    "repack": {
        "model": "gemma-4-12b-3060",
        "env": {"OUROBOROS_DISABLE_LANES": "ocr", "OUROBOROS_REMOTE_REPACK_LANES": "1"},
    },
    "ocr": {
        "model": "paddle-ocr-vl-3060",
        "env": {"OUROBOROS_OCR_LANES": "2", "OUROBOROS_REMOTE_REPACK_LANES": "0"},
    },
}


def log(msg: str) -> None:
    print(
        f"[{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%SZ')}] {msg}", flush=True
    )


def gql(query: str, timeout: float = 600) -> dict:
    req = urllib.request.Request(
        BOX,
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def box_active() -> str:
    models = gql("{ models { name state } }", timeout=30)["data"]["models"]
    return next((m["name"] for m in models if m["state"] == "active"), "")


def mission(*args: str) -> str:
    out = subprocess.run(
        [PY, "ouroboros.py", "mission", *args, "--working-dir", WORKDIR],
        cwd=REPO, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    return out.stdout + out.stderr


def _pids(match) -> list[int]:
    out = subprocess.run(
        ["ps", "-eo", "pid=,comm=,args="], capture_output=True, text=True
    ).stdout
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if (
            len(parts) == 3
            and match(parts[1], parts[2])
            and int(parts[0]) != os.getpid()
        ):
            pids.append(int(parts[0]))
    return pids


def loop_pids() -> list[int]:
    return _pids(
        lambda comm, args: comm == "python"
        and "ouroboros.py start" in args
        and WORKDIR in args
    )


def open_workspace_tools() -> list[int]:
    """The mission's tool subprocesses -- never the closed shelf's."""
    return _pids(
        lambda comm, args: ("fig_review.py" in args or "extract_batch.py" in args)
        and "ouroboros-closed" not in args
    )


def stop_mission() -> None:
    log("pausing the mission")
    mission("pause")
    for _ in range(40):
        if "Status: paused" in mission("status"):
            break
        time.sleep(15)
    for pid in loop_pids():
        log(f"SIGTERM loop {pid}")
        os.kill(pid, signal.SIGTERM)
    for _ in range(60):
        if not loop_pids():
            break
        time.sleep(2)
    tools = open_workspace_tools()
    if tools:
        log(f"stopping orphaned tools {tools}")
        for pid in tools:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(3)


#: Lane resources that live on THIS machine's GPUs (muse's text seats and
#: vision contexts, a local paddle). --quiet-local switches every such lane
#: off, so the box's GPUs can be freed for a test while the 3060 and the
#: network lanes keep working.
LOCAL_RESOURCES = frozenset({"text_seat", "vision_ctx", "paddle"})


def local_lane_names() -> list[str]:
    from agent.scheduler.worker_pool import _all_scraper_lanes

    return sorted(
        {ln.name for ln in _all_scraper_lanes(None) if ln.resource in LOCAL_RESOURCES}
    )


def start_mission(
    profile: str, *, repack_lanes: str | None = None, quiet_local: bool = False
) -> str:
    env = dict(os.environ, **COMMON_ENV, **PROFILES[profile]["env"])
    for k in ("OUROBOROS_DISABLE_LANES", "OUROBOROS_OCR_LANES"):
        if k not in PROFILES[profile]["env"]:
            env.pop(k, None)
    if repack_lanes is not None:
        env["OUROBOROS_REMOTE_REPACK_LANES"] = repack_lanes
    off = {x for x in env.get("OUROBOROS_DISABLE_LANES", "").split(",") if x}
    off |= set(STANDING_OFF)
    if quiet_local:
        off |= set(local_lane_names())
        log(f"local GPU lanes off: {','.join(sorted(off))}")
    env["OUROBOROS_DISABLE_LANES"] = ",".join(sorted(off))
    mission("resume")
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    logf = os.path.expanduser(f"~/tmp/run_{profile}_{stamp}.log")
    subprocess.Popen(
        [PY, "-u", "ouroboros.py", "start", "--working-dir", WORKDIR],
        cwd=REPO, env=env, stdout=open(logf, "w"), stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )  # fmt: skip
    log(
        f"mission started on profile {profile} (repack lanes {env['OUROBOROS_REMOTE_REPACK_LANES']}) -> {logf}"
    )
    return logf


async def ocr_pending() -> int:
    from agent.actions.extraction_actions import _extraction_pending
    from agent.actions.scholarly_actions import read_databank
    from agent.effects.local import LocalEffects

    bank = await read_databank(LocalEffects(WORKDIR))
    return sum(1 for r in bank.values() if _extraction_pending(r))


def wait_ocr_drained(poll_s: int = 300) -> None:
    quiet = 0
    while True:
        n = asyncio.run(ocr_pending())
        busy = [p for p in open_workspace_tools() if p]
        ocr_busy = any("extract_batch.py" in a for a in _cmdlines(busy))
        quiet = quiet + 1 if (n == 0 and not ocr_busy) else 0
        log(f"OCR pending {n}, OCR tool running {ocr_busy} (quiet polls {quiet}/2)")
        if quiet >= 2:
            return
        time.sleep(poll_s)


def wait_repack_idle(poll_s: int = 600, rounds: int = 3) -> None:
    from agent.actions.curation_actions import REPACK_LANE_IDLE

    while True:
        logs = sorted(Path(os.path.expanduser("~/tmp")).glob("run_repack_*.log"))
        tail: list[str] = []
        if logs:
            lines = logs[-1].read_text(errors="replace").splitlines()
            tail = [ln for ln in lines if "repack lane" in ln or "window repair" in ln]
        idle = 0
        for ln in reversed(tail):
            if REPACK_LANE_IDLE not in ln:
                break
            idle += 1
        log(f"repack lane idle rounds in a row: {idle}/{rounds}")
        if idle >= rounds:
            return
        time.sleep(poll_s)


def _cmdlines(pids: list[int]) -> list[str]:
    out = []
    for p in pids:
        try:
            out.append(
                Path(f"/proc/{p}/cmdline").read_bytes().replace(b"\0", b" ").decode()
            )
        except OSError:
            pass
    return out


def swap_box(model: str) -> bool:
    if box_active() == model:
        log(f"box already serves {model}")
        return True
    log(f"swapModel -> {model}")
    try:
        res = gql(
            f'mutation {{ swapModel(name: "{model}", drainS: 60) {{ ok name previous rolledBack error }} }}'
        )
    except Exception as e:  # noqa: BLE001
        log(f"swap request failed: {e}")
        return False
    out = (res.get("data") or {}).get("swapModel") or {}
    log(f"swap result: {out or res.get('errors')}")
    time.sleep(5)
    active = box_active()
    log(f"box active model now: {active}")
    return bool(out.get("ok")) and active == model


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("to", choices=("to",))
    ap.add_argument("profile", choices=tuple(PROFILES))
    ap.add_argument("--wait-ocr-drained", action="store_true")
    ap.add_argument("--bench-triage", action="store_true")
    ap.add_argument("--wait-repack-idle", action="store_true")
    ap.add_argument(
        "--quiet-local",
        action="store_true",
        help="run no lane on this machine's GPUs (free them for a test)",
    )
    a = ap.parse_args()
    target = PROFILES[a.profile]
    back = "ocr" if a.profile == "repack" else "repack"
    if a.wait_ocr_drained:
        wait_ocr_drained()
    if a.wait_repack_idle:
        wait_repack_idle()
    stop_mission()
    if not swap_box(target["model"]):
        log(f"SWAP FAILED -- restarting the mission on '{back}' as it was")
        start_mission(back)
        return 2
    if a.bench_triage and a.profile == "repack":
        start_mission("repack", repack_lanes="0")
        log("running the triage bench's gemma arm on the idle box")
        rc = subprocess.run(
            [PY, "-u", "dev/bench_triage_judge.py", "run", "gemma"],
            cwd=REPO,
            timeout=3 * 3600,
        ).returncode
        log(f"triage bench gemma arm done (rc {rc})")
        subprocess.run([PY, "dev/bench_triage_judge.py", "report"], cwd=REPO)
        stop_mission()
    start_mission(a.profile, quiet_local=a.quiet_local)
    log("transition complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
