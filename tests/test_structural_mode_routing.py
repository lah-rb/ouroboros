"""structural_sweep_next mode branching: batch dispatch and diagnose-first repair.

Parallel mode dispatches build_structure exactly once on a virgin
structural phase, then repairs gate-failed files diagnose-first (the
quality sweep's two-step: diagnose_issue → file_ops with the diagnosis's
structured fields). Serial mode is the regression pin: behavior
identical to before the mode existed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.actions.mission_actions import action_structural_sweep_next
from agent.effects.mock import MockEffects
from tests.conftest import REPO_ROOT, compiled_flows
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    DirectiveReport,
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
    NoteRecord,
)


def _mission(tmp_path: Path, mode: str, goals=None, notes=None) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path), structural_mode=mode),
        architecture=ArchitectureState(
            run_command="python main.py",
            creation_order=["models.py", "main.py"],
            modules=[
                ModuleSpec(file="models.py", responsibility="data"),
                ModuleSpec(file="main.py", responsibility="entry"),
            ],
        ),
        goals=(
            goals
            if goals is not None
            else [
                GoalRecord(
                    description="models",
                    type="structural",
                    associated_files=["models.py"],
                ),
                GoalRecord(
                    description="entry", type="structural", associated_files=["main.py"]
                ),
            ]
        ),
        notes=notes or [],
    )


def _si(mission, effects=None) -> StepInput:
    return StepInput(
        context={"mission": mission},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="structural_sweep_next"),
        effects=effects or MockEffects(mission=mission),
    )


# ── Batch dispatch predicate ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_virgin_phase_dispatches_batch(tmp_path):
    mission = _mission(tmp_path, "batch")
    out = await action_structural_sweep_next(_si(mission))
    assert out.result.get("needs_batch_create") is True
    cfg = out.context_updates["dispatch_config"]
    assert cfg["flow"] == "build_structure"
    assert cfg["goal_id"] == ""
    assert cfg["goal_files"] == ["models.py", "main.py"]


@pytest.mark.asyncio
async def test_serial_virgin_phase_creates_first_file(tmp_path):
    mission = _mission(tmp_path, "serial")
    out = await action_structural_sweep_next(_si(mission))
    assert out.result.get("needs_create") is True
    assert "needs_batch_create" not in out.result
    cfg = out.context_updates["dispatch_config"]
    assert cfg["flow"] == "file_ops"
    assert cfg["target_file_path"] == "models.py"


@pytest.mark.asyncio
async def test_parallel_no_rebatch_after_attempt_note(tmp_path):
    # An empty generation books no reports; the batch summary note is
    # the attempted-flag that prevents an endless re-dispatch loop.
    mission = _mission(
        tmp_path,
        "batch",
        notes=[
            NoteRecord(
                content="batch attempt",
                category="codebase_observation",
                tags=["batch_structural"],
                source_flow="build_structure",
            )
        ],
    )
    out = await action_structural_sweep_next(_si(mission))
    assert out.result.get("needs_create") is True  # serial fallback
    assert out.context_updates["dispatch_config"]["flow"] == "file_ops"


@pytest.mark.asyncio
async def test_parallel_no_rebatch_when_files_exist(tmp_path):
    (tmp_path / "models.py").write_text("class A: pass\n")
    mission = _mission(tmp_path, "batch")
    out = await action_structural_sweep_next(_si(mission))
    assert "needs_batch_create" not in out.result


# ── Diagnose-first repair ─────────────────────────────────────────────


def _failed_report(file_path: str) -> DirectiveReport:
    return DirectiveReport(
        flow="build_structure",
        status="failed",
        summary=f"gate failures in {file_path}",
        files_affected=[file_path],
        checks_failed=[f"syntax: {file_path}"],
        terminal_output=f"[FAIL] syntax: {file_path}\n  stderr: SyntaxError",
    )


@pytest.mark.asyncio
async def test_parallel_gate_failure_dispatches_diagnose(tmp_path):
    (tmp_path / "models.py").write_text("def broken(:\n")
    (tmp_path / "main.py").write_text("print('ok')\n")
    goals = [
        GoalRecord(
            description="models",
            type="structural",
            associated_files=["models.py"],
            reports=[_failed_report("models.py")],
        ),
        GoalRecord(
            description="entry",
            type="structural",
            associated_files=["main.py"],
            status="complete",
        ),
    ]
    mission = _mission(tmp_path, "batch", goals=goals)
    out = await action_structural_sweep_next(_si(mission))
    assert out.result.get("needs_fix") is True
    cfg = out.context_updates["dispatch_config"]
    assert cfg["flow"] == "diagnose_issue"
    assert cfg["target_file_path"] == "models.py"
    assert cfg["goal_id"] == goals[0].id
    assert "syntax: models.py" in cfg["flow_directive"]
    assert "SyntaxError" in cfg["error_output"]


@pytest.mark.asyncio
async def test_parallel_diagnosed_goal_dispatches_fileops_patch(tmp_path):
    (tmp_path / "models.py").write_text("def broken(:\n")
    diagnosis = DirectiveReport(
        flow="diagnose_issue",
        status="diagnosed",
        summary="broken() has an invalid parameter list",
        target_file="models.py",
        target_symbol="broken",
        change_spec="Fix the parameter list of broken().",
        diagnosis_kind="fix",
    )
    goals = [
        GoalRecord(
            description="models",
            type="structural",
            associated_files=["models.py"],
            reports=[_failed_report("models.py"), diagnosis],
        ),
    ]
    mission = _mission(tmp_path, "batch", goals=goals)
    out = await action_structural_sweep_next(_si(mission))
    assert out.result.get("needs_fix") is True
    cfg = out.context_updates["dispatch_config"]
    assert cfg["flow"] == "file_ops"
    assert cfg["goal_type"] == "structural"
    assert cfg["target_file_path"] == "models.py"
    assert cfg["target_symbol"] == "broken"
    assert cfg["change_spec"] == "Fix the parameter list of broken()."


@pytest.mark.asyncio
async def test_serial_gate_failure_keeps_fileops_fix(tmp_path):
    # Regression pin: serial mode never routes to diagnose from the sweep.
    (tmp_path / "models.py").write_text("def broken(:\n")
    goals = [
        GoalRecord(
            description="models",
            type="structural",
            associated_files=["models.py"],
            reports=[
                DirectiveReport(
                    flow="file_ops",
                    status="failed",
                    summary="x",
                    checks_failed=["syntax: models.py"],
                )
            ],
        ),
    ]
    mission = _mission(tmp_path, "serial", goals=goals)
    out = await action_structural_sweep_next(_si(mission))
    assert out.result.get("needs_fix") is True
    cfg = out.context_updates["dispatch_config"]
    assert cfg["flow"] == "file_ops"
    assert "Fix validation issues" in cfg["flow_directive"]


@pytest.mark.asyncio
async def test_parallel_import_block_stays_on_fileops_review(tmp_path):
    # The import fix-or-defer DECISION pass is shared with serial — it's
    # a judgment call, not a defect investigation.
    (tmp_path / "models.py").write_text("import nothere\n")
    goals = [
        GoalRecord(
            description="models",
            type="structural",
            associated_files=["models.py"],
            reports=[
                DirectiveReport(
                    flow="build_structure",
                    status="success",
                    summary="created",
                    checks_failed=["import: models.py"],
                )
            ],
        ),
    ]
    mission = _mission(tmp_path, "batch", goals=goals)
    out = await action_structural_sweep_next(_si(mission))
    cfg = out.context_updates["dispatch_config"]
    assert cfg["flow"] == "file_ops"
    assert "FAILS TO IMPORT" in cfg["flow_directive"]


@pytest.mark.asyncio
async def test_batch_success_report_auto_completes(tmp_path):
    # A passing build_structure report auto-completes at the sweep, same
    # as a file_ops success (covers the import-reviewed-then-accepted path).
    (tmp_path / "models.py").write_text("class A: pass\n")
    (tmp_path / "main.py").write_text("print('ok')\n")
    goals = [
        GoalRecord(
            description="models",
            type="structural",
            associated_files=["models.py"],
            reports=[
                DirectiveReport(flow="build_structure", status="success", summary="ok")
            ],
        ),
        GoalRecord(
            description="entry",
            type="structural",
            associated_files=["main.py"],
            status="complete",
        ),
    ]
    mission = _mission(tmp_path, "batch", goals=goals)
    out = await action_structural_sweep_next(_si(mission))
    assert out.result.get("sweep_complete") is True
    assert goals[0].status == "complete"


# ── Repair-economics instrumentation ──────────────────────────────────


@pytest.mark.asyncio
async def test_diagnose_dispatch_writes_repair_econ_note(tmp_path):
    (tmp_path / "models.py").write_text("def broken(:\n")
    (tmp_path / "main.py").write_text("print('ok')\n")
    goals = [
        GoalRecord(
            description="models",
            type="structural",
            associated_files=["models.py"],
            reports=[_failed_report("models.py")],
        ),
        GoalRecord(
            description="entry",
            type="structural",
            associated_files=["main.py"],
            status="complete",
        ),
    ]
    mission = _mission(tmp_path, "batch", goals=goals)
    fx = MockEffects(mission=mission)
    await action_structural_sweep_next(_si(mission, fx))
    note = next(n for n in mission.notes if "repair_econ" in n.tags)
    assert "stage=diagnose" in note.content
    assert "file=models.py" in note.content
    assert "class=syntax" in note.content
    size = os.path.getsize(tmp_path / "models.py")
    assert f"size_bytes={size}" in note.content
    assert fx.call_count("save_mission") >= 1  # persisted, not just in-context


@pytest.mark.asyncio
async def test_patch_dispatch_writes_repair_econ_note(tmp_path):
    (tmp_path / "models.py").write_text("def broken(:\n")
    diagnosis = DirectiveReport(
        flow="diagnose_issue",
        status="diagnosed",
        summary="bad params",
        target_file="models.py",
        target_symbol="broken",
        change_spec="fix it",
    )
    goals = [
        GoalRecord(
            description="models",
            type="structural",
            associated_files=["models.py"],
            reports=[_failed_report("models.py"), diagnosis],
        ),
    ]
    mission = _mission(tmp_path, "batch", goals=goals)
    await action_structural_sweep_next(_si(mission))
    note = next(n for n in mission.notes if "repair_econ" in n.tags)
    assert "stage=patch" in note.content


def test_repair_econ_note_parses_in_join_script(tmp_path):
    import sys

    # REPO_ROOT-anchored: a bare "dev" is cwd-relative and this test failed
    # from any directory but the repo root (same class as the compiled.json
    # loaders this file used to carry).
    dev_dir = str(REPO_ROOT / "dev")
    sys.path.insert(0, dev_dir)
    try:
        from repair_econ import _NOTE_RE

        m = _NOTE_RE.search(
            "repair_econ stage=diagnose file=engine.py class=syntax size_bytes=812"
        )
        assert m and m.group("cls") == "syntax" and m.group("file") == "engine.py"
    finally:
        sys.path.remove(dev_dir)


# ── Compiled wiring ───────────────────────────────────────────────────


def test_compiled_mission_control_routes_batch_create():
    compiled = compiled_flows()
    mc = compiled["mission_control"]["steps"]
    rules = mc["structural_sweep_next"]["resolver"]["rules"]
    assert any(
        r["condition"] == "result.needs_batch_create == true"
        and r["transition"] == "dispatch_batch_create"
        for r in rules
    )
    assert mc["dispatch_batch_create"]["tail_call"]["flow"] == "build_structure"
    # Dynamic repair target + diagnosis field pass-through.
    fix = mc["dispatch_structural_fix"]["tail_call"]
    assert fix["flow"] == {"$ref": "context.dispatch_config.flow"}
    for key in ("error_output", "target_symbol", "change_spec", "what_happened"):
        assert key in fix["input_map"]


def test_compiled_build_structure_wiring():
    compiled = compiled_flows()
    bs = compiled["build_structure"]
    assert bs["entry"] == "load_state"
    steps = bs["steps"]
    gen = steps["generate_all_files"]
    assert gen["turn"]["response_shape"] == "code"
    assert gen["turn"]["response"]["language"] == ""
    # The slicer needs these context keys declared or its $refs go blank.
    assert "inference_response" in steps["slice_and_write"]["context"]["required"]
    assert "mission" in steps["slice_and_write"]["context"]["required"]
    assert "inference_truncated" in steps["slice_and_write"]["context"]["optional"]


@pytest.mark.asyncio
async def test_serial_fix_dispatch_threads_gate_output(tmp_path):
    # The fix dispatch must carry the actual gate output (traceback /
    # file:line finding), scanned back past later edit-failure reports that
    # have no terminal_output — without it the fix flow's "Validation errors
    # to fix" prompt section is empty (2026-07-16 bossgame rewrite loop).
    (tmp_path / "models.py").write_text("def broken(:\n")
    goals = [
        GoalRecord(
            description="models",
            type="structural",
            associated_files=["models.py"],
            reports=[
                DirectiveReport(
                    flow="build_structure",
                    status="failed",
                    summary="gate failures",
                    checks_failed=["import: models.py"],
                    terminal_output=(
                        "[FAIL] import: models.py\nImportError: cannot import "
                        "name 'ITEMS' from partially initialized module "
                        "'engine' (circular import)"
                    ),
                ),
                DirectiveReport(
                    flow="file_ops",
                    status="failed",
                    summary="rewrite failed: KV cell pool exhausted",
                    checks_failed=["syntax: models.py"],
                ),
            ],
        ),
    ]
    mission = _mission(tmp_path, "serial", goals=goals)
    out = await action_structural_sweep_next(_si(mission))
    assert out.result.get("needs_fix") is True
    cfg = out.context_updates["dispatch_config"]
    assert cfg["flow"] == "file_ops"
    assert "circular import" in cfg["error_output"]
