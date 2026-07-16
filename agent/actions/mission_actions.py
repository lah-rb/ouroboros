"""Mission actions — load state, dispatch tasks, manage lifecycle.

Rebuild v2: Replaces heuristic task matching with LLM menu selection,
adds structured architecture state, removes silent fallbacks.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from typing import Any

from agent.models import StepInput, StepOutput
from agent.actions.reporting_actions import structural_block_reason

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
        infrastructure = {
            "pyproject.toml",
            "setup.cfg",
            "setup.py",
            "requirements.txt",
            "uv.lock",
            "README.md",
            "readme.md",
            "CHANGELOG.md",
            ".gitignore",
            ".editorconfig",
            ".flake8",
            ".pre-commit-config.yaml",
            "Makefile",
            "Dockerfile",
            "docker-compose.yml",
        }
        infrastructure_prefixes = (".", "tests/", "test_", "__pycache__/")
        infrastructure_suffixes = ("__init__.py",)

        disk_files = set()
        for filepath in manifest.keys():
            basename = os.path.basename(filepath)
            if basename in infrastructure:
                continue
            if any(filepath.startswith(p) for p in infrastructure_prefixes):
                continue
            if any(filepath.endswith(s) for s in infrastructure_suffixes):
                continue
            disk_files.add(filepath)

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
    try:
        execution = data.get("execution", {})
        if not isinstance(execution, dict):
            execution = {}
        modules = []
        for m in data.get("modules", []):
            modules.append(
                ModuleSpec(
                    file=m.get("file", ""),
                    responsibility=m.get("responsibility", ""),
                    defines=m.get("defines", []),
                    imports_from=m.get("imports_from", {}),
                )
            )

        interfaces = []
        for iface in data.get("interfaces", []):
            interfaces.append(
                InterfaceContract(
                    caller=iface.get("caller", ""),
                    callee=iface.get("callee", ""),
                    symbol=iface.get("symbol", ""),
                    signature=iface.get("signature", ""),
                )
            )

        data_shapes = []
        for ds in data.get("data_shapes", []):
            data_shapes.append(DataShapeContract.from_llm_dict(ds))

        state_shapes = []
        for ss in data.get("state_shapes", []):
            state_shapes.append(StateShapeContract.from_llm_dict(ss))

        transient_files = [
            str(t).strip() for t in data.get("transient_files", []) if str(t).strip()
        ]

        arch = ArchitectureState(
            import_scheme=execution.get("import_scheme", "flat"),
            run_command=execution.get("run_command", ""),
            smoke_command=execution.get("smoke_command", ""),
            working_directory=execution.get("working_directory", "project root"),
            init_files=execution.get("init_files", False),
            modules=modules,
            creation_order=data.get("creation_order", [m.file for m in modules]),
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
    """Mark mission complete, deadlocked, or aborted and save."""
    effects = step_input.effects
    mission = step_input.context.get("mission")

    if not mission:
        return StepOutput(
            result={"finalized": False},
            observations="No mission to finalize",
        )

    if step_input.params.get("deadlock", False):
        mission.status = "deadlocked"
    elif step_input.params.get("abort", False):
        mission.status = "aborted"
    else:
        mission.status = "completed"

    # Terminal archive sweep: the per-cycle sweep (attach_directive_report)
    # never runs AFTER the final goal completes — this catches the last
    # goal's records before the run ends. Relocation, never deletion.
    if effects:
        try:
            from agent.persistence.archive import archive_mission_overflow

            pm = effects._get_persistence()
            archive_mission_overflow(pm.agent_dir, mission)
        except AttributeError:
            pass  # effects without a persistence dir
        except Exception:
            logger.exception("terminal archive sweep failed")

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
    if hasattr(mission, "objective"):
        objective = mission.objective
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
            f"Produce 4-7 functional goals as a JSON array of strings.\n\n"
            f"\u2705 CORRECT — for a calculator app:\n"
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
    # correction, not a bypass — see dev/SWE_PILOT_1_FINDINGS.md.)
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


def _get_sweep_files(arch: Any) -> list[str]:
    """Build the ordered list of files: creation_order ∪ modules ∪ data_shapes.

    Uses creation_order for sequencing, but unions with all module files
    to catch any that were listed in modules but omitted from the order
    (e.g., __init__.py).  Data shape files are appended last.
    """
    ordered = (
        list(arch.creation_order)
        if hasattr(arch, "creation_order") and arch.creation_order
        else [m.file for m in arch.modules]
        if hasattr(arch, "modules")
        else []
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
        return StepOutput(
            result={"sweep_complete": True},
            observations="No architecture — skip sweep",
        )

    sweep_files = _get_sweep_files(arch)
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

    # ── Parallel mode: one-shot batch creation ────────────────────
    # Dispatch build_structure exactly once, on a virgin structural
    # phase: no goal has any report, no sweep file exists, and no prior
    # batch attempt is on record (the batch summary note doubles as the
    # attempted-flag — an empty generation books no reports, and without
    # the flag the sweep would re-dispatch the batch forever). After the
    # batch, this sweep resumes per-file: missing files → serial create,
    # gate-failed files → diagnose-first repair below.
    if mode == "parallel":
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
        if mode == "parallel" and goal.reports and block_reason != "import":
            last = goal.reports[-1]
            last_flow = getattr(last, "flow", "")
            if last_flow == "diagnose_issue":
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
                gate_output = str(rep.terminal_output)[:800]
                break

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

    # All structural goals are complete
    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={"sweep_complete": True},
        observations="Structural sweep complete — all files created and validated",
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
            held_out = getattr(
                getattr(mission, "config", None), "held_out_tests", False
            )
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
                            {"command": rt["command"], "name": "repair suite",
                             "required": True}
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
                logger.info(
                    "Functional sweep: exploring to build %s", goal.description[:50]
                )
                return StepOutput(
                    result={"sweep_complete": False, "needs_test": True},
                    observations=(
                        f"Functional sweep: exploring to build "
                        f"'{goal.description[:50]}'"
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
            if is_repair_profile(mission) and not getattr(
                goal, "capability_absent", False
            ):
                directive = (
                    "This is a confirmed defect reported against existing code "
                    "(a hidden test pins it). Diagnose the root cause and name "
                    "the specific existing file and symbol to change — make the "
                    "SMALLEST edit that fixes the reported behavior:\n"
                    + goal.description
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
                "error_headline": getattr(last_report, "headline", "")
                or goal.description[:80],
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

        # interact success means goal_met was true (the flow routes on this)
        if report_flow == "interact" and report_status == "success":
            goal.status = "complete"
            # failed_attempts survive completion — the archive sweep
            # relocates them (retry patterns are mining material).
            logger.info("Functional sweep: '%s' completed", goal.description[:50])
            if effects:
                await effects.save_mission(mission)
            made_progress = True
            continue

        # Last report was file_ops — check if it succeeded or failed
        if report_flow == "file_ops":
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

            # Find the diagnosis that led to this attempt, and the
            # interact that triggered that diagnosis — we capture its
            # headline as "pre_headline" so the next diagnose cycle
            # can render before/after regression comparisons.
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
                        "flow_directive": _functional_retest_directive(
                            goal, after="fix"
                        ),
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
                logger.info(
                    "Functional sweep: re-testing %s after fix", goal.description[:50]
                )
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

        # Last report was project_ops — env/dep fix applied, re-test
        if report_flow == "project_ops":
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

        # Last report was diagnose_issue — extract fix target and dispatch
        if report_flow == "diagnose_issue":
            diag_summary = getattr(last_report, "summary", "")
            diag_files = getattr(last_report, "files_affected", [])
            recommended_flow = (
                getattr(last_report, "recommended_flow", "") or "file_ops"
            )

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
            struct_related_symbols = list(
                getattr(last_report, "related_symbols", []) or []
            )

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
                    change_spec_lower.startswith(marker)
                    for marker in _evasion_spec_markers
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
                # Diagnosis explicitly named a file — use it directly
                for sg in mission.goals:
                    if sg.type == "structural" and fix_target in (
                        sg.associated_files or []
                    ):
                        if sg.status == "complete":
                            sg.status = "incomplete"
                            logger.info(
                                "Functional sweep: regressed structural goal for %s",
                                fix_target,
                            )
                        break

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
                logger.info(
                    "Functional sweep: applying fix to %s from diagnosis", fix_target
                )
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
                ["/bin/sh", "-c",
                 "python -m pytest --collect-only -q " + " ".join(test_files[:3])],
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
    "service", "data_transform", "invertible", "repair", "answer", "plain",
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
                "flow_set": flow_set, "profile": profile, "method": method,
                "objective": (getattr(mission, "objective", "") or "")[:500],
                "findings": findings[:500],
            }
            await effects.write_file(
                ".agent/ouroboros-routing.json", json.dumps(rec, indent=2)
            )
        except Exception:  # noqa: BLE001 — audit is non-critical
            pass

    logger.info(
        "Routing: flow_set=%s profile=%s (%s)", flow_set, profile, method
    )
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

    # Regress the structural goal for the selected file
    for sg in mission.goals:
        if sg.type == "structural" and selected_file in (sg.associated_files or []):
            if sg.status == "complete":
                sg.status = "incomplete"
                logger.info(
                    "Fix target resolution: regressed structural goal for %s",
                    selected_file,
                )
            break

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
