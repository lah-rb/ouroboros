"""Mission-history archive — relocation, never truncation.

mission.json is the HOT working set; this module owns the COLD record:
append-only JSONL under ``.agent/archive/`` where completed goals'
reports and failed attempts, and notes/dispatch overflow beyond the
in-file caps, are RELOCATED (never deleted — the archive is the
behavioral-mining substrate: crash-safe, chronologically ordered,
greppable with jq/pandas, strictly better forensics than the mutable
last-write-wins mission.json it relieves).

Layout::

    .agent/archive/
    ├── goals/<goal_id>.jsonl   {"kind": "report"|"failed_attempt", ...}
    ├── notes.jsonl             NoteRecord overflow (oldest-first)
    └── dispatch.jsonl          DispatchRecord overflow (oldest-first)

Every line carries the record verbatim plus ``kind`` and
``archived_at``. Writes are O_APPEND under ``fcntl.flock`` — the
ouroboros.py CLI can touch .agent/ while a run is live, and two
writers interleaving lines is the failure the lock prevents; atomic
rename is unnecessary for append-only files.

The sweep (:func:`archive_mission_overflow`) is the single choke point
that all 12 goal-completion sites funnel through via the per-cycle
``attach_directive_report`` + the terminal ``finalize_mission`` — one
idempotent pass instead of 12 call-site edits.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from typing import Any, Iterator
from agent.persistence.models import _now_iso

logger = logging.getLogger(__name__)

ARCHIVE_DIR = "archive"
GOALS_SUBDIR = "goals"

# In-file caps for the mission-level rolling lists. Director projections
# read far less (last-8 notes / last-5 dispatches); the caps keep enough
# tail for dedupe/tag scans while bounding the per-cycle parse cost.
NOTES_CAP = 100
DISPATCH_CAP = 50



def _archive_root(agent_dir: str) -> str:
    return os.path.join(agent_dir, ARCHIVE_DIR)


def _append_jsonl(path: str, records: list[dict]) -> None:
    """Append records as JSONL lines under an exclusive file lock."""
    if not records:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(payload)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _record_lines(kind: str, records: list[Any]) -> list[dict]:
    stamp = _now_iso()
    out = []
    for rec in records:
        data = rec.model_dump() if hasattr(rec, "model_dump") else dict(rec)
        out.append({"kind": kind, "archived_at": stamp, **data})
    return out


def append_goal_records(
    agent_dir: str, goal_id: str, reports: list[Any], attempts: list[Any]
) -> int:
    """Relocate a completed goal's reports + failed attempts to its JSONL."""
    lines = _record_lines("report", reports) + _record_lines("failed_attempt", attempts)
    path = os.path.join(_archive_root(agent_dir), GOALS_SUBDIR, f"{goal_id}.jsonl")
    _append_jsonl(path, lines)
    return len(lines)


def append_overflow(agent_dir: str, name: str, kind: str, records: list[Any]) -> int:
    """Relocate rolling-list overflow (notes/dispatch) oldest-first."""
    path = os.path.join(_archive_root(agent_dir), f"{name}.jsonl")
    lines = _record_lines(kind, records)
    _append_jsonl(path, lines)
    return len(lines)


def iter_archive(
    agent_dir: str, goal_id: str | None = None, kind: str | None = None
) -> Iterator[dict]:
    """Yield archived records in file order — the miner's entry point.

    goal_id filters to one goal's file; kind filters record kinds
    ("report", "failed_attempt", "note", "dispatch"). Plain JSONL means
    jq/pandas work directly too; this is the convenience wrapper.
    """
    root = _archive_root(agent_dir)
    paths: list[str] = []
    goals_dir = os.path.join(root, GOALS_SUBDIR)
    if goal_id is not None:
        paths.append(os.path.join(goals_dir, f"{goal_id}.jsonl"))
    else:
        if os.path.isdir(goals_dir):
            paths.extend(
                os.path.join(goals_dir, f) for f in sorted(os.listdir(goals_dir))
            )
        for name in ("notes.jsonl", "dispatch.jsonl"):
            paths.append(os.path.join(root, name))
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("unparseable archive line in %s", path)
                    continue
                if kind is None or rec.get("kind") == kind:
                    yield rec


def archive_mission_overflow(agent_dir: str, mission: Any) -> bool:
    """The sweep: relocate completed goals' records + rolling overflow.

    Idempotent and cheap when nothing overflows (length checks only).
    Mutates ``mission`` in place; returns True when it changed (the
    caller persists). NEVER deletes — every removed record lands in the
    archive first, and the additive ``*_archived`` counters keep
    attempted-work signals readable without the records (e.g. the
    batch_attempted guard).
    """
    changed = False

    for goal in getattr(mission, "goals", []):
        if goal.status != "complete":
            continue
        reports = list(goal.reports or [])
        attempts = list(getattr(goal, "failed_attempts", None) or [])
        if not reports and not attempts:
            continue
        append_goal_records(agent_dir, goal.id, reports, attempts)
        goal.reports_archived = int(getattr(goal, "reports_archived", 0)) + len(reports)
        goal.attempts_archived = int(getattr(goal, "attempts_archived", 0)) + len(
            attempts
        )
        goal.reports = []
        goal.failed_attempts = []
        changed = True
        logger.info(
            "🗄️ archived goal %s: %d report(s), %d attempt(s)",
            goal.id,
            len(reports),
            len(attempts),
        )

    notes = getattr(mission, "notes", None)
    if notes is not None and len(notes) > NOTES_CAP:
        overflow = notes[:-NOTES_CAP]
        append_overflow(agent_dir, "notes", "note", overflow)
        mission.notes_archived = int(getattr(mission, "notes_archived", 0)) + len(
            overflow
        )
        del notes[:-NOTES_CAP]
        changed = True
        logger.info("🗄️ archived %d note(s) beyond the %d cap", len(overflow), NOTES_CAP)

    dispatch = getattr(mission, "dispatch_history", None)
    if dispatch is not None and len(dispatch) > DISPATCH_CAP:
        overflow = dispatch[:-DISPATCH_CAP]
        append_overflow(agent_dir, "dispatch", "dispatch", overflow)
        mission.dispatch_archived = int(getattr(mission, "dispatch_archived", 0)) + len(
            overflow
        )
        del dispatch[:-DISPATCH_CAP]
        changed = True
        logger.info(
            "🗄️ archived %d dispatch record(s) beyond the %d cap",
            len(overflow),
            DISPATCH_CAP,
        )

    return changed
