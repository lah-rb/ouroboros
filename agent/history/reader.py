"""Read side of the history store — pure functions over an ``.agent`` dir.

Returns plain dicts in the shape the old JSONL trace produced (plus a
``_history`` entry carrying store metadata), so the finite-time ledger,
``trace_cli`` and the dev scripts consume it unchanged. pyarrow only — no
pandas — and no effects, so a CLI, a test or a notebook can call it.

Every reader DEDUPES on the table's primary key: compaction renames the merged
file in before unlinking its inputs, so a crash in between leaves rows twice.
"""

from __future__ import annotations

import glob
import json
import os
from typing import Iterator, Optional

import pyarrow as pa
import pyarrow.dataset as ds

from agent.history.schema import (
    PRIMARY_KEY,
    RUNS_SCHEMA,
    TABLES,
    TURNS_SCHEMA,
    event_dict_from_row,
    turn_dict_from_row,
)
from agent.history.store import HISTORY_DIR


def _history_dir(agent_dir: str) -> str:
    return os.path.join(agent_dir, HISTORY_DIR)


def has_history(agent_dir: str) -> bool:
    return os.path.isdir(_history_dir(agent_dir))


def _run_dirs(agent_dir: str, table: str) -> list[tuple[str, str]]:
    """(run_id, dir) for every run partition of a table."""
    base = os.path.join(_history_dir(agent_dir), table)
    out = []
    for d in sorted(glob.glob(os.path.join(base, "run=*"))):
        if os.path.isdir(d):
            out.append((os.path.basename(d)[len("run=") :], d))
    return out


def _files(agent_dir: str, table: str, run_id: Optional[str]) -> list[str]:
    files: list[str] = []
    for rid, d in _run_dirs(agent_dir, table):
        if run_id is not None and rid != run_id:
            continue
        files.extend(sorted(glob.glob(os.path.join(d, "*.parquet"))))
    return files


def _read_table(files: list[str], schema: pa.Schema) -> pa.Table:
    """All rows of ``files`` under one schema (missing columns → null).
    Retries once if compaction removed a file between listing and read."""
    if not files:
        return schema.empty_table()
    try:
        return ds.dataset(files, format="parquet", schema=schema).to_table()
    except FileNotFoundError:
        alive = [f for f in files if os.path.exists(f)]
        if not alive:
            return schema.empty_table()
        return ds.dataset(alive, format="parquet", schema=schema).to_table()


def _dedupe(rows: list[dict], key: str) -> list[dict]:
    seen: set = set()
    out = []
    for r in rows:
        k = r.get(key)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _table_rows(agent_dir: str, table: str, run_id: Optional[str] = None) -> list[dict]:
    tbl = _read_table(_files(agent_dir, table, run_id), TABLES[table])
    return _dedupe(tbl.to_pylist(), PRIMARY_KEY[table])


# ── runs ──────────────────────────────────────────────────────────────


def list_runs(agent_dir: str) -> list[dict]:
    """Every run, oldest first. A run whose runs row is missing (crash before
    the first write) is listed from its partition directory alone."""
    files = sorted(
        glob.glob(os.path.join(_history_dir(agent_dir), "runs", "run-*.parquet"))
    )
    rows = (
        _dedupe(_read_table(files, RUNS_SCHEMA).to_pylist(), "run_id") if files else []
    )
    known = {r["run_id"] for r in rows}
    for table in ("turns", "events", "commits"):
        for rid, _ in _run_dirs(agent_dir, table):
            if rid not in known:
                rows.append({"run_id": rid, "started_at": None, "final_status": None})
                known.add(rid)
    # run_ids begin with a UTC timestamp, so they sort chronologically when the
    # started_at column is missing.
    rows.sort(key=lambda r: (r.get("started_at") or "", r["run_id"]))
    return rows


def latest_run_id(agent_dir: str) -> Optional[str]:
    runs = list_runs(agent_dir)
    return runs[-1]["run_id"] if runs else None


def _run_order(agent_dir: str) -> dict[str, int]:
    return {r["run_id"]: i for i, r in enumerate(list_runs(agent_dir))}


def _sort_key(order: dict[str, int]):
    def key(d: dict) -> tuple:
        h = d.get("_history") or d
        return (order.get(h.get("run_id"), 10**9), h.get("seq") or 0)

    return key


# ── turns / events / commits ──────────────────────────────────────────


def load_commits(agent_dir: str, run_id: Optional[str] = None) -> list[dict]:
    rows = _table_rows(agent_dir, "commits", run_id)
    order = _run_order(agent_dir)
    rows.sort(key=lambda r: (order.get(r.get("run_id"), 10**9), r.get("seq") or 0))
    return rows


def _head_by_run(agent_dir: str) -> dict[str, str]:
    return {r["run_id"]: (r.get("head_sha") or "") for r in list_runs(agent_dir)}


def load_turns(
    agent_dir: str, run_id: Optional[str] = None, *, with_content: bool = True
) -> list[dict]:
    """Turn rows (flat, all columns), sorted by (run, seq), with
    ``tree_after`` derived: the first commit after the turn in its run, else
    the run's head."""
    rows = _table_rows(agent_dir, "turns", run_id)
    commits = load_commits(agent_dir, run_id)
    heads = _head_by_run(agent_dir)
    order = _run_order(agent_dir)
    by_run: dict[str, list[tuple[int, str]]] = {}
    for c in commits:
        by_run.setdefault(c["run_id"], []).append((c.get("seq") or 0, c["commit_sha"]))
    for v in by_run.values():
        v.sort()
    out = []
    for r in rows:
        d = turn_dict_from_row(r)
        after = ""
        for seq, sha in by_run.get(r["run_id"], []):
            if seq > (r.get("seq") or 0):
                after = sha
                break
        d["tree_after"] = after or heads.get(r["run_id"], "")
        if not with_content:
            for c in (
                "prompt_content",
                "prompt_static",
                "prompt_dynamic",
                "response_content",
                "thinking_content",
            ):
                d.pop(c, None)
        out.append(d)
    out.sort(key=_sort_key(order))
    return out


def load_events(
    agent_dir: str, run_id: Optional[str] = None, *, include_turns: bool = True
) -> list[dict]:
    """Every event as the old JSONL dict shape, interleaved by (run, seq).
    Turns appear as ``event_type == "inference_call"`` dicts."""
    events = [event_dict_from_row(r) for r in _table_rows(agent_dir, "events", run_id)]
    if include_turns:
        events.extend(load_turns(agent_dir, run_id))
    events.sort(key=_sort_key(_run_order(agent_dir)))
    return events


def iter_events(agent_dir: str, run_id: Optional[str] = None) -> Iterator[dict]:
    yield from load_events(agent_dir, run_id)


def iter_turns(agent_dir: str, run_id: Optional[str] = None) -> Iterator[dict]:
    yield from load_turns(agent_dir, run_id)


def turns_table(agent_dir: str, run_id: Optional[str] = None) -> pa.Table:
    """The raw turns table for analysts (pyarrow)."""
    return _read_table(_files(agent_dir, "turns", run_id), TURNS_SCHEMA)


def get_turn(agent_dir: str, ref: str) -> Optional[dict]:
    """A turn by ``turn_id``, ``<run_id>:<seq>``, or a bare ``seq`` in the
    latest run."""
    ref = str(ref).strip()
    turns = load_turns(agent_dir)
    for t in turns:
        if t.get("turn_id") == ref:
            return t
    if ":" in ref:
        rid, _, s = ref.rpartition(":")
        if s.isdigit():
            for t in turns:
                h = t["_history"]
                if h["run_id"] == rid and h["seq"] == int(s):
                    return t
        return None
    if ref.isdigit():
        latest = latest_run_id(agent_dir)
        for t in turns:
            h = t["_history"]
            if h["run_id"] == latest and h["seq"] == int(ref):
                return t
    return None


# ── summary ───────────────────────────────────────────────────────────


def load_summary(agent_dir: str, run_id: Optional[str] = None) -> Optional[dict]:
    """The finite-time summary for a run: the runs row's ``summary_json``,
    else the ``runs/<id>.summary.json`` head, else recomputed from events.
    Returns the ``RunSummary``-shaped record (``summary`` key holds the
    ledger output) or None when the run has nothing."""
    from agent.trace import summarize_events

    rid = run_id or latest_run_id(agent_dir)
    if rid is None:
        return None
    for r in list_runs(agent_dir):
        if r["run_id"] == rid and r.get("summary_json"):
            summary = json.loads(r["summary_json"])
            return {
                "event_type": "run_summary",
                "mission_id": r.get("mission_id", ""),
                "run_id": rid,
                "total_wall_ms": summary.get("total_wall_ms"),
                "summary": summary,
            }
    head = os.path.join(_history_dir(agent_dir), "runs", f"{rid}.summary.json")
    if os.path.isfile(head):
        with open(head, encoding="utf-8") as f:
            return json.load(f)
    events = load_events(agent_dir, rid)
    if not events:
        return None
    summary = summarize_events(events)
    return {
        "event_type": "run_summary",
        "mission_id": next(
            (e.get("mission_id", "") for e in events if e.get("mission_id")), ""
        ),
        "run_id": rid,
        "total_wall_ms": summary.get("total_wall_ms"),
        "summary": summary,
    }


def token_totals(agent_dir: str) -> tuple[int, int]:
    """(tokens_in, tokens_out) summed over every turn of every run — the
    figure the terminal-bench adapter reports per task."""
    tin = tout = 0
    for t in load_turns(agent_dir, with_content=False):
        tin += int(t.get("tokens_in") or 0)
        tout += int(t.get("tokens_out") or 0)
    return tin, tout


def find_agent_dirs(root_glob: str) -> list[str]:
    """``.agent`` directories with a history store under a glob of
    workspaces (``~/ouroboros-runs/tier_work/*``)."""
    out = []
    for w in sorted(glob.glob(os.path.expanduser(root_glob))):
        a = os.path.join(w, ".agent")
        if has_history(a):
            out.append(a)
    return out


__all__ = [
    "find_agent_dirs",
    "legacy_jsonl_events",
    "legacy_trace_files",
    "load_events_any",
    "load_summary_any",
    "get_turn",
    "has_history",
    "iter_events",
    "iter_turns",
    "latest_run_id",
    "list_runs",
    "load_commits",
    "load_events",
    "load_summary",
    "load_turns",
    "token_totals",
    "turns_table",
]


# ── Legacy JSONL (retired .agent/traces) ──────────────────────────────
#
# Finished runs on disk still carry the old trace. ``history import-traces``
# loads them into the store; until an archive has been imported, these give
# dev tooling the same dicts straight from the JSONL.


def legacy_trace_files(agent_dir: str) -> list[str]:
    files = glob.glob(os.path.join(agent_dir, "traces", "*.jsonl"))
    files.sort(key=lambda p: os.path.getmtime(p))
    return files


def legacy_jsonl_events(agent_dir: str, newest_only: bool = True) -> list[dict]:
    """Events from ``.agent/traces/*.jsonl`` (the newest file, or all)."""
    files = legacy_trace_files(agent_dir)
    if newest_only and files:
        files = files[-1:]
    out: list[dict] = []
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def load_events_any(agent_dir: str, run_id: Optional[str] = None) -> list[dict]:
    """The store's events when the workspace has a history, else the newest
    legacy JSONL trace. For tooling that must read archives of both eras."""
    if has_history(agent_dir):
        return load_events(agent_dir, run_id)
    return legacy_jsonl_events(agent_dir)


def load_summary_any(agent_dir: str, run_id: Optional[str] = None) -> Optional[dict]:
    """The recorded summary head (store, else the newest legacy
    ``*.summary.json``), as the ``RunSummary`` record shape."""
    if has_history(agent_dir):
        return load_summary(agent_dir, run_id)
    heads = sorted(glob.glob(os.path.join(agent_dir, "traces", "*.summary.json")))
    if not heads:
        return None
    with open(heads[-1], encoding="utf-8") as f:
        return json.load(f)
