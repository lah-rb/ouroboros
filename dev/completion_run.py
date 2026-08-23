#!/usr/bin/env python3
"""Launch a mission and let it run to its OWN completion.

NOT a tier assessment. The tier harness exists to make arms comparable, and
every mechanism it adds for that — the league governors, the phase ceiling as
provenance, staging for blind flights — is a BOUND. `game_challenge_tier_session`
already carries `run_until: completed`; under the tier runner the league is what
stops it (contemplators: 30 cycles hard). Run outside that harness with neither
--max-cycles nor --max-wall-clock and the only stop condition left is the
mission's own final gate.

Detaches with start_new_session=True, which is what makes a multi-day run
survive the launching shell exiting (nohup alone does not).
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "http://localhost:8008/graphql"
BOOT_TIMEOUT_S = 3600  # 104.2 GB across 4 shards — first load is not quick


def healthy() -> bool:
    try:
        req = urllib.request.Request(
            ENDPOINT,
            data=b'{"query":"{ availableInstances }"}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> int:
    config = sys.argv[1]
    mission = sys.argv[2]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = Path.home() / "ouroboros-runs" / f"completion_{config}_{stamp}"
    base.mkdir(parents=True, exist_ok=True)
    work = Path("/tmp/completion") / config
    log = (base / "launch.log").open("w", buffering=1)

    def say(m: str) -> None:
        log.write(f"[{datetime.now():%m-%d %H:%M:%S}] {m}\n")

    say(f"COMPLETION RUN — {config} · {mission}")
    say(f"  base={base}  work={work}")
    say("  UNBOUNDED: no --max-cycles, no --max-wall-clock; run_until=completed")

    # ── server ────────────────────────────────────────────────────
    # Booting a second server against an already-resident 104 GB model would
    # try to wire the weights twice. Adopt a healthy one instead — but only
    # after confirming it is serving THIS config, since a server left up by
    # something else is the wrong model wearing a healthy endpoint.
    live = (ROOT / "llmvp" / "active_config.txt").read_text().strip()
    if healthy() and live == config:
        say(f"  server already UP on {config} — adopting it")
        return _launch(config, mission, base, work, say, log)
    (ROOT / "llmvp" / "active_config.txt").write_text(config)
    slog = (base / "server.log").open("w")
    subprocess.Popen(
        [str(ROOT / "llmvp" / ".venv" / "bin" / "python"), "api/main.py"],
        cwd=ROOT / "llmvp",
        stdout=slog,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    say(f"  booting server (timeout {BOOT_TIMEOUT_S // 60}min)…")
    deadline = time.time() + BOOT_TIMEOUT_S
    while time.time() < deadline:
        if healthy():
            say(f"  server UP after {int(time.time() - (deadline - BOOT_TIMEOUT_S))}s")
            break
        txt = (base / "server.log").read_text(errors="ignore")
        if "Startup failed" in txt:
            say("  !! STARTUP FAILED — see server.log")
            return 1
        time.sleep(10)
    else:
        say("  !! server never became healthy")
        return 1

    return _launch(config, mission, base, work, say, log)


def _launch(config, mission, base, work, say, log) -> int:
    # ── mission ───────────────────────────────────────────────────
    if (work / ".agent").exists():
        say(f"  reusing existing workspace {work}")
    else:
        # `mission create` requires the directory to ALREADY exist — it
        # refuses rather than creating it, and reports that refusal on
        # stdout with an empty stderr, so a caller capturing only stderr
        # sees a bare "failed" with no reason.
        work.mkdir(parents=True, exist_ok=True)
        say("  creating mission…")
        c = subprocess.run(
            [
                sys.executable,
                "ouroboros.py",
                "mission",
                "create",
                "--mission_config",
                mission,
                "--working-dir",
                str(work),
                "--top-phase",
                "quality",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if c.returncode != 0:
            say(f"  !! mission create failed: {(c.stderr or c.stdout)[-400:]}")
            return 1
        say("  mission created")

    # ── the run itself, detached and unbounded ────────────────────
    rlog = (base / "run.log").open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "ouroboros.py",
            "start",
            "--working-dir",
            str(work),
            "--trace-thinking",
        ],
        cwd=ROOT,
        stdout=rlog,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    say(f"  RUN STARTED pid={proc.pid} — unbounded, to completion")
    (base / "RUN_PID").write_text(str(proc.pid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
