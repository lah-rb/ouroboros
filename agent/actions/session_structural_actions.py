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

  * the serialized round trip (``roundtrip_contract.py``) —
    ``_transfer_shape_violations`` (batch_structural_actions.py) only indexes
    producers that return dict LITERALS, so a contract mediated by a FILE
    (``save_state`` dumps -> ``load_state`` returns ``json.load(f)``) has
    nothing to bind. That is exactly the seam that shipped. It is held to the
    persisted shape the design DECLARED, and names the file to fix.
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


# ══════════════════════════════════════════════════════════════════════
# The two checks the batch path structurally cannot make
# ══════════════════════════════════════════════════════════════════════


def _implicated_file(message: str, written: list[str]) -> str:
    """The written file a cross-file violation names first, if any.

    Violation text carries its own provenance ("entity registry: data/world.yaml
    was to define …"), so the owner is recoverable without restructuring the
    checks' return type. The round-trip check does not come through here — it
    returns the file to fix alongside each message.
    """
    best, best_pos = "", len(message) + 1
    for path in written:
        if not path:
            continue
        i = message.find(path)
        if i >= 0 and i < best_pos:
            best, best_pos = path, i
    return best


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
                f"{', '.join(missing)} — not present in the file as "
                f"written. Siblings are being told to reference these ids."
            )
        for sibling, ids in (entry.get("references") or {}).items():
            if sibling not in present:
                continue
            dangling = sorted({str(i) for i in ids} - present[sibling])
            if dangling:
                out.append(
                    f"entity registry: {path} references "
                    f"{', '.join(dangling)} in {sibling}, which does not "
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
            lines.append(f"### {path}\n" + "\n".join(f"  {s}" for s in sigs))
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
            lines.append(f"{path} defines: {', '.join(seen)}")
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
    # NO length cap. There was one (4,000 chars, cut from the END), and the
    # contracts come first — on 2026-09-22 they filled it, and the "Already
    # written" index, the only deterministic account of what is ON DISK, was
    # cut whole. The repair turn that most needed save.py's real names
    # (state_to_dict/save_game/load_game) got none of them, argued from recall
    # against a prompt that said the finding was fact, and shipped 330 dead
    # lines. Evidence is not trimmed to fit a budget.
    return "\n\n".join(parts)


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
        truncated_files = list(ctx.get("session_truncated_files") or [])
        manifest = {
            "written": list(written_paths),
            "missing": [f for f in declared if f not in written_paths],
            "extra": [],
            "truncated": bool(truncated_files),
            "truncated_files": truncated_files,
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
    # Turns the ENGINE cut (budget or context ceiling). Recorded for the
    # manifest, which hardcoded truncated=False and so reported a walk whose
    # keystone turn died at the context ceiling as an ordinary one.
    truncated_files = list(ctx.get("session_truncated_files") or [])
    if ctx.get("inference_truncated") and current and current not in truncated_files:
        truncated_files.append(current)
        logger.warning(
            "session write: the %s turn was TRUNCATED by the engine", current
        )
    bookkeeping = {"session_truncated_files": truncated_files}

    if not current or not effects:
        return StepOutput(
            result={"write_success": False},
            observations="session write: no target file or effects",
            context_updates=bookkeeping,
        )

    # fallback_path makes this a single-file site, which it is by contract —
    # the walk owns the path, not the model. Without it, a fence missing its
    # FILE marker (muse put the marker ABOVE the fence, 2026-08-14) parses to
    # [] and the file is silently lost; the len(blocks)==1 rescue below can
    # never fire on an empty list, so its own comment described a dead branch.
    # FENCED turns only: parse_file_blocks' final fallback also wraps BARE
    # text as the file, and a fence-less turn here is prose (a refusal, an
    # apology), not code — that must keep failing cleanly so the serial path
    # rebuilds the file instead of shipping "sorry, no." as ledger.py.
    fenced = "```" in raw or "~~~" in raw
    blocks = parse_file_blocks(raw, fallback_path=current if fenced else "")
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
            context_updates=bookkeeping,
        )

    ok, err = await guarded_write_file(effects, current, body)
    if not ok:
        return StepOutput(
            result={"write_success": False},
            observations=f"session write: {current} refused — {err or 'guard'}",
            context_updates=bookkeeping,
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
            **bookkeeping,
        },
    )


async def action_rewind_session_turn(step_input: StepInput) -> StepOutput:
    """Take the walk's last turn back out of the session (llmvp
    rewindSessionTurn) when it left nothing usable — no answer, or a block the
    write refused.

    Every later file's instruction calls the files above it FACTS that are on
    disk; an abandoned attempt left in the context is neither, and each one
    the walk keeps is paid for again by every turn after it. Skipped when the
    turn never entered the context (the server rolls an answerless turn out at
    commit) or the server predates turn ids. Always continues the walk.

    Result: rewound.
    """
    effects = step_input.effects
    ctx = step_input.context
    session_id = ctx.get("structural_session_id")
    turn_id = ctx.get("inference_session_turn_id")
    current = ctx.get("current_file") or "?"
    reason = "skipped"
    if not ctx.get("inference_turn_committed", True):
        reason = "already_absent (rolled out at commit)"
    elif session_id and turn_id is not None and effects is not None:
        rewind = getattr(effects, "rewind_inference_session_turn", None)
        if rewind is not None:
            outcome = await rewind(session_id, int(turn_id))
            reason = str(outcome.get("reason", "?"))
    rewound = reason in ("rolled_back", "history_truncated")
    logger.info("session walk: unusable %s turn — rewind: %s", current, reason)
    return StepOutput(
        result={"rewound": rewound},
        observations=f"session walk: {current} turn rewind — {reason}",
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
    from agent.actions.roundtrip_contract import (
        declared_persisted_contracts,
        roundtrip_contract_findings,
    )

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
    data = {p: s for p, s in sources.items() if languages.is_data(_ext(p))}

    # ── FILESET CHECKS RUN ONCE, WHEN THE FILESET EXISTS ──────────────
    # A whole-project check applied after every file asks a question the
    # walk cannot yet answer. Save/load wiring lives in the entry point,
    # which creation_order writes LAST, so "nothing reads this payload" is
    # true of every intermediate state and means nothing until the final
    # file lands. Measured live: the round trip failed at file 5 of 7 while
    # the consumer was still unwritten, and the model burned repair turns on
    # a condition it had no way to satisfy.
    pending = list(ctx.get("pending_files") or [])
    # (file to fix, message). The round trip names its file itself; the
    # registry's messages name theirs in the text.
    cross: list[tuple[str, str]] = []
    if not pending:
        cross.extend(
            roundtrip_contract_findings(
                sources, declared_persisted_contracts(ctx.get("mission"))
            )
        )
        for msg in _data_registry_violations(data, ctx.get("data_registry") or []):
            cross.append((_implicated_file(msg, written_paths) or current, msg))

    entry = results.setdefault(
        current, {"passed": True, "checks_failed": [], "output": ""}
    )
    own = list(entry.get("checks_failed") or [])
    violations = [f"{current}: {c}" for c in own] + [msg for _, msg in cross]

    # ── ATTRIBUTE TO THE FILE THAT OWNS THE DEFECT ────────────────────
    # Booking a cross-file violation against whatever file happened to be
    # current is how a defect in game.py's save payload became a diagnosis
    # aimed at data/world.yaml, which was then patched to satisfy it. Each
    # finding carries the file that must change; book it there.
    current_implicated = False
    for target, msg in cross:
        if target == current:
            current_implicated = True
        tgt_entry = results.setdefault(
            target, {"passed": True, "checks_failed": [], "output": ""}
        )
        tgt_entry["passed"] = False
        tgt_entry["checks_failed"] = list(tgt_entry.get("checks_failed") or []) + [
            "cross_file"
        ]
        tgt_entry["output"] = (tgt_entry.get("output") or "") + "\n" + msg

    # Repair only what THIS turn can fix. A violation owned by another file
    # is recorded against that file and left to the sweep, which will
    # diagnose it with the right target instead of rewriting a bystander.
    file_ok = bool(entry.get("passed", True)) and not current_implicated
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
