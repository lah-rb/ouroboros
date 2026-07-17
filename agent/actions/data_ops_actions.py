"""Actions for the ``data_patch`` sub-flow — surgical YAML edits via ``data_ops``.

When ``file_ops`` sees a YAML data file (no AST symbols), it routes here instead
of a full-file rewrite. ``translate_data_ops_turn`` turns the prose
``change_spec`` + the file's data shape + its current content into structured
``DataOp``s and dry-runs them; ``apply_data_ops`` writes the patched text. Any
miss (non-YAML, unparseable, no valid ops, patch won't re-apply, write fails)
publishes ``full_rewrite_requested`` so the parent falls back to today's rewrite
— the path is strictly additive.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.data_ops import DataOp, Fmt, detect_fmt, patch_text
from agent.llm_json import parse_llm_json
from agent.models import StepInput, StepOutput
from agent.schema_extract import extract_data_skeleton

logger = logging.getLogger(__name__)

_MAX_INLINE = 8000  # include the whole current file when under this; else skeleton-only

DATA_OPS_PROMPT = """\
You are editing a {fmt} data file with a SURGICAL patch — emit a minimal set of
path-scoped operations, NOT a rewritten file.

## The fix to apply
{change_spec}

## Shape of the data file
{skeleton}

## Current content of {path}
{current}

## How to address a path (RFC-6901 JSON Pointer)
- /rooms/0/exits/north        keys and list indices, separated by "/"
- /rooms/-                    "-" appends to a list
- /rooms/[id=bedroom]/exits   select a list item by a child field (prefer this over indices)
- escape "/" inside a key as "~1" and "~" as "~0"

## Operations (each targets exactly one path)
- {{"op": "set",    "path": "<ptr>", "value": <json>}}   replace or create a value
- {{"op": "add",    "path": "<ptr>", "value": <json>}}   insert into a list / upsert a key
- {{"op": "remove", "path": "<ptr>"}}                     delete a key or list item
- {{"op": "move",   "from": "<ptr>", "path": "<ptr>"}}    relocate a subtree

Values are real JSON (true/false/null, numbers, objects, lists — not strings).

Return ONLY this JSON object:
{{"ops": [ ... ], "reason": "<one line>"}}
If the change cannot be expressed as a few targeted ops, return
{{"ops": [], "reason": "needs full rewrite"}}.
"""


def _param(step_input: StepInput, key: str, default: str = "") -> Any:
    """params (resolved $refs) first, then context — the established read order."""
    val = step_input.params.get(key)
    if val in (None, ""):
        val = step_input.context.get(key, default)
    return default if val is None else val


def _coerce_ops(parsed: Any) -> list[DataOp]:
    """Validate LLM JSON into DataOps; drop malformed entries."""
    raw = parsed.get("ops") if isinstance(parsed, dict) else parsed
    if not isinstance(raw, list):
        return []
    ops: list[DataOp] = []
    for o in raw:
        if not isinstance(o, dict):
            continue
        name = str(o.get("op", "")).lower()
        path = o.get("path", "")
        if (
            name not in ("set", "add", "remove", "move")
            or not isinstance(path, str)
            or not path
        ):
            continue
        ops.append(
            DataOp(
                op=name,  # type: ignore[arg-type]
                path=path,
                value=o.get("value"),
                from_=o.get("from") or o.get("from_"),
            )
        )
    return ops


def _defer(reason: str) -> StepOutput:
    return StepOutput(
        result={"status": "full_rewrite_requested"},
        observations=f"data_patch deferring to rewrite: {reason}",
    )


async def action_translate_data_ops_turn(step_input: StepInput) -> StepOutput:
    """Translate the prose change_spec → structured DataOps, validate, and
    dry-run them. Publishes ``data_patched_text`` + ``ops_ready`` on success;
    otherwise ``full_rewrite_requested``."""
    effects = step_input.effects
    path = str(_param(step_input, "target_file_path"))
    change_spec = str(
        _param(step_input, "change_spec") or _param(step_input, "flow_directive")
    )

    fmt = detect_fmt(path)
    if fmt is not Fmt.YAML:
        return _defer(f"{fmt.value} not supported in v1 (YAML only)")

    content = str(_param(step_input, "file_content"))
    if not content and effects is not None and path:
        try:
            fc = await effects.read_file(path)
            content = getattr(fc, "content", "") or ""
        except Exception:  # noqa: BLE001
            content = ""
    if not content:
        return _defer("no file content")
    if effects is None:
        return _defer("no effects available")

    skeleton = extract_data_skeleton(content, path) or "(shape unavailable)"
    current = (
        content
        if len(content) <= _MAX_INLINE
        else "(file too large to inline — use the shape above and name precise paths)"
    )
    prompt = DATA_OPS_PROMPT.format(
        fmt=fmt.value,
        change_spec=change_spec or "(none given)",
        skeleton=skeleton,
        path=path,
        current=current,
    )

    try:
        res = await effects.run_inference(prompt)
        text = getattr(res, "text", "") or ""
    except Exception as e:  # noqa: BLE001
        logger.debug("data-ops translation inference failed", exc_info=True)
        return _defer(f"translation inference failed ({type(e).__name__})")

    ops = _coerce_ops(parse_llm_json(text))
    if not ops:
        return _defer("no valid data ops produced")

    patched = patch_text(content, fmt, ops)
    if not patched.ok:
        reasons = "; ".join(r for _op, r in patched.failed)[:160] or "ops did not apply"
        return _defer(reasons)

    return StepOutput(
        result={"ops_ready": True},
        observations=f"data_patch: {len(patched.applied)} surgical op(s) on {path}",
        context_updates={
            "data_patched_text": patched.text,
            "data_ops_summary": f"{len(patched.applied)} op(s)",
        },
    )


async def action_apply_data_ops(step_input: StepInput) -> StepOutput:
    """Write the dry-run-validated patched text. Deterministic; no inference.

    Scaffolding parse floor (shared with guarded_write_file): a patched
    toml/json/yaml/ini must still parse — never hand the grader/toolchain a
    broken config."""
    from agent.actions.file_ops_actions import scaffold_parse_error

    effects = step_input.effects
    path = str(_param(step_input, "target_file_path"))
    text = str(step_input.context.get("data_patched_text", ""))
    if not text or not path:
        return _defer("no patched text/path")
    if effects is None:
        return _defer("no effects available")
    try:
        existing = await effects.read_file(path)
        existing_content = (
            existing.content if getattr(existing, "exists", False) else None
        )
        parse_err = scaffold_parse_error(path, text, existing_content)
        if parse_err:
            return _defer(f"patched {path} would not parse ({parse_err})")
        wr = await effects.write_file(path, text)
    except Exception as e:  # noqa: BLE001
        return _defer(f"write raised ({type(e).__name__})")
    if not getattr(wr, "success", False):
        return _defer(f"write failed ({getattr(wr, 'error', '')})")

    summary = str(step_input.context.get("data_ops_summary", "data patch"))
    return StepOutput(
        result={"status": "success"},
        observations=f"data_patch wrote {path}",
        context_updates={
            "files_changed": [path],
            "edit_summary": f"data_ops: {summary} on {path}",
        },
    )
