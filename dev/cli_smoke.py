"""CLI smoke test — exercise `--help` on every ouroboros.py subcommand.

Catches silent import rot: a subcommand whose handler imports a deleted
module will fail to parse even its own --help invocation. Cheap way to
keep dead CLI commands from accumulating hidden errors.

Run:
    python dev/cli_smoke.py

Exits non-zero on any failure.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Top-level subcommands. Each is invoked as:
#     ouroboros.py <cmd> --help
# Mission subcommands are invoked as:
#     ouroboros.py mission <subcmd> --help
TOP_LEVEL_SUBCOMMANDS = [
    "start",
    "blueprint",
    "trace",
    "lint",
    "cue-compile",
    "lint-flows",
    "smoke",
    "mission",
    "llmvp",
]

MISSION_SUBCOMMANDS = [
    "create",
    "status",
    "pause",
    "resume",
    "abort",
    "message",
    "history",
]

LLMVP_SUBCOMMANDS = [
    "models",
    "swap",
]


def _run(argv: list[str]) -> tuple[int, str]:
    """Run a CLI invocation and capture exit code + combined stderr/stdout."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return result.returncode, (result.stdout + result.stderr).strip()


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    entry = repo_root / "ouroboros.py"
    if not entry.exists():
        print(f"ERROR: {entry} not found", file=sys.stderr)
        return 2

    # Always invoke via the same interpreter that's running this script.
    python = sys.executable

    failures: list[tuple[str, int, str]] = []
    checks = 0

    # Top-level --help first (ensures the main parser is wired)
    checks += 1
    rc, out = _run([python, str(entry), "--help"])
    if rc != 0:
        failures.append(("--help", rc, out))

    for cmd in TOP_LEVEL_SUBCOMMANDS:
        checks += 1
        rc, out = _run([python, str(entry), cmd, "--help"])
        if rc != 0:
            failures.append((cmd, rc, out))

    for sub in MISSION_SUBCOMMANDS:
        checks += 1
        rc, out = _run([python, str(entry), "mission", sub, "--help"])
        if rc != 0:
            failures.append((f"mission {sub}", rc, out))

    for sub in LLMVP_SUBCOMMANDS:
        checks += 1
        rc, out = _run([python, str(entry), "llmvp", sub, "--help"])
        if rc != 0:
            failures.append((f"llmvp {sub}", rc, out))

    print(f"CLI smoke: {checks - len(failures)}/{checks} passed")
    if failures:
        print()
        for name, rc, out in failures:
            print(f"  FAIL: `ouroboros.py {name} --help` exited {rc}")
            # Show first 3 lines of output to keep signal high
            for line in out.splitlines()[:3]:
                print(f"    {line}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
