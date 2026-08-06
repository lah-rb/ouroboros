"""Mission actions — load state, dispatch tasks, manage lifecycle.

Rebuild v2: Replaces heuristic task matching with LLM menu selection,
adds structured architecture state, removes silent fallbacks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import Counter
from typing import Any

from agent.models import StepInput, StepOutput
from agent.actions.pipeline_actions import _cap_diagnostic
from agent.actions.reporting_actions import (
    is_infrastructure_file,
    structural_block_reason,
)

logger = logging.getLogger(__name__)


def _regress_startup_goal(mission) -> bool:
    """Re-open the 'Program starts cleanly' startup goal after a file edit.

    The startup goal is uniquely marked ``interaction_mode == "deterministic"``
    (see action_derive_project_goals) and is verified by actually running the
    program (run_startup_check). When a file is edited we regress it alongside
    the file's structural goal, so the startup check re-runs — catching an edit
    that broke a previously-passing startup, and verifying a fix that should make
    it pass. Only mutates ``status``. Returns True if a complete startup goal was
    regressed.
    """
    for g in mission.goals:
        if (
            getattr(g, "type", "") == "functional"
            and getattr(g, "interaction_mode", None) == "deterministic"
            and getattr(g, "status", "") == "complete"
        ):
            g.status = "incomplete"
            logger.info(
                "Regressed startup goal (re-verify after edit): %s",
                getattr(g, "description", "")[:60],
            )
            return True
    return False


def _reopen_structural_goal(
    mission, file_path: str, triggering_goal_id: str, source_flow: str
) -> bool:
    """Reopen the complete structural goal owning ``file_path`` ahead of a
    diagnosis-driven fix edit — WITH regression provenance.

    A bare ``status = "incomplete"`` flip here leaves the final mission.json
    indistinguishable from a never-verified goal (regression_reopened False,
    no note; and once the archive sweep has relocated the goal's reports, no
    on-goal evidence at all — observed on the gemma-4 greenfield run,
    2026-07-23). It also hides the goal from the bidirectional sweep's
    auto-complete direction (flow_sets ``regression_pending``), which keys on
    ``regression_reopened``. So: set the same flag the regression sweep sets
    (honoring the flip-flop guard) and drop a note naming the trigger.
    Returns True if a complete goal was reopened.
    """
    from agent.persistence.models import NoteRecord

    for sg in mission.goals:
        if sg.type == "structural" and file_path in (sg.associated_files or []):
            if sg.status == "complete":
                sg.status = "incomplete"
                sg.regression_reopened = not getattr(
                    sg, "regression_autocompleted", False
                )
                mission.notes.append(
                    NoteRecord(
                        content=(
                            f"regression: structural goal '{sg.description[:80]}' "
                            f"reopened — a diagnosis for goal_id="
                            f"{triggering_goal_id or 'n/a'} named its file "
                            f"{file_path} for a fix edit."
                        ),
                        category="failure_analysis",
                        tags=[
                            t for t in ["regression", sg.id, triggering_goal_id] if t
                        ],
                        source_flow=source_flow,
                    )
                )
                logger.info(
                    "Regressed structural goal for %s (via %s)",
                    file_path,
                    source_flow,
                )
                return True
            break
    return False


# ══════════════════════════════════════════════════════════════════════
# State Loading & Event Handling (kept from v1, lightly cleaned)
# ══════════════════════════════════════════════════════════════════════


async def action_load_mission_state(step_input: StepInput) -> StepOutput:
    """Load mission state and event queue from persistence.

    Publishes: mission, events
    """
    effects = step_input.effects
    if not effects:
        return StepOutput(
            result={"mission": None},
            observations="No effects interface — cannot load state",
        )

    mission = await effects.load_mission()
    if mission is None:
        return StepOutput(
            result={"mission": None},
            observations="No mission found in persistence",
        )

    events = await effects.read_events()

    return StepOutput(
        result={"mission": {"status": mission.status}},
        observations=f"Loaded mission {mission.id}: {mission.status}, "
        f"{len(mission.goals)} goals, {len(events)} events",
        context_updates={
            "mission": mission,
            "events": events,
        },
    )


async def action_handle_events(step_input: StepInput) -> StepOutput:
    """Process user messages, abort/pause signals from the event queue."""
    effects = step_input.effects
    mission = step_input.context.get("mission")
    events = step_input.context.get("events", [])

    if not mission or not effects:
        return StepOutput(
            result={"abort_requested": False, "pause_requested": False},
            observations="No mission or effects",
        )

    abort_requested = False
    pause_requested = False
    user_messages = []

    for event in events:
        if event.type == "abort":
            abort_requested = True
        elif event.type == "pause":
            pause_requested = True
            mission.status = "paused"
        elif event.type == "user_message":
            msg = event.payload.get("message", "")
            if msg:
                user_messages.append(msg)

    # Append user messages as notes.
    # NOTE: This action mutates multiple mission fields (status, notes) and
    # persists once at the end. We can't use effects.push_note here because
    # that reloads mission from disk, losing our status edits.
    if user_messages:
        from agent.persistence.models import NoteRecord

        for msg in user_messages:
            mission.notes.append(
                NoteRecord(
                    content=msg,
                    category="general",
                    source_flow="user_message",
                )
            )

    # Clear processed events
    await effects.clear_events()
    await effects.save_mission(mission)

    return StepOutput(
        result={
            "abort_requested": abort_requested,
            "pause_requested": pause_requested,
        },
        observations=f"Processed {len(events)} events: "
        f"abort={abort_requested}, pause={pause_requested}, "
        f"messages={len(user_messages)}",
        context_updates={"mission": mission},
    )


# ══════════════════════════════════════════════════════════════════════
# Memoryful Session Management for mission_control
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# NEW: Dispatch History Tracking
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# Architecture State Management (NEW)
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# Architecture Drift Detection (deterministic, no inference)
# ══════════════════════════════════════════════════════════════════════


async def action_design_gate(step_input: StepInput) -> StepOutput:
    """Design-gate drift action — the unified ``design_gate`` action, two modes.

    ``mode="route"`` (default): the PRE-design routing pass. Compares the stored
      architecture's canonical files against project_manifest keys and returns
      has_architecture/has_tasks/drift_detected/new_files so design_and_plan
      routes design vs reconcile vs derive_goals. Byte-for-byte the former
      ``action_check_architecture_drift``.
    ``mode="facts"``: the POST-parse pass. The SAME drift facts, additionally
      published to context as ``drift_facts`` for the coherence critic's evidence
      bundle. No routing decision rides on it.

    Infrastructure files (pyproject.toml, README.md, __init__.py, etc.) are
    excluded from drift detection since they're not application architecture.
    """
    mode = (step_input.params.get("mode") or "route").strip().lower()
    mission = step_input.context.get("mission")
    manifest = step_input.context.get("project_manifest", {})

    drift_summary = ""
    if not mission:
        result = {
            "has_architecture": False,
            "has_tasks": False,
            "drift_detected": False,
            "new_files": [],
        }
        observations = "No mission in context"
    elif mission.architecture is None:
        result = {
            "has_architecture": False,
            "has_tasks": len(mission.goals) > 0,
            "drift_detected": False,
            "new_files": [],
        }
        observations = "No architecture exists — initial design needed"
    else:
        has_tasks = len(mission.goals) > 0
        arch_files = set(mission.architecture.canonical_files())

        disk_files = {
            filepath
            for filepath in manifest.keys()
            if not is_infrastructure_file(filepath)
        }

        new_on_disk = sorted(disk_files - arch_files)
        drift_detected = len(new_on_disk) > 0
        if drift_detected:
            drift_summary = (
                f"Architecture drift: {len(new_on_disk)} file(s) on disk "
                f"not in architecture: {', '.join(new_on_disk)}"
            )
        result = {
            "has_architecture": True,
            "has_tasks": has_tasks,
            "drift_detected": drift_detected,
            "new_files": new_on_disk,
        }
        observations = (
            drift_summary
            or f"No drift — architecture matches disk ({len(arch_files)} files)"
        )

    updates: dict = {}
    if drift_summary:
        updates["drift_summary"] = drift_summary
    if mode == "facts":
        updates["drift_facts"] = result
    return StepOutput(result=result, observations=observations, context_updates=updates)


# Back-compat alias — the pre-design routing step historically dispatched
# "check_architecture_drift"; the unified two-mode action powers it now.
action_check_architecture_drift = action_design_gate


async def action_parse_and_store_architecture(step_input: StepInput) -> StepOutput:
    """Parse the LLM's architecture JSON and store as structured mission state.

    Reads: context.inference_response (raw JSON from design step)
    Writes: mission.architecture (ArchitectureState)
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    response = step_input.context.get("inference_response", "")
    logger.info(
        "parse_arch: response type=%s, len=%d, first100=%s",
        type(response).__name__,
        len(response),
        repr(response[:100]),
    )

    if not mission or not response:
        return StepOutput(
            result={"architecture_parsed": False},
            observations="No mission or inference response",
        )

    from agent.persistence.models import (
        ArchitectureState,
        ModuleSpec,
        InterfaceContract,
        DataShapeContract,
        StateShapeContract,
    )
    from agent.llm_json import parse_llm_json

    # Parse JSON from response
    data = parse_llm_json(response)
    if not isinstance(data, dict):
        return StepOutput(
            result={"architecture_parsed": False},
            observations="Failed to parse architecture JSON",
        )

    # Build ArchitectureState. Any field the model gets wrong (a list where a
    # str is expected, a bad enum value the coercions don't catch) raises a
    # ValidationError — degrade to architecture_parsed=False so the flow takes
    # its parse-failure branch rather than crashing the cycle into a retry loop.
    def _salvage_list(raw: Any, label: str, build) -> list:
        """Per-element SALVAGE for a blueprint list: one malformed element
        must not nuke the whole architecture.

        The modules loop below established the discipline in 2026-06; the
        contract lists (interfaces / data_shapes / state_shapes) never got
        it, so a model emitting `"interfaces": ["main calls loader.load"]`
        raised `'str' object has no attribute 'get'` INSIDE the blanket
        try and discarded the entire blueprint — olmo-think, arm18,
        2026-08-01. A non-list container (str/dict) is treated as empty
        with a warning rather than iterated into characters/keys.
        """
        if not isinstance(raw, list):
            if raw:
                logger.warning(
                    "Architecture salvage: %s is %s, not a list — ignored",
                    label,
                    type(raw).__name__,
                )
            return []
        out = []
        dropped: list[str] = []
        for el in raw:
            if not isinstance(el, dict):
                dropped.append(f"{str(el)[:60]!r} (not an object)")
                continue
            try:
                out.append(build(el))
            except Exception as ee:  # noqa: BLE001 — salvage the rest
                dropped.append(str(ee).splitlines()[0][:120])
        if dropped:
            logger.warning(
                "Architecture salvage: dropped %d invalid %s element(s): %s",
                len(dropped),
                label,
                "; ".join(dropped)[:400],
            )
        return out

    try:
        execution = data.get("execution", {})
        if not isinstance(execution, dict):
            execution = {}
        # Per-module SALVAGE: one malformed module must not nuke the whole
        # blueprint (before this, any single ValidationError discarded a
        # complete architecture → the zombie no-architecture mission). The
        # before-validators on ModuleSpec absorb the known shape misses;
        # anything still invalid drops alone, with a note.
        modules = []
        dropped_modules: list[str] = []
        raw_modules = data.get("modules", [])
        if not isinstance(raw_modules, list):
            logger.warning(
                "Architecture salvage: modules is %s, not a list — ignored",
                type(raw_modules).__name__,
            )
            raw_modules = []
        for m in raw_modules:
            if not isinstance(m, dict):
                dropped_modules.append(f"{str(m)[:40]!r} (not an object)")
                continue
            try:
                modules.append(
                    ModuleSpec(
                        file=m.get("file", ""),
                        responsibility=m.get("responsibility", ""),
                        defines=m.get("defines", []),
                        imports_from=m.get("imports_from", {}),
                    )
                )
            except Exception as me:  # noqa: BLE001 — salvage the rest
                dropped_modules.append(
                    f"{m.get('file', '?')} ({str(me).splitlines()[0][:120]})"
                )
        if dropped_modules:
            logger.warning(
                "Architecture salvage: dropped %d invalid module(s): %s",
                len(dropped_modules),
                "; ".join(dropped_modules)[:400],
            )
        if not modules and dropped_modules:
            # Nothing salvageable — treat as a parse failure so the flow
            # takes its failure branch (greenfield: back to design).
            return StepOutput(
                result={"architecture_parsed": False},
                observations=(
                    f"Architecture failed validation: all "
                    f"{len(dropped_modules)} modules invalid"
                ),
            )

        interfaces = _salvage_list(
            data.get("interfaces", []),
            "interfaces",
            lambda el: InterfaceContract(
                caller=el.get("caller", ""),
                callee=el.get("callee", ""),
                symbol=el.get("symbol", ""),
                signature=el.get("signature", ""),
            ),
        )

        data_shapes = _salvage_list(
            data.get("data_shapes", []), "data_shapes", DataShapeContract.from_llm_dict
        )

        state_shapes = _salvage_list(
            data.get("state_shapes", []),
            "state_shapes",
            StateShapeContract.from_llm_dict,
        )
        # CARRY-FORWARD on omission, mirroring transient_files below: this
        # function builds a brand-new ArchitectureState, so a reconcile pass
        # that leaves out state_shapes would silently wipe the state contracts
        # every downstream renderer (DATA CONTRACTS block) depends on. An empty
        # list must mean "no opinion", not "erase the contracts".
        if not state_shapes:
            prior_shapes = getattr(
                getattr(mission, "architecture", None), "state_shapes", None
            )
            if prior_shapes:
                state_shapes = list(prior_shapes)
                logger.info(
                    "architecture: carried forward %d state_shapes contract(s) "
                    "the response omitted",
                    len(state_shapes),
                )

        raw_transient = data.get("transient_files", [])
        if not isinstance(raw_transient, list):
            # A bare str would iterate into characters; a dict into keys.
            raw_transient = [raw_transient] if isinstance(raw_transient, str) else []
        transient_files = [str(t).strip() for t in raw_transient if str(t).strip()]
        # CARRY THE PRIOR VALUE FORWARD when the response omits the field.
        #
        # This function builds a BRAND-NEW ArchitectureState rather than merging,
        # and format_existing_architecture never shows the model the current
        # transient_files — so a reconcile pass silently resets it to whatever
        # the model re-invents, exactly the way coherence_* gets wiped. Since
        # 2026-07-29 the value is declared POST-structurally by
        # project_ops.declare_artifacts (from source, not from a guess), which
        # means the correction is what would be lost. An omission must mean "no
        # opinion", not "erase what was measured".
        #
        # Only on OMISSION: a response that supplies its own list still wins, so
        # the design/ingest paths can set and change it normally.
        if not transient_files:
            prior = getattr(
                getattr(mission, "architecture", None), "transient_files", None
            )
            if prior:
                transient_files = [str(t) for t in prior]
                logger.info(
                    "architecture: carried forward %d transient_files pattern(s) "
                    "the response omitted",
                    len(transient_files),
                )

        raw_order = data.get("creation_order")
        if isinstance(raw_order, str) and raw_order.strip():
            raw_order = [raw_order]  # bare-string coercion (pydantic would reject)
        creation_order = (
            [str(x) for x in raw_order]
            if isinstance(raw_order, list) and raw_order
            else [m.file for m in modules]
        )

        arch = ArchitectureState(
            import_scheme=execution.get("import_scheme", "flat"),
            run_command=execution.get("run_command", ""),
            smoke_command=execution.get("smoke_command", ""),
            working_directory=execution.get("working_directory", "project root"),
            init_files=execution.get("init_files", False),
            modules=modules,
            creation_order=creation_order,
            interfaces=interfaces,
            data_shapes=data_shapes,
            state_shapes=state_shapes,
            transient_files=transient_files,
            notes=data.get("notes", ""),
        )
    except Exception as e:  # ValidationError or malformed field shapes
        logger.warning("Architecture validation failed: %s", e)
        return StepOutput(
            result={"architecture_parsed": False},
            observations=f"Architecture failed validation: {e}",
        )

    mission.architecture = arch

    # Also save a brief note for prompt context
    from agent.persistence.models import NoteRecord

    arch_summary = (
        f"Import scheme: {arch.import_scheme}. "
        f"Run: {arch.run_command}. "
        + (f"Smoke: {arch.smoke_command}. " if arch.smoke_command else "")
        + f"Files: {', '.join(arch.canonical_files())}."
    )
    if arch.data_shapes:
        shape_lines = [
            f"  {ds.file} → {ds.consumed_by}: {ds.structure}" for ds in arch.data_shapes
        ]
        arch_summary += "\nData shapes:\n" + "\n".join(shape_lines)
    if arch.state_shapes:
        state_lines = [
            f"  {ss.name} (owner {ss.owner}, consumers {ss.consumed_by}): "
            f"{ss.structure}"
            for ss in arch.state_shapes
        ]
        arch_summary += "\nState contracts:\n" + "\n".join(state_lines)

    # NOTE: This action writes mission.architecture and appends a note in
    # the same cycle, persisting once. effects.push_note would reload from
    # disk between the two writes and lose the architecture edit.
    mission.notes.append(
        NoteRecord(
            content=arch_summary,
            category="architecture_blueprint",
            source_flow="design_and_plan",
        )
    )

    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={
            "architecture_parsed": True,
            "module_count": len(modules),
        },
        observations=f"Architecture stored: {len(modules)} modules, "
        f"scheme={arch.import_scheme}, "
        f"order={', '.join(arch.creation_order)}",
        context_updates={"mission": mission, "architecture": arch},
    )


async def action_persist_transient_files(step_input: StepInput) -> StepOutput:
    """Store the post-structural runtime-artifact declaration.

    WHY THIS EXISTS AND WHY IT IS HERE. `transient_files` used to be declared in
    `design_architecture` — before any code existed, so it was a prediction. On
    2026-07-29 the prediction was ['save.json', '*.autosave.json'] for a program
    that wrote `game_state.json`; the flush matched nothing 22 times out of 22,
    91% of behavioural sessions resumed mid-run off the unflushed save, and one
    of them read a CORRECT refusal as a parser bug and spent 586 seconds
    diagnosing working code.

    `project_ops` runs after the structural phase and before the first
    behavioural session, so a declaration made there can see the code it is
    describing. This action is the persistence half.

    Reads `runtime_artifacts` (schemas/runtime_artifacts.json): a list of
    {pattern, written_by}. `written_by` is not stored — it exists to force the
    model to cite the write site rather than guess — but it IS logged, because
    it is the audit trail for a later deletion.

    Context required: mission, inference_response
    Publishes: transient_files
    """
    from agent.llm_json import parse_llm_json

    effects = step_input.effects
    mission = step_input.context.get("mission")
    response = step_input.context.get("inference_response", "")

    if mission is None and effects is not None:
        mission = await effects.load_mission()
    arch = getattr(mission, "architecture", None) if mission else None
    if arch is None:
        return StepOutput(
            result={"transient_files": []},
            observations="no architecture to record runtime artifacts on",
            context_updates=({"mission": mission} if mission else {}),
        )

    data = parse_llm_json(response)
    entries = data.get("transient_files", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        entries = []

    patterns: list[str] = []
    evidence: list[str] = []
    for item in entries:
        if isinstance(item, dict):
            pattern = str(item.get("pattern", "") or "").strip()
            written_by = str(item.get("written_by", "") or "").strip()
        else:
            # Tolerate a bare string even though the schema forbids it: a
            # usable pattern with no citation still beats discarding it.
            pattern, written_by = str(item or "").strip(), ""
        if not pattern:
            continue
        if pattern.startswith(("/", "~")) or ".." in pattern:
            logger.warning(
                "runtime artifacts: refusing non-relative pattern %r", pattern
            )
            continue
        if pattern not in patterns:
            patterns.append(pattern)
            evidence.append(f"{pattern} <- {written_by or '(no citation)'}")

    # An EMPTY declaration is meaningful — "this program writes nothing" — and
    # must be recorded as such. But an unparseable response is not a
    # declaration, and overwriting a good prior value with [] because the model
    # returned junk is the failure this whole change exists to prevent.
    if not isinstance(data, dict):
        return StepOutput(
            result={"transient_files": list(arch.transient_files or [])},
            observations="runtime-artifact response unparseable — prior "
            "declaration left intact",
            context_updates={"mission": mission},
        )

    arch.transient_files = patterns
    # NO push_note BEFORE save_mission: push_note reloads the mission from disk
    # and re-saves it, which would drop this architecture edit (see the note in
    # action_parse_and_store_architecture).
    if effects is not None:
        await effects.save_mission(mission)

    if patterns:
        logger.info(
            "🧹 runtime artifacts declared (%d): %s", len(patterns), "; ".join(evidence)
        )
    else:
        logger.info("🧹 runtime artifacts: none — this program writes no state")

    return StepOutput(
        result={"transient_files": patterns},
        observations=(
            f"Runtime artifacts: {', '.join(patterns)}"
            if patterns
            else "Runtime artifacts: none declared (program writes no state)"
        ),
        context_updates={"mission": mission, "transient_files": patterns},
    )


def _ground_coherence_criteria(criteria: list, arch) -> list[str]:
    """Achievability filter: keep only critique criteria that reference something
    the blueprint actually has — a declared module file (path / basename / top
    dir) or a load-bearing execution field (run/smoke command, import scheme,
    working directory, the src-vs-`python -m` layout vocabulary). A criterion
    naming a module the blueprint does NOT declare is an un-achievable
    hallucination and is dropped."""
    anchors: set[str] = set()
    for m in getattr(arch, "modules", []):
        f = (getattr(m, "file", "") or "").strip()
        if not f:
            continue
        anchors.add(f.lower())
        anchors.add(os.path.basename(f).lower())
        top = f.split("/")[0].lower()
        if top:
            anchors.add(top)
    field_terms = (
        "run_command",
        "run command",
        "smoke_command",
        "smoke command",
        "import_scheme",
        "import scheme",
        "working_directory",
        "working directory",
        "python -m",
        "-m ",
        "pythonpath",
        "editable",
        "pip install",
        "src/",
        "src ",
    )
    grounded: list[str] = []
    for c in criteria:
        cl = str(c).lower()
        if any(a in cl for a in anchors) or any(t in cl for t in field_terms):
            grounded.append(str(c).strip())
    return grounded


async def action_ground_design_gate_verdict(step_input: StepInput) -> StepOutput:
    """Parse the coherence critic's verdict, ground its criteria to achievable
    fixes, decide coherent vs loop, and persist. Pre-build gate — enforcement is
    the verdict, not a shell check (there are no files yet). Mirrors
    ``action_parse_and_store_architecture``: parse_llm_json → CoherenceVerdict
    (degradable) → save once.

    Grounding (achievability + evidence-based over-block guard):
      - drop criteria that name modules the blueprint doesn't declare;
      - if the verdict is incoherent but NO concrete criterion survives grounding,
        flip to coherent — never BLOCK the mission on an ungrounded critique;
      - union-merge tighten-only the survivors onto ``architecture.coherence_criteria``.

    Fail-safe: an unparseable/invalid verdict loops (coherent=False) while the
    budget holds, then degrades to coherent=True once the budget is spent
    (fail-open — a parse glitch must not BLOCK the mission).
    """
    from agent.llm_json import parse_llm_json
    from agent.persistence.models import CoherenceVerdict, NoteRecord

    effects = step_input.effects
    mission = step_input.context.get("mission")
    response = step_input.context.get("inference_response", "")

    if not mission or mission.architecture is None:
        return StepOutput(
            result={"coherent": True},
            observations="design_gate: no architecture to critique — passing",
            context_updates=({"mission": mission} if mission else {}),
        )

    arch = mission.architecture
    # In-flow visits and this persisted counter both count gate iterations; the
    # persisted one is the durable audit / belt-and-suspenders for the resolver's
    # meta.attempt guard.
    arch.coherence_attempts = int(getattr(arch, "coherence_attempts", 0) or 0) + 1
    attempt = arch.coherence_attempts
    budget = 2  # ≤2 reconcile loops (matches file_ops check_retry)

    data = parse_llm_json(response)
    criteria: list[str] = []
    if not isinstance(data, dict):
        coherent = attempt > budget  # fail-open only once the budget is spent
        reason = "critic verdict unparseable"
    else:
        try:
            verdict = CoherenceVerdict(
                coherent=bool(data.get("coherent", True)),
                reason=str(data.get("reason", "") or ""),
                criteria=data.get("criteria", []),
            )
        except Exception as e:  # ValidationError / malformed shapes
            logger.warning("Coherence verdict validation failed: %s", e)
            coherent = attempt > budget
            reason = f"critic verdict invalid: {e}"
        else:
            coherent = verdict.coherent
            reason = verdict.reason
            criteria = _ground_coherence_criteria(verdict.criteria, arch)
            if not coherent and not criteria:
                # Evidence-based over-block guard: no concrete incoherence survived.
                coherent = True
                reason = reason or "no concrete incoherence survived grounding"

    if coherent:
        arch.coherence_criteria = []
        arch.coherence_reason = ""
        arch.coherence_grounded = True
        mission.architecture = arch
        if effects:
            await effects.save_mission(mission)
        return StepOutput(
            result={"coherent": True},
            observations=f"design_gate: coherent (attempt {attempt})",
            context_updates={"mission": mission, "design_gate_feedback": ""},
        )

    # Incoherent — union-merge tighten-only (never drop a prior-iteration finding).
    merged = list(arch.coherence_criteria)
    for c in criteria:
        if c and c not in merged:
            merged.append(c)
    arch.coherence_criteria = merged
    arch.coherence_reason = reason
    arch.coherence_grounded = True
    mission.architecture = arch
    feedback = reason + ("\n" + "\n".join(f"- {c}" for c in merged) if merged else "")
    mission.notes.append(
        NoteRecord(
            content=(
                f"design_gate rejected the blueprint (attempt {attempt}): {reason}. "
                f"Required fixes: {'; '.join(merged) if merged else '(none extracted)'}"
            ),
            category="architecture_blueprint",
            source_flow="design_and_plan",
        )
    )
    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"coherent": False},
        observations=f"design_gate: incoherent (attempt {attempt}) — {reason}",
        context_updates={"mission": mission, "design_gate_feedback": feedback},
    )


# B5: Stale flow names the model sometimes produces from memory.
# Maps old names → current canonical names.
_FLOW_NAME_REMAP: dict[str, str] = {
    "file_write": "file_ops",
    "create_file": "file_ops",
    "create": "file_ops",
    "modify_file": "rewrite",
    "ast_edit_session": "patch",
}


# ══════════════════════════════════════════════════════════════════════
# Mission Lifecycle (kept from v1)
# ══════════════════════════════════════════════════════════════════════


async def action_finalize_mission(step_input: StepInput) -> StepOutput:
    """Mark mission complete or aborted and save.

    There was a third status, "deadlocked", from the early frustration
    system. The escalation system fully superseded that, and by 2026-08-05
    it was unreachable: this action could still produce it, but all
    sixteen finalize_mission steps pass either {} or {"abort": True} — no
    flow had requested a deadlock in a long time, and nothing branched on
    the status. Surfaced by the action_reads_undeclared_param triage.
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")

    if not mission:
        return StepOutput(
            result={"finalized": False},
            observations="No mission to finalize",
        )

    if step_input.params.get("abort", False):
        mission.status = "aborted"
    else:
        mission.status = "completed"
        # Record the ceiling this completion was earned at — the stacking
        # continuance key (resume reopens when top_phase is later raised).
        mission.completed_at_phase = str(
            getattr(getattr(mission, "config", None), "top_phase", "") or "quality"
        )

    # Terminal archive sweep: the per-cycle sweep (attach_directive_report)
    # never runs AFTER the final goal completes — this catches the last
    # goal's records before the run ends. Relocation, never deletion.
    if effects and hasattr(effects, "archive_overflow"):
        await effects.archive_overflow(mission)

    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={"finalized": True, "status": mission.status},
        observations=f"Mission {mission.id} finalized: {mission.status}",
    )


async def action_enter_idle(step_input: StepInput) -> StepOutput:
    """Enter idle state — waiting for events."""
    return StepOutput(
        result={"idle": True},
        observations="Entering idle state — waiting for events",
    )


# ══════════════════════════════════════════════════════════════════════
# File Operations (fixed: no assumed-pass, clear errors)
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# Goal Derivation (Context Contract Architecture)
# ══════════════════════════════════════════════════════════════════════


async def action_derive_project_goals(step_input: StepInput) -> StepOutput:
    """Derive project goals from architecture and mission objective.

    Two-pass derivation:
      Pass 1 (deterministic): structural goals from architecture modules
      Pass 2 (inference): functional goals from objective + architecture

    Stores goals on MissionState and links tasks to parent goals.

    Context required: mission
    Context optional: architecture
    Publishes: goals, mission
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    architecture = step_input.context.get("architecture")
    logger.info(
        "derive_goals: architecture type=%s, value=%s",
        type(architecture).__name__,
        repr(architecture)[:200] if architecture else "None",
    )

    if not effects or not mission:
        return StepOutput(
            result={"goals_derived": False},
            observations="No effects or mission state",
        )

    # Import persistence model
    from agent.persistence.models import GoalRecord

    # Access mission as dict or object
    # `_arch_src` is bound in BOTH branches so the transient filter below has
    # one name to read — the object branch calls it `architecture`, the dict
    # branch `arch`, and only the latter was ever in scope after the if/elif.
    if hasattr(mission, "objective"):
        objective = mission.objective
        _arch_src = architecture
        modules = (
            architecture.modules
            if architecture and hasattr(architecture, "modules")
            else []
        )
        data_shapes = (
            architecture.data_shapes
            if architecture and hasattr(architecture, "data_shapes")
            else []
        )
    elif isinstance(mission, dict):
        objective = mission.get("objective", "")
        arch = mission.get("architecture") or architecture or {}
        _arch_src = arch
        if isinstance(arch, dict):
            modules = arch.get("modules", [])
            data_shapes = arch.get("data_shapes", [])
        else:
            modules = getattr(arch, "modules", [])
            data_shapes = getattr(arch, "data_shapes", [])
    else:
        return StepOutput(
            result={"goals_derived": False},
            observations="Cannot read mission state",
        )

    # Runtime state is never a structural goal. Filtered HERE, at the source,
    # rather than only in _get_sweep_files: goals are built straight off
    # `modules`/`data_shapes`, so filtering the sweep alone would still create
    # a goal to author the save file — and the serial create path would still
    # fulfil it. Operator ruling 2026-08-06.
    _transient = transient_exact_names(_arch_src)
    if _transient:

        def _keep(entry: Any) -> bool:
            f = (
                entry.get("file", "")
                if isinstance(entry, dict)
                else getattr(entry, "file", "")
            )
            return _norm_decl_path(f) not in _transient

        _before = len(modules) + len(data_shapes)
        modules = [m for m in modules if _keep(m)]
        data_shapes = [d for d in data_shapes if _keep(d)]
        if _before != len(modules) + len(data_shapes):
            logger.info(
                "goal derivation: excluded declared-transient file(s) from "
                "structural goals: %s",
                ", ".join(sorted(_transient)),
            )

    goals = []

    # ── Pass 1: Deterministic structural goals from architecture ──
    for mod in modules:
        if isinstance(mod, dict):
            file_path = mod.get("file", "")
            responsibility = mod.get("responsibility", "")
        else:
            file_path = getattr(mod, "file", "")
            responsibility = getattr(mod, "responsibility", "")

        if not file_path:
            continue

        goal = GoalRecord(
            description=responsibility or f"Implement {file_path}",
            type="structural",
            associated_files=[file_path],
        )
        goals.append(goal)

    # Track files already covered by module goals to avoid duplicates
    covered_files = {f for g in goals for f in g.associated_files}

    # Collect data file goals — we'll enrich them with content briefs
    data_file_goals = []
    for ds in data_shapes:
        if isinstance(ds, dict):
            file_path = ds.get("file", "")
            consumed_by = ds.get("consumed_by", "")
            structure = ds.get("structure", "")
        else:
            file_path = getattr(ds, "file", "")
            consumed_by = getattr(ds, "consumed_by", "")
            structure = getattr(ds, "structure", "")

        if not file_path:
            continue

        # Skip if this file already has a module goal (1a deduplication)
        if file_path in covered_files:
            continue
        covered_files.add(file_path)

        data_file_goals.append(
            {
                "file_path": file_path,
                "consumed_by": consumed_by,
                "structure": structure,
            }
        )

    # ── Pass 1b: Content briefs for data files ───────────────────
    # Generate creative content requirements for data files that need
    # substantive content (world definitions, game data, etc.).
    # Runs at t*1.0 — community default temperature for creative work.
    if effects and objective and data_file_goals:
        data_summary = "\n".join(
            f"- {d['file_path']} (consumed by {d['consumed_by']}): {d['structure'][:200]}"
            for d in data_file_goals
        )
        content_brief_prompt = (
            f"Mission objective:\n{objective}\n\n"
            f"Data files to create:\n{data_summary}\n\n"
            f"For each data file, write a CONTENT BRIEF — a short natural language "
            f"description of what interesting, substantive content should go in the file. "
            f"Focus on making the content engaging and meeting the quantitative "
            f"requirements from the mission objective (e.g., number of rooms, items, NPCs).\n\n"
            f"Do NOT describe the file format or structure — that is already defined. "
            f"Describe WHAT the content should be: names, descriptions, relationships, "
            f"and any creative elements that make it interesting.\n\n"
            f"Return a JSON object mapping file paths to content brief strings.\n"
            f'Example: {{"world.yaml": "Create a mysterious castle with at least 6 rooms '
            f"including a dungeon, throne room, and secret garden. Include 4 items such as "
            f"a magic lantern and enchanted key. Add 2 NPCs: a wise old wizard with branching "
            f"dialogue about the castle's history, and a suspicious guard who challenges "
            f'the player."}}\n\n'
            f"Return ONLY the fenced JSON."
        )

        try:
            brief_result = await effects.run_inference(
                prompt=content_brief_prompt,
                config_overrides={"temperature": "t*1.0"},
            )
            if brief_result.text:
                from agent.llm_json import parse_llm_json

                briefs = parse_llm_json(brief_result.text)
                if isinstance(briefs, dict):
                    for d in data_file_goals:
                        if d["file_path"] in briefs:
                            d["content_brief"] = briefs[d["file_path"]]
                            logger.info(
                                "Content brief for %s: %s",
                                d["file_path"],
                                d["content_brief"][:80],
                            )
        except Exception as e:
            logger.warning("Content brief generation failed: %s", e)

    # Create GoalRecords for data files with enriched descriptions
    for d in data_file_goals:
        content_brief = d.get("content_brief", "")
        if content_brief:
            description = f"Create {d['file_path']} with content: {content_brief}"
        else:
            description = (
                f"Create data file {d['file_path']} (consumed by {d['consumed_by']})"
            )

        goal = GoalRecord(
            description=description,
            type="structural",
            associated_files=[d["file_path"]],
        )
        goals.append(goal)

    # ── Pass 2: Inference-derived functional goals ──
    #
    # NO FIXED COUNT (2026-07-25). This asked for "4-7 functional goals" and
    # got 8/8/8/8/9/9/9/10/10 across ten runs — models sat at or above the
    # ceiling, and the band compressed goal counts into a near-constant that
    # had nothing to do with any objective's actual scope. The count was the
    # ONE confirmed place the framework dictated mission shape: the
    # architecture prompt was cleared on all three charges (filenames,
    # granularity, downstream reconcile — see the anchoring trilogy in dev/),
    # leaving this f-string as the real anchor. It now asks for coverage:
    # the goals define "done", so the objective's scope sets the count.
    # Regression guard: tests/test_project_goal_derivation.py.
    #
    # COST NOTE: every functional goal drives its own interact session, so
    # goal count multiplies the functional phase's wall clock. An objective
    # promising fifteen capabilities will now produce fifteen goals and take
    # correspondingly longer — the intended trade (honest completeness over a
    # fixed budget), but the first thing to look at if phases run long.
    if effects and objective:
        structural_summary = "\n".join(
            f"- {g.description} ({', '.join(g.associated_files)})" for g in goals
        )
        prompt = (
            f"You are a goal decomposition module in an automated coding pipeline. "
            f"Your output will be parsed by a JSON extractor. Return ONLY a JSON "
            f"array inside a fenced code block — no explanation, no commentary.\n\n"
            f"Given these structural goals:\n{structural_summary}\n\n"
            f"And the mission objective:\n{objective}\n\n"
            f"Derive the functional goals — user-facing capabilities the project "
            f"must deliver. Each goal will be tested independently in an "
            f"interactive session, so follow these rules:\n\n"
            f"GRANULARITY: Each goal must test exactly ONE capability that can "
            f"pass or fail on its own, without requiring other untested "
            f"capabilities to work. Do NOT combine multiple capabilities with "
            f"'and'. If a feature has basic and advanced aspects, split them "
            f"into separate goals.\n\n"
            f"ORDERING: Return goals from simplest/most foundational to most "
            f"complex/integrative. Basic capabilities that other features "
            f"depend on must come first. Goals that require multiple systems "
            f"working together (integration tests, end-to-end scenarios) "
            f"must come last.\n\n"
            f"COVERAGE: the goals together define what 'done' means, so a "
            f"program that passes all of them must be a COMPLETE, working "
            f"implementation of the objective. Every user-facing capability "
            f"the objective promises gets a goal. Do not drop a promised "
            f"capability to keep the list short, and do not pad the list with "
            f"work the objective never asked for. Let the objective's own "
            f"scope decide how many goals that is.\n\n"
            f"Return the goals as a JSON array of strings.\n\n"
            f"\u2705 CORRECT — for a calculator app (five capabilities because "
            f"that is what THIS objective promises; a larger objective "
            f"needs more, a smaller one fewer):\n"
            f"```json\n"
            f"[\n"
            f'  "User can enter numbers and see them displayed",\n'
            f'  "Basic arithmetic operations produce correct results",\n'
            f'  "Error messages appear for invalid input like division by zero",\n'
            f'  "Calculation history persists across multiple operations",\n'
            f'  "User can recall and reuse previous results in new calculations"\n'
            f"]\n"
            f"```\n"
            f"Each goal tests one thing, ordered from basic to advanced. "
            f"The last goal implicitly requires earlier capabilities to work.\n\n"
            f"\u274c WRONG — do not add explanation or combine capabilities:\n"
            f"Here are the functional goals based on the architecture:\n"
            f"```json\n"
            f'["Users can enter data and perform calculations and see history"]\n'
            f"```\n"
            f"This combines three capabilities into one goal.\n\n"
            f"Return ONLY the fenced JSON array."
        )

        try:
            result = await effects.run_inference(
                prompt=prompt,
                # NOTE: No max_tokens cap — truncated JSON arrays are
                # unparseable. See AGENT.md output preservation policy.
                config_overrides={"temperature": 0.4},
            )
            if result.text:
                from agent.llm_json import parse_llm_json

                functional_goals = parse_llm_json(result.text)
                if isinstance(functional_goals, list):
                    for desc in functional_goals:
                        if isinstance(desc, str) and desc.strip():
                            goals.append(
                                GoalRecord(
                                    description=desc.strip(),
                                    type="functional",
                                )
                            )
                else:
                    logger.warning(
                        "Could not parse functional goals from response: %s",
                        result.text[:200],
                    )
        except Exception as e:
            logger.warning("Functional goal inference failed: %s", e)

    # ── Pass 3: Synthetic startup goal ──
    # If the architecture defines a startup command, inject a deterministic
    # startup verification goal as the FIRST functional goal. This ensures
    # the program starts cleanly before any interactive testing begins.
    # The interact flow routes this to run_commands (not run_session) —
    # which needs the self-terminating smoke command, not the interactive
    # launch (effective_smoke_command falls back for pre-split missions).
    run_command = ""
    if architecture and hasattr(architecture, "effective_smoke_command"):
        run_command = architecture.effective_smoke_command or ""
    elif isinstance(architecture, dict):
        run_command = (
            architecture.get("smoke_command") or architecture.get("run_command") or ""
        )

    if run_command:
        startup_goal = GoalRecord(
            description="Program starts cleanly and exits without errors",
            type="functional",
            interaction_mode="deterministic",
        )
        # Insert before other functional goals
        structural_count = sum(1 for g in goals if g.type == "structural")
        goals.insert(structural_count, startup_goal)

    # ── Persist goals on mission state ──
    goal_dicts = [g.model_dump() for g in goals]
    if hasattr(mission, "goals"):
        mission.goals = goals
        await effects.save_mission(mission)
    elif isinstance(mission, dict):
        mission["goals"] = goal_dicts
        await effects.save_mission(mission)

    return StepOutput(
        result={
            "goals_derived": True,
            "structural_count": sum(1 for g in goals if g.type == "structural"),
            "functional_count": sum(1 for g in goals if g.type == "functional"),
        },
        observations=f"Derived {len(goals)} goals ({sum(1 for g in goals if g.type == 'structural')} structural, {sum(1 for g in goals if g.type == 'functional')} functional)",
        context_updates={
            "goals": goal_dicts,
            "mission": mission,
        },
    )


def _directive_slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.strip().lower()).strip("-")[
        :60
    ]


async def action_derive_directive_goals(step_input: StepInput) -> StepOutput:
    """Decompose ``mission.pending_directive`` into APPEND-ONLY goals against the
    existing architecture (brownfield replan). New-file modules become structural
    goals (and append a ModuleSpec to the architecture); capabilities become
    functional ``capability_absent`` goals. Idempotent by ``finding_signature``;
    clears ``pending_directive`` so the replan phase falls through.

    Unlike ``action_derive_project_goals`` (which REPLACES ``mission.goals``),
    this ONLY appends — existing complete goals and ModuleSpecs are never
    touched.

    Context: mission, inference_response
    Result: goals_derived, structural_count, functional_count
    Publishes: mission
    """
    from agent.llm_json import parse_llm_json
    from agent.persistence.models import GoalRecord, ModuleSpec

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"goals_derived": False}, observations="No mission")

    directive = str(getattr(mission, "pending_directive", "") or "").strip()
    if not directive:
        # Re-entry safety: the directive was already consumed.
        return StepOutput(
            result={"goals_derived": False},
            observations="No pending directive to decompose",
        )

    from agent.actions.pipeline_actions import is_repair_profile

    # Repair missions FIX existing code; a derived functional goal is a fix,
    # not an absent capability to explore-and-build. capability_absent stays
    # False so the goal routes diagnose-first (bounded CONCLUDE schema) instead
    # of the explore-and-build path that exploded pilot-1's scope. (Semantic
    # correction, not a bypass — see dev/archive/docs/SWE_PILOT_1_FINDINGS.md.)
    repair = is_repair_profile(mission)

    parsed = parse_llm_json(str(step_input.context.get("inference_response", "")))
    parsed = parsed if isinstance(parsed, dict) else {}
    new_files = parsed.get("new_files") or []
    capabilities = parsed.get("capabilities") or []

    existing_sigs = {
        getattr(g, "finding_signature", "")
        for g in mission.goals
        if getattr(g, "finding_signature", "")
    }
    arch = getattr(mission, "architecture", None)

    # ── Pass 1: new files → structural goals (+ ModuleSpec) ───────────
    structural_count = 0
    for entry in new_files:
        if not isinstance(entry, dict):
            continue
        file_path = str(entry.get("file") or "").strip()
        if not file_path:
            continue
        sig = f"directive-struct:{file_path}"
        # Skip already-signed goals and architecture-known files (don't
        # re-plan something that already exists in the blueprint).
        if sig in existing_sigs or (arch is not None and arch.has_file(file_path)):
            continue
        if arch is not None:
            arch.modules.append(
                ModuleSpec(
                    file=file_path,
                    responsibility=str(entry.get("responsibility") or ""),
                    defines=[str(d) for d in (entry.get("defines") or [])],
                    imports_from=entry.get("imports_from") or {},
                )
            )
            if file_path not in arch.creation_order:
                arch.creation_order.append(file_path)
        mission.goals.append(
            GoalRecord(
                description=str(
                    entry.get("responsibility") or f"Implement {file_path}"
                ),
                type="structural",
                associated_files=[file_path],
                origin="directive",
                finding_signature=sig,
            )
        )
        existing_sigs.add(sig)
        structural_count += 1

    # ── Pass 2: capabilities → functional "absent = build" goals ──────
    functional_count = 0
    for entry in capabilities:
        if isinstance(entry, dict):
            desc = str(entry.get("description") or "").strip()
            placement = str(entry.get("placement") or "").strip()
        else:
            desc, placement = str(entry or "").strip(), ""
        if not desc:
            continue
        sig = f"directive-func:{_directive_slug(desc)}"
        if sig in existing_sigs:
            continue
        full_desc = desc if not placement else f"{desc}\n\nPlacement: {placement}"
        mission.goals.append(
            GoalRecord(
                description=full_desc,
                type="functional",
                origin="directive",
                # Repair goals are FIXES to existing code (diagnose-first);
                # only a greenfield directive builds an absent capability.
                capability_absent=not repair,
                interaction_mode="exploratory",
                finding_signature=sig,
            )
        )
        existing_sigs.add(sig)
        functional_count += 1

    # Clear UNCONDITIONALLY (even on 0 goals) so an undecomposable directive
    # can't loop forever in the replan phase. Dedup makes a re-run a no-op.
    mission.pending_directive = ""
    derived = structural_count + functional_count
    if derived == 0:
        logger.warning(
            "Directive decomposed to 0 goals (cleared anyway): %r", directive[:80]
        )
    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={
            "goals_derived": derived > 0,
            "structural_count": structural_count,
            "functional_count": functional_count,
        },
        observations=(
            f"Directive decomposed: +{structural_count} structural, "
            f"+{functional_count} functional goal(s)"
        ),
        context_updates={"mission": mission},
    )


# ══════════════════════════════════════════════════════════════════════
# Creation Order Sweep
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# Pipeline Phase Actions (mission_control v9)
# ══════════════════════════════════════════════════════════════════════


def _get_working_dir(mission: Any) -> str:
    """Extract working directory from mission, handling object and dict forms."""
    if hasattr(mission, "config") and hasattr(mission.config, "working_directory"):
        return mission.config.working_directory
    if isinstance(mission, dict):
        return mission.get("config", {}).get("working_directory", "")
    return ""


def _norm_decl_path(p: Any) -> str:
    """Normalise a declared path for comparison — `./x.py` and `x.py` are one.

    A PREFIX strip, deliberately. `lstrip("./")` would also eat the leading
    dot of `.gitignore` and every leading slash of an absolute path.
    """
    s = str(p).strip()
    while s.startswith("./"):
        s = s[2:]
    return s


def transient_exact_names(arch: Any) -> set[str]:
    """Files the architecture names EXACTLY (not by glob) as transient.

    A transient file is RUNTIME STATE, so it is not a structural goal, not
    part of the declared sweep, and not protected from the flush. Operator
    ruling 2026-08-06: remove them from consideration for structural goals
    entirely.

    WHY THIS EXISTS. A design can list the same path in `data_shapes` AND in
    `transient_files` — "author this file" and "this is runtime state, delete
    it at session close", at once. Two of the campaign's three
    missing-by-one-file batch turns were exactly that, and they were the only
    two runs in which any file was declared both ways:

        tier_20260801-185254  gpt-oss       savegame.json  in both
        tier_20260805-092309  qwen3-next    save.json      in both

    The chain: the file is declared, the model sensibly declines to author a
    save file, the batch is scored as MISSING it, the serial create path
    authors a pristine empty-state save, the flush protects it (declared
    data_shapes were exempt), and it ships. Both artifacts carry one. A judge
    then read one as proof the program had been run.

    EXACT NAMES ONLY. A glob keeps the flush's protection, so a broad
    `*.json` cannot silently swallow a declared data_shape — the guard that
    exemption was written for. Both real cases were exact names.
    """
    # dict OR object: mission state arrives both ways here, and a bare
    # getattr on a dict returns None — a silent miss for exactly the shape
    # the goal-derivation path uses.
    raw = (
        arch.get("transient_files")
        if isinstance(arch, dict)
        else getattr(arch, "transient_files", None)
    )
    out: set[str] = set()
    for p in raw or []:
        s = str(p).strip()
        # VALIDATE THE RAW STRING, THEN NORMALISE. `lstrip("./")` is a
        # CHARACTER-SET strip, not a prefix strip: it turns "/etc/passwd" into
        # "etc/passwd" and "../secrets.json" into "secrets.json". Sanitising
        # first would launder exactly the paths these guards exist to reject —
        # and this set is subtracted from the flush's `protected` list, so a
        # laundered entry un-protects a real file from deletion.
        if not s or s.startswith(("/", "~")) or ".." in s:
            continue
        if any(c in s for c in "*?["):
            continue  # a glob keeps the data_shapes exemption
        out.add(_norm_decl_path(s))
    return out


def _get_sweep_files(arch: Any) -> list[str]:
    """Build the ordered list of files: creation_order ∪ modules ∪ data_shapes.

    Uses creation_order for sequencing, but unions with all module files
    to catch any that were listed in modules but omitted from the order
    (e.g., __init__.py).  Data shape files are appended last.

    Exact-named `transient_files` are subtracted at the end — runtime state
    is not a file anyone is asked to author (see transient_exact_names).
    """
    ordered = (
        list(arch.creation_order)
        if hasattr(arch, "creation_order") and arch.creation_order
        else [m.file for m in arch.modules] if hasattr(arch, "modules") else []
    )
    seen = set(ordered)

    # Add any module files not already in creation_order
    if hasattr(arch, "modules"):
        for m in arch.modules:
            f = m.file if hasattr(m, "file") else m.get("file", "")
            if f and f not in seen:
                ordered.append(f)
                seen.add(f)

    # Add data_shape files not already covered
    if hasattr(arch, "data_shapes"):
        for ds in arch.data_shapes:
            f = ds.file if hasattr(ds, "file") else ds.get("file", "")
            if f and f not in seen:
                ordered.append(f)
                seen.add(f)

    transient = transient_exact_names(arch)
    if transient:
        dropped = [f for f in ordered if _norm_decl_path(f) in transient]
        if dropped:
            logger.info(
                "sweep: dropping %d declared-transient file(s) — runtime state "
                "is not a structural goal: %s",
                len(dropped),
                ", ".join(dropped),
            )
        ordered = [f for f in ordered if _norm_decl_path(f) not in transient]

    return ordered


async def action_check_pipeline_phase(step_input: StepInput) -> StepOutput:
    """Compute the current pipeline phase from the mission's flow-set spec.

    The phase order lives in agent/flow_sets.py as a declarative spec
    selected by ``mission.config.flow_set`` (default code_core, whose
    order is: plan → structural → environment → functional →
    quality_fix → quality). The returned phase names are the contract
    with the controller flow's check_phase resolver rules.

    Context required: mission
    """
    from agent.flow_sets import DEFAULT_FLOW_SET, evaluate_phases, get_flow_set

    mission = step_input.context.get("mission")
    config = getattr(mission, "config", None) if mission else None
    set_name = getattr(config, "flow_set", DEFAULT_FLOW_SET) or DEFAULT_FLOW_SET
    phase, observation = evaluate_phases(mission, get_flow_set(set_name).phases)
    return StepOutput(result={"phase": phase}, observations=observation)


def _repair_failure_class(report: Any) -> str:
    """Coarse failure class from a report's checks_failed prefixes."""
    checks = getattr(report, "checks_failed", None) or []
    for prefix in ("syntax", "import", "smoke"):
        if any(str(c).startswith(f"{prefix}:") for c in checks):
            return prefix
    return "other"


async def _note_repair_econ(
    effects: Any,
    mission: Any,
    working_dir: str,
    file_path: str,
    stage: str,
    report: Any,
) -> None:
    """Tag a parallel-mode repair dispatch for offline economics joins.

    One machine-parseable line per dispatch (stage = diagnose | patch):
    file, failure class, file size. The note's own timestamp brackets
    the episode against llmvp interactions.jsonl, where the actual
    read/write token counts live — dev/repair_econ.py does the join.
    These notes feed the regenerate-vs-diagnose tiering decision with
    measured per-failure-class costs instead of borrowed estimates.
    """
    from agent.persistence.models import NoteRecord

    try:
        size = os.path.getsize(os.path.join(working_dir, file_path))
    except OSError:
        size = 0
    failure_class = _repair_failure_class(report)
    mission.notes.append(
        NoteRecord(
            content=(
                f"repair_econ stage={stage} file={file_path} "
                f"class={failure_class} size_bytes={size}"
            ),
            category="failure_analysis",
            tags=["repair_econ", file_path, failure_class],
            source_flow="structural_sweep_next",
        )
    )
    if effects:
        try:
            await effects.save_mission(mission)
        except Exception:  # noqa: BLE001 - telemetry must not break dispatch
            logger.warning("repair_econ note save failed", exc_info=True)


def _related_goal_context(mission: Any, goal: Any, files: list[str]) -> str:
    """Sibling-goal context for a fix dispatch: every OTHER goal bound to
    the same file(s), open or complete.

    A shared file is a shared constraint surface. The 2026-07-16 bossgame
    baseline oscillated 17↔19 goals for eight hours because each
    world.yaml exit-defect goal was dispatched alone: the model patched
    the named exit, silently broke its reciprocal, and regression
    detection reopened the goals it had just completed — with no prompt
    ever showing the sibling constraints. Rendering them here lets one
    edit satisfy the full constraint set instead of trading defects.
    """
    file_set = set(files or [])
    if not file_set or mission is None:
        return ""
    open_sibs, done_sibs = [], []
    for g in mission.goals:
        if g.id == goal.id or not (set(g.associated_files or []) & file_set):
            continue
        desc = " ".join(str(g.description).split())[:220]
        (done_sibs if g.status == "complete" else open_sibs).append(desc)
    if not open_sibs and not done_sibs:
        return ""
    parts = []
    if open_sibs:
        parts.append(
            "Other OPEN goals on this file — one edit should satisfy ALL of "
            "these together, they are one constraint set:\n"
            + "\n".join(f"  - {d}" for d in open_sibs[:8])
        )
    if done_sibs:
        parts.append(
            "COMPLETED goals on this file — do NOT regress these; an edit "
            "that breaks one reopens it:\n"
            + "\n".join(f"  - {d}" for d in done_sibs[:8])
        )
    return "\n\n".join(parts)


async def action_structural_sweep_next(step_input: StepInput) -> StepOutput:
    """Find the next incomplete structural goal and determine what it needs.

    Walks creation_order + data_shapes. For each incomplete structural goal:
      - File missing on disk → needs_create=True
      - File exists but goal incomplete (last report failed) → needs_fix=True
      - File exists and last report clean → mark complete, continue

    Context required: mission
    Publishes: dispatch_config
    """
    mission = step_input.context.get("mission")
    effects = step_input.effects

    if not mission:
        return StepOutput(
            result={"sweep_complete": True},
            observations="No mission — skip sweep",
        )

    arch = getattr(mission, "architecture", None)
    if not arch:
        # SUSPENDERS (the origin-stamp phase fix is the belt): incomplete
        # structural goals with NO architecture are unaddressable by this
        # sweep — its walk is creation_order-derived, and the 2026-07-18
        # goal-file union below never runs on this early exit. Returning
        # "complete" here spun check_phase↔sweep to the 51x guard (OLMo
        # 2026-07-23). Escalate to replan instead; genuinely goal-less
        # missions still exit vacuously complete.
        pending = [
            g
            for g in getattr(mission, "goals", []) or []
            if getattr(g, "type", "") == "structural" and g.status != "complete"
        ]
        if pending:
            logger.warning(
                "Structural sweep: %d incomplete structural goal(s) but no "
                "architecture — unaddressable, escalating to replan",
                len(pending),
            )
            return StepOutput(
                result={"sweep_complete": False, "needs_replan": True},
                observations=(
                    "Structural goals exist but no architecture — re-planning"
                ),
            )
        return StepOutput(
            result={"sweep_complete": True},
            observations="No architecture — skip sweep",
        )

    sweep_files = _get_sweep_files(arch)
    # Include incomplete structural goals whose file the architecture does
    # NOT list — e.g. a create_backfill goal for an unplanned file. The
    # sweep's walk is architecture-derived, so without this such a goal is
    # counted incomplete by check_phase yet never SELECTED below: the sweep
    # returns "complete", check_phase re-routes here, and the controller
    # spins to the 51x-no-dispatch guard (2026-07-18: a create_backfill
    # `src/command.py` goal crashed the boss baseline exactly this way).
    # Appended LAST so architecture files keep their creation_order priority.
    _seen_sweep = set(sweep_files)
    for _g in mission.goals:
        if getattr(_g, "type", "") == "structural" and _g.status != "complete":
            for _f in _g.associated_files or []:
                if _f and _f not in _seen_sweep:
                    sweep_files.append(_f)
                    _seen_sweep.add(_f)
    working_dir = _get_working_dir(mission)

    if not sweep_files or not working_dir:
        return StepOutput(
            result={"sweep_complete": True},
            observations="No files or working directory — skip sweep",
        )

    mode = (
        str(getattr(getattr(mission, "config", None), "structural_mode", "") or "")
        or "serial"
    )
    if mode == "parallel":  # legacy alias, renamed 2026-07-23 (true
        mode = "batch"  # parallelism now means the swarm/batched paths)

    # ── Batch mode: one-shot batch creation ───────────────────────
    # Dispatch build_structure exactly once, on a virgin structural
    # phase: no goal has any report, no sweep file exists, and no prior
    # batch attempt is on record (the batch summary note doubles as the
    # attempted-flag — an empty generation books no reports, and without
    # the flag the sweep would re-dispatch the batch forever). After the
    # batch, this sweep resumes per-file: missing files → serial create,
    # gate-failed files → diagnose-first repair below.
    if mode == "batch":
        structural_goals = [g for g in mission.goals if g.type == "structural"]
        batch_attempted = any(
            g.reports or getattr(g, "reports_archived", 0) for g in structural_goals
        ) or any(
            "batch_structural" in (getattr(n, "tags", None) or [])
            for n in mission.notes
        )
        any_file_exists = any(
            os.path.isfile(os.path.join(working_dir, f)) for f in sweep_files
        )
        if structural_goals and not batch_attempted and not any_file_exists:
            dispatch_config = {
                "goal_id": "",
                "goal_description": "Create all architecture files in one batch",
                "goal_type": "structural",
                "goal_files": list(sweep_files),
                "flow": "build_structure",
                "target_file_path": "",
                "flow_directive": (
                    "Create every file in the architecture blueprint in one "
                    "batch generation."
                ),
                "recent_reports": [],
            }
            logger.info("Structural sweep: batch-creating %d files", len(sweep_files))
            return StepOutput(
                result={"sweep_complete": False, "needs_batch_create": True},
                observations=(
                    f"Structural sweep: parallel mode — batch-creating all "
                    f"{len(sweep_files)} files in one generation"
                ),
                context_updates={"dispatch_config": dispatch_config},
            )

    # ── Batch mode: content-goal fan-out ──────────────────────────
    # After the code-symbol swarm, the data files remain as independent
    # structural goals generated one-per-cycle through the serial create
    # path — the measured serial residue. When ≥2 are still missing,
    # dispatch ONE create_content_batch burst (stateless completion per
    # file). One-shot via the "content_batch"-tagged note; any file the
    # burst fails falls back to the serial walk below unchanged.
    if mode == "batch":
        content_attempted = any(
            "content_batch" in (getattr(n, "tags", None) or []) for n in mission.notes
        )
        if not content_attempted:
            _data_exts = (".yaml", ".yml", ".json", ".toml")
            missing_data = []
            for _f in sweep_files:
                if not _f.lower().endswith(_data_exts):
                    continue
                _g = next(
                    (
                        g
                        for g in mission.goals
                        if g.type == "structural"
                        and _f in (g.associated_files or [])
                        and g.status != "complete"
                        and getattr(g, "origin", "") != "create_backfill"
                    ),
                    None,
                )
                if _g is not None and not os.path.isfile(os.path.join(working_dir, _f)):
                    missing_data.append(_f)
            if len(missing_data) >= 2:
                dispatch_config = {
                    "goal_id": "",
                    "goal_description": "Generate all missing data files in one batch",
                    "goal_type": "structural",
                    "goal_files": missing_data,
                    "flow": "create_content_batch",
                    "target_file_path": "",
                    "flow_directive": (
                        "Generate every missing data file concurrently from its "
                        "registry-enriched content goal."
                    ),
                    "recent_reports": [],
                }
                logger.info(
                    "Structural sweep: content-batching %d data files",
                    len(missing_data),
                )
                return StepOutput(
                    result={"sweep_complete": False, "needs_content_batch": True},
                    observations=(
                        f"Structural sweep: batch mode — fanning out all "
                        f"{len(missing_data)} missing data files in one burst"
                    ),
                    context_updates={"dispatch_config": dispatch_config},
                )

    # ── Batch mode: failed-goal diagnosis fan-out ─────────────────
    # When ≥2 gate-failed goals would each take a full serial
    # diagnose_issue cycle, dispatch ONE diagnose_batch triage burst
    # first. Confidence-gated per goal (an unconfident triage books
    # nothing and the goal takes the interactive flow as today);
    # re-triage is prevented per goal id via the diagnose_batch notes.
    if mode == "batch":
        from agent.actions.contract_swarm_actions import (
            _diagnose_batch_candidates,
        )

        diag_candidates = _diagnose_batch_candidates(mission, working_dir)
        # A lint-blocked goal makes the burst worth firing on its own. The >=2
        # threshold is an economy heuristic against the SERIAL diagnose_issue a
        # hard gate failure would otherwise take; for lint the counterfactual is
        # spending nothing at all, so the comparison is one cheap one-shot
        # worker vs. shipping a known defect into the functional phase. Bursting
        # at one keeps the ask deterministic instead of leaving a lone
        # lint-blocked goal stalled until some unrelated goal also fails.
        lint_blocked = [
            g
            for g, _, last in diag_candidates
            if structural_block_reason(g, getattr(last, "checks_failed", []) or [])
            == "lint"
        ]
        if len(diag_candidates) >= 2 or lint_blocked:
            # DO NOT mark lint_reviewed here. Marking at dispatch clears the
            # block before the burst runs, and swarm_diagnose_batch recomputes
            # candidates — so it finds nothing and the gate triages NOTHING
            # while still costing a cycle (2026-07-27 APEX arm, cycle 4).
            # The flag is spent inside the burst, where a worker actually sees
            # the finding; the triaged-goal ledger plus the per-file lint
            # decision below are what bound it if the burst books nothing.
            dispatch_config = {
                "goal_id": "",
                "goal_description": "Triage all gate-failed goals in one batch",
                "goal_type": "structural",
                "goal_files": [p for _, p, _ in diag_candidates],
                "flow": "diagnose_batch",
                "target_file_path": "",
                "flow_directive": (
                    "Triage every gate-failed structural goal concurrently "
                    "with one-shot diagnoses."
                ),
                "recent_reports": [],
            }
            logger.info(
                "Structural sweep: diagnose-batching %d gate-failed goals",
                len(diag_candidates),
            )
            return StepOutput(
                result={"sweep_complete": False, "needs_diagnose_batch": True},
                observations=(
                    f"Structural sweep: batch mode — triaging all "
                    f"{len(diag_candidates)} gate-failed goals in one burst"
                ),
                context_updates={"dispatch_config": dispatch_config},
            )

    # ── §19 cadence: seam-gate every fileset change, not only phase exit ──
    seam_dispatch = await _seam_gate_cadence(mission, effects, sweep_files)
    if seam_dispatch is not None:
        return seam_dispatch

    # Walk files in order, find the first incomplete structural goal
    for file_path in sweep_files:
        # Find the goal for this file
        goal = None
        for g in mission.goals:
            if g.type == "structural" and file_path in (g.associated_files or []):
                goal = g
                break

        if goal is None:
            continue  # No goal for this file — skip

        if goal.status == "complete":
            continue  # Already done

        # Goal is incomplete — check what it needs
        full_path = os.path.join(working_dir, file_path)
        file_exists = os.path.isfile(full_path)

        if not file_exists:
            # A create_backfill orphan whose file is now gone is the
            # backfill's "remove it if it should not exist" outcome —
            # resolved. Complete it; never RECREATE an unplanned file (that
            # would spin a create→remove→create loop).
            if getattr(goal, "origin", "") == "create_backfill":
                goal.status = "complete"
                logger.info(
                    "Structural sweep: %s (backfill) removed — goal completed",
                    file_path,
                )
                if effects:
                    await effects.save_mission(mission)
                continue

            # File doesn't exist — create it.
            # Use the goal description as the directive — for data files,
            # this contains the content brief with creative requirements.
            directive = goal.description
            if not directive or directive == file_path:
                directive = (
                    f"Create {file_path} according to the architecture specification."
                )

            dispatch_config = {
                "goal_id": goal.id,
                "goal_description": goal.description,
                "goal_type": "structural",
                "goal_files": [file_path],
                "flow": "file_ops",
                "target_file_path": file_path,
                "flow_directive": directive,
                "recent_reports": [],
            }
            logger.info("Structural sweep: creating %s", file_path)
            return StepOutput(
                result={"sweep_complete": False, "needs_create": True},
                observations=f"Structural sweep: {file_path} does not exist — creating",
                context_updates={"dispatch_config": dispatch_config},
            )

        # ── Verify-only rung (cheapest): deterministic re-certification ──
        # A regression-reopened goal whose reports were archived at its
        # earlier completion arrives here EVIDENCE-LESS, so the report-based
        # auto-complete below can never fire — and before this rung existed,
        # every such goal fell through to a generic fixing dispatch
        # (bossgame2_adaptive long run: 541 fixing dispatches, 0 cheap
        # auto-completes, 486 whole-file rewrites of files that were almost
        # always fine — ~2.5-3h/day of avoidable LLM work). The goal DID
        # pass its gate once (reports_archived / last_completed_at prove
        # it); re-run the deterministic checks and re-certify on pass. On
        # fail, fall through to the normal repair paths with real evidence.
        if (
            not goal.reports
            and getattr(goal, "regression_reopened", False)
            and (
                getattr(goal, "reports_archived", 0)
                or getattr(goal, "last_completed_at", "")
            )
        ):
            from agent.actions.batch_structural_actions import (
                action_run_batch_file_checks,
            )
            from agent.models import StepInput as _StepInput

            check_out = await action_run_batch_file_checks(
                _StepInput(context={"files_changed": [file_path]}, effects=effects)
            )
            per_file = (
                (check_out.context_updates or {})
                .get("batch_check_results", {})
                .get(file_path, {})
            )
            if per_file.get("passed"):
                goal.status = "complete"
                goal.regression_reopened = False
                logger.info(
                    "Structural sweep: %s re-certified deterministically "
                    "(verify-only rung — checks pass, no LLM dispatch)",
                    file_path,
                )
                if effects:
                    await effects.save_mission(mission)
                continue
            logger.info(
                "Structural sweep: %s failed deterministic re-cert (%s) — "
                "routing to repair with gate output",
                file_path,
                ", ".join(per_file.get("checks_failed", []) or []) or "?",
            )
            recert_gate_output = _cap_diagnostic(per_file.get("output") or "", 800)
        else:
            recert_gate_output = ""

        # File exists but goal is incomplete — check if we can auto-complete
        # based on the latest report, or if it needs fixing
        block_reason = None
        if goal.reports:
            last_report = goal.reports[-1]
            report_flow = getattr(last_report, "flow", "")
            report_status = getattr(last_report, "status", "")
            checks_failed = getattr(last_report, "checks_failed", [])

            # Gate: syntax always blocks; a never-reviewed import failure blocks
            # for one fix-or-defer decision pass; lint is optional. See
            # structural_block_reason.
            block_reason = structural_block_reason(goal, checks_failed)

            if (
                report_flow in ("file_ops", "build_structure")
                and report_status == "success"
                and block_reason is None
            ):
                # Required checks pass — auto-complete this goal
                goal.status = "complete"
                logger.info("Structural sweep: %s auto-completed", file_path)
                if effects:
                    await effects.save_mission(mission)
                continue  # Move to next file

        # ── Parallel mode: diagnose-first repair ──────────────────
        # The serial path's inline self-correct loop already retried
        # inside file_ops; a batch-created file got no such loop, and
        # the failure class differs (a fresh file failing its gate vs.
        # an edit regressing). Route to diagnose_issue first, then map
        # the diagnosis to a file_ops patch — the same two-step the
        # quality sweep uses. The import fix-or-defer DECISION pass
        # stays on the shared file_ops path below (it's a judgment
        # call, not a defect investigation).
        if mode == "batch" and goal.reports and block_reason != "import":
            last = goal.reports[-1]
            last_flow = getattr(last, "flow", "")
            # diagnose_batch books the same structured diagnosis contract
            # as diagnose_issue (one-shot triage burst) — both map to a
            # file_ops patch here.
            if last_flow in ("diagnose_issue", "diagnose_batch"):
                # HONOR recommended_flow=project_ops (2026-08-03, the title-
                # match root cause). The functional sweep has honored it since
                # b75; THIS branch forced every diagnosis into a file_ops
                # patch — so a diagnosis that correctly said "environment,
                # not code" (env.json's bare-`python` syntax gate on a
                # python3-only machine) was routed into the module-frame
                # editor, whose schema can only express a code statement, and
                # "ensure the environment provides python and ruff" was
                # reified as `assert shutil.which('python') ...` in
                # parser.py — converting soft gate failures into a hard
                # import failure and costing ~10 cycles of thrash.
                if (getattr(last, "recommended_flow", "") or "") == "project_ops":
                    diag_summary = getattr(last, "summary", "") or "no details"
                    dispatch_config = {
                        "goal_id": goal.id,
                        "goal_description": goal.description,
                        "goal_type": "structural",
                        "goal_files": [file_path],
                        "flow": "project_ops",
                        "target_file_path": "",
                        "flow_directive": (
                            f"Fix the environment/tooling issue blocking "
                            f"{file_path}'s validation gate — the diagnosis "
                            f"found no code defect.\n"
                            f"Diagnosis: {diag_summary[:500]}"
                        ),
                        "recent_reports": [],
                    }
                    logger.info(
                        "Structural sweep: dispatching project_ops from "
                        "diagnosis for %s (env/tooling, not code)",
                        file_path,
                    )
                    return StepOutput(
                        result={"sweep_complete": False, "needs_fix": True},
                        observations=(
                            f"Structural sweep: project_ops fix for {file_path}"
                        ),
                        context_updates={"dispatch_config": dispatch_config},
                    )
                fileops = _fileops_dispatch_from_quality_diagnosis(
                    last, goal_id=goal.id, goal_description=goal.description
                )
                if fileops:
                    fileops["goal_type"] = "structural"
                    fileops["target_file_path"] = (
                        fileops.get("target_file_path") or file_path
                    )
                    fileops["goal_files"] = [fileops["target_file_path"]]
                    await _note_repair_econ(
                        effects, mission, working_dir, file_path, "patch", last
                    )
                    logger.info(
                        "Structural sweep: patching %s from diagnosis", file_path
                    )
                    return StepOutput(
                        result={"sweep_complete": False, "needs_fix": True},
                        observations=(
                            f"Structural sweep: patching {file_path} from diagnosis"
                        ),
                        context_updates={"dispatch_config": fileops},
                    )
                # Diagnosis produced no actionable target — fall through
                # to the generic file_ops fix directive below.
            elif getattr(last, "status", "") == "failed":
                failed_checks = ", ".join(getattr(last, "checks_failed", []) or [])
                error_output = getattr(last, "terminal_output", "") or ""
                issue = (
                    f"{file_path} was just created but fails its validation "
                    f"gate ({failed_checks or 'see output'})."
                )
                diag_siblings = _related_goal_context(mission, goal, [file_path])
                dispatch_config = {
                    "goal_id": goal.id,
                    "goal_description": goal.description,
                    "goal_type": "structural",
                    "goal_files": [file_path],
                    "flow": "diagnose_issue",
                    "target_file_path": file_path,
                    "flow_directive": (
                        "A freshly created file failed its validation gate. "
                        "Diagnose the root cause and identify the specific "
                        f"file and symbol to change:\n{issue}"
                        + (
                            f"\n\nGate output:\n{error_output[:800]}"
                            if error_output
                            else ""
                        )
                        + (f"\n\n{diag_siblings}" if diag_siblings else "")
                    ),
                    "what_happened": issue,
                    "error_headline": issue[:80],
                    "error_output": error_output[:800],
                    "recent_reports": [],
                }
                await _note_repair_econ(
                    effects, mission, working_dir, file_path, "diagnose", last
                )
                logger.info("Structural sweep: diagnosing %s", file_path)
                return StepOutput(
                    result={"sweep_complete": False, "needs_fix": True},
                    observations=f"Structural sweep: diagnosing {file_path}",
                    context_updates={"dispatch_config": dispatch_config},
                )

        # File exists, goal incomplete, needs fixing
        # Build a fix directive from the last report if available
        fix_directive = f"Fix issues in {file_path}."
        if block_reason == "import" and goal.reports:
            # Surface the import failure as a fix-or-defer DECISION, and mark it
            # reviewed so it won't be re-litigated (one pass only).
            last = goal.reports[-1]
            goal.import_reviewed = True
            # Persist the one-shot flag NOW so a reload next cycle doesn't
            # re-trigger the review (which would loop).
            if effects:
                await effects.save_mission(mission)
            fix_directive = (
                f"{file_path} compiles but FAILS TO IMPORT "
                f"({', '.join(getattr(last, 'checks_failed', []))}). "
                f"Decide: if this is a real import bug — a wrong or relative "
                f"import path (e.g. 'from .x' with no package), a circular "
                f"import, or importing a name that does not exist — FIX it now. "
                f"If it fails ONLY because a project module you depend on has "
                f"not been created yet, make NO change; it will resolve once "
                f"that module exists. {getattr(last, 'summary', '')[:200]}"
            )
            logger.info("Structural sweep: import review for %s", file_path)
        elif block_reason == "lint" and goal.reports:
            # Fallback ask, for the paths the triage burst does not cover
            # (serial mode; a goal already in the triaged ledger). Same
            # one-pass contract: flag set on ASK, persisted before dispatch.
            #
            # Framed as a DECISION rather than an order because the history
            # here is a hard-blocking gate that deadlocked on an unfixable
            # finding. "Fix it if you can, say so if you can't" is the whole
            # point; declining is a legitimate, terminal answer.
            last = goal.reports[-1]
            goal.lint_reviewed = True
            if effects:
                await effects.save_mission(mission)
            lint_out = _cap_diagnostic(getattr(last, "terminal_output", "") or "", 800)
            fix_directive = (
                f"{file_path} compiles and imports but FAILS LINT "
                f"({', '.join(getattr(last, 'checks_failed', []))}). The "
                f"auto-fixable findings have already been applied, so what "
                f"remains needed a human-shaped decision. Decide: if this is a "
                f"real defect — an undefined name, an unused or missing "
                f"import, a shadowed binding — FIX it now. If it is a style "
                f"preference, or a finding you cannot resolve without changing "
                f"behaviour, make NO change and say why. You get ONE pass; the "
                f"goal proceeds either way."
                + (f"\n\nLint output:\n{lint_out}" if lint_out else "")
            )
            logger.info("Structural sweep: lint review for %s", file_path)
        elif goal.reports:
            last = goal.reports[-1]
            if getattr(last, "checks_failed", []):
                fix_directive = (
                    f"Fix validation issues in {file_path}: "
                    f"{', '.join(last.checks_failed)}"
                )
            elif getattr(last, "summary", ""):
                fix_directive = f"Fix {file_path}: {last.summary[:200]}"

        # Thread the actual gate output (import traceback, lint finding with
        # file:line) into the dispatch. Scan back to the NEWEST report that
        # carries both checks_failed and terminal_output — later failed edit
        # attempts bury the validation report under inference-error noise,
        # and without this the fix flow's "Validation errors to fix" prompt
        # section is empty (the 2026-07-16 bossgame loop rewrote engine.py
        # 360× knowing only "syntax, import, lint").
        gate_output = ""
        for rep in reversed(goal.reports or []):
            if getattr(rep, "checks_failed", None) and getattr(
                rep, "terminal_output", ""
            ):
                gate_output = _cap_diagnostic(str(rep.terminal_output), 800)
                break
        # A failed verify-only re-cert produced FRESH gate output moments ago
        # (and such goals have no reports to scan) — it wins over stale finds.
        if recert_gate_output:
            gate_output = recert_gate_output
            fix_directive = (
                f"{file_path} was previously complete but fails its "
                f"deterministic re-certification after a related edit. "
                f"{fix_directive}"
            )

        # Sibling constraints ride the directive itself — the one carrier
        # every fix sub-flow (module fix, add-symbol, data edit, rewrite)
        # already renders, so the data path gets it without new plumbing.
        siblings = _related_goal_context(mission, goal, [file_path])
        if siblings:
            fix_directive = f"{fix_directive}\n\n{siblings}"

        dispatch_config = {
            "goal_id": goal.id,
            "goal_description": goal.description,
            "goal_type": "structural",
            "goal_files": [file_path],
            "flow": "file_ops",
            "target_file_path": file_path,
            "flow_directive": fix_directive,
            "error_output": gate_output,
            "recent_reports": [],
        }
        logger.info("Structural sweep: fixing %s", file_path)
        return StepOutput(
            result={"sweep_complete": False, "needs_fix": True},
            observations=f"Structural sweep: {file_path} needs fixing",
            context_updates={"dispatch_config": dispatch_config},
        )

    # All structural goals are complete — phase-exit seam gate.
    #
    # The cross-module gates (transfer-shape + typecheck, 671ee57) run only
    # inside build_structure, so a SERIAL-mode mission (game_challenge_boss
    # pins serial) never executes them: per-file checks all pass while the
    # assembled modules disagree at their seams. Live cost of the gap: the
    # 2026-07-23 dense-mistral artifact carried three cross-module shape
    # bugs (constructor arity, required-kwarg, raw-string-vs-Command), each
    # only discoverable at ~7 min/diagnose in the functional phase. Run the
    # deterministic gates once here, over the assembled fileset, before the
    # phase may close.
    #
    # Failure dispatches a fix WITHOUT reopening the goal — reopening would
    # ping-pong with the verify-only rung above (single-file checks cannot
    # see seams, so it would blindly re-certify). The gate itself blocks
    # phase exit until the seams clear, bounded by _SEAM_GATE_MAX_ATTEMPTS
    # (then fail-open with a note: a stubborn false positive must not wedge
    # the mission; the gates are conservative so this should be rare).
    seam_dispatch = await _seam_gate_cadence(
        mission, effects, sweep_files, at_exit=True
    )
    if seam_dispatch is not None:
        return seam_dispatch

    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={"sweep_complete": True},
        observations="Structural sweep complete — all files created and validated",
    )


_SEAM_GATE_MAX_ATTEMPTS = 3

# ── §18/§19 seam-gate run memory (process-local, per mission) ────────
# Keyed by mission id. Holds the previous gate run's symbol snapshot (for the
# live→dead REGRESSION check), the fileset hash (cadence throttle: re-gate
# only when content actually changed), and the run counter (the §19 zero-run
# warning at park). Process-local on purpose: a resume rebuilds the baseline
# on its first gate run, which costs one un-diffed check and nothing else —
# no persistence-model changes, no migration surface.
_SEAM_GATE_MEMO: dict[str, dict[str, Any]] = {}


def _seam_gate_memo(mission: Any) -> dict[str, Any]:
    key = str(getattr(mission, "id", "") or id(mission))
    return _SEAM_GATE_MEMO.setdefault(
        key, {"defined": set(), "dead": set(), "runs": 0, "fileset_hash": ""}
    )


async def _seam_gate_cadence(
    mission: Any, effects: Any, sweep_files: list[str], *, at_exit: bool = False
) -> StepOutput | None:
    """§19: gate on every FILESET CHANGE, not only at phase exit.

    The exit-only design left coverage anti-correlated with need: a run that
    never completed the structural phase was never seam-checked at all —
    gemma-31b did 109 work cycles with zero gate runs, and arm14 shipped a
    reachable six-attribute mismatch the gate parses for, unchecked, because
    19 cycles never once exited the phase.

    Throttle is a content hash of the readable structural .py set: unchanged
    files never re-gate (the gate is deterministic — same input, same answer),
    and every real edit is gated on the next sweep pass. The hash is stored
    BEFORE the gate runs so a blocking dispatch cannot re-trigger itself on
    an unchanged tree.
    """
    py = [f for f in sweep_files if str(f).endswith(".py")]
    if len(py) < 2:
        # At phase exit, delegate so the gate logs its own inert verdict —
        # "no seam-gate lines" must keep meaning NEVER RAN, not ran-quietly
        # (the §19 diagnosability rule). Mid-walk, silence is correct: this
        # branch recurs every cycle during early serial creation.
        return await _phase_exit_seam_gate(mission, effects) if at_exit else None
    h = hashlib.sha256()
    readable = 0
    for f in sorted(set(py)):
        try:
            fc = await effects.read_file(f)
        except Exception:  # noqa: BLE001 — unreadable file simply isn't hashed
            continue
        if getattr(fc, "exists", False):
            readable += 1
            h.update(str(f).encode())
            h.update(b"\x00")
            h.update((getattr(fc, "content", "") or "").encode())
    if readable < 2:
        return await _phase_exit_seam_gate(mission, effects) if at_exit else None
    digest = h.hexdigest()
    memo = _seam_gate_memo(mission)
    if memo["fileset_hash"] == digest:
        # Unchanged tree. Mid-walk: never re-gate (deterministic — same
        # input, same answer). At phase exit: skip only when the last verdict
        # on this exact content was CLEAN; an unresolved BLOCK keeps its
        # re-run pressure so the attempts bound can do its job.
        if not at_exit or memo.get("last_clean", False):
            return None
    memo["fileset_hash"] = digest
    dispatch = await _phase_exit_seam_gate(mission, effects)
    memo["last_clean"] = dispatch is None
    return dispatch


async def _phase_exit_seam_gate(mission: Any, effects: Any) -> StepOutput | None:
    """Cross-module seam gate at serial structural-phase exit.

    Runs the deterministic transfer-shape and typecheck analyses over the
    assembled structural fileset. Returns a fixing-dispatch StepOutput when
    seams are found (goals stay COMPLETE — the gate itself blocks phase
    exit), or None when clean, inert (<2 py files), or attempt-bounded.

    EVERY outcome logs. The gate originally returned None silently on all
    three pass paths, which made it unobservable in run logs: "no seam-gate
    lines" could not distinguish *ran and passed* from *never ran*, so its
    live validation could never be closed by evidence. Clean and inert log
    INFO; the fail-open bound logs WARNING (a silent fail-open is exactly
    the state an operator must not miss).
    """
    from agent.actions.batch_structural_actions import (
        _symbol_reachability,
        _transfer_shape_violations,
    )
    from agent.actions.contract_swarm_actions import action_run_contract_typecheck
    from agent.models import FlowMeta as _FlowMeta
    from agent.models import StepInput as _StepInput
    from agent.persistence.models import NoteRecord

    if effects is None or mission is None:
        return None
    files: list[str] = []
    for g in getattr(mission, "goals", []) or []:
        if getattr(g, "type", "") == "structural":
            files.extend(
                f for f in (g.associated_files or []) if str(f).endswith(".py")
            )
    files = sorted(set(files))
    if len(files) < 2:
        logger.info(
            "Seam gate: inert — %d python file(s) in the structural set "
            "(cross-module checks need 2+)",
            len(files),
        )
        return None

    attempts = sum(
        1
        for n in (getattr(mission, "notes", []) or [])
        if "seam_gate" in (getattr(n, "tags", None) or [])
    )
    if attempts >= _SEAM_GATE_MAX_ATTEMPTS:
        logger.warning(
            "Seam gate: attempt bound reached (%d/%d) — failing OPEN, phase "
            "exits with UNRESOLVED seams (evidence in the seam_gate notes)",
            attempts,
            _SEAM_GATE_MAX_ATTEMPTS,
        )
        return None  # fail-open: earlier notes carry the unresolved seams

    sources: dict[str, str] = {}
    for f in files:
        try:
            fc = await effects.read_file(f)
            if getattr(fc, "exists", False):
                sources[f] = getattr(fc, "content", "") or ""
        except Exception:  # noqa: BLE001 — unreadable file simply isn't gated
            continue
    if len(sources) < 2:
        logger.info(
            "Seam gate: inert — %d of %d structural file(s) readable",
            len(sources),
            len(files),
        )
        return None

    problems: dict[str, list[str]] = {}
    for f, vs in _transfer_shape_violations(sources).items():
        problems.setdefault(f, []).extend(vs)

    tc_out = await action_run_contract_typecheck(
        _StepInput(
            context={"files_changed": list(sources)},
            effects=effects,
            meta=_FlowMeta(
                flow_name="mission_control", step_id="structural_sweep_next"
            ),
        )
    )
    for f, entry in (
        (tc_out.context_updates or {}).get("batch_check_results", {}) or {}
    ).items():
        if not entry.get("passed", True):
            out = entry.get("output") or "typecheck failed"
            problems.setdefault(f, []).append(out[:500])

    # ── reachability split ────────────────────────────────────────────
    # A seam every one of whose access sites is unreachable cannot affect the
    # artifact's behaviour, and must not consume the fix budget. Five of the
    # eight historical fail-opens were one such seam
    # (GameEngine._handle_flee, called only from a dispatcher nothing wired
    # up) re-reported at five successive phase exits. Still reported — dead
    # code carrying a real AttributeError is cruft — just not blocking.
    reach = _symbol_reachability(sources)
    _dead: set[str] = reach["dead"]
    _sites: dict[str, set[str]] = reach["access_sites"]
    _defined: set[str] = reach.get("defined") or set()

    # ── §18 regression check: live at the previous gate run, dead now ──
    # A symbol whose LAST caller an edit removed. This is the shape the plain
    # dead set cannot gate on (dead code is normal — 47 symbols across 12
    # campaign arms — and computed-name dispatch fakes it), but a live→dead
    # TRANSITION is neither: static false-dead is stable across runs, so it
    # never transitions, and a genuine severed call does. arm13: a lint fix
    # rewrote GameEngine and deleted the only initiate_combat() call; the
    # gate ran, logged clean, and the artifact shipped unwinnable.
    memo = _seam_gate_memo(mission)
    prev_live: set[str] = (memo["defined"] - memo["dead"]) if memo["runs"] else set()
    regressed_by_file: dict[str, list[str]] = {}
    for q in sorted(_dead & prev_live & _defined):
        regressed_by_file.setdefault(q.split("::", 1)[0], []).append(q)
    memo["runs"] += 1
    memo["defined"] = set(_defined)
    # Baseline retention: keep currently-flagged symbols in the LIVE baseline
    # (subtract them from the stored dead set) so an unresolved regression
    # re-flags on the next run instead of silently becoming the new normal.
    # It clears only by being called again or by its definition being deleted.
    memo["dead"] = set(_dead) - {q for qs in regressed_by_file.values() for q in qs}

    def _missing_attr(v: str) -> str:
        m = re.search(r"has no attribute/method '([A-Za-z_]\w*)'", v)
        return m.group(1) if m else ""

    blocking: dict[str, list[str]] = {}
    unreachable: list[str] = []
    for f, vs in problems.items():
        for v in vs:
            attr = _missing_attr(v)
            holders = _sites.get(attr) or set()
            # Only suppress when we positively know every access site AND all
            # of them are dead. No sites found = keep it blocking.
            if attr and holders and holders <= _dead:
                unreachable.append(v)
            else:
                blocking.setdefault(f, []).append(v)

    cruft = [
        f"dead duplicate: {d} duplicates live {live}" for d, live in reach["dead_dupes"]
    ] + [
        f"orphaned method at module level (fell out of its class): {o}"
        for o in reach["orphans"]
    ]
    if unreachable or cruft:
        for line in unreachable:
            logger.info("Seam gate: unreachable seam (dead code) — %s", line)
        for line in cruft:
            logger.info("Seam gate: %s", line)
        mission.notes.append(
            NoteRecord(
                content=(
                    "seam gate: structural cruft that does not affect behaviour "
                    "(NOT blocking, no fix attempt spent): "
                    + "; ".join(unreachable + cruft)[:600]
                ),
                category="failure_analysis",
                tags=["seam_gate_advisory"],
                source_flow="structural_sweep",
            )
        )
        await effects.save_mission(mission)

    # §18: regressions are BLOCKING and join after the reachability split —
    # they are not typecheck mismatches, so the suppression logic above never
    # sees them (and must not: suppressing a regression because the symbol is
    # dead would be circular — dead is what the regression IS).
    for f, qs in regressed_by_file.items():
        tgt = f if f in sources else sorted(sources)[0]
        blocking.setdefault(tgt, []).extend(
            f"REGRESSION: {q} was reachable at the previous seam check and "
            f"now has no callers — an edit removed the last call to it"
            for q in qs
        )

    if not blocking:
        logger.info(
            "Seam gate: clean — %d file(s) checked (transfer-shape + typecheck "
            "+ live-set diff), no reachable mismatches (%d unreachable "
            "seam(s), %d cruft item(s) reported)",
            len(sources),
            len(unreachable),
            len(cruft),
        )
        return None
    problems = blocking

    target = sorted(problems)[0]
    # Per-problem caps (§21): the old global [:800] truncated later problems
    # out of the fix directive entirely on multi-problem gates. Regressions
    # lead — they are the highest-signal entries — then everything else,
    # each capped alone, total bounded.
    _ordered = sorted(
        (v for vs in problems.values() for v in vs),
        key=lambda v: 0 if v.startswith("REGRESSION:") else 1,
    )
    seams = "\n".join(v[:300] for v in _ordered)[:1600]
    # Direction: a missing DEFINITION is not a wrong call. The old directive
    # said "fix {target} so its cross-module calls match", which points the
    # model at the call site — so a missing method got its caller re-edited
    # three times and the definition was never written (devstral, 3 identical
    # attempts). Name the absent members and say where they belong.
    missing = sorted(
        {a for vs in problems.values() for a in (_missing_attr(v) for v in vs) if a}
    )
    goal = next(
        (
            g
            for g in mission.goals
            if getattr(g, "type", "") == "structural"
            and target in (g.associated_files or [])
        ),
        None,
    )
    mission.notes.append(
        NoteRecord(
            content=(
                f"seam gate: cross-module interface check failed at structural "
                f"phase exit (attempt {attempts + 1}/{_SEAM_GATE_MAX_ATTEMPTS}): "
                f"{seams[:300]}"
            ),
            category="failure_analysis",
            tags=["seam_gate"],
            source_flow="structural_sweep",
        )
    )
    await effects.save_mission(mission)
    dispatch_config = {
        "goal_id": goal.id if goal else "",
        "goal_description": (
            goal.description if goal else "cross-module interface consistency"
        ),
        "goal_type": "structural",
        "goal_files": [target],
        "flow": "file_ops",
        "target_file_path": target,
        "flow_directive": (
            (
                # §18 regression: the defect is a SEVERED CALL, not a wrong
                # one — pointing the model at "fix the calls" would have it
                # re-edit correct code. Name what was orphaned and offer the
                # deliberate-removal exit (deleting the dead definition also
                # clears the check), so an intentional cut cannot wedge.
                f"A recent edit severed the last call to: "
                f"{', '.join(q for qs in regressed_by_file.values() for q in qs)}. "
                f"The definition(s) still exist and are now unreachable. "
                f"Restore the call path the edit removed — look at what "
                f"recently changed in {target}, not at the definitions. If "
                f"the removal was intentional, delete the now-dead "
                f"definition(s) as well:\n{seams}"
            )
            if any(v.startswith("REGRESSION:") for v in problems.get(target, []))
            else (
                (
                    f"Interface check failed at structural phase exit. The "
                    f"following members are CALLED but never DEFINED: "
                    f"{', '.join(missing)}. Add the missing definition(s) to the "
                    f"class that should own them in {target}. The call sites are "
                    f"correct — do not edit them, and do not delete the calls:\n"
                    f"{seams}"
                )
                if missing
                else (
                    f"Cross-module interface check failed at structural phase exit. "
                    f"Fix {target} so its cross-module calls match what the other "
                    f"modules actually define and return:\n{seams}"
                )
            )
        ),
        "error_output": seams,
        "recent_reports": [],
    }
    logger.info(
        "Structural sweep: seam gate failed — fixing %s (attempt %d/%d)",
        target,
        attempts + 1,
        _SEAM_GATE_MAX_ATTEMPTS,
    )
    return StepOutput(
        result={"sweep_complete": False, "needs_fix": True},
        observations=f"Seam gate: cross-module mismatch — fixing {target}",
        context_updates={"dispatch_config": dispatch_config},
    )


async def _sweep_first_test(
    goal: Any,
    mission: Any,
    effects: Any,
    goal_mode: str,
    run_command: str,
    interactive_prompt: str,
) -> StepOutput:
    """First dispatch for a goal with no reports yet: repair-test-loop
    derivation, capability_absent explore, confirmed-defect diagnose
    (repair / quality_gate origin), or the default verify-interact."""
    # Repair test loop (Phase B.5): on a repair-profile mission, the
    # repo's OWN failing tests are the goal's ground truth. Derive them
    # once and dispatch a DETERMINISTIC pytest verification (zero
    # inference) — its pytest output (failing node ids) flows into the
    # diagnose seed as error_output, so the fix loop sees exactly how
    # the code is called. Falls through to the normal dispatch when no
    # suite matches or the profile isn't repair.
    #
    # capability_absent goals are NOT excluded here: on a repair
    # mission the "absent capability" has a failing test naming it —
    # the test IS the build spec (fsspec: `test_open_async` calls the
    # missing method with the exact signature). The first retest
    # excluded them and BOTH repo-scale tasks silently skipped the
    # whole loop, falling back to exploratory interact + static-grep
    # acceptance checks (the signature-blind trap this loop replaces).
    # The explore-charter path remains for non-repair missions.
    from agent.actions.pipeline_actions import (
        derive_repair_tests,
        is_repair_profile,
    )

    # Held-out-test missions (SWE-bench) skip the repair-test loop
    # entirely: no in-repo test indicts the bug (the regression test is
    # held out), so a baseline-failing witness is always a red-herring
    # that hijacks the goal into a deterministic verify against an
    # effectively-green suite. Fall through to diagnose-first, which
    # drives off the problem statement.
    held_out = getattr(getattr(mission, "config", None), "held_out_tests", False)
    if (
        is_repair_profile(mission)
        and not held_out
        and not (getattr(goal, "repair_tests", None) or {}).get("derived")
    ):
        rt = await derive_repair_tests(effects, goal.description)
        goal.repair_tests = rt or {"derived": True}
        if rt.get("command"):
            cmds = {c.get("command") for c in (goal.acceptance_checks or [])}
            if rt["command"] not in cmds:
                goal.acceptance_checks = list(goal.acceptance_checks or []) + [
                    {"command": rt["command"], "name": "repair suite", "required": True}
                ]
            if effects:
                await effects.save_mission(mission)
            dispatch_config = {
                "goal_id": goal.id,
                "goal_description": goal.description,
                "goal_type": "functional",
                "goal_files": goal.associated_files or [],
                "flow": "interact",
                "target_file_path": "",
                "flow_directive": (
                    "Verify this repair against the repo's own tests:\n"
                    + goal.description
                ),
                "interaction_mode": "deterministic",
                "run_command": rt["command"],
                "interactive_prompt": "",
            }
            logger.info(
                "Functional sweep: repair test-loop for %s → %s",
                goal.description[:50],
                rt["test_files"],
            )
            return StepOutput(
                result={"sweep_complete": False, "needs_test": True},
                observations=(
                    f"Functional sweep: repair suite {rt['test_files']} "
                    f"for '{goal.description[:50]}'"
                ),
                context_updates={"dispatch_config": dispatch_config},
            )
        if effects:
            await effects.save_mission(mission)
        # No matching suite — fall through to the normal dispatch.
    # capability_absent goals (brownfield directive) name a feature that
    # does NOT exist yet — a thing to BUILD, not verify. Default interact
    # would charter "prove this works" and immediately fail on absence.
    # Instead run an absence-aware explore session (charter_mode=explore)
    # that reads player-view placement, then diagnose explores the code
    # and patch builds it. Subsequent reports ride the normal report-walk
    # below (interact success completes; a diagnose->file_ops cycle
    # re-tests), so only the FIRST dispatch differs.
    if getattr(goal, "capability_absent", False):
        directive = (
            "This capability does not exist yet — it is a feature to "
            "BUILD, not a bug to reproduce. Explore the running program "
            "and the code to find where it fits, then describe what to "
            "build:\n" + goal.description
        )
        dispatch_config = {
            "goal_id": goal.id,
            "goal_description": goal.description,
            "goal_type": "functional",
            "goal_files": goal.associated_files or [],
            "flow": "interact",
            "target_file_path": "",
            "flow_directive": directive,
            "interaction_mode": "exploratory",
            "charter_mode": "explore",
            "run_command": "",
            "interactive_prompt": interactive_prompt,
        }
        logger.info("Functional sweep: exploring to build %s", goal.description[:50])
        return StepOutput(
            result={"sweep_complete": False, "needs_test": True},
            observations=(
                f"Functional sweep: exploring to build " f"'{goal.description[:50]}'"
            ),
            context_updates={"dispatch_config": dispatch_config},
        )
    # Repair-profile fix goals are ALREADY-CONFIRMED defects: the user
    # filed the bug (the problem statement IS the report) and a hidden
    # test pins it. In SWE-bench that test is HELD OUT, so the repo's
    # baseline is green and the repair-test loop above finds no witness
    # → without this branch the goal falls to the default "verify it
    # works" interact, which trivially passes on the held-out test and
    # completes the goal with ZERO edits (pilot-2: 5 empty patches,
    # gold-file hit 8→2). Route straight to diagnose -> file_ops from
    # the problem statement — the confirmed-defect polarity that forces
    # a surgical fix AND localizes (diagnose explores to name the
    # target). Only for non-capability_absent goals (a real fix, not a
    # feature to build).
    if is_repair_profile(mission) and not getattr(goal, "capability_absent", False):
        directive = (
            "This is a confirmed defect reported against existing code "
            "(a hidden test pins it). Diagnose the root cause and name "
            "the specific existing file and symbol to change — make the "
            "SMALLEST edit that fixes the reported behavior:\n" + goal.description
        )
        directive += _goal_repro_block(goal)
        dispatch_config = {
            "goal_id": goal.id,
            "goal_description": goal.description,
            "goal_type": "functional",
            "goal_files": goal.associated_files or [],
            "flow": "diagnose_issue",
            "target_file_path": "",
            "flow_directive": directive,
            "what_happened": goal.description,
            "error_headline": goal.description[:80],
        }
        logger.info(
            "Functional sweep: diagnosing repair defect %s",
            goal.description[:50],
        )
        return StepOutput(
            result={"sweep_complete": False, "needs_fix": True},
            observations=f"Functional sweep: diagnosing repair defect '{goal.description[:50]}'",
            context_updates={"dispatch_config": dispatch_config},
        )
    # quality_gate-origin goals are ALREADY-CONFIRMED defects (the gate
    # found them). Re-reproducing one via interact mis-frames a bug
    # report as a capability to "verify works" and stochastically
    # false-passes (the goal-driven validation thrashed a startup crash
    # through ~10 false-pass/re-gate rounds before a diagnose finally
    # ran). Go straight to diagnose -> file_ops; the post-fix interact
    # re-test (defect-resolution polarity) is the real verification.
    if getattr(goal, "origin", "design") == "quality_gate":
        directive = (
            "A quality-gate review reported this defect. Diagnose the "
            "root cause and identify the specific file and symbol to "
            "change:\n" + goal.description
        )
        directive += _goal_repro_block(goal)
        dispatch_config = {
            "goal_id": goal.id,
            "goal_description": goal.description,
            "goal_type": "functional",
            "goal_files": goal.associated_files or [],
            "flow": "diagnose_issue",
            "target_file_path": "",
            "flow_directive": directive,
            "what_happened": goal.description,
            "error_headline": goal.description[:80],
        }
        logger.info(
            "Functional sweep: diagnosing reported defect %s",
            goal.description[:50],
        )
        return StepOutput(
            result={"sweep_complete": False, "needs_fix": True},
            observations=f"Functional sweep: diagnosing reported defect '{goal.description[:50]}'",
            context_updates={"dispatch_config": dispatch_config},
        )
    # design-origin: reproduce/verify the capability via interact
    dispatch_config = {
        "goal_id": goal.id,
        "goal_description": goal.description,
        "goal_type": "functional",
        "goal_files": goal.associated_files or [],
        "flow": "interact",
        "target_file_path": "",
        "flow_directive": (
            f"Test this capability: {goal.description}\n"
            f"Run the program and verify the described behavior works correctly."
        ),
        "interaction_mode": goal_mode,
        "run_command": run_command if goal_mode == "deterministic" else "",
        "interactive_prompt": interactive_prompt,
    }
    logger.info("Functional sweep: testing %s", goal.description[:50])
    return StepOutput(
        result={"sweep_complete": False, "needs_test": True},
        observations=f"Functional sweep: testing '{goal.description[:50]}'",
        context_updates={"dispatch_config": dispatch_config},
    )


async def _sweep_capability_build(goal: Any, last_report: Any) -> StepOutput:
    """Route an explored capability_absent goal to diagnose -> file_ops
    so the scouted feature actually gets built."""
    directive = (
        "Build this capability from the exploration and placement notes "
        "above. Diagnose what file and symbol to create or extend, and "
        "how it connects to the existing structure:\n" + goal.description
    )
    dispatch_config = {
        "goal_id": goal.id,
        "goal_description": goal.description,
        "goal_type": "functional",
        "goal_files": goal.associated_files or [],
        "flow": "diagnose_issue",
        "target_file_path": "",
        "flow_directive": directive,
        "what_happened": getattr(last_report, "summary", ""),
        "error_headline": getattr(last_report, "headline", "") or goal.description[:80],
    }
    logger.info(
        "Functional sweep: building explored capability %s",
        goal.description[:50],
    )
    return StepOutput(
        result={"sweep_complete": False, "needs_fix": True},
        observations=(
            f"Functional sweep: building explored capability "
            f"'{goal.description[:50]}'"
        ),
        context_updates={"dispatch_config": dispatch_config},
    )


async def _sweep_after_file_ops(
    goal: Any,
    mission: Any,
    effects: Any,
    last_report: Any,
    report_status: str,
    goal_mode: str,
    run_command: str,
    interactive_prompt: str,
) -> StepOutput:
    """After a file_ops fix attempt: record the attempt, then re-test on
    success (repair suite with collection floor when applicable) or
    re-diagnose on bail/error."""
    from agent.persistence.models import FailedAttempt

    # Record every file_ops completion as an attempt, regardless
    # of status.  A "successful" fix that doesn't resolve the
    # test failure is just as important a signal as a bail —
    # both indicate the diagnosis targeted the wrong file or
    # the wrong aspect of the problem.
    fops_summary = getattr(last_report, "summary", "no details")
    fops_files = getattr(last_report, "files_affected", [])
    fops_target = fops_files[0] if fops_files else ""
    # Prefer the structured target_symbol on the report
    # (populated by Phase A from the flat diagnosis schema).
    # If absent — older cycles pre-redesign — leave blank; the
    # diagnose seed's target-repeat detection then just matches
    # on target_file alone, still useful.
    fops_target_symbol = getattr(last_report, "target_symbol", "") or ""

    prior_diag_summary, prior_interact_headline = _prior_diagnosis_context(goal)

    goal.failed_attempts.append(
        FailedAttempt(
            target_file=fops_target,
            target_symbol=fops_target_symbol,
            flow="file_ops",
            reason=fops_summary,
            diagnosis_summary=prior_diag_summary,
            pre_headline=prior_interact_headline,
        )
    )

    if report_status == "success":
        # Repair goal: re-test against the repo's OWN suite, and run a
        # cheap COLLECTION FLOOR first. An edit that breaks imports (the
        # astropy `str | None` on py3.9) fails the WHOLE suite at
        # collection — pytest returns INTERNALERROR / parser_results
        # null, which reads as an unparseable grade. Catch it with a
        # `--collect-only` and route straight back to diagnose with the
        # import error, UNLESS the baseline already couldn't collect
        # (unbuilt checkout → stand down, never blame the edit).
        rt = getattr(goal, "repair_tests", None) or {}
        if rt.get("command") and effects is not None:
            if rt.get("collect_ok", True) and rt.get("test_files"):
                from agent.actions.pipeline_actions import _parse_pytest_output

                collect_cmd = "python -m pytest --collect-only -q " + " ".join(
                    rt["test_files"]
                )
                collect_ok_now = True
                cout = ""
                try:
                    cres = await effects.run_command(
                        ["/bin/sh", "-c", collect_cmd], timeout=60
                    )
                    cout = (getattr(cres, "stdout", "") or "") + (
                        getattr(cres, "stderr", "") or ""
                    )
                    _n, collect_ok_now = _parse_pytest_output(cout)
                except Exception:
                    collect_ok_now = True  # infra miss → don't block
                if not collect_ok_now:
                    logger.info(
                        "Functional sweep: fix broke test collection for "
                        "%s — re-diagnosing",
                        goal.description[:50],
                    )
                    dispatch_config = {
                        "goal_id": goal.id,
                        "goal_description": goal.description,
                        "goal_type": "functional",
                        "goal_files": goal.associated_files or [],
                        "flow": "diagnose_issue",
                        "target_file_path": "",
                        "flow_directive": (
                            "The last edit broke test COLLECTION — the "
                            "suite no longer imports. Fix the import/"
                            "syntax breakage (this is collateral damage, "
                            "not the original bug):\n" + goal.description
                        ),
                        "error_output": cout[:4000],
                        "what_happened": "the fix broke test collection",
                        "error_headline": "test collection failed after edit",
                        "failed_attempts_context": [
                            {
                                "target_file": a.target_file,
                                "target_symbol": getattr(a, "target_symbol", ""),
                                "flow": a.flow,
                                "reason": a.reason,
                                "diagnosis_summary": a.diagnosis_summary,
                                "pre_headline": getattr(a, "pre_headline", ""),
                            }
                            for a in goal.failed_attempts
                        ],
                    }
                    if effects:
                        await effects.save_mission(mission)
                    return StepOutput(
                        result={"sweep_complete": False, "needs_fix": True},
                        observations=(
                            f"Functional sweep: fix broke collection for "
                            f"'{goal.description[:50]}' — re-diagnosing"
                        ),
                        context_updates={"dispatch_config": dispatch_config},
                    )
            # Collection clean (or baseline stood down) — re-test on the
            # repo's own suite, deterministically.
            dispatch_config = {
                "goal_id": goal.id,
                "goal_description": goal.description,
                "goal_type": "functional",
                "goal_files": goal.associated_files or [],
                "flow": "interact",
                "target_file_path": "",
                "flow_directive": _functional_retest_directive(goal, after="fix"),
                "interaction_mode": "deterministic",
                "run_command": rt["command"],
                "interactive_prompt": "",
            }
            logger.info(
                "Functional sweep: re-testing %s after fix (repair suite)",
                goal.description[:50],
            )
            if effects:
                await effects.save_mission(mission)
            return StepOutput(
                result={"sweep_complete": False, "needs_test": True},
                observations=(
                    f"Functional sweep: re-testing '{goal.description[:50]}' "
                    "after fix (repair suite)"
                ),
                context_updates={"dispatch_config": dispatch_config},
            )
        # Fix applied — re-test to see if it actually resolved
        # the functional failure
        dispatch_config = {
            "goal_id": goal.id,
            "goal_description": goal.description,
            "goal_type": "functional",
            "goal_files": goal.associated_files or [],
            "flow": "interact",
            "target_file_path": "",
            "flow_directive": _functional_retest_directive(goal, after="fix"),
            "interaction_mode": goal_mode,
            "run_command": run_command if goal_mode == "deterministic" else "",
            "interactive_prompt": interactive_prompt,
        }
        logger.info("Functional sweep: re-testing %s after fix", goal.description[:50])
        if effects:
            await effects.save_mission(mission)
        return StepOutput(
            result={"sweep_complete": False, "needs_test": True},
            observations=f"Functional sweep: re-testing '{goal.description[:50]}' after fix",
            context_updates={"dispatch_config": dispatch_config},
        )
    else:
        # Fix failed (bail or error) — re-diagnose with
        # accumulated attempt context

        # Serialize all attempts for the renderer
        failed_attempts_data = [
            {
                "target_file": a.target_file,
                "target_symbol": getattr(a, "target_symbol", ""),
                "flow": a.flow,
                "reason": a.reason,
                "diagnosis_summary": a.diagnosis_summary,
                "pre_headline": getattr(a, "pre_headline", ""),
            }
            for a in goal.failed_attempts
        ]

        terminal_output = getattr(last_report, "terminal_output", "")
        # This path triggers after a file_ops that bailed or
        # errored without ever running a functional test — so
        # the last report is file_ops, not interact. No fresh
        # headline to compare against; the new seed will show
        # Prior attempts without a Before/After pair.
        error_description = (
            f"Previous fix attempts failed for: {goal.description}\n\n"
            f"The editor rejected these targets — re-diagnose with a "
            f"different approach or different file.\n"
        )

        dispatch_config = {
            "goal_id": goal.id,
            "goal_description": goal.description,
            "goal_type": "functional",
            "goal_files": [],
            "flow": "diagnose_issue",
            "target_file_path": "",
            "flow_directive": error_description,
            "error_output": terminal_output,
            "what_happened": getattr(last_report, "summary", ""),
            "error_headline": getattr(last_report, "headline", ""),
            "failed_attempts_context": failed_attempts_data,
        }
        logger.info(
            "Functional sweep: re-diagnosing '%s' after %d failed attempt(s)",
            goal.description[:50],
            len(goal.failed_attempts),
        )
        if effects:
            await effects.save_mission(mission)
        return StepOutput(
            result={"sweep_complete": False, "needs_fix": True},
            observations=f"Functional sweep: re-diagnosing '{goal.description[:50]}' after bail ({len(goal.failed_attempts)} failed attempts)",
            context_updates={"dispatch_config": dispatch_config},
        )


def _prior_diagnosis_context(goal: Any) -> tuple[str, str]:
    """The diagnosis that led to the latest attempt, plus the headline of the
    interact that triggered it.

    The headline becomes a ``pre_headline`` so the next diagnose cycle can
    render a before/after regression comparison. Shared by the file_ops and
    project_ops routes so both record attempts the same way.
    """
    prior_diag_summary = ""
    prior_interact_headline = ""
    saw_diag = False
    for prev_report in reversed(goal.reports[:-1]):
        flow = getattr(prev_report, "flow", "")
        if flow == "diagnose_issue" and not prior_diag_summary:
            prior_diag_summary = getattr(prev_report, "summary", "")
            saw_diag = True
        elif saw_diag and flow == "interact":
            prior_interact_headline = getattr(prev_report, "headline", "")
            break
    return prior_diag_summary, prior_interact_headline


# The repeat-target warning keys on "target_file:target_symbol" and skips empty
# keys, so an environment fix needs a stable non-empty marker to accumulate a
# count. Re-exported rather than redeclared so the marker the sweep WRITES and
# the marker the diagnosis seed READS cannot drift: on a drift the env attempt
# falls through to the file phrasing and renders "editing <environment> has
# failed to resolve the goal", which is nonsense the model cannot act on.
from agent.actions.diagnosis_session_actions import (  # noqa: E402
    ENV_ATTEMPT_TARGET as _ENV_ATTEMPT_TARGET,
)


async def _sweep_after_project_ops(
    goal: Any,
    last_report: Any,
    report_status: str,
    goal_mode: str,
    run_command: str,
    interactive_prompt: str,
) -> StepOutput:
    """After a project_ops env/dep fix: re-test on success, re-diagnose
    on failure."""
    # RECORD THE ATTEMPT, exactly as the file_ops route does. Without this,
    # goal.failed_attempts stayed [] forever on the environment route, which
    # silently disabled three mechanisms that are all already built: the
    # "## Prior attempts" seed section, the repeat-target CRITICAL warning, and
    # the stuck-goal web-search gate (which needs >= 2 attempts). The model
    # therefore re-diagnosed from a byte-identical seed every cycle and,
    # correctly, produced an identical conclusion — 26 times in one run
    # (dev/POOLSIDE_TRAP_ROOTCAUSE.md).
    #
    # Appended before branching on status, mirroring file_ops: project_ops
    # reporting "success" only means its install commands exited 0, not that
    # the goal is fixed. If the re-test below passes, the goal completes and
    # this record is discarded with it.
    from agent.persistence.models import FailedAttempt

    prior_diag_summary, prior_interact_headline = _prior_diagnosis_context(goal)
    goal.failed_attempts.append(
        FailedAttempt(
            target_file=_ENV_ATTEMPT_TARGET,
            target_symbol="",
            flow="project_ops",
            reason=(getattr(last_report, "summary", "") or "no details")[:500],
            diagnosis_summary=prior_diag_summary,
            pre_headline=prior_interact_headline,
        )
    )

    if report_status == "success":
        # Environment fix applied — re-test the goal
        dispatch_config = {
            "goal_id": goal.id,
            "goal_description": goal.description,
            "goal_type": "functional",
            "goal_files": goal.associated_files or [],
            "flow": "interact",
            "target_file_path": "",
            "flow_directive": _functional_retest_directive(
                goal, after="environment fix"
            ),
            "interaction_mode": goal_mode,
            "run_command": run_command if goal_mode == "deterministic" else "",
            "interactive_prompt": interactive_prompt,
        }
        logger.info(
            "Functional sweep: re-testing %s after project_ops fix",
            goal.description[:50],
        )
        return StepOutput(
            result={"sweep_complete": False, "needs_test": True},
            observations=f"Functional sweep: re-testing '{goal.description[:50]}' after project_ops",
            context_updates={"dispatch_config": dispatch_config},
        )
    else:
        # project_ops failed — re-diagnose to find a different approach
        error_description = (
            f"Environment fix failed for: {goal.description}\n\n"
            f"project_ops reported: {getattr(last_report, 'summary', 'no details')[:500]}\n"
        )
        dispatch_config = {
            "goal_id": goal.id,
            "goal_description": goal.description,
            "goal_type": "functional",
            "goal_files": [],
            "flow": "diagnose_issue",
            "target_file_path": "",
            "flow_directive": error_description,
            "what_happened": getattr(last_report, "summary", ""),
            "error_headline": getattr(last_report, "headline", ""),
        }
        logger.info(
            "Functional sweep: re-diagnosing %s after project_ops failure",
            goal.description[:50],
        )
        return StepOutput(
            result={"sweep_complete": False, "needs_fix": True},
            observations=f"Functional sweep: re-diagnosing '{goal.description[:50]}' after project_ops failure",
            context_updates={"dispatch_config": dispatch_config},
        )


async def _sweep_after_diagnose(
    goal: Any,
    mission: Any,
    effects: Any,
    last_report: Any,
) -> StepOutput:
    """After a diagnose_issue report: extract the structured fix target
    (Phase A operation spec) and dispatch file_ops/project_ops, with the
    junk-target and evasion-loop guards."""
    diag_summary = getattr(last_report, "summary", "")
    diag_files = getattr(last_report, "files_affected", [])
    recommended_flow = getattr(last_report, "recommended_flow", "") or "file_ops"

    # Phase A (patch redesign) — read structured operation spec
    # from the report. Diagnose's flat schema gives us the
    # target_file + optional target_symbol + change_spec
    # directly; we prefer these over the legacy files_affected
    # derivation. file_ops routes internally: path doesn't
    # exist → create; path exists + symbol in AST → patch;
    # path exists + symbol absent → add_symbol (Phase D);
    # otherwise → rewrite.
    struct_target_file = getattr(last_report, "target_file", "") or ""
    struct_target_symbol = getattr(last_report, "target_symbol", "") or ""
    struct_change_spec = getattr(last_report, "change_spec", "") or ""
    struct_kind = getattr(last_report, "diagnosis_kind", "") or ""
    # Structured module-fix declaration — the literal module-level line
    # accompanying kind == "module_fix"; file_ops's
    # check_module_fix routes on it.
    struct_module_statement = getattr(last_report, "module_statement", "") or ""
    # Multi-symbol patching (505 round). When diagnose
    # emits a list of co-dependent symbols, we thread them
    # through to file_ops → patch so the rewrite_queue
    # picks them up alongside target_symbol. Empty list
    # means the change is local to target_symbol, which is
    # the majority of cases.
    struct_related_symbols = list(getattr(last_report, "related_symbols", []) or [])

    # b75 regression guard — the model sometimes emits
    # placeholder markers from the CONCLUDE_PROMPT example
    # ('path/to/file.py', '<file>') or hedging tokens
    # ('UNKNOWN', 'N/A', '?') when it can't identify a target.
    # Treat these as empty so downstream sees the absence
    # rather than a bogus path.
    _junk_target_tokens = {
        "",
        "unknown",
        "n/a",
        "?",
        "path/to/file.py",
        "path/to/file",
        "<file>",
        "<path>",
        "<real_path_in_this_project>",
        "<classname.method_or_function_name>",
    }
    if struct_target_file.strip().lower() in _junk_target_tokens:
        logger.warning(
            "Functional sweep: diagnose returned junk target_file %r "
            "(kind=%r, recommended_flow=%r); treating as empty",
            struct_target_file,
            struct_kind,
            recommended_flow,
        )
        struct_target_file = ""
    if struct_target_symbol.strip().lower() in _junk_target_tokens:
        struct_target_symbol = ""

    # Evasion-loop guard — b75 showed 5 cycles thrashing when
    # diagnose couldn't identify a target and fell through to
    # recommended_flow=project_ops with a generic "gather more
    # evidence" change_spec. project_ops then edits README /
    # pyproject.toml cosmetically, interact still fails, next
    # diagnose produces the same evasion. If diagnose couldn't
    # name a target AND the change_spec is meta-advice rather
    # than a real project_ops directive, fail the goal's
    # current attempt cleanly so the mission moves on.
    _evasion_spec_markers = (
        "obtain",
        "gather",
        "provide concrete",
        "collect additional",
        "more diagnostic",
        "additional evidence",
    )
    change_spec_lower = struct_change_spec.strip().lower()
    looks_like_evasion = (
        not struct_target_file
        and recommended_flow == "project_ops"
        and any(
            change_spec_lower.startswith(marker) for marker in _evasion_spec_markers
        )
    )
    if looks_like_evasion:
        logger.warning(
            "Functional sweep: diagnose evaded with empty target + "
            "generic 'gather evidence' change_spec (%r); skipping "
            "project_ops dispatch for '%s'",
            struct_change_spec[:80],
            goal.description[:50],
        )
        if effects:
            await effects.save_mission(mission)
        return StepOutput(
            result={"sweep_complete": False, "skip_goal": True},
            observations=(
                f"Functional sweep: diagnose produced no actionable "
                f"target for '{goal.description[:50]}'; skipping "
                f"this cycle to avoid thrash"
            ),
            context_updates={},
        )

    # If diagnosis recommends project_ops (dependency/env fix),
    # dispatch directly — no file target needed.
    if recommended_flow == "project_ops":
        fix_directive = (
            f"Fix the environment/dependency issue that prevents: {goal.description}\n"
            f"Diagnosis: {diag_summary[:500]}"
        )
        dispatch_config = {
            "goal_id": goal.id,
            "goal_description": goal.description,
            "goal_type": "functional",
            "goal_files": [],
            "flow": "project_ops",
            "target_file_path": "",
            "flow_directive": fix_directive,
        }
        logger.info(
            "Functional sweep: dispatching project_ops from diagnosis for '%s'",
            goal.description[:50],
        )
        if effects:
            await effects.save_mission(mission)
        return StepOutput(
            result={"sweep_complete": False, "needs_fix": True},
            observations=f"Functional sweep: project_ops fix for '{goal.description[:50]}'",
            context_updates={"dispatch_config": dispatch_config},
        )

    # file_ops path. Prefer structured target_file from the
    # flat diagnosis; fall back to legacy files_affected[0]
    # only when the diagnose session ran under an older path
    # that didn't populate the structured field.
    fix_target = struct_target_file or (diag_files[0] if diag_files else "")

    if fix_target:
        # Diagnosis explicitly named a file — reopen its structural goal with
        # full regression provenance (flag + note), not a bare status flip.
        _reopen_structural_goal(mission, fix_target, goal.id, "functional_sweep")

        # Editing a file can break (or fix) the program's startup —
        # re-open the startup goal so the startup check re-runs.
        _regress_startup_goal(mission)

        fix_directive = (
            f"Fix the issue in {fix_target} that prevents: {goal.description}\n"
            f"Diagnosis: {diag_summary[:500]}"
        )
        dispatch_config = {
            "goal_id": goal.id,
            "goal_description": goal.description,
            "goal_type": "functional",
            "goal_files": [fix_target],
            "flow": "file_ops",
            "target_file_path": fix_target,
            "flow_directive": fix_directive,
            # Phase A / D — structured fields for file_ops
            # routing. target_symbol lets file_ops choose
            # patch (symbol exists in AST) vs add_symbol
            # (symbol missing from AST). change_spec feeds
            # each sub-flow's authoring prompt.
            "target_symbol": struct_target_symbol,
            "change_spec": struct_change_spec,
            "diagnosis_kind": struct_kind,
            "module_statement": struct_module_statement,
            # Multi-symbol patching (505 round). Passed
            # through file_ops input_map → patch input_map
            # → prepare_next_rewrite, which seeds the
            # rewrite queue with the primary target plus
            # these related symbols so they're all
            # rewritten in one atomic batch with shared
            # context.
            "related_symbols": struct_related_symbols,
        }
        logger.info("Functional sweep: applying fix to %s from diagnosis", fix_target)
        if effects:
            await effects.save_mission(mission)
        return StepOutput(
            result={"sweep_complete": False, "needs_fix": True},
            observations=f"Functional sweep: applying diagnosis fix to {fix_target}",
            context_updates={"dispatch_config": dispatch_config},
        )
    else:
        # No explicit file target — let LLM select from project files
        logger.info(
            "Functional sweep: diagnosis for '%s' needs target resolution via LLM menu",
            goal.description[:50],
        )
        dispatch_config = {
            "goal_id": goal.id,
            "goal_description": goal.description,
            "goal_type": "functional",
            "goal_files": [],
            "flow": "file_ops",
            "target_file_path": "",
            "flow_directive": (
                f"Fix the issue that prevents: {goal.description}\n"
                f"Diagnosis: {diag_summary[:500]}"
            ),
            "diagnosis_summary": diag_summary,
        }
        return StepOutput(
            result={"sweep_complete": False, "needs_target_resolution": True},
            observations=f"Functional sweep: needs LLM to select fix target for '{goal.description[:50]}'",
            context_updates={"dispatch_config": dispatch_config},
        )


async def _sweep_interact_failure(
    goal: Any,
    mission: Any,
    effects: Any,
    last_report: Any,
) -> StepOutput:
    """Fallback: interact failed — dispatch diagnose_issue with the
    accumulated attempt history."""
    # interact failed — dispatch diagnose_issue to identify root cause
    # and the correct file to fix. Diagnosis uses LLM analysis of the
    # error context rather than fragile regex on tracebacks.
    terminal_output = getattr(last_report, "terminal_output", "")
    summary = getattr(last_report, "summary", "")
    headline = getattr(last_report, "headline", "")

    error_description = (
        f"Functional test failed for: {goal.description}\n\n"
        f"Test summary: {summary}\n"
    )

    dispatch_config = {
        "goal_id": goal.id,
        "goal_description": goal.description,
        "goal_type": "functional",
        "goal_files": [],
        "flow": "diagnose_issue",
        "target_file_path": "",
        "flow_directive": error_description,
        "error_output": terminal_output,
        # Structured fields for the new diagnose seed (Goal /
        # What happened / Prior attempts). The seed-building
        # action reads these directly instead of re-parsing
        # error_description.
        "what_happened": summary,
        "error_headline": headline,
    }

    # Pass accumulated attempt history so the diagnosis model
    # knows what has already been tried (even if those attempts
    # reported "success" but didn't resolve the test failure).
    if goal.failed_attempts:
        dispatch_config["failed_attempts_context"] = [
            {
                "target_file": a.target_file,
                "target_symbol": getattr(a, "target_symbol", ""),
                "flow": a.flow,
                "reason": a.reason,
                "diagnosis_summary": a.diagnosis_summary,
                "pre_headline": getattr(a, "pre_headline", ""),
            }
            for a in goal.failed_attempts
        ]

    logger.info(
        "Functional sweep: diagnosing '%s' after interact failure",
        goal.description[:50],
    )
    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"sweep_complete": False, "needs_fix": True},
        observations=f"Functional sweep: diagnosing '{goal.description[:50]}'",
        context_updates={"dispatch_config": dispatch_config},
    )


async def action_functional_sweep_next(step_input: StepInput) -> StepOutput:
    """Find the next incomplete functional goal and determine what it needs.

    For each incomplete functional goal (in derivation order):
      - No interact report yet → needs_test=True (dispatch interact)
      - Last interact succeeded (goal_met) → mark complete, continue
      - Last interact failed → needs_fix=True (dispatch diagnose_issue)
      - Last report was a fix attempt → needs_test=True (re-test after fix)

    When no goal can be advanced (all tested, all fix targets unknown),
    returns sweep_complete=True to break the loop and advance to quality gate.

    Context required: mission
    Publishes: dispatch_config
    """
    mission = step_input.context.get("mission")
    effects = step_input.effects

    if not mission:
        return StepOutput(
            result={"sweep_complete": True},
            observations="No mission — skip functional sweep",
        )

    functional = [g for g in mission.goals if g.type == "functional"]
    incomplete = [g for g in functional if g.status == "incomplete"]

    if not incomplete:
        return StepOutput(
            result={"sweep_complete": True},
            observations="All functional goals complete",
        )

    # Deterministic goals run via run_commands and need the self-
    # terminating smoke command, not the interactive launch.
    arch = getattr(mission, "architecture", None)
    run_command = ""
    if arch:
        run_command = getattr(arch, "effective_smoke_command", "") or ""

    # Load interactive_prompt from env config (set by set_env). Read via effects
    # so the path resolves against the mission working_directory, not the agent
    # process cwd (a bare relative Path read the repo's own .agent/).
    interactive_prompt = ""
    if effects is not None:
        try:
            fc = await effects.read_file(".agent/env.json")
            if getattr(fc, "exists", False):
                env_data = json.loads(getattr(fc, "content", "") or "") or {}
                interactive_prompt = env_data.get("interactive_prompt", "")
        except Exception:
            pass  # Non-critical — prompt detection falls back to heuristics

    # Track whether we can make progress on any goal
    made_progress = False

    for goal in incomplete:
        # Determine interaction mode from goal metadata
        goal_mode = getattr(goal, "interaction_mode", None) or ""

        # Check the latest report to determine what this goal needs
        if not goal.reports:
            return await _sweep_first_test(
                goal, mission, effects, goal_mode, run_command, interactive_prompt
            )

        last_report = goal.reports[-1]
        report_flow = getattr(last_report, "flow", "")
        report_status = getattr(last_report, "status", "")

        # capability_absent goal, BUILD phase: the explore-interact session
        # scouted placement (it produces a build spec, not a working feature),
        # so route it to diagnose -> file_ops to actually build — regardless of
        # the session's pass/fail. Only applies before a build has happened (no
        # file_ops yet); once built, the normal report-walk below re-tests and
        # completes it like any functional goal.
        if (
            getattr(goal, "capability_absent", False)
            and report_flow == "interact"
            and not any(getattr(r, "flow", "") == "file_ops" for r in goal.reports)
        ):
            return await _sweep_capability_build(goal, last_report)

        # interact success means goal_met was true (the flow routes on this)
        if report_flow == "interact" and report_status == "success":
            goal.status = "complete"
            # Bidirectional regression sweep: a genuine full-bar re-cert clears
            # the sweep-provenance marker (MANDATORY — a later harvester/test-gate
            # reopen must not inherit stale auto-complete eligibility) and re-arms
            # the flip-flop guard (permissive: a future distinct break may
            # auto-complete again after a real re-verification here).
            goal.regression_reopened = False
            goal.regression_autocompleted = False
            # failed_attempts survive completion — the archive sweep
            # relocates them (retry patterns are mining material).
            logger.info("Functional sweep: '%s' completed", goal.description[:50])
            if effects:
                await effects.save_mission(mission)
            made_progress = True
            continue

        # Last report was file_ops — check if it succeeded or failed
        if report_flow == "file_ops":
            return await _sweep_after_file_ops(
                goal,
                mission,
                effects,
                last_report,
                report_status,
                goal_mode,
                run_command,
                interactive_prompt,
            )

        # Last report was project_ops — env/dep fix applied, re-test
        if report_flow == "project_ops":
            return await _sweep_after_project_ops(
                goal,
                last_report,
                report_status,
                goal_mode,
                run_command,
                interactive_prompt,
            )

        # Last report was diagnose_issue — extract fix target and dispatch
        if report_flow == "diagnose_issue":
            return await _sweep_after_diagnose(goal, mission, effects, last_report)

        return await _sweep_interact_failure(goal, mission, effects, last_report)

    # All functional goals visited — did we make progress?
    if effects:
        await effects.save_mission(mission)

    if made_progress:
        # Some goals completed this pass — check again
        return StepOutput(
            result={"sweep_complete": False, "needs_test": False, "needs_fix": False},
            observations="Functional sweep: some goals completed, re-checking",
        )

    # No progress possible — all goals either complete or stuck
    # Advance to quality gate rather than looping
    return StepOutput(
        result={"sweep_complete": True},
        observations="Functional sweep: no further progress possible — advancing to quality gate",
    )


# ══════════════════════════════════════════════════════════════════════
# Quality findings -> classified goals (harvest) + quality sweep
# ══════════════════════════════════════════════════════════════════════
#
# When the quality gate fails, action_harvest_quality_findings turns each
# finding (quality_results.fix_tasks, labelled functional|quality by summarize)
# into a goal. Functional findings ride functional_sweep_next (diagnose -> fix
# -> interact re-test); quality (cosmetic/content) findings ride
# action_quality_sweep_next (diagnose -> file_ops -> complete-on-patch). Bounded
# by the mission cycle budget; dedup is at goal CREATION (finding_signature), and
# a completed goal whose finding the gate re-reports is re-opened for a retry.

# Placeholder/hedge tokens diagnose emits when it can't name a real target.
# Kept local (duplicates the functional sweep's guard) to avoid touching the
# hot functional path.
_JUNK_TARGET_TOKENS = {
    "",
    "unknown",
    "n/a",
    "?",
    "path/to/file.py",
    "path/to/file",
    "<file>",
    "<path>",
    "<real_path_in_this_project>",
    "<classname.method_or_function_name>",
}


# Code-locus anchors for the finding signature: Python filenames, and
# dotted/snake_case identifiers (must contain a '.' or '_' joining alnum runs,
# so plain English words don't match). Line numbers are stripped first — they
# shift after edits and the model phrases them inconsistently.
_SIG_LINE_NO = re.compile(r"\bline\s+\d+\b", re.I)
_SIG_PY_FILE = re.compile(r"\b[\w-]+\.py\b")
_SIG_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[._][A-Za-z0-9_]+)+")


# Claim text inside apply_verification_results' refuted-claim telemetry
# notes ("Quality gate claimed: <claim> — probe REFUTED it: ...").
_REFUTED_CLAIM_RE = re.compile(
    r"Quality gate claimed:\s*(.*?)\s*—\s*probe REFUTED", re.S
)

# A completed goal reopened this many times by its OWN deterministic shape
# signature (behavior passed, shape-check keeps re-flagging) is a probable
# false positive → suppress. Matches the probe-refuted K above.
_SHAPE_REFUTE_K = 2


def _quality_finding_signature(fix_task: Any) -> str:
    """Stable dedup key for a quality finding, robust to LLM rephrasing.

    The gate re-reports the SAME defect with different framing each round
    ("references undefined X" / "raises NameError for X" / "crashes on startup
    with X"), so a literal-text key spawns a fresh goal every round and burns
    the whole cycle budget (the goal-driven validation produced 10 duplicate
    goals for one injected crash). Anchor instead on the code locus the finding
    names — Python filenames and dotted/snake_case identifiers — which survive
    rephrasing. Fall back to normalized prose only when the finding names no
    code anchor (rare; those don't recur as duplicates in practice)."""
    # Deterministic sources (the shape checker) precompute exact
    # signatures — honor them so the same violation maps to the same
    # goal every round, no prose in the loop.
    if isinstance(fix_task, dict) and fix_task.get("signature"):
        return str(fix_task["signature"])
    text = _quality_finding_text(fix_task)
    if not text:
        return ""
    low = _SIG_LINE_NO.sub(" ", text.lower())
    # The untested marker is framing, not identity: "take command was not
    # exercised" and "untested: take command was not exercised" are the
    # same finding (live-observed: the prefix variation across gate
    # rounds spawned duplicate goals that re-litigated completed work).
    low = low.strip()
    if low.startswith("untested:"):
        low = low[len("untested:") :].strip()
    anchors = {
        a
        for a in (set(_SIG_PY_FILE.findall(low)) | set(_SIG_IDENT.findall(low)))
        if len(a) >= 4 and a not in _JUNK_TARGET_TOKENS
    }
    if anchors:
        return "|".join(sorted(anchors))
    return " ".join(low.split())[:200]


def _quality_finding_text(fix_task: Any) -> str:
    if isinstance(fix_task, dict):
        return str(fix_task.get("issue") or fix_task.get("description") or "").strip()
    return str(fix_task).strip()


def _goal_repro_block(goal: Any) -> str:
    """Render a goal's probe-verified repro + evidence for a flow directive.

    Verify-before-harvest goals carry the exact stdin sequence that
    demonstrated the defect at the gate; diagnose and the post-fix re-test
    start from it instead of re-deriving a scenario from prose."""
    repro = list(getattr(goal, "repro_commands", None) or [])
    if not repro:
        return ""
    lines = "\n".join(f"  {i}. {cmd}" for i, cmd in enumerate(repro, 1))
    block = (
        "\nVerified reproduction (each line is typed into the running "
        f"program, in order):\n{lines}"
    )
    evidence = str(getattr(goal, "verification_evidence", "") or "").strip()
    if evidence:
        block += f"\nObserved when reproduced: {evidence[:300]}"
    return block


def _functional_retest_directive(goal: Any, *, after: str) -> str:
    """interact re-test directive after a fix, polarity-correct per goal origin.

    A design-origin functional goal's description is a CAPABILITY to verify
    ("player can pick up items") — "verify the described behavior works" is
    right. A quality_gate-origin goal's description is a DEFECT report
    ("main.py crashes on startup") — verifying that "works correctly" is
    nonsense and false-passes, so frame it as defect-resolution instead."""
    desc = getattr(goal, "description", "")
    if getattr(goal, "origin", "design") == "quality_gate":
        directive = (
            f"A quality-gate review reported this defect: {desc}\n"
            f"A {after} was just applied. Run the program and verify the defect "
            f"NO LONGER occurs — the program runs and the affected behavior is "
            f"correct. If the defect still reproduces, report failure."
        )
        repro_block = _goal_repro_block(goal)
        if repro_block:
            directive += (
                f"{repro_block}\n"
                "Re-run this exact sequence and confirm the defect no longer occurs."
            )
        return directive
    return (
        f"Re-test this capability after a {after}: {desc}\n"
        f"Run the program and verify the described behavior works correctly."
    )


def _rget(report: Any, key: str, default: Any = "") -> Any:
    """Read a field from a directive report that may be a dict or a model."""
    if isinstance(report, dict):
        return report.get(key, default)
    return getattr(report, key, default)


def _diag_dispatch_from_quality_finding(
    issue: str, *, goal_id: str = "", file_hint: str = "", repro_block: str = ""
) -> dict:
    """diagnose_issue dispatch_config for a quality goal's finding (free-text).
    goal_id binds the diagnosis report back to the quality goal."""
    issue = str(issue).strip()
    return {
        "goal_id": goal_id,
        "goal_description": issue,
        "goal_type": "quality",
        "goal_files": [],
        "flow": "diagnose_issue",
        "target_file_path": str(file_hint or ""),
        "flow_directive": (
            "A quality-gate review found this issue. Diagnose the root cause "
            "and identify the specific file and symbol to change:\n"
            + issue
            + repro_block
        ),
        "what_happened": issue,
        "error_headline": issue[:80],
    }


def _fileops_dispatch_from_quality_diagnosis(
    report: Any, *, goal_id: str = "", goal_description: str = ""
) -> dict | None:
    """Map a diagnose_issue report (dict OR DirectiveReport) to a file_ops
    dispatch_config for a quality goal. None when diagnose produced no actionable
    file target (junk token / no target / project_ops)."""
    recommended = (str(_rget(report, "recommended_flow", "")) or "file_ops").strip()
    target_file = str(_rget(report, "target_file", "") or "").strip()
    target_symbol = str(_rget(report, "target_symbol", "") or "").strip()
    if target_file.lower() in _JUNK_TARGET_TOKENS:
        target_file = ""
    if target_symbol.lower() in _JUNK_TARGET_TOKENS:
        target_symbol = ""
    if recommended == "project_ops" or not target_file:
        return None
    summary = str(_rget(report, "summary", ""))
    return {
        "goal_id": goal_id,
        "goal_description": goal_description or "quality finding",
        "goal_type": "quality",
        "goal_files": [target_file],
        "flow": "file_ops",
        "target_file_path": target_file,
        "flow_directive": f"Fix the quality issue in {target_file}:\n{summary[:500]}",
        "target_symbol": target_symbol,
        "change_spec": str(_rget(report, "change_spec", "") or ""),
        "diagnosis_kind": str(_rget(report, "diagnosis_kind", "") or ""),
        "module_statement": str(_rget(report, "module_statement", "") or ""),
        "related_symbols": list(_rget(report, "related_symbols", []) or []),
    }


async def action_harvest_quality_findings(step_input: StepInput) -> StepOutput:
    """Turn quality-gate findings into goals — one per finding, classified by the
    summarize ``class``. Reached from dispatch_quality_gate on failure.

    Per finding (keyed by normalized signature):
      - no existing goal      -> create a new incomplete goal (type = class)
      - existing & complete   -> RE-OPEN it (the fix didn't hold; retry — bounded
                                 only by the mission cycle budget)
      - existing & incomplete -> skip (already being worked)

    Result: ``harvested`` (-> check_phase routes the new goals to their sweeps)
    or ``done`` (gate failed but produced no findings -> finalize).
    """
    from agent.persistence.models import GoalRecord

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"done": True}, observations="No mission — finalize")

    # The gate just pushed notes (survivor + refuted-claim telemetry from
    # apply_verification_results) via effects.push_note, which persisted
    # them to disk — but THIS cycle's context mission was loaded before
    # the gate ran. Saving it below would clobber those notes (observed
    # live: every gate-pushed note vanished). Freshen notes from disk
    # before mutating goals.
    if effects:
        try:
            fresh = await effects.load_mission()
            if fresh is not None and len(fresh.notes) > len(mission.notes):
                mission.notes = fresh.notes
        except Exception:
            pass

    quality_results = step_input.context.get("quality_results") or {}
    fix_tasks = (
        (quality_results.get("fix_tasks") or [])
        if isinstance(quality_results, dict)
        else []
    )
    if not fix_tasks:
        return StepOutput(
            result={"done": True},
            observations="Quality gate failed but produced no findings — finalizing",
        )

    by_sig = {
        getattr(g, "finding_signature", ""): g
        for g in mission.goals
        if getattr(g, "finding_signature", "")
    }

    # ── Refuted-signature suppression ──────────────────────────────
    # Verification telemetry is a memory, not just a log: a claim the
    # probes have refuted TWICE is a noise pattern, not a regression
    # (live: 7 claims re-raised and re-refuted up to 8x each across 52
    # rounds — 2/3 of all verification effort). One refutation stays
    # harmless (a real regression may legitimately recur); the second
    # suppresses. Keyed on the prose-normalized signature of the
    # refuted claim text from the notes, matched against the task's
    # text signature so deterministic and prose findings both match.
    refuted_counts: Counter = Counter()
    for n in mission.notes:
        rm = _REFUTED_CLAIM_RE.search(getattr(n, "content", "") or "")
        if rm:
            rsig = _quality_finding_signature({"description": rm.group(1)})
            if rsig:
                refuted_counts[rsig] += 1

    created = reopened = skipped = suppressed = 0
    for task in fix_tasks:
        sig = _quality_finding_signature(task)
        if not sig:
            continue
        text_sig = _quality_finding_signature(
            {"description": _quality_finding_text(task)}
        )
        if refuted_counts.get(text_sig, 0) >= 2:
            suppressed += 1
            continue
        cls = (
            "quality"
            if (isinstance(task, dict) and task.get("class") == "quality")
            else "functional"
        )
        repro = (
            [str(ln) for ln in (task.get("repro") or []) if str(ln).strip()]
            if isinstance(task, dict)
            else []
        )
        evidence = (
            str(task.get("verification_evidence") or "")
            if isinstance(task, dict)
            else ""
        )
        existing = by_sig.get(sig)
        if existing is not None:
            if existing.status == "complete":
                # Behavior refutes the shape check: a goal whose interact
                # PASSED (it completed), reopened ONLY by its own deterministic
                # shape signature, means the program works but the shape-check
                # keeps re-flagging it — the open-map / under-sampled-exemplar
                # false positive. A real key rename breaks behavior (interact
                # fails → the goal never completes → never lands here), so
                # suppressing this is safe. Suppress at the 2nd refute, leaving
                # the goal complete (feeds the all-suppressed finalize below).
                if sig.startswith("shape|"):
                    existing.shape_refutes += 1
                    if existing.shape_refutes >= _SHAPE_REFUTE_K:
                        suppressed += 1
                        logger.info(
                            "Quality harvest: suppressing twice-behavior-"
                            "refuted shape finding %s (goal stays complete)",
                            sig,
                        )
                        continue  # do NOT reopen — leave the goal complete
                existing.status = "incomplete"
                reopened += 1
                # The fresh finding survived a new probe — its repro and
                # evidence supersede whatever the goal carried before.
                if repro:
                    existing.repro_commands = repro
                if evidence:
                    existing.verification_evidence = evidence
            else:
                skipped += 1
            continue
        # Data-shape tasks carry the real data file (refinement_actions
        # _deterministic_shape_tasks); associating it lets the diagnose seed
        # surface the data instead of losing the link (files=[] → code-only).
        task_file = task.get("file", "") if isinstance(task, dict) else ""
        mission.goals.append(
            GoalRecord(
                description=_quality_finding_text(task) or "quality finding",
                type=cls,
                status="incomplete",
                origin="quality_gate",
                finding_signature=sig,
                interaction_mode="exploratory" if cls == "functional" else None,
                repro_commands=repro,
                verification_evidence=evidence,
                associated_files=[task_file] if task_file else [],
            )
        )
        created += 1

    if effects:
        await effects.save_mission(mission)

    logger.info(
        "Quality harvest: %d new + %d reopened goal(s) (%d in flight, %d suppressed)",
        created,
        reopened,
        skipped,
        suppressed,
    )
    if suppressed and not (created or reopened or skipped):
        # Every finding this round was a twice-refuted noise pattern.
        # A gate failing SOLELY on suppressed claims must not loop the
        # mission forever (live: 52 consecutive gate-fail rounds kept
        # alive by 7 immortal claims) — treat as no actionable findings.
        return StepOutput(
            result={"done": True},
            observations=(
                f"Quality gate: all {suppressed} finding(s) suppressed as "
                f"twice-refuted noise — no actionable findings, finalizing"
            ),
        )
    # created+reopened>0 at gate time (the gate only runs when all goals are
    # complete, so a fresh finding is new or matches a completed goal). skipped
    # is a safety branch; either way there are now incomplete goals to work, so
    # re-enter phase routing.
    return StepOutput(
        result={"harvested": True},
        observations=(
            f"Quality gate: harvested {created} new + {reopened} reopened goal(s) "
            f"({skipped} already in flight, {suppressed} suppressed)"
        ),
    )


_REGRESSION_CONCURRENCY = 8
_REGRESSION_CHECK_TIMEOUT = 30


async def action_regression_sweep(step_input: StepInput) -> StepOutput:
    """Bidirectional cross-goal regression suite. Runs required acceptance
    checks in parallel (no short-circuit), aggregated PER GOAL:

    - REOPEN direction — every COMPLETE goal's checks; if ANY fail, an edit
      regressed a verified behavior -> reopen the goal (mark regression_reopened
      so it's auto-complete-eligible; the flip-flop guard leaves it False if it
      was already auto-completed once, forcing the interact path).
    - AUTO-COMPLETE direction — goals the sweep itself reopened
      (regression_reopened, grounded); if ALL their checks pass, a collateral/
      root fix re-cleared them -> complete them (do NOT re-derive), so a root
      fix re-clears its whole blast radius in one deterministic wave instead of
      one interact cycle per goal.

    Skips the just-completed goal in the reopen direction (best-effort; the
    disarm backstop covers a missed skip). Always advances last_regression_cycle
    to disarm the PhaseRule until the next edit. A persistently-refuted
    (brittle/stateful) check is removed by the interact backstop
    (action_reconcile_acceptance) — a false positive cannot immortalize or
    wrongly-close a goal (a genuine regression keeps the check failing).

    Context: mission (required), last_goal_id (optional).
    Publishes: mission.
    """
    import asyncio

    from agent.actions.check_result import check_result
    from agent.persistence.models import NoteRecord
    from agent.trace import get_step_context

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"reopened": 0}, observations="No mission")

    skip_id = str(
        step_input.inputs.get("last_goal_id", "")
        or step_input.context.get("last_goal_id", "")
        or ""
    )
    sc = get_step_context() or {}
    cycle = int(sc.get("cycle", 0) or 0)
    # Disarm the regression PhaseRule regardless of outcome (set before the save
    # so it persists; the next file-affecting report re-arms regression_dirty).
    # The cycle field is telemetry only — restart-fragile, never load-bearing.
    mission.regression_dirty = False
    mission.last_regression_cycle = cycle

    if effects is None:
        return StepOutput(
            result={"reopened": 0},
            observations="Regression sweep: no effects",
            context_updates={"mission": mission},
        )

    # Direction 1 (reopen): completed goals' required checks — a FAIL means an
    # edit regressed a verified behavior -> reopen the owning goal.
    complete_pairs = [
        (g, c)
        for g in mission.goals
        if g.status == "complete" and g.id != skip_id
        for c in (g.acceptance_checks or [])
        if c.get("required", True)
        and isinstance(c.get("command"), str)
        and c["command"].strip()
    ]
    # Direction 2 (auto-complete): goals the SWEEP itself reopened, still
    # grounded — a PASS means a collateral/root fix re-cleared them. skip_id is
    # irrelevant here (these are incomplete, not the just-completed goal).
    recomplete_pairs = [
        (g, c)
        for g in mission.goals
        if g.status == "incomplete"
        and getattr(g, "regression_reopened", False)
        and getattr(g, "acceptance_grounded", False)
        for c in (g.acceptance_checks or [])
        if c.get("required", True)
        and isinstance(c.get("command"), str)
        and c["command"].strip()
    ]

    reopened: set[str] = set()
    autocompleted: set[str] = set()
    ran = 0
    if complete_pairs or recomplete_pairs:
        sem = asyncio.Semaphore(_REGRESSION_CONCURRENCY)

        async def _run(goal, check, direction):
            cmd = check["command"]
            async with sem:
                try:
                    res = await effects.run_command(
                        ["/bin/sh", "-c", cmd], timeout=_REGRESSION_CHECK_TIMEOUT
                    )
                    passed = res.return_code == 0 and not getattr(
                        res, "timed_out", False
                    )
                    row = check_result(
                        check.get("name", "acceptance check"),
                        cmd,
                        passed,
                        required=True,
                        stdout=res.stdout,
                        stderr=res.stderr,
                        return_code=res.return_code,
                    )
                except Exception as e:  # noqa: BLE001 — a bad check never aborts
                    row = check_result(
                        check.get("name", "acceptance check"),
                        cmd,
                        False,
                        required=True,
                        stderr=str(e),
                        return_code=1,
                    )
            return direction, goal, check, row

        outcomes = await asyncio.gather(
            *[_run(g, c, "reopen") for g, c in complete_pairs],
            *[_run(g, c, "recomplete") for g, c in recomplete_pairs],
        )
        ran = len(outcomes)

        # Aggregate PER GOAL: reopen if ANY required check failed; auto-complete
        # only if ALL required checks pass (a per-check loop would auto-complete
        # a multi-check goal on its first passing check while another failed).
        by_goal: dict = {}
        for direction, goal, check, row in outcomes:
            slot = by_goal.setdefault(
                goal.id, {"goal": goal, "direction": direction, "rows": []}
            )
            slot["rows"].append((check, row))

        for gid, slot in by_goal.items():
            goal = slot["goal"]
            rows = slot["rows"]
            if slot["direction"] == "reopen":
                failed = next((r for _, r in rows if not r["passed"]), None)
                if failed is None:
                    continue  # complete + all pass -> no-op
                goal.status = "incomplete"  # reopen idiom (harvester)
                # flip-flop guard: a goal already auto-completed once is NOT
                # eligible again — force it down the interact path (disarm-capable).
                goal.regression_reopened = not getattr(
                    goal, "regression_autocompleted", False
                )
                reopened.add(gid)
                bad_cmd = next((c["command"] for c, r in rows if not r["passed"]), "")
                mission.notes.append(
                    NoteRecord(
                        content=(
                            f"regression: goal '{goal.description[:80]}' reopened — "
                            f"an acceptance check failed after an edit (triggering "
                            f"goal_id={skip_id or 'n/a'}). check={bad_cmd[:160]} "
                            f"| rc={failed['return_code']} stderr={failed['stderr'][:160]}"
                        ),
                        category="failure_analysis",
                        tags=[
                            t
                            for t in [
                                "regression",
                                goal.finding_signature or goal.id,
                                skip_id,
                            ]
                            if t
                        ],
                        source_flow="regression_sweep_next",
                    )
                )
                logger.info("Regression sweep: reopened '%s'", goal.description[:50])
            else:  # recomplete
                if not all(r["passed"] for _, r in rows):
                    continue  # still failing -> stays reopened (no-op)
                goal.status = "complete"  # AUTO-COMPLETE (do NOT re-derive)
                goal.regression_reopened = False
                goal.regression_autocompleted = True  # arm the flip-flop guard
                autocompleted.add(gid)
                mission.notes.append(
                    NoteRecord(
                        content=(
                            f"regression resolved: goal '{goal.description[:80]}' "
                            f"auto-completed — its acceptance check(s) passed again "
                            f"after a fix (triggering goal_id={skip_id or 'n/a'})."
                        ),
                        category="general",
                        tags=[
                            t
                            for t in [
                                "regression",
                                "auto_complete",
                                goal.finding_signature or goal.id,
                                skip_id,
                            ]
                            if t
                        ],
                        source_flow="regression_sweep_next",
                    )
                )
                logger.info(
                    "Regression sweep: auto-completed '%s'", goal.description[:50]
                )

    await effects.save_mission(mission)
    n_goals = len(
        {g.id for g, _ in complete_pairs} | {g.id for g, _ in recomplete_pairs}
    )
    obs = (
        f"ran {ran} check(s) over {n_goals} goal(s) -> {len(reopened)} reopened, "
        f"{len(autocompleted)} auto-completed"
        if (complete_pairs or recomplete_pairs)
        else "no grounded checks to sweep"
    )
    return StepOutput(
        result={"reopened": len(reopened), "autocompleted": len(autocompleted)},
        observations=f"Regression sweep: {obs}",
        context_updates={"mission": mission},
    )


_TEST_GATE_SIG = "test-gate:"


async def action_run_test_suite_gate(step_input: StepInput) -> StepOutput:
    """Test-suite gate (Phase B.5): run the repo's OWN suite between functional
    completion and the quality gate; failures harvest fix goals.

    Config-togglable (mission.config.test_gate):
      off  → set tests_verified, pass through (no change for suite-less projects
             that opt out).
      auto → self-gate on detection: run when a suite is found, else set
             tests_verified silently (a project with no tests needs no config).
      on   → run when found; passes when none found (nothing to run) but never
             skips by choice.

    On failures: harvest ≤N functional fix goals (origin="test_gate",
    finding_signature per failing node, repro=[the pytest command]) — idempotent
    by signature — and DON'T set tests_verified, so the functional→fix loop runs
    first; the gate re-fires and clears once the suite passes. On a clean/absent
    suite: set tests_verified so the flag_unset phase rule stops firing.

    Context: mission (required). Publishes: mission.
    """
    from agent.actions.pipeline_actions import _parse_pytest_output, derive_repair_tests
    from agent.persistence.models import GoalRecord

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"done": True}, observations="No mission")

    mode = getattr(getattr(mission, "config", None), "test_gate", "auto")

    def _pass(reason: str) -> StepOutput:
        mission.tests_verified = True
        return StepOutput(
            result={"tests_verified": True, "harvested": 0},
            observations=f"Test gate: {reason}",
            context_updates={"mission": mission},
        )

    if mode == "off":
        return _pass("disabled (test_gate=off)")
    if getattr(getattr(mission, "config", None), "held_out_tests", False):
        # SWE-bench: the failing regression test is HELD OUT, so the repo's
        # suite cannot verify THIS fix — every baseline-failing node is a
        # pre-existing red-herring. Harvesting them manufactured phantom fix
        # goals (pilot-3: astropy 1 real goal → 9, chasing test_models.py
        # failures unrelated to the separability bug; the gate can never clear
        # because a fix can't make an unrelated pre-existing failure pass).
        return _pass("held-out tests — repo suite cannot verify the fix")
    if effects is None:
        return _pass("no effects — cannot run suite")

    # Resolve the suite command: union of goals' already-derived repair tests
    # (their test files), else derive from the objective (reuses the repair-test
    # selection). A repair mission's functional goals already carry repair_tests.
    test_files: list[str] = []
    baseline_collect_ok = True
    for g in mission.goals:
        rt = getattr(g, "repair_tests", None) or {}
        for tf in rt.get("test_files", []) or []:
            if tf not in test_files:
                test_files.append(tf)
        if rt.get("collect_ok") is False:
            baseline_collect_ok = False
    if not test_files:
        derived = await derive_repair_tests(effects, getattr(mission, "objective", ""))
        test_files = derived.get("test_files", []) or []
        if derived.get("collect_ok") is False:
            baseline_collect_ok = False

    if not test_files:
        # No suite discoverable. auto/on both pass (nothing to run); the
        # distinction only matters if we later add a hard "on requires a suite".
        return _pass("no test suite found — nothing to verify")
    if not baseline_collect_ok:
        # The suite couldn't collect at BASELINE — but baselines go STALE: on
        # the b5d fsspec run the agent fixed the collection blocker mid-mission
        # (created the missing _version module), and standing down on the stale
        # flag certified a still-failing repo. Re-check collection NOW; stand
        # down only if it is STILL broken (a genuinely unbuildable checkout —
        # never loop on an environmental failure).
        try:
            cres = await effects.run_command(
                [
                    "/bin/sh",
                    "-c",
                    "python -m pytest --collect-only -q " + " ".join(test_files[:3]),
                ],
                timeout=60,
            )
            cout = (getattr(cres, "stdout", "") or "") + (
                getattr(cres, "stderr", "") or ""
            )
            _n, collect_ok_now = _parse_pytest_output(cout)
        except Exception:
            collect_ok_now = False
        if not collect_ok_now:
            return _pass("suite still does not collect — standing down")
        logger.info("Test gate: stale baseline — collection now clean, proceeding")

    command = "python -m pytest -q --no-header " + " ".join(test_files[:3])
    try:
        # 90s cap — see the b5c teardown race note in derive_repair_tests.
        res = await effects.run_command(["/bin/sh", "-c", command], timeout=90)
        out = (getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")
        rc = getattr(res, "return_code", 1)
    except Exception as e:  # noqa: BLE001
        logger.warning("Test gate: suite run failed (%s) — standing down", e)
        return _pass(f"suite run errored ({type(e).__name__}) — standing down")

    failing_nodes, collect_ok = _parse_pytest_output(out)
    if not collect_ok:
        # Post-hoc collection break with a clean baseline → a fix broke imports;
        # harvest it as a fix goal like any other failure (node = the file).
        failing_nodes = failing_nodes or [f"{tf}::collection" for tf in test_files[:1]]
    if rc == 0 and not failing_nodes:
        return _pass(f"suite passed ({len(test_files)} file(s))")

    # Harvest fix goals from failing nodes, idempotent by signature.
    by_sig = {
        getattr(g, "finding_signature", ""): g
        for g in mission.goals
        if getattr(g, "finding_signature", "")
    }
    created = reopened = 0
    for node in failing_nodes[:8]:
        sig = _TEST_GATE_SIG + node
        existing = by_sig.get(sig)
        if existing is not None:
            if existing.status == "complete":
                existing.status = "incomplete"
                reopened += 1
            continue
        mission.goals.append(
            GoalRecord(
                description=f"Fix failing test: {node}",
                type="functional",
                status="incomplete",
                origin="test_gate",
                finding_signature=sig,
                repro_commands=[f"python -m pytest -q {node}"],
            )
        )
        created += 1

    if not (created or reopened):
        # Failures exist but every node already has an in-flight goal — avoid a
        # spin: certify so the mission can finalize rather than loop the gate.
        return _pass(
            f"{len(failing_nodes)} failing but all already in flight — finalizing"
        )

    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"tests_verified": False, "harvested": created, "reopened": reopened},
        observations=(
            f"Test gate: {len(failing_nodes)} failing node(s) → "
            f"{created} new + {reopened} reopened fix goal(s)"
        ),
        context_updates={"mission": mission},
    )


# How many genuine repair attempts an evidenced warning gets before it stands
# down. Matches _ACCEPTANCE_DISARM_K / _SHAPE_REFUTE_K — the house threshold
# for "tried twice, believe the evidence".
WARNING_MAX_ATTEMPTS = 2


async def action_warning_sweep_next(step_input: StepInput) -> StepOutput:
    """Take the next evidenced warning and build a diagnose_issue dispatch.

    THE SECOND EVIDENCE CHANNEL. Everything else in the repair loop is driven
    by a PTY session: a failure gets fixed because a session observed it. A
    deterministic check (an unaccounted runtime file, an unreachable room
    graph, a cross-module type mismatch) has no session behind it and, before
    2026-08-06, no route into repair at all — it was logged and dropped.

    THE WARNING TRAVELS AS FLOW INPUTS, NOT AS A NOTE. diagnose_issue strips
    notes from its session seed (diagnosis_session_actions.py, "cross-goal
    leak in 7e7"), so a note-borne warning arrives as nothing. That is exactly
    how the flush tripwire stayed invisible for an entire 8-hour run: it wrote
    a correct, actionable note tagged to `save.json`, and _filter_notes_for_file
    only surfaces a note to whoever is working on that file — which no goal
    ever is, because it is a runtime artifact.

    The evidence is quoted verbatim into flow_directive. A thin payload
    recreates the blind-diagnose trap, where a fixer with no evidence opens a
    session and starts guessing (699 investigate steps on thompson-nfa without
    ever running the code).

    Context required: mission
    Publishes: dispatch_config
    """
    mission = step_input.context.get("mission")
    warning = mission.next_pending_warning() if mission else None
    if warning is None:
        return StepOutput(
            result={"sweep_complete": True},
            observations="No pending warnings",
        )

    mission.dispatch_warning(warning.id)
    if step_input.effects:
        await step_input.effects.save_mission(mission)

    subject = warning.subject or warning.kind
    fix_line = (
        f"\n\nPrescribed fix: {warning.prescribed_fix}"
        if warning.prescribed_fix
        else ""
    )
    directive = (
        f"A deterministic check raised this, WITHOUT a behavioural test session "
        f"behind it — so it will not appear in any transcript. Diagnose the root "
        f"cause and identify what to change.\n\n"
        f"Observed by: {warning.source_flow} ({warning.kind})\n"
        f"Subject: {subject}\n\n"
        f"Evidence:\n{warning.evidence}{fix_line}"
    )
    headline = f"{warning.kind}: {subject}"[:80]

    logger.info(
        "Warning sweep: dispatching %s (attempt %d/%d)",
        headline,
        warning.attempts,
        WARNING_MAX_ATTEMPTS,
    )
    return StepOutput(
        result={"sweep_complete": False, "needs_diagnosis": True},
        observations=f"Evidenced warning → diagnose: {headline}",
        context_updates={
            "mission": mission,
            "dispatch_config": {
                # No goal binds a warning — it is not goal-shaped. The
                # warning's own lifecycle (dispatched -> abandoned) is what
                # guarantees forward progress, not a goal completion.
                "goal_id": "",
                "goal_description": headline,
                "goal_type": "warning",
                "goal_files": [],
                "flow": "diagnose_issue",
                "target_file_path": "",
                "flow_directive": directive,
                "what_happened": warning.evidence,
                "error_headline": headline,
            },
        },
    )


async def action_quality_sweep_next(step_input: StepInput) -> StepOutput:
    """Work the next incomplete ``quality`` goal: diagnose -> file_ops -> complete
    on a successful patch (quality goals have no clean interact re-test). Mirror
    of action_functional_sweep_next, goal-centric (reads goal.reports).

    Result: ``needs_fix`` (+ dispatch_config) | else -> check_phase (which routes
    to the next quality goal, or to the re-gate once all are complete).
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"sweep_complete": True}, observations="No mission")

    goal = next(
        (g for g in mission.goals if g.type == "quality" and g.status == "incomplete"),
        None,
    )
    if goal is None:
        return StepOutput(
            result={"sweep_complete": True},
            observations="Quality sweep: all quality goals complete — re-gating",
        )

    last = goal.reports[-1] if goal.reports else None
    last_flow = _rget(last, "flow", "") if last is not None else ""

    # After file_ops: the patch landed -> complete (no interact re-test for a
    # cosmetic/content fix). Re-evaluate via check_phase for the next goal.
    if last_flow == "file_ops":
        goal.status = "complete"
        if effects:
            await effects.save_mission(mission)
        return StepOutput(
            result={"sweep_complete": False},
            observations=f"Quality sweep: '{goal.description[:50]}' patched — complete",
        )

    # After diagnose: map to a file_ops fix.
    if last_flow == "diagnose_issue":
        fileops = _fileops_dispatch_from_quality_diagnosis(
            last, goal_id=goal.id, goal_description=goal.description
        )
        if fileops:
            return StepOutput(
                result={"needs_fix": True},
                observations=f"Quality sweep: patching {fileops['target_file_path']}",
                context_updates={"dispatch_config": fileops},
            )
        # Diagnose found no actionable target. Best-effort complete to avoid a
        # within-sweep spin; the next gate run re-reports it and the harvester
        # re-opens it for a fresh attempt (budget-bounded across rounds).
        goal.status = "complete"
        if effects:
            await effects.save_mission(mission)
        return StepOutput(
            result={"sweep_complete": False},
            observations=(
                f"Quality sweep: no target for '{goal.description[:50]}' — "
                f"best-effort complete (gate will re-report if unfixed)"
            ),
        )

    # Fresh goal (no diagnose yet) -> diagnose it.
    return StepOutput(
        result={"needs_fix": True},
        observations=f"Quality sweep: diagnosing '{goal.description[:50]}'",
        context_updates={
            "dispatch_config": _diag_dispatch_from_quality_finding(
                goal.description,
                goal_id=goal.id,
                repro_block=_goal_repro_block(goal),
            )
        },
    )


# ══════════════════════════════════════════════════════════════════════
# Fix Target Resolution
# ══════════════════════════════════════════════════════════════════════
#
# The menu assembly logic formerly in action_build_fix_target_menu
# moved to the `project_fix_target_menu` projection (see
# agent/projections.py). The resolve_fix_target step now reads the
# pre-composed option list via options_from.projection; no helper
# action step is required.


async def action_fallback_fix_target(step_input: StepInput) -> StepOutput:
    """Deterministic fix-target when the LLM menu can't answer.

    LIVENESS backstop for resolve_fix_target: after the (token-capped)
    menu retries exhaust, take the projection's top-ranked option
    instead of looping back into the sweep — live failure: 43 runaway
    generations cancelled at the watchdog ceiling with zero cycles of
    progress. A wrong pick costs one budgeted dispatch and the gate
    machinery self-corrects; spinning costs everything.

    Params: options — the fix_target_menu projection (list of {id, ...})
    Publishes: selected_fix_target
    """
    options = step_input.params.get("options") or []
    first = ""
    if options and isinstance(options, list):
        head = options[0]
        first = str(head.get("id", "")) if isinstance(head, dict) else str(head)

    if not first:
        return StepOutput(
            result={"has_target": False},
            observations="Fallback fix target: no menu options available",
        )
    logger.info("Fallback fix target: deterministically selected %s", first)
    return StepOutput(
        result={"has_target": True},
        observations=f"Menu unanswerable — fell back to top-ranked target {first}",
        context_updates={"selected_fix_target": first},
    )


# ── In-graph task router (classify flow) ──────────────────────────────
# The `classify` flow's two menu turns publish the picked labels to context
# (routed_flow_set / routed_profile); this action writes them onto the mission
# so the chosen controller derives the right phases, then the handoff tail-calls
# the set's entry flow. Full-local autonomy: reached when flow_set=="auto";
# explicit config skips the whole flow. Label sets mirror adapters.tb.task_judge
# (the router this generalizes) — kept inline to avoid an agent→adapters.tb dep.
_ROUTABLE_FLOW_SETS = ("ops", "code_core")
_ROUTABLE_PROFILES = (
    "service",
    "data_transform",
    "invertible",
    "repair",
    "answer",
    "plain",
)


async def action_persist_routing(step_input: StepInput) -> StepOutput:
    """Persist the classify flow's menu choices onto the mission.

    Reads context.routed_flow_set / routed_profile (the raw option keys the menu
    turns published). Writes mission.config.flow_set + task_profile, defaulting
    to (ops, plain) when a choice is missing/invalid (no_answer path) — the same
    safe default as classify_flow_set's exhaustion. When code_core, seeds
    pending_directive = objective so ingest_workspace → replan drives the
    brownfield sweep (mirrors the adapter entry logic). Best-effort audit record
    to .agent/ouroboros-routing.json.

    Context: mission (required), routed_flow_set / routed_profile (optional).
    Publishes: mission. Result carries flow_set for the handoff resolver.
    """
    mission = step_input.context.get("mission")
    effects = step_input.effects
    if not mission:
        return StepOutput(result={"flow_set": "ops"}, observations="No mission")

    picked_fs = str(step_input.context.get("routed_flow_set", "") or "")
    picked_pr = str(step_input.context.get("routed_profile", "") or "")
    flow_set = picked_fs if picked_fs in _ROUTABLE_FLOW_SETS else "ops"
    profile = picked_pr if picked_pr in _ROUTABLE_PROFILES else "plain"
    method = "llm" if picked_fs in _ROUTABLE_FLOW_SETS else "default"

    # NO deterministic repair floor: flow_set is now decided by conclude_route
    # AFTER the router investigated the workspace (localized single-file →
    # ops, diffuse/multi-file → code_core). "small-local vs multi-file" is a
    # soft attribute — an informed inference, not a blunt override. (The old
    # profile==repair→code_core floor mis-routed langcodes, which ops solves
    # 3/3 and code_core 1/3.) The hard gates (explicit flow_set / OURO_FLOW_SET,
    # held_out_tests) sit earlier and are untouched.
    findings = str(step_input.context.get("router_findings", "") or "")

    mission.config.flow_set = flow_set
    mission.config.task_profile = profile
    mission.router_findings = findings  # warm start for the routed flow's prompts
    # code_core adopts the existing workspace: ingest_workspace scans + extracts
    # the architecture, then the pending directive drives replan → the sweep.
    if flow_set == "code_core":
        mission.pending_directive = getattr(mission, "objective", "") or ""

    if effects:
        await effects.save_mission(mission)
        try:  # best-effort routing audit (parity with task_judge's log)
            rec = {
                "flow_set": flow_set,
                "profile": profile,
                "method": method,
                "objective": (getattr(mission, "objective", "") or "")[:500],
                "findings": findings[:500],
            }
            await effects.write_file(
                ".agent/ouroboros-routing.json", json.dumps(rec, indent=2)
            )
        except Exception:  # noqa: BLE001 — audit is non-critical
            pass

    logger.info("Routing: flow_set=%s profile=%s (%s)", flow_set, profile, method)
    return StepOutput(
        result={"flow_set": flow_set, "profile": profile, "method": method},
        observations=f"Routed to {flow_set} / {profile} ({method})",
        context_updates={"mission": mission},
    )


async def action_apply_fix_target(step_input: StepInput) -> StepOutput:
    """Apply the LLM-selected fix target to the dispatch_config.

    Reads the selected file from context (published by the LLM menu's
    publish_selection), updates dispatch_config with the target, and
    regresses the structural goal for that file.

    Context required: mission, dispatch_config, selected_fix_target
    """
    mission = step_input.context.get("mission")
    effects = step_input.effects
    dispatch_config = step_input.context.get("dispatch_config", {})
    selected_file = step_input.context.get("selected_fix_target", "")

    if not selected_file or not mission:
        return StepOutput(
            result={"target_applied": False},
            observations="No selected file or mission",
            context_updates={"dispatch_config": dispatch_config},
        )

    # Update dispatch_config with the selected target
    dispatch_config["target_file_path"] = selected_file
    dispatch_config["goal_files"] = [selected_file]

    # Update the flow_directive to mention the target file
    diagnosis = dispatch_config.get("diagnosis_summary", "")
    goal_desc = dispatch_config.get("goal_description", "")
    dispatch_config["flow_directive"] = (
        f"Fix the issue in {selected_file} that prevents: {goal_desc}\n"
        f"Diagnosis: {diagnosis[:500]}"
    )

    # Regress the structural goal for the selected file (with provenance)
    _reopen_structural_goal(
        mission,
        selected_file,
        dispatch_config.get("goal_id", ""),
        "fix_target_resolution",
    )

    # Re-open the startup goal too, so the startup check re-verifies after the edit.
    _regress_startup_goal(mission)

    if effects:
        await effects.save_mission(mission)

    logger.info("Fix target resolution: selected %s", selected_file)

    return StepOutput(
        result={"target_applied": True},
        observations=f"Fix target resolved: {selected_file}",
        context_updates={"dispatch_config": dispatch_config},
    )
