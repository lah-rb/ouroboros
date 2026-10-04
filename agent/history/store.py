"""HistoryStore — the writer for ``<workspace>/.agent/history/``.

One store per mission PROCESS ("run"). It is the sink behind
``LocalEffects.emit_trace``: every trace event is ingested, becomes a row
(``InferenceCall`` → ``turns``, everything else → ``events``), and the same
dict is what the finite-time ledger folds — so the ledger and the tables can
never disagree.

DURABILITY. A turn's prompt and response are the expensive things; they are
written as their own part file the moment the turn is ingested, so a crash in
the next step loses nothing already paid for. Events are flushed at step and
cycle boundaries (the loop's ``flush_traces`` cadence). Part files are written
tmp+rename, so a reader never sees a half-written file.

PARQUET IS IMMUTABLE. Appending means small part files; a run partition is
compacted at every cycle end (``cycle-…``) and at close (``run-…``). Compaction
writes the merged file, renames it in, THEN unlinks its inputs — a crash in
between leaves duplicates, which the reader dedupes on the primary key. The
worst case is therefore "some rows exist twice", never "rows are gone".

ONE WRITER. The running mission holds ``LOCK`` (flock) for its lifetime; CLI
writers (rollback, compact, import) take the same lock and refuse while it is
held. Within the process everything funnels through one asyncio loop; the
parquet I/O runs in a worker thread behind one lock so a flush and a
compaction never interleave.
"""

from __future__ import annotations

import asyncio
import fcntl
import glob
import json
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Literal, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from agent.history import redact as _redact
from agent.history.schema import (
    CONTENT_COLUMNS,
    COMMITS_SCHEMA,
    EVENTS_SCHEMA,
    RUNS_SCHEMA,
    SCHEMA_VERSION,
    TABLES,
    TURNS_SCHEMA,
    coerce_row,
    event_row_from_event,
    turn_row_from_event,
)

logger = logging.getLogger(__name__)

HISTORY_DIR = "history"
LOCK_FILE = "LOCK"
REPO_DIR = "repo.git"
HistoryMode = Literal["full", "metrics", "off"]
HISTORY_MODES: tuple[str, ...] = ("full", "metrics", "off")

# Ingesting one of these flushes the events buffer: they close a unit of work,
# and the loop's own flush_traces lands at cycle_end anyway.
_FLUSH_ON = frozenset({"step_end", "cycle_end", "flow_return"})


class HistoryLocked(RuntimeError):
    """Another process holds ``.agent/history/LOCK``."""


@dataclass
class SnapshotResult:
    """What a workspace snapshotter returns for one tree change (P3 fills
    this in; the store only needs the shape to book a ``commits`` row)."""

    commit_sha: str
    parent_sha: str
    tree_sha: str
    changes: list[dict] = field(default_factory=list)
    large_files: list[dict] = field(default_factory=list)
    files_scanned: int = 0
    bytes_hashed: int = 0
    scan_ms: float = 0.0
    workspace_changed: bool = True

    @property
    def counts(self) -> tuple[int, int, int]:
        add = sum(1 for c in self.changes if c.get("kind") == "add")
        mod = sum(1 for c in self.changes if c.get("kind") == "modify")
        rm = sum(1 for c in self.changes if c.get("kind") == "delete")
        return add, mod, rm


def history_dir(working_dir: str) -> str:
    return os.path.join(working_dir, ".agent", HISTORY_DIR)


def new_run_id(prefix: str = "") -> str:
    """Sortable: UTC timestamp first, a short random tail to keep two starts
    in the same second apart."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    tail = uuid.uuid4().hex[:6]
    return f"{prefix}{ts}-{tail}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Locking ───────────────────────────────────────────────────────────


def _try_lock(path: str) -> Optional[int]:
    """Open + flock(EX|NB). Returns the fd, or None when someone holds it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def is_locked(working_dir: str) -> bool:
    """True when a process currently holds the history lock."""
    fd = _try_lock(os.path.join(history_dir(working_dir), LOCK_FILE))
    if fd is None:
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    return False


@contextmanager
def cli_lock(working_dir: str) -> Iterator[None]:
    """Hold the history lock for a CLI writer (rollback, compact, import).
    Raises HistoryLocked when the mission process has it."""
    fd = _try_lock(os.path.join(history_dir(working_dir), LOCK_FILE))
    if fd is None:
        raise HistoryLocked(
            f"{history_dir(working_dir)}/LOCK is held — a mission process is "
            "running here; `mission pause` and wait for it to drain first."
        )
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ── Atomic file helpers ───────────────────────────────────────────────


def _atomic_json(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, default=str)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_parquet_atomic(table: pa.Table, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        pq.write_table(table, tmp, compression="zstd")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_PART_NAME = re.compile(r"^part-(\d+)-(\d+)-[0-9a-f]+$")
_CYCLE_NAME = re.compile(r"^cycle-\d+-(\d+)-(\d+)(?:-[0-9a-f]+)?$")
_RUN_NAME = re.compile(r"^run-(\d+)-(\d+)(?:-[0-9a-f]+)?$")


def _seq_range(filename: str) -> tuple[int, int]:
    """The seq range a part/cycle/run file covers, from its name. Each shape
    is matched by position — a random hex suffix can be all digits (it was:
    ``…-00200293`` named a cycle file after a suffix), so "the last two
    ints" is not a rule that holds."""
    stem = os.path.basename(filename).rsplit(".", 1)[0]
    for pat in (_PART_NAME, _CYCLE_NAME, _RUN_NAME):
        m = pat.match(stem)
        if m:
            return int(m.group(1)), int(m.group(2))
    return 0, 0


def merge_parquet_files(inputs: list[str], output: str, schema: pa.Schema) -> None:
    """Stream ``inputs`` into ``output`` (one row group per input), rename in,
    then unlink the inputs. Memory is bounded by the largest input."""
    if not inputs:
        return
    os.makedirs(os.path.dirname(output), exist_ok=True)
    tmp = f"{output}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        with pq.ParquetWriter(tmp, schema, compression="zstd") as writer:
            for f in inputs:
                writer.write_table(pq.read_table(f, schema=schema))
        os.chmod(tmp, 0o600)
        os.replace(tmp, output)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    for f in inputs:
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass


# ── The store ─────────────────────────────────────────────────────────


class HistoryStore:
    """Writer for one mission run. See the module docstring."""

    def __init__(
        self,
        working_dir: str,
        mission_id: str,
        mode: str = "full",
        *,
        run_id: Optional[str] = None,
        snapshotter: Any = None,
        meta: Optional[dict] = None,
        lock: bool = True,
        started_at: Optional[str] = None,
        write_runs_row: bool = True,
    ) -> None:
        """``lock=False`` is for CLI writers that already hold ``cli_lock``;
        ``started_at`` lets an import record the run's real start;
        ``write_runs_row=False`` reopens an existing run (compaction) without
        overwriting its runs row."""
        if mode not in ("full", "metrics"):
            raise ValueError(f"HistoryStore mode must be full|metrics, got {mode!r}")
        self.working_dir = os.path.realpath(working_dir)
        self.mission_id = mission_id
        self.mode = mode
        self.run_id = run_id or new_run_id()
        self.history_dir = history_dir(self.working_dir)
        self._snapshotter = snapshotter
        self._meta = dict(meta or {})
        self._seq = 0
        self._pending: dict[str, list[dict]] = {
            "turns": [],
            "events": [],
            "commits": [],
        }
        self._counts = {"turns": 0, "events": 0, "commits": 0, "cycles": 0}
        self._io_lock = asyncio.Lock()
        self._lock_fd: Optional[int] = None
        self._closed = False
        self.last_ingested_id = ""
        self._write_runs = write_runs_row
        self.started_at = started_at or _iso_now()
        os.makedirs(self.history_dir, exist_ok=True)
        if lock:
            fd = _try_lock(os.path.join(self.history_dir, LOCK_FILE))
            if fd is None:
                raise HistoryLocked(
                    f"{self.history_dir}/LOCK is held by another process"
                )
            self._lock_fd = fd
        # The runs row exists from the first moment, so a crashed run still
        # lists; close() rewrites it with the end state and the summary.
        if self._write_runs:
            self._write_runs_row(final_status="", summary=None)

    @classmethod
    def open_for_run(
        cls,
        working_dir: str,
        mission_id: str,
        mode: str = "full",
        *,
        run_id: Optional[str] = None,
        snapshotter: Any = None,
        meta: Optional[dict] = None,
    ) -> "HistoryStore":
        return cls(
            working_dir,
            mission_id,
            mode,
            run_id=run_id,
            snapshotter=snapshotter,
            meta=meta,
        )

    # ── identity ──────────────────────────────────────────────────────

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def head(self) -> str:
        """Commit sha of the workspace tree as last snapshotted ("" if none)."""
        if self._snapshotter is None:
            return ""
        try:
            return str(self._snapshotter.head() or "")
        except Exception:  # noqa: BLE001 — history must never break a run
            logger.debug("snapshotter.head failed", exc_info=True)
            return ""

    @property
    def counts(self) -> dict:
        return dict(self._counts)

    # ── ingest ────────────────────────────────────────────────────────

    def ingest(self, event: Any) -> dict:
        """Buffer one trace event; return the dict the ledger should fold.

        Synchronous on purpose: seq assignment and the buffer append cannot
        interleave with another coroutine. Returns the ORIGINAL event dict
        (what ``fold_event`` expects), not the row.
        """
        d = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        et = d.get("event_type", "")
        seq = self.next_seq()
        ts_ms = _now_ms()
        row_id = uuid.uuid4().hex[:16]
        self.last_ingested_id = row_id  # the turn_id / event_id just minted
        if et == "inference_call":
            row = turn_row_from_event(
                d,
                run_id=self.run_id,
                seq=seq,
                turn_id=row_id,
                ts_ms=ts_ms,
                tree_before=self.head(),
                drop_content=(self.mode == "metrics"),
            )
            if self.mode == "full" and _redact.enabled():
                kinds: list[str] = []
                for c in CONTENT_COLUMNS:
                    if row.get(c):
                        row[c], found = _redact.redact(row[c])
                        kinds.extend(found)
                if kinds:
                    extra = json.loads(row.get("extra_json") or "{}")
                    extra["content_redacted"] = sorted(set(kinds))
                    row["extra_json"] = json.dumps(extra)
            self._pending["turns"].append(row)
            self._counts["turns"] += 1
        else:
            if et == "cycle_start":
                self._counts["cycles"] += 1
            row = event_row_from_event(
                d, run_id=self.run_id, seq=seq, event_id=row_id, ts_ms=ts_ms
            )
            self._pending["events"].append(row)
            self._counts["events"] += 1
        return d

    def wants_flush(self, event: Any) -> bool:
        """Whether ingesting this event should be followed by a flush:
        turns always (durability), and the unit-of-work closers."""
        et = getattr(event, "event_type", None) or (
            event.get("event_type") if isinstance(event, dict) else ""
        )
        return et == "inference_call" or et in _FLUSH_ON

    async def record(self, event: Any) -> dict:
        """ingest + flush when the event calls for it."""
        d = self.ingest(event)
        if self.wants_flush(event):
            await self.flush(
                "turn" if d.get("event_type") == "inference_call" else "step"
            )
        return d

    def add_commit(self, row: dict) -> None:
        """Book a commits row (the snapshotter's result, or an import)."""
        row = dict(row)
        row.setdefault("run_id", self.run_id)
        row.setdefault("seq", self.next_seq())
        row.setdefault("ts_ms", _now_ms())
        row.setdefault("mission_id", self.mission_id)
        row.setdefault("schema_version", SCHEMA_VERSION)
        self._pending["commits"].append(row)
        self._counts["commits"] += 1

    # ── flush / compaction ────────────────────────────────────────────

    def _part_dir(self, table: str) -> str:
        return os.path.join(self.history_dir, table, f"run={self.run_id}")

    def _take_pending(self) -> dict[str, list[dict]]:
        taken = {k: v for k, v in self._pending.items() if v}
        for k in taken:
            self._pending[k] = []
        return taken

    def _write_parts(self, taken: dict[str, list[dict]]) -> int:
        n = 0
        for table, rows in taken.items():
            schema = TABLES[table]
            coerced = [coerce_row(r, schema) for r in rows]
            tbl = pa.Table.from_pylist(coerced, schema=schema)
            first, last = rows[0]["seq"], rows[-1]["seq"]
            name = f"part-{first:08d}-{last:08d}-{uuid.uuid4().hex[:6]}.parquet"
            _write_parquet_atomic(tbl, os.path.join(self._part_dir(table), name))
            n += len(rows)
        return n

    async def flush(self, reason: str = "") -> int:
        """Write every buffered row as part files. Returns rows written."""
        taken = self._take_pending()
        if not taken:
            return 0
        async with self._io_lock:
            try:
                n = await asyncio.to_thread(self._write_parts, taken)
            except Exception:
                # Never lose rows to an I/O hiccup: put them back for the
                # next flush and let the run continue.
                for k, rows in taken.items():
                    self._pending[k] = rows + self._pending[k]
                logger.warning(
                    "history flush (%s) failed; rows kept", reason, exc_info=True
                )
                return 0
        logger.debug("history flush (%s): %d row(s)", reason, n)
        return n

    def _compact_table(self, table: str, level: str, cycle: Optional[int]) -> None:
        d = self._part_dir(table)
        if not os.path.isdir(d):
            return
        if level == "cycle":
            inputs = sorted(
                glob.glob(os.path.join(d, "part-*.parquet")), key=_seq_range
            )
            prefix = (
                f"cycle-{(cycle if cycle is not None else self._counts['cycles']):06d}"
            )
        else:
            inputs = sorted(
                glob.glob(os.path.join(d, "part-*.parquet"))
                + glob.glob(os.path.join(d, "cycle-*.parquet")),
                key=_seq_range,
            )
            prefix = "run"
        if len(inputs) < 2:
            return
        first = _seq_range(inputs[0])[0]
        last = _seq_range(inputs[-1])[1]
        out = os.path.join(d, f"{prefix}-{first:08d}-{last:08d}.parquet")
        if os.path.exists(out):  # a previous compaction with the same range
            out = os.path.join(
                d, f"{prefix}-{first:08d}-{last:08d}-{uuid.uuid4().hex[:4]}.parquet"
            )
        merge_parquet_files(inputs, out, TABLES[table])

    async def compact(self, level: str = "cycle", cycle: Optional[int] = None) -> None:
        """Merge a run partition's files: ``cycle`` folds the part files
        written since the last compaction; ``run`` folds everything."""
        async with self._io_lock:
            for table in ("turns", "events", "commits"):
                try:
                    await asyncio.to_thread(self._compact_table, table, level, cycle)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "history compaction (%s/%s) failed", table, level, exc_info=True
                    )

    # ── snapshots ─────────────────────────────────────────────────────

    async def checkpoint(
        self,
        source: str,
        trigger: str = "",
        ctx: Optional[dict] = None,
        *,
        force: bool = False,
    ) -> Optional[str]:
        """Snapshot the workspace tree; book a commits row if it changed
        (or always, with ``force`` — a marker commit). Returns the new commit
        sha, or None (no snapshotter / no change)."""
        if self._snapshotter is None:
            return None
        ctx = ctx or {}
        seq = self.next_seq()
        message = (
            f"{source} {self.mission_id} run={self.run_id} seq={seq} "
            f"cycle={ctx.get('cycle', '')} flow={ctx.get('flow', '')} "
            f"step={ctx.get('step', '')} branch={ctx.get('branch', '')}\n\n{trigger}"
        )
        async with self._io_lock:
            try:
                res: Optional[SnapshotResult] = await asyncio.to_thread(
                    self._snapshotter.snapshot,
                    message=message,
                    source=source,
                    force=force,
                )
            except Exception:  # noqa: BLE001
                logger.warning("history checkpoint (%s) failed", source, exc_info=True)
                return None
        if res is None:
            return None
        add, mod, rm = res.counts
        self.add_commit(
            {
                "commit_sha": res.commit_sha,
                "parent_sha": res.parent_sha,
                "tree_sha": res.tree_sha,
                "seq": seq,
                "branch": ctx.get("branch", ""),
                "cycle": ctx.get("cycle"),
                "flow": ctx.get("flow", ""),
                "step": ctx.get("step", ""),
                "source": source,
                "trigger": trigger,
                "workspace_changed": res.workspace_changed,
                "files_added": add,
                "files_modified": mod,
                "files_deleted": rm,
                "changes_json": json.dumps(res.changes, default=str),
                "large_files_json": (
                    json.dumps(res.large_files, default=str)
                    if res.large_files
                    else None
                ),
                "files_scanned": res.files_scanned,
                "bytes_hashed": res.bytes_hashed,
                "scan_ms": res.scan_ms,
            }
        )
        await self.flush("checkpoint")
        return res.commit_sha

    # ── summary / runs row / close ────────────────────────────────────

    def summary_path(self) -> str:
        return os.path.join(self.history_dir, "runs", f"{self.run_id}.summary.json")

    def write_summary(self, record: dict) -> None:
        """The ledger head, rewritten at every flush (replaces
        ``<trace>.summary.json``)."""
        try:
            _atomic_json(self.summary_path(), record)
        except Exception:  # noqa: BLE001
            logger.debug("history summary write skipped", exc_info=True)

    def _runs_row(self, final_status: str, summary: Optional[dict]) -> dict:
        return {
            "run_id": self.run_id,
            "mission_id": self.mission_id,
            "started_at": self.started_at,
            "ended_at": _iso_now() if final_status else None,
            "history_mode": self.mode,
            "agent_sha": self._meta.get("agent_sha", ""),
            "endpoint": self._meta.get("endpoint", ""),
            "flow_set": self._meta.get("flow_set", ""),
            "entry_flow": self._meta.get("entry_flow", ""),
            "cycles": self._counts["cycles"],
            "turns": self._counts["turns"],
            "commits": self._counts["commits"],
            "head_sha": self.head(),
            "final_status": final_status,
            "summary_json": json.dumps(summary, default=str) if summary else None,
            "schema_version": SCHEMA_VERSION,
        }

    def _write_runs_row(self, final_status: str, summary: Optional[dict]) -> None:
        row = coerce_row(self._runs_row(final_status, summary), RUNS_SCHEMA)
        tbl = pa.Table.from_pylist([row], schema=RUNS_SCHEMA)
        try:
            _write_parquet_atomic(
                tbl,
                os.path.join(self.history_dir, "runs", f"run-{self.run_id}.parquet"),
            )
        except Exception:  # noqa: BLE001
            logger.warning("history runs row write failed", exc_info=True)

    async def close(
        self, final_status: str = "ended", summary: Optional[dict] = None
    ) -> None:
        """Flush, compact the run, write the final runs row, release LOCK.

        Deliberately does ALL of it synchronously, with no await between the
        steps: this runs inside the teardown drain, where a cancellation from
        a closing cancel scope can land at any await. The first live run
        (2026-09-24) lost exactly that race — its turns were run-compacted,
        its events were not, and its runs row never closed. A blocking close
        at teardown costs the loop a few hundred ms and cannot be cut in half.
        """
        self.close_sync(final_status, summary)

    def close_sync(
        self, final_status: str = "ended", summary: Optional[dict] = None
    ) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            taken = self._take_pending()
            if taken:
                try:
                    self._write_parts(taken)
                except Exception:  # noqa: BLE001
                    logger.warning("history close: final flush failed", exc_info=True)
            for table in ("turns", "events", "commits"):
                try:
                    self._compact_table(table, "run", None)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "history close: compaction (%s) failed", table, exc_info=True
                    )
            if summary is not None:
                self.write_summary(
                    {
                        "event_type": "run_summary",
                        "mission_id": self.mission_id,
                        "run_id": self.run_id,
                        "total_wall_ms": summary.get("total_wall_ms"),
                        "summary": summary,
                    }
                )
            if self._write_runs:
                self._write_runs_row(final_status, summary)
            close = getattr(self._snapshotter, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    logger.debug("snapshotter close skipped", exc_info=True)
        finally:
            if self._lock_fd is not None:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                    os.close(self._lock_fd)
                finally:
                    self._lock_fd = None


__all__ = [
    "HISTORY_DIR",
    "HISTORY_MODES",
    "HistoryLocked",
    "HistoryMode",
    "HistoryStore",
    "SnapshotResult",
    "cli_lock",
    "history_dir",
    "is_locked",
    "merge_parquet_files",
    "new_run_id",
    "COMMITS_SCHEMA",
    "EVENTS_SCHEMA",
    "RUNS_SCHEMA",
    "TURNS_SCHEMA",
]
