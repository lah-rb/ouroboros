"""Phase-exit seam gate: cross-module checks at serial structural completion.

The transfer-shape/typecheck gates (671ee57) run only inside
build_structure, so serial-mode missions (game_challenge_boss pins
serial) never executed them — the 2026-07-23 dense-mistral artifact
reached the functional phase with three cross-module shape bugs the
gates catch statically. The gate runs the same analyses once at the
sweep's all-complete exit, dispatching a fix WITHOUT reopening goals
(reopening would ping-pong with the verify-only re-cert rung).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.actions.mission_actions import action_structural_sweep_next
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
    NoteRecord,
)

# The fair-ablation combat seam, miniaturized: producer returns only
# {"outcome", "message"}; consumer reads "monster_defeated".
COMBAT_SRC = """\
class CombatEngine:
    def run(self, player, monster):
        if player:
            return {"outcome": "win", "message": "you win"}
        return {"outcome": "loss", "message": "you lose"}
"""
ENGINE_SRC = """\
from combat import CombatEngine

def tick(player, monster):
    combat = CombatEngine()
    result = combat.run(player, monster)
    if result.get("monster_defeated"):
        return "victory"
    return result.get("message", "")
"""
ENGINE_CLEAN_SRC = """\
from combat import CombatEngine

def tick(player, monster):
    combat = CombatEngine()
    result = combat.run(player, monster)
    if result.get("outcome") == "win":
        return "victory"
    return result.get("message", "")
"""


def _mission(tmp_path: Path, notes=None) -> MissionState:
    goals = [
        GoalRecord(
            description="combat",
            type="structural",
            associated_files=["combat.py"],
            status="complete",
        ),
        GoalRecord(
            description="engine",
            type="structural",
            associated_files=["engine.py"],
            status="complete",
        ),
    ]
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path), structural_mode="serial"),
        architecture=ArchitectureState(
            run_command="python engine.py",
            creation_order=["combat.py", "engine.py"],
            modules=[
                ModuleSpec(file="combat.py", responsibility="combat"),
                ModuleSpec(file="engine.py", responsibility="loop"),
            ],
        ),
        goals=goals,
        notes=notes or [],
    )


def _si(mission, files) -> StepInput:
    return StepInput(
        context={"mission": mission},
        effects=MockEffects(mission=mission, files=files),
        meta=FlowMeta(flow_name="mission_control", step_id="structural_sweep_next"),
    )


def _touch(tmp_path: Path, names):
    for n in names:
        (tmp_path / n).write_text("# on disk\n")


@pytest.mark.asyncio
async def test_seam_gate_blocks_phase_and_dispatches_consumer_fix(tmp_path):
    _touch(tmp_path, ["combat.py", "engine.py"])
    mission = _mission(tmp_path)
    out = await action_structural_sweep_next(
        _si(mission, {"combat.py": COMBAT_SRC, "engine.py": ENGINE_SRC})
    )
    assert out.result.get("sweep_complete") is False
    dc = (out.context_updates or {}).get("dispatch_config")
    assert dc is not None and dc["target_file_path"] == "engine.py"
    assert "monster_defeated" in dc["error_output"]
    # Goals stay COMPLETE — the gate blocks the phase, not the goals
    # (reopening would ping-pong with the verify-only re-cert rung).
    assert all(g.status == "complete" for g in mission.goals)
    # Attempt is booked as a tagged note.
    assert any("seam_gate" in (n.tags or []) for n in mission.notes)


@pytest.mark.asyncio
async def test_seam_gate_clean_fileset_completes_phase(tmp_path):
    _touch(tmp_path, ["combat.py", "engine.py"])
    mission = _mission(tmp_path)
    out = await action_structural_sweep_next(
        _si(mission, {"combat.py": COMBAT_SRC, "engine.py": ENGINE_CLEAN_SRC})
    )
    assert out.result.get("sweep_complete") is True


@pytest.mark.asyncio
async def test_seam_gate_fails_open_after_attempt_bound(tmp_path):
    _touch(tmp_path, ["combat.py", "engine.py"])
    notes = [
        NoteRecord(content=f"seam gate attempt {i}", tags=["seam_gate"])
        for i in range(3)
    ]
    mission = _mission(tmp_path, notes=notes)
    out = await action_structural_sweep_next(
        _si(mission, {"combat.py": COMBAT_SRC, "engine.py": ENGINE_SRC})
    )
    # Bounded: a stubborn seam (or false positive) must not wedge the
    # mission — the phase completes and the notes carry the evidence.
    assert out.result.get("sweep_complete") is True
