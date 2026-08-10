"""Actions for the ``data_patch`` sub-flow — surgical data edits via ``data_ops``.

When ``file_ops`` sees a data file (no AST symbols), it routes here instead of a
full-file rewrite. ``translate_data_ops_turn`` turns the prose ``change_spec`` +
the file's data shape + its current content into structured ``DataOp``s and
dry-runs them; ``apply_data_ops`` writes the patched text. Any miss
(unparseable, no valid ops, patch won't re-apply, result won't parse, write
fails) publishes ``full_rewrite_requested`` so the parent falls back to the
rewrite — the path is strictly additive.

YAML, TOML and JSON all round-trip. The format is decided by ``detect_fmt`` and
carried into the prompt, because the ops a model should reach for differ: a TOML
table and a YAML mapping address identically but do not LOOK alike, and the
model is shown the real file.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent.data_ops import DataOp, detect_fmt, patch_text
from agent.llm_json import parse_llm_json
from agent.models import StepInput, StepOutput
from agent.schema_extract import extract_data_skeleton
from agent.loader import load_prompt_text

logger = logging.getLogger(__name__)

_MAX_INLINE = 8000  # include the whole current file when under this; else skeleton-only

DATA_OPS_PROMPT = load_prompt_text("data_patch/translate")


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


async def _trace_turn(effects: Any, prompt: str, res: Any, start: float) -> None:
    """Emit the InferenceCall this turn would otherwise never produce.

    THE WALKER'S TURN WAS INVISIBLE. It calls ``effects.run_inference``
    directly rather than being an ``action: "inference"`` step, and the runtime
    emits its InferenceCall inside the step path it renders — so with BOTH
    --trace-prompts and --trace-thinking on, the trace held nothing for this
    call. Live on 2026-08-10 the flow reported success over a byte-identical
    write and there was no way to ask what ops it had proposed; the answer had
    to be reconstructed from an mtime and a byte comparison, and the fix that
    made the eventual repair legible was a logger.info line.

    Emitted HERE rather than in the effects layer, deliberately: the runtime
    already emits for the calls it originates, so emitting inside
    ``run_inference`` would double-log every inference step. This mirrors
    ``LocalEffects.session_inference``, which emits at the call site for
    exactly the same reason. flow/step/mission/cycle come from the bound
    ``agent.trace.step_context``; unbound (tests, mocks) it degrades to a
    no-op rather than a mis-attributed row.

    Best-effort throughout — a telemetry failure must never fail a patch.
    """
    try:
        from agent.trace import InferenceCall, get_step_context, trace_enabled

        if not trace_enabled(effects):
            return
        ctx = get_step_context() or {}
        prompt_content = response_content = ""
        if getattr(effects, "trace_prompts", False):
            prompt_content = prompt
            response_content = getattr(res, "text", "") or ""
        thinking = ""
        if getattr(effects, "trace_thinking", False) and hasattr(
            effects, "fetch_thinking"
        ):
            try:
                thinking = await effects.fetch_thinking()
            except Exception:  # noqa: BLE001
                thinking = ""
        await effects.emit_trace(
            InferenceCall(
                mission_id=ctx.get("mission_id", ""),
                cycle=ctx.get("cycle", 0),
                flow=ctx.get("flow", "data_patch"),
                step=ctx.get("step", "translate_ops"),
                tokens_in=len(prompt.split()),
                tokens_out=len((getattr(res, "text", "") or "").split()),
                wall_ms=(time.monotonic() - start) * 1000,
                purpose="step_inference",
                thinking_content=thinking,
                prompt_content=prompt_content,
                response_content=response_content,
                truncated=bool(getattr(res, "truncated", False)),
                generated_tokens=int(getattr(res, "generated_tokens", 0) or 0),
            )
        )
    except Exception:  # noqa: BLE001 — telemetry never breaks the patch
        logger.debug("data_patch: could not emit inference trace", exc_info=True)


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

    start = time.monotonic()
    try:
        res = await effects.run_inference(prompt)
        text = getattr(res, "text", "") or ""
    except Exception as e:  # noqa: BLE001
        logger.debug("data-ops translation inference failed", exc_info=True)
        return _defer(f"translation inference failed ({type(e).__name__})")
    await _trace_turn(effects, prompt, res, start)

    ops = _coerce_ops(parse_llm_json(text))
    logger.info(
        "data_patch %s: model proposed %d op(s): %s",
        path,
        len(ops),
        "; ".join(f"{o.op} {o.path}" for o in ops[:8]) or "(none)",
    )
    if not ops:
        return _defer("no valid data ops produced")

    patched = patch_text(content, fmt, ops)
    if not patched.ok:
        reasons = "; ".join(r for _op, r in patched.failed)[:160] or "ops did not apply"
        return _defer(reasons)

    # APPLIED IS NOT CHANGED. Every op can succeed and still net to nothing —
    # setting a key to the value it already holds, re-adding an entry that is
    # already there. Live: the walker reported "1 surgical op" on a manifest and
    # rewrote it byte-for-byte, file_ops called that a successful edit, and the
    # quality sweep completed the goal on it. Defer to the rewrite instead: it
    # is the stronger tool, and this is precisely the case where the surgical
    # path has demonstrated it has nothing to offer.
    if patched.text == content:
        return _defer(
            f"{len(patched.applied)} op(s) applied but the file is unchanged — "
            f"the edit was a no-op"
        )

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
        # Second guard on the same rule as translate_ops, at the point of
        # WRITE: the dry-run ran against the content translate_ops was handed,
        # and the file on disk is what actually matters. A write that changes
        # nothing must not be reported as an edit — file_ops treats a data_patch
        # success as a landed change and the quality sweep completes the goal on
        # it, so a no-op here closes a goal that nothing has fixed.
        if existing_content is not None and text == existing_content:
            return _defer(f"{path} is already byte-identical — nothing to write")
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
