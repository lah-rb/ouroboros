"""Two 2026-07-17 bossgame-postmortem behavior pins.

Create loophole: a file the agent writes with no goal association was
invisible to every gate (the run left a stub-content
`inventory.py:equip_item` on disk that nothing ever checked). Now any
reported-affected file that no goal covers gets a generic structural
goal (origin="create_backfill").

Sibling context: fix dispatches for a goal on a shared file carry the
OTHER goals bound to that file in the flow_directive — open siblings as
one constraint set, completed siblings as do-not-regress. Without this
the baseline arm oscillated 17↔19 goals for eight hours patching
world.yaml exits one defect at a time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.actions.mission_actions import action_structural_sweep_next
from agent.actions.reporting_actions import _backfill_untracked_file_goals
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    DirectiveReport,
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
)


def _mission(tmp_path: Path, goals) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path), structural_mode="serial"),
        architecture=ArchitectureState(
            run_command="python main.py",
            creation_order=["world.yaml"],
            modules=[ModuleSpec(file="world.yaml", responsibility="world data")],
        ),
        goals=goals,
    )


def _report(files, status="success"):
    return DirectiveReport(
        flow="file_ops", status=status, summary="s", files_affected=files
    )


# ── Create-loophole backfill ─────────────────────────────────────────


def test_uncovered_file_gets_backfill_goal(tmp_path):
    mission = _mission(
        tmp_path,
        [GoalRecord(description="g", type="structural", associated_files=["a.py"])],
    )
    added = _backfill_untracked_file_goals(mission, _report(["a.py", "helper.py"]))
    assert added == 1
    new = mission.goals[-1]
    assert new.associated_files == ["helper.py"]
    assert new.origin == "create_backfill"
    assert new.type == "structural"
    assert new.status == "incomplete"


def test_backfill_only_for_file_ops_reports(tmp_path):
    """Artifact-producing flows (PDF downloads, runtime saves) must not
    accrete structural goals — the scraper e2e caught this on day one."""
    mission = _mission(
        tmp_path,
        [GoalRecord(description="g", type="structural", associated_files=["a.py"])],
    )
    pdf_report = DirectiveReport(
        flow="acquire_catalog",
        status="success",
        summary="s",
        files_affected=["pdfs/paper.pdf"],
    )
    assert _backfill_untracked_file_goals(mission, pdf_report) == 0
    assert len(mission.goals) == 1


def test_backfill_is_idempotent_and_skips_covered(tmp_path):
    mission = _mission(
        tmp_path,
        [GoalRecord(description="g", type="structural", associated_files=["a.py"])],
    )
    assert _backfill_untracked_file_goals(mission, _report(["helper.py"])) == 1
    # Second report on the same file: covered now (and signature-deduped).
    assert _backfill_untracked_file_goals(mission, _report(["helper.py"])) == 0
    assert _backfill_untracked_file_goals(mission, _report(["a.py"])) == 0
    assert _backfill_untracked_file_goals(mission, _report([])) == 0
    assert len(mission.goals) == 2


def test_backfill_covers_corrupted_symbol_paths(tmp_path):
    """The exact artifact from the incident: a `file:symbol` path written
    by a stub — it must get a goal so gates force cleanup."""
    mission = _mission(
        tmp_path,
        [
            GoalRecord(
                description="g", type="structural", associated_files=["inventory.py"]
            )
        ],
    )
    assert (
        _backfill_untracked_file_goals(mission, _report(["inventory.py:equip_item"]))
        == 1
    )
    assert mission.goals[-1].associated_files == ["inventory.py:equip_item"]


def test_backfill_skips_infrastructure_files(tmp_path):
    """A package `__init__.py` (and other infra) is not smuggled code and
    is not in the architecture — a backfill goal for it can never be
    selected by the structural sweep, so it stalls the run in a
    no-dispatch loop (2026-07-17 swarm round 3 died at structural 9/10 on
    a `src/__init__.py` backfill goal). It must be skipped, matching the
    drift detector's infrastructure policy."""
    mission = _mission(
        tmp_path,
        [GoalRecord(description="g", type="structural", associated_files=["main.py"])],
    )
    report = _report(["src/__init__.py", "__init__.py", "pyproject.toml", "loader.py"])
    assert _backfill_untracked_file_goals(mission, report) == 1
    # Only the real application module got a goal; the three infra files
    # were skipped.
    assert mission.goals[-1].associated_files == ["loader.py"]
    assert all(g.associated_files != ["src/__init__.py"] for g in mission.goals)


# ── Sibling-goal context in fix dispatches ───────────────────────────


def _si(mission) -> StepInput:
    return StepInput(
        context={"mission": mission},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="structural_sweep_next"),
        effects=MockEffects(mission=mission),
    )


def _failed_report():
    return DirectiveReport(
        flow="file_ops",
        status="failed",
        summary="exit key mismatch",
        checks_failed=["data: world.yaml"],
        terminal_output="rooms[1].exits: declared key 'north' is absent",
    )


@pytest.mark.asyncio
async def test_fix_dispatch_carries_sibling_goals(tmp_path):
    (tmp_path / "world.yaml").write_text("rooms: []\n")
    target = GoalRecord(
        description="rooms[1].exits: declared key 'north' is absent",
        type="structural",
        associated_files=["world.yaml"],
        reports=[_failed_report()],
    )
    open_sib = GoalRecord(
        description="rooms[2].exits: key 'west' is not in the declared example",
        type="structural",
        associated_files=["world.yaml"],
    )
    done_sib = GoalRecord(
        description="Create world.yaml with content: the Forgotten Keep",
        type="structural",
        associated_files=["world.yaml"],
        status="complete",
    )
    unrelated = GoalRecord(
        description="entry point", type="structural", associated_files=["main.py"]
    )
    mission = _mission(tmp_path, [target, open_sib, done_sib, unrelated])

    out = await action_structural_sweep_next(_si(mission))
    cfg = out.context_updates["dispatch_config"]
    directive = cfg["flow_directive"]
    assert "rooms[2].exits" in directive  # open sibling present
    assert "one constraint set" in directive
    assert "Forgotten Keep" in directive  # completed sibling present
    assert "do NOT regress" in directive
    assert "entry point" not in directive  # unrelated file excluded


@pytest.mark.asyncio
async def test_fix_dispatch_without_siblings_has_no_block(tmp_path):
    (tmp_path / "world.yaml").write_text("rooms: []\n")
    target = GoalRecord(
        description="fix exits",
        type="structural",
        associated_files=["world.yaml"],
        reports=[_failed_report()],
    )
    mission = _mission(tmp_path, [target])

    out = await action_structural_sweep_next(_si(mission))
    directive = out.context_updates["dispatch_config"]["flow_directive"]
    assert "one constraint set" not in directive
    assert "do NOT regress" not in directive


# ── Orphan create_backfill goal handling in the structural sweep ─────
# A backfill goal's file is NOT in the architecture, so the sweep's
# architecture-derived walk could never select it — check_phase counted
# it incomplete, the sweep returned "complete", and the controller spun
# to the 51x-no-dispatch guard (2026-07-18: crashed the boss baseline on
# a `src/command.py` orphan). The sweep now walks orphan goal files too.


@pytest.mark.asyncio
async def test_sweep_drives_orphan_backfill_goal_that_exists(tmp_path):
    (tmp_path / "extra.py").write_text("x = 1\n")  # unplanned file on disk
    arch_done = GoalRecord(
        description="world",
        type="structural",
        associated_files=["world.yaml"],
        status="complete",
    )
    orphan = GoalRecord(
        description="extra.py was written without a planned goal — review it.",
        type="structural",
        associated_files=["extra.py"],
        origin="create_backfill",
    )
    mission = _mission(tmp_path, [arch_done, orphan])
    out = await action_structural_sweep_next(_si(mission))
    # SELECTED + dispatched (not sweep_complete=True — the orphan-loop bug).
    assert out.result.get("sweep_complete") is not True
    assert out.context_updates["dispatch_config"]["target_file_path"] == "extra.py"


@pytest.mark.asyncio
async def test_sweep_completes_orphan_backfill_goal_when_file_removed(tmp_path):
    # The backfill's "remove it if it should not exist" outcome: file gone
    # → complete the goal; never recreate (no create→remove→create loop).
    arch_done = GoalRecord(
        description="world",
        type="structural",
        associated_files=["world.yaml"],
        status="complete",
    )
    orphan = GoalRecord(
        description="gone.py was written without a planned goal — review it.",
        type="structural",
        associated_files=["gone.py"],  # NOT on disk
        origin="create_backfill",
    )
    mission = _mission(tmp_path, [arch_done, orphan])
    out = await action_structural_sweep_next(_si(mission))
    assert orphan.status == "complete"
    assert not out.result.get("needs_create")
    assert out.result.get("sweep_complete") is True


@pytest.mark.asyncio
async def test_sweep_still_creates_missing_architecture_file(tmp_path):
    # Guard: a NORMAL (design-origin) missing arch file is still created —
    # the create path is untouched for non-backfill goals.
    goal = GoalRecord(
        description="Create world.yaml with content: a world",
        type="structural",
        associated_files=["world.yaml"],  # NOT on disk
    )
    mission = _mission(tmp_path, [goal])
    out = await action_structural_sweep_next(_si(mission))
    assert out.result.get("needs_create") is True
    assert goal.status == "incomplete"  # not spuriously completed
