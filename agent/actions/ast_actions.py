"""AST-aware editing actions — symbol extraction, selection, and rewriting.

Powers the ast_edit_session sub-flow: tree-sitter extracts symbols from
target files, presents them as a constrained menu, and the model rewrites
each selected symbol sequentially in a memoryful inference session.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.models import StepInput, StepOutput
from agent.repomap import extract_file_symbols, is_tree_sitter_available
from agent.actions.refinement_actions import extract_code_from_response

logger = logging.getLogger(__name__)


def _ensure_parsed(value: Any) -> Any:
    """Ensure a value that may have been JSON-stringified through Jinja2 is parsed.

    When complex objects (lists/dicts) pass through flow input_map templates,
    they get serialized as Python repr strings or JSON strings. This helper
    safely parses them back to native types.
    """
    if isinstance(value, str) and value.strip():
        # Try JSON first
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass
        # Try Python literal eval for repr-style strings like "[{'key': 'val'}]"
        try:
            import ast

            parsed = ast.literal_eval(value)
            if isinstance(parsed, (list, dict)):
                return parsed
        except (ValueError, SyntaxError):
            pass
    return value


# ── extract_symbol_bodies ─────────────────────────────────────────────


async def action_extract_symbol_bodies(step_input: StepInput) -> StepOutput:
    """Extract symbols from target file and build selection menu.

    Reads: context.target_file (path + content)
    Publishes: symbol_table (list of dicts), symbol_menu_options (list for dynamic menu)

    Each symbol_table entry:
        {
            "name": "GameEngine.process_command",
            "kind": "method",
            "signature": "def process_command(self, raw_input: str) -> str:",
            "line": 30,
            "end_line": 85,
            "start_byte": 450,
            "end_byte": 1820,
            "body": "def process_command(self, raw_input: str) -> str:\n    ...",
            "parent": "GameEngine"
        }

    Each symbol_menu_options entry (for dynamic LLM menu):
        {
            "id": "GameEngine.process_command",
            "description": "method (lines 30-85): def process_command(self, raw_input: str) -> str:"
        }

    The last menu option is always the full-rewrite escape hatch.
    """
    target_file = step_input.context.get("target_file", {})
    file_path = target_file.get("path", "")
    file_content = target_file.get("content", "")

    if not file_content or not file_path:
        return StepOutput(
            result={"symbols_extracted": 0},
            observations="No target file content available for symbol extraction",
            context_updates={"symbol_table": [], "symbol_menu_options": []},
        )

    if not is_tree_sitter_available():
        return StepOutput(
            result={"symbols_extracted": 0},
            observations="tree-sitter not available — falling back to full rewrite",
            context_updates={"symbol_table": [], "symbol_menu_options": []},
        )

    # Extract symbols using tree-sitter
    defs, _refs = extract_file_symbols(file_path, file_content)

    # Filter to function, method, class — skip imports and variables
    editable = [d for d in defs if d.kind in ("function", "method", "class")]

    if not editable:
        return StepOutput(
            result={"symbols_extracted": 0},
            observations=f"No editable symbols found in {file_path}",
            context_updates={"symbol_table": [], "symbol_menu_options": []},
        )

    # Build symbol table with bodies extracted via byte ranges
    # Use the encoded bytes for accurate slicing (tree-sitter byte offsets
    # correspond to the UTF-8 encoded content)
    content_bytes = file_content.encode("utf-8")
    symbol_table: list[dict[str, Any]] = []
    symbol_menu_options: list[dict[str, str]] = []

    for sym in editable:
        # Build qualified name for methods
        qualified_name = f"{sym.parent}.{sym.name}" if sym.parent else sym.name

        # Extract body using byte ranges
        if sym.start_byte > 0 or sym.end_byte > 0:
            body = content_bytes[sym.start_byte : sym.end_byte].decode(
                "utf-8", errors="replace"
            )
        else:
            body = ""

        symbol_table.append(
            {
                "name": qualified_name,
                "kind": sym.kind,
                "signature": sym.signature,
                "line": sym.line,
                "end_line": sym.end_line,
                "start_byte": sym.start_byte,
                "end_byte": sym.end_byte,
                "body": body,
                "parent": sym.parent,
            }
        )

        description = f"{sym.kind} (lines {sym.line}-{sym.end_line}): {sym.signature}"
        symbol_menu_options.append({"id": qualified_name, "description": description})

    # Always append the full-rewrite escape hatch
    symbol_menu_options.append(
        {
            "id": "__full_rewrite__",
            "description": "Full file rewrite — use when structural changes are needed across the entire file",
        }
    )

    # Bail option — wrong file or task doesn't apply here
    symbol_menu_options.append(
        {
            "id": "__bail__",
            "description": "BAIL — this file does not need changes, or the task description targets the wrong file",
        }
    )

    return StepOutput(
        result={"symbols_extracted": len(symbol_table)},
        observations=f"Extracted {len(symbol_table)} editable symbols from {file_path}",
        context_updates={
            "symbol_table": symbol_table,
            "symbol_menu_options": symbol_menu_options,
        },
    )


# ── start_edit_session ────────────────────────────────────────────────


async def action_start_edit_session(step_input: StepInput) -> StepOutput:
    """Start a memoryful inference session for the edit workflow.

    Opens a session, sends the initial context (file overview, task, mode),
    and returns the session ID.
    """
    effects = step_input.effects
    if not effects:
        return StepOutput(
            result={"session_started": False},
            observations="No effects interface — cannot start session",
        )

    params = step_input.params
    file_path = params.get("file_path", step_input.context.get("file_path", ""))
    task_description = params.get(
        "task_description", step_input.context.get("task_description", "")
    )
    reason = params.get("reason", step_input.context.get("reason", ""))
    mode = params.get("mode", step_input.context.get("mode", "fix"))
    relevant_notes = params.get(
        "relevant_notes", step_input.context.get("relevant_notes", "")
    )

    try:
        session_id = await effects.start_inference_session({"ttl_seconds": 600})
    except Exception as e:
        logger.error("Failed to start inference session: %s", e)
        return StepOutput(
            result={"session_started": False},
            observations=f"Failed to start inference session: {e}",
        )

    # Send initial context to the session
    initial_prompt = (
        "You are a code editor. You will be shown a file's structure and asked "
        "to select which functions/classes to modify, then rewrite each one.\n\n"
        f"File: {file_path}\n"
        f"Task: {task_description}\n"
    )
    if reason:
        initial_prompt += f"Reason: {reason}\n"
    initial_prompt += f"Mode: {mode}\n"
    if relevant_notes:
        initial_prompt += f"\nNotes:\n{relevant_notes}\n"
    initial_prompt += "\nAcknowledge with 'ready'."

    try:
        await effects.session_inference(
            session_id, initial_prompt, {"temperature": 0.1, "max_tokens": 20}
        )
    except Exception as e:
        logger.warning("Initial session turn failed: %s", e)
        # Session is still open — continue

    # Publish file_content and file_path into context so downstream steps
    # (rewrite_symbol, finalize) can access them without extra params.
    file_content = step_input.params.get(
        "file_content", step_input.context.get("file_content", "")
    )

    logger.info(
        "start_edit_session: publishing file_content type=%s len=%d | "
        "file_path=%r | from_params=%s from_context=%s",
        type(file_content).__name__,
        len(file_content) if isinstance(file_content, str) else -1,
        file_path,
        "file_content" in step_input.params,
        "file_content" in step_input.context,
    )

    return StepOutput(
        result={"session_started": True},
        observations=f"Edit session started: {session_id}",
        context_updates={
            "edit_session_id": session_id,
            "selected_symbols": [],
            "file_content": file_content,
            "file_path": file_path,
            "mode": mode,
        },
    )


# ── select_symbol_turn ────────────────────────────────────────────────


async def action_select_symbol_turn(step_input: StepInput) -> StepOutput:
    """One turn of the symbol selection loop.

    Presents the symbol menu to the memoryful session. Model picks
    a letter (grammar-constrained) or empty string to finish.
    """
    effects = step_input.effects
    session_id = step_input.context.get("edit_session_id", "")
    menu_options = _ensure_parsed(step_input.context.get("symbol_menu_options", []))
    selected = list(
        _ensure_parsed(step_input.context.get("selected_symbols", [])) or []
    )

    if not effects or not session_id or not menu_options:
        return StepOutput(
            result={
                "selection_complete": True,
                "full_rewrite_requested": False,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations="Missing session or menu options — finishing selection",
            context_updates={"selected_symbols": selected},
        )

    # Safety: auto-complete if all selectable symbols are already selected
    selectable_ids = [o["id"] for o in menu_options if o["id"] != "__full_rewrite__"]
    if selected and all(sid in selected for sid in selectable_ids):
        return StepOutput(
            result={
                "selection_complete": True,
                "full_rewrite_requested": False,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations=f"All {len(selected)} symbols already selected — auto-completing",
            context_updates={"selected_symbols": selected},
        )

    # Safety: hard cap on selection turns to prevent runaway loops.
    # Track turn count via context (incremented each call).
    selection_turn = int(step_input.context.get("selection_turn", 0)) + 1
    max_turns = len(menu_options) * 2 + 2
    if selection_turn > max_turns:
        logger.warning(
            "Selection exceeded %d turns — auto-completing with %d symbols",
            max_turns,
            len(selected),
        )
        return StepOutput(
            result={
                "selection_complete": True,
                "full_rewrite_requested": False,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations=f"Max selection turns ({max_turns}) exceeded — auto-completing",
            context_updates={
                "selected_symbols": selected,
                "selection_turn": selection_turn,
            },
        )

    # Append explicit "done" option — LLMs can't produce empty strings,
    # so we use a real letter option instead of relying on "" in the grammar.
    done_option = {
        "id": "__done__",
        "description": "Done — finish selection and proceed to rewriting",
    }
    display_options = list(menu_options) + [done_option]

    # Build the menu prompt
    lines = ["Available symbols:"]
    for i, opt in enumerate(display_options):
        letter = chr(ord("a") + i)
        lines.append(f"{letter}) {opt['description']}")

    if selected:
        selected_names = ", ".join(selected)
        lines.append(f"\nSelected so far: {selected_names}")
    else:
        lines.append("\nSelected so far: none")

    lines.append(
        "\nPick the next symbol to modify, or select 'Done' to finish selection."
    )

    prompt = "\n".join(lines)

    # Build GBNF grammar — all letters including the "done" option
    n = len(display_options)
    last_letter = chr(ord("a") + n - 1)
    grammar = f"root ::= [a-{last_letter}]"

    try:
        result = await effects.session_inference(
            session_id,
            prompt,
            {"temperature": 0.1, "max_tokens": 5, "grammar": grammar},
        )
        response = result.text.strip()
    except Exception as e:
        logger.error("Symbol selection turn failed: %s", e)
        return StepOutput(
            result={
                "selection_complete": True,
                "full_rewrite_requested": False,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations=f"Selection turn failed: {e}",
            context_updates={"selected_symbols": selected},
        )

    # Parse the response
    if not response:
        # Empty response = selection complete
        return StepOutput(
            result={
                "selection_complete": True,
                "full_rewrite_requested": False,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations=f"Selection complete: {len(selected)} symbols selected",
            context_updates={"selected_symbols": selected},
        )

    # Map letter to option (using display_options which includes __done__)
    letter = response[0].lower()
    index = ord(letter) - ord("a")

    if index < 0 or index >= len(display_options):
        # Invalid selection — treat as done
        return StepOutput(
            result={
                "selection_complete": True,
                "full_rewrite_requested": False,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations=f"Invalid selection '{response}' — finishing",
            context_updates={
                "selected_symbols": selected,
                "selection_turn": selection_turn,
            },
        )

    option = display_options[index]

    # "Done" option — finish selection
    if option["id"] == "__done__":
        return StepOutput(
            result={
                "selection_complete": True,
                "full_rewrite_requested": False,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations=f"Model selected 'Done': {len(selected)} symbols selected",
            context_updates={
                "selected_symbols": selected,
                "selection_turn": selection_turn,
            },
        )

    if option["id"] == "__full_rewrite__":
        return StepOutput(
            result={
                "selection_complete": False,
                "full_rewrite_requested": True,
                "bail_requested": False,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations="Full rewrite requested by model",
            context_updates={"selected_symbols": selected},
        )

    if option["id"] == "__bail__":
        return StepOutput(
            result={
                "selection_complete": False,
                "full_rewrite_requested": False,
                "bail_requested": True,
                "symbol_selected": False,
                "symbols_selected": 0,
            },
            observations="Model bailed — file does not need changes or task targets wrong file",
            context_updates={"selected_symbols": selected},
        )

    # Add to selected (avoid duplicates)
    if option["id"] not in selected:
        selected.append(option["id"])

    return StepOutput(
        result={
            "selection_complete": False,
            "full_rewrite_requested": False,
            "symbol_selected": True,
            "symbols_selected": len(selected),
        },
        observations=f"Selected symbol: {option['id']} (total: {len(selected)})",
        context_updates={
            "selected_symbols": selected,
            "selection_turn": selection_turn,
        },
    )


# ── prepare_next_rewrite ──────────────────────────────────────────────


async def action_prepare_next_rewrite(step_input: StepInput) -> StepOutput:
    """Initialize rewrite queue from selected symbols.

    Takes selected_symbols list and symbol_table, builds an ordered queue.
    Pops the first item as current_symbol.
    """
    selected_raw = step_input.context.get("selected_symbols", [])
    symbol_table_raw = step_input.context.get("symbol_table", [])
    selected = _ensure_parsed(selected_raw)
    symbol_table = _ensure_parsed(symbol_table_raw)

    logger.info(
        "prepare_next_rewrite: selected_type=%s selected=%s | "
        "symbol_table_type=%s symbol_table_len=%s | "
        "raw_selected_type=%s raw_table_type=%s",
        type(selected).__name__,
        repr(selected)[:300],
        type(symbol_table).__name__,
        len(symbol_table) if isinstance(symbol_table, list) else "N/A",
        type(selected_raw).__name__,
        type(symbol_table_raw).__name__,
    )

    if not selected or not symbol_table:
        logger.error(
            "prepare_next_rewrite EMPTY: selected=%s symbol_table=%s",
            bool(selected),
            bool(symbol_table),
        )
        return StepOutput(
            result={"has_next": False},
            observations="No symbols selected for rewriting",
            context_updates={"rewrite_queue": [], "current_symbol": None},
        )

    # Build lookup by name
    sym_by_name = {s["name"]: s for s in symbol_table}

    # Build ordered queue from selected symbols
    queue = []
    for name in selected:
        if name in sym_by_name:
            queue.append(sym_by_name[name])

    if not queue:
        return StepOutput(
            result={"has_next": False},
            observations="Selected symbols not found in symbol table",
            context_updates={"rewrite_queue": [], "current_symbol": None},
        )

    # Pop the first item
    current = queue.pop(0)

    return StepOutput(
        result={"has_next": True},
        observations=f"Rewrite queue: {len(queue) + 1} symbols, starting with {current['name']}",
        context_updates={"rewrite_queue": queue, "current_symbol": current},
    )


# ── rewrite_symbol_turn ──────────────────────────────────────────────


async def action_rewrite_symbol_turn(step_input: StepInput) -> StepOutput:
    """Rewrite one symbol in the memoryful session.

    Sends the current symbol body to the session, receives the rewritten
    version, splices it into the file content, and re-parses with tree-sitter
    to get updated byte offsets for remaining symbols.

    Special mode: if params.bail_prompt is True, instead of rewriting a symbol
    this asks the model to explain why it bailed, captures the reasoning, and
    returns it as bail_reason for the note system.
    """
    effects = step_input.effects
    session_id = step_input.context.get("edit_session_id", "")

    # ── Bail prompt mode ─────────────────────────────────────────
    if step_input.params.get("bail_prompt"):
        if not effects or not session_id:
            return StepOutput(
                result={"bail_reason_captured": False},
                observations="Cannot capture bail reason — no session",
                context_updates={
                    "bail_reason": "No session available to capture reasoning"
                },
            )

        bail_prompt = (
            "You chose to BAIL on modifying this file. Before we close, "
            "explain in 2-3 sentences:\n"
            "1. Why this file does not need the requested changes\n"
            "2. Which file or approach SHOULD be targeted instead\n"
            "Be specific — your answer becomes a note for the task director."
        )

        try:
            result = await effects.session_inference(
                session_id,
                bail_prompt,
                {"temperature": 0.3, "max_tokens": 300},
            )
            bail_reason = (
                result.text.strip()
                if result.text
                else "Model did not provide reasoning"
            )
        except Exception as e:
            logger.warning("Bail reason inference failed: %s", e)
            bail_reason = f"Failed to capture reasoning: {e}"

        return StepOutput(
            result={"bail_reason_captured": True},
            observations=f"Bail reason: {bail_reason[:200]}",
            context_updates={"bail_reason": bail_reason},
        )

    # ── Normal rewrite mode ──────────────────────────────────────
    effects = step_input.effects
    session_id = step_input.context.get("edit_session_id", "")
    current_symbol = _ensure_parsed(step_input.context.get("current_symbol"))
    queue = list(_ensure_parsed(step_input.context.get("rewrite_queue", [])) or [])
    file_content = step_input.context.get(
        "file_content_updated",
        step_input.context.get("file_content", ""),
    )
    # Also try to get from params (passed via input_map)
    if not file_content:
        file_content = step_input.params.get("file_content", "")
    file_path = step_input.params.get(
        "file_path", step_input.context.get("file_path", "")
    )
    mode = step_input.params.get("mode", step_input.context.get("mode", "fix"))

    if not effects or not session_id or not current_symbol or not file_content:
        # Detailed diagnostic logging — identify exactly which value is falsy
        missing = []
        if not effects:
            missing.append("effects=None")
        if not session_id:
            missing.append(f"session_id={session_id!r}")
        if not current_symbol:
            missing.append(
                f"current_symbol={type(current_symbol).__name__}:"
                f"{repr(current_symbol)[:200]}"
            )
        if not file_content:
            missing.append(
                f"file_content={type(file_content).__name__}:"
                f"{repr(file_content)[:200]}"
            )
        # Log what IS available for cross-reference
        logger.error(
            "rewrite_symbol_turn BAIL: missing=[%s] | "
            "context_keys=%s | params_keys=%s | "
            "file_content_type=%s len=%d | "
            "current_symbol_type=%s",
            ", ".join(missing),
            list(step_input.context.keys()),
            list(step_input.params.keys()),
            type(file_content).__name__,
            len(file_content) if isinstance(file_content, str) else -1,
            type(current_symbol).__name__,
        )
        return StepOutput(
            result={"rewrite_success": False, "has_next": False},
            observations=f"BAIL: {', '.join(missing)}",
            context_updates={},
        )

    # Build the rewrite prompt
    body = current_symbol.get("body", "")
    name = current_symbol.get("name", "unknown")

    mode_guidance = {
        "fix": "Fix the specific issue described. Preserve all other behavior.",
        "refactor": "Improve structure while preserving behavior.",
    }.get(mode, "Apply the requested change.")

    prompt = (
        f"Now rewrite this function. Produce the COMPLETE new version.\n"
        f"Do not include anything outside the function body.\n\n"
        f"Current implementation:\n```\n{body}\n```\n\n"
        f"{mode_guidance}\n\n"
        f"Respond with ONLY the complete rewritten function:\n"
        f"```\ndef function_name(...):\n    # new implementation\n```"
    )

    try:
        result = await effects.session_inference(
            session_id,
            prompt,
            {"temperature": 0.3, "max_tokens": 4096},
        )
        response = result.text.strip()
    except Exception as e:
        logger.error("Rewrite turn failed for %s: %s", name, e)
        return StepOutput(
            result={"rewrite_success": False, "has_next": len(queue) > 0},
            observations=f"Rewrite failed for {name}: {e}",
            context_updates={
                "current_symbol": queue.pop(0) if queue else None,
                "rewrite_queue": queue,
            },
        )

    # Extract code from response
    new_body = extract_code_from_response(response)

    if not new_body.strip():
        logger.warning("Empty rewrite response for %s", name)
        has_next = len(queue) > 0
        return StepOutput(
            result={"rewrite_success": False, "has_next": has_next},
            observations=f"Empty rewrite for {name}",
            context_updates={
                "current_symbol": queue.pop(0) if queue else None,
                "rewrite_queue": queue,
                "file_content_updated": file_content,
            },
        )

    # Splice the new body into the file content at byte offsets
    start_byte = current_symbol.get("start_byte", 0)
    end_byte = current_symbol.get("end_byte", 0)

    content_bytes = file_content.encode("utf-8")
    new_body_bytes = new_body.encode("utf-8")

    updated_bytes = (
        content_bytes[:start_byte] + new_body_bytes + content_bytes[end_byte:]
    )
    updated_content = updated_bytes.decode("utf-8", errors="replace")

    # Re-parse with tree-sitter to get updated byte offsets for remaining symbols
    if queue:
        queue = _recalculate_queue_offsets(updated_content, file_path, queue)

    # Pop next symbol
    has_next = len(queue) > 0
    next_symbol = queue.pop(0) if has_next else None

    return StepOutput(
        result={
            "rewrite_success": True,
            "has_next": has_next or next_symbol is not None,
        },
        observations=f"Rewrote {name} ({len(new_body)} chars)",
        context_updates={
            "current_symbol": next_symbol,
            "rewrite_queue": queue,
            "file_content_updated": updated_content,
        },
    )


def _recalculate_queue_offsets(
    updated_content: str,
    file_path: str,
    queue: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Re-parse file with tree-sitter and update byte offsets for queued symbols.

    After a splice, all subsequent symbols' byte ranges shift. The simplest
    and most reliable approach: re-parse the entire file and match by name.
    """
    try:
        defs, _ = extract_file_symbols(file_path, updated_content)
    except Exception as e:
        logger.warning("Failed to re-parse after splice: %s", e)
        return queue

    # Build lookup by qualified name
    content_bytes = updated_content.encode("utf-8")
    fresh_by_name: dict[str, dict[str, Any]] = {}
    for d in defs:
        if d.kind in ("function", "method", "class"):
            qname = f"{d.parent}.{d.name}" if d.parent else d.name
            body = ""
            if d.start_byte > 0 or d.end_byte > 0:
                body = content_bytes[d.start_byte : d.end_byte].decode(
                    "utf-8", errors="replace"
                )
            fresh_by_name[qname] = {
                "name": qname,
                "kind": d.kind,
                "signature": d.signature,
                "line": d.line,
                "end_line": d.end_line,
                "start_byte": d.start_byte,
                "end_byte": d.end_byte,
                "body": body,
                "parent": d.parent,
            }

    # Update queue entries with fresh offsets
    updated_queue = []
    for item in queue:
        name = item["name"]
        if name in fresh_by_name:
            updated_queue.append(fresh_by_name[name])
        else:
            # Symbol may have been removed or renamed — keep old entry
            logger.warning(
                "Symbol %s not found after re-parse, keeping old offsets", name
            )
            updated_queue.append(item)

    return updated_queue


# ── finalize_edit_session ─────────────────────────────────────────────


async def action_finalize_edit_session(step_input: StepInput) -> StepOutput:
    """Write the modified file to disk and close the inference session.

    1. Write file_content_updated to file_path via effects.write_file
    2. End the inference session via effects.end_inference_session
    3. Build edit_summary from the list of rewritten symbols
    """
    effects = step_input.effects
    session_id = step_input.context.get("edit_session_id", "")
    file_content = step_input.context.get("file_content_updated", "")
    file_path = step_input.params.get(
        "file_path", step_input.context.get("file_path", "")
    )
    selected = step_input.context.get("selected_symbols", [])

    files_changed: list[str] = []
    edit_summary = "No changes applied"

    logger.info(
        "finalize_edit_session: file_content_type=%s len=%d | "
        "file_path=%r | selected=%s | session_id=%s | context_keys=%s",
        type(file_content).__name__,
        len(file_content) if isinstance(file_content, str) else -1,
        file_path,
        repr(selected)[:200],
        session_id[:12] if session_id else "none",
        list(step_input.context.keys()),
    )

    if effects and file_content and file_path:
        try:
            wr = await effects.write_file(file_path, file_content)
            if wr.success:
                files_changed.append(file_path)
                symbols_str = ", ".join(selected) if selected else "unknown"
                edit_summary = (
                    f"AST-edited {file_path}: rewrote {len(selected)} symbol(s) "
                    f"({symbols_str})"
                )
            else:
                edit_summary = f"Failed to write {file_path}: {wr.error}"
        except Exception as e:
            edit_summary = f"Error writing {file_path}: {e}"

    # End the inference session
    if effects and session_id:
        try:
            await effects.end_inference_session(session_id)
        except Exception as e:
            logger.warning("Failed to end session %s: %s", session_id, e)

    return StepOutput(
        result={"status": "success" if files_changed else "failed"},
        observations=edit_summary,
        context_updates={
            "files_changed": files_changed,
            "edit_summary": edit_summary,
        },
    )


# ── close_edit_session ────────────────────────────────────────────────


async def action_close_edit_session(step_input: StepInput) -> StepOutput:
    """Close the inference session without writing (no changes or full rewrite requested).

    End the inference session, return appropriate status.
    Uses params.return_status to set the status in result (defaults to "closed").
    This lets the parent flow's resolver match on the correct status.
    """
    effects = step_input.effects
    session_id = step_input.context.get("edit_session_id", "")
    # Allow the step YAML to configure what status to return
    return_status = step_input.params.get("return_status", "closed")

    if effects and session_id:
        try:
            await effects.end_inference_session(session_id)
        except Exception as e:
            logger.warning("Failed to end session %s: %s", session_id, e)

    return StepOutput(
        result={"status": return_status},
        observations=f"Edit session {session_id} closed (status={return_status})",
        context_updates={
            "edit_summary": "Edit session closed — no symbol changes applied",
        },
    )
