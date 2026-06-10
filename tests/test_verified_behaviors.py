"""Verified-behavior immunity at the quality gate.

A behavior with a verified passing play-test must not be reported as
untested/broken just because the latest UX session didn't re-tour it —
otherwise "untested: X" findings re-open completed goals on every gate
pass (the harvester's fix-didn't-hold logic) and verified work
ping-pongs forever. quality_overview projects the verified list;
format_verified_behaviors renders it for the summarize prompt's
do-not-relitigate rule.
"""

from __future__ import annotations

from agent.formatters import format_verified_behaviors
from agent.persistence.models import GoalRecord, MissionConfig, MissionState
from agent.projections import project_quality_overview


def _mission() -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        goals=[
            GoalRecord(
                description="Player can move between rooms",
                type="functional",
                status="complete",
            ),
            GoalRecord(
                description="Save and load restores state",
                type="functional",
                status="incomplete",
            ),
            GoalRecord(
                description="Data classes exist",
                type="structural",
                status="complete",
            ),
        ],
    )


def test_quality_overview_projects_verified_functional_goals_only():
    overview = project_quality_overview(_mission(), {})
    assert overview["verified_behaviors"] == ["Player can move between rooms"]


def test_formatter_renders_block_and_empties_cleanly():
    overview = project_quality_overview(_mission(), {})
    block = format_verified_behaviors({"source": overview}, namespaces={})
    assert "VERIFIED passing play-test" in block
    assert "Player can move between rooms" in block
    assert "Save and load" not in block

    assert format_verified_behaviors({"source": {}}, namespaces={}) == ""
