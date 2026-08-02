"""Epoch v2.0 W2 — league run protocol + park-at-resumable-point.

The old guard (`target != entry`) let one more mission_control pass run after
budget exhaustion — a pass that DECIDES: it re-certifies, commits a
dispatch_config, and parks with the repair pending (devstral shipped a broken
engine exactly this way). The inverted guard parks at the work→entry boundary
with the finished flow's tail-call inputs persisted for replay.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

import agent.tier.runner as tr
from agent.effects.mock import MockEffects
from agent.loop import run_agent
from agent.persistence.models import (
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
    ArchitectureState,
)


def _mission(tmp_path):
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path), structural_mode="serial"),
        architecture=ArchitectureState(
            run_command="python m.py",
            creation_order=["m.py"],
            modules=[ModuleSpec(file="m.py", responsibility="all")],
        ),
        goals=[
            GoalRecord(
                description="m",
                type="structural",
                associated_files=["m.py"],
                status="incomplete",
            )
        ],
    )


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(mission, fx, max_cycles=1, entry_inputs=None):
    with pytest.raises(RuntimeError, match="parked"):
        asyncio.run(
            run_agent(
                mission_id=mission.id,
                effects=fx,
                flows_dir=os.path.join(ROOT, "flows"),
                prompts_dir=os.path.join(ROOT, "prompts"),
                entry_flow="mission_control",
                entry_inputs=entry_inputs,
                max_cycles=max_cycles,
            )
        )
    return fx._state["mission"]


class TestParkBoundary:
    def test_park_persists_pending_return_and_books_cycles(self, tmp_path):
        mission = _mission(tmp_path)
        fx = MockEffects(
            mission=mission,
            inference_responses=["```python\n# === FILE: m.py ===\nx = 1\n```"] * 6,
        )
        saved = _run(mission, fx, max_cycles=1)
        assert saved.status == "paused"
        assert saved.cycles_consumed == 1
        # The work flow's tail-call inputs survived the park for replay.
        assert isinstance(saved.pending_return, dict)
        assert saved.pending_return  # non-empty: mission_id at minimum

    def test_cycles_accumulate_across_parks(self, tmp_path):
        mission = _mission(tmp_path)
        fx = MockEffects(
            mission=mission,
            inference_responses=["```python\n# === FILE: m.py ===\nx = 1\n```"] * 6,
        )
        saved = _run(mission, fx, max_cycles=1)
        assert saved.cycles_consumed == 1
        saved.status = "active"
        fx2 = MockEffects(
            mission=saved,
            inference_responses=["```python\n# === FILE: m.py ===\nx = 2\n```"] * 6,
        )
        saved2 = _run(saved, fx2, max_cycles=1, entry_inputs=dict(saved.pending_return))
        assert saved2.cycles_consumed == 2

    def test_old_mission_json_without_new_fields_loads(self):
        m = MissionState.model_validate(
            {
                "objective": "x",
                "status": "active",
                "config": {"working_directory": "/tmp/w"},
            }
        )
        assert m.pending_return == {} and m.cycles_consumed == 0


class TestLeagueResolution:
    def _fake_configs(self, tmp_path, name, tier_block):
        d = tmp_path / "llmvp" / "configs"
        d.mkdir(parents=True, exist_ok=True)
        body = "model:\n  name: x\n"
        if tier_block is not None:
            body += "tier:\n" + tier_block
        (d / f"{name}.yaml").write_text(body)
        return tmp_path

    def test_league_read(self, tmp_path, monkeypatch):
        root = self._fake_configs(tmp_path, "slow", "  league: contemplator\n")
        self._fake_configs(tmp_path, "fast", "  league: grinder\n")
        self._fake_configs(tmp_path, "dual", "  league: both\n")
        self._fake_configs(tmp_path, "plain", "  status: not_yet_run\n")
        monkeypatch.setattr(tr, "ROOT", root)
        assert tr.config_league("slow") == "contemplator"
        assert tr.config_league("fast") == "grinder"
        assert tr.config_league("dual") == "both"
        assert tr.config_league("plain") == "grinder"  # absent key -> default
        assert tr.config_league("missing") == "grinder"  # no file -> default

    def test_league_malformed_yaml_defaults_grinder(self, tmp_path, monkeypatch):
        d = tmp_path / "llmvp" / "configs"
        d.mkdir(parents=True)
        (d / "broken.yaml").write_text("tier: [unclosed\n  league: what")
        monkeypatch.setattr(tr, "ROOT", tmp_path)
        assert tr.config_league("broken") == "grinder"

    def test_arm_labels_beat_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tr, "ROOT", tmp_path)  # no configs at all
        assert tr.arm_league("hy3[c]") == ("hy3", "contemplator")
        assert tr.arm_league("hy3[g]") == ("hy3", "grinder")
        assert tr.arm_league("bare") == ("bare", "grinder")

    def test_both_without_label_runs_grinder(self, tmp_path, monkeypatch):
        root = self._fake_configs(tmp_path, "dual", "  league: both\n")
        monkeypatch.setattr(tr, "ROOT", root)
        # a bare `dual` arm (expansion bypassed) must still get ONE league
        assert tr.arm_league("dual") == ("dual", "grinder")


class TestCyclesCarry:
    def test_reads_mission_json(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        (agent_dir / "mission.json").write_text(json.dumps({"cycles_consumed": 12}))
        assert tr.cycles_consumed(tmp_path) == 12

    def test_missing_or_corrupt_is_zero(self, tmp_path):
        assert tr.cycles_consumed(tmp_path) == 0
        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        (agent_dir / "mission.json").write_text("{not json")
        assert tr.cycles_consumed(tmp_path) == 0

    def test_remaining_wall_backstop_param(self):
        run = tr.TierRun(arms=["x"], wall="2h")
        # grinder: against the stage wall
        assert run._remaining_wall_s(3600) == 3600.0
        # contemplator: against the 4h safety wall
        assert run._remaining_wall_s(3600, tr.CONTEMPLATOR_SAFETY_WALL) == 3 * 3600.0
        # floor
        assert run._remaining_wall_s(10**6, "2h") == 60.0
