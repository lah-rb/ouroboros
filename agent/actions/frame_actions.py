"""Module-frame editing: a structured module-fix router + an inference frame editor.

When a fix is module-class (a missing *module-level* line — an import, a script's
shebang, a ``source``/``set`` line), the symbol-scoped patch flow can't express it
— it only rewrites a named function/class body, where a top-level statement can
never live (this trapped Mistral in a 6× loop). This module keeps the project's
"deterministic routing + inference edits" split:

  • action_check_module_fix — pure reader of the diagnosis's structured
    ``module_fix`` declaration: validates the literal ``module_statement`` and
    routes per file type (imports for Python/Go/JS-TS, shebang/``source``/``set``
    for shell) — a routing decision; nothing is written here.
  • action_prepare_frame / action_rewrite_frame_turn / action_splice_frame —
    the INFERENCE frame editor. The model edits the file's non-symbol "frame"
    (docstring, imports, top-level statements, ``__main__``) while every
    function/class body is preserved verbatim and spliced back deterministically.
    The model chooses placement (guided by the per-language module_fix_placement
    hint); determinism lives in the routing and the pinned bodies. Any splice
    mismatch falls back to a full rewrite — never stuck, never corrupts a file.
"""

import ast as stdlib_ast
import logging
import re

from agent import languages
from agent.actions.refinement_actions import extract_code_from_response
from agent.models import StepInput, StepOutput
from agent.repomap import extract_file_symbols

logger = logging.getLogger(__name__)

# Distinctive sentinel that replaces a preserved symbol body in the frame view.
# The leading comment prefix is per-language (build_frame picks "#" / "//" so the
# placeholder is a real comment in the target file); the MARKER is constant, and
# _SENTINEL_RE matches it prefix-agnostically so splice round-trips any language.
_SENTINEL_MARKER = "⟦OUROBOROS-SYMBOL "
_SENTINEL_RE = re.compile(r"⟦OUROBOROS-SYMBOL (?P<name>[^⟧]+?)⟧")


def _import_present(content, statement):
    """True if an equivalent import already exists in content (idempotency)."""
    try:
        want = stdlib_ast.parse(statement).body[0]
    except (SyntaxError, IndexError):
        return False
    try:
        tree = stdlib_ast.parse(content)
    except SyntaxError:
        return statement.strip() in content
    want_names = {a.name for a in getattr(want, "names", [])}
    for node in tree.body:
        if isinstance(want, stdlib_ast.Import) and isinstance(node, stdlib_ast.Import):
            if want_names <= {a.name for a in node.names}:
                return True
        if isinstance(want, stdlib_ast.ImportFrom) and isinstance(
            node, stdlib_ast.ImportFrom
        ):
            if node.module == getattr(want, "module", None) and want_names <= {
                a.name for a in node.names
            }:
                return True
    return False


def _statement_present(file_path, content, statement):
    """True if the module-level statement already exists (idempotency).

    Python *single* imports get the precise AST check (handles ``import a, b``
    merging, ``from x import y`` aliasing). Everything else — a shell shebang, a
    ``source``/``set`` line, a multi-line Go ``import ( … )`` block — uses
    stripped-line equality: present iff every non-blank line of the statement
    already appears as a line in the file. That avoids the substring false
    positive a bare ``in`` would hit (e.g. ``# source x.sh`` in a comment) and is
    best-effort for multi-line blocks; the splice parse-gate is the final net.
    """
    stmt = (statement or "").strip()
    if not stmt:
        return False
    if file_path.endswith(".py"):
        try:
            body = stdlib_ast.parse(stmt).body
        except SyntaxError:
            body = []
        if len(body) == 1 and isinstance(
            body[0], (stdlib_ast.Import, stdlib_ast.ImportFrom)
        ):
            return _import_present(content, stmt)
    want = [ln.strip() for ln in stmt.splitlines() if ln.strip()]
    have = {ln.strip() for ln in content.splitlines() if ln.strip()}
    return bool(want) and all(ln in have for ln in want)


# ── Frame build / splice (deterministic, model-free) ─────────────────
def build_frame(file_path, content):
    """Build the frame view + preserved bodies.

    Each TOP-LEVEL function/class body span is replaced by a single sentinel
    line (carrying its name + signature for the model's context); everything
    else (docstring, imports, top-level statements, ``__main__``) is verbatim.
    Returns (frame_text, preserved: {name: body}, ok, reason). ok=False (e.g.
    duplicate top-level names) means the caller should not frame-edit.
    """
    defs, _ = extract_file_symbols(file_path, content)
    syms = [
        d
        for d in defs
        if d.kind in ("function", "class")
        and not d.parent
        and d.line
        and d.end_line >= d.line
    ]
    names = [s.name for s in syms]
    if len(names) != len(set(names)):
        return content, {}, False, "duplicate top-level symbol names"

    prefix = languages.comment_prefix_for_path(file_path)  # "#" / "//" per language
    lines = content.splitlines(keepends=True)
    sym_by_start = {s.line: s for s in syms}
    preserved = {}
    out = []
    pos = 0  # 0-indexed line cursor
    n = len(lines)
    while pos < n:
        s = sym_by_start.get(pos + 1)
        if s is not None:
            preserved[s.name] = "".join(lines[s.line - 1 : s.end_line])
            sig = (s.signature or s.name).strip().replace("\n", " ")
            out.append(
                f"{prefix} {_SENTINEL_MARKER}{s.name}⟧ {sig}  (body preserved — keep this line)\n"
            )
            pos = s.end_line  # end_line is inclusive (1-indexed) → resume after it
        else:
            out.append(lines[pos])
            pos += 1
    return "".join(out), preserved, True, ""


def splice_frame(model_frame, preserved, file_path=""):
    """Replace each sentinel line in the model's edited frame with its preserved
    body. Returns (content, ok, reason). ok=False → caller falls back to rewrite.

    Validates: every preserved symbol's sentinel appears exactly once, no unknown
    sentinels, and the spliced file parses — Python via stdlib ast (precise),
    other languages via tree-sitter error nodes, unknown grammars skipped.
    """
    out = []
    seen = set()
    for line in model_frame.splitlines(keepends=True):
        m = _SENTINEL_RE.search(line)
        if m:
            name = m.group("name").strip()
            if name not in preserved:
                return "", False, f"unknown symbol sentinel '{name}'"
            if name in seen:
                return "", False, f"duplicate sentinel '{name}'"
            seen.add(name)
            body = preserved[name]
            if not body.endswith("\n"):
                body += "\n"
            out.append(body)
        else:
            out.append(line)
    missing = set(preserved) - seen
    if missing:
        return "", False, f"missing sentinels: {sorted(missing)}"
    content = "".join(out)
    # Language-aware validity: Python via stdlib ast (precise); other grammars via
    # tree-sitter error nodes; unknown/unsupported grammar skips (the sentinel
    # round-trip above already guarantees every body is intact + placed once).
    if file_path.endswith(".py") or not file_path:
        try:
            stdlib_ast.parse(content)
        except SyntaxError as e:
            return "", False, f"parse error after splice: {e}"
    else:
        from agent.repomap import parse_has_error

        if parse_has_error(file_path, content) is True:
            return "", False, "parse error after splice (tree-sitter error nodes)"
    return content, True, ""


# ── Actions ──────────────────────────────────────────────────────────
def _ctx(step_input, key, default=""):
    return step_input.params.get(key) or step_input.context.get(key) or default


def _not_module_fix(observation: str) -> StepOutput:
    return StepOutput(
        result={"is_module_fix": False},
        observations=observation,
        context_updates={
            "module_statement": "",
            "module_directive": "",
            "module_fix_symbol_continue": False,
        },
    )


async def action_check_module_fix(step_input: StepInput) -> StepOutput:
    """Pure reader of the diagnosis's structured module-fix declaration.

    Honored only when diagnosis_kind == "module_fix". Validates the literal
    ``module_statement`` (for ``.py``, that it parses as module-level code; other
    languages are validated by the splice parse-gate downstream) plus an
    idempotency check, then routes to the module-frame editor for any file type —
    a Python/Go/JS-TS import, a shell shebang/``source``/``set`` line.
    Degenerate declarations (missing/unparseable statement, comments-only
    statement, line already present) fall through to normal symbol routing.
    Comment lines inside the statement are guidance smuggled in by the
    diagnosis, not code: they are stripped from the splice and appended to
    module_directive instead. When the diagnosis also names a target_symbol,
    module_fix_symbol_continue routes the pass onward to symbol routing after
    the frame edit (multi-part fixes). Pure routing — no file is written
    here. Publishes is_module_fix + module_statement/directive/continue-flag.

    NOTE: this action is on file_ops' hot path — read_target routes EVERY
    existing-file edit through it — so the ``kind != "module_fix"`` early-return
    below must stay first and cheap.
    """
    kind = _ctx(step_input, "diagnosis_kind")
    if kind != "module_fix":
        return _not_module_fix("not declared a module fix")

    target = _ctx(step_input, "target_file_path")
    file_content = _ctx(step_input, "file_content")
    stmt = _ctx(step_input, "module_statement").strip()

    # Diagnoses smuggle multi-part instructions into module_statement as
    # comment lines ("# Inside GameEngine.__init__: ..."). Comments parse
    # fine, so without this split the splice writes instructions-as-comments
    # into the file while the real second half of the fix is silently
    # dropped (2026-07-16 bossgame circular-import loop). Splice ONLY the
    # executable lines; carry the guidance into the editor directive.
    # Python-only: in shell files a leading-# line can be the fix itself
    # (shebang), and other languages don't use # comments.
    if target.endswith(".py"):
        lines = stmt.splitlines()
        guidance_lines = [ln for ln in lines if ln.lstrip().startswith("#")]
        code_stmt = "\n".join(
            ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")
        ).strip()
    else:
        guidance_lines = []
        code_stmt = stmt

    ok = bool(code_stmt) and bool(file_content)
    if ok and target.endswith(".py"):
        # Python precision: it must at least parse as module-level code (any
        # number of statements — a multi-import is fine). Other languages skip
        # fragment-parsing here; the splice tree-sitter gate validates the result.
        try:
            parsed = stdlib_ast.parse(code_stmt)
            ok = bool(parsed.body)  # comments-only → no executable statements
        except SyntaxError:
            ok = False
    if not ok:
        logger.warning(
            "check_module_fix: kind=module_fix but module_statement %r "
            "has no usable executable statement for %s — falling through "
            "to symbol routing",
            stmt,
            target,
        )
        return _not_module_fix(
            f"module_fix declared but statement unusable ({stmt!r}) — symbol routing"
        )
    if _statement_present(target, file_content, code_stmt):
        return _not_module_fix(f"module-level line already present: {code_stmt}")

    placement = languages.module_fix_placement_for_path(target)
    directive = f"Add the missing module-level line `{code_stmt}` to this file."
    if placement:
        directive += f" Place it {placement}."
    if guidance_lines:
        directive += "\nDiagnosis guidance (context, not code to insert):\n" + "\n".join(
            guidance_lines
        )
    # Multi-part fixes: when the diagnosis ALSO names a concrete symbol to
    # change, the module line is only half the fix — after a successful
    # module-frame edit, file_ops routes onward to symbol routing (patch)
    # instead of finishing the pass. Symbol-less remainders can't do this
    # (patch structurally requires a target symbol).
    symbol_continue = bool(_ctx(step_input, "target_symbol").strip())
    logger.info(
        "check_module_fix: module-class fix for %s → %s%s",
        target,
        code_stmt,
        " (+ symbol continuation)" if symbol_continue else "",
    )
    return StepOutput(
        result={"is_module_fix": True},
        observations=f"module-class fix: {code_stmt}",
        context_updates={
            "module_statement": code_stmt,
            "module_directive": directive,
            "module_fix_symbol_continue": symbol_continue,
        },
    )


async def action_prepare_frame(step_input: StepInput) -> StepOutput:
    """Build the frame view (bodies → sentinels) + preserve original bodies."""
    file_path = _ctx(step_input, "target_file_path") or _ctx(step_input, "file_path")
    content = _ctx(step_input, "file_content")
    frame_text, preserved, ok, reason = build_frame(file_path, content)
    if not ok:
        logger.warning("prepare_frame: cannot frame-edit %s (%s)", file_path, reason)
    return StepOutput(
        result={"frame_ready": ok, "frame_reason": reason},
        observations=f"frame: {len(preserved)} bodies preserved" if ok else reason,
        context_updates={
            "frame_text": frame_text,
            "preserved_bodies": preserved,
        },
    )


def _frame_lang(file_path: str) -> tuple[str, str]:
    """(display label, code-fence) for the frame prompt — delegates to the
    canonical language registry. Unknown → ('file', ''); the extractor strips
    any fence regardless."""
    return languages.frame_label_and_fence(file_path)


_FRAME_INSTRUCTION = (
    "You are editing the FRAME of a {label} — the top-level code OUTSIDE any "
    "function/class body. {directive}\n\n"
    "Rules:\n"
    "- Edit ONLY top-level code: the file header/shebang, imports or includes, "
    "top-level statements, and any entry-point block. Do NOT touch function or "
    "class bodies.\n"
    "- Lines containing the `⟦OUROBOROS-SYMBOL ...⟧` placeholder stand in for "
    "function/class bodies that are preserved elsewhere. Keep each such line "
    "EXACTLY as-is — do not edit, remove, reorder, or add them.\n"
    "- Return the COMPLETE edited frame as one code block.\n\n"
    "Frame:\n```{fence}\n{frame}\n```"
)


async def action_rewrite_frame_turn(step_input: StepInput) -> StepOutput:
    """Single inference turn: the model edits the frame per the directive."""
    effects = step_input.effects
    file_path = _ctx(step_input, "target_file_path") or _ctx(step_input, "file_path")
    frame_text = _ctx(step_input, "frame_text")
    directive = _ctx(step_input, "module_directive") or _ctx(
        step_input, "flow_directive"
    )
    label, fence = _frame_lang(file_path)
    prompt = _FRAME_INSTRUCTION.format(
        label=label, fence=fence, directive=directive, frame=frame_text
    )

    model_frame = ""
    if effects:
        try:
            result = await effects.run_inference(
                prompt=prompt, config_overrides={"temperature": 0.2}
            )
            if getattr(result, "error", None):
                logger.warning("rewrite_frame_turn inference error: %s", result.error)
            model_frame = extract_code_from_response(result.text or "")
        except Exception as e:  # noqa: BLE001
            logger.warning("rewrite_frame_turn failed: %s", e)

    ok = bool(model_frame.strip())
    return StepOutput(
        result={"frame_edited": ok},
        observations=(
            f"frame edit: {len(model_frame)} chars" if ok else "empty frame edit"
        ),
        context_updates={"model_frame": model_frame},
    )


async def action_splice_frame(step_input: StepInput) -> StepOutput:
    """Re-insert preserved bodies into the edited frame, validate, write.

    On any mismatch (missing/unknown/duplicate sentinel or parse error) signals
    splice_failed=True so the flow falls back to a full rewrite.
    """
    effects = step_input.effects
    file_path = _ctx(step_input, "target_file_path") or _ctx(step_input, "file_path")
    model_frame = _ctx(step_input, "model_frame")
    preserved = step_input.context.get("preserved_bodies") or {}

    if not model_frame.strip():
        return StepOutput(
            result={"status": "full_rewrite_requested", "splice_failed": True},
            observations="empty frame edit → fall back to rewrite",
            context_updates={},
        )

    content, ok, reason = splice_frame(model_frame, preserved, file_path)
    if not ok:
        logger.warning("splice_frame: %s → falling back to rewrite", reason)
        return StepOutput(
            result={"status": "full_rewrite_requested", "splice_failed": True},
            observations=f"splice failed ({reason}) → rewrite",
            context_updates={},
        )

    wrote = True
    if effects:
        wr = await effects.write_file(file_path, content)
        wrote = getattr(wr, "success", True)
    if not wrote:
        return StepOutput(
            result={"status": "failed"},
            observations="frame splice write failed",
            context_updates={},
        )

    logger.info(
        "splice_frame: module frame edited + bodies preserved for %s", file_path
    )
    return StepOutput(
        result={"status": "success"},
        observations=f"module frame edited: {file_path}",
        context_updates={
            "files_changed": [file_path],
            "file_content_updated": content,
            "write_action": "module_frame_edit",
            "edit_summary": f"Edited module frame of {file_path} (bodies preserved)",
        },
    )
