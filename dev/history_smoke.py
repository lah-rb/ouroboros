"""End-to-end smoke of the history store against a LIVE LLMVP server.

Creates a throwaway mission, runs a few code_core cycles, then checks that
what the run did is in ``.agent/history/``: one turn row per inference the run
log reports, a commit for every file the run wrote, a rollback to the first
turn that empties the workspace again, and a clean resume. Run once per mode:

    uv run python dev/history_smoke.py --mode full
    uv run python dev/history_smoke.py --mode metrics
    uv run python dev/history_smoke.py --mode off

Not a unit test (needs the server); the assertions print PASS/FAIL lines and
the script exits non-zero on any FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
sys.path.insert(0, ROOT)

from agent.history import reader  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


def run(
    args: list[str], cwd: str = ROOT, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, os.path.join(ROOT, "ouroboros.py"), *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "metrics", "off"], default="full")
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--keep", action="store_true", help="leave the workspace in place")
    ap.add_argument(
        "--workdir",
        help="use this workspace (default: a fresh temp dir under ~/ouroboros-runs)",
    )
    a = ap.parse_args()

    base = os.path.expanduser("~/ouroboros-runs/history_smoke")
    os.makedirs(base, exist_ok=True)
    wd = a.workdir or tempfile.mkdtemp(prefix=f"{a.mode}_", dir=base)
    print(f"workspace: {wd}")
    r = run(
        [
            "mission",
            "create",
            "--objective",
            "A tiny CLI that prints the sum of two integers given as arguments.",
            "--working-dir",
            wd,
            "--flow-set",
            "code_core",
            "--history",
            a.mode,
        ]
    )
    check(r.returncode == 0, f"mission create ({r.stderr.strip()[-200:]})")
    env = {**os.environ, "OURO_HISTORY": a.mode}
    r = run(["start", "--working-dir", wd, "--max-cycles", str(a.cycles)], env=env)
    log = (r.stdout or "") + (r.stderr or "")
    check(r.returncode == 0, f"start ran {a.cycles} cycles (rc={r.returncode})")
    if r.returncode != 0:
        print(log[-2000:])
    agent_dir = os.path.join(wd, ".agent")

    if a.mode == "off":
        check(
            not reader.has_history(agent_dir) or not reader.list_runs(agent_dir),
            "off: no history rows",
        )
        check(
            not os.path.isdir(os.path.join(agent_dir, "traces")), "no legacy traces dir"
        )
        return finish(wd, a.keep)

    runs = reader.list_runs(agent_dir)
    check(
        len(runs) == 1 and runs[0]["final_status"],
        f"one run, closed with status {runs[0].get('final_status') if runs else None!r}",
    )
    turns = reader.load_turns(agent_dir)
    check(len(turns) > 0, f"{len(turns)} turn rows recorded")
    summary = reader.load_summary(agent_dir)
    check(
        summary is not None
        and summary["summary"]["counts"]["inferences"] == len(turns),
        "ledger inference count == turn rows",
    )
    if a.mode == "full":
        check(
            all(t.get("prompt_content") for t in turns),
            "every turn has its full prompt",
        )
        check(
            all(t.get("response_content") is not None for t in turns),
            "every turn has its response",
        )
    else:
        check(
            all(t.get("content_dropped") for t in turns),
            "metrics: content dropped on every turn",
        )
    commits = reader.load_commits(agent_dir)
    check(
        any(c["source"] == "run_start" for c in commits), "run_start snapshot present"
    )
    written = set()
    for c in commits:
        if c["source"] == "effects_write":
            written.add(c["trigger"])
            paths = {ch["path"] for ch in json.loads(c["changes_json"] or "[]")}
            check(
                c["trigger"] in paths or not c.get("workspace_changed", True),
                f"commit for write {c['trigger']} names it",
            )
    ws_files = [p for p in os.listdir(wd) if not p.startswith(".")]
    check(
        bool(written) == bool(ws_files),
        f"writes recorded ({len(written)}) iff files exist ({len(ws_files)})",
    )
    check(
        not os.path.isdir(os.path.join(wd, ".git")), "no .git placed in the workspace"
    )

    if turns:
        first = turns[0]["turn_id"]
        r = run(["history", "rollback", "--to", first, "--working-dir", wd])
        check(
            r.returncode == 0,
            f"rollback to first turn ({r.stdout.strip().splitlines()[0] if r.stdout else r.stderr[-200:]})",
        )
        # excluded dirs (__pycache__, .venv, …) are never versioned, so a
        # rollback never touches them: only tracked files count here
        from agent.history.excludes import is_excluded_dir

        after = [
            p for p in os.listdir(wd) if not p.startswith(".") and not is_excluded_dir(p)
        ]
        check(not after, f"workspace empty after rollback (left: {after})")
        r = run(["start", "--working-dir", wd, "--max-cycles", "1"], env=env)
        check(r.returncode == 0, "resume after rollback runs a cycle")
        check(
            len(reader.list_runs(agent_dir)) == 3, "runs: original + rollback + resume"
        )
    r = run(
        [
            "trace",
            "--working-dir",
            wd,
            "--format",
            "summary",
            "--output",
            os.path.join(wd, "trace.md"),
        ]
    )
    check(
        r.returncode == 0 and os.path.exists(os.path.join(wd, "trace.md")),
        "trace renders from the store",
    )
    return finish(wd, a.keep)


def finish(wd: str, keep: bool) -> int:
    if FAILS:
        print(f"\n{len(FAILS)} FAIL(s):\n  " + "\n  ".join(FAILS))
    else:
        print("\nall checks passed")
    if not keep and not FAILS:
        shutil.rmtree(wd, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
