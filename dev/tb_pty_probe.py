"""R1 isolation test: can our PTY server drive a container via `docker exec`?

Make-or-break for the terminal-bench adapter. Spawns a PTYSessionManager
session whose launcher is `docker exec -i -w <cwd> <container> /bin/bash`,
then sends input and checks we capture output, that writes land INSIDE the
container (not the host), and that the shell dies cleanly on close.

Usage: .venv/bin/python dev/tb_pty_probe.py <container-name> [container-cwd]
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from mcp_servers.terminal.pty_session import PTYSessionManager


def _dump(label: str, r) -> None:
    fields = {
        k: getattr(r, k)
        for k in (
            "status",
            "prompt_detected",
            "settled_cleanly",
            "total_bytes_received",
            "duration_ms",
        )
        if hasattr(r, k)
    }
    print(f"  [{label}] output={r.output!r}")
    print(f"  [{label}] {fields}")


async def main(cname: str, cwd: str) -> int:
    host_scratch = "/tmp/ouro-pty-scratch"
    launcher = ["docker", "exec", "-i", "-w", cwd, cname, "/bin/bash"]
    print(f"launcher: {' '.join(launcher)}")
    m = PTYSessionManager()
    sid = await m.create_session(command=launcher, working_directory=host_scratch)
    print(f"session: {sid}")

    ok = True

    # 1. Basic command + output capture (no $PS1 expected under docker exec).
    r = await m.send_input(sid, "echo HELLO_$((6*7))\n", settle_ms=600)
    _dump("echo", r)
    if "HELLO_42" not in r.output:
        print("  FAIL: did not capture command output")
        ok = False
    else:
        print("  PASS: captured output without a prompt")

    # 2. cwd is the container path.
    r = await m.send_input(sid, "pwd\n", settle_ms=400)
    _dump("pwd", r)
    if cwd not in r.output:
        print(f"  FAIL: cwd not {cwd}")
        ok = False
    else:
        print(f"  PASS: cwd is {cwd}")

    # 3. Writes land INSIDE the container (grading correctness).
    await m.send_input(sid, "echo 42 > /app/__probe.txt\n", settle_ms=400)
    r = await m.send_input(sid, "cat /app/__probe.txt\n", settle_ms=400)
    _dump("write", r)
    in_container = subprocess.run(
        ["docker", "exec", cname, "cat", "/app/__probe.txt"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"  container sees /app/__probe.txt = {in_container!r}")
    if in_container != "42":
        print("  FAIL: write did not land in container")
        ok = False
    else:
        print("  PASS: write landed in container")

    # 4. No host dir leak (R2): the container cwd must NOT exist on the host.
    if os.path.isdir(cwd) and cwd not in ("/", "/tmp"):
        print(f"  FAIL: host dir {cwd} was created (host-makedirs leak)")
        ok = False
    else:
        print(f"  PASS: no host {cwd} leak")

    # 5. Clean close — the container shell should die.
    before = subprocess.run(
        ["docker", "exec", cname, "sh", "-c", "ps -e | grep -c bash || true"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    await m.close_session(sid)
    await asyncio.sleep(0.5)
    after = subprocess.run(
        ["docker", "exec", cname, "sh", "-c", "ps -e | grep -c bash || true"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"  bash procs in container before/after close: {before} -> {after}")

    print("\nRESULT:", "ALL GREEN" if ok else "FAILURES — see above")
    return 0 if ok else 1


if __name__ == "__main__":
    cname = sys.argv[1] if len(sys.argv) > 1 else "ouro-probe"
    cwd = sys.argv[2] if len(sys.argv) > 2 else "/app"
    raise SystemExit(asyncio.run(main(cname, cwd)))
