"""Load a retired ``.agent/traces/*.jsonl`` trace into the history store.

The JSONL trace is no longer written, but finished runs on disk still carry
one. Each file becomes one run (``run_id = "import-<timestamp>"`` from the
file name), its lines become turns and events in line order, and the
companion ``<trace>.summary.json`` becomes the run's recorded summary.
Idempotent: a run id already present is skipped.
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

from agent.history import reader
from agent.history.store import HistoryStore, cli_lock

_TS = re.compile(r"_(\d{8}T\d{6})\.jsonl$")


def _run_id_for(path: str) -> str:
    m = _TS.search(os.path.basename(path))
    if m:
        return f"import-{m.group(1)}"
    stamp = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).strftime(
        "%Y%m%dT%H%M%S"
    )
    return f"import-{stamp}"


def _started_at(run_id: str) -> Optional[str]:
    try:
        return (
            datetime.strptime(run_id[len("import-") :], "%Y%m%dT%H%M%S")
            .replace(tzinfo=timezone.utc)
            .isoformat()
        )
    except ValueError:
        return None


def _summary_for(path: str) -> Optional[dict]:
    if not path.endswith(".jsonl"):
        return None
    head = path[:-6] + ".summary.json"
    if not os.path.isfile(head):
        return None
    try:
        with open(head, encoding="utf-8") as f:
            record = json.load(f)
        return record.get("summary") or None
    except Exception:  # noqa: BLE001
        return None


def import_trace_file(working_dir: str, path: str, *, mission_id: str = "") -> dict:
    """Import one JSONL trace. Returns a report dict (run_id, turns, events,
    skipped). Caller holds the CLI lock."""
    import asyncio

    agent_dir = os.path.join(working_dir, ".agent")
    run_id = _run_id_for(path)
    if any(r["run_id"] == run_id for r in reader.list_runs(agent_dir)):
        return {
            "run_id": run_id,
            "turns": 0,
            "events": 0,
            "skipped": True,
            "path": path,
        }
    lines: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    mid = mission_id or next(
        (e.get("mission_id") for e in lines if e.get("mission_id")), ""
    )
    store = HistoryStore(
        working_dir,
        mid or "unknown",
        "full",
        run_id=run_id,
        lock=False,
        meta={"entry_flow": "", "flow_set": ""},
        started_at=_started_at(run_id),
    )
    turns = events = 0
    for e in lines:
        et = e.get("event_type", "")
        if et == "run_summary":
            continue  # the head is written from the companion file below
        store.ingest(e)
        if et == "inference_call":
            turns += 1
        else:
            events += 1
    summary = _summary_for(path)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(store.close("imported", summary))
    finally:
        loop.close()
    return {
        "run_id": run_id,
        "turns": turns,
        "events": events,
        "skipped": False,
        "path": path,
    }


def import_traces(working_dir: str, paths: Optional[list[str]] = None) -> list[dict]:
    """Import every JSONL trace under ``<working_dir>/.agent/traces`` (or the
    given paths) into the workspace's history store."""
    if not paths:
        paths = sorted(
            glob.glob(os.path.join(working_dir, ".agent", "traces", "*.jsonl"))
        )
    reports = []
    with cli_lock(working_dir):
        for p in paths:
            reports.append(import_trace_file(working_dir, p))
    return reports
