"""OPEN_TASKS §18 + §19 — seam-gate live→dead regression and cadence.

§18: the gate computed the dead-symbol set and consumed it only to SUPPRESS
typecheck mismatches — a perfectly written method whose last caller an edit
removed was invisible (arm13: a lint fix deleted the only initiate_combat()
call; the gate logged clean; the artifact shipped unwinnable). The fix blocks
on the live→dead TRANSITION between gate runs, which the known false-dead
confound (computed-name getattr dispatch) cannot trip: static false-dead is
stable across runs and never transitions.

§19: the gate ran only at structural-phase EXIT, so runs that never completed
the phase were never seam-checked at all (gemma-31b: 109 cycles, zero runs).
The fix gates on every fileset CONTENT CHANGE via the sweep walk, throttled by
hash so an unchanged tree never re-gates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agent.actions.mission_actions as ma
from agent.actions.batch_structural_actions import _symbol_reachability
from agent.actions.mission_actions import action_structural_sweep_next
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
)

# The arm13 shape, miniaturized. v1: movement calls initiate_combat.
# v2: a "lint fix" rewrote movement, replacing the call with two prints.
COMBAT_V1 = """\
class GameEngine:
    def initiate_combat(self, monster):
        return f"fighting {monster}"

    def handle_movement(self, room):
        if room.monster:
            return self.initiate_combat(room.monster)
        return "moved"
"""
COMBAT_V2 = """\
class GameEngine:
    def initiate_combat(self, monster):
        return f"fighting {monster}"

    def handle_movement(self, room):
        if room.monster:
            print(f"A {room.monster} blocks your path!")
            print("Type 'attack' to engage the enemy.")
        return "moved"
"""
MAIN_SRC = """\
from engine import GameEngine

def run():
    eng = GameEngine()
    return eng.handle_movement(object())
"""


def _mission(tmp_path: Path) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path), structural_mode="serial"),
        architecture=ArchitectureState(
            run_command="python main.py",
            creation_order=["engine.py", "main.py"],
            modules=[
                ModuleSpec(file="engine.py", responsibility="engine"),
                ModuleSpec(file="main.py", responsibility="entry"),
            ],
        ),
        goals=[
            GoalRecord(
                description="engine",
                type="structural",
                associated_files=["engine.py"],
                status="complete",
            ),
            GoalRecord(
                description="main",
                type="structural",
                associated_files=["main.py"],
                status="complete",
            ),
        ],
        notes=[],
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


@pytest.fixture(autouse=True)
def _fresh_memo():
    ma._SEAM_GATE_MEMO.clear()
    yield
    ma._SEAM_GATE_MEMO.clear()


class TestReachabilityDefined:
    def test_returns_full_symbol_universe(self):
        reach = _symbol_reachability({"engine.py": COMBAT_V1, "main.py": MAIN_SRC})
        assert "defined" in reach
        assert any("initiate_combat" in q for q in reach["defined"])
        assert any("handle_movement" in q for q in reach["defined"])


class TestRegression:
    @pytest.mark.asyncio
    async def test_live_to_dead_blocks_with_regression_directive(self, tmp_path):
        """The arm13 shape: run clean on v1, then the severing edit lands —
        the second gate run must BLOCK and name the orphaned symbol."""
        _touch(tmp_path, ["engine.py", "main.py"])
        mission = _mission(tmp_path)

        out1 = await action_structural_sweep_next(
            _si(mission, {"engine.py": COMBAT_V1, "main.py": MAIN_SRC})
        )
        assert out1.result.get("sweep_complete") is True  # v1 is clean

        out2 = await action_structural_sweep_next(
            _si(mission, {"engine.py": COMBAT_V2, "main.py": MAIN_SRC})
        )
        assert out2.result.get("sweep_complete") is False
        dc = (out2.context_updates or {}).get("dispatch_config")
        assert dc is not None
        assert "initiate_combat" in dc["flow_directive"]
        assert "severed" in dc["flow_directive"]
        # The severed-call directive must NOT point at the definition side.
        assert "never DEFINED" not in dc["flow_directive"]

    @pytest.mark.asyncio
    async def test_stable_dead_never_flags(self, tmp_path):
        """The arm04 confound: a symbol dead at EVERY check (computed-name
        dispatch fakes this) must never trip the regression — no transition,
        no block."""
        _touch(tmp_path, ["engine.py", "main.py"])
        mission = _mission(tmp_path)
        for _ in range(3):
            out = await action_structural_sweep_next(
                _si(mission, {"engine.py": COMBAT_V2, "main.py": MAIN_SRC})
            )
            assert out.result.get("sweep_complete") is True

    @pytest.mark.asyncio
    async def test_deliberate_removal_clears(self, tmp_path):
        """Deleting the now-dead definition too (the intentional-cut exit the
        directive offers) must clear the check, not wedge it."""
        _touch(tmp_path, ["engine.py", "main.py"])
        mission = _mission(tmp_path)
        await action_structural_sweep_next(
            _si(mission, {"engine.py": COMBAT_V1, "main.py": MAIN_SRC})
        )
        await action_structural_sweep_next(
            _si(mission, {"engine.py": COMBAT_V2, "main.py": MAIN_SRC})
        )
        removed = COMBAT_V2.replace(
            "    def initiate_combat(self, monster):\n"
            '        return f"fighting {monster}"\n\n',
            "",
        )
        assert "initiate_combat" not in removed
        out = await action_structural_sweep_next(
            _si(mission, {"engine.py": removed, "main.py": MAIN_SRC})
        )
        assert out.result.get("sweep_complete") is True

    @pytest.mark.asyncio
    async def test_unresolved_regression_reflags(self, tmp_path):
        """Baseline retention: if the dispatched fix does not land, the same
        regression must flag again on the next changed-tree run rather than
        becoming the new normal."""
        _touch(tmp_path, ["engine.py", "main.py"])
        mission = _mission(tmp_path)
        await action_structural_sweep_next(
            _si(mission, {"engine.py": COMBAT_V1, "main.py": MAIN_SRC})
        )
        out2 = await action_structural_sweep_next(
            _si(mission, {"engine.py": COMBAT_V2, "main.py": MAIN_SRC})
        )
        assert out2.result.get("sweep_complete") is False
        # An unrelated edit lands; initiate_combat is STILL severed.
        v2b = COMBAT_V2 + "\n# cosmetic comment\n"
        out3 = await action_structural_sweep_next(
            _si(mission, {"engine.py": v2b, "main.py": MAIN_SRC})
        )
        assert out3.result.get("sweep_complete") is False
        dc = (out3.context_updates or {}).get("dispatch_config")
        assert "initiate_combat" in dc["flow_directive"]


class TestCadence:
    @pytest.mark.asyncio
    async def test_unchanged_tree_gates_once(self, tmp_path, monkeypatch):
        """The hash throttle: repeated sweeps over identical content must not
        re-run the gate."""
        _touch(tmp_path, ["engine.py", "main.py"])
        mission = _mission(tmp_path)
        calls = {"n": 0}
        real = ma._phase_exit_seam_gate

        async def counting(mission_, effects_):
            calls["n"] += 1
            return await real(mission_, effects_)

        monkeypatch.setattr(ma, "_phase_exit_seam_gate", counting)
        files = {"engine.py": COMBAT_V1, "main.py": MAIN_SRC}
        for _ in range(3):
            await action_structural_sweep_next(_si(mission, files))
        # ONE gate run total: the cadence gates the new tree, records the
        # clean verdict, and both the walk and the phase-exit path skip the
        # unchanged content afterwards.
        assert calls["n"] == 1
        await action_structural_sweep_next(_si(mission, files))
        assert calls["n"] == 1
        # A real edit re-gates exactly once.
        files2 = {"engine.py": COMBAT_V1 + "\n# edit\n", "main.py": MAIN_SRC}
        await action_structural_sweep_next(_si(mission, files2))
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_single_file_inert(self, tmp_path):
        """<2 py files: cadence must stay inert (cross-module checks need a
        pair), exactly like the gate's own inert branch."""
        _touch(tmp_path, ["engine.py"])
        mission = _mission(tmp_path)
        mission.goals = mission.goals[:1]
        mission.architecture.modules = mission.architecture.modules[:1]
        mission.architecture.creation_order = ["engine.py"]
        out = await action_structural_sweep_next(_si(mission, {"engine.py": COMBAT_V1}))
        assert out.result.get("sweep_complete") is True
