#!/usr/bin/env python3
"""Record wired memory to disk durably enough to survive a hard crash.

    llmvp/.venv/bin/python dev/wired_mem_recorder.py --label hy3-24576 [--hz 20]

WHY THIS EXISTS. On 2026-07-28 the Hy3-REAP-200B ladder loaded a model whose
weights plus KV compute to 116.4 GB — ABOVE iogpu.wired_limit_mb=116000 — and
served fine. So the wired limit is not the hard ceiling I assumed, and nobody
knows where the real one is. The way to find out is to be recording when the
machine dies.

THE ONLY PROPERTY THAT MATTERS IS THAT THE LAST LINE SURVIVES. A buffered
writer loses exactly the samples we care about — the ones just before the
kernel gave up. So every sample is written, flushed, and fsync'd before the
next is taken. That caps throughput far below what psutil can do (~400k
samples/sec) and it is the right trade: this is a flight recorder, not a
profiler.

Output is CSV under ~/ouroboros-runs/, never /tmp — /tmp does not survive the
reboot this is designed to observe.

Reading it afterwards: the last row is the high-water mark the machine died at.
`--peak-every N` also writes a PEAK row every N seconds so a long quiet run
leaves a compact record rather than only its tail.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover — the venv that has it is llmvp's
    sys.exit(
        "psutil not available. Run with llmvp/.venv/bin/python, which has it."
    )

RUNS = Path.home() / "ouroboros-runs"


def gb(n: float) -> float:
    return round(n / 1e9, 3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run", help="tag for the output filename")
    ap.add_argument("--hz", type=float, default=20.0,
                    help="samples per second (fsync-bound; 20 is comfortable)")
    ap.add_argument("--peak-every", type=float, default=30.0,
                    help="seconds between PEAK summary rows")
    ap.add_argument("--watch", default="api/main.py",
                    help="substring of the process cmdline to track RSS for")
    args = ap.parse_args()

    RUNS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RUNS / f"wired_{args.label}_{stamp}.csv"

    limit_mb = 0
    try:
        import subprocess

        limit_mb = int(
            subprocess.run(["sysctl", "-n", "iogpu.wired_limit_mb"],
                           capture_output=True, text=True).stdout.strip() or 0
        )
    except Exception:  # noqa: BLE001 — a missing sysctl must not stop recording
        pass

    total_gb = gb(psutil.virtual_memory().total)
    f = out.open("w", buffering=1)
    f.write(f"# physical={total_gb}GB iogpu.wired_limit_mb={limit_mb} "
            f"label={args.label} hz={args.hz}\n")
    f.write("kind,ts,elapsed_s,wired_gb,active_gb,free_gb,available_gb,"
            "swap_used_gb,watched_rss_gb,pct_of_wired_limit\n")
    f.flush()
    os.fsync(f.fileno())

    print(f"recording -> {out}")
    print(f"  physical {total_gb} GB · iogpu.wired_limit_mb {limit_mb} · {args.hz} Hz")
    print("  every row is fsync'd; the LAST row is the high-water mark at death")

    stop = False

    def _sig(_s, _f):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    t0 = time.time()
    period = 1.0 / max(args.hz, 0.1)
    peak_wired = 0.0
    peak_row = ""
    last_peak_write = t0

    # Resolve the watched process once, re-resolve if it dies (a rung restarts
    # the server between loads, so the pid changes and a stale handle would
    # silently record zeros for the rest of the run).
    proc = None

    def _find():
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                if p.info["cmdline"] and any(args.watch in c for c in p.info["cmdline"]):
                    return p
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None

    try:
        while not stop:
            m = psutil.virtual_memory()
            try:
                sw = psutil.swap_memory().used
            except Exception:  # noqa: BLE001
                sw = 0
            if proc is None or not proc.is_running():
                proc = _find()
            rss = 0
            if proc is not None:
                try:
                    rss = proc.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    proc = None
            now = time.time()
            wired = gb(m.wired)
            pct = round(100.0 * m.wired / (limit_mb * 1e6), 1) if limit_mb else 0.0
            row = (f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]},"
                   f"{now - t0:.2f},{wired},{gb(m.active)},{gb(m.free)},"
                   f"{gb(m.available)},{gb(sw)},{gb(rss)},{pct}")
            f.write("S," + row + "\n")
            f.flush()
            os.fsync(f.fileno())  # THE point of this script

            if wired > peak_wired:
                peak_wired, peak_row = wired, row
            if now - last_peak_write >= args.peak_every:
                f.write("PEAK," + peak_row + "\n")
                f.flush()
                os.fsync(f.fileno())
                last_peak_write = now

            time.sleep(period)
    finally:
        if peak_row:
            f.write("PEAK," + peak_row + "\n")
        f.write(f"# clean exit; peak wired {peak_wired} GB of "
                f"{limit_mb / 1000 if limit_mb else '?'} GB limit\n")
        f.flush()
        os.fsync(f.fileno())
        f.close()
        print(f"peak wired: {peak_wired} GB   ->  {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
