"""Escalation layer v1 — the shared mid-flow recovery primitive.

A deterministic pipeline step hit a wall (a failed validation check, a
rejected write, a broken setup). Instead of a hardcoded single fallback (the
devolution pattern: file_ops' self_correct → whole-file rewrite), the invoker
hands the failure to THIS flow: a bounded REACT loop (diagnose_issue's proven
memoryful-session + compound-menu shape) over a minimal tool menu —
read_file / run_command / write_file / conclude — with a typed outcome that
rejoins the invoker's normal progression:

    resolved → the invoker re-validates and continues as if its own step
               had succeeded
    deferred → the invoker takes its existing failure path (never a dead end)

Design rules (dev/archive/docs/ESCALATION_PRIMITIVE.md):
  - Block verdicts, not attempts: deferring honestly is a valid outcome;
    burning the budget appeasing an unfixable check is not.
  - Tools are the existing guarded primitives: writes go through
    guarded_write_file (anti-gut + scaffold parse floor), commands through
    effects.run_command. No raw power.
  - Corrections (missing file, rejected write) queue guidance and do NOT eat
    the turn budget — honest mistakes recover without pressure.

web_search tool (added v1.1): when the fix needs external knowledge (library/
API behavior, error semantics) the model dispatches the deep_search sub-flow —
a bounded reflect-and-refine web loop — and its synthesized findings fold back
into this session as one turn. Gated on mission web_research (+ ~/.exa_key).

v1 non-goals: consult menu entry, amended(restart) mode, adoption sites beyond
file_ops.self_correct.
"""

from __future__ import annotations

import hashlib
import logging

from agent.llm_json import parse_llm_json
from agent.models import StepInput, StepOutput
from agent.session_injections import queue as queue_injection
from agent.loader import load_prompt_text

logger = logging.getLogger(__name__)

# ONE budget number, kept in agreement across the constant, the instruction
# template, and escalate.cue's check_budget rule (the diagnose template's
# 8-vs-10 drift is the cautionary tale).
MAX_ESCALATION_TURNS = 6
MAX_ESCALATION_CORRECTIONS = 4

# Static session head (the flow-fork pattern: invariant persona pinned once
# per instance; key changes iff the text changes).
SYSTEM_PROMPT = load_prompt_text("personas/escalation_seed")

CONCLUDE_PROMPT = load_prompt_text("escalate/conclude")


def _flow_key() -> str:
    return (
        f"escalate:start:{hashlib.md5(SYSTEM_PROMPT.encode('utf-8')).hexdigest()[:10]}"
    )


async def _session_used(step_input: StepInput) -> int:
    """What the escalation session holds — the "used" side of the
    whole-if-it-fits rule (agent/context_fit.py) for everything it reads."""
    from agent.context_fit import session_used

    return await session_used(
        step_input.effects, str(step_input.context.get("escalation_session_id", ""))
    )


async def _sized(step_input: StepInput, text: str, kind: str, label: str) -> str:
    """Output, evidence or findings WHOLE when they fit the session; else
    saved under .agent/outputs and shown as a line index to read from. It
    replaces 4,000-char head cuts that kept the start and lost the error at
    the end."""
    from agent.context_fit import output_view

    sid = str(step_input.context.get("escalation_session_id", "") or "session")
    turn = int(step_input.context.get("escalation_turn", 0) or 0)
    return await output_view(
        step_input.effects,
        (text or "").strip(),
        used=await _session_used(step_input),
        save_path=f".agent/outputs/escalation-{sid}-{turn}-{kind}.txt",
        label=label,
    )


async def action_open_escalation_session(step_input: StepInput) -> StepOutput:
    """Open the memoryful escalation session and queue the seed (facts only:
    who escalated, what failed, what must become true, where to look).

    Inputs: invoking_flow, failure_evidence, expected_outcome,
            target_file_path (optional).
    Publishes: inference_session_id, escalation_session_id, escalation_turn,
               escalation_corrections, escalation_files.
    """
    effects = step_input.effects
    if not effects:
        return StepOutput(
            result={"session_started": False},
            observations="No effects interface — cannot start escalation",
        )
    try:
        session_id = await effects.start_inference_session(
            {"ttl_seconds": 600}, static_prefix=SYSTEM_PROMPT, flow_key=_flow_key()
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to start escalation session: %s", e)
        return StepOutput(
            result={"session_started": False},
            observations=f"Failed to start escalation session: {e}",
        )

    inputs = step_input.inputs or {}
    parts: list[str] = [SYSTEM_PROMPT, ""]
    invoking = str(inputs.get("invoking_flow", "") or "a pipeline step")
    parts.append("## What failed")
    parts.append(f"The `{invoking}` step hit a deterministic failure:")
    parts.append("```")
    from agent.context_fit import estimate_tokens, output_view

    parts.append(
        await output_view(
            effects,
            str(inputs.get("failure_evidence", "") or "(no evidence provided)").strip(),
            used=estimate_tokens(SYSTEM_PROMPT),
            save_path=f".agent/outputs/escalation-{session_id}-evidence.txt",
            label="the failure evidence",
        )
    )
    parts.append("```")
    parts.append("")
    parts.append("## Expected outcome (conclude 'resolved' only when this holds)")
    parts.append(
        str(inputs.get("expected_outcome", "") or "The failure above no longer occurs.")
    )
    parts.append("")
    target = str(inputs.get("target_file_path", "") or "")
    if target:
        parts.append("## Focus")
        parts.append(f"The change under validation targets `{target}`.")
        parts.append("")
    # web_search availability. deep_search self-gates too, but announce it so the
    # model doesn't reach for a disabled tool in a hermetic mission (SWE).
    web_enabled = True
    try:
        mission = await effects.load_mission()
        web_enabled = bool(
            getattr(getattr(mission, "config", None), "web_research", True)
        )
    except (
        Exception
    ):  # noqa: BLE001 - no mission → assume enabled, rely on key backstop
        web_enabled = True
    if web_enabled:
        parts.append(
            "You may use `web_search` for external knowledge (library/API "
            "behavior, error semantics) you cannot get from the repo."
        )
    else:
        parts.append("`web_search` is unavailable in this mission — rely on the repo.")
    parts.append("")
    parts.append(f"You have up to {MAX_ESCALATION_TURNS} tool actions.")

    updates: dict = {
        "inference_session_id": session_id,
        "escalation_session_id": session_id,
        "escalation_turn": 0,
        "escalation_corrections": 0,
        "escalation_files": [],
    }
    # Forced boss consult (operator, 2026-08-07): a stuck goal's third
    # escalation routes to the consult BEFORE the first tool action — two
    # self-recovery loops already failed, so the supervisor speaks first.
    # The consult prompt reads escalation_choice_arg (normally the model's
    # own question from the work menu); under force we synthesize the
    # question from the evidence the invoker passed.
    force_consult = bool(step_input.params.get("force_consult", False))
    if force_consult:
        updates["escalation_choice_arg"] = (
            "This is a repeated escalation for the same failure — prior "
            "self-recovery attempts did not resolve it. Given the evidence "
            "above: is the premise of these repair attempts wrong, and what "
            "should the next concrete action be?"
        )
    queue_injection(updates, step_input.context, "\n".join(parts))
    logger.info(
        "Escalation session started for %s: %s%s",
        invoking,
        session_id,
        " (boss consult forced)" if force_consult else "",
    )
    return StepOutput(
        result={"session_started": True, "force_consult": force_consult},
        observations=f"Escalation opened for {invoking}"
        + (" — boss consult forced" if force_consult else ""),
        context_updates=updates,
    )


def _correction(step_input: StepInput, msg: str) -> StepOutput:
    """Queue a correction and bump the corrections counter — NOT the turn
    budget (honest mistakes recover without pressure). Signals exhausted once
    the model oscillates past the correction cap."""
    corrections = int(step_input.context.get("escalation_corrections", 0) or 0) + 1
    updates: dict = {"escalation_corrections": corrections}
    queue_injection(updates, step_input.context, f"Action failed — {msg}")
    exhausted = corrections >= MAX_ESCALATION_CORRECTIONS
    return StepOutput(
        result={"action_ok": False, "exhausted": exhausted},
        observations=f"escalation correction ({corrections}): {msg[:120]}",
        context_updates=updates,
    )


def _observe(
    step_input: StepInput, message: str, extra: dict | None = None
) -> StepOutput:
    """Queue an observation and bump the turn budget."""
    turn = int(step_input.context.get("escalation_turn", 0) or 0) + 1
    updates: dict = {"escalation_turn": turn}
    if extra:
        updates.update(extra)
    queue_injection(updates, step_input.context, message)
    return StepOutput(
        result={"action_ok": True},
        observations=f"escalation turn {turn}/{MAX_ESCALATION_TURNS}",
        context_updates=updates,
    )


async def action_escalation_read(step_input: StepInput) -> StepOutput:
    """read_file tool: the named file — or one part of it, `path:<Symbol>`,
    `path:/pointer`, `path:<first>-<last>` — WHOLE when it fits the session,
    else the file's index to read a part from (agent/context_fit.py).

    Context: escalation_choice_arg (the path), counters.
    """
    from agent.context_fit import read_file_view

    ref = str(step_input.context.get("escalation_choice_arg", "") or "").strip()
    view, error = await read_file_view(
        step_input.effects, ref, used=await _session_used(step_input)
    )
    if error:
        return _correction(step_input, error)
    return _observe(step_input, view)


async def action_escalation_run(step_input: StepInput) -> StepOutput:
    """run_command tool: /bin/sh -c the command, inject exit code + output.

    Context: escalation_choice_arg (the command), counters.
    """
    effects = step_input.effects
    cmd = str(step_input.context.get("escalation_choice_arg", "") or "").strip()
    if not cmd:
        return _correction(step_input, "run_command needs a command argument.")
    try:
        res = await effects.run_command(["/bin/sh", "-c", cmd], timeout=30)
    except Exception as e:  # noqa: BLE001
        return _correction(step_input, f"command failed to run: {e}")
    out = (getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")
    rc = getattr(res, "return_code", None)
    shown = await _sized(step_input, out, "run", "the command output")
    return _observe(step_input, f"Observation:\n$ {cmd}\n[exit {rc}]\n{shown}")


# Abbreviation markers. A body carrying any of these is not a file, it is a
# summary of one — and applying it silently deletes everything it stands in
# for. Lifted from the write that actually did this: the body ended
# "def _handle_move(...):\n    \"\"\"\n    ...\n\n    (rest of file unchanged)".
_ELISION_MARKERS = (
    "rest of file unchanged",
    "rest of the file unchanged",
    "(unchanged)",
    "rest of file omitted",
    "... rest of",
    "# ... (truncated)",
    "implement this later",
    "unchanged from the original",
)


async def action_escalation_propose(step_input: StepInput) -> StepOutput:
    """propose_fix tool: record the model's fenced body as ADVISORY ONLY.

    ESCALATION IS READ-ONLY (operator ruling, 2026-08-11). This replaces
    ``escalation_write``, which wrote through guarded_write_file. That path is
    how a working 17-method engine.py became a 2-method stub: an escalation
    write emitted a body ending in "(rest of file unchanged)" and the guard
    accepted it at 24.2% retention — four points above its anti-gut floor,
    because that floor is a PER-WRITE ratio with no memory of the file's
    original shape. Later writes measured against the wreck and looked healthy.

    The threshold was not the real problem. A recovery loop that can write is a
    SECOND authoring path around the flows that own file edits, carrying none
    of their review, and it opened exactly the hole it was meant to patch. So
    the proposal is recorded and handed back; the owning flow decides.

    Elision is refused here rather than recorded, because an abbreviated body
    is worse than no proposal: it reads as a complete file to whoever applies
    it. The ``code_author`` persona already forbids placeholders — this is the
    first place that rule is enforced instead of merely stated.

    Context: inference_response (raw turn text), escalation_choice_arg (path),
             escalation_proposals, counters.
    """
    from agent.markdown_fence import parse_file_blocks

    raw = str(step_input.context.get("inference_response", "") or "")
    fallback = str(step_input.context.get("escalation_choice_arg", "") or "").strip()
    blocks = [(p, c) for p, c in parse_file_blocks(raw, fallback_path=fallback) if p]
    if not blocks:
        return _correction(
            step_input,
            "propose_fix needs the proposed file content in a fenced code block "
            "(first line `# === FILE: path ===`) in the SAME response as the "
            "JSON action.",
        )

    elided = [(p, m) for p, c in blocks for m in _ELISION_MARKERS if m in c]
    if elided:
        path, marker = elided[0]
        return _correction(
            step_input,
            f"proposal for {path} is ABBREVIATED — it contains {marker!r}. "
            "A partial body is worse than none: whoever applies it cannot tell "
            "what you left out. Either propose the COMPLETE file, or propose a "
            "single named symbol and say which one.",
        )

    proposals = list(step_input.context.get("escalation_proposals", []) or [])
    for path, content in blocks:
        proposals.append({"path": path, "content": content})
    turn = int(step_input.context.get("escalation_turn", 0) or 0) + 1
    names = ", ".join(p for p, _ in blocks)
    return StepOutput(
        result={"exhausted": False, "proposed": [p for p, _ in blocks]},
        observations=(
            f"Recorded an ADVISORY proposal for {names} "
            f"({sum(len(c) for _, c in blocks)} chars). Nothing was written — "
            "escalation is read-only; the flow that owns the file decides."
        ),
        context_updates={
            "escalation_turn": turn,
            "escalation_proposals": proposals,
            "escalation_corrections": list(
                step_input.context.get("escalation_corrections", []) or []
            ),
        },
    )


async def action_escalation_write(step_input: StepInput) -> StepOutput:
    """RETIRED — escalation is read-only; this refuses instead of writing.

    Kept as a live refusal rather than deleted so any flow still routing here
    fails loudly instead of finding no action. Original contract follows.

    write_file tool: the model's SAME response carries the full new file
    body in a fenced block (`# === FILE: path ===` marker, or a bare fence
    with the path given as the menu arg). Every block goes through
    guarded_write_file — the anti-gut guard and the scaffold parse floor apply
    to escalations exactly as they do everywhere else; a guard rejection is a
    CORRECTION (the model hears why and may retry smaller), not a turn.

    Context: inference_response (raw turn text), escalation_choice_arg (path),
             escalation_files, counters.
    """
    return StepOutput(
        result={"exhausted": False, "refused": True},
        observations=(
            "escalation_write is RETIRED — escalation is read-only. An "
            "escalation write once reduced a 17-method file to 2 by emitting a "
            "body ending '(rest of file unchanged)', which the per-write "
            "anti-gut floor passed at 24.2%. Use propose_fix; the flow that "
            "owns the file applies it."
        ),
        context_updates={},
    )

    from agent.actions.file_ops_actions import guarded_write_file  # noqa: F401
    from agent.markdown_fence import parse_file_blocks  # noqa: F401

    effects = step_input.effects
    raw = str(step_input.context.get("inference_response", "") or "")
    fallback = str(step_input.context.get("escalation_choice_arg", "") or "").strip()
    blocks = parse_file_blocks(raw, fallback_path=fallback)
    blocks = [(p, c) for p, c in blocks if p]
    if not blocks:
        return _correction(
            step_input,
            "write_file needs the full new file content in a fenced code block "
            "(first line `# === FILE: path ===`) in the SAME response as the "
            "JSON action.",
        )

    from agent.actions.file_ops_actions import _is_repair_mission

    repair_mode = await _is_repair_mission(effects)
    written: list[str] = []
    rejections: list[str] = []
    for path, content in blocks:
        try:
            ok, err = await guarded_write_file(
                effects, path, content, repair_mode=repair_mode
            )
        except Exception as e:  # noqa: BLE001
            ok, err = False, f"write raised: {e}"
        if ok:
            written.append(path)
        else:
            rejections.append(err or f"write failed for {path}")

    if not written:
        return _correction(step_input, " / ".join(rejections)[:400])

    files = list(step_input.context.get("escalation_files", []) or [])
    for p in written:
        if p not in files:
            files.append(p)
    msg = f"Observation: wrote {', '.join(written)}."
    if rejections:
        msg += f" Rejected: {' / '.join(rejections)[:300]}"
    return _observe(step_input, msg, extra={"escalation_files": files})


async def action_conclude_escalation(step_input: StepInput) -> StepOutput:
    """One conclude turn in the session → {outcome, summary, key_change}.
    Fail-safe: an unparseable/withheld conclusion is DEFERRED — the invoker's
    failure path runs; we never fabricate a resolution.

    Context: escalation_session_id (required), escalation_files (optional).
    Result: outcome ("resolved" | "deferred").
    Publishes: escalation_summary, files_changed.
    """
    effects = step_input.effects
    session_id = step_input.context.get(
        "escalation_session_id"
    ) or step_input.context.get("inference_session_id")
    files = list(step_input.context.get("escalation_files", []) or [])
    outcome = "deferred"
    summary = ""
    try:
        res = await effects.session_inference(
            session_id, CONCLUDE_PROMPT, {"temperature": "t*0.2"}
        )
        text = getattr(res, "text", None) or str(res or "")
        parsed = parse_llm_json(text)
        if isinstance(parsed, dict) and parsed.get("outcome") in (
            "resolved",
            "deferred",
        ):
            outcome = parsed["outcome"]
            summary = str(parsed.get("summary", "") or "")[:400]
        else:
            summary = "conclusion unparseable — deferred (fail-safe)"
    except Exception as e:  # noqa: BLE001
        summary = f"conclude turn failed ({type(e).__name__}) — deferred"

    # Resolved with zero writes is only honest if the model VERIFIED the
    # outcome; we can't check that cheaply, so allow it but tag the summary —
    # the invoker re-validates anyway (the hook-back contract), so a false
    # "resolved" costs one re-check, never a false certification.
    if outcome == "resolved" and not files:
        summary = (summary + " (no files changed)").strip()

    return StepOutput(
        result={"outcome": outcome},
        observations=f"escalation {outcome}: {summary[:140]}",
        context_updates={
            "escalation_summary": summary,
            "files_changed": files,
        },
    )


async def action_escalation_fold_search(step_input: StepInput) -> StepOutput:
    """web_search fold-back: inject the deep_search research_summary into the
    escalation session as ONE turn.

    The deep_search sub-flow already ran its own bounded multi-query
    reflect-and-refine loop; here we only fold its synthesized findings so the
    next work turn (and the conclude turn) reason over them. Counts as one
    escalation turn regardless of how many web queries deep_search ran.

    Context: research_summary (from the sub-flow), counters.
    """
    summary = str(step_input.context.get("research_summary", "") or "").strip()
    if not summary:
        return _observe(
            step_input,
            "Observation (web_search): no usable findings returned — proceed from "
            "the code, or conclude.",
        )
    return _observe(
        step_input,
        "Observation (web_search findings):\n"
        + await _sized(step_input, summary, "web", "the findings"),
    )


async def action_escalation_fold_consult(step_input: StepInput) -> StepOutput:
    """consult_boss fold-back: inject the supervising boss model's direction
    into the escalation session as ONE turn.

    The do_consult step already ran the stateless boss completion (routed
    to the boss registry entry via config.model); here we only fold its
    text so the next work turn reasons over the direction. A failed or
    empty consult degrades to an observation — the loop never stalls on
    the supervisor being unreachable.

    Context: inference_response (the boss's reply), counters.
    """
    guidance = str(step_input.context.get("inference_response", "") or "").strip()
    if not guidance:
        return _observe(
            step_input,
            "Observation (consult_boss): the supervisor was unreachable or "
            "returned nothing — proceed on your own judgment, or conclude.",
        )
    return _observe(
        step_input,
        "Observation (supervisor direction):\n"
        + await _sized(step_input, guidance, "consult", "the direction"),
    )
