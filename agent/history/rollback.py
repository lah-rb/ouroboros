"""Roll the workspace back to the tree a recorded turn saw.

    history rollback --to <turn> [--to-commit SHA] [--dry-run] [--keep-mission-state]

The target is a turn's ``tree_before`` (the workspace as it stood when that
call started) or an explicit commit. The rollback itself is recorded and
undoable: the current tree is committed first (``pre_rollback``), then the
files are restored, then the restored tree is committed (``rollback``) with
the pre_rollback commit as its parent — history is append-only, nothing is
rewritten. ``.agent/mission.json`` is restored with the code unless told
otherwise, so goals and notes match the tree; the persistence layer
re-bootstraps its Loro journal from the restored view on the next load.

Refused while a mission process holds the history LOCK. Resuming afterwards
is just ``ouroboros.py start`` — server sessions are ephemeral and the flows
recreate them.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from agent.history import reader
from agent.history.snapshot import RestoreReport, WorkspaceSnapshotter
from agent.history.store import (
    REPO_DIR,
    HistoryStore,
    cli_lock,
    history_dir,
    new_run_id,
)


@dataclass
class RollbackReport:
    target_commit: str
    turn_id: str = ""
    dry_run: bool = False
    pre_rollback_commit: str = ""
    rollback_commit: str = ""
    restore: Optional[RestoreReport] = None
    warnings: list[str] = field(default_factory=list)
    mission_status: str = ""


def _mission_status(working_dir: str) -> str:
    path = os.path.join(working_dir, ".agent", "mission.json")
    try:
        with open(path, encoding="utf-8") as f:
            return str(json.load(f).get("status") or "")
    except (OSError, ValueError):
        return ""


def resolve_target(
    agent_dir: str, to: Optional[str], to_commit: Optional[str]
) -> tuple[str, str]:
    """(commit sha, turn_id) for a rollback target. Raises ValueError."""
    if to_commit:
        matches = [
            c["commit_sha"]
            for c in reader.load_commits(agent_dir)
            if c["commit_sha"].startswith(to_commit)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"commit {to_commit!r} matches {len(matches)} recorded commit(s)"
            )
        return matches[0], ""
    if not to:
        raise ValueError("a target is required: --to <turn> or --to-commit <sha>")
    turn = reader.get_turn(agent_dir, to)
    if turn is None:
        raise ValueError(f"no turn {to!r}")
    sha = turn.get("tree_before") or ""
    if not sha:
        raise ValueError(
            f"turn {turn['turn_id']} recorded no tree (snapshots were off for its run)"
        )
    return sha, turn["turn_id"]


def rollback(
    working_dir: str,
    *,
    to: Optional[str] = None,
    to_commit: Optional[str] = None,
    dry_run: bool = False,
    keep_mission_state: bool = False,
) -> RollbackReport:
    working_dir = os.path.realpath(working_dir)
    agent_dir = os.path.join(working_dir, ".agent")
    with cli_lock(working_dir):
        target, turn_id = resolve_target(agent_dir, to, to_commit)
        report = RollbackReport(target_commit=target, turn_id=turn_id, dry_run=dry_run)
        status = _mission_status(working_dir)
        if status == "active":
            report.warnings.append(
                "mission.json says the mission is ACTIVE but no process holds the "
                "lock — a crashed run; the rollback proceeds"
            )
        snapshotter = WorkspaceSnapshotter(
            working_dir, os.path.join(history_dir(working_dir), REPO_DIR)
        )
        if dry_run:
            report.restore = snapshotter.restore(
                target, dry_run=True, keep_agent_state=keep_mission_state
            )
            report.mission_status = status
            return report
        mission_id = (
            next(
                (
                    r.get("mission_id")
                    for r in reversed(reader.list_runs(agent_dir))
                    if r.get("mission_id")
                ),
                "",
            )
            or ""
        )
        store = HistoryStore(
            working_dir,
            mission_id,
            "full",
            run_id=new_run_id("cli-rollback-"),
            snapshotter=snapshotter,
            lock=False,
            meta={"flow_set": "", "entry_flow": "history rollback"},
        )
        loop = asyncio.new_event_loop()
        try:
            # 1. the tree as it is now, so the rollback itself can be undone
            report.pre_rollback_commit = (
                loop.run_until_complete(
                    store.checkpoint(
                        "pre_rollback", trigger=turn_id or target, force=True
                    )
                )
                or snapshotter.head()
            )
            # 2. restore
            report.restore = snapshotter.restore(
                target, dry_run=False, keep_agent_state=keep_mission_state
            )
            # 3. record the restored tree (parent = pre_rollback) + the event
            report.rollback_commit = (
                loop.run_until_complete(
                    store.checkpoint("rollback", trigger=turn_id or target)
                )
                or snapshotter.head()
            )
            store.ingest(
                {
                    "event_type": "rollback",
                    "mission_id": mission_id,
                    "flow": "history",
                    "step": "rollback",
                    "target_commit": target,
                    "turn_id": turn_id,
                    "pre_rollback_commit": report.pre_rollback_commit,
                    "rollback_commit": report.rollback_commit,
                    "restored": list(report.restore.restored),
                    "deleted": list(report.restore.deleted),
                    "skipped": list(report.restore.skipped),
                    "keep_mission_state": keep_mission_state,
                }
            )
            loop.run_until_complete(store.close("rollback"))
        finally:
            loop.close()
        report.mission_status = _mission_status(working_dir)
        return report


def format_report(report: RollbackReport) -> str:
    r = report.restore
    lines = []
    head = "DRY RUN — nothing changed" if report.dry_run else "rolled back"
    lines.append(
        f"{head}: workspace → {report.target_commit[:12]}"
        + (f" (turn {report.turn_id}, its tree_before)" if report.turn_id else "")
    )
    for w in report.warnings:
        lines.append(f"  warning: {w}")
    if r is not None:
        lines.append(
            f"  restored {len(r.restored)} file(s), deleted {len(r.deleted)}, "
            f"skipped {len(r.skipped)}, unchanged {r.unchanged}"
        )
        for p in r.restored:
            lines.append(f"    ~ {p}")
        for p in r.deleted:
            lines.append(f"    - {p}")
        for p in r.skipped:
            lines.append(f"    ! skipped {p} (stub or outside the tree)")
    if not report.dry_run:
        lines.append(
            f"  pre_rollback commit {report.pre_rollback_commit[:12]}  →  rollback commit {report.rollback_commit[:12]}"
        )
        lines.append(f"  mission status now: {report.mission_status or '(none)'}")
        lines.append("  resume with: ouroboros.py start --working-dir <workspace>")
        lines.append(
            f"  undo with:   ouroboros.py history rollback --to-commit {report.pre_rollback_commit[:12]}"
        )
    return "\n".join(lines)


__all__ = ["RollbackReport", "format_report", "resolve_target", "rollback"]
