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
