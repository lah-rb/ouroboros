"""Module-frame editing: a structured import-fix router + an inference frame editor.

When a fix is import-class (a missing *module-level* import), the symbol-scoped
patch flow can't express it — it only rewrites a named function/class body, where
a top-level ``import`` can never live (this trapped Mistral in a 6× loop). This
module keeps the project's "deterministic routing + inference edits" split:

  • action_check_import_fix — pure reader of the diagnosis's structured
    ``import_fix`` declaration: validates the literal ``import_statement``
    (a routing decision; nothing is written here).
  • action_prepare_frame / action_rewrite_frame_turn / action_splice_frame —
    the INFERENCE frame editor. The model edits the file's non-symbol "frame"
    (docstring, imports, top-level statements, ``__main__``) while every
    function/class body is preserved verbatim and spliced back deterministically.
    The model chooses placement (after docstrings / ``from __future__`` / etc.);
    determinism lives in the routing and the pinned bodies. Any splice mismatch
    falls back to a full rewrite, so it is never stuck and never corrupts a file.
"""

import ast as stdlib_ast
import logging
import re

from agent.actions.refinement_actions import extract_code_from_response
from agent.models import StepInput, StepOutput
from agent.repomap import extract_file_symbols

logger = logging.getLogger(__name__)

# Distinctive sentinel that replaces a preserved symbol body in the frame view.
_SENTINEL_PREFIX = "# ⟦OUROBOROS-SYMBOL "
_SENTINEL_RE = re.compile(r"^\s*#\s*⟦OUROBOROS-SYMBOL (?P<name>[^⟧]+?)⟧")


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
                f"{_SENTINEL_PREFIX}{s.name}⟧ {sig}  (body preserved — keep this line)\n"
            )
            pos = s.end_line  # end_line is inclusive (1-indexed) → resume after it
        else:
            out.append(lines[pos])
            pos += 1
    return "".join(out), preserved, True, ""


def splice_frame(model_frame, preserved):
    """Replace each sentinel line in the model's edited frame with its preserved
    body. Returns (content, ok, reason). ok=False → caller falls back to rewrite.

    Validates: every preserved symbol's sentinel appears exactly once, no unknown
    sentinels, and the spliced file parses.
    """
    out = []
    seen = set()
    for line in model_frame.splitlines(keepends=True):
        m = _SENTINEL_RE.match(line)
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
    try:
        stdlib_ast.parse(content)
    except SyntaxError as e:
        return "", False, f"parse error after splice: {e}"
    return content, True, ""


# ── Actions ──────────────────────────────────────────────────────────
def _ctx(step_input, key, default=""):
    return step_input.params.get(key) or step_input.context.get(key) or default


def _not_import_fix(observation: str) -> StepOutput:
    return StepOutput(
        result={"is_import_fix": False},
        observations=observation,
        context_updates={"import_statement": "", "import_directive": ""},
    )


async def action_check_import_fix(step_input: StepInput) -> StepOutput:
    """Pure reader of the diagnosis's structured import-fix declaration.

    Honored only when diagnosis_kind == "import_fix". Validates the literal
    import_statement (ast.parse to exactly one Import/ImportFrom node) plus
    the idempotency check, then routes to the module-frame editor. Degenerate
    declarations (missing/unparseable statement, non-.py target, import
    already present) fall through to normal symbol routing. Pure routing —
    no file is written here. Publishes is_import_fix +
    import_statement/directive.
    """
    target = _ctx(step_input, "target_file_path")
    file_content = _ctx(step_input, "file_content")
    kind = _ctx(step_input, "diagnosis_kind")
    stmt = _ctx(step_input, "import_statement").strip()

    if kind != "import_fix":
        return _not_import_fix("not declared an import fix")

    ok = bool(stmt) and target.endswith(".py") and bool(file_content)
    if ok:
        try:
            tree = stdlib_ast.parse(stmt)
            ok = len(tree.body) == 1 and isinstance(
                tree.body[0], (stdlib_ast.Import, stdlib_ast.ImportFrom)
            )
        except SyntaxError:
            ok = False
    if not ok:
        logger.warning(
            "check_import_fix: kind=import_fix but import_statement %r "
            "missing/unparseable for %s — falling through to symbol routing",
            stmt,
            target,
        )
        return _not_import_fix(
            f"import_fix declared but statement unusable ({stmt!r}) — symbol routing"
        )
    if _import_present(file_content, stmt):
        return _not_import_fix(f"import already present: {stmt}")

    directive = f"Add the missing module-level import `{stmt}` to this file."
    logger.info("check_import_fix: import-class fix for %s → %s", target, stmt)
    return StepOutput(
        result={"is_import_fix": True},
        observations=f"import-class fix: {stmt}",
        context_updates={"import_statement": stmt, "import_directive": directive},
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


_FRAME_INSTRUCTION = (
    "You are editing the MODULE FRAME of a Python file. {directive}\n\n"
    "Rules:\n"
    "- Edit ONLY module-level code: the docstring, imports, top-level statements, "
    'and the `if __name__ == "__main__":` block.\n'
    "- Lines beginning with `# ⟦OUROBOROS-SYMBOL ...⟧` are placeholders for "
    "function/class bodies that are preserved elsewhere. Keep each such line "
    "EXACTLY as-is — do not edit, remove, reorder, or add them.\n"
    "- Return the COMPLETE edited frame as one Python code block.\n\n"
    "Frame:\n```python\n{frame}\n```"
)


async def action_rewrite_frame_turn(step_input: StepInput) -> StepOutput:
    """Single inference turn: the model edits the frame per the directive."""
    effects = step_input.effects
    frame_text = _ctx(step_input, "frame_text")
    directive = _ctx(step_input, "import_directive") or _ctx(
        step_input, "flow_directive"
    )
    prompt = _FRAME_INSTRUCTION.format(directive=directive, frame=frame_text)

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

    content, ok, reason = splice_frame(model_frame, preserved)
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
