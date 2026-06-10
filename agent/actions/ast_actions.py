"""AST-aware editing actions — symbol extraction, selection, and rewriting.

Powers the ast_edit_session sub-flow: tree-sitter extracts symbols from
target files, presents them as a constrained menu, and the model rewrites
each selected symbol sequentially in a memoryful inference session.
"""

from __future__ import annotations

import ast as stdlib_ast
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


# File extensions eligible for the surgical data-patch path (data_ops). v1 is
# YAML-only; extend to json/toml when those round-trip backends land. A data
# file yields zero tree-sitter symbols, so it reaches the "no editable symbols"
# return below — that's where this flag routes file_ops to data_patch instead
# of a full rewrite.
_DATA_PATCH_EXTS = {"yaml", "yml"}


def _data_patch_eligible(path: str) -> bool:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in _DATA_PATCH_EXTS


def _build_symbol_table(file_path: str, file_content: str) -> list[dict[str, Any]]:
    """Extract the editable symbol table for one file.

    Shared by extract_symbol_bodies (the file_ops routing step) and
    load_next_file (the cross-file patch advance) so both produce
    identical entry shapes. Returns [] when tree-sitter is unavailable
    or the file has no editable symbols.
    """
    if not file_path or not file_content or not is_tree_sitter_available():
        return []

    defs, _refs = extract_file_symbols(file_path, file_content)

    # Filter to editable symbol kinds. "variable" covers module-level
    # assignments (things like _COMMAND_PATTERNS = [...]) — 88c showed
    # these need to be rewritable or add_symbol appends duplicates to
    # files that already contain the binding. The splicer handles them
    # via the same line-based replacement path as functions/classes
    # because _extract_python_tree_sitter now populates end_line and
    # byte offsets for variables. Imports are still excluded — rewriting
    # an individual import line is rarely the right fix (diagnose should
    # reach for rewrite of a block or patch of the importing function
    # instead).
    editable = [
        d for d in defs if d.kind in ("function", "method", "class", "variable")
    ]

    # Build symbol table with bodies extracted via line ranges.
    # Line-based extraction is more robust than byte offsets — line
    # boundaries never split mid-token or mid-docstring.
    content_lines = file_content.splitlines(keepends=True)
    symbol_table: list[dict[str, Any]] = []
    for sym in editable:
        qualified_name = f"{sym.parent}.{sym.name}" if sym.parent else sym.name
        if sym.line > 0 and sym.end_line >= sym.line:
            body = "".join(content_lines[sym.line - 1 : sym.end_line])
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
    return symbol_table


def _lookup_symbol(symbol_table: list, qname: str) -> dict | None:
    """Resolve a (possibly qualified) symbol name against a symbol table.

    Tree-sitter may emit both plain and qualified forms (e.g.
    "pick_up_item" as a method AND "GameEngine.pick_up_item" — the
    unqualified name takes "parent: GameEngine"). Handles both forms so
    diagnose can reference by whichever convention it used.
    """
    if not qname or not symbol_table:
        return None
    by_name: dict[str, dict] = {}
    by_qualified: dict[str, dict] = {}
    for sym in symbol_table:
        if not isinstance(sym, dict):
            continue
        name = sym.get("name", "")
        parent = sym.get("parent") or ""
        if name:
            by_name[name] = sym
            if parent:
                by_qualified[f"{parent}.{name}"] = sym
            else:
                by_qualified[name] = sym
    # Prefer exact qualified match, then bare, then the "Class.method"
    # form where only the bare method name was extracted.
    hit = by_qualified.get(qname)
    if hit is not None:
        return hit
    hit = by_name.get(qname)
    if hit is not None:
        return hit
    if "." in qname:
        return by_name.get(qname.rsplit(".", 1)[1])
    return None


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

    Step C Batch E: escape-hatch options (__full_rewrite__, __bail__,
    __done__) are declared as stock options on the select_symbols turn
    declaration, not appended here. This action publishes ONLY real
    symbol entries.
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
            result={
                "symbols_extracted": 0,
                "data_patch_eligible": _data_patch_eligible(file_path),
            },
            observations="tree-sitter not available — falling back to full rewrite",
            context_updates={"symbol_table": [], "symbol_menu_options": []},
        )

    # Extract the editable symbol table (shared helper with load_next_file).
    symbol_table = _build_symbol_table(file_path, file_content)

    if not symbol_table:
        return StepOutput(
            result={
                "symbols_extracted": 0,
                "data_patch_eligible": _data_patch_eligible(file_path),
            },
            observations=f"No editable symbols found in {file_path}",
            context_updates={"symbol_table": [], "symbol_menu_options": []},
        )

    symbol_menu_options: list[dict[str, str]] = [
        {
            "id": sym["name"],
            "description": (
                f"{sym['kind']} (lines {sym['line']}-{sym['end_line']}): "
                f"{sym['signature']}"
            ),
        }
        for sym in symbol_table
    ]

    # Step C Batch E: escape-hatch options (__full_rewrite__, __bail__)
    # no longer appended here. The select_symbols turn declares them
    # as stock options — a single authoritative source.

    # Phase D (patch redesign): check whether the diagnose-named
    # target_symbol is actually present in this file's AST. file_ops
    # uses this to decide between patch (symbol exists → rewrite its
    # body) and add_symbol (symbol missing → insert new). Reads the
    # input passed through from file_ops; empty string means diagnose
    # didn't name a symbol and the old AST-presence routing applies.
    target_symbol = (
        step_input.params.get("target_symbol", "")
        or step_input.context.get("target_symbol", "")
        or ""
    )
    target_symbol_in_ast = False
    if target_symbol:
        target_symbol_in_ast = any(s.get("name") == target_symbol for s in symbol_table)

    return StepOutput(
        result={
            "symbols_extracted": len(symbol_table),
            "target_symbol_in_ast": target_symbol_in_ast,
            "target_symbol_named": bool(target_symbol),
        },
        observations=(
            f"Extracted {len(symbol_table)} editable symbols from {file_path}"
            + (
                f"; target_symbol={target_symbol!r} " f"in_ast={target_symbol_in_ast}"
                if target_symbol
                else ""
            )
        ),
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
    task_description = params.get("task_description", "")
    mode = params.get("mode", step_input.context.get("mode", "fix"))

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
    initial_prompt += f"Mode: {mode}\n"

    # ── Surface dependency signatures from file_context ──────────
    # The file_context projection provides import_deps with field_signatures
    # extracted from actual source code AST. Showing these gives the model
    # the constructor/function signatures of everything this file calls,
    # preventing blind editing where the model guesses at callee APIs.
    file_context = step_input.context.get("file_context")
    if isinstance(file_context, dict):
        import_deps = file_context.get("import_deps", [])
        dep_lines = []
        for dep in import_deps:
            if not isinstance(dep, dict):
                continue
            dep_file = dep.get("file", "")
            responsibility = dep.get("responsibility", "")
            if not dep_file:
                continue
            dep_lines.append(f"  {dep_file}: {responsibility}")
            # Show field signatures (class constructors)
            field_sigs = dep.get("field_signatures", {})
            if isinstance(field_sigs, dict):
                for cls_name, sig in field_sigs.items():
                    dep_lines.append(f"    {cls_name}({sig})")
            # Show top-level function signatures from symbol_bodies
            symbol_bodies = dep.get("symbol_bodies", {})
            if isinstance(symbol_bodies, dict):
                for sym_name, body in symbol_bodies.items():
                    # Extract just the def line for functions
                    if isinstance(body, str):
                        for line in body.splitlines():
                            stripped = line.strip()
                            if stripped.startswith("def "):
                                dep_lines.append(f"    {stripped}")
                                break
        if dep_lines:
            initial_prompt += (
                "\nDependency signatures (imports used by this file — READ ONLY):\n"
                + "\n".join(dep_lines)
                + "\n"
            )

        # File outline (Phase B1 — patch redesign): list all symbols in
        # this file with signatures + first-line docstrings so the model
        # sees what sibling methods exist. Prevents inventing parallel
        # methods (7e7's `.target`/`.direction` invention class) and
        # helps pick the right edit target when diagnose's suggestion
        # doesn't match exactly. Built from symbol_table that
        # extract_symbol_bodies already produced upstream.
        from agent.formatters import _format_file_outline

        symbol_table = step_input.context.get("symbol_table", []) or []
        outline_text = _format_file_outline(
            {"symbol_table": symbol_table, "target_file": file_path},
            namespaces={},
        )
        if outline_text:
            initial_prompt += "\n" + outline_text + "\n"

        # Data contracts: the architecture's data_shapes describe file
        # formats that loader/save/etc. are obligated to honor. Without
        # this, symbol-level edits can silently drift the contract        # (e.g. the b6c run, where the loader grew a required
        # `player_location` field that world.yaml never had). The
        # contract renderer produces a visually prominent MANDATORY
        # block, so the model treats key names as binding rather than
        # suggestive.
        from agent.renderers import render_data_contracts

        contract_text = render_data_contracts({"source": file_context}, namespaces={})
        if contract_text:
            initial_prompt += "\n" + contract_text + "\n"

    # Surface module-level imports — these are OUTSIDE all editable symbols
    # and cannot be changed by symbol-level rewrites. If the task requires
    # changing imports, the model should select "Full file rewrite" instead.
    file_content = step_input.params.get(
        "file_content", step_input.context.get("file_content", "")
    )
    if file_content:
        import_lines = [
            line
            for line in file_content.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        if import_lines:
            initial_prompt += (
                "\nModule-level imports (OUTSIDE editable symbols — "
                "select 'Full file rewrite' if these need to change):\n"
                + "\n".join(f"  {line.strip()}" for line in import_lines)
                + "\n"
            )

    # Surface validation errors so the model knows what to fix
    validation_errors = step_input.context.get("validation_errors", "")
    if validation_errors:
        if isinstance(validation_errors, list):
            error_lines = []
            for err in validation_errors:
                if isinstance(err, dict):
                    name = err.get("name", "unknown check")
                    passed = err.get("passed", True)
                    if not passed:
                        stderr = err.get("stderr", "")
                        stdout = err.get("stdout", "")
                        output = stderr or stdout or "(no output)"
                        error_lines.append(f"  - {name}: {output}")
            if error_lines:
                initial_prompt += (
                    "\nValidation errors to fix:\n" + "\n".join(error_lines) + "\n"
                )
        elif isinstance(validation_errors, str) and validation_errors.strip():
            initial_prompt += f"\nValidation errors to fix:\n{validation_errors}\n"

    # NOTE: we deliberately do NOT send the initial prompt to the
    # model here — the "Acknowledge with 'ready'" ceremony that
    # used to live here was pure waste and, worse, required
    # max_tokens=20 to keep it cheap. That cap was catastrophic
    # for reasoning-model families: Nemotron-3-super (ChatML
    # prefilled thinking) spends thousands of tokens on reasoning
    # BEFORE emitting "ready", so the cap cut it mid-sentence,
    # producing outputs like
    # ``"We need to respond with 'eady' as per instruction..."``.
    #
    # We queue the seed as a session injection; select_symbol_turn
    # (the next step) consumes it via session_injections.consume()
    # so one real inference combines seed + menu. See
    # agent/session_injections.py for the pattern.
    from agent.session_injections import queue as queue_injection

    # Publish file_content and file_path into context so downstream steps
    # (rewrite_symbol, finalize) can access them without extra params.
    # (file_content already read above for import extraction)

    logger.info(
        "start_edit_session: publishing file_content type=%s len=%d | "
        "file_path=%r | from_params=%s from_context=%s",
        type(file_content).__name__,
        len(file_content) if isinstance(file_content, str) else -1,
        file_path,
        "file_content" in step_input.params,
        "file_content" in step_input.context,
    )

    # Queue the seed for the next real inference — select_symbol_turn
    # will prepend it to its first menu prompt via consume().
    context_updates: dict[str, Any] = {
        "edit_session_id": session_id,
        "selected_symbols": [],
        "file_content": file_content,
        "file_path": file_path,
        "mode": mode,
    }
    queue_injection(context_updates, step_input.context, initial_prompt)

    return StepOutput(
        result={"session_started": True},
        observations=f"Edit session started: {session_id}",
        context_updates=context_updates,
    )


# ── select_symbol_turn ────────────────────────────────────────────────


async def action_select_symbol_turn(step_input: StepInput) -> StepOutput:
    """One turn of the symbol selection loop.

    Presents the symbol menu to the memoryful session. Model responds
    with a JSON {"choice": "..."} picking a symbol letter or DONE to
    finish; the choice is parsed by the standard llm_menu extract_choice
    with retry (no grammar constraint — see agent/resolvers/llm_menu.py).
    If >50% of symbols are selected, routes to full rewrite.
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

    # Safety: hard cap — if >50% of symbols are selected, the model
    # is trying to rewrite most of the file. Route to full rewrite
    # instead of editing symbols one at a time.
    selectable_count = len(selectable_ids)
    half_cap = max(1, selectable_count // 2)
    selection_turn = int(step_input.context.get("selection_turn", 0)) + 1

    if len(selected) >= half_cap and selectable_count > 2:
        logger.info(
            "Selection hit 50%% cap (%d/%d symbols) — routing to full rewrite",
            len(selected),
            selectable_count,
        )
        return StepOutput(
            result={
                "selection_complete": False,
                "full_rewrite_requested": True,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations=f"Selected {len(selected)}/{selectable_count} symbols (>50%) — full rewrite more efficient",
            context_updates={
                "selected_symbols": selected,
                "selection_turn": selection_turn,
            },
        )

    # Secondary safety: hard turn cap prevents runaway loops from
    # invalid responses that don't match any option.
    max_turns = selectable_count + 4  # generous but bounded
    if selection_turn > max_turns:
        logger.warning(
            "Selection exceeded %d turns — routing to full rewrite",
            max_turns,
        )
        return StepOutput(
            result={
                "selection_complete": False,
                "full_rewrite_requested": True,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations=f"Max selection turns ({max_turns}) exceeded — full rewrite",
            context_updates={
                "selected_symbols": selected,
                "selection_turn": selection_turn,
            },
        )

    # Append explicit "done" option — stock option from turn declaration
    # is automatically included by the renderer, but we need it in the
    # choice-validation list for extract_turn_menu_choice below.

    # Render the menu prompt from the attached #Turn declaration.
    # Step input carries the turn (Step C Batch E pattern). Fall back
    # to a bare menu prompt if called outside the runtime (tests).
    from agent.runtime import (
        render_turn_prompt,
        extract_turn_menu_choice,
    )
    from agent.session_injections import consume as consume_injections

    # The turn's problem section references input.flow_directive to
    # render the "## Task" block. If input is empty, that section
    # silently omits — the model then sees "Pick the symbol(s) you
    # need to modify to satisfy the Task..." with no Task visible and
    # 902 regression fix — use step_input.inputs. Previously this
    # site did a manual one-field forward of flow_directive from
    # context to input; that was a workaround for the same
    # underlying bug (custom actions couldn't reach flow inputs).
    # Now that StepInput.inputs is populated by _build_step_input,
    # every input-namespace ref in the turn template resolves
    # automatically.
    namespaces = {
        "input": (
            dict(step_input.inputs)
            if step_input.inputs
            else {
                # Backward-compat fallback: if inputs aren't populated
                # (older trace replays, misconfigured tests), preserve
                # the prior minimal surface so the template still renders.
                "flow_directive": step_input.context.get("flow_directive", ""),
            }
        ),
        "context": dict(step_input.context),
        "meta": {},
    }

    turn = step_input.turn
    if turn is not None:
        menu_prompt = render_turn_prompt(turn, namespaces)
    else:
        # Legacy fallback path — should not fire in normal runtime since
        # patch.cue declares the turn. Kept for test compatibility.
        menu_prompt = "Pick a symbol to rewrite, or __done__ to finish."

    prompt, injection_clears = consume_injections(step_input.context, menu_prompt)

    # Temperature from the turn config (resolved t* specifier); fallback
    # to 0.3 matching the Site #11 menu-regime calibration.
    temp_spec = turn.config.get("temperature") if turn and turn.config else None
    try:
        from agent.runtime import _safe_float_temp

        temperature = _safe_float_temp(temp_spec) if temp_spec else 0.3
    except Exception:
        temperature = 0.3

    try:
        result = await effects.session_inference(
            session_id,
            prompt,
            {"temperature": temperature},
        )
        response = result.text.strip() if result.text else ""
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
            context_updates={
                **injection_clears,
                "selected_symbols": selected,
            },
        )

    # Extract the choice against the turn's resolved option set (symbol
    # IDs + the three stock options: __full_rewrite__, __bail__,
    # __done__). Falls back to a local extract_choice if no turn.
    chosen_id: str | None = None
    if turn is not None:
        chosen_id = extract_turn_menu_choice(turn, namespaces, response)
    else:
        from agent.resolvers.llm_menu import extract_choice

        chosen_id = extract_choice(
            response,
            [o["id"] for o in menu_options]
            + ["__done__", "__full_rewrite__", "__bail__"],
        )

    if not chosen_id:
        # Could not parse — treat as done
        return StepOutput(
            result={
                "selection_complete": True,
                "full_rewrite_requested": False,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations=f"Could not parse selection from '{response[:60]}' — finishing",
            context_updates={
                **injection_clears,
                "selected_symbols": selected,
                "selection_turn": selection_turn,
            },
        )

    # "Done" stock option — finish selection
    if chosen_id == "__done__":
        return StepOutput(
            result={
                "selection_complete": True,
                "full_rewrite_requested": False,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations=f"Model selected 'Done': {len(selected)} symbols selected",
            context_updates={
                **injection_clears,
                "selected_symbols": selected,
                "selection_turn": selection_turn,
            },
        )

    if chosen_id == "__full_rewrite__":
        return StepOutput(
            result={
                "selection_complete": False,
                "full_rewrite_requested": True,
                "bail_requested": False,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations="Full rewrite requested by model",
            context_updates={**injection_clears, "selected_symbols": selected},
        )

    if chosen_id == "__bail__":
        return StepOutput(
            result={
                "selection_complete": False,
                "full_rewrite_requested": False,
                "bail_requested": True,
                "symbol_selected": False,
                "symbols_selected": 0,
            },
            observations="Model bailed — file does not need changes or task targets wrong file",
            context_updates={**injection_clears, "selected_symbols": selected},
        )

    # Validate choice is actually a selectable symbol ID
    if chosen_id not in [o["id"] for o in menu_options]:
        return StepOutput(
            result={
                "selection_complete": True,
                "full_rewrite_requested": False,
                "symbol_selected": False,
                "symbols_selected": len(selected),
            },
            observations=f"Selection '{chosen_id}' not in options — finishing",
            context_updates={
                **injection_clears,
                "selected_symbols": selected,
                "selection_turn": selection_turn,
            },
        )

    # Add to selected (avoid duplicates)
    if chosen_id not in selected:
        selected.append(chosen_id)

    return StepOutput(
        result={
            "selection_complete": False,
            "full_rewrite_requested": False,
            "symbol_selected": True,
            "symbols_selected": len(selected),
        },
        observations=f"Selected symbol: {chosen_id} (total: {len(selected)})",
        context_updates={
            **injection_clears,
            "selected_symbols": selected,
            "selection_turn": selection_turn,
        },
    )


# ── prepare_next_rewrite ──────────────────────────────────────────────


def _bail_updates() -> dict[str, Any]:
    """Consistent context shape for prepare_next_rewrite's bail paths."""
    return {
        "rewrite_queue": [],
        "current_symbol": None,
        "already_rewritten": {},
        "cross_file_queue": [],
        "unresolved_symbols": [],
        "files_changed": [],
    }


async def action_prepare_next_rewrite(step_input: StepInput) -> StepOutput:
    """Pick the target symbol + seed the queues with any related symbols.

    Phase C (patch redesign): diagnose names the primary target_symbol
    that file_ops routed here. Phase E / 505 round extends that with an
    optional ``related_symbols`` list. Entries resolve against the
    current file's symbol table — same-file entries (bare names, or
    file-qualified ``path.py:Sym`` whose path matches the patch's
    file_path) seed rewrite_queue; file-qualified entries naming OTHER
    files are partitioned into cross_file_queue, which load_next_file
    drains after the current file's batch is written.

    Reads:
      context.target_symbol OR params.target_symbol — primary target
      params.related_symbols — list of qualified names to co-rewrite
      params.file_path — the current file (for same-file partitioning)
      context.symbol_table — AST-parsed symbols

    Publishes:
      current_symbol — the dict for the primary target
      rewrite_queue — list of dicts for co-dependent symbols in the
                      current file, drained FIFO after current_symbol.
      cross_file_queue — ordered [{"file": path, "symbols": [name, ...]}]
                         for entries living in other files.
      unresolved_symbols — entries that named nothing resolvable in the
                           current file. Surfaced in edit_summary by
                           finalize (no silent drops).
      already_rewritten — empty dict initially. Populated by
                          rewrite_symbol_turn after each successful
                          rewrite with {file:qual_name: new_body}, so
                          later rewrites in the same batch can see
                          what their co-dependents finalized to.
      files_changed — empty list; write_patched_file aggregates into it.
    """
    import os.path as _osp

    target_symbol = (
        step_input.context.get("target_symbol", "")
        or step_input.params.get("target_symbol", "")
        or ""
    )
    # related_symbols arrives via the step's params (patch.cue's
    # begin_rewrite declares {$ref: "input.related_symbols", default: []})
    related_raw = step_input.params.get("related_symbols", []) or []
    if isinstance(related_raw, str):
        # Defensive: collapse single-string to list.
        related_symbols = [s.strip() for s in related_raw.split(",") if s.strip()]
    elif isinstance(related_raw, list):
        related_symbols = [str(s).strip() for s in related_raw if str(s).strip()]
    else:
        related_symbols = []

    file_path = step_input.params.get(
        "file_path", step_input.context.get("file_path", "")
    )
    symbol_table = _ensure_parsed(step_input.context.get("symbol_table", []))

    if not target_symbol:
        return StepOutput(
            result={"has_next": False},
            observations="prepare_next_rewrite: no target_symbol in context",
            context_updates=_bail_updates(),
        )

    if not symbol_table:
        return StepOutput(
            result={"has_next": False},
            observations="prepare_next_rewrite: symbol_table empty",
            context_updates=_bail_updates(),
        )

    # Partition related_symbols: file-qualified entries (path:symbol)
    # naming a different file go to the cross-file queue; everything
    # else resolves against the current file's symbol table.
    norm_current = _osp.normpath(file_path) if file_path else ""
    local_names: list[str] = []
    cross_by_file: dict[str, list[str]] = {}
    cross_order: list[str] = []
    for entry in related_symbols:
        if ":" in entry:
            f_part, s_part = entry.split(":", 1)
            f_part, s_part = f_part.strip(), s_part.strip()
            if f_part and s_part and _osp.normpath(f_part) != norm_current:
                if f_part not in cross_by_file:
                    cross_by_file[f_part] = []
                    cross_order.append(f_part)
                cross_by_file[f_part].append(s_part)
                continue
            entry = s_part or entry
        local_names.append(entry)
    cross_file_queue = [{"file": f, "symbols": cross_by_file[f]} for f in cross_order]

    # Primary target: same single-symbol resolution as before.
    current = _lookup_symbol(symbol_table, target_symbol)
    if current is None:
        logger.warning(
            "prepare_next_rewrite: target_symbol %r not in symbol_table — "
            "file_ops routing should have caught this; falling through to bail",
            target_symbol,
        )
        return StepOutput(
            result={"has_next": False},
            observations=(
                f"Target symbol {target_symbol!r} not found in "
                f"{len(symbol_table)} extracted symbols"
            ),
            context_updates=_bail_updates(),
        )

    # Same-file related symbols: resolve each, record unresolvable.
    # Deduping against the primary and against earlier queue entries.
    queue: list[dict] = []
    already_seen: set[str] = set()
    primary_name = current.get("name", "")
    primary_parent = current.get("parent") or ""
    primary_qual = (
        f"{primary_parent}.{primary_name}" if primary_parent else primary_name
    )
    already_seen.add(primary_name)
    if primary_qual and primary_qual != primary_name:
        already_seen.add(primary_qual)

    unresolved: list[str] = []
    for rname in local_names:
        sym = _lookup_symbol(symbol_table, rname)
        if sym is None:
            unresolved.append(f"{file_path}:{rname}" if file_path else rname)
            continue
        sname = sym.get("name", "")
        sparent = sym.get("parent") or ""
        squal = f"{sparent}.{sname}" if sparent else sname
        if sname in already_seen or squal in already_seen:
            continue
        already_seen.add(sname)
        if squal:
            already_seen.add(squal)
        queue.append(sym)

    obs_parts = [f"Prepared rewrite: {primary_qual or primary_name}"]
    if queue:
        qlist = ", ".join(
            (s.get("parent") + "." if s.get("parent") else "") + (s.get("name", ""))
            for s in queue
        )
        obs_parts.append(f"queued {len(queue)} related: [{qlist}]")
    if cross_file_queue:
        obs_parts.append(
            "cross-file: "
            + ", ".join(f"{e['file']} ({len(e['symbols'])})" for e in cross_file_queue)
        )
    if unresolved:
        obs_parts.append(f"unresolved related_symbols: {unresolved}")
        logger.warning(
            "prepare_next_rewrite: %d related_symbols not in symbol_table: %s",
            len(unresolved),
            unresolved,
        )

    return StepOutput(
        result={"has_next": True},
        observations="; ".join(obs_parts),
        context_updates={
            "rewrite_queue": queue,
            "current_symbol": current,
            "cross_file_queue": cross_file_queue,
            "unresolved_symbols": unresolved,
            "files_changed": [],
            # Tracks what's been rewritten so far in this batch.
            # rewrite_symbol_turn appends to it after each successful
            # rewrite; the turn template shows the list to subsequent
            # rewrites so they see what their co-dependents finalized
            # to and coordinate naturally.
            "already_rewritten": {},
        },
    )


async def action_load_next_file(step_input: StepInput) -> StepOutput:
    """Advance the cross-file patch batch to the next file.

    Pops entries off ``cross_file_queue`` until one yields a readable
    file with at least one resolvable symbol: reads it via
    effects.read_file, rebuilds the symbol table (same shape as
    extract_symbol_bodies), seeds current_symbol + rewrite_queue, and
    queues a session injection announcing the file switch so the
    memoryful edit session carries the cross-file contract forward.
    Files that can't be read or whose symbols all fail to resolve are
    recorded in unresolved_symbols (no silent drops) and skipped.

    Result: has_next (bool) — false when the queue is exhausted.
    Publishes: current_symbol, rewrite_queue, cross_file_queue,
               file_path, file_content, file_content_updated,
               symbol_table, unresolved_symbols
    """
    from agent.session_injections import queue as queue_injection

    effects = step_input.effects
    cross_file_queue = list(
        _ensure_parsed(step_input.context.get("cross_file_queue", [])) or []
    )
    unresolved = list(step_input.context.get("unresolved_symbols", []) or [])

    def _exhausted(observation: str) -> StepOutput:
        return StepOutput(
            result={"has_next": False},
            observations=observation,
            context_updates={
                "cross_file_queue": [],
                "unresolved_symbols": unresolved,
                "current_symbol": None,
                "rewrite_queue": [],
            },
        )

    if not effects:
        for entry in cross_file_queue:
            unresolved.extend(f"{entry['file']}:{s}" for s in entry.get("symbols", []))
        return _exhausted("load_next_file: no effects — cross-file entries dropped")

    while cross_file_queue:
        entry = cross_file_queue.pop(0)
        fpath = str(entry.get("file", ""))
        names = [str(s) for s in entry.get("symbols", []) if str(s)]
        if not fpath or not names:
            continue

        try:
            fc = await effects.read_file(fpath)
        except Exception as e:  # noqa: BLE001 - unreadable file → record + skip
            logger.warning("load_next_file: read failed for %s: %s", fpath, e)
            fc = None
        if fc is None or not getattr(fc, "exists", False) or not fc.content:
            unresolved.extend(f"{fpath}:{s}" for s in names)
            continue

        symbol_table = _build_symbol_table(fpath, fc.content)
        resolved: list[dict] = []
        seen: set[str] = set()
        for name in names:
            sym = _lookup_symbol(symbol_table, name)
            if sym is None:
                unresolved.append(f"{fpath}:{name}")
                continue
            squal = (
                f"{sym.get('parent')}.{sym.get('name')}"
                if sym.get("parent")
                else sym.get("name", "")
            )
            if squal in seen:
                continue
            seen.add(squal)
            resolved.append(sym)
        if not resolved:
            continue

        current, queue = resolved[0], resolved[1:]
        updates: dict[str, Any] = {
            "current_symbol": current,
            "rewrite_queue": queue,
            "cross_file_queue": cross_file_queue,
            "file_path": fpath,
            "file_content": fc.content,
            "file_content_updated": fc.content,
            "symbol_table": symbol_table,
            "unresolved_symbols": unresolved,
        }
        queue_injection(
            updates,
            step_input.context,
            f"[Now editing {fpath} — continue applying the same cross-file "
            f"contract described in the task.]",
        )
        return StepOutput(
            result={"has_next": True},
            observations=(
                f"Advanced to {fpath}: {len(resolved)} symbol(s) queued, "
                f"{len(cross_file_queue)} file(s) remaining"
            ),
            context_updates=updates,
        )

    return _exhausted(
        "Cross-file queue exhausted"
        + (f"; unresolved: {unresolved}" if unresolved else "")
    )


async def action_write_patched_file(step_input: StepInput) -> StepOutput:
    """Write the current file's accumulated splices to disk.

    Runs when the current file's rewrite queue drains — before
    advance_file moves the batch to the next file. Aggregates
    files_changed across the batch and records a per-file summary
    fragment (which symbols were rewritten) for finalize's
    edit_summary.

    Result: write_success (bool).
    Publishes: files_changed, edit_summary_parts
    """
    effects = step_input.effects
    file_path = step_input.context.get("file_path", "")
    file_content = step_input.context.get("file_content_updated", "")
    files_changed = list(step_input.context.get("files_changed", []) or [])
    summary_parts = list(step_input.context.get("edit_summary_parts", []) or [])
    already_rewritten = step_input.context.get("already_rewritten", {}) or {}

    if not effects or not file_path or not file_content:
        return StepOutput(
            result={"write_success": False},
            observations=(
                f"write_patched_file: nothing to write "
                f"(file_path={file_path!r}, content={len(file_content)} chars)"
            ),
            context_updates={
                "files_changed": files_changed,
                "edit_summary_parts": summary_parts,
            },
        )

    prefix = f"{file_path}:"
    rewritten = [k[len(prefix) :] for k in already_rewritten if k.startswith(prefix)]
    if not rewritten:
        # Every rewrite for this file was rejected — file_content_updated
        # still holds the original content. Skip the write so an unchanged
        # file isn't reported as changed; continue to the next file in the
        # batch (its symbols are independent of this file's failure).
        summary_parts.append(f"{file_path}: no rewrites landed (all rejected)")
        return StepOutput(
            result={"write_success": True},
            observations=f"No rewrites landed for {file_path} — skipping write",
            context_updates={
                "files_changed": files_changed,
                "edit_summary_parts": summary_parts,
            },
        )

    try:
        wr = await effects.write_file(file_path, file_content)
        write_success = bool(getattr(wr, "success", False))
        write_error = getattr(wr, "error", None)
    except Exception as e:  # noqa: BLE001 - surfaced via result + summary
        write_success = False
        write_error = str(e)

    if write_success:
        files_changed.append(file_path)
        summary_parts.append(f"{file_path}: rewrote {', '.join(rewritten)}")
        observation = f"Wrote {file_path} ({len(file_content)} chars)"
    else:
        summary_parts.append(f"{file_path}: write FAILED ({write_error})")
        observation = f"Write failed for {file_path}: {write_error}"
        logger.error("write_patched_file: %s", observation)

    return StepOutput(
        result={"write_success": write_success},
        observations=observation,
        context_updates={
            "files_changed": files_changed,
            "edit_summary_parts": summary_parts,
        },
    )


# ── build_call_graph ─────────────────────────────────────────────────
#
# Phase B2 (patch redesign): produce a structural context block for the
# symbol about to be rewritten. Scans project .py files for callers of
# current_symbol (who uses this), extracts callees from current_symbol's
# body (what this invokes). Prevents the invention class of regressions
# (7e7's `.target`, 128's `CommandParser` import omission) by grounding
# the rewrite in the symbol's actual contract.


async def action_build_call_graph(step_input: StepInput) -> StepOutput:
    """Build a structural call-graph block for ``current_symbol``.

    Reads:
      context.current_symbol — dict with name, body, etc.
      context.symbol_table   — full target-file symbol list (for innermost caller resolution)
      context.file_path      — target file path (excluded from self-caller search)
      input.working_directory — project root; scan .py files here

    Publishes:
      context.call_graph_block — formatted text block, or empty string
        when no signal found. Rewrite templates interpolate this via
        {context.call_graph_block}.

    Ignores non-Python projects silently — the formatter returns empty
    for them, and the templates handle empty interpolation cleanly.
    """
    current_symbol = step_input.context.get("current_symbol")
    if not isinstance(current_symbol, dict):
        return StepOutput(
            result={"built": False},
            observations="No current_symbol in context — skipping call-graph build",
            context_updates={"call_graph_block": ""},
        )

    symbol_name = current_symbol.get("name", "")
    if not symbol_name:
        return StepOutput(
            result={"built": False},
            observations="current_symbol has no name — skipping call-graph build",
            context_updates={"call_graph_block": ""},
        )

    # Gather project files. working_directory comes through params (if
    # the step config passed it explicitly) or falls back to context.
    # Using pathlib for portability.
    from pathlib import Path

    working_dir = (
        step_input.params.get("working_directory")
        or step_input.context.get("working_directory")
        or ""
    )
    project_files: dict[str, str] = {}
    if working_dir:
        root = Path(working_dir)
        if root.is_dir():
            # Walk .py files. Cap at a reasonable count so a large repo
            # doesn't stall the flow — 200 files covers the challenge
            # project sizes we've been running comfortably, and well
            # beyond. We read at most 200 files; anything larger and
            # we'd want incremental tree-sitter indexing anyway.
            collected = 0
            for path in root.rglob("*.py"):
                # Skip common noise dirs — .venv, __pycache__, site-packages.
                rel = path.relative_to(root)
                parts = rel.parts
                if any(
                    p
                    in {
                        ".venv",
                        "venv",
                        "__pycache__",
                        "site-packages",
                        ".git",
                        "node_modules",
                    }
                    for p in parts
                ):
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                project_files[str(rel)] = content
                collected += 1
                if collected >= 200:
                    break

    from agent.formatters import _format_call_graph

    target_file = step_input.context.get("file_path", "") or ""
    symbol_table = step_input.context.get("symbol_table") or []

    block = _format_call_graph(
        {
            "target_symbol": symbol_name,
            "target_file": target_file,
            "symbol_table": symbol_table,
            "project_files": project_files,
        },
        namespaces={},
    )

    return StepOutput(
        result={"built": True, "has_block": bool(block)},
        observations=(
            f"Call graph for {symbol_name}: "
            f"{'populated' if block else 'no signal'} "
            f"(scanned {len(project_files)} project files)"
        ),
        context_updates={"call_graph_block": block},
    )


# ── rewrite_symbol_turn ──────────────────────────────────────────────


async def action_rewrite_symbol_turn(step_input: StepInput) -> StepOutput:
    """Rewrite one symbol in the memoryful session.

    Sends the current symbol body to the session, receives the rewritten
    version, splices it into the file content, and re-parses with tree-sitter
    to get updated byte offsets for remaining symbols.

    Step C Batch E: prompt construction moved to the attached #Turn
    declaration (see patch.cue's rewrite_symbol step). Kind-aware
    instruction selection happens via pre_compute emitting
    `context.kind_instruction_template` which the turn's instruction
    section resolves through a Ref-typed template field.

    The wrapper keeps what the schema can't express: kind-mismatch
    validation after inference + targeted correction re-prompt.
    """
    effects = step_input.effects
    session_id = step_input.context.get("edit_session_id", "")

    # ── Normal rewrite mode ──────────────────────────────────────
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

    # Build the rewrite prompt from the attached turn declaration.
    # The turn carries dynamic instruction template selection per
    # symbol kind via a Ref-typed template field (see pre_compute
    # select_rewrite_instruction_template).
    body = current_symbol.get("body", "")
    name = current_symbol.get("name", "unknown")
    kind = current_symbol.get("kind", "function")

    from agent.runtime import render_turn_prompt, _safe_float_temp

    turn = step_input.turn
    if turn is not None:
        # 902 regression fix — flow inputs (change_spec, target_symbol,
        # target_file_path, etc.) reach this action via
        # step_input.inputs, populated by _build_step_input. Before
        # this fix the input namespace was empty, so every
        # ``{input.change_spec}`` reference in the rewrite_symbol
        # turn template resolved to empty string — the
        # ``## What must change`` section rendered with its heading
        # but no body in all 13 rewrite attempts of the 902 run.
        namespaces = {
            "input": dict(step_input.inputs) if step_input.inputs else {},
            "context": dict(step_input.context),
            "meta": {},
        }
        # pre_compute formatters for this step are run by the runtime
        # dispatcher before this action fires (agent/runtime.py's
        # custom-action branch), so kind_instruction_template and
        # call_graph_block are already in step_input.context by now.
        prompt = render_turn_prompt(turn, namespaces)
        temp_spec = turn.config.get("temperature") if turn.config else None
        try:
            temperature = _safe_float_temp(temp_spec) if temp_spec else 0.4
        except Exception:
            temperature = 0.4
    else:
        # Legacy fallback — shouldn't fire with patch.cue's turn
        # declaration. Kept minimal for safety.
        mode_guidance = {
            "fix": "Fix the specific issue described.",
            "refactor": "Improve structure while preserving behavior.",
        }.get(mode, "Apply the requested change.")
        prompt = (
            f"Rewrite this {kind}. Produce the COMPLETE new version.\n\n"
            f"Current implementation:\n```\n{body}\n```\n\n{mode_guidance}"
        )
        temperature = 0.4
    kind_label = "class" if kind == "class" else "function"

    # Pending session injections (the edit-session seed from
    # start_edit_session, file-switch notices in cross-file batches)
    # ride the first inference of this turn. ``injection_clears`` must
    # be merged into the context_updates of every return path after a
    # delivered inference, so the queue isn't replayed on later turns.
    # The exception path deliberately does NOT clear — delivery is
    # unconfirmed there, and re-delivering a notice beats losing it.
    from agent.session_injections import consume as consume_injections

    injection_clears: dict[str, Any] = {}

    async def _do_inference(p: str) -> str:
        """Run inference in the session and return stripped response text.

        No ``max_tokens`` override: rewrites of large function bodies
        must not be silently truncated mid-code by a fixed cap. Falls
        through to LLMVP's configured default (see
        config.generation.max_tokens_default on the active model).
        If the model ever genuinely runs away here, the
        truncated flag surfaces it — a silent cap was the failure
        mode we cannot afford on code output.
        """
        nonlocal injection_clears
        if not injection_clears:
            p, injection_clears = consume_injections(step_input.context, p)
        r = await effects.session_inference(
            session_id,
            p,
            {"temperature": temperature},
        )
        return r.text.strip() if r.text else ""

    try:
        response = await _do_inference(prompt)
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
                **injection_clears,
                "current_symbol": queue.pop(0) if queue else None,
                "rewrite_queue": queue,
                "file_content_updated": file_content,
            },
        )

    # ── Structural validation: verify the replacement preserves symbol kind ──
    #
    # Use stdlib ast.parse to check the top-level node type of the LLM's
    # output. If a class was replaced with a function (or vice versa),
    # re-prompt once. If the retry also fails, reject the splice.

    kind_ok = _validate_symbol_kind(new_body, kind)

    if not kind_ok:
        logger.warning(
            "Symbol kind mismatch for %s: expected %s, got different node type — re-prompting",
            name,
            kind,
        )
        correction_prompt = (
            f"Your previous response produced a {_detect_kind(new_body) or 'unknown'} "
            f"but the symbol '{name}' is a {kind_label}. "
            f"You MUST produce a {kind_label} definition.\n\n"
            f"Produce the COMPLETE rewritten {kind_label} — nothing else:\n"
            f"```\n{body}\n```"
        )

        try:
            retry_response = await _do_inference(correction_prompt)
            new_body = extract_code_from_response(retry_response)
            kind_ok = (
                _validate_symbol_kind(new_body, kind) if new_body.strip() else False
            )
        except Exception as e:
            logger.warning("Re-prompt for kind correction failed: %s", e)
            kind_ok = False

        if not kind_ok:
            logger.error(
                "Symbol kind mismatch persists for %s after re-prompt — rejecting splice",
                name,
            )
            has_next = len(queue) > 0
            return StepOutput(
                result={"rewrite_success": False, "has_next": has_next},
                observations=f"Kind mismatch for {name}: expected {kind}, replacement is wrong type",
                context_updates={
                    **injection_clears,
                    "current_symbol": queue.pop(0) if queue else None,
                    "rewrite_queue": queue,
                    "file_content_updated": file_content,
                },
            )

    # ── Scope-violation gate: reject multi-definition responses ──
    #
    # 88c audit found that when the LLM ignores ``scope: "symbol"``
    # and produces an entire file (or multiple top-level symbols)
    # in response to a single-symbol rewrite, the splicer pastes
    # that full content into the target's line range — creating
    # duplicate definitions. Python tolerates duplicates (second
    # def shadows the first) so the post-splice parse gate does
    # NOT catch this — files get written with shadowed symbols
    # that corrupt runtime behavior. 88c's _COMMAND_PATTERNS
    # triple definition via add_symbol was one route; an LLM
    # emitting the whole file through patch is the other.
    #
    # The fix: count top-level definitions in the replacement. The
    # model was asked to rewrite ONE symbol, so the output should
    # have exactly one top-level class / function / assignment. If
    # it has more, re-prompt once with the symbol's kind stated
    # explicitly and scope reinforced. If the retry also fails,
    # reject the splice — the file stays untouched and the step
    # reports failure (the flow's retry loop will handle it).
    #
    # Only gated on .py (stdlib ast is Python-only).
    if file_path.endswith(".py"):
        def_count = _count_top_level_defs(new_body)
        if def_count > 1:
            logger.warning(
                "Scope violation for %s: LLM emitted %d top-level definitions "
                "(expected exactly 1) — re-prompting",
                name,
                def_count,
            )
            scope_prompt = (
                f"Your previous response contained {def_count} top-level "
                f"definitions, but you were asked to rewrite ONLY the single "
                f"{kind_label} '{name}'. A full-file or multi-symbol response "
                f"would be spliced into the target's line range, creating "
                f"duplicate definitions that corrupt the file.\n\n"
                f"Produce ONLY the rewritten '{name}' {kind_label} — no "
                f"imports, no surrounding code, no other functions or "
                f"classes. Exactly one top-level definition:\n"
                f"```\n{body}\n```"
            )
            try:
                retry_response = await _do_inference(scope_prompt)
                retry_body = extract_code_from_response(retry_response)
                retry_count = (
                    _count_top_level_defs(retry_body) if retry_body.strip() else 0
                )
                retry_kind_ok = (
                    _validate_symbol_kind(retry_body, kind)
                    if retry_body.strip()
                    else False
                )
                if retry_count == 1 and retry_kind_ok:
                    new_body = retry_body
                    def_count = 1
                else:
                    logger.error(
                        "Scope violation persists for %s after re-prompt "
                        "(count=%d, kind_ok=%s) — rejecting splice",
                        name,
                        retry_count,
                        retry_kind_ok,
                    )
                    has_next = len(queue) > 0
                    return StepOutput(
                        result={"rewrite_success": False, "has_next": has_next},
                        observations=(
                            f"Scope violation for {name}: replacement had "
                            f"{def_count} top-level definitions; retry had "
                            f"{retry_count}; file left untouched"
                        ),
                        context_updates={
                            **injection_clears,
                            "current_symbol": queue.pop(0) if queue else None,
                            "rewrite_queue": queue,
                            "file_content_updated": file_content,
                        },
                    )
            except Exception as e:
                logger.warning("Re-prompt for scope correction failed: %s", e)
                has_next = len(queue) > 0
                return StepOutput(
                    result={"rewrite_success": False, "has_next": has_next},
                    observations=(
                        f"Scope violation for {name}: retry failed with {e}; "
                        f"file left untouched"
                    ),
                    context_updates={
                        **injection_clears,
                        "current_symbol": queue.pop(0) if queue else None,
                        "rewrite_queue": queue,
                        "file_content_updated": file_content,
                    },
                )

    # ── Indentation alignment ─────────────────────────────────────
    #
    # The LLM produces code at its "natural" indentation (column 0 for
    # a standalone function).  If the original symbol was indented (e.g.
    # a method inside a class), we must re-indent the replacement to
    # match.  Without this, methods can be ejected from their class body.
    start_line = current_symbol.get("line", 0)
    end_line = current_symbol.get("end_line", 0)

    if start_line >= 1:
        file_lines = file_content.splitlines(keepends=True)
        original_first_line = (
            file_lines[start_line - 1] if start_line <= len(file_lines) else ""
        )
        target_indent = _get_indentation(original_first_line)
        new_body = _reindent(new_body, target_indent)
        logger.debug(
            "Indentation alignment for %s: target=%r",
            name,
            target_indent,
        )

    # Splice the new body into the file content at line boundaries.
    #
    # Line-based splicing is more robust than byte-offset splicing:
    # line boundaries never cut mid-token, mid-docstring, or mid-comment.
    # Tree-sitter's line numbers (1-indexed, decorator-adjusted) give us
    # clean boundaries that align with how the LLM sees the code.
    if start_line < 1 or end_line < start_line:
        # Fallback to byte splice if line numbers are missing/invalid
        logger.warning(
            "Invalid line range for %s (%d-%d), falling back to byte splice",
            name,
            start_line,
            end_line,
        )
        start_byte = current_symbol.get("start_byte", 0)
        end_byte = current_symbol.get("end_byte", 0)
        content_bytes = file_content.encode("utf-8")
        new_body_bytes = new_body.encode("utf-8")
        updated_bytes = (
            content_bytes[:start_byte] + new_body_bytes + content_bytes[end_byte:]
        )
        updated_content = updated_bytes.decode("utf-8", errors="replace")
    else:
        file_lines = file_content.splitlines(keepends=True)
        # start_line and end_line are 1-indexed; end_line is inclusive
        before = file_lines[: start_line - 1]
        after = file_lines[end_line:]

        # Ensure new_body ends with a newline so the splice doesn't
        # merge the last line of the new body with the first line after
        if new_body and not new_body.endswith("\n"):
            new_body += "\n"

        updated_content = "".join(before) + new_body + "".join(after)

    # ── Post-splice parse gate ───────────────────────────────────
    #
    # Belt-and-suspenders: even with kind validation and indentation
    # alignment, splice can produce malformed output when the LLM
    # emits inconsistent indentation or drops structural lines. Parse
    # the full post-splice file and reject the splice if it doesn't
    # parse — the corruption goes into observations, the file is left
    # untouched, and the flow takes the reject branch the same as a
    # kind-mismatch rejection.
    #
    # Only gated on .py today because stdlib ast only parses Python.
    # If tree-sitter grammars for other languages are added later, a
    # corresponding per-language parse check should replace this one.
    if file_path.endswith(".py"):
        try:
            stdlib_ast.parse(updated_content)
        except SyntaxError as e:
            logger.warning(
                "Post-splice parse failed for %s (symbol %s): %s — "
                "rejecting splice, keeping previous file_content",
                file_path,
                name,
                e,
            )
            has_next = len(queue) > 0
            return StepOutput(
                result={"rewrite_success": False, "has_next": has_next},
                observations=(
                    f"Splice rejected for {name}: post-splice parse error "
                    f"({e.msg} at line {e.lineno}). File unchanged."
                ),
                context_updates={
                    **injection_clears,
                    "current_symbol": queue.pop(0) if queue else None,
                    "rewrite_queue": queue,
                    # Preserve the pre-splice content — critical: do NOT
                    # publish the corrupted updated_content downstream.
                    "file_content_updated": file_content,
                },
            )

    # Re-parse with tree-sitter to get updated byte offsets for remaining symbols
    if queue:
        queue = _recalculate_queue_offsets(updated_content, file_path, queue)

    # Pop next symbol
    has_next = len(queue) > 0
    next_symbol = queue.pop(0) if has_next else None

    # 505 round — track what we just rewrote so later rewrites in
    # the same batch can see it. The turn template reads
    # ``context.already_rewritten`` (via a pre_compute formatter) to
    # render an "Other symbols changing in this same batch" block
    # that gives co-dependent rewrites visibility into their
    # neighbors' final shape. Empty until first successful rewrite.
    # Keys are file-qualified ("path.py:Class.method") so cross-file
    # batches stay unambiguous and write_patched_file can attribute
    # rewrites to the file being written.
    already_rewritten = dict(step_input.context.get("already_rewritten", {}) or {})
    primary_parent = current_symbol.get("parent") or ""
    primary_name = current_symbol.get("name", "")
    qual = f"{primary_parent}.{primary_name}" if primary_parent else primary_name
    if qual:
        already_rewritten[f"{file_path}:{qual}" if file_path else qual] = new_body

    return StepOutput(
        result={
            "rewrite_success": True,
            "has_next": has_next or next_symbol is not None,
        },
        observations=f"Rewrote {name} ({len(new_body)} chars)",
        context_updates={
            **injection_clears,
            "current_symbol": next_symbol,
            "rewrite_queue": queue,
            "file_content_updated": updated_content,
            "already_rewritten": already_rewritten,
        },
    )


# ── Indentation alignment ────────────────────────────────────────────


def _get_indentation(line: str) -> str:
    """Return the leading whitespace of a line."""
    return line[: len(line) - len(line.lstrip())]


def _reindent(code: str, target_indent: str) -> str:
    """Re-indent a code block so its outermost structural line sits at *target_indent*.

    A "structural line" is the first non-blank, non-comment line that
    begins the symbol: a decorator (``@...``) if present, otherwise the
    ``def``/``async def``/``class`` statement. Using the decorator as
    the anchor when present is essential — the model often emits a
    decorator at one column and the ``def`` at another, and anchoring
    to ``def`` alone would hide the inconsistency and let malformed
    output through unchanged.

    Once the anchor is found, every line is shifted by the same delta
    (target_indent_length − source_indent_length), preserving relative
    indent between lines. This is a whitespace-character-preserving
    prefix operation — tabs stay tabs, spaces stay spaces. No attempt
    is made to normalize mixed-indent input; such input is left to the
    post-splice parse gate to catch.

    Even when source_indent == target_indent, the function runs — the
    loop body is a no-op shift for correctly-aligned lines and
    corrects lines that drifted from the anchor.
    """
    if not code or not code.strip():
        return code

    lines = code.splitlines(keepends=True)

    # Find the anchor: first structural line (decorator or definition).
    source_indent: str | None = None
    for ln in lines:
        stripped = ln.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("@") or stripped.startswith(
            ("def ", "async def ", "class ")
        ):
            source_indent = _get_indentation(ln)
            break
    # Fallback: first non-blank line (covers pathological cases
    # where the LLM emitted no recognizable structural keyword).
    if source_indent is None:
        for ln in lines:
            stripped = ln.lstrip()
            if stripped and not stripped.startswith("#"):
                source_indent = _get_indentation(ln)
                break
    if source_indent is None:
        return code  # all blank/comments — nothing to do

    # Note: no short-circuit on source_indent == target_indent.
    # When equal, the shift below is a no-op for well-aligned lines
    # and still corrects any line whose indent drifted from the anchor.

    src_len = len(source_indent)
    result_lines: list[str] = []
    for ln in lines:
        if not ln.strip():
            # Preserve blank lines as-is
            result_lines.append(ln)
            continue
        current_indent = _get_indentation(ln)
        if current_indent.startswith(source_indent):
            # Normal case: line is at or deeper than source indent.
            # Replace the source prefix with the target prefix.
            result_lines.append(target_indent + ln[src_len:])
        elif source_indent.startswith(current_indent):
            # Line is *less* indented than the source. Rare in
            # well-formed output but safe to handle: shift to target
            # while preserving the "less indented than anchor" relation
            # relative to target_indent.
            delta = src_len - len(current_indent)
            if delta <= len(target_indent):
                result_lines.append(target_indent[:-delta] + ln.lstrip())
            else:
                # Can't preserve relation inside target_indent — bail
                # to anchor column. Parse gate will catch any true
                # breakage.
                result_lines.append(target_indent + ln.lstrip())
        else:
            # Unrelated indentation (mixed tabs/spaces, etc.). Leave
            # alone rather than corrupting; parse gate is the safety.
            result_lines.append(ln)

    return "".join(result_lines)


# ── Symbol kind validation helpers ────────────────────────────────────


def _symbol_already_bound(file_content: str, target_symbol: str) -> bool:
    """Ground-truth check: does ``target_symbol`` already exist in the file?

    Uses stdlib ast to bypass whatever tree-sitter extraction may have
    missed. Handles three name shapes:

    - ``Foo`` — looks at module-level FunctionDef/ClassDef/Assign/
      AnnAssign with that name.
    - ``Class.method`` — looks up the class, then scans its body for
      a method or attribute with the bare name.
    - Any other dotted form — conservatively returns False (let
      add_symbol proceed; better a spurious insertion than a silent
      miss in unusual cases).

    Returns False on parse failure — the splicer's post-splice parse
    gate will catch structural problems downstream.
    """
    try:
        tree = stdlib_ast.parse(file_content)
    except SyntaxError:
        return False

    if "." not in target_symbol:
        bare = target_symbol
        for node in tree.body:
            if isinstance(node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
                if node.name == bare:
                    return True
            elif isinstance(node, stdlib_ast.ClassDef):
                if node.name == bare:
                    return True
            elif isinstance(node, stdlib_ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, stdlib_ast.Name) and tgt.id == bare:
                        return True
            elif isinstance(node, stdlib_ast.AnnAssign):
                if isinstance(node.target, stdlib_ast.Name) and node.target.id == bare:
                    return True
        return False

    parent, _, bare = target_symbol.rpartition(".")
    # Single-level class lookup — nested classes aren't a common
    # diagnose target, and the routing above doesn't currently
    # synthesize deeper qualified names.
    if "." in parent:
        return False
    for node in tree.body:
        if isinstance(node, stdlib_ast.ClassDef) and node.name == parent:
            for child in node.body:
                if isinstance(
                    child, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)
                ):
                    if child.name == bare:
                        return True
                elif isinstance(child, stdlib_ast.Assign):
                    for tgt in child.targets:
                        if isinstance(tgt, stdlib_ast.Name) and tgt.id == bare:
                            return True
                elif isinstance(child, stdlib_ast.AnnAssign):
                    if (
                        isinstance(child.target, stdlib_ast.Name)
                        and child.target.id == bare
                    ):
                        return True
            break
    return False


def _detect_kind(code: str) -> str | None:
    """Detect whether a code fragment defines a class, function, or variable.

    Uses stdlib ast.parse to find the top-level definition node.
    Skips decorators, comments, and blank lines — only looks at the
    actual node type. Returns "class", "function", "variable", or None if
    parsing fails or no definition is found.
    """
    try:
        tree = stdlib_ast.parse(code)
    except SyntaxError:
        return None

    for node in tree.body:
        if isinstance(node, stdlib_ast.ClassDef):
            return "class"
        if isinstance(node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
            return "function"
        # 88c fix — module-level assignments are valid rewrite targets
        # now that repomap extracts them with byte offsets. Both plain
        # `_X = [...]` and annotated `_X: T = [...]` count.
        if isinstance(node, (stdlib_ast.Assign, stdlib_ast.AnnAssign)):
            return "variable"

    return None


def _count_top_level_defs(code: str) -> int:
    """Count top-level definitions (class, function, variable assignments).

    Used by the scope-violation guard: an LLM asked to rewrite a single
    symbol should emit exactly one top-level definition. If it emits
    more, it ignored ``scope: "symbol"`` and produced a full-file or
    multi-symbol response — splicing that in place yields duplicate
    definitions that silently shadow each other (a Python file can
    have `def foo(): ...` twice and still parse). Post-splice parse
    gate won't catch this class of corruption.

    Imports are NOT counted — LLMs sometimes produce an import along
    with their target and those cases are handled acceptably by the
    splicer (the import goes inside the splice region; file still
    parses; worst case is a redundant import already-at-top-level).

    Returns 0 on parse failure (let the caller's existing parse-based
    rejection handle it).
    """
    try:
        tree = stdlib_ast.parse(code)
    except SyntaxError:
        return 0
    count = 0
    for node in tree.body:
        if isinstance(
            node,
            (
                stdlib_ast.ClassDef,
                stdlib_ast.FunctionDef,
                stdlib_ast.AsyncFunctionDef,
                stdlib_ast.Assign,
                stdlib_ast.AnnAssign,
            ),
        ):
            count += 1
    return count


def _validate_symbol_kind(code: str, expected_kind: str) -> bool:
    """Check that a code fragment's top-level definition matches the expected kind.

    Args:
        code: The LLM-generated replacement code.
        expected_kind: The original symbol's kind — "class", "function", "method", or "variable".

    Returns:
        True if the replacement's structural type matches the original.
        Also returns True if the code can't be parsed (give it the benefit
        of the doubt — tree-sitter validation downstream will catch real errors).
    """
    detected = _detect_kind(code)

    if detected is None:
        # Can't parse — let it through, downstream validation will catch errors
        return True

    # "method" is stored as a function in the AST — both map to "function"
    if expected_kind in ("function", "method"):
        return detected == "function"

    return detected == expected_kind


def _recalculate_queue_offsets(
    updated_content: str,
    file_path: str,
    queue: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Re-parse file with tree-sitter and update line/byte offsets for queued symbols.

    After a splice, all subsequent symbols' ranges shift. The simplest
    and most reliable approach: re-parse the entire file and match by name.
    """
    try:
        defs, _ = extract_file_symbols(file_path, updated_content)
    except Exception as e:
        logger.warning("Failed to re-parse after splice: %s", e)
        return queue

    # Build lookup by qualified name — extract bodies using line ranges
    lines = updated_content.splitlines(keepends=True)
    fresh_by_name: dict[str, dict[str, Any]] = {}
    for d in defs:
        if d.kind in ("function", "method", "class"):
            qname = f"{d.parent}.{d.name}" if d.parent else d.name
            body = ""
            if d.line > 0 and d.end_line >= d.line:
                # Lines are 1-indexed, end_line is inclusive
                body = "".join(lines[d.line - 1 : d.end_line])
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
    """Close the edit session and assemble the batch's edit_summary.

    File writes happen per-file in write_patched_file as each file's
    rewrite queue drains — this step only closes the memoryful session
    and summarizes what the batch changed, including any related
    symbols that never resolved (no silent drops: unresolved entries
    ride edit_summary into the DirectiveReport).
    """
    effects = step_input.effects
    session_id = step_input.context.get("edit_session_id", "")
    files_changed = list(step_input.context.get("files_changed", []) or [])
    summary_parts = list(step_input.context.get("edit_summary_parts", []) or [])
    unresolved = list(step_input.context.get("unresolved_symbols", []) or [])

    if summary_parts:
        edit_summary = "AST-edited " + "; ".join(summary_parts)
    else:
        edit_summary = "No changes applied"
    if unresolved:
        edit_summary += f"; unresolved: {', '.join(unresolved)}"

    logger.info(
        "finalize_edit_session: files_changed=%s | session_id=%s | unresolved=%s",
        files_changed,
        session_id[:12] if session_id else "none",
        unresolved,
    )

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


# ══════════════════════════════════════════════════════════════════════
# Step C Batch E — capture_bail_turn (Site #10b)
# ══════════════════════════════════════════════════════════════════════


async def action_capture_bail_turn(step_input: StepInput) -> StepOutput:
    """Capture the model's bail reasoning as a free-text note.

    Narrow wrapper: renders the attached prose turn, calls
    session_inference, publishes the response text as ``bail_reason``.
    Replaces the former ``rewrite_symbol_turn`` ``bail_prompt: true``
    branch per Site #10b's record.
    """
    effects = step_input.effects
    session_id = step_input.context.get("edit_session_id", "")

    if not effects or not session_id:
        return StepOutput(
            result={"bail_reason_captured": False},
            observations="Cannot capture bail reason — no session",
            context_updates={
                "bail_reason": "No session available to capture reasoning"
            },
        )

    from agent.runtime import render_turn_prompt, _safe_float_temp

    turn = step_input.turn
    if turn is not None:
        # 902 fix: pull flow inputs so turn templates can reference them.
        namespaces = {
            "input": dict(step_input.inputs) if step_input.inputs else {},
            "context": dict(step_input.context),
            "meta": {},
        }
        prompt = render_turn_prompt(turn, namespaces)
        temp_spec = turn.config.get("temperature") if turn.config else None
        try:
            temperature = _safe_float_temp(temp_spec) if temp_spec else 0.4
        except Exception:
            temperature = 0.4
    else:
        prompt = "Explain in 1-2 sentences why this task cannot be completed."
        temperature = 0.4

    try:
        result = await effects.session_inference(
            session_id,
            prompt,
            {"temperature": temperature},
        )
        bail_reason = (
            result.text.strip() if result.text else "Model did not provide reasoning"
        )
    except Exception as e:
        logger.warning("Bail reason inference failed: %s", e)
        bail_reason = f"Failed to capture reasoning: {e}"

    return StepOutput(
        result={"bail_reason_captured": True},
        observations=f"Bail reason: {bail_reason[:200]}",
        context_updates={"bail_reason": bail_reason},
    )


# ── insert_new_symbol ────────────────────────────────────────────────
#
# Phase D (patch redesign): inserts a newly-authored symbol into an
# existing file at an AST-computed position. The sibling to
# ``rewrite_symbol_turn`` — same byte-precise splicing, but for
# additions rather than replacements.
#
# Placement inference, purely from the qualified name:
#   - Contains '.'  → parent is a class. Locate that class in the
#                     symbol_table; insert at end of its body (after
#                     the last method).
#   - No '.'       → top-level function. Append at end of file,
#                    but BEFORE any trailing ``if __name__`` block
#                    so we don't push the entry point past our new
#                    symbol.


async def action_insert_new_symbol(step_input: StepInput) -> StepOutput:
    """Insert a new symbol into an existing file and write to disk.

    Context required:
      file_path, file_content, symbol_table, target_symbol, inference_response

    The inference_response is the model's generated source for the new
    symbol — we run it through ``extract_code_from_response`` to strip
    fences and any surrounding commentary the model may have produced
    despite instruction. Placement is inferred from target_symbol.

    Publishes files_changed, edit_summary, file_content_updated. A
    failed insertion returns ``inserted=False`` with a diagnostic
    observation; the flow routes that to report_failure.
    """
    file_path = step_input.context.get("file_path", "") or step_input.params.get(
        "file_path", ""
    )
    file_content = step_input.context.get("file_content", "") or ""
    symbol_table = step_input.context.get("symbol_table") or []
    target_symbol = step_input.context.get("target_symbol", "") or ""
    inference_response = step_input.context.get("inference_response", "") or ""
    working_dir = step_input.context.get(
        "working_directory", ""
    ) or step_input.params.get("working_directory", "")

    if not (file_path and file_content and target_symbol and inference_response):
        return StepOutput(
            result={"inserted": False},
            observations=(
                "insert_new_symbol: missing required context "
                f"(file_path={bool(file_path)}, content={bool(file_content)}, "
                f"target_symbol={bool(target_symbol)}, response={bool(inference_response)})"
            ),
        )

    # Strip code fences / commentary from the model's response.
    symbol_code = extract_code_from_response(inference_response).strip()
    if not symbol_code:
        return StepOutput(
            result={"inserted": False},
            observations="insert_new_symbol: inference_response empty after code extraction",
        )

    # ── Ground-truth presence check via stdlib ast ──────────────
    #
    # 88c regression fix. Tree-sitter's symbol_table can miss things
    # the target module does in fact define — notably module-level
    # assignments used to fall through that gap and land here, where
    # we'd append a new definition alongside an existing one. Three
    # stacked `_COMMAND_PATTERNS = ...` definitions shadowed each
    # other and broke parser.py.
    #
    # Before inserting, re-scan the file with stdlib ast and check
    # whether the target name is already bound. If it is, bail with a
    # diagnostic observation rather than silently producing a
    # duplicate. The Phase D routing upstream (file_ops) has also
    # been made variable-aware, so this is defense in depth: even if
    # symbol_table extraction regresses again, we won't corrupt a
    # file by appending duplicate bindings.
    #
    # Only gated on .py today — stdlib ast only parses Python. The
    # check is skipped for other languages; non-Python add_symbol is
    # rare and the Phase D gate does most of the work regardless.
    if file_path.endswith(".py"):
        already_defined = _symbol_already_bound(file_content, target_symbol)
        if already_defined:
            return StepOutput(
                result={"inserted": False},
                observations=(
                    f"insert_new_symbol: {target_symbol!r} is already defined "
                    f"in {file_path} (stdlib ast ground truth). Aborting to "
                    f"avoid appending a duplicate definition. Route this "
                    f"change through patch instead."
                ),
            )

    # ── Compute insertion point ─────────────────────────────────
    content_lines = file_content.splitlines(keepends=True)
    insertion_line: int | None = None  # 1-indexed; insertion goes BEFORE this line

    if "." in target_symbol:
        # Qualified name → method on a class. Find the class in
        # symbol_table and insert after its last child method.
        parent_name, _, _bare = target_symbol.rpartition(".")
        parent_entry = None
        for sym in symbol_table:
            if not isinstance(sym, dict):
                continue
            if sym.get("kind") == "class" and sym.get("name") == parent_name:
                parent_entry = sym
                break

        if parent_entry is None:
            return StepOutput(
                result={"inserted": False},
                observations=(
                    f"insert_new_symbol: parent class {parent_name!r} not found in "
                    f"symbol_table for {file_path}"
                ),
            )

        # Find the last method of this class by max end_line among
        # symbols whose parent matches parent_name.
        last_method_end: int | None = None
        for sym in symbol_table:
            if not isinstance(sym, dict):
                continue
            if sym.get("parent") == parent_name:
                el = sym.get("end_line")
                if isinstance(el, int) and (
                    last_method_end is None or el > last_method_end
                ):
                    last_method_end = el

        if last_method_end is not None:
            insertion_line = last_method_end + 1
        else:
            # Empty class body — insert right after the class signature.
            insertion_line = (parent_entry.get("line") or 0) + 1

        # Indent the generated code to one class-body level (4 spaces).
        # Defensive: only indent lines that aren't already indented — the
        # model might output either indented method form or bare def form.
        first_nonblank = next((ln for ln in symbol_code.splitlines() if ln.strip()), "")
        needs_indent = not first_nonblank.startswith((" ", "\t"))
        if needs_indent:
            symbol_code = "\n".join(
                ("    " + ln) if ln.strip() else ln for ln in symbol_code.splitlines()
            )

    else:
        # Top-level function. Append at end, but above any
        # `if __name__ == "__main__":` guard if present.
        guard_line: int | None = None
        for i, line in enumerate(content_lines, start=1):
            stripped = line.strip()
            if stripped.startswith("if __name__") and "__main__" in stripped:
                guard_line = i
                break
        insertion_line = (
            guard_line if guard_line is not None else len(content_lines) + 1
        )

    # ── Splice the new symbol into content_lines ────────────────
    if not symbol_code.endswith("\n"):
        symbol_code += "\n"
    # A blank line before the new symbol keeps the file readable.
    injection = "\n" + symbol_code + "\n"

    idx = max(0, (insertion_line or (len(content_lines) + 1)) - 1)
    new_content_lines = content_lines[:idx] + [injection] + content_lines[idx:]
    new_content = "".join(new_content_lines)

    # ── Sanity check: parse the new content via stdlib ast ──────
    try:
        stdlib_ast.parse(new_content)
    except SyntaxError as e:
        return StepOutput(
            result={"inserted": False},
            observations=(
                f"insert_new_symbol: generated code produced invalid syntax "
                f"when spliced: {e}"
            ),
        )

    # ── Write to disk ───────────────────────────────────────────
    effects = step_input.effects
    if effects is None:
        return StepOutput(
            result={"inserted": False},
            observations="insert_new_symbol: no effects available for file write",
        )

    from pathlib import Path

    target_path = file_path
    if working_dir and not Path(file_path).is_absolute():
        target_path = str(Path(working_dir) / file_path)

    try:
        write_result = await effects.write_file(target_path, new_content)
        ok = getattr(write_result, "ok", None)
        if ok is False:
            return StepOutput(
                result={"inserted": False},
                observations=(
                    f"insert_new_symbol: write_file failed: "
                    f"{getattr(write_result, 'error', '(no error)')}"
                ),
            )
    except Exception as e:
        return StepOutput(
            result={"inserted": False},
            observations=f"insert_new_symbol: write raised: {e}",
        )

    edit_summary = (
        f"Inserted new symbol {target_symbol} into {file_path} "
        f"at line {insertion_line}"
    )
    return StepOutput(
        result={"inserted": True, "status": "success"},
        observations=edit_summary,
        context_updates={
            "files_changed": [file_path],
            "edit_summary": edit_summary,
            "file_content_updated": new_content,
        },
    )


# ── prepare_insert_context ──────────────────────────────────────────
#
# Phase D: synthesize the ``current_symbol`` dict the symbol-scope
# envelope expects from the turn renderer. Patch's pipeline gets this
# from ``action_prepare_next_rewrite`` (which works off the AST-parsed
# symbol_table). For add_symbol the named symbol isn't in the AST yet,
# so we fabricate the minimum: name + kind (method vs function vs
# class), inferred from the qualified name shape.


async def action_prepare_insert_context(step_input: StepInput) -> StepOutput:
    """Fabricate ``current_symbol`` from target_symbol for add_symbol.

    Context required: target_symbol (str)
    Publishes: current_symbol (dict with name + kind)

    Kind inference:
      - "Name.other"   → method (insertion goes into parent class body)
      - "Name"         → function (top-level)
      - Override via input.target_kind if the caller provides one
        (future: diagnose could signal "class" explicitly).
    """
    target_symbol = (
        step_input.context.get("target_symbol", "")
        or step_input.params.get("target_symbol", "")
        or ""
    )
    if not target_symbol:
        return StepOutput(
            result={"prepared": False},
            observations="prepare_insert_context: no target_symbol in context",
        )

    explicit_kind = (
        step_input.context.get("target_kind", "")
        or step_input.params.get("target_kind", "")
        or ""
    )
    if explicit_kind in ("method", "function", "class"):
        kind = explicit_kind
    elif "." in target_symbol:
        kind = "method"
    else:
        kind = "function"

    current_symbol = {
        "name": target_symbol,
        "kind": kind,
    }

    return StepOutput(
        result={"prepared": True},
        observations=(
            f"prepare_insert_context: synthesized current_symbol "
            f"name={target_symbol!r} kind={kind!r}"
        ),
        context_updates={"current_symbol": current_symbol},
    )
