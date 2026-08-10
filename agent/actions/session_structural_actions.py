"""Session structural creation — one file per TURN, checked between turns.

WHY THIS MODE EXISTS. Seam bugs are the field's most prevalent decisive defect,
and their origin is batch time. On the gpt-oss-medium artifact the save/load
seam a blind judge called decisive had ZERO repair records — never diagnosed,
never targeted, written by the batch generation and never touched again. The
corpus says it out loud: "nine files written TOGETHER in one shared-context
generation disagree in FIVE places. Shared context is necessary for cross-file
coherence, not sufficient."

The swarm has an entity-id registry that would help, and it was never ported
here. Not because it is infeasible — it is one turn producing a pruned
defines/references map (``build_contracts.cue:78`` -> ``store_data_registry``),
and it could be pasted into a batch prompt tomorrow. It would not WORK there:
batch is one completion, so the contract is read at token 0 and nothing
re-asserts it at file five. A CONTRACT WITH NO CHECKPOINT IS A SUGGESTION. In
the swarm it binds because each isolated worker has nothing else.

This mode supplies the missing checkpoint. Files are generated one per turn
inside ONE inference session, so each file is written with its real siblings in
context (not a prediction of them), and between every pair of turns a
deterministic check runs against BOTH the architecture's contracts and the
vocabulary the earlier files actually declared.

Two checks here are new because two blind spots are proven:

  * ``_serialized_roundtrip_violations`` — ``_transfer_shape_violations``
    (batch_structural_actions.py) only indexes producers that return dict
    LITERALS, so a contract mediated by a FILE (``save_state`` dumps ->
    ``load_state`` returns ``json.load(f)``) has nothing to bind. That is
    exactly the seam that shipped.
  * ``_data_registry_violations`` — nothing anywhere re-reads a written data
    file to confirm its ids. The registry is enforced by prompt injection only.

Everything else is reuse: the blueprint renderer, the slice/write primitives,
the per-file and cross-module gates, and the goal bookkeeping are all the
batch path's, so the A/B measures the generation strategy and not a second
difference.
"""

from __future__ import annotations

import logging
from typing import Any

from agent import languages
from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

# Repairs per file before the walk moves on. Bounded twice — here and as a
# `meta.attempt` stop in the CUE resolver — because this is the FIRST per-item
# retry in any list walk in the codebase (load_next_file, rewrite_symbol_turn
# and the quality-gate probe walk all record-and-skip), and an unbounded
# repair loop is the shape that produced a 12-round live-lock on 2026-08-10.
_SESSION_REPAIR_ATTEMPTS = 2

# How much observed vocabulary to carry into a turn. The session already holds
# every earlier file verbatim; this block is the DETERMINISTIC index of it, not
# a replacement for it, so it is a digest rather than a dump.
_VOCAB_CHAR_CAP = 4000


# ══════════════════════════════════════════════════════════════════════
# The two checks the batch path structurally cannot make
# ══════════════════════════════════════════════════════════════════════


def _dict_keys(node: Any) -> set[str] | None:
    """String keys of a dict literal, or None if it is not one / is dynamic."""
    import ast as stdlib_ast

    if not isinstance(node, stdlib_ast.Dict):
        return None
    keys: set[str] = set()
    for k in node.keys:
        if k is None:  # ** unpacking — the key set is not knowable
            return None
        if not (isinstance(k, stdlib_ast.Constant) and isinstance(k.value, str)):
            return None
        keys.add(k.value)
    return keys


def _serializer_functions(sources: dict[str, str]) -> tuple[set[str], set[str]]:
    """(writer_names, reader_names) — functions that json.dump / json.load.

    Bare names, because that is how they are imported and called
    (`from save_load import save_state, load_state`). A function is a writer
    if its body reaches `json.dump(s)`, a reader if it reaches `json.load(s)`.
    """
    import ast as stdlib_ast

    writers: set[str] = set()
    readers: set[str] = set()
    for src in sources.values():
        try:
            tree = stdlib_ast.parse(src)
        except SyntaxError:
            continue
        for fn in stdlib_ast.walk(tree):
            if not isinstance(
                fn, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)
            ):
                continue
            for n in stdlib_ast.walk(fn):
                f = getattr(n, "func", None)
                if (
                    isinstance(n, stdlib_ast.Call)
                    and isinstance(f, stdlib_ast.Attribute)
                    and isinstance(f.value, stdlib_ast.Name)
                    and f.value.id == "json"
                ):
                    if f.attr in ("dump", "dumps"):
                        writers.add(fn.name)
                    elif f.attr in ("load", "loads"):
                        readers.add(fn.name)
    return writers, readers


def _roundtrip_keys(
    src: str, writers: set[str], readers: set[str]
) -> tuple[set[str], set[str]]:
    """(written, read) payload keys in ONE module, following both hops.

    Hop one: `state = {...}` then `save_state(state)` — the payload is built
    here and serialized elsewhere. Hop two: `state = load_state()` then
    `state["k"]` — the payload is deserialized elsewhere and consumed here.
    Also covers the direct shape, where the same function both builds the dict
    and calls json.dump on it.
    """
    import ast as stdlib_ast

    try:
        tree = stdlib_ast.parse(src)
    except SyntaxError:
        return set(), set()

    written: set[str] = set()
    read: set[str] = set()

    for fn in stdlib_ast.walk(tree):
        if not isinstance(fn, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
            continue
        dict_vars: dict[str, set[str]] = {}
        payload_vars: set[str] = set()
        direct_write = any(
            isinstance(n, stdlib_ast.Call)
            and isinstance(n.func, stdlib_ast.Attribute)
            and isinstance(n.func.value, stdlib_ast.Name)
            and n.func.value.id == "json"
            and n.func.attr in ("dump", "dumps")
            for n in stdlib_ast.walk(fn)
        )

        for n in stdlib_ast.walk(fn):
            if isinstance(n, stdlib_ast.Assign) and len(n.targets) == 1:
                tgt = n.targets[0]
                if not isinstance(tgt, stdlib_ast.Name):
                    continue
                keys = _dict_keys(n.value)
                if keys is not None:
                    dict_vars[tgt.id] = keys
                elif (
                    isinstance(n.value, stdlib_ast.Call)
                    and isinstance(n.value.func, stdlib_ast.Name)
                    and n.value.func.id in readers
                ):
                    payload_vars.add(tgt.id)

        for n in stdlib_ast.walk(fn):
            # writer_fn(payload) — the cross-module hop
            if (
                isinstance(n, stdlib_ast.Call)
                and isinstance(n.func, stdlib_ast.Name)
                and n.func.id in writers
                and n.args
            ):
                a = n.args[0]
                if isinstance(a, stdlib_ast.Name) and a.id in dict_vars:
                    written |= dict_vars[a.id]
                else:
                    lit = _dict_keys(a)
                    if lit:
                        written |= lit
            # payload["k"] / payload.get("k") — the consumer hop
            if isinstance(n, stdlib_ast.Subscript) and isinstance(
                n.value, stdlib_ast.Name
            ):
                s = n.slice
                if (
                    n.value.id in payload_vars
                    and isinstance(s, stdlib_ast.Constant)
                    and isinstance(s.value, str)
                ):
                    read.add(s.value)
            if (
                isinstance(n, stdlib_ast.Call)
                and isinstance(n.func, stdlib_ast.Attribute)
                and n.func.attr == "get"
                and isinstance(n.func.value, stdlib_ast.Name)
                and n.func.value.id in payload_vars
                and n.args
                and isinstance(n.args[0], stdlib_ast.Constant)
                and isinstance(n.args[0].value, str)
            ):
                read.add(n.args[0].value)

        if direct_write:
            for keys in dict_vars.values():
                written |= keys

    return written, read


def _serialized_roundtrip_violations(sources: dict[str, str]) -> list[str]:
    """Keys written to a serialized payload that nothing ever reads back.

    THE CHECK THAT WOULD HAVE CAUGHT THE SHIPPED SEAM. `handle_save`
    serialized four world tables; `handle_load` read none of them. Both sides
    were valid Python, both agreed on every call shape, and `save_load.py`
    typed the payload `Dict[str, Any]` — the seam lived entirely inside the
    `Any`, so every existing gate passed it. The save FILE was correct; the
    reader ignored it.

    IT MUST FOLLOW TWO HOPS. The first version of this check looked for the
    dict literals in the same module as the `json` call and found nothing on
    the very artifact it was written for — because `game.py` builds the
    payload and `save_load.py` does the I/O. That indirection is precisely
    what defeats `_transfer_shape_violations`, so reproducing it would have
    shipped a check that passes its own motivating case.

    Reported one-way only (written-never-read). The reverse is a legitimate
    shape for optional keys with defaults.
    """
    py = {p: s for p, s in sources.items() if p.endswith(".py")}
    if len(py) < 2:
        return []
    writers, readers = _serializer_functions(py)
    if not writers or not readers:
        return []

    written: set[str] = set()
    read: set[str] = set()
    writer_files: list[str] = []
    reader_files: list[str] = []
    for path, src in py.items():
        w, r = _roundtrip_keys(src, writers, readers)
        if w:
            written |= w
            writer_files.append(path)
        if r:
            read |= r
            reader_files.append(path)

    orphaned = sorted(k for k in written - read if not k.startswith("_"))
    if not orphaned or not writer_files:
        return []
    return [
        f"serialized round trip: {', '.join(orphaned[:12])} "
        f"{'is' if len(orphaned) == 1 else 'are'} written into the saved "
        f"payload ({', '.join(sorted(writer_files)[:3])}) and never read back "
        f"({', '.join(sorted(reader_files)[:3]) or 'no reader consumes the '
          'loaded payload at all'}). A save whose loader ignores what the "
        f"writer stored restores an incomplete world."
    ]


def _data_registry_violations(
    data_sources: dict[str, str], registry: list[dict] | None
) -> list[str]:
    """Re-read written data files and hold them to the id registry.

    The registry is authored once, before generation, and broadcast into
    prompts. NOTHING has ever read a file back to confirm the ids landed —
    which makes it advice, not a contract. This is the verification half.
    """
    if not registry or not data_sources:
        return []
    from agent.data_ops import Document, DataOpsError, detect_fmt

    present: dict[str, set[str]] = {}
    for path, content in data_sources.items():
        try:
            doc = Document.read(content, detect_fmt(path, content))
        except DataOpsError:
            continue  # unparseable is the syntax gate's business, not ours
        ids: set[str] = set()
        for node in doc.walk():
            ptr = node.pointer
            if not ptr:
                continue
            tail = ptr.rsplit("/", 1)[-1]
            if tail and not tail.isdigit():
                ids.add(tail)
            if isinstance(node.value, dict) and isinstance(node.value.get("id"), str):
                ids.add(node.value["id"])
        present[path] = ids

    out: list[str] = []
    for entry in registry:
        path = str(entry.get("file") or "")
        if path not in present:
            continue  # not written yet — the walk has not reached it
        declared = {str(i) for i in (entry.get("defines") or [])}
        missing = sorted(declared - present[path])
        if missing:
            out.append(
                f"entity registry: {path} was to define "
                f"{', '.join(missing[:10])} — not present in the file as "
                f"written. Siblings are being told to reference these ids."
            )
        for sibling, ids in (entry.get("references") or {}).items():
            if sibling not in present:
                continue
            dangling = sorted({str(i) for i in ids} - present[sibling])
            if dangling:
                out.append(
                    f"entity registry: {path} references "
                    f"{', '.join(dangling[:10])} in {sibling}, which does not "
                    f"define them."
                )
    return out


# ══════════════════════════════════════════════════════════════════════
# The binding vocabulary — contracts AND what is actually on disk
# ══════════════════════════════════════════════════════════════════════


def _observed_symbols(sources: dict[str, str]) -> str:
    """A signature index of what the already-written files declare.

    NOT `contract_swarm._project_digest` — that renders a `.pyi` from a
    CONTRACT SET (`stub_text` per module), which is the swarm's pre-generation
    artefact and does not exist here. The whole point of this mode is that the
    vocabulary comes off disk, so it is extracted from the written bytes with
    the same tree-sitter reader the repomap uses.
    """
    if not sources:
        return ""
    from agent.repomap import extract_file_symbols

    _KINDS = {"class", "function", "method"}
    lines: list[str] = []
    for path in sorted(sources):
        try:
            defs, _refs = extract_file_symbols(path, sources[path])
        except Exception:  # noqa: BLE001 — a digest miss weakens the block, not the run
            logger.debug("session vocabulary: %s not indexable", path, exc_info=True)
            continue
        sigs: list[str] = []
        for d in defs:
            if getattr(d, "kind", "") not in _KINDS:
                continue
            sig = (getattr(d, "signature", "") or "").strip()
            parent = getattr(d, "parent", None)
            name = getattr(d, "name", "")
            label = f"{parent}.{name}" if parent else name
            sigs.append(sig if sig else label)
        if sigs:
            lines.append(f"### {path}\n" + "\n".join(f"  {s}" for s in sigs[:40]))
    return "\n".join(lines)


def _observed_ids(data_sources: dict[str, str]) -> str:
    """The literal id strings present in the data files already written.

    `extract_data_skeleton` deliberately renders ids as `<id>` placeholders,
    so it answers "what keys" and not "which ids" — and the id vocabulary is
    precisely where shadow_lord/shadow_lich lives. Document.walk is the only
    helper that surfaces the actual strings.
    """
    if not data_sources:
        return ""
    from agent.data_ops import Document, DataOpsError, detect_fmt

    lines: list[str] = []
    for path in sorted(data_sources):
        try:
            doc = Document.read(
                data_sources[path], detect_fmt(path, data_sources[path])
            )
        except DataOpsError:
            continue
        ids: list[str] = []
        for node in doc.walk():
            if isinstance(node.value, dict) and isinstance(node.value.get("id"), str):
                ids.append(node.value["id"])
            elif node.pointer.count("/") == 2:
                tail = node.pointer.rsplit("/", 1)[-1]
                if tail and not tail.isdigit():
                    ids.append(tail)
        seen: list[str] = []
        for i in ids:
            if i not in seen:
                seen.append(i)
        if seen:
            lines.append(f"{path} defines: {', '.join(seen[:40])}")
    return "\n".join(lines)


def _binding_vocabulary(
    mission: Any, written: dict[str, str], registry: list[dict] | None
) -> str:
    """Contracts + observed, as one prompt block.

    DISAGREEMENT RULE, and it is the whole point of deriving from disk: where
    the contract and the written files disagree, IDS take the written value
    (the file that exists is the fact; the contract was a prediction made
    before anything was written) and SIGNATURES take the contract (a caller
    not yet written must bind to the declared shape, not to whatever the first
    implementer happened to emit). The block states this so the model does not
    have to infer it.
    """
    from agent.actions.contract_swarm_actions import (
        _data_contracts,
        _data_digest,
        _registry_digest,
        _state_contracts,
        _state_digest,
    )

    code = {p: s for p, s in written.items() if not languages.is_data(_ext(p))}
    data = {p: s for p, s in written.items() if languages.is_data(_ext(p))}

    parts: list[str] = []

    contract_bits = [
        _data_digest(_data_contracts(mission)),
        _state_digest(_state_contracts(mission)),
        _registry_digest(registry),
    ]
    contract_text = "\n\n".join(b for b in contract_bits if b)
    if contract_text:
        parts.append(
            "## Declared contracts (authoritative for SIGNATURES)\n" + contract_text
        )

    sym = _observed_symbols(code)
    ids = _observed_ids(data)
    if sym or ids:
        observed = "\n\n".join(b for b in (sym, ids) if b)
        parts.append(
            "## Already written, and therefore BINDING (authoritative for IDS "
            "and NAMES)\n"
            "These files exist on disk now. Call them exactly as they are — do "
            "not invent a variant spelling, and do not assume a symbol you "
            "cannot see here.\n\n" + observed
        )

    if not parts:
        return ""
    block = "\n\n".join(parts)
    if len(block) > _VOCAB_CHAR_CAP:
        block = block[:_VOCAB_CHAR_CAP].rstrip() + "\n… (truncated)"
    return block


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


# ══════════════════════════════════════════════════════════════════════
# Actions
# ══════════════════════════════════════════════════════════════════════


async def action_open_structural_session(step_input: StepInput) -> StepOutput:
    """Open the ONE session the whole file walk runs inside.

    There is no generic opener in the codebase — every family has its own that
    also builds and queues the seed (start_diagnosis_session,
    open_search_session, open_escalation_session). This follows that contract:

      * the SAME id is published under two keys. `inference_session_id` is
        ambient (`runtime._AMBIENT_CONTEXT_KEYS`) so turn steps route into the
        session without declaring it — the filter that once made diagnose
        turns silently stateless. `structural_session_id` is the flow-scoped
        alias that steps DO declare, which is what keeps the dependency
        lint-visible; ambient status deliberately does not satisfy a
        `required`.
      * the seed rides `session_injections`, consumed once before the first
        turn's retry loop rather than re-sent per turn.

    The seed is the SAME blueprint the batch prompt gets
    (`render_batch_blueprint`), so the two modes start from identical
    information and the A/B measures the walk, not the briefing.
    """
    from agent.renderers import render_batch_blueprint
    from agent.session_injections import queue as queue_injection

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not effects:
        return StepOutput(
            result={"session_started": False},
            observations="No effects interface — cannot open a structural session",
        )

    arch = getattr(mission, "architecture", None)
    try:
        blueprint = render_batch_blueprint({"source": arch}, {}) or ""
    except Exception:  # noqa: BLE001 — an unrenderable blueprint is still workable
        logger.debug("session open: blueprint render failed", exc_info=True)
        blueprint = ""

    try:
        session_id = await effects.start_inference_session({"ttl_seconds": 1800})
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to open structural session: %s", e)
        return StepOutput(
            result={"session_started": False},
            observations=f"Failed to open structural session: {e}",
        )

    seed = (
        "You are about to write a project one file at a time, in dependency "
        "order. Every file you write stays in front of you, so each later file "
        "must bind to the names the earlier ones actually declared — not to a "
        "plausible variant of them.\n\n" + blueprint
    )
    context_updates: dict[str, Any] = {
        "structural_session_id": session_id,
        "inference_session_id": session_id,
        "session_files_written": [],
        "files_changed": [],
    }
    queue_injection(context_updates, step_input.context, seed)
    logger.info(
        "structural session opened: %s (blueprint seed %d chars)",
        session_id,
        len(blueprint),
    )
    return StepOutput(
        result={"session_started": True},
        observations=f"Structural session opened: {session_id}",
        context_updates=context_updates,
    )


async def _read_written(effects, paths: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in paths:
        try:
            fc = await effects.read_file(p)
            if getattr(fc, "exists", False):
                out[p] = getattr(fc, "content", "") or ""
        except Exception:  # noqa: BLE001 — an unreadable file simply is not indexed
            continue
    return out


async def action_session_next_file(step_input: StepInput) -> StepOutput:
    """Pop the next file off the walk and assemble its binding vocabulary.

    Cursor lives in CONTEXT (`pending_files`), never a mission field and never
    an integer index — the idiom `patch.cue`'s cross-file walk uses. Seeded on
    first call from the architecture's ordered file list, so the walk follows
    `creation_order`: dependencies before dependents, which is what makes
    "bind to what is already written" mean anything.

    Publishes: pending_files, session_files_written, current_file,
    binding_vocabulary. Result: has_next.
    """
    effects = step_input.effects
    ctx = step_input.context
    mission = ctx.get("mission")

    pending = ctx.get("pending_files")
    written_paths = list(ctx.get("session_files_written") or [])

    if pending is None:
        from agent.actions.mission_actions import _get_sweep_files

        arch = getattr(mission, "architecture", None)
        pending = list(_get_sweep_files(arch)) if arch is not None else []
        logger.info("session walk: %d file(s) in creation order", len(pending))
    pending = list(pending)

    if not pending:
        # THE MANIFEST IS THE BOOKKEEPING CONTRACT, not telemetry.
        # apply_batch_results reads `batch_manifest["written"]` to decide which
        # structural goal each file belongs to; with no manifest it skips every
        # goal, returns wrote_any=False, and the flow reports FAILURE over a
        # complete artifact. That is exactly what happened on this mode's first
        # live run: nine files on disk, nine goals still incomplete, zero
        # reports booked. Reusing batch's bookkeeping means supplying the
        # contract batch supplies, not merely calling the same action.
        from agent.actions.mission_actions import _get_sweep_files

        arch = getattr(mission, "architecture", None)
        declared = list(_get_sweep_files(arch)) if arch is not None else []
        manifest = {
            "written": list(written_paths),
            "missing": [f for f in declared if f not in written_paths],
            "extra": [],
            "truncated": False,
            "salvaged": False,
            "deliberation_chars": 0,
            "attempts": 1,
            "fallback_rung": "session walk",
        }
        logger.info(
            "session walk: complete — %d/%d file(s) written%s",
            len(written_paths),
            len(declared) or len(written_paths),
            (
                f", missing {', '.join(manifest['missing'][:6])}"
                if manifest["missing"]
                else ""
            ),
        )
        return StepOutput(
            result={"has_next": False},
            observations=(
                f"session walk: complete — {len(written_paths)} file(s) written"
            ),
            context_updates={
                "pending_files": [],
                "session_files_written": written_paths,
                "batch_manifest": manifest,
            },
        )

    current = pending.pop(0)
    written = await _read_written(effects, written_paths) if effects else {}
    registry = ctx.get("data_registry") or []
    vocabulary = _binding_vocabulary(mission, written, registry)

    logger.info(
        "session walk: file %d/%d — %s (%d sibling(s) binding)",
        len(written_paths) + 1,
        len(written_paths) + 1 + len(pending),
        current,
        len(written),
    )
    return StepOutput(
        result={"has_next": True},
        observations=(
            f"session walk: generating {current} "
            f"({len(pending)} remaining, {len(written)} sibling(s) binding)"
        ),
        context_updates={
            "pending_files": pending,
            "session_files_written": written_paths,
            "current_file": current,
            "binding_vocabulary": vocabulary,
            # A fresh file starts its own repair budget.
            "session_repairs": 0,
        },
    )


async def action_write_session_file(step_input: StepInput) -> StepOutput:
    """Write the one file this turn produced.

    Same primitives as the batch path — `parse_file_blocks` then
    `guarded_write_file` (anti-gut retention floor + the scaffold parse floor)
    — so a session-written file is held to exactly the batch standard.
    Anything the turn emits under a path other than `current_file` is dropped:
    the walk owns which file is being written, not the model.

    Publishes: files_changed, session_files_written, file_written.
    Result: write_success.
    """
    from agent.actions.file_ops_actions import guarded_write_file
    from agent.markdown_fence import parse_file_blocks

    effects = step_input.effects
    ctx = step_input.context
    current = str(ctx.get("current_file") or "")
    raw = str(ctx.get("inference_response") or "")
    written_paths = list(ctx.get("session_files_written") or [])
    changed = list(ctx.get("files_changed") or [])

    if not current or not effects:
        return StepOutput(
            result={"write_success": False},
            observations="session write: no target file or effects",
        )

    blocks = parse_file_blocks(raw)
    body = ""
    extra: list[str] = []
    for path, content in blocks:
        norm = str(path or "").lstrip("./")
        if norm == current.lstrip("./") and not body:
            body = content
        else:
            extra.append(norm)
    # A single unlabelled fence is the common shape when only one file was
    # asked for — take it rather than lose the turn to a missing marker.
    if not body and len(blocks) == 1:
        body = blocks[0][1]
    if not body:
        return StepOutput(
            result={"write_success": False},
            observations=(
                f"session write: the turn produced no block for {current}"
                + (f" (saw {', '.join(extra[:4])})" if extra else "")
            ),
        )

    ok, err = await guarded_write_file(effects, current, body)
    if not ok:
        return StepOutput(
            result={"write_success": False},
            observations=f"session write: {current} refused — {err or 'guard'}",
        )
    if extra:
        logger.info(
            "session write: dropped %d off-target block(s) from the %s turn: %s",
            len(extra),
            current,
            ", ".join(extra[:4]),
        )
    if current not in written_paths:
        written_paths.append(current)
    if current not in changed:
        changed.append(current)
    return StepOutput(
        result={"write_success": True},
        observations=f"session write: {current}",
        context_updates={
            "files_changed": changed,
            "session_files_written": written_paths,
            "file_written": current,
        },
    )


async def action_check_session_file(step_input: StepInput) -> StepOutput:
    """Gate the file just written, and the fileset it now belongs to.

    THE CHECKPOINT. Reuses the batch path's own gates so the standard is
    identical — `run_batch_file_checks` (syntax/lint/env per file, plus the
    data-boundary and in-process transfer-shape checks) and
    `run_contract_typecheck` (call shape across modules) — then adds the two
    checks no existing gate can make: the file-mediated round trip and the
    id registry read back off disk.

    Publishes: batch_check_results, violations. Result: file_ok, repairs_left.
    """
    from agent.actions.batch_structural_actions import action_run_batch_file_checks
    from agent.actions.contract_swarm_actions import action_run_contract_typecheck

    effects = step_input.effects
    ctx = step_input.context
    current = str(ctx.get("current_file") or "")
    written_paths = list(ctx.get("session_files_written") or [])
    repairs = int(ctx.get("session_repairs") or 0)

    if not current or not effects:
        return StepOutput(
            result={"file_ok": True, "repairs_left": 0},
            observations="session check: nothing to check",
        )

    # Run the shared gates over EVERY file written so far, not just this one:
    # a new file is exactly what can break an earlier file's assumption, and
    # the cross-module checks are only meaningful over the whole set.
    shared = StepInput(
        context={"files_changed": written_paths, "mission": ctx.get("mission")},
        params={},
        meta=step_input.meta,
        effects=effects,
    )
    per_file_out = await action_run_batch_file_checks(shared)
    results = dict(per_file_out.context_updates.get("batch_check_results") or {})

    typed = StepInput(
        context={"files_changed": written_paths, "batch_check_results": results},
        params={},
        meta=step_input.meta,
        effects=effects,
    )
    typed_out = await action_run_contract_typecheck(typed)
    results = dict(typed_out.context_updates.get("batch_check_results") or results)

    sources = await _read_written(effects, written_paths)
    code = {p: s for p, s in sources.items() if p.endswith(".py")}
    data = {p: s for p, s in sources.items() if languages.is_data(_ext(p))}

    cross: list[str] = []
    cross.extend(_serialized_roundtrip_violations(code))
    cross.extend(_data_registry_violations(data, ctx.get("data_registry") or []))

    entry = results.setdefault(
        current, {"passed": True, "checks_failed": [], "output": ""}
    )
    own = list(entry.get("checks_failed") or [])
    violations = [f"{current}: {c}" for c in own] + cross
    if cross:
        entry["passed"] = False
        entry["checks_failed"] = own + ["cross_file"]
        entry["output"] = (entry.get("output") or "") + "\n" + "\n".join(cross)

    file_ok = bool(entry.get("passed", True)) and not cross
    repairs_left = max(0, _SESSION_REPAIR_ATTEMPTS - repairs)
    if not file_ok:
        logger.info(
            "session check: %s FAILED (%d violation(s), %d repair(s) left)",
            current,
            len(violations),
            repairs_left,
        )
    return StepOutput(
        result={"file_ok": file_ok, "repairs_left": repairs_left},
        observations=(
            f"session check: {current} {'ok' if file_ok else 'failed'}"
            + (f" — {len(violations)} violation(s)" if violations else "")
        ),
        context_updates={
            "batch_check_results": results,
            "violations": violations,
            "session_repairs": repairs + (0 if file_ok else 1),
        },
    )
