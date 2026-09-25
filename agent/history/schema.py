"""Column contracts for the history store — the one place the tables are defined.

Column names are the trace dataclass field names (``agent/trace.py``) wherever a
column carries a trace field, so a row read back is the same flat dict a trace
event produced and ``fold_event`` / ``trace_cli`` need no translation layer.
Every part file is written with its table's FULL schema (nulls for what a row
lacks), so a dataset never has to unify schemas across files, and
``schema_version`` on every row says which contract wrote it.

List- and dict-valued trace fields ride in a JSON column (``payload_json`` /
``extra_json``): they are never queried columnarly, and keeping them opaque
means a new trace field needs no schema change to be recorded.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

SCHEMA_VERSION = 1

# Columns the STORE owns on every table. A trace event field with one of these
# names (CapacitySample has its own ``seq``) is preserved in the JSON column
# and restored by the reader; the store's value is reported under ``_history``.
STORE_META_COLUMNS: tuple[str, ...] = ("run_id", "seq", "ts_ms", "schema_version")

# The columns ``history: metrics`` blanks.
CONTENT_COLUMNS: tuple[str, ...] = (
    "prompt_content",
    "prompt_static",
    "prompt_dynamic",
    "response_content",
    "thinking_content",
)

_META_FIELDS = [
    pa.field("run_id", pa.string()),
    pa.field("seq", pa.int64()),
    pa.field("ts_ms", pa.int64()),
]
_VERSION_FIELD = pa.field("schema_version", pa.int32())
_BASE_FIELDS = [
    pa.field("wall_time", pa.string()),
    pa.field("timestamp", pa.float64()),
    pa.field("mission_id", pa.string()),
    pa.field("branch", pa.string()),
    pa.field("cycle", pa.int32()),
    pa.field("flow", pa.string()),
    pa.field("step", pa.string()),
]

TURNS_SCHEMA = pa.schema(
    [
        pa.field("turn_id", pa.string()),
        pa.field("event_id", pa.string()),
        *_META_FIELDS,
        *_BASE_FIELDS,
        pa.field("step_attempt", pa.int32()),
        pa.field("call_attempt", pa.int32()),
        pa.field("goal_id", pa.string()),
        pa.field("flow_directive", pa.string()),
        pa.field("purpose", pa.string()),
        # Identity the server returns.
        pa.field("request_id", pa.string()),
        pa.field("session_id", pa.string()),
        pa.field("session_turn_id", pa.int64()),
        pa.field("turn_committed", pa.bool_()),
        pa.field("end_reason", pa.string()),
        pa.field("error", pa.string()),
        pa.field("degenerate", pa.bool_()),
        pa.field("degenerate_reason", pa.string()),
        pa.field("degenerate_tokens", pa.int64()),
        pa.field("finished", pa.bool_()),
        pa.field("truncated", pa.bool_()),
        pa.field("cache_hit", pa.bool_()),
        # Tokens.
        pa.field("tokens_in", pa.int64()),
        pa.field("tokens_out", pa.int64()),
        pa.field("prompt_tokens", pa.int64()),
        pa.field("cached_prefix_tokens", pa.int64()),
        pa.field("fresh_prefill_tokens", pa.int64()),
        pa.field("generated_tokens", pa.int64()),
        pa.field("reasoning_tokens", pa.int64()),
        # Timings.
        pa.field("wall_ms", pa.float64()),
        pa.field("prefill_ms", pa.float64()),
        pa.field("decode_ms", pa.float64()),
        pa.field("prompt_render_ms", pa.float64()),
        pa.field("injection_ms", pa.float64()),
        pa.field("pre_compute_ms", pa.float64()),
        # Request configuration.
        pa.field("temperature", pa.float64()),
        pa.field("max_tokens", pa.int64()),
        pa.field("reasoning", pa.string()),
        pa.field("model", pa.string()),
        pa.field("endpoint", pa.string()),
        pa.field("domain", pa.string()),
        pa.field("flow_key", pa.string()),
        pa.field("static_prefix_hash", pa.string()),
        # Content — the point of the table.
        pa.field("prompt_content", pa.string()),
        pa.field("prompt_static", pa.string()),
        pa.field("prompt_dynamic", pa.string()),
        pa.field("response_content", pa.string()),
        pa.field("thinking_content", pa.string()),
        pa.field("content_dropped", pa.bool_()),
        # Workspace state when the call started; tree_after is derived.
        pa.field("tree_before", pa.string()),
        pa.field("replay_of", pa.string()),
        pa.field("extra_json", pa.string()),
        _VERSION_FIELD,
    ]
)

EVENTS_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string()),
        *_META_FIELDS,
        pa.field("event_type", pa.string()),
        *_BASE_FIELDS,
        pa.field("action", pa.string()),
        pa.field("action_type", pa.string()),
        pa.field("child_flow", pa.string()),
        pa.field("return_status", pa.string()),
        pa.field("session_id", pa.string()),
        pa.field("resolver_type", pa.string()),
        pa.field("resolver_decision", pa.string()),
        pa.field("outcome", pa.string()),
        pa.field("target_flow", pa.string()),
        pa.field("status", pa.string()),
        pa.field("command", pa.string()),
        pa.field("tool", pa.string()),
        pa.field("server", pa.string()),
        pa.field("category", pa.string()),
        pa.field("source_flow", pa.string()),
        pa.field("error", pa.string()),
        pa.field("return_code", pa.int32()),
        pa.field("timed_out", pa.bool_()),
        pa.field("success", pa.bool_()),
        pa.field("wall_ms", pa.float64()),
        pa.field("span_ms", pa.float64()),
        pa.field("step_duration_ms", pa.float64()),
        pa.field("cycle_duration_ms", pa.float64()),
        pa.field("projection_ms", pa.float64()),
        pa.field("tail_resolution_ms", pa.float64()),
        pa.field("input_build_ms", pa.float64()),
        pa.field("resolver_ms", pa.float64()),
        pa.field("child_duration_ms", pa.float64()),
        pa.field("payload_json", pa.string()),
        _VERSION_FIELD,
    ]
)

COMMITS_SCHEMA = pa.schema(
    [
        pa.field("commit_sha", pa.string()),
        pa.field("parent_sha", pa.string()),
        pa.field("tree_sha", pa.string()),
        *_META_FIELDS,
        pa.field("mission_id", pa.string()),
        pa.field("branch", pa.string()),
        pa.field("cycle", pa.int32()),
        pa.field("flow", pa.string()),
        pa.field("step", pa.string()),
        pa.field("source", pa.string()),
        pa.field("trigger", pa.string()),
        pa.field("workspace_changed", pa.bool_()),
        pa.field("files_added", pa.int32()),
        pa.field("files_modified", pa.int32()),
        pa.field("files_deleted", pa.int32()),
        pa.field("changes_json", pa.string()),
        pa.field("large_files_json", pa.string()),
        pa.field("files_scanned", pa.int32()),
        pa.field("bytes_hashed", pa.int64()),
        pa.field("scan_ms", pa.float64()),
        _VERSION_FIELD,
    ]
)

RUNS_SCHEMA = pa.schema(
    [
        pa.field("run_id", pa.string()),
        pa.field("mission_id", pa.string()),
        pa.field("started_at", pa.string()),
        pa.field("ended_at", pa.string()),
        pa.field("history_mode", pa.string()),
        pa.field("agent_sha", pa.string()),
        pa.field("endpoint", pa.string()),
        pa.field("flow_set", pa.string()),
        pa.field("entry_flow", pa.string()),
        pa.field("cycles", pa.int32()),
        pa.field("turns", pa.int32()),
        pa.field("commits", pa.int32()),
        pa.field("head_sha", pa.string()),
        pa.field("final_status", pa.string()),
        pa.field("summary_json", pa.string()),
        _VERSION_FIELD,
    ]
)

TABLES: dict[str, pa.Schema] = {
    "turns": TURNS_SCHEMA,
    "events": EVENTS_SCHEMA,
    "commits": COMMITS_SCHEMA,
    "runs": RUNS_SCHEMA,
}
PRIMARY_KEY: dict[str, str] = {
    "turns": "turn_id",
    "events": "event_id",
    "commits": "commit_sha",
    "runs": "run_id",
}

_EVENT_TYPED = frozenset(
    n
    for n in EVENTS_SCHEMA.names
    if n not in ("payload_json", "event_id", *STORE_META_COLUMNS)
)
_TURN_TYPED = frozenset(
    n
    for n in TURNS_SCHEMA.names
    if n
    not in ("extra_json", "turn_id", "event_id", "content_dropped", *STORE_META_COLUMNS)
)


# ── Coercion ──────────────────────────────────────────────────────────


def _coerce(value: Any, typ: pa.DataType) -> Any:
    """Bend a Python value to a column type; None when it cannot be told."""
    if value is None:
        return None
    if pa.types.is_boolean(typ):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes")
        return None
    if pa.types.is_integer(typ):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                return None
        return None
    if pa.types.is_floating(typ):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None
    if pa.types.is_string(typ) or pa.types.is_large_string(typ):
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, default=str)
        return str(value)
    return value


def coerce_row(row: dict, schema: pa.Schema) -> dict:
    """A row holding exactly the schema's columns, values coerced, unknown
    keys dropped. ``schema_version`` is stamped when absent."""
    out: dict[str, Any] = {}
    for f in schema:
        out[f.name] = _coerce(row.get(f.name), f.type)
    if out.get("schema_version") is None and "schema_version" in schema.names:
        out["schema_version"] = SCHEMA_VERSION
    return out


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


# ── Trace event → row ─────────────────────────────────────────────────


def event_row_from_event(
    event: dict, *, run_id: str, seq: int, event_id: str, ts_ms: int
) -> dict:
    """A non-inference trace event dict as an ``events`` row.

    Typed columns take the event's non-None scalar fields; everything else —
    lists, dicts, None-valued fields (kept as JSON null so the read-back dict
    has the same keys as the original) and any field colliding with a
    store-owned column — goes to ``payload_json``.
    """
    row: dict[str, Any] = {
        "event_id": event_id,
        "run_id": run_id,
        "seq": seq,
        "ts_ms": ts_ms,
        "schema_version": SCHEMA_VERSION,
    }
    payload: dict[str, Any] = {}
    for k, v in event.items():
        if (
            k in _EVENT_TYPED
            and v is not None
            and not isinstance(v, (dict, list, tuple))
        ):
            row[k] = v
        else:
            payload[k] = v
    row["payload_json"] = _json(payload)
    return row


def turn_row_from_event(
    event: dict,
    *,
    run_id: str,
    seq: int,
    turn_id: str,
    ts_ms: int,
    tree_before: str,
    drop_content: bool,
) -> dict:
    """An ``inference_call`` trace event dict as a ``turns`` row."""
    row: dict[str, Any] = {
        "turn_id": turn_id,
        "event_id": turn_id,
        "run_id": run_id,
        "seq": seq,
        "ts_ms": ts_ms,
        "tree_before": tree_before,
        "content_dropped": bool(drop_content),
        "schema_version": SCHEMA_VERSION,
    }
    extra: dict[str, Any] = {}
    for k, v in event.items():
        if k == "event_type":
            continue
        if k in _TURN_TYPED and not isinstance(v, (dict, list, tuple)):
            row[k] = v
        else:
            extra[k] = v
    if drop_content:
        for c in CONTENT_COLUMNS:
            row[c] = None
    row["extra_json"] = _json(extra) if extra else None
    return row


# ── Row → trace event dict ────────────────────────────────────────────


def _history_meta(row: dict, id_key: str) -> dict:
    return {
        "run_id": row.get("run_id"),
        "seq": row.get("seq"),
        "event_id": row.get("event_id"),
        id_key: row.get(id_key),
        "ts_ms": row.get("ts_ms"),
    }


def event_dict_from_row(row: dict) -> dict:
    """The old JSONL line shape back from an ``events`` row, plus
    ``_history`` (store metadata) so nothing the store knows is lost."""
    out: dict[str, Any] = {
        k: v for k, v in row.items() if k in _EVENT_TYPED and v is not None
    }
    payload = row.get("payload_json")
    if payload:
        out.update(json.loads(payload))
    out["_history"] = _history_meta(row, "event_id")
    return out


def turn_dict_from_row(row: dict) -> dict:
    """A ``turns`` row as an ``inference_call`` event dict (old shape plus
    the new identity/metric fields), plus ``_history``."""
    out: dict[str, Any] = {"event_type": "inference_call"}
    for k, v in row.items():
        if k in STORE_META_COLUMNS or k in ("event_id", "extra_json"):
            continue
        if v is not None:
            out[k] = v
    extra = row.get("extra_json")
    if extra:
        out.update(json.loads(extra))
    out["_history"] = _history_meta(row, "turn_id")
    return out
